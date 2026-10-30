"""Private attachments: upload and download, limits, access, and failures halfway.

Every file is synthetic: a few bytes that only start the way PDF, PNG or JPEG
files must. Each app keeps its attachment folder under tmp_path, inside the
isolated test root; nothing else on disk is read or written.
"""
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from app.library import attachments as attachment_service
from app.library import web
from app.library.content import EXPERIENCE_CATEGORIES
from app.library.errors import StorageError
from app.library.files import FileStore
from app.library.models import Attachment, AttachmentState, AuditEvent, ExperienceItem, ExperienceVersion
from library_helpers import JPEG, PDF, PNG, Browser, form_for

MiB = 1024 * 1024
LIMIT = 10 * MiB


# --- helpers ---------------------------------------------------------------------


def database(app):
    return app.state.library.database


def store(app) -> FileStore:
    return app.state.library.files


def rows(app) -> list[Attachment]:
    with database(app).transaction() as db:
        return list(db.scalars(select(Attachment).order_by(Attachment.created_at, Attachment.id)))


def kept_files(app) -> list[Path]:
    return sorted(path for path in store(app).files_dir.rglob("*") if not path.is_dir())


def leftovers(app) -> list[Path]:
    return sorted([*store(app).staging_dir.iterdir(), *store(app).exports_dir.iterdir()])


def assert_nothing_kept(app):
    assert rows(app) == []
    assert kept_files(app) == []
    assert leftovers(app) == []


def upload_action(item_id: str) -> str:
    return f"/experiences/{item_id}/attachments"


def multipart(*parts, boundary="u1c-test-boundary") -> tuple[bytes, str]:
    """A multipart body from (name, filename or None, content type or None, bytes) parts, in order."""
    body = b""

    for name, filename, kind, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
        head = f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"

        if kind:
            head += f"Content-Type: {kind}\r\n"

        body += head.encode("utf-8") + b"\r\n" + data + b"\r\n"

    return body + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def form_fields(browser, item_id) -> dict[str, str]:
    fields = browser.form(f"/experiences/{item_id}", upload_action(item_id)).fields

    return {name: value for name, value in fields.items() if name != "file"}


def post_raw(browser, item_id, body: bytes, content_type: str, **headers):
    return browser.client.post(
        upload_action(item_id),
        content=body,
        headers={"Content-Type": content_type, **headers},
    )


def create_of(browser, category: str) -> str:
    form = browser.form(f"/experiences/new?kind={category}", "/experiences")
    field = next(name for name in form.fields if name not in ("csrf_token", "category", "next", "after_save", "private_note"))
    response = browser.post("/experiences", {**form.fields, field: f"{category} 的经历"})
    assert response.status_code == 303, response.text

    return response.headers["location"].split("/")[2].split("?")[0]


# --- normal use ------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename, data, media_type, label",
    [
        ("成绩单.pdf", PDF, "application/pdf", "PDF"),
        ("截图.png", PNG, "image/png", "PNG"),
        ("照片.JPEG", JPEG, "image/jpeg", "JPEG"),
    ],
)
def test_pdf_png_and_jpeg_come_back_byte_for_byte(app, browser_a, filename, data, media_type, label):
    item_id = browser_a.create(title="附件经历", actions="做了一件事")
    response = browser_a.upload(item_id, filename, data)

    assert response.status_code == 303
    assert response.headers["location"] == f"/experiences/{item_id}?notice=attachment_added#attachments"

    page = browser_a.get(response.headers["location"]).text
    [row] = rows(app)

    assert "已上传附件。" in page
    assert f'<span class="attachment-name">{filename}</span>' in page
    assert f"{label} · {len(data)} 字节" in page
    assert (row.state, row.filename, row.media_type, row.size) == (AttachmentState.READY, filename, media_type, len(data))

    download = browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download")

    assert download.status_code == 200
    assert download.content == data
    assert download.headers["content-type"] == media_type
    assert download.headers["content-disposition"].startswith('attachment; filename="')
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert leftovers(app) == []


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_every_category_of_saved_experience_takes_attachments(app, browser_a, category):
    item_id = create_of(browser_a, category)

    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    assert "证明.pdf" in browser_a.get(f"/experiences/{item_id}").text
    assert [row.item_id for row in rows(app)] == [item_id]


def test_the_profile_and_the_preferences_have_no_attachments(app, browser_a):
    assert browser_a.submit("/basic-profile/edit", "/basic-profile", name="合成名字").status_code == 303

    with database(app).transaction() as db:
        profile_id = db.scalar(select(ExperienceItem.id).where(ExperienceItem.category == "profile"))

    body, kind = multipart(
        ("csrf_token", None, None, browser_a.csrf_token().encode()),
        ("upload_key", None, None, b"k" * 32),
        ("file", "证明.pdf", "application/pdf", PDF),
    )

    assert post_raw(browser_a, profile_id, body, kind).status_code == 404
    assert 'enctype="multipart/form-data"' not in browser_a.get("/basic-profile").text
    assert browser_a.get(f"/experiences/{profile_id}").status_code == 404
    assert_nothing_kept(app)


def test_a_record_needs_no_attachment_and_uploading_never_touches_its_text(app, browser_a):
    item_id = browser_a.create(title="只有文字", actions="第一版")
    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    page = browser_a.get(f"/experiences/{item_id}").text

    assert "还没有附件。" in page
    assert "现在还能添加 10 个" in page

    with database(app).transaction() as db:
        item = db.get(ExperienceItem, item_id)
        before = (item.revision, item.state, dict(item.content), item.confirmed_version_id)
        versions = [(v.id, v.content) for v in db.scalars(select(ExperienceVersion))]

    assert browser_a.upload(item_id, "证明.pdf").status_code == 303

    with database(app).transaction() as db:
        item = db.get(ExperienceItem, item_id)
        after = (item.revision, item.state, dict(item.content), item.confirmed_version_id)
        versions_after = [(v.id, v.content) for v in db.scalars(select(ExperienceVersion))]
        actions = [event.action for event in db.scalars(select(AuditEvent).where(AuditEvent.item_id == item_id))]

    assert after == before
    assert versions_after == versions
    assert actions[-1] == "attachment_added"
    assert "添加一个附件" in browser_a.get(f"/experiences/{item_id}/history").text


# --- what the upload refuses ---------------------------------------------------


@pytest.mark.parametrize(
    "filename, data, declared, status, message",
    [
        ("空的.pdf", b"", "application/pdf", 422, "这个文件是空的"),
        ("简历.docx", b"PK\x03\x04docx", None, 415, "只能上传 PDF、PNG 或 JPEG"),
        ("图.svg", b"<svg/>", None, 415, "只能上传 PDF、PNG 或 JPEG"),
        ("页面.html", b"<html>", None, 415, "只能上传 PDF、PNG 或 JPEG"),
        ("程序.exe", b"MZ", None, 415, "只能上传 PDF、PNG 或 JPEG"),
        ("没有扩展名", PDF, "application/pdf", 415, "只能上传 PDF、PNG 或 JPEG"),
        ("报告.pdf", PDF, "image/png", 415, "扩展名和浏览器报告的文件类型对不上"),
        ("报告.pdf", PDF, "application/octet-stream", 415, "扩展名和浏览器报告的文件类型对不上"),
        ("改名的图片.pdf", PNG, "application/pdf", 415, "文件内容和它的扩展名对不上"),
        ("改名的文档.png", PDF, "image/png", 415, "文件内容和它的扩展名对不上"),
        ("改名的文档.jpg", PDF, "image/jpeg", 415, "文件内容和它的扩展名对不上"),
        ("...", PDF, "application/pdf", 422, "没有可用的文件名"),
    ],
)
def test_files_the_upload_refuses_leave_nothing_behind(app, browser_a, filename, data, declared, status, message):
    item_id = browser_a.create(actions="经历")
    response = browser_a.upload(item_id, filename, data, declared)

    assert response.status_code == status
    assert message in response.text
    assert "已上传" not in response.text
    # The page is shown again with a new form; the browser cannot fill in the file again.
    assert "请重新选择文件" in response.text or "只能上传" in response.text
    assert form_for(response.text, upload_action(item_id)).fields["upload_key"]
    assert_nothing_kept(app)


def test_the_size_limit_is_ten_mebibytes_exactly(app, browser_a):
    item_id = browser_a.create(actions="大文件")
    largest = b"%PDF-" + bytes(LIMIT - 5)

    assert browser_a.upload(item_id, "刚好.pdf", largest).status_code == 303

    [row] = rows(app)

    assert row.size == LIMIT
    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").content == largest

    response = browser_a.upload(item_id, "多一个字节.pdf", largest + b"x")

    assert response.status_code == 413
    assert "超过 10 MB" in response.text
    assert [row.filename for row in rows(app)] == ["刚好.pdf"]
    assert len(kept_files(app)) == 1
    assert leftovers(app) == []


def test_a_body_must_be_exactly_as_long_as_it_says(app, browser_a):
    item_id = browser_a.create(actions="经历")
    fields = form_fields(browser_a, item_id)
    body, kind = multipart(
        ("csrf_token", None, None, fields["csrf_token"].encode()),
        ("upload_key", None, None, fields["upload_key"].encode()),
        ("file", "证明.pdf", "application/pdf", PDF),
    )

    def chunks():
        yield body

    # No length at all (a chunked body): refused before anything is read.
    assert browser_a.client.post(upload_action(item_id), content=chunks(), headers={"Content-Type": kind}).status_code == 411
    # Announcing more than an upload may be.
    assert post_raw(browser_a, item_id, body, kind, **{"Content-Length": str(LIMIT + 64 * 1024 + 1)}).status_code == 413

    # More bytes than announced, or fewer (the upload broke off).
    longer = post_raw(browser_a, item_id, body, kind, **{"Content-Length": str(len(body) - 40)})
    shorter = post_raw(browser_a, item_id, body[:-40], kind, **{"Content-Length": str(len(body))})

    assert (longer.status_code, shorter.status_code) == (400, 400)
    assert "声明的长度不一致" in longer.text
    assert "不完整" in shorter.text
    assert_nothing_kept(app)

    # The same body with its true length is accepted: the requests above were well formed.
    assert post_raw(browser_a, item_id, body, kind).status_code == 303


def test_ordinary_forms_keep_their_small_limit(app, browser_a):
    item_id = browser_a.create(actions="经历")
    form = browser_a.form(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft")
    big = "长" * (100 * 1024)

    assert browser_a.post(f"/experiences/{item_id}/draft", {**form.fields, "actions": big}).status_code == 413
    # A multipart post to any other path gets the ordinary limit too.
    body, kind = multipart(("file", "证明.pdf", "application/pdf", b"%PDF-" + bytes(300 * 1024)))

    assert browser_a.client.post(f"/experiences/{item_id}/draft", content=body, headers={"Content-Type": kind}).status_code == 413


@pytest.mark.parametrize(
    "case, status, message",
    [
        ("extra field", 400, "不认识的内容"),
        ("two files", 400, "不认识的内容"),
        ("file under a field name", 400, "不认识的内容"),
        ("long field", 400, "不认识的内容"),
        ("no key", 400, "上传表单不完整"),
        ("bad key", 400, "上传表单不完整"),
        ("no file", 400, "没有收到文件"),
        ("broken body", 400, "不完整或格式不对"),
        ("not multipart", 400, "不是附件上传表单"),
    ],
)
def test_posts_that_are_not_the_upload_form_are_refused(app, browser_a, case, status, message):
    item_id = browser_a.create(actions="经历")
    fields = form_fields(browser_a, item_id)
    token = ("csrf_token", None, None, fields["csrf_token"].encode())
    key = ("upload_key", None, None, fields["upload_key"].encode())
    file = ("file", "证明.pdf", "application/pdf", PDF)
    parts = {
        "extra field": [token, key, ("owner_id", None, None, b"someone-else"), file],
        "two files": [token, key, file, ("file", "第二个.pdf", "application/pdf", PDF)],
        "file under a field name": [token, ("upload_key", "k.pdf", "application/pdf", PDF), file],
        "long field": [token, ("upload_key", None, None, b"k" * 300), file],
        "no key": [token, file],
        "bad key": [token, ("upload_key", None, None, b"../../x"), file],
        "no file": [token, key],
        "broken body": [token, key, file],
        "not multipart": [],
    }[case]

    if case == "not multipart":
        response = browser_a.post(upload_action(item_id), fields)
    else:
        body, kind = multipart(*parts)

        if case == "broken body":
            body = body.replace(b"--u1c-test-boundary--", b"")

        response = post_raw(browser_a, item_id, body, kind)

    assert response.status_code == status
    assert message in response.text
    assert_nothing_kept(app)


def test_the_eleventh_attachment_is_refused_and_the_bin_keeps_its_places(app, browser_a, clock):
    item_id = browser_a.create(actions="很多材料")

    for number in range(10):
        assert browser_a.upload(item_id, f"材料{number}.png", PNG + bytes([number])).status_code == 303

    page = browser_a.get(f"/experiences/{item_id}").text

    assert 'enctype="multipart/form-data"' not in page
    assert "达到上限" in page

    # A post from a form opened before the tenth upload is refused all the same.
    body, kind = multipart(
        ("csrf_token", None, None, browser_a.csrf_token().encode()),
        ("upload_key", None, None, b"k" * 32),
        ("file", "第十一个.pdf", "application/pdf", PDF),
    )
    refused = post_raw(browser_a, item_id, body, kind)

    assert refused.status_code == 409
    assert "达到上限" in refused.text

    # A deleted attachment still holds its place while it can be restored ...
    first = rows(app)[0].id
    path = f"/experiences/{item_id}/attachments/{first}/delete"
    assert browser_a.submit(path, path).status_code == 303
    assert post_raw(browser_a, item_id, body, kind).status_code == 409

    # ... and frees it once it is permanently deleted and cleaned up.
    purge = f"/experiences/{item_id}/attachments/{first}/purge"
    assert browser_a.submit(purge, purge, acknowledge="yes").status_code == 303
    assert browser_a.upload(item_id, "第十一个.pdf").status_code == 303
    assert len([row for row in rows(app) if row.deleted_at is None]) == 10
    assert len(kept_files(app)) == 10


def test_uploads_at_the_same_moment_cannot_pass_the_limit(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="并发上传")

    for number in range(8):
        assert browser_a.upload(item_id, f"已有{number}.pdf", PDF + bytes([number])).status_code == 303

    senders = [Browser.signed_in(app, "合成用户 A") for _ in range(5)]
    forms = [form_fields(sender, item_id) for sender in senders]
    together = threading.Barrier(5)
    real_store = web.store_upload

    def store_together(*args):
        # Every upload is received and waits here, so all five reserve at once.
        together.wait(timeout=20)
        return real_store(*args)

    monkeypatch.setattr(web, "store_upload", store_together)
    statuses = [None] * 5

    def send(number):
        response = senders[number].client.post(
            upload_action(item_id),
            data=forms[number],
            files={"file": (f"同时{number}.pdf", PDF + b"same moment" + bytes([number]), "application/pdf")},
        )
        statuses[number] = response.status_code

    threads = [threading.Thread(target=send, args=(number,)) for number in range(5)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(timeout=60)

    assert sorted(statuses) == [303, 303, 409, 409, 409]
    assert [row.state for row in rows(app)] == [AttachmentState.READY] * 10
    assert len(kept_files(app)) == 10
    assert leftovers(app) == []


def test_sending_the_same_form_again_never_adds_the_file_twice(app, browser_a):
    item_id = browser_a.create(actions="重复提交")
    fields = form_fields(browser_a, item_id)
    file = {"file": ("证明.pdf", PDF, "application/pdf")}

    first = browser_a.client.post(upload_action(item_id), data=fields, files=file)
    again = browser_a.client.post(upload_action(item_id), data=fields, files=file)

    assert first.headers["location"].endswith("?notice=attachment_added#attachments")
    assert again.headers["location"].endswith("?notice=attachment_already_added#attachments")
    assert "已经上传过了" in browser_a.get(again.headers["location"]).text
    assert len(rows(app)) == 1

    # The same form with another file (an old page, say) is refused rather than guessed at.
    other = browser_a.client.post(upload_action(item_id), data=fields, files={"file": ("别的.png", PNG, "image/png")})

    assert other.status_code == 409
    assert "表单已经用过了" in other.text
    assert len(rows(app)) == 1
    assert len(kept_files(app)) == 1


# --- who may upload, download and change what -------------------------------------


def test_uploads_need_this_sessions_token_before_the_file(app, browser_a, browser_b):
    item_id = browser_a.create(actions="A 的经历")
    fields = form_fields(browser_a, item_id)
    key = ("upload_key", None, None, fields["upload_key"].encode())
    file = ("file", "证明.pdf", "application/pdf", PDF)
    anonymous = Browser(app)

    for token in (b"", b"forged-token", browser_b.csrf_token().encode()):
        body, kind = multipart(("csrf_token", None, None, token), key, file)
        assert post_raw(browser_a, item_id, body, kind).status_code == 403

    # The token after the file is too late: nothing of the file may be kept first.
    body, kind = multipart(key, file, ("csrf_token", None, None, fields["csrf_token"].encode()))
    assert post_raw(browser_a, item_id, body, kind).status_code == 403

    body, kind = multipart(("csrf_token", None, None, fields["csrf_token"].encode()), key, file)

    assert post_raw(anonymous, item_id, body, kind).status_code == 401
    assert post_raw(browser_a, item_id, body, kind, Origin="https://attacker.example").status_code == 403
    assert post_raw(browser_a, item_id, body, kind, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert post_raw(browser_b, item_id, body, kind).status_code == 404
    assert_nothing_kept(app)
    # Control: the same body from the owner is accepted.
    assert post_raw(browser_a, item_id, body, kind).status_code == 303


def test_an_attachment_is_only_found_under_its_own_experience(app, browser_a, browser_b):
    first = browser_a.create(actions="第一条")
    second = browser_a.create(actions="第二条")
    assert browser_a.upload(first, "第一条的附件.pdf").status_code == 303
    [row] = rows(app)
    token = browser_a.csrf_token()

    for item_id, user in ((second, browser_a), (first, browser_b)):
        assert user.get(f"/experiences/{item_id}/attachments/{row.id}/download").status_code == 404
        assert user.get(f"/experiences/{item_id}/attachments/{row.id}/delete").status_code == 404

    response = browser_a.post(
        f"/experiences/{second}/attachments/{row.id}/delete",
        {"csrf_token": token, "expected_revision": str(row.revision)},
    )

    assert response.status_code == 404
    assert rows(app)[0].deleted_at is None
    assert browser_a.get(f"/experiences/{first}/attachments/{row.id}/download").status_code == 200


def test_no_static_or_guessable_address_serves_a_file(app, browser_a):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    [row] = rows(app)
    page = browser_a.get(f"/experiences/{item_id}").text

    assert row.storage_key not in page
    assert str(store(app).root) not in page

    for path in (
        f"/static/{row.storage_key}",
        f"/static/../files/{row.owner_id}/{row.storage_key}",
        f"/files/{row.owner_id}/{row.storage_key}",
        f"/experiences/{item_id}/attachments/{row.storage_key}/download",
        f"/experiences/{item_id}/attachments/{row.id}",
    ):
        response = browser_a.get(path)
        assert response.status_code in (404, 405), path
        assert response.content != PDF


def test_dangerous_file_names_are_cleaned_and_shown_as_text(app, browser_a):
    item_id = browser_a.create(actions="经历")
    names = {
        "../../etc/passwd.pdf": "passwd.pdf",
        "C:\\Users\\x\\简历.pdf": "简历.pdf",
        "<img src=x onerror=alert(1)>.pdf": "<img src=x onerror=alert(1)>.pdf",
        "看起来是图片\u202egnp.pdf": "看起来是图片gnp.pdf",
        'a"b;c=d%0D%0AX-Injected: yes.pdf': 'a"b;c=d%0D%0AX-Injected: yes.pdf',
        "长" * 300 + ".pdf": "长" * 251 + ".pdf",
    }

    for raw in names:
        body, kind = multipart(
            ("csrf_token", None, None, browser_a.csrf_token().encode()),
            ("upload_key", None, None, form_fields(browser_a, item_id)["upload_key"].encode()),
            ("file", raw.replace('"', "%22"), "application/pdf", PDF + raw.encode()),
        )
        assert post_raw(browser_a, item_id, body, kind).status_code == 303, raw

    stored = {row.filename for row in rows(app)}
    page = browser_a.get(f"/experiences/{item_id}").text

    assert stored == {name.replace('"', "%22") for name in names.values()}
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;.pdf" in page
    assert "\u202e" not in page

    for row in rows(app):
        header = browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").headers["content-disposition"]

        assert "\r" not in header and "\n" not in header
        assert header.count('"') == 2
        assert header.isascii()


# --- failures halfway ----------------------------------------------------------------


def _fail(*args, **kwargs):
    raise OSError("disk unavailable")


def test_a_file_that_cannot_be_put_in_place_is_not_kept(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "已有的.pdf").status_code == 303
    before = rows(app)
    monkeypatch.setattr(FileStore, "place", _fail)
    response = browser_a.upload(item_id, "新的.pdf", PDF + b"new")

    assert response.status_code == 500
    assert "上传没有完成" in response.text
    assert "已上传" not in response.text
    assert [row.id for row in rows(app)] == [row.id for row in before]
    assert len(kept_files(app)) == 1
    assert leftovers(app) == []
    assert "现在还能添加 9 个" in response.text


def test_a_database_failure_when_reserving_keeps_nothing(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")

    def unavailable(*args):
        raise StorageError("database operation failed")

    monkeypatch.setattr(attachment_service, "_reserve", unavailable)
    response = browser_a.upload(item_id, "证明.pdf")

    assert response.status_code == 500
    assert "上传没有完成" in response.text
    assert_nothing_kept(app)


def test_a_database_failure_when_activating_is_undone_at_once(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")

    def unavailable(*args):
        raise StorageError("database operation failed")

    monkeypatch.setattr(attachment_service, "_activate", unavailable)
    response = browser_a.upload(item_id, "证明.pdf")

    assert response.status_code == 500
    assert "上传没有完成" in response.text
    assert_nothing_kept(app)


def test_what_cannot_be_undone_at_once_is_finished_by_the_clean_up_pass(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")

    def unavailable(*args):
        raise StorageError("database operation failed")

    monkeypatch.setattr(attachment_service, "_activate", unavailable)
    monkeypatch.setattr(FileStore, "remove_kept", _fail)

    assert browser_a.upload(item_id, "证明.pdf").status_code == 500

    [row] = rows(app)

    # Marked failed: never listed, never downloadable, and no longer taking a place.
    assert row.state == AttachmentState.FAILED
    assert len(kept_files(app)) == 1
    assert "证明.pdf" not in browser_a.get(f"/experiences/{item_id}").text
    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").status_code == 404

    monkeypatch.undo()
    app.state.library.run_cleanup()

    assert_nothing_kept(app)


def test_an_upload_cut_off_after_placing_its_file_is_removed_after_an_hour(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="经历")

    # The process "stops" right after the file is in place: no activation, no undo.
    def stopped(*args):
        raise StorageError("process stopped")

    monkeypatch.setattr(attachment_service, "_activate", stopped)
    monkeypatch.setattr(attachment_service, "abandon_upload", lambda *args, **kwargs: False)
    browser_a.upload(item_id, "证明.pdf")
    monkeypatch.undo()
    [row] = rows(app)

    assert row.state == AttachmentState.PENDING
    assert len(kept_files(app)) == 1
    assert "证明.pdf" not in browser_a.get(f"/experiences/{item_id}").text
    assert "现在还能添加 9 个" in browser_a.get(f"/experiences/{item_id}").text

    clock.advance(minutes=59)
    app.state.library.run_cleanup()

    assert len(rows(app)) == 1

    clock.advance(minutes=1)
    app.state.library.run_cleanup()

    assert_nothing_kept(app)


def test_an_experience_deleted_during_an_upload_gets_no_attachment(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="上传期间被删除")
    real_place = FileStore.place

    def delete_first(self, staging, owner_id):
        # The experience goes to the recycle bin between reserving and placing.
        page = browser_a.form(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
        assert browser_a.post(f"/experiences/{item_id}/delete", page.fields).status_code == 303
        real_place(self, staging, owner_id)

    monkeypatch.setattr(FileStore, "place", delete_first)
    response = browser_a.upload(item_id, "证明.pdf")
    monkeypatch.undo()

    assert response.status_code == 404
    assert "上传期间被删除" in response.text
    assert_nothing_kept(app)

    trash = browser_a.get("/experiences?tab=trash").text
    restore = form_for(trash, f"/experiences/{item_id}/restore")
    assert browser_a.post(f"/experiences/{item_id}/restore", restore.fields).status_code == 303
    assert "还没有附件。" in browser_a.get(f"/experiences/{item_id}").text


def test_leftover_files_are_removed_only_when_old_and_unused(app, browser_a, clock):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "保留的.pdf").status_code == 303
    [row] = rows(app)
    files = store(app)
    kept = files.files_dir / row.owner_id / row.storage_key
    an_hour_ago = os.path.getmtime(kept) - 3601

    def leftover(path: Path, *, old: bool) -> Path:
        path.write_bytes(b"leftover")

        if old:
            os.utime(path, (an_hour_ago, an_hour_ago))

        return path

    old_staging = leftover(files.staging_dir / ("a" * 32 + ".part"), old=True)
    new_staging = leftover(files.staging_dir / ("b" * 32 + ".part"), old=False)
    in_use = leftover(files.staging_dir / ("c" * 32 + ".part"), old=True)
    old_export = leftover(files.exports_dir / ("d" * 32 + ".zip.part"), old=True)
    orphan = leftover(files.files_dir / row.owner_id / ("e" * 32), old=True)
    stranger = leftover(files.staging_dir / "not-made-by-the-store.txt", old=True)
    link = files.files_dir / row.owner_id / ("f" * 32)
    link.symlink_to(stranger)
    os.utime(kept, (an_hour_ago, an_hour_ago))
    files.hold(in_use)
    app.state.library.run_cleanup()
    files.release(in_use)

    assert not old_staging.exists() and not old_export.exists() and not orphan.exists()
    assert new_staging.exists() and in_use.exists() and stranger.exists() and link.is_symlink()
    # The file a record refers to stays, however old it is.
    assert kept.read_bytes() == PDF
    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").content == PDF


def test_a_link_in_place_of_a_kept_file_is_never_followed(app, browser_a, tmp_path):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    [row] = rows(app)
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"secret outside the store")
    kept = store(app).files_dir / row.owner_id / row.storage_key
    kept.unlink()
    kept.symlink_to(secret)
    download = browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download")

    assert download.status_code == 500
    assert "无法读取" in download.text
    assert b"secret outside" not in download.content
    assert "文件无法读取，不能下载" in browser_a.get(f"/experiences/{item_id}").text

    path = f"/experiences/{item_id}/attachments/{row.id}/delete"
    assert browser_a.submit(path, path).status_code == 303
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    response = browser_a.submit(purge, purge, acknowledge="yes")

    assert response.headers["location"].endswith("notice=attachment_purged#attachments")
    # The link itself is gone; what it pointed to is untouched.
    assert not kept.is_symlink() and not kept.exists()
    assert secret.read_bytes() == b"secret outside the store"


def test_an_owner_folder_replaced_by_a_link_is_never_used(app, browser_a, tmp_path):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    [row] = rows(app)
    folder = store(app).files_dir / row.owner_id
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / row.storage_key).write_bytes(PDF)
    (folder / row.storage_key).rename(tmp_path / "moved-away")
    folder.rmdir()
    folder.symlink_to(elsewhere)

    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").status_code == 500
    assert browser_a.upload(item_id, "另一个.pdf", PDF + b"2").status_code == 500

    path = f"/experiences/{item_id}/attachments/{row.id}/delete"
    assert browser_a.submit(path, path).status_code == 303
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    response = browser_a.submit(purge, purge, acknowledge="yes")

    # Not finished, and saying so; the file outside the store is untouched.
    assert response.headers["location"].endswith("notice=attachment_purge_pending#attachments")
    assert (elsewhere / row.storage_key).read_bytes() == PDF
    # The refused upload wrote nothing there either; its row stays failed (never
    # listed) until a clean-up can remove it.
    states = {r.filename: r.state for r in rows(app)}

    assert states == {"证明.pdf": AttachmentState.READY, "另一个.pdf": AttachmentState.FAILED}
    assert sorted(path.name for path in elsewhere.iterdir()) == [row.storage_key]
