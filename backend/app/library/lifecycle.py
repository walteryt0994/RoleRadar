"""Deleting, restoring and permanently removing records and their versions.

Deleting moves an object to the recycle bin. It leaves normal use at once and
can be restored for exactly RESTORE_WINDOW (7 days). From that deadline on it
counts as gone everywhere, even before its rows are physically removed.

Physical removal is done by purge jobs: one small record per deletion with
ids and status only. A job re-checks its target right before removing
anything, so a job left over from a deletion that was later restored can
never remove the restored data. A failed attempt is recorded and retried on
the next run.

Attachments follow the same rules with two additions. Deleting an experience
takes its attachments into the recycle bin under the experience's own
deletion, and restoring it brings back exactly those; attachments deleted
on their own before keep their own deadline. And a purge removes the files
of the attachments it covers before their rows: a job is only done once the
files are really gone, and a job that failed after removing some files finds
them gone on its next try and finishes.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.library.content import DOCUMENT_KINDS, EXPERIENCE_CATEGORIES, PREFERENCES, PROFILE
from app.library.db import Database
from app.library.errors import AttachmentConflict, FileStoreError, NotAllowed, NotFound, VersionConflict
from app.library.files import FileStore
from app.library.models import (
    Attachment,
    AttachmentState,
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

    # Kept attachments go with it, under its deletion, so restoring it can
    # tell them from attachments deleted on their own before.
    for attachment in _attachments(db, item, Attachment.deleted_at.is_(None)):
        attachment.deleted_at = now
        attachment.purge_after = item.purge_after
        attachment.deletion_id = item.deletion_id
        attachment.revision += 1
        attachment.updated_at = now

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

    for attachment in _attachments(db, item, Attachment.deletion_id == item.deletion_id):
        attachment.deleted_at = None
        attachment.purge_after = None
        attachment.deletion_id = None
        attachment.revision += 1
        attachment.updated_at = now

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

    for attachment in _attachments(db, item, Attachment.deletion_id == item.deletion_id):
        attachment.purge_after = now
        attachment.revision += 1
        attachment.updated_at = now

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


def _attachments(db: Session, item: ExperienceItem, *conditions) -> list[Attachment]:
    """Ready attachments of an experience that meet the conditions."""
    return list(
        db.scalars(
            select(Attachment).where(
                Attachment.item_id == item.id,
                Attachment.state == AttachmentState.READY,
                *conditions,
            )
        )
    )


def attachment_of(
    db: Session,
    owner_id: str,
    item_id: str,
    attachment_id: str,
    now: datetime,
    *,
    in_bin: bool,
) -> tuple[ExperienceItem, Attachment]:
    """A ready attachment of an experience that is in use: kept, or (in_bin)
    deleted on its own and still restorable.

    While the experience itself is in the recycle bin, none of its
    attachments can be reached on its own: restoring the experience comes
    first.
    """
    item = active_item(db, owner_id, item_id)
    attachment = db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.item_id == item.id,
            Attachment.owner_id == owner_id,
            Attachment.state == AttachmentState.READY,
            Attachment.deleted_at.is_not(None) if in_bin else Attachment.deleted_at.is_(None),
        )
    )

    if attachment is None or is_expired(attachment, now):
        raise NotFound()

    return item, attachment


def check_attachment_revision(attachment: Attachment, expected_revision: int) -> None:
    if attachment.revision != expected_revision:
        raise AttachmentConflict(attachment.item_id)


def delete_attachment(
    db: Session,
    owner_id: str,
    item_id: str,
    attachment_id: str,
    expected_revision: int,
    now: datetime,
) -> Attachment:
    item, attachment = attachment_of(db, owner_id, item_id, attachment_id, now, in_bin=False)
    check_attachment_revision(attachment, expected_revision)
    _start_deletion(db, owner_id, TargetType.ATTACHMENT, attachment, item, now)
    attachment.revision += 1
    attachment.updated_at = now
    record_event(db, owner_id, item.id, TargetType.ATTACHMENT, attachment.id, "attachment_deleted", now)

    return attachment


def restore_attachment(
    db: Session,
    owner_id: str,
    item_id: str,
    attachment_id: str,
    expected_revision: int,
    now: datetime,
) -> Attachment:
    item, attachment = attachment_of(db, owner_id, item_id, attachment_id, now, in_bin=True)
    check_attachment_revision(attachment, expected_revision)
    _cancel_deletion(db, attachment, now)
    attachment.revision += 1
    attachment.updated_at = now
    record_event(db, owner_id, item.id, TargetType.ATTACHMENT, attachment.id, "attachment_restored", now)

    return attachment


def request_attachment_purge(
    db: Session,
    owner_id: str,
    item_id: str,
    attachment_id: str,
    expected_revision: int,
    now: datetime,
) -> str:
    """Make an attachment in the bin unrestorable now; return its purge job id."""
    item, attachment = attachment_of(db, owner_id, item_id, attachment_id, now, in_bin=True)
    check_attachment_revision(attachment, expected_revision)
    job_id = _expire_now(db, attachment, now)
    attachment.revision += 1
    attachment.updated_at = now
    record_event(
        db, owner_id, item.id, TargetType.ATTACHMENT, attachment.id, "attachment_purge_requested", now
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
    # Kept attachments, which go to the bin with the record, and attachments
    # deleted on their own before, which keep their own deadlines.
    kept_attachments: int = 0
    attachments_already_in_bin: int = 0


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
    attachments = _attachments(db, item)

    return DeletionScope(
        item=item,
        kept_versions=len(versions) - in_bin,
        versions_already_in_bin=in_bin,
        restore_deadline=now + RESTORE_WINDOW,
        kept_attachments=sum(1 for row in attachments if row.deleted_at is None),
        attachments_already_in_bin=sum(
            1 for row in attachments if row.deleted_at is not None and not is_expired(row, now)
        ),
    )


def attachment_count(db: Session, item: ExperienceItem) -> int:
    """Every ready attachment of a record, wherever it is: what purging the record removes."""
    return len(_attachments(db, item))


@dataclass
class TrashView:
    items: list[ExperienceItem]
    waiting_cleanups: list[PurgeJob]
    finished_cleanups: list[PurgeJob]
    # How many attachments went to the bin with each record (restored with it).
    attachments_with: dict[str, int] = field(default_factory=dict)


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
    items = [item for item in binned if not is_expired(item, now)]

    return TrashView(
        items=items,
        waiting_cleanups=waiting,
        finished_cleanups=finished,
        attachments_with={
            item.id: len(_attachments(db, item, Attachment.deletion_id == item.deletion_id))
            for item in items
        },
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


def purge_due(database: Database, now: datetime, files: FileStore | None = None) -> PurgeReport:
    """Run every purge job whose due time has passed.

    files is the attachment store; without it, a job that has attachment
    files to remove fails (and is retried) rather than leave them behind.
    """
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
        outcome = run_purge_job(database, job_id, now, files)
        setattr(report, outcome, getattr(report, outcome) + 1)

    return report


def run_purge_job(
    database: Database,
    job_id: str,
    now: datetime,
    files: FileStore | None = None,
) -> str:
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

                # Files first: if removing the rows fails after this, the
                # rows stay unrestorable and the retry finds the files gone.
                _remove_files(db, job, target, files)
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


TARGET_MODELS = {
    TargetType.EXPERIENCE: ExperienceItem,
    TargetType.VERSION: ExperienceVersion,
    TargetType.ATTACHMENT: Attachment,
}


def _target_of(db: Session, job: PurgeJob):
    model = TARGET_MODELS[job.target_type]

    return db.scalar(
        select(model).where(model.id == job.target_id, model.owner_id == job.owner_id)
    )


def _remove_files(db: Session, job: PurgeJob, target, files: FileStore | None) -> None:
    """Remove the attachment files of the rows that _remove is about to delete.

    Each file goes under both its names, so a staging name that an upload
    left behind is gone too before the job can count as done.
    """
    if job.target_type == TargetType.EXPERIENCE:
        rows = db.scalars(select(Attachment).where(Attachment.item_id == target.id))
        keys = [(row.owner_id, row.storage_key) for row in rows]
    elif job.target_type == TargetType.ATTACHMENT:
        keys = [(target.owner_id, target.storage_key)]
    else:
        return

    if keys and files is None:
        raise FileStoreError("no attachment store to remove the files from")

    for owner_id, key in keys:
        files.remove_file(owner_id, key)


def _remove(db: Session, job: PurgeJob, target, now: datetime) -> None:
    """Physically delete the target's rows (tests replace this to simulate failure)."""
    if job.target_type == TargetType.EXPERIENCE:
        # The experience takes all its versions and attachments with it,
        # including those that were waiting in the bin on their own.
        own_jobs = db.scalars(
            select(PurgeJob).where(
                PurgeJob.item_id == target.id,
                PurgeJob.target_type.in_((TargetType.VERSION, TargetType.ATTACHMENT)),
                PurgeJob.status.in_(OPEN_STATUSES),
            )
        )

        for own_job in own_jobs:
            own_job.status = JobStatus.DONE
            own_job.completed_at = now
            own_job.updated_at = now

        db.execute(delete(ExperienceVersion).where(ExperienceVersion.item_id == target.id))
        db.execute(delete(Attachment).where(Attachment.item_id == target.id))

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
