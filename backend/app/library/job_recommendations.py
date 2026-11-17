"""Current recommendations held for tracking (U3-D4b-2): 临时留存跟踪, told apart from 我的岗位 by their row in
job_recommendations alone.

state_of says where a kept posting stands, by the app's clock. It is the one rule every page, form, check, batch and
keeping of recommendations asks, when the request comes, when the work begins and right before it writes:

- MINE: no such row: 我的岗位, the user's own, which never lapses;
- CURRENT: held, until not come, its latest successful check not closed: tracked, shown as 当前推荐;
- CLOSED: held, until not come, its latest successful check closed with its evidence: no longer tracked, shown apart
  with that evidence and when it goes;
- LAPSED: until has come. From that moment it is shown nowhere, never checked and never written to, whether or not it
  is removed yet.

remove_rows deletes one held posting from the database: its checks, its rechecks, its audit events, its row here and
the posting itself, never through the recycle bin. What memory holds of it (the text a 申请前核对 read) is dropped by
the caller, before the transaction commits (app.discovery.records.remove_recommendations).
"""
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.library.job_choices import CHECK_CLOSED, summarize
from app.library.models import (
    AuditEvent,
    JobFeedback,
    JobPosting,
    JobPostingCheck,
    JobPostingRecheck,
    JobRecommendation,
    TargetType,
)

# How long a current recommendation is held, from when it was kept or kept again.
KEEP_DAYS = 7
KEEP_FOR = timedelta(days=KEEP_DAYS)


class RecState(StrEnum):
    MINE = "mine"
    CURRENT = "current"
    CLOSED = "closed"
    LAPSED = "lapsed"


# A recommendation still held: shown, and open to being added to 我的岗位 or no longer tracked.
HELD = (RecState.CURRENT, RecState.CLOSED)


def marker_of(db: Session, record_id: str) -> JobRecommendation | None:
    return db.get(JobRecommendation, record_id)


def lapsed(db: Session, row: JobPosting, now: datetime) -> bool:
    """A current recommendation whose time has come: never so for 我的岗位."""
    marker = db.get(JobRecommendation, row.id)

    return marker is not None and now >= marker.until


def state_of(db: Session, row: JobPosting, now: datetime) -> RecState:
    marker = db.get(JobRecommendation, row.id)

    if marker is None or row.deleted_at is not None:
        return RecState.MINE

    if now >= marker.until:
        return RecState.LAPSED

    return RecState.CLOSED if closed(db, row) else RecState.CURRENT


def closed(db: Session, row: JobPosting) -> bool:
    """Its latest successful check, of a search or from its record, found it closed with evidence."""
    checks = list(db.scalars(select(JobPostingCheck).where(JobPostingCheck.posting_record_id == row.id)))
    rechecks = list(db.scalars(select(JobPostingRecheck).where(JobPostingRecheck.posting_record_id == row.id)))
    succeeded = summarize([*checks, *rechecks]).succeeded

    return succeeded is not None and succeeded.status == CHECK_CLOSED


def markers(db: Session, owner_id: str) -> list[JobRecommendation]:
    return list(
        db.scalars(select(JobRecommendation).where(JobRecommendation.owner_id == owner_id).order_by(JobRecommendation.posting_record_id))
    )


def lapsed_markers(db: Session, now: datetime, owner_id: str | None = None) -> list[JobRecommendation]:
    """The recommendations whose time has come, of one owner or of all."""
    query = select(JobRecommendation).where(JobRecommendation.until <= now)

    if owner_id is not None:
        query = query.where(JobRecommendation.owner_id == owner_id)

    return list(db.scalars(query.order_by(JobRecommendation.owner_id, JobRecommendation.posting_record_id)))


def remove_rows(db: Session, record_id: str) -> bool:
    """Delete a held recommendation from the database, in the caller's transaction; False if it is not one.

    A held one never has the user's decision or feedback: either adds it to 我的岗位 first, in the same write. Were one
    found with either, what the user wrote stays theirs: it is added to 我的岗位 instead, and nothing is deleted.
    """
    marker = db.get(JobRecommendation, record_id)

    if marker is None:
        return False

    row = db.get(JobPosting, record_id)
    has_feedback = db.scalar(select(JobFeedback.id).where(JobFeedback.posting_record_id == record_id).limit(1)) is not None

    if row is not None and (row.decision is not None or has_feedback):
        db.execute(delete(JobRecommendation).where(JobRecommendation.posting_record_id == record_id))

        return False

    db.execute(delete(JobPostingCheck).where(JobPostingCheck.posting_record_id == record_id))
    db.execute(delete(JobPostingRecheck).where(JobPostingRecheck.posting_record_id == record_id))
    db.execute(
        delete(AuditEvent).where(AuditEvent.object_type == TargetType.JOB_POSTING.value, AuditEvent.object_id == record_id)
    )
    db.execute(delete(JobRecommendation).where(JobRecommendation.posting_record_id == record_id))
    db.execute(delete(JobPosting).where(JobPosting.id == record_id))

    return True
