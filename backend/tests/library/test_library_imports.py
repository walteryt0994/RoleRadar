"""Resume imports (U2-B1): reading, keeping for seven days, discarding and cleaning up.

All material is synthetic (import_samples.py). Files are read by the real
child process unless a test gives the runtime stand-in readings.
"""
import hashlib
import io
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError

import app.library.imports as imports_module
from app.library import migrate, resume_reader
from app.library.db import Database, LibraryBase, make_engine
from app.library.errors import ConfigError, StorageError, UnreadableFile
from app.library.files import LEFTOVER_AFTER
from app.library.identity import ensure_dev_users
from app.library.imports import (
    IMPORT_WINDOW,
    MAX_OPEN_IMPORTS,
    MAX_TEXT_BYTES,
    STALE_READING_AFTER,
    STALE_RECEIVING_AFTER,
    discard,
)
from app.library.models import (
    SCHEMA_VERSION,
    Attachment,
    AuditEvent,
    ExperienceItem,
    ExperienceVersion,
    ImportState,
    PurgeJob,
    ResumeImport,
    User,
    create_schema,
)
from app.library.resume_reader import (
    BUSY,
    CANCELLED,
    CRASHED,
    DAMAGED,
    EMPTY_PAGES,
    ENCRYPTED,
    MACROS,
    MAX_TEXT_CHARS,
    NO_TEXT,
    NOT_WORD,
    PARTIAL,
    PDF,
    READ,
    TIMEOUT,
    TOO_BIG_INSIDE,
    TOO_MANY_PAGES,
    TRUNCATED,
    UNREADABLE_PAGES,
    Readings,
    _refuse,
    read_docx,
    read_pdf,
    worker_command,
)
from app.web_main import create_app
from import_samples import (
    DOCX_MEDIA,
    INFLATING,
    OLE,
    UNREADABLE,
    bomb_docx,
    encrypted_pdf,
    make_docx,
    make_pdf,
    many_members_docx,
    p,
    table,
)
from library_helpers import LOCAL_BASE_URL, PNG, Browser, form_for

# The same start as conftest's clock.
START = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
RESUME_LINES = ["合成简历：张合成", "Python 数据分析 project, 2025", "Mixed 中英 line"]
PDF_FILE = ("合成简历.pdf", make_pdf([RESUME_LINES, ["第二页 Second page"]]), "application/pdf")
DOCX_BODY = (
    p("合成学生 Synthetic Student")
    + table([["学校", "合成大学"], ["专业", "Data Science 数据科学"]])
    + p("项目：校园活动报名工具（合成）")
)
DOCX_FILE = ("合成简历.docx", make_docx(DOCX_BODY), DOCX_MEDIA)
NO_FILE = ("", b"", "application/octet-stream")
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'


# --- helpers ------------------------------------------------------------------


def rt(app):
    return app.state.library


def owner_id(app, label: str = "合成用户 A") -> str:
    with rt(app).database.transaction() as db:
        return db.scalar(select(User.id).where(User.display_label == label))


def form_fields(browser) -> dict:
    """The import form's own fields (CSRF token, one-time key), as the page has them."""
    form = browser.form("/imports", "/imports")

    return {name: value for name, value in form.fields.items() if name not in ("file", "text", "consent")}


def send(browser, *, file=NO_FILE, text: str = "", consent: bool = True, fields: dict | None = None):
    """Fill the import page's form as a browser would and send it (a file input always sends a part)."""
    data = {**(fields or form_fields(browser)), "text": text}

    if consent:
        data["consent"] = "yes"

    return browser.client.post("/imports", data=data, files={"file": file})


def import_id_from(response) -> str:
    assert response.status_code == 303, response.text
    match = re.match(r"^/imports/([0-9a-f-]{36})", response.headers["location"])
    assert match, response.headers["location"]

    return match.group(1)


def row_of(app, import_id: str) -> ResumeImport | None:
    with rt(app).database.transaction() as db:
        return db.get(ResumeImport, import_id)


def all_rows(app) -> list[ResumeImport]:
    with rt(app).database.transaction() as db:
        return list(db.scalars(select(ResumeImport)))


def kept_files(app) -> list[Path]:
    return sorted(path for path in rt(app).files.imports_dir.rglob("*") if path.is_file())


def staging_files(app) -> list[Path]:
    return sorted(rt(app).files.staging_dir.iterdir())


def library_state(app):
    """Everything an import must never touch: experiences, versions, attachments."""
    with rt(app).database.transaction() as db:
        return (
            [(r.id, r.revision, r.state, r.content, r.private_note, r.deleted_at) for r in db.scalars(select(ExperienceItem))],
            [(r.id, r.content, r.deleted_at) for r in db.scalars(select(ExperienceVersion))],
            [(r.id, r.state, r.revision, r.deleted_at) for r in db.scalars(select(Attachment))],
        )


@pytest.fixture
def children(monkeypatch) -> list:
    """The reader child processes a test starts, kept to check that each one really ended."""
    started = []
    real = resume_reader.subprocess.Popen

    def tracked(*args, **kwargs):
        child = real(*args, **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(resume_reader.subprocess, "Popen", tracked)

    return started


def stand_in(script: str):
    """A reader command that runs script instead of the real reader."""
    return lambda kind: [sys.executable, "-I", "-c", script, kind]


def answer_script(reading: dict) -> str:
    return f"import sys, json; sys.stdin.buffer.read(); sys.stdout.write(json.dumps({reading!r}))"


# --- reading PDF files -----------------------------------------------------------


def test_a_text_pdf_is_read_page_by_page_with_mixed_languages():
    reading = read_pdf(make_pdf([RESUME_LINES, ["English only page"]]))

    assert reading.status == READ
    assert [segment["number"] for segment in reading.segments] == [1, 2]
    assert reading.segments[0]["text"].split("\n") == RESUME_LINES
    assert reading.segments[1]["text"] == "English only page"
    assert reading.pages == 2


def test_a_pdf_with_a_page_without_text_is_partial_and_says_which_page():
    reading = read_pdf(make_pdf([["合成第一页"], None, ["合成第三页"]]))

    assert reading.status == PARTIAL
    assert reading.notes == {EMPTY_PAGES: [2]}
    assert [segment["text"] for segment in reading.segments] == ["合成第一页", "", "合成第三页"]


@pytest.mark.parametrize(
    "data, failure",
    [
        (make_pdf([None, None]), NO_TEXT),
        (encrypted_pdf(), ENCRYPTED),
        (b"%PDF-1.7\n" + b"\x00garbled" * 40, DAMAGED),
        (make_pdf([["页"]] * 51), TOO_MANY_PAGES),
    ],
)
def test_a_pdf_that_cannot_be_read_says_why(data, failure):
    reading = read_pdf(data)

    assert reading.status == "failed" and reading.failure == failure
    assert reading.segments == []


def test_a_page_that_cannot_be_read_keeps_the_pages_around_it():
    reading = read_pdf(make_pdf([["合成第一页 page one"], UNREADABLE, ["合成第三页"]]))

    assert reading.status == PARTIAL and reading.notes == {UNREADABLE_PAGES: [2]}
    assert [(segment["text"], segment.get("unreadable", False)) for segment in reading.segments] == [
        ("合成第一页 page one", False),
        ("", True),
        ("合成第三页", False),
    ]


@pytest.mark.parametrize("pages", [[UNREADABLE, UNREADABLE], [UNREADABLE, None]])
def test_a_pdf_with_nothing_readable_left_still_fails(pages):
    reading = read_pdf(make_pdf(pages))

    assert reading.status == "failed" and reading.failure == DAMAGED and reading.segments == []


def test_a_page_past_the_inflation_limit_still_refuses_the_whole_file():
    # Read by the real child, which holds every stream to its own limit: a partial result gives none up.
    reading = Readings().read("inflating", PDF, make_pdf([["前面能读的合成页"], INFLATING]))

    assert reading.status == "failed" and reading.failure == TOO_BIG_INSIDE and reading.segments == []


def test_a_pdf_over_the_text_limit_is_cut_there_and_marked_partial():
    line = "合" * 1000
    reading = read_pdf(make_pdf([[line] * 60, [line] * 60]))

    assert reading.status == PARTIAL and reading.notes[TRUNCATED] is True
    assert reading.chars == MAX_TEXT_CHARS


# --- reading DOCX files ---------------------------------------------------------------


def test_a_docx_body_is_read_as_paragraphs_and_tables_in_order():
    body = (
        p("合成简历 Synthetic")
        + "<w:p><w:r><w:t>Python</w:t></w:r><w:r><w:tab/><w:t>SQL</w:t></w:r>"
        "<w:del><w:r><w:delText>删掉的旧内容</w:delText></w:r></w:del>"
        '<w:hyperlink r:id="rId9"><w:r><w:t> 作品链接</w:t></w:r></w:hyperlink>'
        '<w:r><w:instrText>HYPERLINK "http://isolation-probe.invalid"</w:instrText></w:r></w:p>'
        + table([["学校", "合成大学"], ["时间", "2023–2027"]])
        + "<w:p><w:r><mc:AlternateContent><mc:Choice Requires=\"wps\"><w:drawing><w:txbxContent>"
        + p("文本框里的话")
        + "</w:txbxContent></w:drawing></mc:Choice><mc:Fallback><w:pict><w:txbxContent>"
        + p("文本框里的话")
        + "</w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
        + p("")
        + p("Last line 最后一行")
    )
    reading = read_docx(make_docx(body))

    assert reading.status == READ
    assert reading.segments == [
        {"kind": "paragraph", "number": 1, "text": "合成简历 Synthetic"},
        {"kind": "paragraph", "number": 2, "text": "Python\tSQL 作品链接"},
        {"kind": "table", "number": 1, "rows": [["学校", "合成大学"], ["时间", "2023–2027"]]},
        # A text box is read once, not once more from its fallback copy.
        {"kind": "paragraph", "number": 3, "text": "文本框里的话"},
        {"kind": "paragraph", "number": 4, "text": "Last line 最后一行"},
    ]


@pytest.mark.parametrize(
    "data, failure",
    [
        (make_docx(p("合成"), main_type="application/vnd.ms-word.document.macroEnabled.main+xml"), MACROS),
        (make_docx(p("合成"), extra={"word/vbaProject.bin": b"synthetic"}), MACROS),
        (OLE, ENCRYPTED),
        (b"PK\x03\x04 broken zip", NOT_WORD),
        (make_docx(document='<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]><d>&e;</d>'), DAMAGED),
        (make_docx(document="<w:document xmlns:w='x'><unclosed></w:document>"), DAMAGED),
        (make_docx(p("")), NO_TEXT),
        (bomb_docx(11 * 1024 * 1024), TOO_BIG_INSIDE),
        (many_members_docx(501), TOO_BIG_INSIDE),
        (make_docx("<w:sdt><w:sdtContent>" * 120 + p("深") + "</w:sdtContent></w:sdt>" * 120), DAMAGED),
    ],
)
def test_a_docx_that_cannot_be_read_says_why(data, failure):
    reading = read_docx(data)

    assert reading.status == "failed" and reading.failure == failure


def test_a_docx_over_the_text_limit_is_cut_there_and_marked_partial():
    reading = read_docx(make_docx(p("合" * 60_000) + p("成" * 60_000)))

    assert reading.status == PARTIAL and reading.notes == {TRUNCATED: True}
    assert reading.chars == MAX_TEXT_CHARS


# --- the reading child process -----------------------------------------------------------


def test_the_real_reader_runs_in_an_isolated_child_and_reads_both_types():
    command = worker_command(PDF)
    readings = Readings()

    assert command[:3] == [sys.executable, "-I", "-B"]
    assert readings.read("pdf", PDF, PDF_FILE[1]).status == READ
    assert readings.read("docx", "docx", DOCX_FILE[1]).segments[1]["kind"] == "table"


def test_the_reader_child_gets_no_environment_of_the_app():
    script = (
        "import os, sys, json; sys.stdin.buffer.read(); names = sorted(os.environ); "
        "sys.stdout.write(json.dumps({'status': 'read', 'segments': [{'kind': 'page', 'number': 1, "
        "'text': ','.join(names) or 'none'}], 'notes': {}, 'failure': None, 'pages': 1}))"
    )
    reading = Readings(command=stand_in(script)).read("env", PDF, b"%PDF-")
    seen = set(reading.segments[0]["text"].split(","))

    assert reading.status == READ
    assert not seen & {"ROLERADAR_TEST_ROOT", "LIBRARY_AUDIT_LOG", "ROLERADAR_LIBRARY_DB_PATH", "PATH", "HOME"}


@pytest.mark.parametrize(
    "event, args",
    [
        ("socket.connect", (None, ("127.0.0.1", 80))),
        ("socket.getaddrinfo", ("isolation-probe.invalid", 80, 0, 0, 0)),
        ("subprocess.Popen", ("ls", ["ls"], None, None)),
        ("os.system", ("ls",)),
        ("open", ("/tmp/written-by-reader", "w", os.O_WRONLY | os.O_CREAT)),
        ("os.remove", ("/tmp/anything", -1)),
    ],
)
def test_the_reader_child_refuses_connections_programs_and_writes(event, args):
    with pytest.raises(PermissionError):
        _refuse(event, args)


def test_the_reader_child_may_still_read_files_it_needs():
    _refuse("open", ("/some/module.py", "r", os.O_RDONLY))


def test_a_reading_past_its_time_limit_is_ended_not_only_waited_for(children):
    readings = Readings(seconds=1, command=lambda kind: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    reading = readings.read("slow", PDF, b"%PDF-")

    assert reading.failure == TIMEOUT
    assert time.monotonic() - started < 15
    # The child itself was ended and collected, long before its 60 seconds.
    (child,) = children
    assert child.poll() == -signal.SIGKILL


def test_stopping_a_reading_ends_its_process_and_it_answers_cancelled(children):
    readings = Readings(seconds=30, command=lambda kind: [sys.executable, "-c", "import time; time.sleep(60)"])
    started = time.monotonic()
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("reading", readings.read("key", PDF, b"%PDF-")))
    worker.start()
    deadline = time.monotonic() + 10

    while not readings.running("key") and time.monotonic() < deadline:
        time.sleep(0.02)

    readings.stop("key")
    worker.join(15)

    assert not worker.is_alive() and result["reading"].failure == CANCELLED
    assert time.monotonic() - started < 15
    (child,) = children
    assert child.poll() == -signal.SIGKILL


def test_readings_beyond_the_parallel_limit_wait_then_give_up_as_busy():
    readings = Readings(parallel=1, seconds=30, command=lambda kind: [sys.executable, "-c", "import time; time.sleep(60)"])
    worker = threading.Thread(target=readings.read, args=("first", PDF, b"%PDF-"))
    worker.start()
    deadline = time.monotonic() + 10

    while not readings.running("first") and time.monotonic() < deadline:
        time.sleep(0.02)

    assert readings.read("second", PDF, b"%PDF-", queue_seconds=0.2).failure == BUSY

    readings.stop("first")
    worker.join(15)


@pytest.mark.parametrize(
    "script",
    [
        "import sys; sys.stdin.buffer.read(); sys.exit(3)",
        "import sys; sys.stdin.buffer.read(); sys.stdout.write('not json')",
        answer_script({"status": "read", "segments": [{"kind": "page", "number": 1, "text": "x" * (MAX_TEXT_CHARS + 1)}], "notes": {}, "failure": None, "pages": 1}),
        answer_script({"status": "read", "segments": [{"kind": "script", "number": 1, "text": "x"}], "notes": {}, "failure": None, "pages": 1}),
        answer_script({"status": "failed", "segments": [], "notes": {}, "failure": None, "pages": None}),
    ],
)
def test_an_answer_the_reader_should_not_give_counts_as_a_crash(script):
    assert Readings(command=stand_in(script)).read("odd", PDF, b"%PDF-").failure == CRASHED


# --- importing through the pages ----------------------------------------------------------


def test_a_pdf_import_is_read_and_shown_as_it_was_found(app, browser_a, clock):
    before = library_state(app)
    response = send(browser_a, file=PDF_FILE)
    import_id = import_id_from(response)
    page = browser_a.get(response.headers["location"])
    row = row_of(app, import_id)

    assert page.status_code == 200
    assert "已暂存，没有加入经历库。" in page.text
    assert "文字已读取" in page.text and "还没有加入经历库" in page.text
    assert "第 1 页" in page.text and "第 2 页" in page.text
    assert all(line in page.text for line in RESUME_LINES)
    assert row.state == ImportState.READ and row.source == "pdf" and row.filename == "合成简历.pdf"
    assert row.received_at == START and row.expires_at == START + IMPORT_WINDOW
    assert len(kept_files(app)) == 1 and staging_files(app) == []
    # Reading is not adding: the library stays exactly as it was.
    assert library_state(app) == before


def test_a_docx_import_shows_its_paragraphs_and_table(app, browser_a):
    import_id = import_id_from(send(browser_a, file=DOCX_FILE))
    page = browser_a.get(f"/imports/{import_id}").text

    assert "合成学生 Synthetic Student" in page and "Data Science 数据科学" in page
    assert '<table class="import-table">' in page and "表格 1" in page
    assert row_of(app, import_id).source == "docx"


def test_pasted_text_is_kept_as_it_was_pasted(app, browser_a):
    pasted = "张合成\r\n2025 合成大学 Data Science\r\n参与（不是主导）校园活动报名工具\x00"
    import_id = import_id_from(send(browser_a, text=pasted))
    row = row_of(app, import_id)
    page = browser_a.get(f"/imports/{import_id}").text

    assert row.source == "text" and row.storage_key is None and row.filename is None
    assert row.segments == [{"kind": "text", "number": 1, "text": "张合成\n2025 合成大学 Data Science\n参与（不是主导）校园活动报名工具"}]
    assert "粘贴的文字" in page and "参与（不是主导）校园活动报名工具" in page
    assert kept_files(app) == []


def test_a_failed_reading_explains_itself_and_offers_pasting_instead(app, browser_a):
    import_id = import_id_from(send(browser_a, file=("扫描件.pdf", make_pdf([None]), "application/pdf")))
    page = browser_a.get(f"/imports/{import_id}").text
    row = row_of(app, import_id)

    assert row.state == ImportState.FAILED and row.outcome == NO_TEXT
    assert "读取失败" in page and "扫描件或图片" in page and "改为粘贴简历文字" in page
    # Reading the same file again would not help.
    assert "/retry" not in page
    # The original stays downloadable until it expires or is discarded.
    assert 'href="/imports/%s/original"' % import_id in page
    # Discarding it clears the file; nothing was read, so nothing read is promised away.
    confirm = browser_a.get(f"/imports/{import_id}/discard").text
    assert "原文件“扫描件.pdf”。" in confirm and "读到的文字" not in confirm


def test_a_partial_reading_says_which_pages_had_no_text(app, browser_a):
    import_id = import_id_from(send(browser_a, file=("部分.pdf", make_pdf([["有字"], None]), "application/pdf")))
    page = browser_a.get(f"/imports/{import_id}").text

    assert "部分读取" in page and "第 2 页没有读到文字" in page and "这一页没有读到文字" in page


def test_a_pdf_with_an_unreadable_page_shows_what_was_read_and_what_was_not(app, browser_a):
    data = make_pdf([["合成第一页 page one"], UNREADABLE, ["合成第三页"]])
    import_id = import_id_from(send(browser_a, file=("部分损坏.pdf", data, "application/pdf")))
    page = browser_a.get(f"/imports/{import_id}").text
    read = browser_a.get(f"/imports/{import_id}/text").text
    row = row_of(app, import_id)

    assert row.state == ImportState.PARTIAL and row.details[UNREADABLE_PAGES] == [2]
    assert "部分读取" in page and "第 2 页无法读取" in page and "这一页无法读取" in page
    assert "合成第一页 page one" in page and "合成第三页" in page and "文字已读取" not in page
    assert read == "=== 第 1 页 ===\n合成第一页 page one\n\n=== 第 2 页（无法读取） ===\n\n=== 第 3 页 ===\n合成第三页\n"


@pytest.mark.parametrize(
    "file, status, words",
    [
        (("笔记.txt", b"plain", "text/plain"), 415, "只能导入文字可读的 PDF 或 Word"),
        (("旧简历.doc", OLE, "application/msword"), 415, "旧版 Word（.doc）"),
        (("图片.pdf", PNG, "application/pdf"), 415, "文件内容和它的扩展名对不上"),
        (("加密.docx", OLE, DOCX_MEDIA), 415, "设置了打开密码"),
        (("空.pdf", b"", "application/pdf"), 422, "这个文件是空的"),
        (("类型不符.pdf", PDF_FILE[1], "image/png"), 415, "扩展名和浏览器报告的文件类型对不上"),
        (("太大.pdf", b"%PDF-" + b"0" * (10 * 1024 * 1024), "application/pdf"), 413, "超过 10 MB"),
    ],
)
def test_a_file_that_is_refused_leaves_nothing_behind(app, browser_a, file, status, words):
    response = send(browser_a, file=file)

    assert response.status_code == status
    assert words in response.text
    assert all_rows(app) == [] and kept_files(app) == [] and staging_files(app) == []


def test_nothing_is_kept_without_consent(app, browser_a):
    for response in (send(browser_a, file=PDF_FILE, consent=False), send(browser_a, text="合成文字", consent=False)):
        assert response.status_code == 422 and "请先勾选同意暂存" in response.text

    assert all_rows(app) == [] and kept_files(app) == [] and staging_files(app) == []


def test_a_file_and_text_together_are_refused_and_the_text_is_kept(app, browser_a):
    response = send(browser_a, file=PDF_FILE, text="还在的合成文字")

    assert response.status_code == 422 and "只能二选一" in response.text
    assert "还在的合成文字</textarea>" in response.text and "data-initially-dirty" in response.text
    assert all_rows(app) == [] and staging_files(app) == []


def test_a_refused_file_still_gives_back_the_pasted_text(app, browser_a):
    response = send(browser_a, file=("旧简历.doc", OLE, "application/msword"), text="同时粘贴的文字")

    assert response.status_code == 415 and "同时粘贴的文字</textarea>" in response.text


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_an_empty_import_is_refused(app, browser_a, text):
    response = send(browser_a, text=text)

    assert response.status_code == 422 and "请选择一个文件，或者粘贴简历文字" in response.text
    assert all_rows(app) == []


def test_pasted_text_over_the_limit_is_refused(app, browser_a):
    over_chars = send(browser_a, text="a" * (MAX_TEXT_CHARS + 1))
    over_bytes = send(browser_a, text="合" * (MAX_TEXT_BYTES // 3 + 1))

    assert over_chars.status_code == 413 and over_bytes.status_code == 413
    assert "10 万字的上限" in over_chars.text and "10 万字的上限" in over_bytes.text
    assert all_rows(app) == []


def test_the_same_form_sent_twice_keeps_one_import(app, browser_a):
    fields = form_fields(browser_a)
    first = send(browser_a, file=PDF_FILE, fields=fields)
    again = send(browser_a, file=PDF_FILE, fields=fields)
    other = send(browser_a, file=("另一份.pdf", make_pdf([["别的合成内容"]]), "application/pdf"), fields=fields)

    assert import_id_from(again) == import_id_from(first)
    assert again.headers["location"].endswith("notice=import_already_added")
    assert other.status_code == 409 and "表单已经用过了" in other.text
    assert len(all_rows(app)) == 1 and len(kept_files(app)) == 1 and staging_files(app) == []


def test_the_same_form_sent_twice_at_once_keeps_one_import(app, browser_a):
    fields = form_fields(browser_a)

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: send(browser_a, file=PDF_FILE, fields=fields), range(2)))

    assert len({import_id_from(response) for response in responses}) == 1
    assert len(all_rows(app)) == 1 and len(kept_files(app)) == 1


def test_at_most_five_imports_are_kept_at_once(app, browser_a):
    # A form opened before the last place was taken.
    fields = form_fields(browser_a)

    for number in range(MAX_OPEN_IMPORTS):
        import_id_from(send(browser_a, text=f"合成文字 {number}"))

    page = browser_a.get("/imports").text
    refused = send(browser_a, text="第六份", fields=fields)

    assert "达到上限" in page and 'action="/imports"' not in page
    assert refused.status_code == 409 and "达到上限" in refused.text
    assert len(all_rows(app)) == MAX_OPEN_IMPORTS


def test_a_refusal_at_the_limit_gives_back_the_typed_text_with_its_guard(app, browser_a):
    # Forms opened in another tab, before the other tabs took the last place.
    stale = [form_fields(browser_a) for _ in range(2)]

    for number in range(MAX_OPEN_IMPORTS):
        import_id_from(send(browser_a, text=f"合成文字 {number}"))

    typed = "刚写好、还没暂存的合成文字\n第二行"
    refused = send(browser_a, text=typed, fields=stale[0])
    page = refused.text

    assert refused.status_code == 409 and len(all_rows(app)) == MAX_OPEN_IMPORTS
    assert f"{typed}</textarea>" in page
    assert "data-leave-guard" in page and "data-initially-dirty" in page and 'id="leave-dialog"' in page
    assert "可以先复制" in page and "新标签页" in page
    # The imports open in another tab, so freeing a place does not leave the typed text.
    assert page.count('target="_blank" rel="noopener"') == MAX_OPEN_IMPORTS

    # Any other refusal at the limit gives the text back the same way.
    both = send(browser_a, file=PDF_FILE, text=typed, fields=stale[1])
    assert both.status_code == 422 and f"{typed}</textarea>" in both.text

    # The limit still holds for the form the refusal shows ...
    again = {name: value for name, value in form_for(page, "/imports").fields.items() if name not in ("file", "text", "consent")}
    assert send(browser_a, text=typed, fields=again).status_code == 409
    assert len(all_rows(app)) == MAX_OPEN_IMPORTS

    # ... until a place is free; then the same text is kept.
    first = all_rows(app)[0].id
    browser_a.submit(f"/imports/{first}/discard", f"/imports/{first}/discard")
    kept = import_id_from(send(browser_a, text=typed, fields=again))
    assert row_of(app, kept).segments[0]["text"] == typed


def test_two_imports_racing_for_the_last_place_give_back_the_refused_text(app, browser_a):
    for number in range(MAX_OPEN_IMPORTS - 1):
        import_id_from(send(browser_a, text=f"合成文字 {number}"))

    forms = [form_fields(browser_a) for _ in range(2)]
    texts = ["甲标签页的合成文字", "乙标签页的合成文字"]

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda pair: send(browser_a, text=pair[1], fields=pair[0]), zip(forms, texts)))

    statuses = [response.status_code for response in responses]
    refused = statuses.index(409)

    assert sorted(statuses) == [303, 409]
    assert f"{texts[refused]}</textarea>" in responses[refused].text
    assert len(all_rows(app)) == MAX_OPEN_IMPORTS


def test_a_failed_store_leaves_no_file_and_no_record(app, browser_a, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rt(app).files, "place", refuse)
    response = send(browser_a, file=PDF_FILE)

    assert response.status_code == 500 and "暂存没有完成" in response.text and "留在本机" not in response.text
    assert all_rows(app) == [] and kept_files(app) == [] and staging_files(app) == []


def test_a_failed_upload_that_leaves_its_file_says_so_and_the_clean_up_removes_it(app, browser_a, monkeypatch):
    store = rt(app).files
    place, remove = store.place, store.remove_file

    def place_then_fail(*args, **kwargs):
        place(*args, **kwargs)
        raise OSError("synthetic failure after the file was placed")

    def cannot_remove(*args, **kwargs):
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(store, "place", place_then_fail)
    monkeypatch.setattr(store, "remove_file", cannot_remove)
    response = send(browser_a, file=PDF_FILE)
    (row,) = all_rows(app)

    assert response.status_code == 500
    assert "还有一份留在本机" in response.text and "自动重试清理" in response.text
    assert "没有留下" not in response.text and "已经删除" not in response.text
    # No content and no way in, but the row keeps what the clean-up needs.
    assert row.state == ImportState.DISCARDED and row.cleared_at is None and row.storage_key is not None
    assert (row.filename, row.segments, row.cleanup_attempts) == (None, None, 1)
    assert len(kept_files(app)) == 1 and staging_files(app) == []
    listing = browser_a.get("/imports").text
    assert "还在清理文件" in listing and "合成简历.pdf" not in listing

    monkeypatch.setattr(store, "remove_file", remove)
    rt(app).run_cleanup()

    assert all_rows(app) == [] and kept_files(app) == []


def test_a_failed_upload_whose_staging_copy_could_not_go_says_so_too(app, browser_a, monkeypatch):
    store = rt(app).files
    check = store.check_folder
    failing = []

    def reserve_fails(*args, **kwargs):
        failing.append(True)
        raise StorageError("database operation failed")

    def staging_refused(path):
        if failing and path == store.staging_dir:
            raise UnreadableFile()

        return check(path)

    monkeypatch.setattr(imports_module, "_reserve", reserve_fails)
    monkeypatch.setattr(store, "check_folder", staging_refused)
    response = send(browser_a, file=PDF_FILE)

    assert response.status_code == 500 and "还有一份留在本机" in response.text
    assert all_rows(app) == [] and len(staging_files(app)) == 1

    # The sweep removes it once it is old.
    monkeypatch.setattr(store, "check_folder", check)
    old = time.time() - LEFTOVER_AFTER - 60
    os.utime(staging_files(app)[0], (old, old))
    rt(app).run_cleanup()

    assert staging_files(app) == []


# --- keeping, expiring and discarding --------------------------------------------------------


def test_looking_and_reading_again_never_move_the_seven_days(app, browser_a, clock):
    readings = rt(app).readings
    rt(app).readings = Readings(command=stand_in("import sys; sys.stdin.buffer.read(); sys.exit(1)"))
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    assert row_of(app, import_id).outcome == CRASHED

    clock.advance(days=3)
    # A sign-in lasts 12 hours; the import outlives it.
    browser_a.sign_in("合成用户 A")
    page = browser_a.get(f"/imports/{import_id}").text
    rt(app).readings = readings
    retry = browser_a.submit(f"/imports/{import_id}", f"/imports/{import_id}/retry")
    row = row_of(app, import_id)

    assert "重新读取" in page and retry.status_code == 303
    assert row.state == ImportState.READ and row.expires_at == START + IMPORT_WINDOW


def test_reading_again_needs_the_page_it_was_offered_on(app, browser_a):
    rt(app).readings = Readings(command=stand_in("import sys; sys.stdin.buffer.read(); sys.exit(1)"))
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    form = browser_a.form(f"/imports/{import_id}", f"/imports/{import_id}/retry")
    stale = browser_a.post(f"/imports/{import_id}/retry", {**form.fields, "expected_revision": "1"})

    assert stale.status_code == 409 and "不能重新读取" in stale.text
    assert row_of(app, import_id).state == ImportState.FAILED


def test_an_import_ends_for_every_page_the_moment_it_expires(app, browser_a, clock):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")

    for path in ("", "/original", "/text", "/discard"):
        response = browser_a.get(f"/imports/{import_id}{path}")
        assert response.status_code == 404 and "找不到这次导入" in response.text, path

    assert import_id not in browser_a.get("/imports").text
    # Not cleaned up yet, and honest about it.
    assert "还在清理文件" in browser_a.get("/imports").text and len(kept_files(app)) == 1

    rt(app).run_cleanup()
    row = row_of(app, import_id)

    assert row.state == ImportState.EXPIRED and row.cleared_at == START + timedelta(days=7)
    assert (row.filename, row.segments, row.sha256, row.storage_key) == (None, None, None, None)
    assert kept_files(app) == [] and "还在清理文件" not in browser_a.get("/imports").text


def test_a_cleaned_up_import_is_forgotten_after_thirty_days(app, browser_a, clock):
    import_id = import_id_from(send(browser_a, text="合成"))
    clock.advance(days=7)
    rt(app).run_cleanup()
    clock.advance(days=30)
    rt(app).run_cleanup()

    assert row_of(app, import_id) is None


def test_discarding_explains_itself_then_clears_everything_at_once(app, browser_a):
    before = library_state(app)
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    confirm = browser_a.get(f"/imports/{import_id}/discard").text
    response = browser_a.submit(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")
    row = row_of(app, import_id)

    assert "原文件“合成简历.pdf”" in confirm and "经历库、基本资料、求职偏好和其他导入都不受影响" in confirm
    assert "没有 7 天恢复期" in confirm
    assert response.headers["location"] == "/imports?notice=import_discarded"
    assert "原文件和读到的文字已经从本机删除" in browser_a.get(response.headers["location"]).text
    assert row.state == ImportState.DISCARDED and row.cleared_at is not None
    assert (row.filename, row.segments, row.details, row.storage_key) == (None, None, None, None)
    assert kept_files(app) == []
    assert browser_a.get(f"/imports/{import_id}/original").status_code == 404
    assert library_state(app) == before


def test_a_discard_whose_file_cannot_go_yet_says_so_and_is_finished_later(app, browser_a, monkeypatch):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    store = rt(app).files
    remove = store.remove_file

    def refuse(*args, **kwargs):
        raise OSError("busy disk")

    monkeypatch.setattr(store, "remove_file", refuse)
    response = browser_a.submit(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")
    row = row_of(app, import_id)

    assert response.headers["location"] == "/imports?notice=import_discard_pending"
    assert "文件清理暂未完成" in browser_a.get(response.headers["location"]).text
    assert row.segments is None and row.filename is None and row.cleared_at is None
    assert (row.cleanup_attempts, row.cleanup_error) == (1, "OSError")
    assert browser_a.get(f"/imports/{import_id}").status_code == 404
    assert "还在清理文件" in browser_a.get("/imports").text

    monkeypatch.setattr(store, "remove_file", remove)
    rt(app).run_cleanup()

    assert row_of(app, import_id).cleared_at is not None and kept_files(app) == []


def test_a_reading_that_ends_after_a_discard_is_dropped(app, browser_a, monkeypatch):
    readings = rt(app).readings
    read = readings.read

    def discarded_meanwhile(key, kind, data, **options):
        # The user discards the import in another tab while it is being read.
        discard(rt(app).database, rt(app).files, readings, owner_id(app), key, rt(app).now())
        return read(key, kind, data, **options)

    monkeypatch.setattr(readings, "read", discarded_meanwhile)
    response = send(browser_a, file=PDF_FILE)
    (row,) = all_rows(app)

    assert response.headers["location"] == "/imports?notice=import_ended"
    assert row.state == ImportState.DISCARDED and row.segments is None and row.cleared_at is not None
    assert kept_files(app) == []


def test_a_restarted_app_finds_the_import_and_its_file(app, browser_a, make_config, clock):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    again = create_app(make_config(), clock=clock)

    with TestClient(again, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        browser = Browser.signed_in(again, "合成用户 A")
        page = browser.get(f"/imports/{import_id}")
        original = browser.get(f"/imports/{import_id}/original")

    assert page.status_code == 200 and RESUME_LINES[0] in page.text
    assert original.content == PDF_FILE[1]
    assert row_of(app, import_id).expires_at == START + IMPORT_WINDOW


def test_a_reading_cut_off_by_a_restart_can_be_read_again(app, browser_a, clock):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))

    with rt(app).database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        row.state, row.segments, row.updated_at = ImportState.READING, None, START

    clock.advance(seconds=STALE_READING_AFTER.total_seconds())
    rt(app).run_cleanup()
    page = browser_a.get(f"/imports/{import_id}").text

    assert row_of(app, import_id).outcome == "interrupted" and "读取被中断了" in page
    assert browser_a.submit(f"/imports/{import_id}", f"/imports/{import_id}/retry").status_code == 303
    assert row_of(app, import_id).state == ImportState.READ


def test_an_upload_left_unplaced_is_removed_with_its_file(app, browser_a, clock):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))

    with rt(app).database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        row.state, row.updated_at = ImportState.RECEIVING, START

    clock.advance(seconds=STALE_RECEIVING_AFTER.total_seconds())
    rt(app).run_cleanup()

    assert row_of(app, import_id) is None and kept_files(app) == []


def test_the_leftover_sweep_keeps_import_files_that_a_record_still_uses(app, browser_a):
    import_id_from(send(browser_a, file=PDF_FILE))
    (kept,) = kept_files(app)
    stray = kept.parent / ("e" * 32)
    stray.write_bytes(b"synthetic leftover")
    old = time.time() - LEFTOVER_AFTER - 60

    for path in (kept, stray):
        os.utime(path, (old, old))

    rt(app).run_cleanup()

    assert kept_files(app) == [kept]


# --- downloads ---------------------------------------------------------------------------------


def test_the_original_and_the_text_download_as_files_never_as_pages(app, browser_a):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    original = browser_a.get(f"/imports/{import_id}/original")
    read = browser_a.get(f"/imports/{import_id}/text")

    assert original.content == PDF_FILE[1] and original.headers["content-type"] == "application/pdf"
    assert read.headers["content-type"] == "text/plain; charset=utf-8"

    for response in (original, read):
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    assert read.text.startswith("=== 第 1 页 ===\n" + "\n".join(RESUME_LINES))
    assert "%E8%AF%BB%E5%88%B0%E7%9A%84%E6%96%87%E5%AD%97" in read.headers["content-disposition"]


def test_hostile_names_and_text_are_shown_as_text(app, browser_a):
    file_id = import_id_from(send(browser_a, file=(f"{HOSTILE}../../etc/简历.pdf", PDF_FILE[1], "application/pdf")))
    text_id = import_id_from(send(browser_a, text=f"合成 {HOSTILE}"))
    pages = [browser_a.get(f"/imports/{file_id}").text, browser_a.get(f"/imports/{text_id}").text, browser_a.get("/imports").text]

    assert row_of(app, file_id).filename == "简历.pdf"
    assert all("<script>alert" not in page and "<img src=x" not in page for page in pages)
    assert "&lt;script&gt;" in pages[1]
    assert all(path.name not in ("简历.pdf", "passwd") for path in kept_files(app))


# --- who may do what ----------------------------------------------------------------------------


def test_another_user_finds_nothing_of_an_import(app, browser_a, browser_b):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    before = row_of(app, import_id)
    token = browser_b.csrf_token()

    for path in ("", "/original", "/text", "/discard"):
        response = browser_b.get(f"/imports/{import_id}{path}")
        assert response.status_code == 404 and RESUME_LINES[0] not in response.text, path

    for action in ("discard", "retry"):
        response = browser_b.post(f"/imports/{import_id}/{action}", {"csrf_token": token, "expected_revision": "2"})
        assert response.status_code == 404, action

    assert "合成简历.pdf" not in browser_b.get("/imports").text
    after = row_of(app, import_id)
    assert (after.state, after.revision, after.segments) == (before.state, before.revision, before.segments)


def test_imports_need_a_session_and_its_token(app, browser_a, browser_b):
    import_id = import_id_from(send(browser_a, text="合成"))
    anonymous = Browser(app)
    fields = form_fields(browser_a)

    for path in ("/imports", f"/imports/{import_id}", f"/imports/{import_id}/text", f"/imports/{import_id}/discard"):
        assert anonymous.get(path).headers.get("location") == "/login", path

    assert anonymous.client.post("/imports", data={**fields, "text": "匿名", "consent": "yes"}, files={"file": NO_FILE}).status_code == 401
    forged = browser_a.client.post("/imports", data={**fields, "csrf_token": browser_b.csrf_token(), "text": "伪造", "consent": "yes"}, files={"file": PDF_FILE})
    other_site = browser_a.client.post(
        "/imports",
        data={**fields, "text": "跨站", "consent": "yes"},
        files={"file": NO_FILE},
        headers={"origin": "http://evil.invalid"},
    )
    discard_forged = browser_a.post(f"/imports/{import_id}/discard", {"csrf_token": "guess"})

    assert (forged.status_code, other_site.status_code, discard_forged.status_code) == (403, 403, 403)
    assert len(all_rows(app)) == 1 and row_of(app, import_id).state == ImportState.READ
    assert staging_files(app) == []


# --- the pages -------------------------------------------------------------------------------------


def test_the_import_page_asks_for_consent_and_guards_unsaved_input(app, browser_a):
    page = browser_a.get("/imports").text
    subnav = page[page.index('<nav class="subnav"'): page.index("</nav>", page.index('<nav class="subnav"'))]

    assert '<a href="/imports" aria-current="page">导入简历</a>' in subnav
    assert 'enctype="multipart/form-data"' in page and "data-leave-guard" in page and 'id="leave-dialog"' in page
    assert 'name="consent" value="yes" required' in page and "最多 7 天" in page and "不会发给外部 AI" in page
    assert f'maxlength="{MAX_TEXT_CHARS}"' in page and 'accept=".pdf,.docx"' in page
    assert page.index('name="file"') < page.index('name="text"') < page.index('name="consent"')
    assert '<script src="/static/leave_guard.js"></script>' in page and '<script src="/static/import.js"></script>' in page


def test_the_list_shows_each_import_with_its_deadline(app, browser_a, clock):
    import_id_from(send(browser_a, file=PDF_FILE))
    clock.advance(hours=1)
    import_id_from(send(browser_a, text="合成"))
    page = browser_a.get("/imports").text

    assert page.index("粘贴的文字") < page.index("合成简历.pdf")
    assert page.count("保留到") == 2 and "还剩 6 天 23 小时" in page and "还剩 7 天 0 小时" in page


def test_no_page_offers_what_is_not_built_yet(app, browser_a):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    page = browser_a.get(f"/imports/{import_id}").text
    posts = re.findall(r'<form method="post" action="([^"]+)"', page)

    assert posts == ["/logout"]
    assert "加入经历库</button>" not in page and "保存草稿" not in page


# --- taking it away in "export all" ------------------------------------------------------------------


def test_export_all_holds_the_kept_imports_and_says_they_are_temporary(app, browser_a):
    kept_id = import_id_from(send(browser_a, file=PDF_FILE))
    ended_id = import_id_from(send(browser_a, text="会放弃的合成文字"))
    browser_a.submit(f"/imports/{ended_id}/discard", f"/imports/{ended_id}/discard")
    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    data = json.loads(files["data.json"])
    readme = files["README.md"].decode("utf-8")
    (entry,) = manifest["imports"]

    assert manifest["format_version"] == 4 and manifest["counts"]["imports"] == 1 and manifest["complete"]
    assert entry["import_id"] == kept_id and entry["original_status"] == "included"
    assert files[entry["original_path"]] == PDF_FILE[1]
    assert files[entry["text_path"]].decode("utf-8").startswith("=== 第 1 页 ===")
    assert [record["id"] for record in data["imports"]] == [kept_id]
    assert "导入中的临时资料" in readme and "还没有加入经历库" in readme
    assert "会放弃的合成文字" not in json.dumps(data, ensure_ascii=False)
    assert "已放弃或已到期的简历导入" in manifest["not_included"]


def test_export_all_says_when_an_import_file_is_missing(app, browser_a):
    import_id_from(send(browser_a, file=PDF_FILE))
    kept_files(app)[0].unlink()
    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    manifest = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith("/manifest.json"))))

    assert manifest["complete"] is False and manifest["imports"][0]["original_status"] == "missing"


# --- the structure upgrade from version 3 ---------------------------------------------------------


def version_3_file(path: Path, clock) -> dict:
    """A version 3 library as U2-A left it, with a row in each of its tables; returns its row counts.

    Version 4 only adds resume_imports, so a version 4 file without that
    table and with version 3 is a version 3 file.
    """
    engine = make_engine(path)
    create_schema(engine)
    database = Database(engine)
    now = clock.now()
    item, version, attachment = (str(uuid.uuid4()) for _ in range(3))

    with database.transaction() as db:
        ensure_dev_users(db, now)

    with database.transaction() as db:
        user = db.scalar(select(User.id).order_by(User.id))
        stamps = {"created_at": now, "updated_at": now}
        gone = {"deleted_at": None, "purge_after": None, "deletion_id": None}
        db.add(
            ExperienceItem(
                id=item, owner_id=user, category="project", state="draft", revision=1, content={"title": "合成项目"},
                private_note="合成备注", confirmed_version_id=None, confirmation_count=0, last_confirmed_at=None,
                **stamps, **gone,
            )
        )
        db.flush()
        db.add(
            ExperienceVersion(
                id=version, item_id=item, owner_id=user, kind="initial", number=1, content={"title": "合成项目"},
                private_note="", source_revision=1, created_at=now, **gone,
            )
        )
        db.add(
            Attachment(
                id=attachment, owner_id=user, item_id=item, storage_key="a" * 32, filename="证明.pdf",
                media_type="application/pdf", size=5, sha256="b" * 64, state="ready",
                upload_key="synthetic-upload-key-0001", revision=1, **stamps, **gone,
            )
        )
        db.add(
            PurgeJob(
                id=str(uuid.uuid4()), owner_id=user, deletion_id=str(uuid.uuid4()), target_type="experience",
                target_id=item, item_id=item, category="project", reason="user_request", status="done",
                due_at=now, attempts=1, last_error=None, completed_at=now, **stamps,
            )
        )
        db.add(AuditEvent(owner_id=user, item_id=item, object_type="experience", object_id=item, action="created", at=now))

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE resume_imports"))
        connection.execute(text("UPDATE library_meta SET value = '3' WHERE key = 'schema_version'"))

    with engine.connect() as connection:
        names = [name for name in inspect(connection).get_table_names()]
        counts = {name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one() for name in names}

    engine.dispose()

    return counts


def meta_of(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT key, value FROM library_meta"))


def tables_of(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_app_refuses_a_version_3_file_and_says_how_to_upgrade_it(make_config, tmp_path, clock):
    path = tmp_path / "library.db"
    version_3_file(path, clock)
    digest = digest_of(path)

    with pytest.raises(ConfigError, match=r"结构版本是 3.*python -m app\.library\.migrate --db"):
        create_app(make_config(db_path=path), clock=clock)

    assert digest_of(path) == digest


def test_checking_a_version_3_file_changes_nothing(tmp_path, clock, capsys):
    path = tmp_path / "library.db"
    version_3_file(path, clock)
    digest = digest_of(path)

    assert migrate.main(["--db", str(path)]) == 0
    assert "这次只检查，没有修改文件" in capsys.readouterr().out
    assert digest_of(path) == digest and meta_of(path)["schema_version"] == "3"


def test_upgrading_a_version_3_file_keeps_every_row_and_its_identity(make_config, tmp_path, clock, capsys):
    path = tmp_path / "library.db"
    counts = version_3_file(path, clock)
    library_id = meta_of(path)["library_id"]

    assert migrate.main(["--db", str(path), "--apply"]) == 0
    out = capsys.readouterr().out
    engine = make_engine(path)

    with engine.connect() as connection:
        after = {name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one() for name in counts}
        new = connection.execute(text("SELECT count(*) FROM resume_imports")).scalar_one()

    engine.dispose()

    assert "已升级" in out and "原有各表行数不变" in out
    assert meta_of(path) == {"schema_version": SCHEMA_VERSION, "library_id": library_id}
    assert after == counts and new == 0
    assert tables_of(path) == set(LibraryBase.metadata.tables)
    # Running it again changes nothing, and the app opens the file.
    digest = digest_of(path)
    assert migrate.main(["--db", str(path), "--apply"]) == 0 and digest_of(path) == digest
    create_app(make_config(db_path=path), clock=clock).state.library.database.engine.dispose()


def test_a_failed_upgrade_leaves_the_file_as_it_was(tmp_path, clock, monkeypatch, capsys):
    path = tmp_path / "library.db"
    counts = version_3_file(path, clock)
    real_create = ResumeImport.__table__.create

    def create_then_fail(connection, *args, **kwargs):
        real_create(connection, *args, **kwargs)
        raise OperationalError("CREATE", None, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(ResumeImport.__table__, "create", create_then_fail)

    assert migrate.main(["--db", str(path), "--apply"]) == migrate.FAILED
    assert "已回滚" in capsys.readouterr().err
    assert meta_of(path)["schema_version"] == "3" and "resume_imports" not in tables_of(path)
    assert set(counts) == tables_of(path)


@pytest.mark.parametrize("version", ["2", "5"])
def test_other_versions_are_refused_untouched(tmp_path, clock, version):
    path = tmp_path / "library.db"
    version_3_file(path, clock)

    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE library_meta SET value = ? WHERE key = 'schema_version'", (version,))

    digest = digest_of(path)

    # Checking says so too, rather than promising an upgrade that would then be refused.
    assert migrate.main(["--db", str(path)]) == migrate.REFUSED
    assert migrate.main(["--db", str(path), "--apply"]) == migrate.REFUSED
    assert digest_of(path) == digest


def test_files_that_are_not_libraries_are_refused_and_none_is_created(tmp_path):
    other = tmp_path / "other.db"

    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")

    digest = digest_of(other)
    missing = tmp_path / "missing.db"

    assert migrate.main(["--db", str(other), "--apply"]) == migrate.REFUSED
    assert migrate.main(["--db", str(missing), "--apply"]) == migrate.REFUSED
    assert digest_of(other) == digest and not missing.exists()
