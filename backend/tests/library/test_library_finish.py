"""Finishing a resume import: keeping its original as an attachment, then ending it (U2-B3b).

All material is synthetic and every candidate comes from the local
simulated rules. The original is kept only in an experience the user picks,
as a whole and as an ordinary attachment that outlives the import; ending
a temporary import clears it, ending a saved draft moves it to the recycle
bin. Whatever fails, nothing half made is shown, the import keeps its
original, and what was adopted or kept stays. Tests compare stored rows and
files, not only what pages say.
"""
import hashlib
import io
import json
import threading
import zipfile
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.library import finish_web
from app.library.imports import discard
from app.library.lifecycle import delete_attachment, delete_experience
from app.library.models import Attachment, AttachmentState, ImportState
from app.library.originals import KeptFile, keep_original
from library_helpers import PNG, Browser, form_for
from test_library_adoption import adopt_new, items, refused
from test_library_candidates import ask, library_rows, one, owner_id, paste, rt
from test_library_drafts import RESUME_PDF, delete, draft, restore, save
from test_library_imports import DOCX_FILE, import_id_from, kept_files, row_of, send

# --- helpers ------------------------------------------------------------------


def attachments(app, item_id: str | None = None) -> list[Attachment]:
    """Every attachment row (also pending, failed or in the bin), oldest first."""
    query = select(Attachment).order_by(Attachment.created_at, Attachment.id)

    with rt(app).database.transaction() as db:
        return list(db.scalars(query if item_id is None else query.where(Attachment.item_id == item_id)))


def attachment_files(app) -> list:
    """The files in the attachment area (kept names), whatever row names them."""
    return sorted(path for path in rt(app).files.files_dir.rglob("*") if path.is_file() and not path.name.startswith("roleradar-"))


@contextmanager
def refused_once(app, statement: str):
    """While open, the database refuses the first statement that starts so, as a passing disk failure would."""
    engine, fired = rt(app).database.engine, []

    def refuse(connection, cursor, sql, parameters, context, executemany):
        if not fired and sql.lstrip().upper().startswith(statement.upper()):
            fired.append(sql)
            raise OperationalError(sql, {}, Exception("synthetic disk failure"))

    event.listen(engine, "before_cursor_execute", refuse)

    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", refuse)


def keep(browser, import_id: str, item_id: str, **fields):
    page = f"/imports/{import_id}/keep-file"

    return browser.submit(page, page, target=item_id, **fields)


def finish(browser, import_id: str):
    return browser.submit(f"/imports/{import_id}/finish", f"/imports/{import_id}/finish")


def download(browser, attachment: Attachment):
    return browser.get(f"/experiences/{attachment.item_id}/attachments/{attachment.id}/download")


def imported_pdf(app, browser) -> tuple[str, str]:
    """A resume PDF read, asked for candidates and its project adopted as a new draft; returns (import, project)."""
    import_id = import_id_from(send(browser, file=RESUME_PDF))
    ask(browser, import_id)
    assert "notice=adopted#" in adopt_new(browser, import_id, one(app, import_id, "project").id).headers["location"]

    return import_id, items(app, "project")[0].id


def export_files(browser) -> tuple[dict, dict]:
    archive = zipfile.ZipFile(io.BytesIO(browser.get("/export/all.zip").content))
    files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}

    return json.loads(files["manifest.json"]), files


# --- keeping the original --------------------------------------------------------------------------


def test_a_pdf_original_is_kept_whole_in_the_chosen_experience_and_outlives_the_import(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    other = browser_a.create(title="另一条合成经历")
    row = row_of(app, import_id)
    page = browser_a.get(f"/imports/{import_id}/keep-file").text

    # Nothing is picked; the experience this import went into comes first.
    assert " checked" not in page and page.index(project) < page.index(other) and "这次导入加入的" in page
    assert "里面可能有你的联系方式和其他经历" in page

    kept = keep(browser_a, import_id, project)

    assert kept.headers["location"] == f"/imports/{import_id}?notice=original_kept"
    (attachment,) = attachments(app)
    assert (attachment.item_id, attachment.state, attachment.media_type) == (project, AttachmentState.READY, "application/pdf")
    assert (attachment.filename, attachment.size, attachment.sha256) == (row.filename, row.size, row.sha256)
    assert attachment.storage_key != row.storage_key and download(browser_a, attachment).content == RESUME_PDF[1]
    assert "PDF" in browser_a.get(f"/experiences/{project}").text and attachments(app, other) == []
    # The import is as it was: still in use, its own file in place.
    assert row_of(app, import_id).state == ImportState.READ and len(kept_files(app)) == 1

    ended = finish(browser_a, import_id)

    assert ended.headers["location"] == "/imports?notice=import_finished"
    assert row_of(app, import_id).state == ImportState.DISCARDED and kept_files(app) == []
    assert download(browser_a, attachment).content == RESUME_PDF[1]
    manifest, files = export_files(browser_a)
    (entry,) = manifest["attachments"]
    assert entry["status"] == "included" and hashlib.sha256(files[entry["path"]]).hexdigest() == row.sha256


def test_a_word_original_is_kept_only_this_way(app, browser_a):
    import_id = import_id_from(send(browser_a, file=DOCX_FILE))
    target = browser_a.create(title="合成经历")

    keep(browser_a, import_id, target)

    (attachment,) = attachments(app)
    assert attachment.media_type == DOCX_FILE[2] and download(browser_a, attachment).content == DOCX_FILE[1]
    assert "Word（.docx）" in browser_a.get(f"/experiences/{target}").text
    manifest, files = export_files(browser_a)
    assert files[manifest["attachments"][0]["path"]] == DOCX_FILE[1]
    # The upload form still takes PDF, PNG and JPEG only.
    refused_upload = browser_a.upload(target, "合成简历.docx", DOCX_FILE[1], DOCX_FILE[2])
    assert refused_upload.status_code == 415 and len(attachments(app)) == 1


def test_pasted_text_has_no_original_to_keep(app, browser_a):
    import_id = paste(browser_a)
    target = browser_a.create(title="合成经历")
    page = browser_a.get(f"/imports/{import_id}").text

    assert f"/imports/{import_id}/keep-file" not in page and "结束这次导入…" in page
    assert browser_a.get(f"/imports/{import_id}/keep-file").status_code == 409
    forged = browser_a.post(
        f"/imports/{import_id}/keep-file",
        {"csrf_token": browser_a.csrf_token(), "upload_key": "synthetic-keep-key-000001", "target": target},
    )
    assert forged.status_code == 409 and "没有原文件" in forged.text and attachments(app) == []


def test_only_the_owners_experiences_in_use_can_receive_it(app, browser_a, browser_b):
    import_id, project = imported_pdf(app, browser_a)
    binned = browser_a.create(title="会删除的合成经历")
    browser_a.submit(f"/experiences/{binned}/delete", f"/experiences/{binned}/delete")
    browser_a.submit("/basic-profile/edit", "/basic-profile", name="合成用户甲")
    profile_id = [item.id for item in items(app, "profile")][0]
    theirs = browser_b.create(title="B 的合成经历")
    page = browser_a.get(f"/imports/{import_id}/keep-file").text
    form = browser_a.form(f"/imports/{import_id}/keep-file", f"/imports/{import_id}/keep-file")

    assert binned not in page and profile_id not in page and theirs not in page
    for target in (binned, profile_id, theirs, "00000000-0000-4000-8000-000000000000"):
        refused_target = browser_a.post(f"/imports/{import_id}/keep-file", {**form.fields, "target": target})
        assert refused_target.status_code == 409 and "选择的经历已经不在了" in refused_target.text, target
    assert browser_a.post(f"/imports/{import_id}/keep-file", {"target": project, "upload_key": form.fields["upload_key"]}).status_code == 403
    assert Browser(app).post(f"/imports/{import_id}/keep-file", {**form.fields, "target": project}).status_code == 401
    as_b = {**form.fields, "csrf_token": browser_b.csrf_token(), "target": theirs}
    assert browser_b.post(f"/imports/{import_id}/keep-file", as_b).status_code == 404
    assert browser_b.get(f"/imports/{import_id}/finish").status_code == 404
    assert attachments(app) == []


def test_a_full_experience_or_one_that_has_the_file_gets_no_copy(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    crowded = browser_a.create(title="附件很多的合成经历")
    for number in range(10):
        assert browser_a.upload(crowded, f"合成截图{number}.png", PNG).status_code == 303
    one_of_them = attachments(app, crowded)[0]
    browser_a.submit(
        f"/experiences/{crowded}/attachments/{one_of_them.id}/delete", f"/experiences/{crowded}/attachments/{one_of_them.id}/delete"
    )

    full = keep(browser_a, import_id, crowded)

    # One in the recycle bin still takes its place; nothing is deleted for the user.
    assert full.status_code == 409 and "附件已满" in full.text and "没有替你删除任何附件" in full.text
    assert len(attachments(app, crowded)) == 10 and attachments(app, project) == []
    keep(browser_a, import_id, project)
    again = keep(browser_a, import_id, project)
    assert again.status_code == 409 and "已经有这份原文件" in again.text and len(attachments(app, project)) == 1
    page = browser_a.get(f"/imports/{import_id}/keep-file").text
    assert "已经有这份文件" in page and "附件已满" in page


def test_the_same_form_twice_another_tab_or_two_requests_at_once_keep_one_copy(app, browser_a, clock):
    import_id, project = imported_pdf(app, browser_a)
    page = f"/imports/{import_id}/keep-file"
    first, second = browser_a.form(page, page), browser_a.form(page, page)

    once = browser_a.post(page, {**first.fields, "target": project})
    repeated = browser_a.post(page, {**first.fields, "target": project})
    other_tab = browser_a.post(page, {**second.fields, "target": project})

    assert once.headers["location"].endswith("notice=original_kept")
    assert repeated.headers["location"].endswith("notice=original_kept_already")
    assert other_tab.status_code == 409 and len(attachments(app)) == 1

    target, owner, gate, results = browser_a.create(title="合成经历"), owner_id(app), threading.Barrier(2), []

    def keep_at_once(key):
        gate.wait()
        results.append(keep_original(rt(app).database, rt(app).files, owner, import_id, target, key, clock.now))

    threads = [threading.Thread(target=keep_at_once, args=(f"synthetic-concurrent-key-{n:04d}",)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert sorted(results) == sorted([KeptFile.ADDED, KeptFile.ALREADY_THERE])
    assert len([row for row in attachments(app, target) if row.state == AttachmentState.READY]) == 1
    assert len(attachments(app, target)) == 1


def test_a_changed_or_missing_original_is_never_published(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    (original,) = kept_files(app)
    original.write_bytes(RESUME_PDF[1][:-1] + b"X")

    changed = keep(browser_a, import_id, project)

    assert changed.status_code == 500 and "和读取时不一致" in changed.text
    assert attachments(app) == [] and attachment_files(app) == [] and row_of(app, import_id).state == ImportState.READ
    original.unlink()
    assert keep(browser_a, import_id, project).status_code == 500 and attachments(app) == []


@pytest.mark.parametrize("meanwhile", ["the import ends", "the import expires", "the experience is deleted"])
def test_a_source_or_target_gone_while_copying_publishes_nothing(app, browser_a, clock, monkeypatch, meanwhile):
    import_id, project = imported_pdf(app, browser_a)
    owner, files = owner_id(app), rt(app).files
    real_place = files.place

    def place_after_a_change(staging, owner_of, **kwargs):
        if meanwhile == "the import ends":
            discard(rt(app).database, files, rt(app).readings, owner, import_id, clock.now())
        elif meanwhile == "the import expires":
            clock.advance(days=7)
        else:
            with rt(app).database.transaction() as db:
                delete_experience(db, owner, project, items(app, "project")[0].revision, clock.now())

        return real_place(staging, owner_of, **kwargs)

    monkeypatch.setattr(files, "place", place_after_a_change)

    outcome = keep_original(rt(app).database, files, owner, import_id, project, "synthetic-keep-key-000001", clock.now)

    expected = KeptFile.TARGET_GONE if meanwhile == "the experience is deleted" else KeptFile.SOURCE_GONE
    assert outcome == expected and attachments(app) == [] and attachment_files(app) == []


def test_a_failure_while_keeping_leaves_the_import_as_it_was_and_can_be_retried(app, browser_a, monkeypatch):
    import_id, project = imported_pdf(app, browser_a)
    files = rt(app).files

    def broken_place(*args, **kwargs):
        raise OSError("synthetic: the disk is full")

    monkeypatch.setattr(files, "place", broken_place)
    no_place = keep(browser_a, import_id, project)
    monkeypatch.undo()

    with refused_once(app, "INSERT INTO attachments"):
        no_reservation = keep(browser_a, import_id, project)
    with refused_once(app, "UPDATE attachments"):
        not_made_ready = keep(browser_a, import_id, project)

    for failed in (no_place, no_reservation, not_made_ready):
        assert failed.status_code == 500 and "另存没有完成" in failed.text
    assert attachments(app) == [] and attachment_files(app) == []
    assert row_of(app, import_id).state == ImportState.READ and len(kept_files(app)) == 1

    assert keep(browser_a, import_id, project).headers["location"].endswith("notice=original_kept")
    assert len(attachments(app)) == 1 and len(attachment_files(app)) == 1


def test_a_copy_that_cannot_be_undone_at_once_is_cleared_by_the_clean_up_pass(app, browser_a, monkeypatch, clock):
    import_id, project = imported_pdf(app, browser_a)
    files = rt(app).files

    def broken_place(*args, **kwargs):
        raise OSError("synthetic: the disk is full")

    def stuck_remove(*args, **kwargs):
        raise OSError("synthetic: the file is busy")

    monkeypatch.setattr(files, "place", broken_place)
    monkeypatch.setattr(files, "remove_file", stuck_remove)
    failed = keep(browser_a, import_id, project)
    monkeypatch.undo()

    # A failed row is left, which no page shows; the clean-up pass removes it.
    assert failed.status_code == 500 and [row.state for row in attachments(app)] == [AttachmentState.FAILED]
    assert "合成简历.pdf" not in browser_a.get(f"/experiences/{project}").text
    rt(app).run_cleanup()
    assert attachments(app) == [] and attachment_files(app) == []


# --- ending ------------------------------------------------------------------------------------------


def test_ending_a_temporary_import_says_what_stays_and_clears_the_rest(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    records = library_rows(app)
    page = browser_a.get(f"/imports/{import_id}/finish").text

    assert "结束这次导入？" in page and "会留下" in page and "会清理，不能恢复" in page
    assert f'href="/experiences/{project}"' in page and "作为新的经历草稿加入" in page
    assert "原文件已另存为附件" in page and "还没有另存为附件" not in page
    # What is cleared is the import's own copy; the kept one is said to stay.
    assert "导入里暂存的原文件“合成简历.pdf”（另存为附件的那一份不受影响）" in page
    assert "返回继续核对" in page and f'href="/imports/{import_id}/review"' in page
    assert ">结束这次导入</button>" in page and "移入回收站" not in page

    ended = finish(browser_a, import_id)

    assert ended.headers["location"] == "/imports?notice=import_finished"
    assert "已经加入的内容和另存的附件不受影响" in browser_a.get(ended.headers["location"]).text
    assert library_rows(app) == records and len(attachments(app, project)) == 1
    assert row_of(app, import_id).segments is None and kept_files(app) == []


def test_ending_without_adopting_anything_is_allowed_and_says_the_original_is_not_kept(app, browser_a):
    import_id = import_id_from(send(browser_a, file=DOCX_FILE))
    page = browser_a.get(f"/imports/{import_id}/finish").text

    assert "这次没有加入任何条目。不加入也可以结束" in page and "原文件现在没有可用的另存副本" in page
    assert "以及原文件“合成简历.docx”。" in page
    assert f'href="/imports/{import_id}/keep-file?next=finish"' in page
    assert finish(browser_a, import_id).headers["location"] == "/imports?notice=import_finished"
    assert row_of(app, import_id).state == ImportState.DISCARDED and attachments(app) == []


def test_ending_a_saved_draft_only_moves_it_to_the_recycle_bin(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    save(browser_a, import_id)
    page = browser_a.get(f"/imports/{import_id}/finish").text

    # Looking, or leaving, ends nothing.
    assert ">结束并将草稿移入回收站</button>" in page and "直接离开就好" in page and "不是附件" in page
    assert draft(app, import_id).deleted_at is None

    ended = finish(browser_a, import_id)

    assert ended.headers["location"] == "/imports?notice=draft_finished"
    assert draft(app, import_id).deleted_at is not None and len(kept_files(app)) == 1
    assert len(attachments(app)) == 1 and download(browser_a, attachments(app)[0]).status_code == 200
    assert restore(browser_a, import_id).headers["location"].endswith("notice=draft_restored")
    assert browser_a.get(f"/imports/{import_id}/review").status_code == 200


def test_an_ending_that_fails_keeps_everything_and_retrying_copies_nothing(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)

    with refused(app, "UPDATE resume_imports"):
        failed = finish(browser_a, import_id)

    assert failed.status_code == 500 and "结束没有完成" in failed.text and "原文件已另存为附件" in failed.text
    assert row_of(app, import_id).state == ImportState.READ and len(kept_files(app)) == 1
    assert finish(browser_a, import_id).headers["location"] == "/imports?notice=import_finished"
    assert len(attachments(app)) == 1 and row_of(app, import_id).state == ImportState.DISCARDED


def test_a_finish_page_from_before_the_import_was_saved_does_not_end_the_draft(app, browser_a):
    import_id = import_id_from(send(browser_a, file=RESUME_PDF))
    older = browser_a.form(f"/imports/{import_id}/finish", f"/imports/{import_id}/finish")
    save(browser_a, import_id)

    refused_post = browser_a.post(f"/imports/{import_id}/finish", older.fields)

    assert refused_post.status_code == 409 and "被保存为草稿了" in refused_post.text
    assert ">结束并将草稿移入回收站</button>" in refused_post.text
    assert draft(app, import_id).deleted_at is None and row_of(app, import_id).state == ImportState.READ


def test_the_kept_file_and_the_import_live_by_their_own_rules(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    (attachment,) = attachments(app)
    path = f"/experiences/{project}/attachments/{attachment.id}/delete"

    # Deleting the attachment leaves the import and the experience as they are.
    browser_a.submit(path, path)
    assert row_of(app, import_id).state == ImportState.READ and items(app, "project")[0].deleted_at is None
    # Saved, deleted and removed for good, the import leaves the attachment as it is (in its bin here).
    save(browser_a, import_id)
    delete(browser_a, import_id)
    browser_a.submit(f"/imports/{import_id}/purge", f"/imports/{import_id}/purge", acknowledge="yes")
    assert row_of(app, import_id).state == ImportState.DELETED and kept_files(app) == []
    (still,) = attachments(app)
    assert still.id == attachment.id and still.deleted_at is not None and len(attachment_files(app)) == 1


# --- what counts as kept: the file itself, checked now (Codex U2B3B-F1) ------------------------------


HEALTH_SAID = {"missing": "文件找不到了", "changed": "文件内容和保存时不一致", "unreadable": "文件无法读取"}


def break_file(path, fault: str) -> None:
    """Make an attachment's file unusable on disk, as a lost, edited or locked file would be."""
    if fault == "missing":
        path.unlink()
    elif fault == "changed":
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    else:
        path.chmod(0)


@pytest.mark.parametrize("fault", ["missing", "changed", "unreadable"])
def test_a_copy_whose_file_is_gone_or_changed_is_never_called_kept(app, browser_a, fault):
    import_id, project = imported_pdf(app, browser_a)
    page = f"/imports/{import_id}/keep-file"
    first = browser_a.form(page, page)
    browser_a.post(page, {**first.fields, "target": project})
    (attachment,) = attachments(app)
    (path,) = attachment_files(app)
    break_file(path, fault)

    try:
        finish_page = browser_a.get(f"/imports/{import_id}/finish").text
        keep_page = browser_a.get(page).text
        same_form_again = browser_a.post(page, {**first.fields, "target": project})

        assert download(browser_a, attachment).status_code != 200
        # Said as it is: not kept, why, and that nothing is deleted or overwritten for the user.
        assert "原文件已另存为附件" not in finish_page and "原文件现在没有可用的另存副本" in finish_page
        assert HEALTH_SAID[fault] in finish_page and "系统不会自动删除或覆盖它们" in finish_page
        assert "那里另存的这份现在不可用，可以再存一份" in keep_page and HEALTH_SAID[fault] in keep_page
        assert f'value="{project}" required>' in keep_page
        # The same form again is not told it succeeded.
        assert same_form_again.status_code == 409 and "这个页面之前另存的附件现在不可用" in same_form_again.text
        assert [(row.id, row.state, row.storage_key) for row in attachments(app)] == [
            (attachment.id, AttachmentState.READY, attachment.storage_key)
        ]
        # A new copy can be kept; the unusable one stays where it was, untouched.
        assert keep(browser_a, import_id, project).headers["location"].endswith("notice=original_kept")
        assert len(attachments(app, project)) == 2 and path.exists() == (fault != "missing")
        assert "原文件已另存为附件" in browser_a.get(f"/imports/{import_id}/finish").text
    finally:
        if fault == "unreadable":
            path.chmod(0o600)


def test_ending_with_no_usable_copy_is_allowed_once_the_page_has_said_so(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    (attachment,) = attachments(app)
    (path,) = attachment_files(app)
    break_file(path, "changed")
    page = browser_a.get(f"/imports/{import_id}/finish").text

    assert 'name="keeping" value="none"' in page
    ended = finish(browser_a, import_id)

    assert ended.headers["location"] == "/imports?notice=import_finished"
    assert row_of(app, import_id).state == ImportState.DISCARDED and kept_files(app) == []
    # The unusable attachment is left for the user, as it was.
    assert [row.id for row in attachments(app)] == [attachment.id] and path.exists()


# --- an older finish page against copies that changed (Codex U2B3B-F2) -------------------------------


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        ("temporary", "attachment deleted and purged"),
        ("temporary", "attachment deleted"),
        ("temporary", "experience deleted"),
        ("temporary", "file missing"),
        ("temporary", "file changed"),
        ("draft", "attachment deleted"),
        ("draft", "file changed"),
    ],
)
def test_an_older_finish_page_ends_nothing_once_its_copy_is_not_kept(app, browser_a, clock, kind, change):
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    (attachment,) = attachments(app)

    if kind == "draft":
        save(browser_a, import_id)

    url = f"/imports/{import_id}/finish"
    older = browser_a.form(url, url)
    assert older.fields["keeping"] == attachment.id
    base = f"/experiences/{project}/attachments/{attachment.id}"

    if change.startswith("attachment deleted"):
        assert browser_a.submit(f"{base}/delete", f"{base}/delete").status_code == 303

        if change.endswith("purged"):
            assert browser_a.submit(f"{base}/purge", f"{base}/purge", acknowledge="yes").status_code == 303
    elif change == "experience deleted":
        browser_a.submit(f"/experiences/{project}/delete", f"/experiences/{project}/delete")
    else:
        (path,) = attachment_files(app)
        break_file(path, change.split()[1])

    stale = browser_a.post(url, older.fields)

    # Nothing ended or cleared; the page says how things are now and asks again.
    assert stale.status_code == 409 and "没有结束：原文件的另存情况在你打开这个页面之后变了" in stale.text
    assert row_of(app, import_id).state == ImportState.READ and len(kept_files(app)) == 1
    assert kind == "temporary" or draft(app, import_id).deleted_at is None
    assert "原文件现在没有可用的另存副本" in stale.text and "原文件已另存为附件" not in stale.text
    if change == "attachment deleted":
        assert "在回收站里，过了恢复期限会清除" in stale.text
        assert "回收站里有这份文件，可以先去恢复" in browser_a.get(f"/imports/{import_id}/keep-file").text

    # Confirmed again, knowing no copy is kept, it ends: no endless refusal.
    again = browser_a.post(url, form_for(stale.text, url).fields)

    if kind == "temporary":
        assert again.headers["location"] == "/imports?notice=import_finished" and kept_files(app) == []
    else:
        assert again.headers["location"] == "/imports?notice=draft_finished" and draft(app, import_id).deleted_at is not None


def test_a_copy_kept_after_the_page_was_shown_also_asks_again(app, browser_a):
    import_id, project = imported_pdf(app, browser_a)
    url = f"/imports/{import_id}/finish"
    older = browser_a.form(url, url)
    keep(browser_a, import_id, project)

    stale = browser_a.post(url, older.fields)

    assert stale.status_code == 409 and row_of(app, import_id).state == ImportState.READ
    assert "原文件已另存为附件" in stale.text
    assert browser_a.post(url, form_for(stale.text, url).fields).headers["location"] == "/imports?notice=import_finished"
    assert len(attachments(app)) == 1


def test_a_copy_deleted_between_the_check_and_the_ending_ends_nothing(app, browser_a, clock, monkeypatch):
    """The check is repeated in the ending's own transaction, so another tab acting right after it is found."""
    import_id, project = imported_pdf(app, browser_a)
    keep(browser_a, import_id, project)
    (attachment,) = attachments(app)
    url = f"/imports/{import_id}/finish"
    older = browser_a.form(url, url)
    real, owner, acted = finish_web.copies_of, owner_id(app), []

    def checked_then_deleted_by_another_tab(*args, **kwargs):
        found = real(*args, **kwargs)

        if not acted:
            acted.append(True)

            with rt(app).database.transaction() as db:
                delete_attachment(db, owner, project, attachment.id, 1, clock.now())

        return found

    monkeypatch.setattr(finish_web, "copies_of", checked_then_deleted_by_another_tab)

    stale = browser_a.post(url, older.fields)

    assert acted and stale.status_code == 409 and "没有结束" in stale.text
    assert row_of(app, import_id).state == ImportState.READ and len(kept_files(app)) == 1
    assert attachments(app)[0].deleted_at is not None


def test_an_ending_form_without_what_it_was_shown_is_refused_and_pasted_text_ends_as_before(app, browser_a):
    import_id = paste(browser_a)
    url = f"/imports/{import_id}/finish"
    form = browser_a.form(url, url)

    assert form.fields["keeping"] == "none"
    unbound = {name: value for name, value in form.fields.items() if name != "keeping"}
    assert browser_a.post(url, unbound).status_code == 400 and row_of(app, import_id).state == ImportState.READ
    assert browser_a.post(url, form.fields).headers["location"] == "/imports?notice=import_finished"
