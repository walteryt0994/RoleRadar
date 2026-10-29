"""Deleting, the 7-day restore window, permanent deletion and clean-up."""
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.library import lifecycle
from app.library.content import parse_experience_form
from app.library.errors import Conflict, NotAllowed, NotFound, VersionConflict
from app.library.lifecycle import (
    RESTORE_WINDOW,
    delete_experience,
    delete_version,
    purge_due,
    request_experience_purge,
    request_version_purge,
    restore_experience,
    restore_version,
    run_purge_job,
    trash_view,
    trashed_item,
    version_cleanups,
    version_state,
)
from app.library.models import (
    AuditEvent,
    ExperienceItem,
    ExperienceVersion,
    JobStatus,
    PurgeJob,
    PurgeReason,
    VersionKind,
)
from app.library.service import (
    active_item,
    confirm_experience,
    create_experience,
    experience_history,
    list_experiences,
    save_draft,
)
from app.web_main import create_app
from library_helpers import LOCAL_BASE_URL, Browser


def entry(text):
    return parse_experience_form({"actions": text})


def create(database, owner, clock, text="一段经历"):
    with database.transaction() as db:
        return create_experience(db, owner, entry(text), clock.now()).id


def revision(database, item_id):
    with database.transaction() as db:
        return db.get(ExperienceItem, item_id).revision


def delete(database, owner, item_id, clock):
    current = revision(database, item_id)

    with database.transaction() as db:
        return delete_experience(db, owner, item_id, current, clock.now())


def restore(database, owner, item_id, clock):
    current = revision(database, item_id)

    with database.transaction() as db:
        return restore_experience(db, owner, item_id, current, clock.now())


def current_state(db, version_id):
    return version_state(db, db.get(ExperienceVersion, version_id))


def row(database, model, row_id):
    with database.transaction() as db:
        return db.get(model, row_id)


def jobs(database):
    with database.transaction() as db:
        return list(db.scalars(select(PurgeJob).order_by(PurgeJob.created_at, PurgeJob.id)))


def versions(database, item_id):
    with database.transaction() as db:
        return list(
            db.scalars(
                select(ExperienceVersion)
                .where(ExperienceVersion.item_id == item_id)
                .order_by(ExperienceVersion.created_at, ExperienceVersion.kind)
            )
        )


def test_deleting_takes_the_experience_out_of_normal_use_at_once(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    deleted = delete(database, owner, item_id, clock)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            active_item(db, owner, item_id)

        assert list_experiences(db, owner) == []
        assert [item.id for item in trash_view(db, owner, clock.now()).items] == [item_id]

    assert deleted.purge_after == clock.now() + RESTORE_WINDOW
    assert RESTORE_WINDOW == timedelta(days=7)


def test_restoring_works_until_one_second_before_the_deadline(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=7, seconds=-1)
    restore(database, owner, item_id, clock)

    with database.transaction() as db:
        assert active_item(db, owner, item_id).deleted_at is None


def test_from_the_deadline_on_it_is_gone_even_before_clean_up_runs(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=7)

    current = revision(database, item_id)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            restore_experience(db, owner, item_id, current, clock.now())

        with pytest.raises(NotFound):
            trashed_item(db, owner, item_id, clock.now())

        view = trash_view(db, owner, clock.now())

    assert view.items == []
    assert [job.target_id for job in view.waiting_cleanups] == [item_id]
    # Access is refused by the deadline alone; the rows are still there.
    assert row(database, ExperienceItem, item_id) is not None


def test_clean_up_removes_due_rows_and_records_that_it_finished(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=7)
    report = purge_due(database, clock.now())
    [job] = jobs(database)

    with database.transaction() as db:
        actions = list(db.scalars(select(AuditEvent.action).where(AuditEvent.item_id == item_id)))

    assert report.done == 1
    assert row(database, ExperienceItem, item_id) is None
    assert versions(database, item_id) == []
    assert job.status == JobStatus.DONE
    assert job.completed_at == clock.now()
    assert actions[-1] == "purge_completed"


def test_clean_up_leaves_experiences_that_are_not_due_yet(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=6, hours=23)
    report = purge_due(database, clock.now())

    assert report.done == 0
    assert row(database, ExperienceItem, item_id) is not None


def test_restoring_cancels_the_clean_up_for_good(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock, text="恢复后要一直保留")
    delete(database, owner, item_id, clock)
    clock.advance(days=1)
    restore(database, owner, item_id, clock)
    clock.advance(days=30)
    purge_due(database, clock.now())

    with database.transaction() as db:
        item = active_item(db, owner, item_id)

    assert item.content["actions"] == "恢复后要一直保留"
    assert [job.status for job in jobs(database)] == [JobStatus.CANCELLED]


def test_a_leftover_job_cannot_remove_a_restored_experience(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    restore(database, owner, item_id, clock)

    # Simulate a job that missed its cancellation (for example after a crash).
    with database.transaction() as db:
        db.scalar(select(PurgeJob)).status = JobStatus.PENDING

    clock.advance(days=8)
    report = purge_due(database, clock.now())

    assert report.cancelled == 1
    assert row(database, ExperienceItem, item_id).deleted_at is None


def test_deleting_again_starts_a_new_seven_days(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=1)
    restore(database, owner, item_id, clock)
    clock.advance(days=1)
    second = delete(database, owner, item_id, clock)

    assert second.purge_after == start + timedelta(days=9)

    clock.current = start + timedelta(days=8)
    purge_due(database, clock.now())

    with database.transaction() as db:
        assert trashed_item(db, owner, item_id, clock.now()).id == item_id

    clock.current = start + timedelta(days=9)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            trashed_item(db, owner, item_id, clock.now())


def test_viewing_and_failed_attempts_do_not_extend_the_deadline(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    deadline = delete(database, owner, item_id, clock).purge_after
    clock.advance(days=3)

    with database.transaction() as db:
        trash_view(db, owner, clock.now())

    with pytest.raises(Conflict):
        with database.transaction() as db:
            restore_experience(db, owner, item_id, 1, clock.now())

    purge_due(database, clock.now())

    assert row(database, ExperienceItem, item_id).purge_after == deadline


def test_permanent_deletion_is_unrestorable_at_once_and_then_cleaned_up(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(hours=1)

    current = revision(database, item_id)

    with database.transaction() as db:
        job_id = request_experience_purge(db, owner, item_id, current, clock.now())

    with database.transaction() as db:
        with pytest.raises(NotFound):
            trashed_item(db, owner, item_id, clock.now())

    outcome = run_purge_job(database, job_id, clock.now())
    [job] = jobs(database)

    assert outcome == "done"
    assert job.reason == PurgeReason.USER_REQUEST
    assert row(database, ExperienceItem, item_id) is None


def test_a_failed_clean_up_is_recorded_and_retried_and_never_reopens_access(
    database, owners, clock, monkeypatch
):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=7)
    working_remove = lifecycle._remove

    def unavailable_disk(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(lifecycle, "_remove", unavailable_disk)
    first = purge_due(database, clock.now())
    [job] = jobs(database)

    assert first.failed == 1
    assert (job.status, job.attempts, job.last_error) == (JobStatus.FAILED, 1, "OSError")
    assert row(database, ExperienceItem, item_id) is not None

    with database.transaction() as db:
        with pytest.raises(NotFound):
            trashed_item(db, owner, item_id, clock.now())

        assert [j.status for j in trash_view(db, owner, clock.now()).waiting_cleanups] == [
            JobStatus.FAILED
        ]

    monkeypatch.setattr(lifecycle, "_remove", working_remove)
    clock.advance(minutes=10)
    second = purge_due(database, clock.now())
    [job] = jobs(database)

    assert second.done == 1
    assert (job.status, job.attempts) == (JobStatus.DONE, 2)
    assert row(database, ExperienceItem, item_id) is None


def test_a_clean_up_that_fails_halfway_is_rolled_back_as_a_whole(database, owners, clock, monkeypatch):
    owner, _ = owners
    item_id = create(database, owner, clock)
    delete(database, owner, item_id, clock)
    clock.advance(days=7)
    working_remove = lifecycle._remove

    def fails_after_removing(db, job, target, now):
        working_remove(db, job, target, now)
        raise RuntimeError("crashed after deleting rows")

    monkeypatch.setattr(lifecycle, "_remove", fails_after_removing)
    purge_due(database, clock.now())

    assert row(database, ExperienceItem, item_id) is not None
    assert len(versions(database, item_id)) == 1


def test_the_current_confirmed_version_cannot_be_deleted_on_its_own(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        item = confirm_experience(db, owner, item_id, 1, clock.now())

    with pytest.raises(NotAllowed):
        with database.transaction() as db:
            delete_version(
                db,
                owner,
                item_id,
                item.confirmed_version_id,
                current_state(db, item.confirmed_version_id),
                clock.now(),
            )


def test_a_history_version_has_its_own_seven_day_window(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        delete_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

    clock.advance(days=7, seconds=-1)

    with database.transaction() as db:
        restore_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())
        delete_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

    clock.advance(days=7)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            restore_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

        history = experience_history(db, owner, item_id, clock.now())
        waiting, _ = version_cleanups(db, owner, item_id, clock.now())

    assert history.versions == []
    assert [job.target_id for job in waiting] == [initial.id]

    purge_due(database, clock.now())

    assert row(database, ExperienceVersion, initial.id) is None
    assert row(database, ExperienceItem, item_id) is not None


def test_restoring_an_experience_leaves_separately_deleted_versions_in_the_bin(
    database, owners, clock
):
    owner, _ = owners
    start = clock.now()
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        item = confirm_experience(db, owner, item_id, 1, clock.now())
        confirmed_id = item.confirmed_version_id
        save_draft(db, owner, item_id, 2, entry("改过以后"), clock.now())
        delete_version(db, owner, item_id, confirmed_id, current_state(db, confirmed_id), clock.now())

    clock.advance(hours=1)
    delete(database, owner, item_id, clock)
    clock.advance(hours=1)
    restore(database, owner, item_id, clock)
    kept = {version.kind: version for version in versions(database, item_id)}

    assert kept[VersionKind.INITIAL].deleted_at is None
    assert kept[VersionKind.CONFIRMED].deleted_at is not None
    assert kept[VersionKind.CONFIRMED].purge_after == start + RESTORE_WINDOW


def test_purging_an_experience_takes_versions_waiting_in_the_bin_with_it(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        delete_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

    clock.advance(hours=1)
    delete(database, owner, item_id, clock)

    current = revision(database, item_id)

    with database.transaction() as db:
        job_id = request_experience_purge(db, owner, item_id, current, clock.now())

    run_purge_job(database, job_id, clock.now())

    assert versions(database, item_id) == []
    assert {job.status for job in jobs(database)} == {JobStatus.DONE}


def test_a_permanently_deleted_version_cannot_be_restored(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        delete_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())
        state = current_state(db, initial.id)
        job_id = request_version_purge(db, owner, item_id, initial.id, state, clock.now())

    with database.transaction() as db:
        with pytest.raises(NotFound):
            restore_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

    assert run_purge_job(database, job_id, clock.now()) == "done"
    assert row(database, ExperienceVersion, initial.id) is None


def test_another_owner_cannot_delete_restore_or_purge(database, owners, clock):
    owner, other = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            delete_experience(db, other, item_id, 1, clock.now())

        with pytest.raises(NotFound):
            delete_version(db, other, item_id, initial.id, current_state(db, initial.id), clock.now())

    delete(database, owner, item_id, clock)
    current_revision = revision(database, item_id)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            restore_experience(db, other, item_id, current_revision, clock.now())

        with pytest.raises(NotFound):
            request_experience_purge(db, other, item_id, current_revision, clock.now())

        assert trash_view(db, other, clock.now()).items == []


def test_start_up_catches_up_on_clean_up_missed_while_the_app_was_stopped(make_config, clock):
    config = make_config()
    first_run = create_app(config, clock=clock)

    with TestClient(first_run, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        browser = Browser.signed_in(first_run, "合成用户 A")
        item_id = browser.create(actions="停机前删除的经历")
        response = browser.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
        assert response.status_code == 303

    clock.advance(days=8)
    second_run = create_app(config, clock=clock)

    with TestClient(second_run, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        with second_run.state.library.database.transaction() as db:
            assert db.get(ExperienceItem, item_id) is None
            assert db.scalar(select(PurgeJob)).status == JobStatus.DONE


def test_the_clean_up_loop_keeps_running_after_an_unexpected_error(make_config, clock, monkeypatch):
    application = create_app(make_config(purge_interval_seconds=0.01), clock=clock)
    runtime = application.state.library
    calls = []

    def flaky_cleanup():
        calls.append(len(calls))

        if len(calls) == 2:
            raise RuntimeError("unexpected")

    monkeypatch.setattr(runtime, "run_cleanup", flaky_cleanup)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        deadline = time.monotonic() + 5

        while len(calls) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)

    # Call 1 is the start-up pass; call 2 fails inside the loop; later calls prove it survived.
    assert len(calls) >= 4


def test_the_version_state_changes_with_every_deletion_and_restore(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        kept = current_state(db, initial.id)
        delete_version(db, owner, item_id, initial.id, kept, clock.now())
        first_deletion = current_state(db, initial.id)
        restore_version(db, owner, item_id, initial.id, first_deletion, clock.now())
        kept_again = current_state(db, initial.id)
        delete_version(db, owner, item_id, initial.id, kept_again, clock.now())
        second_deletion = current_state(db, initial.id)

    assert (kept, kept_again) == ("0:kept", "1:kept")
    assert len({kept, first_deletion, kept_again, second_deletion}) == 4


def delete_restore_delete(database, owner, item_id, version_id, clock):
    """Return the version's state after the first deletion, then restore and delete it again."""
    with database.transaction() as db:
        delete_version(db, owner, item_id, version_id, current_state(db, version_id), clock.now())
        first_deletion = current_state(db, version_id)
        restore_version(db, owner, item_id, version_id, first_deletion, clock.now())
        delete_version(db, owner, item_id, version_id, current_state(db, version_id), clock.now())

    return first_deletion


def test_an_old_permanent_delete_cannot_reach_a_later_deletion_of_the_version(
    database, owners, clock
):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)
    stale = delete_restore_delete(database, owner, item_id, initial.id, clock)

    with pytest.raises(VersionConflict):
        with database.transaction() as db:
            request_version_purge(db, owner, item_id, initial.id, stale, clock.now())

    version = row(database, ExperienceVersion, initial.id)

    statuses = {job.deletion_id == version.deletion_id: job.status for job in jobs(database)}

    assert version.deleted_at is not None
    assert version.purge_after > clock.now()
    # The current deletion still waits for its deadline; the first one was cancelled by the restore.
    assert statuses == {True: JobStatus.PENDING, False: JobStatus.CANCELLED}


def test_an_old_restore_cannot_undo_a_later_deletion_of_the_version(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)
    stale = delete_restore_delete(database, owner, item_id, initial.id, clock)

    with pytest.raises(VersionConflict):
        with database.transaction() as db:
            restore_version(db, owner, item_id, initial.id, stale, clock.now())

    assert row(database, ExperienceVersion, initial.id).deleted_at is not None


def test_an_old_delete_is_refused_after_the_version_was_deleted_and_restored(
    database, owners, clock
):
    owner, _ = owners
    item_id = create(database, owner, clock)
    [initial] = versions(database, item_id)

    with database.transaction() as db:
        stale = current_state(db, initial.id)
        delete_version(db, owner, item_id, initial.id, stale, clock.now())
        restore_version(db, owner, item_id, initial.id, current_state(db, initial.id), clock.now())

    with pytest.raises(VersionConflict):
        with database.transaction() as db:
            delete_version(db, owner, item_id, initial.id, stale, clock.now())

    assert row(database, ExperienceVersion, initial.id).deleted_at is None
