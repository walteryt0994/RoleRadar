"""每日查新 (U3-D4c) as the library holds it: a watch on a kept search summary, the postings it has seen and the new
ones it found, read and removed here. What runs a watch and writes what it found is app.discovery.watches.

Nothing here reads an experience, a preference or a posting's text. A new posting is shown, kept or exported only
before its until, judged by the clock of whoever asks (lapsed): the sweep and start-up only remove those whose time has
come. A watch goes when the user stops it, or with its summary's deletion (lifecycle.delete_job_record): with what it
has seen and the new postings it found, at once, never through the recycle bin, and nothing of it comes back.
"""
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.library.models import JobWatch, JobWatchPosting, JobWatchSeen

# How long a new posting is kept from when it was first read; viewing it, or seeing it again, never extends it.
KEEP_NEW_DAYS = 14
KEEP_NEW_FOR = timedelta(days=KEEP_NEW_DAYS)
# Postings seen at which a watch starts no new run: a threshold asked before a run, never a limit on rows. The run
# that crosses it is written whole, so no new posting is lost, and no posting seen is ever forgotten to make room.
SEEN_THRESHOLD = 5000


def watch_of(db: Session, owner_id: str, search_record_id: str) -> JobWatch | None:
    return db.scalar(select(JobWatch).where(JobWatch.owner_id == owner_id, JobWatch.search_record_id == search_record_id))


def watches_of(db: Session, owner_id: str) -> list[JobWatch]:
    return list(db.scalars(select(JobWatch).where(JobWatch.owner_id == owner_id).order_by(JobWatch.created_at, JobWatch.id)))


def seen_count(db: Session, watch_id: str) -> int:
    return db.scalar(select(func.count()).select_from(JobWatchSeen).where(JobWatchSeen.watch_id == watch_id)) or 0


def full(db: Session, watch_id: str) -> bool:
    """Whether the watch has seen SEEN_THRESHOLD postings or more: no new run of it begins."""
    return seen_count(db, watch_id) >= SEEN_THRESHOLD


def lapsed(row: JobWatchPosting, now: datetime) -> bool:
    return now >= row.until


def new_postings(db: Session, watch_id: str, now: datetime, *, offset: int = 0, limit: int | None = None) -> list[JobWatchPosting]:
    """The new postings of a watch whose time has not come, latest found first; within one run, as it ranked them."""
    query = (
        select(JobWatchPosting)
        .where(JobWatchPosting.watch_id == watch_id, JobWatchPosting.until > now)
        .order_by(JobWatchPosting.found_at.desc(), JobWatchPosting.position, JobWatchPosting.id)
        .offset(offset)
    )

    if limit is not None:
        query = query.limit(limit)

    return list(db.scalars(query))


def new_count(db: Session, watch_id: str, now: datetime) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(JobWatchPosting).where(JobWatchPosting.watch_id == watch_id, JobWatchPosting.until > now)
        )
        or 0
    )


def new_posting(db: Session, owner_id: str, posting_id: str, now: datetime) -> JobWatchPosting | None:
    """One new posting of this owner whose time has not come; None otherwise, whoever's and whatever it was."""
    row = db.scalar(select(JobWatchPosting).where(JobWatchPosting.id == posting_id, JobWatchPosting.owner_id == owner_id))

    return None if row is None or lapsed(row, now) else row


def remove(db: Session, watch_id: str) -> None:
    """A watch, what it has seen and the new postings it found, removed at once."""
    db.execute(delete(JobWatchPosting).where(JobWatchPosting.watch_id == watch_id))
    db.execute(delete(JobWatchSeen).where(JobWatchSeen.watch_id == watch_id))
    db.execute(delete(JobWatch).where(JobWatch.id == watch_id))


def remove_of_search(db: Session, owner_id: str, search_record_id: str) -> bool:
    """The watch on this summary, if there is one, removed with all it holds (its summary deleted)."""
    watch = watch_of(db, owner_id, search_record_id)

    if watch is None:
        return False

    remove(db, watch.id)

    return True


def remove_expired(db: Session, now: datetime, *, owner_id: str | None = None) -> int:
    """New postings whose time has come, removed; their ids stay seen. How many."""
    query = delete(JobWatchPosting).where(JobWatchPosting.until <= now)

    if owner_id is not None:
        query = query.where(JobWatchPosting.owner_id == owner_id)

    return db.execute(query).rowcount or 0
