"""Tables of the experience library (separate from app.models).

Current content lives on ExperienceItem: one row per experience, and one
row each for a user's basic profile and job preferences (its category tells
which). ExperienceVersion rows are fixed snapshots. An experience keeps the
first saved text (initial) and every confirmation (confirmed); saving a
draft only updates the item, so drafts are not recorded keystroke by
keystroke. The profile and the preferences are never confirmed, so each of
their saves that changes something is kept as a version (saved).

An Attachment row describes one file kept for an experience in the private
attachment store (see files.py). The row holds the file's facts (display
name, type, size, checksum) and its state; the bytes are only on disk.

A ResumeImport row is one resume a user brought in (U2-B1): an uploaded
PDF or DOCX, whose bytes are in the store's imports area, or pasted text.
It keeps what was read for at most seven days and is not part of the
library: nothing in it is an experience, a profile or a preference.

The candidates read from an import (U2-B2a) belong to it the same way: an
ImportStructuring row says how its text was turned into candidates, and
each ImportCandidate row is one candidate the user checks, edits or leaves
out. They live and end with their import. An ImportAdoption row (U2-B2b)
says where an adopted candidate was written: which record, how and which
fields, never what. It ends with its import too; the record it names lives
on by its own rules.

A LiveApproval row (U2-C1) is one approval of real AI requests: its terms
as first used and whether it is paused. A LiveRequest row is one request
the gate let through: what was reserved for it and what it was charged,
never what it carried (see app.live_gate).

A DirectionExploration row (U3-C1) is one direction exploration the user
chose to keep: the checked result as it was, never rewritten, and how it was
made. A DirectionDecision row is one decision about a direction (keep, defer,
exclude); a user has at most one in use per direction name. A DirectionSource
row says which record, version and fields an exploration or a decision rests
on, by ids and field names only: the record lives by its own rules, and how it
stands now is worked out when asked (app.library.references).

A JobPosting row (U3-D3a) is one posting the user chose to keep from a
search: its public facts as that search had them (a whitelist, never its
text, an analysis or an experience) and the user's current decision about
it. A JobPostingCheck row is one check of it as a search showed it, when its
source answered; a JobFeedback row one "this is not right" the user said of
its facts. A JobSearch row is a search's summary the user chose to keep, its
whitelisted facts only. Nothing is kept unless the user asks for it, and only
from sources the server lets keep (app.discovery.records).
"""
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.library.db import LibraryBase, UTCDateTime
from app.library.errors import ConfigError

# 2: categories beyond project/portfolio, the profile and preferences,
# saved versions, and the category on purge jobs.
# 3: attachments, and the library id that ties a database to its attachment folder.
# 4: resume imports (U2-B1).
# 5: the candidates read from an import, and how they were read (U2-B2a).
# 6: where each adopted candidate was written (U2-B2b).
# 7: resume imports the user saved as drafts to keep checking later (U2-B3a).
# 8: the approvals of real AI requests and the ledger of what each cost (U2-C1).
# 9: saved direction explorations, direction decisions and what each rests on (U3-C1).
# 10: postings kept from a search, their checks and feedback, and kept search summaries (U3-D3a).
# 11: analyses kept from a search, what each rests on, and feedback on them (U3-D3b).
# 12: checks of a kept posting made from its record, and the runs that made them (U3-D4a).
# 13: what each batch of checks of kept postings came to, in counts (U3-D4b-1).
# 14: kept postings held only as current recommendations, and the latest search ticked for them (U3-D4b-2).
# 15: 每日查新 on a kept search summary: the watch, the postings it has seen, and the new ones it found (U3-D4c).
# 16: the text of a posting the user pasted and added to 我的岗位, kept with its record (U3-D4d).
# A file of version 3 to 15 is upgraded by app.library.migrate.
SCHEMA_VERSION = "16"
# The versions app.library.migrate upgrades from.
UPGRADABLE_SCHEMA_VERSIONS = ("3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15")


class ItemState(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


class VersionKind(StrEnum):
    INITIAL = "initial"
    CONFIRMED = "confirmed"
    SAVED = "saved"


class TargetType(StrEnum):
    EXPERIENCE = "experience"
    VERSION = "experience_version"
    ATTACHMENT = "attachment"
    # Records of their own (U3-C1): their purge job names the record itself as its item.
    DIRECTION_EXPLORATION = "direction_exploration"
    DIRECTION_DECISION = "direction_decision"
    # Kept from a search (U3-D3a, D3b), records of their own too.
    JOB_POSTING = "job_posting"
    JOB_SEARCH = "job_search"
    JOB_ANALYSIS = "job_analysis"


class JobDecision(StrEnum):
    """What the user decided about a kept posting (U3-D3a); one at a time, changed or taken back."""

    FAVORITE = "favorite"
    DEFER = "defer"
    SKIP = "skip"
    # Getting ready to apply (U3-D4a): the user's own decision, one of these four like the others. Never that anything
    # was sent or applied for, and never set, kept or changed by a check of the posting's source.
    APPLY = "apply"


class SourceKind(StrEnum):
    """What a direction record rests on: a confirmed experience, or the job preferences (U3-C1)."""

    EXPERIENCE = "experience"
    PREFERENCES = "preferences"


class SourceHolder(StrEnum):
    EXPLORATION = "exploration"
    DECISION = "decision"


class DecisionState(StrEnum):
    KEEP = "keep"
    DEFER = "defer"
    EXCLUDE = "exclude"


class DecisionBasis(StrEnum):
    # Decided on a saved exploration, which it stays linked to.
    SAVED_EXPLORATION = "saved_exploration"
    # Decided on an exploration that was not saved: the full analysis was not kept, only what it rested on.
    UNSAVED_EXPLORATION = "unsaved_exploration"
    # The user's own choice, stated or affirmed again by them; what it rested on before stays as history.
    PERSONAL = "personal"


class AttachmentState(StrEnum):
    # Reserved while its file is put in place; never shown or downloadable.
    PENDING = "pending"
    # In place and checked: the only state that is listed, downloaded or exported.
    READY = "ready"
    # The upload did not finish; clean-up removes the file (if any) and the row.
    FAILED = "failed"


class JobStatus(StrEnum):
    PENDING = "pending"
    FAILED = "failed"
    DONE = "done"
    CANCELLED = "cancelled"


class PurgeReason(StrEnum):
    EXPIRED = "expired"
    USER_REQUEST = "user_request"


class ImportState(StrEnum):
    # Reserved while an uploaded file is put in place; never shown.
    RECEIVING = "receiving"
    READING = "reading"
    READ = "read"
    PARTIAL = "partial"
    FAILED = "failed"
    # Ended: what was kept is no longer shown, and its content is gone from the row.
    DISCARDED = "discarded"
    EXPIRED = "expired"
    # Ended: a saved draft permanently deleted, by the user or past its restore deadline (U2-B3a).
    DELETED = "deleted"


class ImportSource(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TEXT = "text"


class StructuringState(StrEnum):
    RUNNING = "running"
    # At least one candidate passed the checks; they are kept.
    DONE = "done"
    # Nothing usable came back; asking again is allowed.
    FAILED = "failed"


class CandidateState(StrEnum):
    # Waiting for the user to check it.
    PENDING = "pending"
    # Left out by the user; it can be taken back.
    EXCLUDED = "excluded"
    # Written into the user's records at their choice (U2-B2b); its ImportAdoption says where.
    ADOPTED = "adopted"


class AdoptionAction(StrEnum):
    # A new experience, a draft.
    NEW = "new"
    # Chosen fields written into an experience that was there.
    SUPPLEMENT = "supplement"
    # Chosen fields written into the basic profile, made for it if there was none.
    PROFILE = "profile"


class LibraryMeta(LibraryBase):
    __tablename__ = "library_meta"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class User(LibraryBase):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    display_label: Mapped[str] = mapped_column(String(100))
    auth_provider: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class LoginSession(LibraryBase):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ExperienceItem(LibraryBase):
    __tablename__ = "experience_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    private_note: Mapped[str] = mapped_column(Text)
    confirmed_version_id: Mapped[str | None] = mapped_column(String(36))
    # How many numbered versions were made: confirmations of an experience,
    # or saves of the profile or preferences.
    confirmation_count: Mapped[int] = mapped_column(Integer)
    last_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class ExperienceVersion(LibraryBase):
    __tablename__ = "experience_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("experience_items.id"),
        index=True,
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    private_note: Mapped[str] = mapped_column(Text)
    source_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class PurgeJob(LibraryBase):
    """The clean-up task of one deletion.

    It holds ids and status only, never content, so it can outlive the rows
    it removes and serve as evidence that clean-up finished.
    """

    __tablename__ = "purge_jobs"
    __table_args__ = (Index("ix_purge_jobs_status_due", "status", "due_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    deletion_id: Mapped[str] = mapped_column(String(36), unique=True)
    target_type: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    # The kind of record, so a finished clean-up can still say what it removed.
    category: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Attachment(LibraryBase):
    """One file kept for an experience.

    The file itself is at files/<owner_id>/<storage_key> in the attachment
    store; storage_key is random and filename is only for display and as the
    download name. deleted_at, purge_after and deletion_id work as for
    versions. An attachment deleted on its own has a purge job of its own;
    one that went to the recycle bin with its experience carries the
    experience's deletion_id, which is how restoring the experience finds
    exactly the attachments that went with it.
    """

    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("owner_id", "upload_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("experience_items.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(32), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(50))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    # The one-time key of the upload form, so a repeated post of the same
    # form finds this row instead of adding the file a second time.
    upload_key: Mapped[str] = mapped_column(String(64))
    # Goes up with every delete, restore or purge request; forms carry it.
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class ResumeImport(LibraryBase):
    """One resume brought in for an import, kept for at most seven days (U2-B1).

    received_at is when the server had the whole file or text; expires_at,
    seven days later, never moves. An uploaded file is at
    imports/<owner_id>/<storage_key> in the file store; pasted text has no
    file. segments hold what was read, where it was found (see
    resume_reader.Reading); details hold facts about the reading without
    content (pages, what made it partial).

    Discarding or expiring ends an import: its content (segments, file name,
    checksum) is removed from the row at once, its file as soon as the store
    allows. cleared_at says when the file is really gone; until then the row
    keeps the storage key and the clean-up retries. The ended row holds no
    content, only that an import existed and how its clean-up went.

    An import saved as a draft (ImportDraft, U2-B3a) no longer expires; it
    ends (DELETED) only when the user removes it from the recycle bin for
    good, or when its restore deadline passes.
    """

    __tablename__ = "resume_imports"
    __table_args__ = (
        UniqueConstraint("owner_id", "upload_key"),
        Index("ix_resume_imports_state_expires", "state", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str | None] = mapped_column(String(255))
    media_type: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    # The one-time key of the import form: a repeated post finds this row.
    upload_key: Mapped[str] = mapped_column(String(64))
    # Why a reading failed or is partial (resume_reader's codes), or None.
    outcome: Mapped[str | None] = mapped_column(String(30))
    segments: Mapped[list | None] = mapped_column(JSON)
    details: Mapped[dict | None] = mapped_column(JSON)
    text_chars: Mapped[int | None] = mapped_column(Integer)
    # Goes up with every change of state; the page's forms carry it.
    revision: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cleared_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cleanup_attempts: Mapped[int] = mapped_column(Integer)
    cleanup_error: Mapped[str | None] = mapped_column(String(100))


class ImportStructuring(LibraryBase):
    """How the text of one import was turned into candidates (U2-B2a); one row per import.

    attempt names the run whose answer may be stored. An answer is kept only
    while the row still runs that attempt and the import is still open,
    unexpired and as it was when the run began (import_revision), so a
    discard, an expiry or a newer attempt that came first is never
    overwritten. structurer says who answered (only the local simulation in
    this batch). problems lists, without any content, the candidates of the
    answer that were dropped: where they pointed and why.
    """

    __tablename__ = "import_structurings"

    import_id: Mapped[str] = mapped_column(ForeignKey("resume_imports.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(20))
    attempt: Mapped[str] = mapped_column(String(36))
    import_revision: Mapped[int] = mapped_column(Integer)
    structurer: Mapped[str] = mapped_column(String(100))
    contract_version: Mapped[str] = mapped_column(String(10))
    # Why nothing usable came back (candidates' reasons), or None.
    outcome: Mapped[str | None] = mapped_column(String(30))
    problems: Mapped[list | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ImportCandidate(LibraryBase):
    """One candidate read from an import, waiting to be checked (U2-B2a).

    extracted is what the structuring found, field by field: each value is
    made of pieces of the import's text, each with the segment it stands in
    (see candidates.py). edits holds the user's own words for the fields
    they changed: kept apart from what was found, not required to stand in
    the text, and never shown as a quote. cautions are what the text itself
    calls for care about (a negation, team work, a plan, an uncertain date).
    revision goes up with every change; the page's forms carry it.
    """

    __tablename__ = "import_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("resume_imports.id", ondelete="CASCADE"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    extracted: Mapped[dict] = mapped_column(JSON)
    edits: Mapped[dict | None] = mapped_column(JSON)
    cautions: Mapped[list] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImportAdoption(LibraryBase):
    """Where one adopted candidate was written (U2-B2b): ids and field names, no content.

    candidate_id is unique, so a candidate is written at most once, however
    often or from however many pages it is asked. item_id names the record
    written into but is no foreign key: that record is deleted, restored or
    purged by its own rules, and a row here never holds it back or brings
    it back. The row goes with its import (and its candidate).
    """

    __tablename__ = "import_adoptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("resume_imports.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("import_candidates.id", ondelete="CASCADE"), unique=True)
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(20))
    # The names of the fields written, in the record's terms.
    fields: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ImportDraft(LibraryBase):
    """A resume import the user saved to keep checking later (U2-B3a); one row per import at most.

    Saved, the import keeps everything it held (the file, what was read, the
    candidates with the user's edits and choices, where adopted ones went)
    and no longer ends at its expires_at, which stays as it was. Deleted, it
    is in the recycle bin from deleted_at until purge_after: out of use,
    with nothing removed, so restoring brings back exactly what was there.
    From purge_after on it is gone, and the clean-up pass ends the import.
    Saving is no adoption: nothing here is written into the user's records.
    The row goes when its import ends.
    """

    __tablename__ = "import_drafts"

    import_id: Mapped[str] = mapped_column(ForeignKey("resume_imports.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    saved_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LiveRequestState(StrEnum):
    # Let through and handed on; what it cost is not settled yet.
    RESERVED = "reserved"
    # Settled from the usage the service reported, within its reservation; or a counted group whose
    # count alone went out, charged its count.
    SETTLED = "settled"
    # The reported usage cost more than the reservation: charged in full, the approval paused.
    OVER = "over"
    # No usable usage, the app stopped before settling, or a priced count that only may have gone out:
    # kept at a conservative amount (an estimate, no settlement), the approval paused.
    UNKNOWN = "unknown"
    # Its provider said it never left, nor its count: nothing charged, and it does not count.
    RELEASED = "released"


class LiveApproval(LibraryBase):
    """One approval of real AI requests (U2-C1): its terms as first used, and whether it is paused.

    terms is the snapshot taken the first time the approval was used; an app
    that brings the same approval_id with other terms is refused, so neither
    a restart nor a changed setting widens what was approved. paused_at is
    set when a cost could not be known or came out above its reservation;
    nothing in the app clears it, so going on takes a new approval. Nothing
    a request carried is kept here.
    """

    __tablename__ = "live_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    terms: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paused_reason: Mapped[str | None] = mapped_column(String(30))


class LiveRequest(LibraryBase):
    """One real request the gate let through (U2-C1): what was reserved and charged, never what it carried.

    Amounts are in millionths of a US dollar, rounded up; charged is None
    while the request is reserved. Token counts are the ones the service
    reported, where it did. owner_id is no foreign key: the ledger keeps
    what was spent whatever happens to the account later.
    """

    __tablename__ = "live_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("live_approvals.approval_id"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    owner_id: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(20), index=True)
    reserved_micro_usd: Mapped[int] = mapped_column(Integer)
    charged_micro_usd: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    service_tier: Mapped[str | None] = mapped_column(String(20))
    # Why it ended as it did, beyond its state (interrupted, unknown_usage, count_sent, …); None when
    # settled plainly. While a counted group is reserved, how far it got (counting, then counted), so
    # that a restart can tell whether its generation may have gone out (app.live_gate).
    reason: Mapped[str | None] = mapped_column(String(30))
    reserved_at: Mapped[datetime] = mapped_column(UTCDateTime)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DirectionExploration(LibraryBase):
    """One direction exploration the user chose to keep (U3-C1).

    content is the checked result as it was saved and never changes; generation says how it was made (who
    answered, the prompt and notice versions, the input fingerprint, when), as the server recorded it.
    result_id is the id the server gave the result: saving it again finds this row. revision goes up with
    every delete, restore or purge request. deleted_at, purge_after and deletion_id work as for records; one
    deleted together with a record or version carries that deletion's id and comes back only with it.
    """

    __tablename__ = "direction_explorations"
    __table_args__ = (UniqueConstraint("owner_id", "result_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    result_id: Mapped[str] = mapped_column(String(36))
    content: Mapped[dict] = mapped_column(JSON)
    generation: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    saved_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class DirectionDecision(LibraryBase):
    """One decision about a direction (U3-C1): keep, defer or exclude.

    name_key is the name as compared (see app.directions.contracts.name_key); a user has at most one
    decision in use per name_key, which the partial unique index makes sure of. basis says what the decision
    rests on (DecisionBasis); exploration_id names the saved exploration it was made on, and stays when that
    exploration is deleted or purged, so the decision never passes for one made afresh. revision goes up with
    every change; pages and callers name it.
    """

    __tablename__ = "direction_decisions"
    __table_args__ = (
        Index(
            "ux_direction_decisions_in_use",
            "owner_id",
            "name_key",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    name_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(20))
    basis: Mapped[str] = mapped_column(String(30))
    exploration_id: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class DirectionSource(LibraryBase):
    """One record a direction exploration or decision rests on (U3-C1): ids and field names, never content.

    holder_type and holder_id name the exploration or decision; kind, item_id and version_id the experience
    or preferences and the version used; fields the fields that version was used for. item_id is no foreign
    key: the record is deleted, restored or purged by its own rules, and a row here never holds it back or
    brings it back. The rows go when their holder is purged.
    """

    __tablename__ = "direction_sources"
    __table_args__ = (Index("ix_direction_sources_holder", "holder_type", "holder_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    holder_type: Mapped[str] = mapped_column(String(20))
    holder_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    fields: Mapped[list] = mapped_column(JSON)


class JobPosting(LibraryBase):
    """One posting the user chose to keep from a search (U3-D3a).

    source_id and posting_id name the posting in its source; source_access is how the search reached that source,
    and only a source the server lets keep is ever kept (app.discovery.records). facts are the whitelisted public
    facts the search held when it was first kept, never its text, an analysis or anything of an experience;
    search_id names that search. kept_at is when the user kept it and never when its source answered: that is in its
    checks (JobPostingCheck), each with the time its source answered. decision is a JobDecision or None; reasons are
    codes from a fixed list and note is the user's own words, both kept here only. A user has at most one posting in
    use per source and posting id, which the partial unique index makes sure of. revision goes up with every change;
    pages name the id and the revision. deleted_at, purge_after and deletion_id work as for other records, and its
    checks and feedback go and come back with it.
    """

    __tablename__ = "job_postings"
    __table_args__ = (
        Index(
            "ux_job_postings_in_use",
            "owner_id",
            "source_id",
            "posting_id",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(60))
    posting_id: Mapped[str] = mapped_column(String(64))
    source_access: Mapped[str] = mapped_column(String(20))
    facts: Mapped[dict] = mapped_column(JSON)
    search_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(20))
    reasons: Mapped[list] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer)
    kept_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class JobPostingCheck(LibraryBase):
    """One check of a kept posting as a search knew it (U3-D3a).

    status is what its source answered (open, closed; unknown when the search kept no status for an answer, never
    taken for open), or unanswered: a check that got no answer. checked_at is the time of the check: when its source
    answered, or, for an unanswered one, when it was tried. recorded_at is when it was written here; one is never put
    for the other. A search's last answered check and its latest unanswered one are each recorded once (F1).
    """

    __tablename__ = "job_posting_checks"
    __table_args__ = (UniqueConstraint("posting_record_id", "search_id", "checked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    posting_record_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobFeedback(LibraryBase):
    """One "this is not right" the user said of a kept posting's facts (U3-D3a): which fact, why, and their words.

    Kept here only: never sent to a model or anywhere else. search_id names the search it was seen in.
    """

    __tablename__ = "job_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    posting_record_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobSearch(LibraryBase):
    """A search's summary the user chose to keep (U3-D3a).

    summary holds its whitelisted facts only (app.discovery.records.search_summary): never a posting's text, an
    analysis, an experience's words or a decider's own words. search_id is the search's id: keeping it again finds
    the row in use. revision and the recycle bin work as for JobPosting.
    """

    __tablename__ = "job_searches"
    __table_args__ = (
        Index("ux_job_searches_in_use", "owner_id", "search_id", unique=True, sqlite_where=text("deleted_at IS NULL")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    kept_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class JobAnalysis(LibraryBase):
    """One posting's analysis the user chose to keep from a search (U3-D3b).

    search_id, source_id and posting_id name the search and the posting it analysed: a user keeps a search's analysis
    of a posting once (the unique index), deleted or not, so a page of that search never keeps it again. source_access
    is how the search reached that source; only a source the server lets keep analyses is ever kept
    (app.discovery.records). facts are the posting's whitelisted public facts as the search had them, content the
    analysis as its result page showed it, by whitelist: the category, what to confirm first, the requirements as
    judged with the posting's words, each experience quote with what it was read with then and its flags, and the
    preferences compared. Never the posting's text, a whole experience, a prompt or a model's answer as such.
    generation says what made it (the recipient, the versions, the search); content_sha256 tells which text of the
    posting it analysed, never what the text said and never as evidence. What it rests on is in its sources
    (JobAnalysisSource), and how they stand now is worked out on every view (app.library.references): the analysis
    itself never changes. revision goes up with every change; pages name the id and the revision. deleted_at,
    purge_after and deletion_id work as for other records: deleted on its own, or along with an experience or a
    version it rests on, under that deletion; its sources and feedback go and come back with it.
    """

    __tablename__ = "job_analyses"
    __table_args__ = (
        Index("ux_job_analyses_once", "owner_id", "search_id", "source_id", "posting_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(60))
    posting_id: Mapped[str] = mapped_column(String(64))
    source_access: Mapped[str] = mapped_column(String(20))
    facts: Mapped[dict] = mapped_column(JSON)
    content: Mapped[dict] = mapped_column(JSON)
    generation: Mapped[dict] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64))
    analysed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer)
    saved_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    purge_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deletion_id: Mapped[str | None] = mapped_column(String(36))


class JobAnalysisSource(LibraryBase):
    """What a kept analysis rests on (U3-D3b): a confirmed experience or the job preferences, the version used and the
    fields used, as a DirectionSource names them, so app.library.references tells how each stands now. Ids and field
    names only, never words; they go with their analysis."""

    __tablename__ = "job_analysis_sources"
    __table_args__ = (Index("ix_job_analysis_sources_analysis", "analysis_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("job_analyses.id"))
    kind: Mapped[str] = mapped_column(String(20))
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    fields: Mapped[list] = mapped_column(JSON)


class JobAnalysisFeedback(LibraryBase):
    """What the user said is not right in a kept analysis (U3-D3b): which part (target, and target_ref within the
    analysis when it names one), a reason from a fixed list and the user's own words. Added only, kept in the library
    only, never sent anywhere and never read by a search or an analysis; it goes with its analysis."""

    __tablename__ = "job_analysis_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("job_analyses.id"), index=True)
    target: Mapped[str] = mapped_column(String(20))
    target_ref: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobUpdateRun(LibraryBase):
    """One check of kept postings the user asked for from their records (U3-D4a), as app.discovery.updates ran it.

    trigger is recheck (再核对一次) or apply (申请前核对). started_at and finished_at are the run's own times, never a
    posting's check time. calls is how many times its sources were called, each once and never again. outcome is done
    (its checks written), discarded (the record went or changed while it ran: nothing written) or None while it runs;
    reason says why one was discarded. It names no posting and holds nothing of one.
    """

    __tablename__ = "job_update_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))
    calls: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str | None] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class JobUpdateRunCount(LibraryBase):
    """What one batch of checks of kept postings came to (U3-D4b-1), one row for its run: due is how many were due
    when it began, selected how many it took (at most a batch's postings), and each of those ended as one of
    succeeded (a check written: still published, or closed with its evidence), failed (a check written that settles
    nothing: no answer, or an answer that settles nothing), discarded (its record went or changed while it was
    checked: nothing written), skipped (no longer one to check when its turn came, or being checked already: nothing
    sent, nothing written) or unreached (the batch stopped first: nothing sent, nothing written). Counts only: it names
    no posting and holds nothing of one."""

    __tablename__ = "job_update_run_counts"

    run_id: Mapped[str] = mapped_column(ForeignKey("job_update_runs.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    due: Mapped[int] = mapped_column(Integer)
    selected: Mapped[int] = mapped_column(Integer)
    succeeded: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    discarded: Mapped[int] = mapped_column(Integer)
    skipped: Mapped[int] = mapped_column(Integer)
    unreached: Mapped[int] = mapped_column(Integer)


class JobRecommendation(LibraryBase):
    """A kept posting held only as a current recommendation (U3-D4b-2): 临时留存跟踪, never 我的岗位.

    The system keeps it when a search the user ticked 临时留存并跟踪这次的推荐 for ends as it should
    (app.discovery.records.keep_recommendations), with the posting's public facts and checks as any kept posting has
    them (job_postings); this row alone tells it apart. It lapses at until, removed yet or not
    (app.library.job_recommendations.state_of): from then on it is shown nowhere, never checked and never written to,
    and it is removed with its checks, never through the recycle bin. Kept, decided on or said not to be right by the
    user, it is added to 我的岗位: this row goes, and it never lapses again.
    """

    __tablename__ = "job_recommendations"

    posting_record_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    search_id: Mapped[str] = mapped_column(String(64))
    recommended_at: Mapped[datetime] = mapped_column(UTCDateTime)
    until: Mapped[datetime] = mapped_column(UTCDateTime)


class JobRecommendationLatest(LibraryBase):
    """The latest search of a user's ticked for current recommendations that ended as it should (U3-D4b-2), by when it
    started and, for two started at once, its id. A search is taken only if it started later than this one: the same
    search again, or an older one coming back late, changes nothing, even once the recommendations it kept are gone.
    Ids and times only; it is never removed with the recommendations."""

    __tablename__ = "job_recommendation_latest"

    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    search_id: Mapped[str] = mapped_column(String(64))
    search_started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobWatch(LibraryBase):
    """每日查新 on one kept search summary (U3-D4c): the searches the summary kept, made again by a fixed flow, the
    postings they give screened by the summary's scope here, and those never seen before kept as new ones.

    Its conditions are the summary's own, read from it at every run and never copied here, nor taken from a form. It is
    the user's: started on their page (notice_version is the words they saw then) and removed only when they stop it
    or delete the summary, never by itself; nothing of it goes to the recycle bin. baselined names each of the
    summary's searches that has answered once: its first answer is a baseline, never new. run_token is set while a run
    of it runs: every call of the run, and its one write, ask that it still stands (app.discovery.watches). The last
    run's own times, outcome and counts are kept here, none per posting; attempted_at is when the last run that was
    not interrupted began (what 定时检查 counts 24 hours from), completed_at when the last complete one ended.
    """

    __tablename__ = "job_watches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    search_record_id: Mapped[str] = mapped_column(ForeignKey("job_searches.id"), unique=True)
    notice_version: Mapped[str] = mapped_column(String(40))
    baselined: Mapped[list] = mapped_column(JSON)
    baseline_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    run_token: Mapped[str | None] = mapped_column(String(40))
    last_trigger: Mapped[str | None] = mapped_column(String(20))
    last_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_outcome: Mapped[str | None] = mapped_column(String(20))
    last_reason: Mapped[str | None] = mapped_column(String(60))
    last_calls: Mapped[int] = mapped_column(Integer)
    last_counts: Mapped[dict | None] = mapped_column(JSON)
    attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobWatchSeen(LibraryBase):
    """A posting a watch has seen (U3-D4c), by its source and id only: whatever the screening, a skip or a kept record
    made of it, so a posting seen once is never new again; a lead's official posting too. job_key names a job its
    employer numbered (rules.same_posting), so the same job posted again under another id is not new either; it is
    a digest of public facts, never the facts. It goes with its watch, and only with it."""

    __tablename__ = "job_watch_seen"

    watch_id: Mapped[str] = mapped_column(ForeignKey("job_watches.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    posting_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    job_key: Mapped[str | None] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobWatchPosting(LibraryBase):
    """A new posting a watch found (U3-D4c): its public facts by the kept postings' whitelist (records.posting_facts,
    never its text, an analysis or a link), how its screening went, and whether its list was read only in part
    (partial_read: how many postings that list held as read; None when read to its end). Kept from found_at until
    until, viewing never extends it; it goes then, or when the user adds it to 我的岗位 or no longer shows it, or with its
    watch, never through the recycle bin. Its id stays in job_watch_seen, so it is never new again."""

    __tablename__ = "job_watch_postings"
    __table_args__ = (UniqueConstraint("watch_id", "source_id", "posting_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    watch_id: Mapped[str] = mapped_column(ForeignKey("job_watches.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(60))
    posting_id: Mapped[str] = mapped_column(String(64))
    facts: Mapped[dict] = mapped_column(JSON)
    screening: Mapped[dict] = mapped_column(JSON)
    partial_read: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    found_at: Mapped[datetime] = mapped_column(UTCDateTime)
    until: Mapped[datetime] = mapped_column(UTCDateTime)


class JobPostingRecheck(LibraryBase):
    """One check of a kept posting made from its record (U3-D4a), apart from the checks a search kept (JobPostingCheck).

    status is open (its source answered for it: still published then), closed (with evidence of it kept in evidence,
    never without), unknown (its source answered, but nothing it said settles it; reason says what) or unanswered (no
    answer; reason says why). checked_at is when its source last answered in the check, or for an unanswered one when it
    was tried; recorded_at is when it was written here. facts are the public facts the check read (whitelisted, as a
    kept posting's), None when it read none; content_sha256 names the text it read, never what the text said; changes
    names each fact that differs from the version before, with both values, and of the text only that it changed. The
    posting's text is never kept here, nor anywhere. Written only while the record stood as the check began; it goes
    with its record.
    """

    __tablename__ = "job_posting_rechecks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    posting_record_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("job_update_runs.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(String(40))
    evidence: Mapped[dict | None] = mapped_column(JSON)
    facts: Mapped[dict | None] = mapped_column(JSON)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    changes: Mapped[list] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobPostingText(LibraryBase):
    """The text of a posting the user pasted (U3-D4d), kept only once they added that posting to 我的岗位 themselves.

    It is the user's own material, as a decision's note is: only a user-provided posting has one, and a posting a source
    gave never does (a source's text is never kept). text is the text as the page that showed it before saving had it,
    its line ends made "\n"; text_sha256 names it as app.discovery.supplied.text_digest does. It goes to the recycle bin
    and comes back with its record, is never changed, and is deleted with it for good.
    """

    __tablename__ = "job_posting_texts"

    posting_record_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditEvent(LibraryBase):
    """What happened to an object and when. No content is stored here."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    object_type: Mapped[str] = mapped_column(String(30))
    object_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(UTCDateTime)


def create_schema(engine: Engine) -> None:
    """Create the library tables, refusing files that belong to something else.

    Everything happens in one transaction that holds the write lock from its
    start, so two processes opening the same new file cannot both create it.
    A file is checked before anything is written to it: one of another
    program, or of another library version, is refused as it is. A new file
    gets all its tables and its version together, so it is never left half
    created.
    """
    try:
        with Session(engine) as session, session.begin():
            connection = session.connection()
            existing = set(inspect(connection).get_table_names())

            if existing and LibraryMeta.__tablename__ not in existing:
                raise ConfigError("这个数据库文件里已有其他程序的表，不是经历库数据库；请指定一个新文件。")

            if existing:
                meta = session.get(LibraryMeta, "schema_version")
                stored = meta.value if meta is not None else None

                if stored in UPGRADABLE_SCHEMA_VERSIONS:
                    # Starting the app never upgrades a file on its own.
                    raise ConfigError(
                        f"经历库数据库的结构版本是 {stored}，当前程序只支持 {SCHEMA_VERSION}。这个文件没有被修改。"
                        "要继续使用它，请先备份，再在 backend 目录运行迁移命令："
                        "python -m app.library.migrate --db <这个数据库文件的路径> --apply"
                    )

                if stored != SCHEMA_VERSION:
                    raise ConfigError(
                        f"经历库数据库的结构版本是 {stored or '未知'}，当前程序只支持 {SCHEMA_VERSION}；"
                        "这个版本没有迁移工具。这个文件没有被修改，请换一个新文件。"
                    )

                return

            LibraryBase.metadata.create_all(connection)
            session.add(LibraryMeta(key="schema_version", value=SCHEMA_VERSION))
            session.add(LibraryMeta(key="library_id", value=str(uuid.uuid4())))
    except SQLAlchemyError as error:
        raise ConfigError("这个文件不是可用的 SQLite 数据库；请指定一个新文件。") from error


def read_library_id(engine: Engine) -> str:
    """The id this database got when it was created; its attachment folder carries it too."""
    with Session(engine) as session:
        meta = session.get(LibraryMeta, "library_id")

    if meta is None:
        raise ConfigError("经历库数据库缺少编号，无法确认它的附件目录。这个文件没有被修改，请换一个新文件。")

    return meta.value
