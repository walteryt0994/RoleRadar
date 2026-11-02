"""Saved resume import drafts (U2-B3a).

All material is synthetic and every candidate comes from the local
simulated rules. An import saved as a draft keeps everything it held and
no longer ends with its seven days; deleting it puts it in the recycle bin
for seven days with nothing removed; restoring brings it back as it was;
the deadline, or removing it for good, ends it like any import. Whatever
happens to a draft, what was adopted from it stays in the user's records
and is never written twice. Tests compare stored rows, not only what pages
say.
"""
import io
import json
import secrets
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.library import imports as imports_module
from app.library.errors import NotFound
from app.library.imports import INTERRUPTED, RestoreClosed, delete_draft, restore_draft, save_draft
from app.library.lifecycle import RESTORE_WINDOW
from app.library.models import (
    AuditEvent,
    ImportAdoption,
    ImportDraft,
    ImportState,
    ImportStructuring,
    ResumeImport,
    StructuringState,
)
from app.library.resume_reader import TIMEOUT
from app.library.review import Asked, structure_import
from import_samples import make_pdf
from library_helpers import Browser
from test_library_adoption import adopt_new, adopt_url, items, refused
from test_library_candidates import RESUME, Gate, ask, library_rows, one, owner_id, paste, rows, rt
from test_library_imports import PDF_FILE, import_id_from, kept_files, row_of, send

TRASH = "/experiences?tab=trash"
# A resume whose candidates the simulated rules find, as a PDF: a draft with a file and adoptions.
RESUME_PDF = ("合成简历.pdf", make_pdf([RESUME.splitlines()]), "application/pdf")

# --- helpers ------------------------------------------------------------------


def draft(app, import_id: str) -> ImportDraft | None:
    with rt(app).database.transaction() as db:
        return db.get(ImportDraft, import_id)


def review_state(app, import_id: str) -> tuple[list, list]:
    """The candidates as stored (ids, states, edits, revisions) and where adopted ones went."""
    with rt(app).database.transaction() as db:
        adopted = [
            (row.candidate_id, row.item_id, row.action, row.fields)
            for row in db.scalars(select(ImportAdoption).where(ImportAdoption.import_id == import_id))
        ]

    return [(row.id, row.kind, row.state, row.edits, row.revision) for row in rows(app, import_id)], sorted(adopted)


def import_events(app, import_id: str) -> list[str]:
    with rt(app).database.transaction() as db:
        return [
            event.action
            for event in db.scalars(select(AuditEvent).where(AuditEvent.object_id == import_id).order_by(AuditEvent.id))
        ]


def save(browser, import_id: str):
    return browser.submit(f"/imports/{import_id}", f"/imports/{import_id}/save")


def delete(browser, import_id: str):
    return browser.submit(f"/imports/{import_id}/delete", f"/imports/{import_id}/delete")


def restore(browser, import_id: str):
    return browser.submit(TRASH, f"/imports/{import_id}/restore")


def purge(browser, import_id: str):
    return browser.submit(f"/imports/{import_id}/purge", f"/imports/{import_id}/purge", acknowledge="yes")


def post_text(browser, text: str):
    """Send pasted text as the import form does, also when the page shows no form (at the limit)."""
    return browser.client.post(
        "/imports",
        data={"csrf_token": browser.csrf_token(), "upload_key": secrets.token_urlsafe(24), "text": text, "consent": "yes"},
        files={"file": ("", b"", "application/octet-stream")},
    )


def exported(browser) -> tuple[dict, dict, dict]:
    archive = zipfile.ZipFile(io.BytesIO(browser.get("/export/all.zip").content))
    files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}

    return json.loads(files["manifest.json"]), json.loads(files["data.json"]), files


def reviewed(app, browser) -> tuple[str, dict]:
    """A pasted import asked for candidates, one edited, one left out and one adopted as a new draft."""
    import_id = paste(browser)
    ask(browser, import_id)
    work, project, award = (one(app, import_id, kind) for kind in ("work", "project", "award"))
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    browser.submit(edit, edit, role="改过的合成岗位")
    browser.submit(f"/imports/{import_id}/review", f"/imports/{import_id}/candidates/{award.id}/exclude")
    assert "notice=adopted#" in adopt_new(browser, import_id, project.id).headers["location"]

    return import_id, {"work": work.id, "project": project.id, "award": award.id}


def while_the_lock_is_held(app, act, meanwhile):
    """Run act in a thread while another transaction holds the write lock; do meanwhile, then let go.

    act has to wait for the lock (BEGIN IMMEDIATE), so meanwhile happens
    between its arrival and the moment it really acts.
    """
    holding, release, result = threading.Event(), threading.Event(), {}

    def hold():
        with rt(app).database.transaction() as db:
            db.connection()
            holding.set()
            assert release.wait(10)

    def run():
        try:
            result["value"] = act()
        except (NotFound, RestoreClosed) as error:
            result["value"] = type(error).__name__

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    actor = threading.Thread(target=run)
    actor.start()
    time.sleep(0.3)
    meanwhile()
    release.set()
    holder.join(10)
    actor.join(10)

    return result["value"]


# --- saving ---------------------------------------------------------------------------------------------


def test_a_saved_draft_outlives_its_seven_days_and_stays_in_use_without_writing_anything(app, browser_a, clock):
    import_id, ids = reviewed(app, browser_a)
    records, review = library_rows(app), review_state(app, import_id)

    saved = save(browser_a, import_id)

    assert saved.headers["location"] == f"/imports/{import_id}?notice=draft_saved"
    assert draft(app, import_id).saved_at == clock.now() and draft(app, import_id).deleted_at is None
    # Saving is not adopting: the user's records are as they were, the review too.
    assert library_rows(app) == records and review_state(app, import_id) == review

    clock.advance(days=8)
    rt(app).run_cleanup()
    # The sign-in has run out by then; signing in again is all it takes.
    browser_a.sign_in("合成用户 A")

    assert row_of(app, import_id).state == ImportState.READ and review_state(app, import_id) == review
    for path in (f"/imports/{import_id}", f"/imports/{import_id}/review", "/imports"):
        page = browser_a.get(path).text
        assert "已保存的待核对草稿" in page and "保留到" not in page, path
    education = one(app, import_id, "education")
    edit_page = browser_a.get(f"/imports/{import_id}/candidates/{education.id}/edit").text
    assert "跟这份已保存的草稿一起保留" in edit_page and "不是长期草稿" not in edit_page
    assert browser_a.get(f"/imports/{import_id}/text").status_code == 200
    record = json.loads(browser_a.get(f"/imports/{import_id}/candidates.json").content)
    assert (record["kept_as"], record["kept_until"]) == ("saved", None)
    # Checking and adopting go on, explicitly; what was adopted is never written again.
    edit = f"/imports/{import_id}/candidates/{education.id}/edit"
    browser_a.submit(edit, edit, major="改过的合成专业")
    assert one(app, import_id, "education").edits == {"major": "改过的合成专业"}
    assert "notice=adopted#" in adopt_new(browser_a, import_id, education.id).headers["location"]
    assert browser_a.get(adopt_url(import_id, ids["project"])).headers["location"].endswith("notice=adopted_already#c-" + ids["project"])
    assert len(items(app, "project")) == 1 and len(items(app, "education")) == 1
    manifest, _, _ = exported(browser_a)
    (entry,) = manifest["imports"]
    assert (entry["kept_as"], entry["kept_until"], manifest["format_version"]) == ("saved", None, 7)


def test_a_saved_file_draft_keeps_its_file_past_the_seven_days(app, browser_a, clock):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    save(browser_a, import_id)
    clock.advance(days=8)
    rt(app).run_cleanup()
    browser_a.sign_in("合成用户 A")

    original = browser_a.get(f"/imports/{import_id}/original")

    assert original.status_code == 200 and original.content == PDF_FILE[1] and len(kept_files(app)) == 1
    assert browser_a.get(f"/imports/{import_id}/text").status_code == 200
    # In the recycle bin its file stays: the clean-up (and its sweep of files no row names) leaves it.
    delete(browser_a, import_id)
    rt(app).run_cleanup()
    assert len(kept_files(app)) == 1 and browser_a.get(f"/imports/{import_id}/original").status_code == 404
    restore(browser_a, import_id)
    assert browser_a.get(f"/imports/{import_id}/original").content == PDF_FILE[1]


def test_saving_at_the_deadline_is_refused_even_before_the_clean_up(app, browser_a, clock):
    import_id = paste(browser_a)
    form = browser_a.form(f"/imports/{import_id}", f"/imports/{import_id}/save")
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")

    late = browser_a.post(f"/imports/{import_id}/save", {**form.fields, "csrf_token": browser_a.csrf_token()})

    assert late.status_code == 404 and "找不到这次导入" in late.text and draft(app, import_id) is None
    rt(app).run_cleanup()
    assert row_of(app, import_id).state == ImportState.EXPIRED


def test_a_save_that_waited_for_the_lock_past_the_deadline_is_refused(app, browser_a, clock):
    import_id = paste(browser_a)
    clock.advance(days=7, seconds=-1)

    outcome = while_the_lock_is_held(
        app,
        lambda: save_draft(rt(app).database, owner_id(app), import_id, clock.now),
        lambda: clock.advance(seconds=2),
    )

    assert outcome == "NotFound" and draft(app, import_id) is None


def test_saving_waits_while_the_import_is_read_or_its_candidates_are_found(app, browser_a, clock):
    import_id = paste(browser_a)
    owner, gate = owner_id(app), Gate()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(structure_import, rt(app).database, gate, owner, import_id, clock.now)
        assert gate.entered.wait(10)
        busy = save(browser_a, import_id)
        gate.release.set()
        assert running.result(timeout=10) == Asked.STORED

    assert busy.status_code == 409 and "现在还不能保存" in busy.text and draft(app, import_id) is None
    assert save(browser_a, import_id).headers["location"].endswith("notice=draft_saved")

    reading, partial = paste(browser_a, RESUME + "\n（第二份）"), paste(browser_a, RESUME + "\n（第三份）")
    with rt(app).database.transaction() as db:
        db.get(ResumeImport, reading).state = ImportState.READING
        db.get(ResumeImport, partial).state = ImportState.PARTIAL

    assert save(browser_a, reading).status_code == 409 and draft(app, reading) is None
    # Reading only part of a file is no reason to refuse.
    assert save(browser_a, partial).headers["location"].endswith("notice=draft_saved")


def test_saving_twice_or_from_two_tabs_saves_once(app, browser_a):
    import_id = paste(browser_a)
    first = browser_a.form(f"/imports/{import_id}", f"/imports/{import_id}/save")
    second = browser_a.form(f"/imports/{import_id}", f"/imports/{import_id}/save")

    one_save = browser_a.post(f"/imports/{import_id}/save", first.fields)
    again = browser_a.post(f"/imports/{import_id}/save", second.fields)

    assert one_save.headers["location"].endswith("notice=draft_saved")
    assert again.headers["location"].endswith("notice=draft_already")
    assert import_events(app, import_id).count("import_saved") == 1


def test_an_older_discard_form_never_removes_a_saved_draft(app, browser_a):
    import_id, _ = reviewed(app, browser_a)
    older = browser_a.form(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")
    save(browser_a, import_id)
    text, review = row_of(app, import_id).segments, review_state(app, import_id)

    refused_post = browser_a.post(f"/imports/{import_id}/discard", older.fields)

    assert refused_post.status_code == 409 and "没有放弃：这是已保存的草稿" in refused_post.text
    assert row_of(app, import_id).segments == text and review_state(app, import_id) == review
    # Nor is it turned into a deletion behind the user's back.
    assert draft(app, import_id).deleted_at is None
    assert browser_a.get(f"/imports/{import_id}/discard").status_code == 409
    page = browser_a.get(f"/imports/{import_id}").text
    assert "删除这份草稿…" in page and f"/imports/{import_id}/discard" not in page


# --- the recycle bin ------------------------------------------------------------------------------------


def test_a_deleted_draft_is_out_of_use_until_restored_as_it_was(app, browser_a, clock):
    import_id, ids = reviewed(app, browser_a)
    education = one(app, import_id, "education")
    save(browser_a, import_id)
    adopt_form = browser_a.form(adopt_url(import_id, education.id), f"{adopt_url(import_id, education.id)}/new")
    edit_form = browser_a.form(
        f"/imports/{import_id}/candidates/{education.id}/edit", f"/imports/{import_id}/candidates/{education.id}/edit"
    )
    records, review = library_rows(app), review_state(app, import_id)

    deleted = delete(browser_a, import_id)

    assert deleted.headers["location"] == "/imports?notice=draft_deleted"
    assert (draft(app, import_id).deleted_at, draft(app, import_id).purge_after) == (clock.now(), clock.now() + RESTORE_WINDOW)
    for path in (
        f"/imports/{import_id}",
        f"/imports/{import_id}/review",
        f"/imports/{import_id}/text",
        f"/imports/{import_id}/candidates.json",
        f"/imports/{import_id}/candidates/{education.id}/edit",
        adopt_url(import_id, education.id),
    ):
        assert browser_a.get(path).status_code == 404, path
    assert browser_a.post(f"{adopt_url(import_id, education.id)}/new", adopt_form.fields).status_code == 404
    assert browser_a.post(edit_form.action, {**edit_form.fields, "major": "不该写入"}).status_code == 404
    assert browser_a.post(f"/imports/{import_id}/structure", {"csrf_token": browser_a.csrf_token()}).status_code == 404
    listing, trash = browser_a.get("/imports").text, browser_a.get(TRASH).text
    assert f'href="/imports/{import_id}"' not in listing and "回收站里还有 1 份删除的草稿" in listing
    assert "删除的导入草稿" in trash and f'action="/imports/{import_id}/restore"' in trash
    manifest, data, _ = exported(browser_a)
    (entry,) = manifest["imports"]
    assert entry["kept_as"] == "binned" and entry["kept_until"] == data["imports"][0]["kept_until"]
    assert data["imports"][0]["kept_as"] == "binned" and entry["candidates"] == len(review[0])

    restored = restore(browser_a, import_id)

    assert restored.headers["location"] == f"/imports/{import_id}?notice=draft_restored"
    assert draft(app, import_id).deleted_at is None and draft(app, import_id).purge_after is None
    # The same candidates, edits, choices and adoptions; the user's records untouched throughout.
    assert review_state(app, import_id) == review and library_rows(app) == records
    assert "notice=adopted_already" in browser_a.get(adopt_url(import_id, ids["project"])).headers["location"]
    assert len(items(app, "project")) == 1


def test_a_draft_past_its_restore_deadline_cannot_come_back_and_is_cleared(app, browser_a, clock):
    import_id, _ = reviewed(app, browser_a)
    save(browser_a, import_id)
    delete(browser_a, import_id)
    records = library_rows(app)
    form = browser_a.form(TRASH, f"/imports/{import_id}/restore")
    clock.advance(days=RESTORE_WINDOW.days)
    browser_a.sign_in("合成用户 A")
    form.fields["csrf_token"] = browser_a.csrf_token()

    late = browser_a.post(f"/imports/{import_id}/restore", form.fields)

    assert late.status_code == 409 and "不能恢复" in late.text
    trash = browser_a.get(TRASH).text
    assert "删除的导入草稿" not in trash and "导入草稿：1 份已经永久删除或超过恢复期限" in trash

    rt(app).run_cleanup()

    row = row_of(app, import_id)
    assert (row.state, row.segments, row.filename, row.sha256) == (ImportState.DELETED, None, None, None)
    assert draft(app, import_id) is None and rows(app, import_id) == [] and review_state(app, import_id) == ([], [])
    with rt(app).database.transaction() as db:
        assert db.get(ImportStructuring, import_id) is None
    # What was adopted stays as it was.
    assert library_rows(app) == records and len(items(app, "project")) == 1
    assert browser_a.post(f"/imports/{import_id}/restore", form.fields).status_code == 409
    assert "导入草稿：" not in browser_a.get(TRASH).text
    manifest, _, _ = exported(browser_a)
    assert manifest["imports"] == []
    # The row that only said an import existed is forgotten in time, like any ended import.
    clock.advance(days=31)
    rt(app).run_cleanup()
    assert row_of(app, import_id) is None and draft(app, import_id) is None


def test_a_restore_that_waited_for_the_lock_past_the_deadline_is_refused(app, browser_a, clock):
    import_id = paste(browser_a)
    save(browser_a, import_id)
    delete(browser_a, import_id)
    revision = row_of(app, import_id).revision
    clock.advance(days=RESTORE_WINDOW.days, seconds=-1)

    outcome = while_the_lock_is_held(
        app,
        lambda: restore_draft(rt(app).database, owner_id(app), import_id, revision, clock.now),
        lambda: clock.advance(seconds=2),
    )

    assert outcome == "RestoreClosed" and draft(app, import_id).deleted_at is not None


def test_removing_a_draft_for_good_asks_first_and_keeps_what_was_adopted(app, browser_a):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    adopted_id, _ = reviewed(app, browser_a)
    save(browser_a, import_id)
    save(browser_a, adopted_id)
    delete(browser_a, import_id)
    delete(browser_a, adopted_id)
    records = library_rows(app)
    form = browser_a.form(f"/imports/{import_id}/purge", f"/imports/{import_id}/purge")

    unconfirmed = browser_a.post(f"/imports/{import_id}/purge", form.fields)

    assert unconfirmed.status_code == 422 and "请先勾选" in unconfirmed.text and len(kept_files(app)) == 1
    assert purge(browser_a, import_id).headers["location"] == f"{TRASH}&notice=import_draft_purged"
    assert purge(browser_a, adopted_id).headers["location"] == f"{TRASH}&notice=import_draft_purged"
    assert kept_files(app) == [] and row_of(app, import_id).cleared_at is not None
    assert row_of(app, adopted_id).state == ImportState.DELETED and rows(app, adopted_id) == []
    assert library_rows(app) == records and len(items(app, "project")) == 1
    assert browser_a.post(f"/imports/{import_id}/restore", {**form.fields}).status_code == 409


def test_a_file_that_cannot_be_removed_yet_is_said_and_removed_later(app, browser_a, monkeypatch):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    save(browser_a, import_id)
    delete(browser_a, import_id)
    real, calls = rt(app).files.remove_file, []

    def busy_once(*args, **kwargs):
        calls.append(args)

        if len(calls) == 1:
            raise OSError("synthetic: the file is busy")

        return real(*args, **kwargs)

    monkeypatch.setattr(rt(app).files, "remove_file", busy_once)

    assert purge(browser_a, import_id).headers["location"] == f"{TRASH}&notice=import_draft_purge_pending"
    assert len(kept_files(app)) == 1 and row_of(app, import_id).segments is None
    assert "导入草稿：1 份已经永久删除或超过恢复期限" in browser_a.get(TRASH).text
    assert "已经不能查看或恢复" in browser_a.get(f"{TRASH}&notice=import_draft_purge_pending").text
    rt(app).run_cleanup()
    assert kept_files(app) == [] and row_of(app, import_id).cleared_at is not None
    assert "导入草稿：" not in browser_a.get(TRASH).text


def test_a_failed_write_changes_nothing_and_can_be_tried_again(app, browser_a):
    import_id = paste(browser_a)

    with refused(app, "INSERT INTO import_drafts"):
        failed_save = save(browser_a, import_id)
    assert failed_save.status_code == 500 and "没有保存" in failed_save.text and draft(app, import_id) is None
    assert save(browser_a, import_id).headers["location"].endswith("notice=draft_saved")

    with refused(app, "UPDATE import_drafts"):
        failed_delete = delete(browser_a, import_id)
    assert failed_delete.status_code == 500 and "没有删除" in failed_delete.text
    assert draft(app, import_id).deleted_at is None and browser_a.get(f"/imports/{import_id}").status_code == 200
    delete(browser_a, import_id)

    with refused(app, "UPDATE import_drafts"):
        failed_restore = restore(browser_a, import_id)
    assert failed_restore.status_code == 500 and "没有恢复" in failed_restore.text
    assert draft(app, import_id).deleted_at is not None

    with refused(app, "DELETE FROM import_drafts"):
        failed_purge = purge(browser_a, import_id)
    assert failed_purge.status_code == 500 and "没有永久删除" in failed_purge.text
    assert row_of(app, import_id).state == ImportState.READ and draft(app, import_id).deleted_at is not None
    assert restore(browser_a, import_id).headers["location"].endswith("notice=draft_restored")


def test_a_page_older_than_the_draft_deletes_or_restores_nothing(app, browser_a):
    import_id = paste(browser_a)
    save(browser_a, import_id)
    old_delete = browser_a.form(f"/imports/{import_id}/delete", f"/imports/{import_id}/delete")
    delete(browser_a, import_id)
    old_restore = browser_a.form(TRASH, f"/imports/{import_id}/restore")
    restore(browser_a, import_id)

    stale_delete = browser_a.post(f"/imports/{import_id}/delete", old_delete.fields)

    assert stale_delete.status_code == 409 and "没有删除" in stale_delete.text and draft(app, import_id).deleted_at is None
    delete(browser_a, import_id)
    stale_restore = browser_a.post(f"/imports/{import_id}/restore", old_restore.fields)
    assert stale_restore.status_code == 409 and "没有恢复" in stale_restore.text
    assert draft(app, import_id).deleted_at is not None


def test_drafts_are_their_owners_own(app, browser_a, browser_b):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    save(browser_a, import_id)
    form = browser_a.form(f"/imports/{import_id}/delete", f"/imports/{import_id}/delete")
    as_b = {**form.fields, "csrf_token": browser_b.csrf_token()}

    assert browser_b.post(f"/imports/{import_id}/save", {"csrf_token": browser_b.csrf_token()}).status_code == 404
    assert browser_b.get(f"/imports/{import_id}/delete").status_code == 404
    assert browser_b.post(f"/imports/{import_id}/delete", as_b).status_code == 404
    assert Browser(app).post(f"/imports/{import_id}/delete", form.fields).status_code == 401
    assert browser_a.post(f"/imports/{import_id}/delete", {"expected_revision": form.fields["expected_revision"]}).status_code == 403
    delete(browser_a, import_id)
    assert browser_b.post(f"/imports/{import_id}/restore", as_b).status_code == 404
    assert browser_b.get(f"/imports/{import_id}/purge").status_code == 404
    assert browser_b.post(f"/imports/{import_id}/purge", {**as_b, "acknowledge": "yes"}).status_code == 404
    assert "删除的导入草稿" not in browser_b.get(TRASH).text and exported(browser_b)[0]["imports"] == []
    assert draft(app, import_id).deleted_at is not None and len(kept_files(app)) == 1


# --- results that come late -----------------------------------------------------------------------------


def test_a_search_for_candidates_cut_off_by_a_delete_is_never_taken_in(app, browser_a, clock):
    import_id = paste(browser_a)
    save(browser_a, import_id)
    owner, gate = owner_id(app), Gate()

    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(structure_import, rt(app).database, gate, owner, import_id, clock.now)
        assert gate.entered.wait(10)
        delete(browser_a, import_id)
        restore(browser_a, import_id)
        gate.release.set()
        assert late.result(timeout=10) == Asked.DROPPED

    # Restoring started nothing: no candidates came in, and asking again is open.
    assert rows(app, import_id) == []
    with rt(app).database.transaction() as db:
        assert db.get(ImportStructuring, import_id).state == StructuringState.FAILED
    ask(browser_a, import_id)
    assert rows(app, import_id) != []


def test_a_reading_cut_off_by_a_delete_is_never_taken_in_even_after_a_restore(app, browser_a, clock, monkeypatch):
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    save(browser_a, import_id)
    with rt(app).database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        row.state, row.outcome, row.segments = ImportState.FAILED, TIMEOUT, None
    real_read, owner = rt(app).readings.read, owner_id(app)

    def deleted_and_restored_meanwhile(*args, **kwargs):
        revision = row_of(app, import_id).revision
        delete_draft(rt(app).database, owner, import_id, revision, clock.now)
        restore_draft(rt(app).database, owner, import_id, revision + 1, clock.now)

        return real_read(*args, **kwargs)

    monkeypatch.setattr(rt(app).readings, "read", deleted_and_restored_meanwhile)

    retried = browser_a.submit(f"/imports/{import_id}", f"/imports/{import_id}/retry")

    assert retried.headers["location"] == "/imports?notice=import_ended"
    row = row_of(app, import_id)
    assert (row.state, row.outcome, row.segments) == (ImportState.FAILED, INTERRUPTED, None)
    assert draft(app, import_id).deleted_at is None


# --- places ------------------------------------------------------------------------------------------------


def test_drafts_and_drafts_in_the_bin_keep_their_places(app, browser_a):
    imports = [paste(browser_a, f"{RESUME}\n（第 {number} 份）") for number in range(5)]
    save(browser_a, imports[0])
    save(browser_a, imports[1])
    delete(browser_a, imports[1])

    page = browser_a.get("/imports").text
    sixth = post_text(browser_a, "第六份合成文字")

    assert "达到上限" in page and "回收站里还能恢复的草稿" in page
    assert sixth.status_code == 409 and "达到上限" in sixth.text
    # Restoring takes back the place the draft kept, never one more; removing it for good frees it.
    assert restore(browser_a, imports[1]).headers["location"].endswith("notice=draft_restored")
    delete(browser_a, imports[1])
    purge(browser_a, imports[1])
    assert post_text(browser_a, "第六份合成文字").status_code == 303


# --- removing for good when a step after the deletion fails (Codex U2B3A-F1) -------------------------


def busy(*args, **kwargs):
    raise OSError("synthetic: the file is busy")


def failing_clean_up(monkeypatch, app, statement: str, *, busy_file: bool = False):
    """Make only the clean-up that follows an ending fail: these statements refused, the file kept busy."""
    real = imports_module._remove_file

    def clean_up(database, files, *args):
        with refused(app, statement), monkeypatch.context() as patch:
            if busy_file:
                patch.setattr(files, "remove_file", busy)

            return real(database, files, *args)

    monkeypatch.setattr(imports_module, "_remove_file", clean_up)


@pytest.mark.parametrize("phase", ["the deletion", "starting the clean-up", "recording the file gone", "noting a busy file"])
def test_a_failure_while_removing_a_draft_for_good_says_what_really_happened(app, browser_a, monkeypatch, phase):
    import_id = import_id_from(send(browser_a, file=RESUME_PDF))
    ask(browser_a, import_id)
    assert "notice=adopted#" in adopt_new(browser_a, import_id, one(app, import_id, "project").id).headers["location"]
    save(browser_a, import_id)
    delete(browser_a, import_id)
    records, review, text = library_rows(app), review_state(app, import_id), row_of(app, import_id).segments
    (kept,) = kept_files(app)
    original = kept.read_bytes()

    if phase == "the deletion":
        with refused(app, "DELETE FROM import_drafts"):
            response = purge(browser_a, import_id)
    else:
        statement = "SELECT" if phase == "starting the clean-up" else "UPDATE resume_imports"
        failing_clean_up(monkeypatch, app, statement, busy_file=phase == "noting a busy file")
        response = purge(browser_a, import_id)

    row = row_of(app, import_id)
    # What was adopted from the draft is the same in every case.
    assert library_rows(app) == records and len(items(app, "project")) == 1

    if phase == "the deletion":
        # Rolled back: nothing was removed, and saying so is true.
        assert response.status_code == 500 and "没有永久删除" in response.text and "恢复期限不变" in response.text
        assert (row.state, row.segments) == (ImportState.READ, text) and review_state(app, import_id) == review
        assert draft(app, import_id).deleted_at is not None and kept.read_bytes() == original
        assert restore(browser_a, import_id).headers["location"].endswith("notice=draft_restored")
        return

    # Committed: removed for good and said so; never offered back; the clean-up is left to finish.
    assert response.headers["location"] == f"{TRASH}&notice=import_draft_purge_pending"
    page = browser_a.get(response.headers["location"]).text
    assert "已经不能查看或恢复" in page and "没有永久删除" not in page and "导入草稿：1 份已经永久删除" in page
    assert (row.state, row.segments, row.filename, row.cleared_at) == (ImportState.DELETED, None, None, None)
    assert row.storage_key is not None and draft(app, import_id) is None
    assert review_state(app, import_id) == ([], [])
    assert kept.exists() == (phase in ("starting the clean-up", "noting a busy file"))
    assert phase != "noting a busy file" or row.cleanup_attempts == 0
    again = browser_a.post(
        f"/imports/{import_id}/restore", {"csrf_token": browser_a.csrf_token(), "expected_revision": str(row.revision)}
    )
    assert again.status_code == 409 and "不能恢复" in again.text

    # The fault gone, the clean-up pass finishes: the file, then the record that it is gone.
    monkeypatch.undo()
    rt(app).run_cleanup()

    row = row_of(app, import_id)
    assert row.cleared_at is not None and row.storage_key is None and kept_files(app) == []
    assert import_events(app, import_id)[-1] == "import_cleared"
    assert "导入草稿：" not in browser_a.get(TRASH).text
    assert library_rows(app) == records


def test_a_discard_whose_clean_up_fails_afterwards_is_still_said_to_be_done(app, browser_a, monkeypatch):
    """The same line in U2-B1's discard: once it is committed, a failed clean-up is a pending one."""
    import_id = import_id_from(send(browser_a, file=PDF_FILE))
    failing_clean_up(monkeypatch, app, "UPDATE resume_imports")

    response = browser_a.submit(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")

    assert response.headers["location"] == "/imports?notice=import_discard_pending"
    row = row_of(app, import_id)
    assert (row.state, row.segments, row.cleared_at) == (ImportState.DISCARDED, None, None) and kept_files(app) == []
    monkeypatch.undo()
    rt(app).run_cleanup()
    assert row_of(app, import_id).cleared_at is not None and row_of(app, import_id).storage_key is None
