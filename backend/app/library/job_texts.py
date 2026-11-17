"""The texts of postings the user pasted and added to 我的岗位 (U3-D4d): kept with their record, for its owner only.

A text is written once, with its record, when the user adds a posting they pasted to 我的岗位 themselves
(app.discovery.records.keep_supplied), and never changed afterwards. Only such a posting, reached as USER_PROVIDED,
has one: a posting a source gave never does, its text being kept nowhere. The record's page and 去分析 read it only
while the record is in use and the owner's; the export has it while it has the record. It goes to the recycle bin and
comes back with its record (nothing moves: the record's own deleted_at decides), and is deleted with it for good
(app.library.lifecycle).
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.models import JobPosting, JobPostingText

# How a posting the user pasted is reached: by no source, so no source's request, check, batch or watch ever takes it.
USER_PROVIDED = "user_provided"


def keep(db: Session, owner_id: str, record_id: str, text: str, digest: str, now: datetime) -> JobPostingText:
    """The text of the record just made, as the page before saving showed it."""
    row = JobPostingText(posting_record_id=record_id, owner_id=owner_id, text=text, text_sha256=digest, created_at=now)
    db.add(row)
    db.flush()

    return row


def text_of(db: Session, owner_id: str, record_id: str) -> JobPostingText | None:
    """The record's text, whatever state the record is in: the caller decides whether it may be shown."""
    return db.scalar(
        select(JobPostingText).where(JobPostingText.posting_record_id == record_id, JobPostingText.owner_id == owner_id)
    )


def text_in_use(db: Session, owner_id: str, record_id: str) -> str | None:
    """The text of the owner's posting pasted and in use, for its page and 去分析; None for any other record."""
    row = db.scalar(
        select(JobPosting).where(JobPosting.id == record_id, JobPosting.owner_id == owner_id, JobPosting.deleted_at.is_(None))
    )

    if row is None or row.source_access != USER_PROVIDED:
        return None

    kept = text_of(db, owner_id, record_id)

    return None if kept is None else kept.text
