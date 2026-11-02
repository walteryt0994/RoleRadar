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
"""
import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, UniqueConstraint, inspect
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
# A file of version 3, 4, 5 or 6 is upgraded by app.library.migrate.
SCHEMA_VERSION = "7"
# The versions app.library.migrate upgrades from.
UPGRADABLE_SCHEMA_VERSIONS = ("3", "4", "5", "6")


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
