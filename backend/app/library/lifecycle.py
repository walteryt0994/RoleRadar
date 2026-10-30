"""Deleting, restoring and permanently removing records and their versions.

Deleting moves an object to the recycle bin. It leaves normal use at once and
can be restored for exactly RESTORE_WINDOW (7 days). From that deadline on it
counts as gone everywhere, even before its rows are physically removed.

Physical removal is done by purge jobs: one small record per deletion with
ids and status only. A job re-checks its target right before removing
anything, so a job left over from a deletion that was later restored can
never remove the restored data. A failed attempt is recorded and retried on
the next run.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.library.content import DOCUMENT_KINDS, EXPERIENCE_CATEGORIES, PREFERENCES, PROFILE
from app.library.db import Database
from app.library.errors import NotAllowed, NotFound, VersionConflict
from app.library.models import (
    ExperienceItem,
    ExperienceVersion,
    JobStatus,
    PurgeJob,
    PurgeReason,
    TargetType,
)
from app.library.service import (
    active_item,
    check_revision,
    current_document,
    get_version,
    is_current_confirmed,
    is_expired,
    item_versions,
    new_id,
    record_event,
)

RESTORE_WINDOW = timedelta(days=7)
FINISHED_CLEANUPS_SHOWN_FOR = timedelta(days=30)
OPEN_STATUSES = (JobStatus.PENDING, JobStatus.FAILED)
CURRENT_CONFIRMED_REASON = (
    "这是当前的确认版本，代表这条经历现在已确认的事实，不能单独删除。"
    "如果要删除它，可以先修改内容（之后它会成为历史版本），或者删除整条经历。"
)
# Restoring an old profile or preferences next to a newer one would make two.
RESTORE_BLOCKED_REASONS = {
    PROFILE: (
        "你现在已经有一份基本资料。为了不覆盖它，这份旧的基本资料不能直接恢复。"
        "如果想换回旧的，请先删除现在这份，再回到回收站恢复。"
    ),
    PREFERENCES: (
        "你现在已经有一份求职偏好。为了不覆盖它，这份旧的求职偏好不能直接恢复。"
        "如果想换回旧的，请先删除现在这份，再回到回收站恢复。"
    ),
}
KINDS = tuple[str, ...]

logger = logging.getLogger("roleradar.library")


def trashed_item(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> ExperienceItem:
    """A record in the recycle bin that can still be restored."""
    item = db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.id == item_id,
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(kinds),
            ExperienceItem.deleted_at.is_not(None),
        )
    )

    if item is None or is_expired(item, now):
        raise NotFound()

    return item


def delete_experience(
    db: Session,
    owner_id: str,
    item_id: str,
    expected_revision: int,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> ExperienceItem:
    item = active_item(db, owner_id, item_id, kinds)
    check_revision(item, expected_revision)
    _start_deletion(db, owner_id, TargetType.EXPERIENCE, item, item, now)
    item.revision += 1
    record_event(db, owner_id, item.id, TargetType.EXPERIENCE, item.id, "deleted", now)

    return item


def restore_experience(
    db: Session,
    owner_id: str,
    item_id: str,
    expected_revision: int,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> ExperienceItem:
    """Bring a record back; versions deleted on their own stay deleted.

    An old profile or preferences is not brought back next to a current one.
    """
    item = trashed_item(db, owner_id, item_id, now, kinds)
    check_revision(item, expected_revision)

    if item.category in DOCUMENT_KINDS and current_document(db, owner_id, item.category):
        raise NotAllowed(RESTORE_BLOCKED_REASONS[item.category])

    _cancel_deletion(db, item, now)
    item.revision += 1
    record_event(db, owner_id, item.id, TargetType.EXPERIENCE, item.id, "restored", now)

    return item


def request_experience_purge(
    db: Session,
    owner_id: str,
    item_id: str,
    expected_revision: int,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> str:
    """Make a binned record unrestorable now; return its purge job id."""
    item = trashed_item(db, owner_id, item_id, now, kinds)
    check_revision(item, expected_revision)
    job_id = _expire_now(db, item, now)
    item.revision += 1
    record_event(db, owner_id, item.id, TargetType.EXPERIENCE, item.id, "purge_requested", now)

    return job_id


def deletable_version(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> tuple[ExperienceItem, ExperienceVersion]:
    item, version = get_version(db, owner_id, item_id, version_id, kinds)

    if is_current_confirmed(item, version):
        raise NotAllowed(CURRENT_CONFIRMED_REASON)

    return item, version


def trashed_version(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> tuple[ExperienceItem, ExperienceVersion]:
    """A separately deleted version of an active record, still restorable."""
    item = active_item(db, owner_id, item_id, kinds)
    version = db.scalar(
        select(ExperienceVersion).where(
            ExperienceVersion.id == version_id,
            ExperienceVersion.item_id == item.id,
            ExperienceVersion.owner_id == owner_id,
            ExperienceVersion.deleted_at.is_not(None),
        )
    )

    if version is None or is_expired(version, now):
        raise NotFound()

    return item, version


def version_state(db: Session, version: ExperienceVersion) -> str:
    """The delete/restore state of a history version, as a page shows it.

    A version's text never changes, but its deletion state does. Every
    deletion adds a purge job (jobs are never removed), and a version in
    the bin carries the id of its current deletion. Forms carry this value
    and actions are refused when it no longer matches, so a page prepared
    for an earlier deletion can never act on a later one.
    """
    deletions = db.scalar(
        select(func.count())
        .select_from(PurgeJob)
        .where(
            PurgeJob.target_type == TargetType.VERSION,
            PurgeJob.target_id == version.id,
        )
    )

    return f"{deletions}:{version.deletion_id or 'kept'}"


def check_version_state(
    db: Session,
    version: ExperienceVersion,
    expected_state: str,
) -> None:
    if version_state(db, version) != expected_state:
        raise VersionConflict(version.item_id)


def delete_version(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    expected_state: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> ExperienceVersion:
    item, version = deletable_version(db, owner_id, item_id, version_id, kinds)
    check_version_state(db, version, expected_state)
    _start_deletion(db, owner_id, TargetType.VERSION, version, item, now)
    record_event(db, owner_id, item.id, TargetType.VERSION, version.id, "version_deleted", now)

    return version


def restore_version(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    expected_state: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> ExperienceVersion:
    item, version = trashed_version(db, owner_id, item_id, version_id, now, kinds)
    check_version_state(db, version, expected_state)
    _cancel_deletion(db, version, now)
    record_event(db, owner_id, item.id, TargetType.VERSION, version.id, "version_restored", now)

    return version


def request_version_purge(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    expected_state: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> str:
    item, version = trashed_version(db, owner_id, item_id, version_id, now, kinds)
    check_version_state(db, version, expected_state)
    job_id = _expire_now(db, version, now)
    record_event(
        db, owner_id, item.id, TargetType.VERSION, version.id, "version_purge_requested", now
    )

    return job_id


def _start_deletion(
    db: Session,
    owner_id: str,
    target_type: str,
    target,
    item: ExperienceItem,
    now: datetime,
) -> None:
    target.deleted_at = now
    target.purge_after = now + RESTORE_WINDOW
    target.deletion_id = new_id()
    db.add(
        PurgeJob(
            id=new_id(),
            owner_id=owner_id,
            deletion_id=target.deletion_id,
            target_type=target_type,
            target_id=target.id,
            item_id=item.id,
            category=item.category,
            reason=PurgeReason.EXPIRED,
            status=JobStatus.PENDING,
            due_at=target.purge_after,
            attempts=0,
            last_error=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
    )


def _job_of(db: Session, target) -> PurgeJob:
    return db.scalar(select(PurgeJob).where(PurgeJob.deletion_id == target.deletion_id))


def _cancel_deletion(db: Session, target, now: datetime) -> None:
    """Restoring ends this deletion for good, so its purge job is cancelled."""
    job = _job_of(db, target)

    if job is not None and job.status in OPEN_STATUSES:
        job.status = JobStatus.CANCELLED
        job.updated_at = now

    target.deleted_at = None
    target.purge_after = None
    target.deletion_id = None


def _expire_now(db: Session, target, now: datetime) -> str:
    """The user asked for permanent deletion: no restoring from this instant on."""
    target.purge_after = now
    job = _job_of(db, target)
    job.reason = PurgeReason.USER_REQUEST
    job.status = JobStatus.PENDING
    job.due_at = now
    job.updated_at = now

    return job.id


def binned_documents(
    db: Session,
    owner_id: str,
    kind: str,
    now: datetime,
) -> list[ExperienceItem]:
    """The owner's profiles (or preferences) in the recycle bin that can still be restored."""
    items = db.scalars(
        select(ExperienceItem)
        .where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category == kind,
            ExperienceItem.deleted_at.is_not(None),
        )
        .order_by(ExperienceItem.deleted_at.desc())
    )

    return [item for item in items if not is_expired(item, now)]


@dataclass
class DeletionScope:
    item: ExperienceItem
    kept_versions: int
    versions_already_in_bin: int
    restore_deadline: datetime


def deletion_scope(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
    kinds: KINDS = EXPERIENCE_CATEGORIES,
) -> DeletionScope:
    """What deleting this record now would take into the recycle bin."""
    item = active_item(db, owner_id, item_id, kinds)
    versions = item_versions(db, owner_id, item, now)
    in_bin = sum(1 for version in versions if version.deleted_at is not None)

    return DeletionScope(
        item=item,
        kept_versions=len(versions) - in_bin,
        versions_already_in_bin=in_bin,
        restore_deadline=now + RESTORE_WINDOW,
    )


@dataclass
class TrashView:
    items: list[ExperienceItem]
    waiting_cleanups: list[PurgeJob]
    finished_cleanups: list[PurgeJob]


def trash_view(db: Session, owner_id: str, now: datetime) -> TrashView:
    binned = db.scalars(
        select(ExperienceItem)
        .where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.deleted_at.is_not(None),
        )
        .order_by(ExperienceItem.deleted_at.desc())
    )
    waiting, finished = _cleanups(
        db, owner_id, now, PurgeJob.target_type == TargetType.EXPERIENCE
    )

    return TrashView(
        items=[item for item in binned if not is_expired(item, now)],
        waiting_cleanups=waiting,
        finished_cleanups=finished,
    )


def version_cleanups(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
) -> tuple[list[PurgeJob], list[PurgeJob]]:
    return _cleanups(
        db,
        owner_id,
        now,
        PurgeJob.target_type == TargetType.VERSION,
        PurgeJob.item_id == item_id,
    )


def _cleanups(db: Session, owner_id: str, now: datetime, *conditions):
    """Split jobs into "past the deadline, not removed yet" and "recently finished"."""
    jobs = db.scalars(
        select(PurgeJob)
        .where(PurgeJob.owner_id == owner_id, *conditions)
        .order_by(PurgeJob.updated_at.desc())
    )
    waiting, finished = [], []

    for job in jobs:
        if job.status in OPEN_STATUSES and job.due_at <= now:
            waiting.append(job)
        elif (
            job.status == JobStatus.DONE
            and job.completed_at >= now - FINISHED_CLEANUPS_SHOWN_FOR
        ):
            finished.append(job)

    return waiting, finished


@dataclass
class PurgeReport:
    done: int = 0
    cancelled: int = 0
    skipped: int = 0
    failed: int = 0


def purge_due(database: Database, now: datetime) -> PurgeReport:
    """Run every purge job whose due time has passed."""
    with database.transaction() as db:
        job_ids = list(
            db.scalars(
                select(PurgeJob.id)
                .where(PurgeJob.status.in_(OPEN_STATUSES), PurgeJob.due_at <= now)
                .order_by(PurgeJob.due_at, PurgeJob.id)
            )
        )

    report = PurgeReport()

    for job_id in job_ids:
        outcome = run_purge_job(database, job_id, now)
        setattr(report, outcome, getattr(report, outcome) + 1)

    return report


def run_purge_job(database: Database, job_id: str, now: datetime) -> str:
    """Try one job; return "done", "cancelled", "skipped" or "failed"."""
    try:
        with database.transaction() as db:
            job = db.get(PurgeJob, job_id)

            if job is None or job.status not in OPEN_STATUSES or job.due_at > now:
                return "skipped"

            target = _target_of(db, job)

            if target is not None:
                if target.deletion_id != job.deletion_id:
                    # Restored since (and maybe deleted again as a new
                    # deletion with its own job): this job no longer applies.
                    job.status = JobStatus.CANCELLED
                    job.updated_at = now
                    return "cancelled"

                if now < target.purge_after:
                    return "skipped"

                _remove(db, job, target, now)

            job.attempts += 1
            job.status = JobStatus.DONE
            job.last_error = None
            job.completed_at = now
            job.updated_at = now
            record_event(
                db,
                job.owner_id,
                job.item_id,
                job.target_type,
                job.target_id,
                "purge_completed",
                now,
            )

            return "done"
    except Exception as error:  # record it and retry later; never stop other jobs
        _record_failure(database, job_id, now, error)

        return "failed"


def _target_of(db: Session, job: PurgeJob):
    model = ExperienceItem if job.target_type == TargetType.EXPERIENCE else ExperienceVersion

    return db.scalar(
        select(model).where(model.id == job.target_id, model.owner_id == job.owner_id)
    )


def _remove(db: Session, job: PurgeJob, target, now: datetime) -> None:
    """Physically delete the target's rows (tests replace this to simulate failure)."""
    if job.target_type == TargetType.EXPERIENCE:
        # The experience takes all its versions with it, including versions
        # that were waiting in the bin on their own.
        version_jobs = db.scalars(
            select(PurgeJob).where(
                PurgeJob.item_id == target.id,
                PurgeJob.target_type == TargetType.VERSION,
                PurgeJob.status.in_(OPEN_STATUSES),
            )
        )

        for version_job in version_jobs:
            version_job.status = JobStatus.DONE
            version_job.completed_at = now
            version_job.updated_at = now

        db.execute(delete(ExperienceVersion).where(ExperienceVersion.item_id == target.id))

    db.delete(target)
    db.flush()


def _record_failure(database: Database, job_id: str, now: datetime, error: Exception) -> None:
    logger.warning(
        "purge job %s failed (%s); it will be retried",
        job_id,
        type(error).__name__,
    )

    with database.transaction() as db:
        job = db.get(PurgeJob, job_id)

        if job is not None and job.status in OPEN_STATUSES:
            job.status = JobStatus.FAILED
            job.attempts += 1
            job.last_error = type(error).__name__[:100]
            job.updated_at = now
