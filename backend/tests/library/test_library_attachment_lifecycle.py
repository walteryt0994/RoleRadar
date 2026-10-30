"""Attachments in the recycle bin, with and without their experience; clean-up
(also of the staging names that uploads leave), "export all", and the
start-up checks of the attachment folder.

Times move with the manual clock; a session lasts 12 hours, so tests that
move days ahead sign in again. Files are synthetic bytes in the test root.
"""
import hashlib
import io
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.library import attachments as attachment_service
from app.library import export as export_module
from app.library import lifecycle
from app.library.config import FILES_DIR_ENV, check_files_path
from app.library.errors import ConfigError, StorageError, UnsafeStoragePath
from app.library.files import MARKER_NAME, FileStore, StagingFile
from app.library.models import Attachment, AttachmentState, ExperienceItem, JobStatus, PurgeJob, TargetType
from app.web_main import create_app
from library_helpers import JPEG, PDF, PNG, form_for


def database(app):
    return app.state.library.database


def store(app) -> FileStore:
    return app.state.library.files


def rows(app) -> list[Attachment]:
    with database(app).transaction() as db:
        return list(db.scalars(select(Attachment).order_by(Attachment.created_at, Attachment.id)))


def by_name(app) -> dict[str, Attachment]:
    return {row.filename: row for row in rows(app)}


def kept_file(app, row: Attachment) -> Path:
    return store(app).files_dir / row.owner_id / row.storage_key


def attachment_jobs(app) -> list[PurgeJob]:
    with database(app).transaction() as db:
        return list(db.scalars(select(PurgeJob).where(PurgeJob.target_type == TargetType.ATTACHMENT)))


def delete_attachment(browser, item_id, attachment_id):
    path = f"/experiences/{item_id}/attachments/{attachment_id}/delete"
    response = browser.submit(path, path)
    assert response.status_code == 303, response.text

    return response


def restore_form(browser, item_id, attachment_id):
    page = browser.get(f"/experiences/{item_id}").text

    return form_for(page, f"/experiences/{item_id}/attachments/{attachment_id}/restore")


def delete_experience(browser, item_id):
    path = f"/experiences/{item_id}/delete"
    response = browser.submit(path, path)
    assert response.status_code == 303, response.text


def restore_experience(browser, item_id):
    trash = browser.get("/experiences?tab=trash").text
    form = form_for(trash, f"/experiences/{item_id}/restore")

    return browser.post(form.action, form.fields)


def unzip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}


# --- one attachment in the recycle bin -----------------------------------------------


def test_a_deleted_attachment_waits_on_its_page_and_comes_back(app, browser_a):
    item_id = browser_a.create(title="项目", actions="做了一件事")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    assert browser_a.upload(item_id, "截图.png", PNG).status_code == 303
    proof = by_name(app)["证明.pdf"]
    confirm = browser_a.get(f"/experiences/{item_id}/attachments/{proof.id}/delete").text

    assert "删除附件：证明.pdf" in confirm
    assert "只删除这一个附件" in confirm

    response = delete_attachment(browser_a, item_id, proof.id)
    page = browser_a.get(response.headers["location"]).text
    current, _, binned = page.partition("已删除的附件")

    assert "已删除这个附件" in page
    assert f"/attachments/{proof.id}/download" not in page
    assert "证明.pdf" not in current.split('id="attachments"')[1]
    assert "证明.pdf" in binned and "之前可以恢复" in binned
    assert browser_a.get(f"/experiences/{item_id}/attachments/{proof.id}/download").status_code == 404
    assert kept_file(app, proof).read_bytes() == PDF

    form = restore_form(browser_a, item_id, proof.id)
    restored = browser_a.post(form.action, form.fields)

    assert restored.headers["location"].endswith("?notice=attachment_restored#attachments")
    assert browser_a.get(f"/experiences/{item_id}/attachments/{proof.id}/download").content == PDF
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.CANCELLED]


def test_from_exactly_seven_days_on_it_cannot_come_back_and_its_file_is_removed(app, browser_a, clock):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "到期.pdf").status_code == 303
    row = by_name(app)["到期.pdf"]
    delete_attachment(browser_a, item_id, row.id)
    stale = restore_form(browser_a, item_id, row.id)

    clock.advance(days=6, hours=23, minutes=59, seconds=59)
    browser_a.sign_in("合成用户 A")

    assert "还剩 1 分钟" in browser_a.get(f"/experiences/{item_id}").text

    clock.advance(seconds=1)
    response = browser_a.post(stale.action, {**stale.fields, "csrf_token": browser_a.csrf_token()})

    assert response.status_code == 404
    assert "到期.pdf" not in browser_a.get(f"/experiences/{item_id}").text
    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/purge").status_code == 404
    # Gone for use from the deadline on; the clean-up then really removes the file.
    assert kept_file(app, row).exists()

    app.state.library.run_cleanup()

    assert rows(app) == []
    assert not kept_file(app, row).exists()
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.DONE]
    assert "一个附件已清理完成（文件已删除）" in browser_a.get(f"/experiences/{item_id}/history").text


def test_permanent_deletion_says_done_only_once_the_file_is_gone(app, browser_a):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "永久删除.pdf").status_code == 303
    row = by_name(app)["永久删除.pdf"]
    delete_attachment(browser_a, item_id, row.id)
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    unconfirmed = browser_a.submit(purge, purge)

    assert unconfirmed.status_code == 422
    assert "请先勾选" in unconfirmed.text
    assert kept_file(app, row).exists()

    response = browser_a.submit(purge, purge, acknowledge="yes")
    page = browser_a.get(response.headers["location"]).text

    assert "已永久删除这个附件，它的文件已从附件目录中删除。" in page
    assert rows(app) == []
    assert not kept_file(app, row).exists()
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.DONE]


def _disk_fails(*args, **kwargs):
    raise OSError("disk unavailable")


def test_a_purge_that_cannot_remove_the_file_stays_unfinished_and_is_retried(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "删不掉.pdf").status_code == 303
    row = by_name(app)["删不掉.pdf"]
    delete_attachment(browser_a, item_id, row.id)
    stale = restore_form(browser_a, item_id, row.id)
    monkeypatch.setattr(FileStore, "remove_kept", _disk_fails)
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    response = browser_a.submit(purge, purge, acknowledge="yes")
    page = browser_a.get(response.headers["location"]).text

    assert "文件清理暂未完成，系统会自动重试" in page
    assert "已从附件目录中删除" not in page
    assert kept_file(app, row).exists()
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.FAILED]
    # Never restorable again, even though its row and file are still there.
    assert browser_a.post(stale.action, stale.fields).status_code == 404
    assert by_name(app)["删不掉.pdf"].deleted_at is not None

    monkeypatch.undo()
    clock.advance(minutes=10)
    app.state.library.run_cleanup()

    assert rows(app) == []
    assert not kept_file(app, row).exists()
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.DONE]


def test_a_purge_that_failed_after_removing_the_file_finishes_on_retry(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "一半.pdf").status_code == 303
    row = by_name(app)["一半.pdf"]
    delete_attachment(browser_a, item_id, row.id)

    def rows_fail(*args):
        raise RuntimeError("database failed after the file was removed")

    monkeypatch.setattr(lifecycle, "_remove", rows_fail)
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    response = browser_a.submit(purge, purge, acknowledge="yes")

    assert response.headers["location"].endswith("notice=attachment_purge_pending#attachments")
    # The file is gone but the job is not done, and nothing claims it is.
    assert not kept_file(app, row).exists()
    assert [r.id for r in rows(app)] == [row.id]
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.FAILED]

    monkeypatch.undo()
    clock.advance(minutes=10)
    app.state.library.run_cleanup()

    assert rows(app) == []
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.DONE]


def test_an_old_clean_up_job_never_removes_a_restored_attachment(app, browser_a, clock):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "恢复过的.pdf").status_code == 303
    row = by_name(app)["恢复过的.pdf"]
    delete_attachment(browser_a, item_id, row.id)
    form = restore_form(browser_a, item_id, row.id)
    assert browser_a.post(form.action, form.fields).status_code == 303

    clock.advance(days=8)
    app.state.library.run_cleanup()
    browser_a.sign_in("合成用户 A")

    assert [job.status for job in attachment_jobs(app)] == [JobStatus.CANCELLED]
    assert browser_a.get(f"/experiences/{item_id}/attachments/{row.id}/download").content == PDF


def test_an_old_page_cannot_restore_or_purge_a_later_deletion(app, browser_a):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "两次删除.pdf").status_code == 303
    row = by_name(app)["两次删除.pdf"]
    delete_attachment(browser_a, item_id, row.id)
    stale_restore = restore_form(browser_a, item_id, row.id)
    purge = f"/experiences/{item_id}/attachments/{row.id}/purge"
    stale_purge = browser_a.form(purge, purge)
    assert browser_a.post(stale_restore.action, stale_restore.fields).status_code == 303
    delete_attachment(browser_a, item_id, row.id)

    for form in (stale_restore, stale_purge):
        response = browser_a.post(form.action, {**form.fields, "acknowledge": "yes"})

        assert response.status_code == 409
        assert "这个附件的状态已经变化" in response.text

    assert by_name(app)["两次删除.pdf"].deleted_at is not None
    assert kept_file(app, row).exists()


# --- attachments go and come back with their experience ------------------------------


def test_deleting_an_experience_takes_its_attachments_and_restoring_brings_back_only_those(app, browser_a, clock):
    item_id = browser_a.create(title="带附件的经历", actions="经历")

    for name, data in (("一.pdf", PDF), ("二.png", PNG), ("三.jpg", JPEG)):
        assert browser_a.upload(item_id, name, data).status_code == 303

    first, second, third = (by_name(app)[name] for name in ("一.pdf", "二.png", "三.jpg"))
    delete_attachment(browser_a, item_id, third.id)
    own_deadline = by_name(app)["三.jpg"].purge_after

    clock.advance(days=1)
    browser_a.sign_in("合成用户 A")
    confirm = browser_a.get(f"/experiences/{item_id}/delete").text

    assert "2 个附件会一起移到回收站，恢复这条经历时会一起恢复。" in confirm
    assert "另有 1 个附件此前已经单独删除，会保持各自原来的恢复期限，不会随这条经历一起恢复。" in confirm

    delete_experience(browser_a, item_id)

    assert "连同 2 个附件" in browser_a.get("/experiences?tab=trash").text
    assert browser_a.get(f"/experiences/{item_id}/attachments/{first.id}/download").status_code == 404
    # Nothing of it can be restored on its own while the experience is in the bin.
    token = browser_a.csrf_token()
    response = browser_a.post(
        f"/experiences/{item_id}/attachments/{third.id}/restore",
        {"csrf_token": token, "expected_revision": str(by_name(app)["三.jpg"].revision)},
    )

    assert response.status_code == 404

    clock.advance(days=1)
    browser_a.sign_in("合成用户 A")
    assert restore_experience(browser_a, item_id).status_code == 303
    page = browser_a.get(f"/experiences/{item_id}").text
    current, _, binned = page.partition("已删除的附件")

    assert f"/attachments/{first.id}/download" in current and f"/attachments/{second.id}/download" in current
    assert "三.jpg" in binned
    assert by_name(app)["三.jpg"].purge_after == own_deadline
    assert browser_a.get(f"/experiences/{item_id}/attachments/{second.id}/download").content == PNG

    # The attachment deleted on its own still expires on its own deadline.
    clock.advance(days=5)
    app.state.library.run_cleanup()

    assert sorted(by_name(app)) == ["一.pdf", "二.png"]
    assert not kept_file(app, third).exists()
    assert kept_file(app, first).exists() and kept_file(app, second).exists()


def test_purging_an_experience_removes_every_attachment_file(app, browser_a):
    item_id = browser_a.create(actions="要永久删除的经历")
    assert browser_a.upload(item_id, "留着的.pdf").status_code == 303
    assert browser_a.upload(item_id, "单独删的.png", PNG).status_code == 303
    kept, alone = by_name(app)["留着的.pdf"], by_name(app)["单独删的.png"]
    delete_attachment(browser_a, item_id, alone.id)
    delete_experience(browser_a, item_id)
    purge = f"/experiences/{item_id}/purge"

    assert "以及它的 2 个附件文件" in browser_a.get(purge).text

    response = browser_a.submit(purge, purge, acknowledge="yes")

    assert response.headers["location"] == "/experiences?tab=trash&notice=purged"
    assert rows(app) == []
    assert not kept_file(app, kept).exists() and not kept_file(app, alone).exists()
    # The attachment's own clean-up is finished by its experience's.
    assert [job.status for job in attachment_jobs(app)] == [JobStatus.DONE]


def test_an_experience_purge_that_cannot_remove_files_keeps_its_rows_and_retries(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    row = by_name(app)["证明.pdf"]
    delete_experience(browser_a, item_id)
    monkeypatch.setattr(FileStore, "remove_kept", _disk_fails)
    purge = f"/experiences/{item_id}/purge"
    response = browser_a.submit(purge, purge, acknowledge="yes")

    assert response.headers["location"] == "/experiences?tab=trash&notice=purge_pending"
    assert kept_file(app, row).exists()
    assert [r.id for r in rows(app)] == [row.id]

    monkeypatch.undo()
    clock.advance(minutes=10)
    app.state.library.run_cleanup()

    assert rows(app) == []
    assert not kept_file(app, row).exists()

    with database(app).transaction() as db:
        assert db.get(ExperienceItem, item_id) is None


def test_an_experience_that_expires_takes_its_attachments_along(app, browser_a, clock):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    row = by_name(app)["证明.pdf"]
    delete_experience(browser_a, item_id)
    trash = browser_a.get("/experiences?tab=trash").text
    stale = form_for(trash, f"/experiences/{item_id}/restore")

    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")

    assert browser_a.post(stale.action, {**stale.fields, "csrf_token": browser_a.csrf_token()}).status_code == 404

    app.state.library.run_cleanup()

    assert rows(app) == []
    assert not kept_file(app, row).exists()


def test_a_failing_clean_up_step_does_not_stop_the_others(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="经历")
    files = store(app)
    leftover = files.staging_dir / ("a" * 32 + ".part")
    leftover.write_bytes(b"left over")
    old = os.path.getmtime(leftover) - 3601
    os.utime(leftover, (old, old))

    def purge_fails(*args):
        raise StorageError("database operation failed")

    monkeypatch.setattr("app.library.web.purge_due", purge_fails)

    assert app.state.library.run_cleanup() is None
    assert not leftover.exists()
    assert browser_a.get(f"/experiences/{item_id}").status_code == 200


# --- staging names that uploads leave ------------------------------------------------
#
# An upload's bytes also have a staging name until the upload removes it. A
# permanent deletion is done only once that name is gone too, wherever it
# was left: by a disk that refused the removal, or by a process that stopped.

# purged: (the notice once the clean-up is done, the notice while it is not)
NOTICES = {
    "attachment": ("notice=attachment_purged#attachments", "notice=attachment_purge_pending#attachments"),
    "experience": ("notice=purged", "notice=purge_pending"),
}


def staging_file(app, row: Attachment) -> Path:
    return store(app).staging_dir / f"{row.storage_key}.part"


def job_for(app, target_id: str) -> PurgeJob:
    with database(app).transaction() as db:
        return db.scalar(select(PurgeJob).where(PurgeJob.target_id == target_id))


def refuse_removals(monkeypatch, folder: Path) -> None:
    """Make removing any name in folder fail, as a disk or a changed permission might."""
    prefix = str(folder) + os.sep
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if str(path).startswith(prefix):
            raise PermissionError("the folder refuses removals")

        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)


def bin_for_purge(browser, item_id: str, row: Attachment, purged: str) -> tuple[str, str]:
    """Move the attachment, or its whole experience, to the bin; return the purge path and the job's target."""
    if purged == "attachment":
        delete_attachment(browser, item_id, row.id)
        return f"/experiences/{item_id}/attachments/{row.id}/purge", row.id

    delete_experience(browser, item_id)
    return f"/experiences/{item_id}/purge", item_id


@pytest.mark.parametrize("purged", ["attachment", "experience"])
def test_permanent_deletion_also_removes_the_staging_name_an_upload_left(app, browser_a, monkeypatch, purged):
    item_id = browser_a.create(title="要永久删除的", actions="经历")
    other_id = browser_a.create(title="另一条", actions="经历")

    # The uploads succeed, but the staging folder refuses removals meanwhile,
    # so each leaves its staging name: the same bytes, named after its key.
    with monkeypatch.context() as scoped:
        refuse_removals(scoped, store(app).staging_dir)

        for target, name in ((item_id, "删除.pdf"), (item_id, "同一条的.pdf"), (other_id, "另一条的.pdf")):
            assert browser_a.upload(target, name, PDF + name.encode()).status_code == 303

    named = by_name(app)

    for name, row in named.items():
        assert staging_file(app, row).read_bytes() == kept_file(app, row).read_bytes() == PDF + name.encode()

    purge, target_id = bin_for_purge(browser_a, item_id, named["删除.pdf"], purged)
    removed = {"删除.pdf"} if purged == "attachment" else {"删除.pdf", "同一条的.pdf"}
    running = store(app).new_staging()  # another upload, still being received
    running.write(PDF)
    response = browser_a.submit(purge, purge, acknowledge="yes")

    assert response.headers["location"].endswith(NOTICES[purged][0])
    assert job_for(app, target_id).status == JobStatus.DONE

    for name, row in named.items():
        if name in removed:
            assert not staging_file(app, row).exists() and not kept_file(app, row).exists()
        else:
            # Nothing but the purged files is touched.
            assert staging_file(app, row).exists() and kept_file(app, row).read_bytes() == PDF + name.encode()

    assert running.path.exists()
    assert set(by_name(app)) == set(named) - removed

    running.discard()


@pytest.mark.parametrize("purged", ["attachment", "experience"])
def test_a_purge_stays_unfinished_while_a_staging_name_remains_and_a_restart_finishes_it(
    app, browser_a, make_config, clock, monkeypatch, purged
):
    item_id = browser_a.create(actions="经历")
    refuse_removals(monkeypatch, store(app).staging_dir)
    assert browser_a.upload(item_id, "删不掉的暂存.pdf").status_code == 303
    row = by_name(app)["删不掉的暂存.pdf"]
    purge, target_id = bin_for_purge(browser_a, item_id, row, purged)
    response = browser_a.submit(purge, purge, acknowledge="yes")
    job = job_for(app, target_id)

    assert response.headers["location"].endswith(NOTICES[purged][1])
    assert (job.status, job.attempts, job.last_error) == (JobStatus.FAILED, 1, "PermissionError")

    # The next pass tries again; while the name is there, the job is not done,
    # and the record stays, so a later try still knows both names.
    clock.advance(minutes=10)
    app.state.library.run_cleanup()
    job = job_for(app, target_id)

    assert (job.status, job.attempts) == (JobStatus.FAILED, 2)
    assert staging_file(app, row).exists()
    assert [r.id for r in rows(app)] == [row.id]

    # A new process knows only the database and the attachment folder.
    monkeypatch.undo()
    restarted = create_app(make_config(), clock=clock)

    try:
        restarted.state.library.run_cleanup()
    finally:
        restarted.state.library.database.engine.dispose()

    assert job_for(app, target_id).status == JobStatus.DONE
    assert not staging_file(app, row).exists() and not kept_file(app, row).exists()
    assert rows(app) == []


@pytest.mark.parametrize("then", ["its experience is purged", "an hour passes"])
def test_an_upload_cut_off_after_placing_its_file_leaves_neither_name(app, browser_a, clock, monkeypatch, then):
    item_id = browser_a.create(actions="经历")

    # The process "stops" right after the file got its kept name: nothing after that runs.
    def stopped(*args):
        raise StorageError("process stopped")

    with monkeypatch.context() as scoped:
        scoped.setattr(attachment_service, "_activate", stopped)
        scoped.setattr(attachment_service, "abandon_upload", lambda *args, **kwargs: False)
        scoped.setattr(StagingFile, "discard", lambda self: None)
        browser_a.upload(item_id, "中断.pdf")

    [row] = rows(app)

    assert row.state == AttachmentState.PENDING
    assert staging_file(app, row).exists() and kept_file(app, row).exists()

    if then == "its experience is purged":
        delete_experience(browser_a, item_id)
        purge = f"/experiences/{item_id}/purge"

        assert browser_a.submit(purge, purge, acknowledge="yes").headers["location"].endswith("notice=purged")
    else:
        clock.advance(hours=1)
        app.state.library.run_cleanup()

    assert rows(app) == []
    assert not staging_file(app, row).exists() and not kept_file(app, row).exists()


def test_an_upload_still_running_when_its_experience_is_purged_leaves_nothing(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="上传期间被永久删除")
    real_place = FileStore.place

    def purged_first(self, staging, owner_id):
        # Between reserving and placing, the experience is deleted and purged.
        delete_experience(browser_a, item_id)
        purge = f"/experiences/{item_id}/purge"

        assert browser_a.submit(purge, purge, acknowledge="yes").headers["location"].endswith("notice=purged")

        real_place(self, staging, owner_id)

    monkeypatch.setattr(FileStore, "place", purged_first)
    response = browser_a.upload(item_id, "上传中.pdf")
    monkeypatch.undo()

    # The purge took the staging name first, so no kept name could be made
    # after it; the upload fails, and its experience is no longer there.
    assert response.status_code == 404
    assert "已上传附件" not in response.text
    assert rows(app) == []
    assert [path for path in store(app).root.rglob("*") if path.is_file() and path.name != MARKER_NAME] == []


# --- export all ----------------------------------------------------------------------


def test_export_all_carries_kept_and_restorable_attachment_files(app, browser_a):
    current = browser_a.create(title="当前经历", actions="a")
    binned = browser_a.create(title="回收站经历", actions="b")
    assert browser_a.upload(current, "证明.pdf", PDF + b"first").status_code == 303
    assert browser_a.upload(current, "证明.pdf", PDF + b"second").status_code == 303
    assert browser_a.upload(current, "截图.png", PNG).status_code == 303
    assert browser_a.upload(binned, "奖状.jpg", JPEG).status_code == 303
    delete_attachment(browser_a, current, by_name(app)["截图.png"].id)
    delete_experience(browser_a, binned)
    response = browser_a.get("/export/all.zip")
    files = unzip(response)
    manifest = json.loads(files["manifest.json"])
    data = json.loads(files["data.json"])
    readme = files["README.md"].decode("utf-8")
    entries = {entry["attachment_id"]: entry for entry in manifest["attachments"]}

    assert response.headers["content-disposition"].endswith('.zip"')
    assert "incomplete" not in response.headers["content-disposition"]
    assert manifest["complete"] is True and data["complete"] is True
    assert manifest["counts"]["attachments"] == 4
    assert len(entries) == 4

    for row in rows(app):
        entry = entries[row.id]
        payload = files[entry["path"]]

        assert entry["status"] == "included"
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"] == row.sha256
        assert len(payload) == entry["bytes"] == row.size
        assert entry["filename"] == row.filename

    sections = sorted((entry["section"], entry["path"].rsplit("/", 1)[0]) for entry in entries.values())
    current_name = f"01-{current[:8]}"
    binned_name = f"01-{binned[:8]}"

    assert sections == [
        ("current", f"experiences/{current_name}-attachments"),
        ("current", f"experiences/{current_name}-attachments"),
        ("recycle-bin", f"recycle-bin/{binned_name}-attachments"),
        ("recycle-bin", f"recycle-bin/deleted-attachments/{current_name}"),
    ]
    # Equal names never overwrite each other.
    assert sorted(entry["path"].rsplit("/", 1)[1] for entry in entries.values() if entry["filename"] == "证明.pdf") == [
        "01-证明.pdf",
        "02-证明.pdf",
    ]
    assert all(entry["restore_deadline"] for entry in entries.values() if entry["section"] == "recycle-bin")

    listed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}

    for path, payload in files.items():
        if path != "manifest.json":
            assert listed[path] == hashlib.sha256(payload).hexdigest()

    [kept_record] = data["experiences"]
    [binned_record] = data["recycle_bin"]

    assert sorted(a["filename"] for a in kept_record["attachments"]) == ["截图.png", "证明.pdf", "证明.pdf"]
    assert [a["deleted_with_experience"] for a in binned_record["attachments"]] == [True]
    assert next(a for a in kept_record["attachments"] if a["filename"] == "截图.png")["deleted_with_experience"] is False

    markdown = files[f"experiences/{current_name}.md"].decode("utf-8")

    assert "## 附件（3 个）" in markdown
    assert f"文件在导出包的 `experiences/{current_name}-attachments/01-证明.pdf`" in markdown
    assert "截图.png（PNG" in markdown and "已单独删除" in markdown
    assert "共 2 个" in readme and "不完整" not in readme
    # What U1-B exported is still there.
    assert "README.md" in files and "data.json" in files
    assert leftovers_of(app) == []


def leftovers_of(app) -> list[Path]:
    return sorted([*store(app).staging_dir.iterdir(), *store(app).exports_dir.iterdir()])


def test_export_all_says_which_files_it_could_not_include(app, browser_a):
    item_id = browser_a.create(title="文件出问题的经历", actions="a")

    for name in ("缺失.pdf", "被改过.pdf", "读不了.pdf", "正常.pdf"):
        assert browser_a.upload(item_id, name, PDF + name.encode()).status_code == 303

    named = by_name(app)
    kept_file(app, named["缺失.pdf"]).unlink()
    changed = kept_file(app, named["被改过.pdf"])
    changed.write_bytes(b"X" * changed.stat().st_size)
    unreadable = kept_file(app, named["读不了.pdf"])
    unreadable.chmod(0)

    try:
        response = browser_a.get("/export/all.zip")
    finally:
        unreadable.chmod(0o600)

    files = unzip(response)
    manifest = json.loads(files["manifest.json"])
    statuses = {entry["filename"]: (entry["status"], entry["path"]) for entry in manifest["attachments"]}
    readme = files["README.md"].decode("utf-8")
    markdown = next(payload for path, payload in files.items() if path.startswith("experiences/") and path.endswith(".md"))

    assert response.headers["content-disposition"].endswith('-incomplete.zip"')
    assert manifest["complete"] is False
    assert statuses["缺失.pdf"] == ("missing", None)
    assert statuses["被改过.pdf"] == ("changed", None)

    if os.geteuid() != 0:
        assert statuses["读不了.pdf"] == ("unreadable", None)

    assert statuses["正常.pdf"][0] == "included"
    assert manifest["counts"]["attachments"] == 1
    # No made-up or empty files stand in for the missing ones.
    assert [path for path in files if "-attachments/" in path] == [statuses["正常.pdf"][1]]
    assert "这个导出包不完整：有 3 个附件的文件没能放进来" in readme
    assert "缺失.pdf" in readme and "文件缺失，没有包含在导出包里" in markdown.decode("utf-8")


def test_expired_and_purged_attachments_are_never_exported(app, browser_a, clock):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "到期.pdf", PDF + b"expired-sentinel-1f3a").status_code == 303
    assert browser_a.upload(item_id, "永久删除.pdf", PDF + b"purged-sentinel-9c2e").status_code == 303
    named = by_name(app)
    delete_attachment(browser_a, item_id, named["到期.pdf"].id)
    delete_attachment(browser_a, item_id, named["永久删除.pdf"].id)
    purge = f"/experiences/{item_id}/attachments/{named['永久删除.pdf'].id}/purge"
    assert browser_a.submit(purge, purge, acknowledge="yes").status_code == 303

    # From its deadline on, before or after the clean-up ran.
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")
    everything = b"".join(unzip(browser_a.get("/export/all.zip")).values())

    assert b"expired-sentinel-1f3a" not in everything and b"purged-sentinel-9c2e" not in everything
    assert "到期.pdf".encode() not in everything and "永久删除.pdf".encode() not in everything


def test_the_export_package_is_built_in_the_private_folder_and_leaves_nothing(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    made = []
    real_new_export = FileStore.new_export

    def recorded(self):
        path, handle = real_new_export(self)
        made.append(path)
        return path, handle

    monkeypatch.setattr(FileStore, "new_export", recorded)
    response = browser_a.get("/export/all.zip")

    assert response.status_code == 200
    assert [path.parent for path in made] == [store(app).exports_dir]
    assert not made[0].exists()
    assert leftovers_of(app) == []

    def copy_fails(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(export_module, "_copy_attachment", copy_fails)
    failed = browser_a.get("/export/all.zip")

    assert failed.status_code == 500
    assert "导出没有完成" in failed.text
    assert leftovers_of(app) == []


def test_the_export_package_has_no_name_while_it_is_written(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="经历")
    assert browser_a.upload(item_id, "证明.pdf").status_code == 303
    seen = []
    real_copy = export_module._copy_attachment

    def look(*args):
        seen.append(sorted(store(app).exports_dir.iterdir()))
        return real_copy(*args)

    monkeypatch.setattr(export_module, "_copy_attachment", look)

    assert browser_a.get("/export/all.zip").status_code == 200
    # While the attachment was copied in, the folder held no name for the
    # package: a process stopping there leaves no copy behind.
    assert seen == [[]]

    # A name that cannot be removed stops the export before a byte is written.
    refuse_removals(monkeypatch, store(app).exports_dir)
    failed = browser_a.get("/export/all.zip")
    [left] = store(app).exports_dir.iterdir()

    assert failed.status_code == 500
    assert len(seen) == 1
    assert left.stat().st_size == 0


def test_the_single_markdown_export_lists_attachments_without_their_bytes(app, browser_a):
    item_id = browser_a.create(title="单条导出", actions="a")
    assert browser_a.upload(item_id, "证明.pdf", PDF + b"bytes-sentinel-77d0").status_code == 303
    text = browser_a.get(f"/experiences/{item_id}/export.md").content.decode("utf-8")

    assert "## 附件（1 个）" in text
    assert "- 证明.pdf（PDF，" in text
    assert "附件文件本身不在这个 Markdown 文件里" in text
    assert "bytes-sentinel-77d0" not in text


# --- the attachment folder at start-up ---------------------------------------------------


def test_the_launcher_points_the_attachment_folder_into_the_test_root(test_root):
    assert Path(os.environ[FILES_DIR_ENV]).parent == test_root


def test_the_attachment_folder_must_be_named(make_config, tmp_path):
    with pytest.raises(ConfigError, match="明确指定附件目录"):
        create_app(make_config(files_dir=None))

    assert not (tmp_path / "library.db").exists()


@pytest.fixture
def fake_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / "backend" / "records").mkdir(parents=True)

    return repo


@pytest.mark.parametrize(
    "place, message",
    [
        ("relative", "绝对路径"),
        ("in the repository", "代码仓库"),
        ("around the repository", "代码仓库"),
        ("in records", "records 目录"),
        ("around records", "records 目录"),
        ("around the database", "数据库不能放在附件目录里"),
        ("the database itself", "数据库不能放在附件目录里"),
        ("around a legacy database", "旧版 RoleRadar 数据库"),
        ("outside the test root", "测试临时目录"),
        ("a file", "指向了一个文件"),
    ],
)
def test_the_attachment_folder_guard(tmp_path, fake_repo, place, message):
    database_file = tmp_path / "data" / "library.db"
    legacy = tmp_path / "old" / "roleradar.db"
    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"")
    folder = {
        "relative": Path("files"),
        "in the repository": fake_repo / "files",
        "around the repository": tmp_path,
        "in records": tmp_path / "records" / "files",
        "around records": tmp_path / "records-parent",
        "around the database": tmp_path / "data",
        "the database itself": database_file,
        "around a legacy database": tmp_path / "old",
        "outside the test root": tmp_path / "elsewhere",
        "a file": a_file,
    }[place]
    test_root = tmp_path / "root" if place == "outside the test root" else None

    with pytest.raises(UnsafeStoragePath, match=message):
        check_files_path(
            folder,
            db_path=database_file,
            test_root=test_root,
            repo_root=fake_repo,
            records_dir=tmp_path / ("records-parent/records" if place == "around records" else "records"),
            legacy_paths=[legacy],
        )


def test_a_folder_holding_other_files_is_refused_before_anything_is_created(make_config, tmp_path):
    folder = tmp_path / "busy"
    folder.mkdir()
    (folder / "notes.txt").write_text("别人的文件", encoding="utf-8")

    with pytest.raises(ConfigError, match="已有其他内容"):
        create_app(make_config(files_dir=folder))

    assert sorted(path.name for path in folder.iterdir()) == ["notes.txt"]
    assert not (tmp_path / "library.db").exists()


def test_an_attachment_folder_belongs_to_one_database(make_config, tmp_path, clock):
    create_app(make_config(), clock=clock).state.library.database.engine.dispose()
    marker = (tmp_path / "files" / MARKER_NAME).read_text(encoding="ascii")

    # A new database cannot take it over: refused before its file is made.
    with pytest.raises(ConfigError, match="属于另一个经历库数据库"):
        create_app(make_config(db_path=tmp_path / "second.db"))

    assert not (tmp_path / "second.db").exists()

    # Another existing database cannot use it either, and is left as it was.
    other = create_app(make_config(db_path=tmp_path / "third.db", files_dir=tmp_path / "third-files"))
    other.state.library.database.engine.dispose()
    digest = hashlib.sha256((tmp_path / "third.db").read_bytes()).hexdigest()

    with pytest.raises(ConfigError, match="属于另一个经历库数据库"):
        create_app(make_config(db_path=tmp_path / "third.db"))

    assert hashlib.sha256((tmp_path / "third.db").read_bytes()).hexdigest() == digest
    assert (tmp_path / "files" / MARKER_NAME).read_text(encoding="ascii") == marker
    # Its own database opens it again.
    create_app(make_config(), clock=clock).state.library.database.engine.dispose()


def test_a_version_2_database_is_refused_before_anything_is_written(make_config, tmp_path):
    path = tmp_path / "library.db"

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE library_meta (key VARCHAR(50) PRIMARY KEY, value VARCHAR(200))")
        connection.execute("INSERT INTO library_meta VALUES ('schema_version', '2')")
        connection.execute("CREATE TABLE experience_items (id VARCHAR(36) PRIMARY KEY)")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(ConfigError, match="结构版本是 2，当前程序只支持 3"):
        create_app(make_config())

    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # The attachment folder was not claimed for it either.
    assert not (tmp_path / "files").exists()
