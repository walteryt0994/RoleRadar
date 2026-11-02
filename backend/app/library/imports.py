"""Resume imports: reading an old resume and keeping it for at most seven days (U2-B1).

A user uploads one text PDF or DOCX, or pastes text, after agreeing that it
is kept here for at most IMPORT_WINDOW. What is read is shown as it was
found; nothing becomes an experience, a profile or a preference, and nothing
is sent anywhere. Candidates read from the text (review.py, U2-B2a) belong
to the import and end with it.

The seven days start when the server had the whole file or text
(received_at) and never move: opening, refreshing, reading again or
restarting the app changes nothing. From expires_at on, an import is gone
for every page, download and export, even before the clean-up pass has
removed its file.

An uploaded file moves through states that are true at every moment, the
way an attachment does (attachments.py): a receiving row is reserved (it
counts against MAX_OPEN_IMPORTS), the staging file gets its kept name in the
store's imports area, and the row turns to reading. Reading happens in a
child process (resume_reader); its result is stored only if the import is
still reading and not expired, so a discard, an expiry or a second reading
that came first is never overwritten, and an ended import never comes back.

Discarding or expiring ends an import: in one transaction its content (what
was read, the file name, the checksum) leaves the row, then its file is
removed. A file that cannot be removed yet keeps the row's storage key, and
the clean-up pass (cleanup_imports) tries again; the row says so until the
file is gone. Ended rows hold no content and are forgotten FORGET_AFTER
after their clean-up finished.

An open import can be saved as a draft (ImportDraft, U2-B3a) to keep
checking it later: everything it holds stays, and its seven days no longer
apply. Deleting a draft puts it in the recycle bin for RESTORE_WINDOW with
nothing removed, so restoring brings it back as it was; removing it for
good, or its deadline passing, ends it like any import (DELETED). kept_as
says which of these an import is in at a given moment, and every page,
download, export, late result and clean-up asks it; nothing else decides.
A saved draft is never discarded: only deleting it, with its restore
period, ends it. Saving writes nothing into the user's records.
"""
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from app.library.attachments import (
    CONTENT_MISMATCH,
    EMPTY,
    TYPE_MISMATCH,
    FileType,
    UploadRejected,
    clean_filename,
)
from app.library.db import Database
from app.library.errors import FileStoreError, NotFound, StorageError
from app.library.files import IMPORTS, FileStore, StagingFile
from app.library.lifecycle import RESTORE_WINDOW
from app.library.models import (
    ImportAdoption,
    ImportCandidate,
    ImportDraft,
    ImportSource,
    ImportState,
    ImportStructuring,
    ResumeImport,
    StructuringState,
)
from app.library.resume_reader import (
    BUSY,
    CRASHED,
    DOCX,
    MAX_FILE_BYTES,
    MAX_TEXT_CHARS,
    OLE_SIGNATURE,
    PDF,
    TIMEOUT,
    Reading,
    Readings,
    clean_text,
    failed,
)
from app.library.service import new_id, record_event
from app.library.upload import MULTIPART_OVERHEAD, UNEXPECTED_PART, UploadForm

IMPORT_WINDOW = timedelta(days=7)
MAX_OPEN_IMPORTS = 5
# A receiving row is only that while its own request places the file.
STALE_RECEIVING_AFTER = timedelta(hours=1)
# A reading takes at most resume_reader.READ_SECONDS plus its wait for a turn.
STALE_READING_AFTER = timedelta(minutes=10)
# A search for candidates that ran this long was cut off (the app stopped); it may run again.
STALE_STRUCTURING_AFTER = timedelta(minutes=10)
FORGET_AFTER = timedelta(days=30)
# Bytes a pasted text may take in the form: four per character, and line breaks sent as two.
MAX_TEXT_BYTES = 6 * MAX_TEXT_CHARS
MAX_IMPORT_REQUEST_BYTES = MAX_FILE_BYTES + MAX_TEXT_BYTES + MULTIPART_OVERHEAD
IMPORT_OBJECT = "resume_import"

logger = logging.getLogger("roleradar.library")

VISIBLE = (ImportState.READING, ImportState.READ, ImportState.PARTIAL, ImportState.FAILED)
ENDED = (ImportState.DISCARDED, ImportState.EXPIRED, ImportState.DELETED)
# Why a stored file could not be read, beyond what resume_reader reports.
MISSING_FILE = "missing_file"
INTERRUPTED = "interrupted"
# Reading again may help: the file was not at fault.
RETRYABLE = frozenset({TIMEOUT, CRASHED, BUSY, INTERRUPTED})

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Browsers on some systems declare these files as generic binary data; the content is checked anyway.
IMPORT_PDF = FileType(
    "application/pdf",
    "PDF",
    b"%PDF-",
    frozenset({"application/pdf", "application/x-pdf", "application/octet-stream"}),
)
IMPORT_DOCX = FileType(
    DOCX_MEDIA,
    "Word（.docx）",
    b"PK\x03\x04",
    frozenset({DOCX_MEDIA, "application/octet-stream", "application/zip", "application/x-zip-compressed"}),
)
IMPORT_FILE_TYPES = {".pdf": IMPORT_PDF, ".docx": IMPORT_DOCX}
SOURCES = {IMPORT_PDF.media_type: ImportSource.PDF, IMPORT_DOCX.media_type: ImportSource.DOCX}

CHOOSE_AGAIN = "请重新选择文件（浏览器不会自动保留刚才选择的文件），或者改为粘贴文字。"
NO_NAME = "这个文件没有可用的文件名，没有读取。" + CHOOSE_AGAIN
UNSUPPORTED = "只能导入文字可读的 PDF 或 Word（.docx）文件，这个文件没有读取。" + CHOOSE_AGAIN
OLD_WORD = "旧版 Word（.doc）文件不能直接读取。可以在 Word 里另存为 .docx 或 PDF 再导入，或者粘贴文字。"
LOCKED_WORD = (
    "这个 Word 文件设置了打开密码，或者其实是旧版 .doc 格式，无法读取。"
    "可以去掉密码后另存为 .docx 再导入，或者粘贴文字。"
)
TOO_LARGE = "文件超过 10 MB 的上限，没有读取。可以改为粘贴需要的文字。"
TEXT_TOO_LONG = f"粘贴的文字超过 {MAX_TEXT_CHARS // 10000} 万字的上限，没有暂存。可以只粘贴需要的部分。"
NO_CONSENT = "请先勾选同意暂存。这次没有读取，也没有保存任何内容。"
BOTH = "文件和粘贴的文字只能二选一，这次没有读取。粘贴的文字还在下面；如果要用文件，请清空文字后重新选择文件。"
NOTHING = "请选择一个文件，或者粘贴简历文字。"


class Stored(StrEnum):
    ADDED = "added"
    # The same form was sent again with the same file or text.
    ALREADY_ADDED = "already_added"
    # The form's key belongs to another file or text, or to an import that ended.
    FORM_USED = "form_used"
    FULL = "full"
    FAILED = "failed"
    # Failed, and what the upload left on disk could not all be removed yet:
    # no page shows it, and the clean-up pass removes it.
    FAILED_LEFTOVER = "failed_leftover"


class Kept(StrEnum):
    """How an import is kept at a given moment (kept_as); one answer for every use of it."""

    # Not saved, and within its seven days.
    TEMPORARY = "temporary"
    # Saved as a draft: kept until the user deletes it.
    SAVED = "saved"
    # A deleted draft before its restore deadline: out of use, restorable.
    BINNED = "binned"
    # Ended, expired, past its restore deadline, or not yet received.
    GONE = "gone"


class Saved(StrEnum):
    SAVED = "saved"
    # Saved before, from this page or another.
    ALREADY = "already"
    # Being read, or its candidates are being found: saving waits until that ends.
    BUSY = "busy"


class ImportChanged(Exception):
    """The import changed after the page was shown (another tab, or a result that came in); nothing was done."""


class RestoreClosed(Exception):
    """The draft's restore deadline has passed, or it was removed for good: it cannot come back."""


class SavedDraft(Exception):
    """The import is a saved draft: a request to discard it (from an older page) is refused, nothing is removed."""


# --- the import form ------------------------------------------------------------


def check_import_announced(raw_name: str, declared_type: str) -> tuple[str, FileType]:
    """Check an import's file name and declared type, before any byte of it is kept."""
    name = clean_filename(raw_name)

    if not name:
        raise UploadRejected(NO_NAME, 422)

    extension = os.path.splitext(name)[1].lower()

    if extension == ".doc":
        raise UploadRejected(OLD_WORD, 415)

    kind = IMPORT_FILE_TYPES.get(extension)

    if kind is None:
        raise UploadRejected(UNSUPPORTED, 415)

    if declared_type.split(";", 1)[0].strip().lower() not in kind.declared:
        raise UploadRejected(TYPE_MISMATCH, 415)

    return name, kind


def check_import_content(kind: FileType, head: bytes, size: int) -> None:
    """Check an import's first bytes: not empty, and starting the way its type does."""
    if size == 0:
        raise UploadRejected(EMPTY, 422)

    if kind is IMPORT_DOCX and head.startswith(OLE_SIGNATURE):
        raise UploadRejected(LOCKED_WORD, 415)

    if not head.startswith(kind.signature):
        raise UploadRejected(CONTENT_MISMATCH, 415)


IMPORT_FORM = UploadForm(
    fields={
        "csrf_token": (200, UNEXPECTED_PART),
        "upload_key": (200, UNEXPECTED_PART),
        "consent": (20, UNEXPECTED_PART),
        "text": (MAX_TEXT_BYTES, TEXT_TOO_LONG),
    },
    announced=check_import_announced,
    content=check_import_content,
    max_file_bytes=MAX_FILE_BYTES,
    too_large=TOO_LARGE,
    max_request_bytes=MAX_IMPORT_REQUEST_BYTES,
    file_optional=True,
)


def pasted_text(raw: str) -> str:
    """Pasted text as it is kept: line breaks made plain, control characters dropped."""
    return clean_text(raw)


# --- reading -----------------------------------------------------------------------


def draft_of(db: Session, import_id: str) -> ImportDraft | None:
    return db.get(ImportDraft, import_id)


def kept_as(db: Session, row: ResumeImport, now: datetime) -> Kept:
    """Whether the import is a temporary one, a saved draft, a draft in the recycle bin, or gone."""
    if row.state not in VISIBLE:
        return Kept.GONE

    draft = draft_of(db, row.id)

    if draft is None:
        return Kept.TEMPORARY if now < row.expires_at else Kept.GONE

    if draft.deleted_at is None:
        return Kept.SAVED

    return Kept.BINNED if now < draft.purge_after else Kept.GONE


def is_open(db: Session, row: ResumeImport, now: datetime) -> bool:
    """Whether the import is in use: opened, downloaded, read, structured, checked and adopted from."""
    return kept_as(db, row, now) in (Kept.TEMPORARY, Kept.SAVED)


def _draft(*conditions):
    """SQL: the import has a draft row that meets these conditions."""
    return exists().where(ImportDraft.import_id == ResumeImport.id, *conditions)


def _open_now(now: datetime):
    """SQL for is_open."""
    return and_(
        ResumeImport.state.in_(VISIBLE),
        or_(
            and_(~_draft(), ResumeImport.expires_at > now),
            _draft(ImportDraft.deleted_at.is_(None)),
        ),
    )


def _binned_now(now: datetime):
    """SQL: a deleted draft before its restore deadline."""
    return and_(
        ResumeImport.state.in_(VISIBLE),
        _draft(ImportDraft.deleted_at.is_not(None), ImportDraft.purge_after > now),
    )


def _past_deadline(now: datetime):
    """SQL: not ended yet, but gone: a temporary import past its seven days, or a deleted draft past its deadline."""
    return and_(
        ResumeImport.state.in_(VISIBLE),
        or_(
            and_(~_draft(), ResumeImport.expires_at <= now),
            _draft(ImportDraft.deleted_at.is_not(None), ImportDraft.purge_after <= now),
        ),
    )


def open_imports(db: Session, owner_id: str, now: datetime) -> list[ResumeImport]:
    """The owner's imports in use (temporary ones and saved drafts), newest first."""
    return list(
        db.scalars(
            select(ResumeImport)
            .where(ResumeImport.owner_id == owner_id, _open_now(now))
            .order_by(ResumeImport.received_at.desc(), ResumeImport.id)
        )
    )


def kept_imports(db: Session, owner_id: str, now: datetime) -> list[ResumeImport]:
    """The owner's imports still kept: those in use and drafts in the recycle bin (for the export), newest first."""
    return list(
        db.scalars(
            select(ResumeImport)
            .where(ResumeImport.owner_id == owner_id, or_(_open_now(now), _binned_now(now)))
            .order_by(ResumeImport.received_at.desc(), ResumeImport.id)
        )
    )


def binned_drafts(db: Session, owner_id: str, now: datetime) -> list[tuple[ResumeImport, ImportDraft]]:
    """The owner's drafts in the recycle bin that can still be restored, most recently deleted first."""
    return [
        (row, draft)
        for row, draft in db.execute(
            select(ResumeImport, ImportDraft)
            .join(ImportDraft, ImportDraft.import_id == ResumeImport.id)
            .where(
                ResumeImport.owner_id == owner_id,
                ResumeImport.state.in_(VISIBLE),
                ImportDraft.deleted_at.is_not(None),
                ImportDraft.purge_after > now,
            )
            .order_by(ImportDraft.deleted_at.desc(), ResumeImport.id)
        )
    ]


def drafts_clearing(db: Session, owner_id: str, now: datetime) -> int:
    """Deleted drafts of the owner that cannot be restored any more and whose clean-up has not finished."""
    return db.scalar(
        select(func.count())
        .select_from(ResumeImport)
        .where(
            ResumeImport.owner_id == owner_id,
            or_(
                and_(ResumeImport.state == ImportState.DELETED, ResumeImport.cleared_at.is_(None)),
                and_(
                    ResumeImport.state.in_(VISIBLE),
                    _draft(ImportDraft.deleted_at.is_not(None), ImportDraft.purge_after <= now),
                ),
            ),
        )
    )


def _owned(db: Session, owner_id: str, import_id: str) -> ResumeImport | None:
    return db.scalar(select(ResumeImport).where(ResumeImport.id == import_id, ResumeImport.owner_id == owner_id))


def open_import(db: Session, owner_id: str, import_id: str, now: datetime) -> ResumeImport:
    """One import of this owner in use now (temporary or saved); NotFound otherwise."""
    row = _owned(db, owner_id, import_id)

    if row is None or not is_open(db, row, now):
        raise NotFound()

    return row


def binned_draft(db: Session, owner_id: str, import_id: str, now: datetime) -> tuple[ResumeImport, ImportDraft]:
    """One draft of this owner in the recycle bin that can still be restored; NotFound otherwise."""
    row = _owned(db, owner_id, import_id)

    if row is None or kept_as(db, row, now) != Kept.BINNED:
        raise NotFound()

    return row, draft_of(db, row.id)


def taken_places(db: Session, owner_id: str, now: datetime) -> int:
    """Imports that count against MAX_OPEN_IMPORTS: uploads being placed, imports in use and restorable drafts.

    A draft in the recycle bin keeps its place, so restoring it never goes
    past the limit.
    """
    return db.scalar(
        select(func.count())
        .select_from(ResumeImport)
        .where(
            ResumeImport.owner_id == owner_id,
            or_(
                and_(ResumeImport.state == ImportState.RECEIVING, ResumeImport.expires_at > now),
                _open_now(now),
                _binned_now(now),
            ),
        )
    )


def clearing_count(db: Session, owner_id: str, now: datetime) -> int:
    """Ended or gone imports of the owner (expired, discarded, deleted drafts) whose clean-up has not finished."""
    return db.scalar(
        select(func.count())
        .select_from(ResumeImport)
        .where(
            ResumeImport.owner_id == owner_id,
            or_(
                and_(ResumeImport.state.in_(ENDED), ResumeImport.cleared_at.is_(None)),
                _past_deadline(now),
            ),
        )
    )


def retryable(row: ResumeImport) -> bool:
    return row.state == ImportState.FAILED and row.outcome in RETRYABLE and row.storage_key is not None


# --- storing what came in ------------------------------------------------------------


def _new_row(owner_id: str, upload_key: str, source: str, state: str, now: datetime) -> ResumeImport:
    return ResumeImport(
        id=new_id(),
        owner_id=owner_id,
        source=source,
        state=state,
        filename=None,
        media_type=None,
        size=None,
        sha256=None,
        storage_key=None,
        upload_key=upload_key,
        outcome=None,
        segments=None,
        details=None,
        text_chars=None,
        revision=1,
        received_at=now,
        expires_at=now + IMPORT_WINDOW,
        read_at=None,
        created_at=now,
        updated_at=now,
        ended_at=None,
        cleared_at=None,
        cleanup_attempts=0,
        cleanup_error=None,
    )


def _earlier(db: Session, owner_id: str, upload_key: str) -> ResumeImport | None:
    return db.scalar(
        select(ResumeImport).where(ResumeImport.owner_id == owner_id, ResumeImport.upload_key == upload_key)
    )


def store_text(database: Database, owner_id: str, upload_key: str, text: str, now: datetime) -> tuple[Stored, str | None]:
    """Keep pasted text as an import; it needs no reading, it is the original."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

    with database.transaction() as db:
        earlier = _earlier(db, owner_id, upload_key)

        if earlier is not None:
            if earlier.source == ImportSource.TEXT and earlier.sha256 == digest and is_open(db, earlier, now):
                return Stored.ALREADY_ADDED, earlier.id

            return Stored.FORM_USED, None

        if taken_places(db, owner_id, now) >= MAX_OPEN_IMPORTS:
            return Stored.FULL, None

        row = _new_row(owner_id, upload_key, ImportSource.TEXT, ImportState.READ, now)
        row.size = len(text.encode("utf-8"))
        row.sha256 = digest
        row.segments = [{"kind": "text", "number": 1, "text": text}]
        row.details = {"lines": text.count("\n") + 1}
        row.text_chars = len(text)
        row.read_at = now
        db.add(row)
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_received", now)

        return Stored.ADDED, row.id


def store_file(
    database: Database,
    files: FileStore,
    owner_id: str,
    upload_key: str,
    filename: str,
    kind: FileType,
    staging: StagingFile,
    now: datetime,
) -> tuple[Stored, str | None]:
    """Reserve, place and activate one received file; the import is then waiting to be read.

    The staging name is removed at the end; when the file was added, its
    bytes stay under their kept name in the imports area. A failure that
    could not remove everything the upload put on disk (its kept name, or
    its staging name) answers FAILED_LEFTOVER, never FAILED.
    """
    try:
        outcome = _store_file(database, files, owner_id, upload_key, filename, kind, staging, now)
    finally:
        staging_gone = staging.discard()

    if outcome[0] == Stored.FAILED and not staging_gone:
        return Stored.FAILED_LEFTOVER, None

    return outcome


def _store_file(
    database: Database,
    files: FileStore,
    owner_id: str,
    upload_key: str,
    filename: str,
    kind: FileType,
    staging: StagingFile,
    now: datetime,
) -> tuple[Stored, str | None]:
    try:
        with database.transaction() as db:
            outcome, row = _reserve(db, owner_id, upload_key, filename, kind, staging, now)
    except StorageError as error:
        logger.error("reserving an import failed (%s)", type(error.__cause__).__name__)
        return Stored.FAILED, None

    if row is None:
        return outcome

    try:
        files.place(staging, owner_id, area=IMPORTS)
    except (OSError, ValueError, FileStoreError) as error:
        logger.error("placing an import file failed (%s)", type(error).__name__)
        return _undone(abandon(database, files, owner_id, row.id, staging.key, now))

    try:
        with database.transaction() as db:
            activated = _activate(db, owner_id, row.id, now)
    except StorageError as error:
        logger.error("activating an import failed (%s)", type(error.__cause__).__name__)
        activated = False

    if not activated:
        return _undone(abandon(database, files, owner_id, row.id, staging.key, now))

    return Stored.ADDED, row.id


def _undone(gone: bool) -> tuple[Stored, None]:
    return (Stored.FAILED if gone else Stored.FAILED_LEFTOVER), None


def _reserve(
    db: Session,
    owner_id: str,
    upload_key: str,
    filename: str,
    kind: FileType,
    staging: StagingFile,
    now: datetime,
) -> tuple[tuple[Stored, str | None], ResumeImport | None]:
    earlier = _earlier(db, owner_id, upload_key)

    if earlier is not None:
        same_file = (
            earlier.filename == filename
            and earlier.size == staging.size
            and earlier.sha256 == staging.sha256
            and (is_open(db, earlier, now) or earlier.state == ImportState.RECEIVING)
        )

        return ((Stored.ALREADY_ADDED, earlier.id) if same_file else (Stored.FORM_USED, None)), None

    if taken_places(db, owner_id, now) >= MAX_OPEN_IMPORTS:
        return (Stored.FULL, None), None

    row = _new_row(owner_id, upload_key, SOURCES[kind.media_type], ImportState.RECEIVING, now)
    row.filename = filename
    row.media_type = kind.media_type
    row.size = staging.size
    row.sha256 = staging.sha256
    # The staging file's key: the row names both of the file's names.
    row.storage_key = staging.key
    db.add(row)

    return (Stored.ADDED, row.id), row


def _activate(db: Session, owner_id: str, import_id: str, now: datetime) -> bool:
    row = db.get(ResumeImport, import_id)

    if row is None or row.owner_id != owner_id or row.state != ImportState.RECEIVING or now >= row.expires_at:
        return False

    row.state = ImportState.READING
    row.revision += 1
    row.updated_at = now
    record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_received", now)

    return True


def abandon(
    database: Database,
    files: FileStore,
    owner_id: str,
    import_id: str,
    storage_key: str,
    now: datetime,
) -> bool:
    """Undo an upload that did not become an import; True only once nothing of it is left.

    The row is ended first, so no later step can make it readable, then its
    file goes under both its names, then the row: the user never saw it.
    When the file cannot be removed yet, the ended row (without content)
    keeps its storage key, the clean-up pass retries, and False says so. A
    row that is gone does not stop the file from being removed.
    """
    try:
        with database.transaction() as db:
            row = db.get(ResumeImport, import_id)

            if row is not None and row.state != ImportState.RECEIVING:
                # Not an unfinished upload any more: nothing here may be undone.
                return False

            if row is not None:
                _end(db, row, ImportState.DISCARDED, now)
                row.details = {"abandoned": True}

        if row is None:
            files.remove_file(owner_id, storage_key, area=IMPORTS)
            return True

        if not _remove_file(database, files, import_id, owner_id, now):
            return False

        with database.transaction() as db:
            row = db.get(ResumeImport, import_id)

            if row is not None:
                db.delete(row)

        return True
    except (StorageError, FileStoreError, OSError, ValueError) as error:
        logger.warning("an unfinished import upload is left for the clean-up pass (%s)", type(error).__name__)

    return False


# --- reading a stored file ------------------------------------------------------------


def read_import(
    database: Database,
    files: FileStore,
    readings: Readings,
    owner_id: str,
    import_id: str,
    clock: Callable[[], datetime],
) -> str | None:
    """Read a reading import's file and store what was read; return its new state.

    None when the import is not reading any more (ended, expired, deleted,
    or read by another request meanwhile): then the result is dropped and
    nothing is written. The revision must be the one the reading began
    with, so a draft deleted and restored meanwhile does not take it either.
    """
    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)

        if row is None or row.owner_id != owner_id or row.state != ImportState.READING or not is_open(db, row, clock()):
            return None

        revision, key, size, digest, source = row.revision, row.storage_key, row.size, row.sha256, row.source

    try:
        with files.open_checked(owner_id, key, size, digest, area=IMPORTS) as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except (FileStoreError, OSError, ValueError) as error:
        logger.warning("an import file could not be read (%s)", type(error).__name__)
        reading = failed(MISSING_FILE)
    else:
        reading = readings.read(import_id, PDF if source == ImportSource.PDF else DOCX, data)

    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        now = clock()

        if row is None or row.state != ImportState.READING or row.revision != revision or not is_open(db, row, now):
            return None

        _store_reading(row, reading, now)
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, f"import_{row.state}", now)

        return row.state


def _store_reading(row: ResumeImport, reading: Reading, now: datetime) -> None:
    state = {"read": ImportState.READ, "partial": ImportState.PARTIAL}.get(reading.status, ImportState.FAILED)
    row.state = state
    row.outcome = reading.failure
    row.segments = reading.segments or None
    row.details = {**reading.notes, **({"pages": reading.pages} if reading.pages is not None else {})}
    row.text_chars = reading.chars if reading.segments else None
    row.read_at = now
    row.revision += 1
    row.updated_at = now


def start_again(database: Database, owner_id: str, import_id: str, expected_revision: int, now: datetime) -> bool:
    """Make a failed import whose failure was not the file's reading again; False if it cannot."""
    with database.transaction() as db:
        row = open_import(db, owner_id, import_id, now)

        if row.revision != expected_revision or not retryable(row):
            return False

        row.state = ImportState.READING
        row.outcome = None
        row.details = None
        row.revision += 1
        row.updated_at = now

    return True


# --- ending -----------------------------------------------------------------------------


class Ended(StrEnum):
    # The content is gone, and so is the file.
    CLEARED = "cleared"
    # The content is gone; the file is not yet, the clean-up retries.
    PENDING = "pending"


def discard(
    database: Database,
    files: FileStore,
    readings: Readings,
    owner_id: str,
    import_id: str,
    now: datetime,
) -> Ended:
    """End one open import at the user's request; NotFound if it is not open (or is another's).

    Whatever state the import reached since the page was shown, discarding
    is what the user asked for, so it goes ahead; unless it was saved as a
    draft meanwhile (SavedDraft): a draft is only ever deleted, with its
    restore period, never discarded by an older page's form.
    """
    with database.transaction() as db:
        row = open_import(db, owner_id, import_id, now)

        if draft_of(db, row.id) is not None:
            raise SavedDraft()

        _end(db, row, ImportState.DISCARDED, now)
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_discarded", now)
        cleared = row.cleared_at is not None

    # A reading still running stops now; its result would be dropped anyway.
    readings.stop(import_id)

    if cleared or _remove_file(database, files, import_id, owner_id, now):
        return Ended.CLEARED

    return Ended.PENDING


def _end(db: Session, row: ResumeImport, state: str, now: datetime) -> None:
    """End an import: from now on it holds no content; its file is removed next.

    The candidates read from it, with the user's edits, how they were read
    and where adopted ones went go in the same transaction: they are part of
    the import, and so is its draft row, if it was saved. What was adopted
    stays in the records it was written into.
    """
    db.execute(delete(ImportDraft).where(ImportDraft.import_id == row.id))
    db.execute(delete(ImportAdoption).where(ImportAdoption.import_id == row.id))
    db.execute(delete(ImportCandidate).where(ImportCandidate.import_id == row.id))
    db.execute(delete(ImportStructuring).where(ImportStructuring.import_id == row.id))
    row.state = state
    row.ended_at = now
    row.filename = None
    row.media_type = None
    row.size = None
    row.sha256 = None
    row.outcome = None
    row.segments = None
    row.details = None
    row.text_chars = None
    row.revision += 1
    row.updated_at = now

    if row.storage_key is None:
        # Pasted text has no file: removing the row's content was all.
        row.cleared_at = now


def _remove_file(database: Database, files: FileStore, import_id: str, owner_id: str, now: datetime) -> bool:
    """Remove an ended import's file under both its names; True once it is gone and recorded as gone.

    This runs after the import's ending was committed (a discard, an expiry,
    a draft removed for good), which nothing here can undo or may report as
    undone. So a database failure on the way is logged and returns False,
    like a file that cannot be removed yet: the row keeps its storage key
    with cleared_at empty, the pages say the clean-up is still to finish,
    and the clean-up pass tries again. A file already removed is then found
    gone, and only the record is written.
    """
    try:
        with database.transaction() as db:
            row = db.get(ResumeImport, import_id)

            if row is None or row.storage_key is None or row.cleared_at is not None:
                return True

            key = row.storage_key
    except StorageError as failure:
        logger.warning("an ended import's clean-up could not start yet (%s)", type(failure.__cause__).__name__)
        return False

    try:
        files.remove_file(owner_id, key, area=IMPORTS)
    except (FileStoreError, OSError, ValueError) as error:
        logger.warning("an import file could not be removed yet (%s)", type(error).__name__)

        try:
            with database.transaction() as db:
                row = db.get(ResumeImport, import_id)

                if row is not None:
                    row.cleanup_attempts += 1
                    row.cleanup_error = type(error).__name__
        except StorageError as failure:
            logger.warning("a failed import clean-up could not be noted (%s)", type(failure.__cause__).__name__)

        return False

    try:
        with database.transaction() as db:
            row = db.get(ResumeImport, import_id)

            if row is not None:
                row.storage_key = None
                row.cleared_at = now
                row.cleanup_error = None
                record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_cleared", now)
    except StorageError as failure:
        logger.warning("an import file was removed but not yet recorded as gone (%s)", type(failure.__cause__).__name__)
        return False

    return True


# --- saved drafts (U2-B3a) ----------------------------------------------------------


def _locked(db: Session, owner_id: str, import_id: str, clock: Callable[[], datetime]) -> tuple[ResumeImport | None, datetime]:
    """The owner's import and the time, read once this transaction holds the write lock.

    The first statement takes the lock (BEGIN IMMEDIATE); the clock is read
    after it, so a request that waited for the lock past a deadline is
    judged by the moment it acts, not by when it arrived.
    """
    row = _owned(db, owner_id, import_id)

    return row, clock()


def _finding_candidates(db: Session, row: ResumeImport, now: datetime) -> ImportStructuring | None:
    """The search for candidates running for this import now, if any (a stale one is over)."""
    structuring = db.get(ImportStructuring, row.id)

    if structuring is None or structuring.state != StructuringState.RUNNING:
        return None

    return structuring if now < structuring.started_at + STALE_STRUCTURING_AFTER else None


def save_draft(database: Database, owner_id: str, import_id: str, clock: Callable[[], datetime]) -> Saved:
    """Keep an import in use as a draft; NotFound when it is not in use now (ended, expired, deleted, another's).

    A partial reading or an unfinished review can be saved as it is; an
    import being read, or one whose candidates are being found, waits
    (BUSY) so no result in flight is lost or taken in by surprise. Nothing
    is written into the user's records.
    """
    with database.transaction() as db:
        row, now = _locked(db, owner_id, import_id, clock)

        if row is None:
            raise NotFound()

        kept = kept_as(db, row, now)

        if kept == Kept.SAVED:
            return Saved.ALREADY

        if kept != Kept.TEMPORARY:
            raise NotFound()

        if row.state == ImportState.READING or _finding_candidates(db, row, now) is not None:
            return Saved.BUSY

        db.add(ImportDraft(import_id=row.id, owner_id=owner_id, saved_at=now, deleted_at=None, purge_after=None))
        row.revision += 1
        row.updated_at = now
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_saved", now)

    return Saved.SAVED


def delete_draft(
    database: Database, owner_id: str, import_id: str, expected_revision: int, clock: Callable[[], datetime]
) -> None:
    """Put a saved draft in the recycle bin until RESTORE_WINDOW from now; nothing it holds is removed.

    A reading, or a search for candidates, still running for it is ended
    here as interrupted and the revision moves on: a late result is never
    taken in, now or after a restore, and restoring starts nothing. The
    caller stops the reading process itself.
    """
    with database.transaction() as db:
        row, now = _locked(db, owner_id, import_id, clock)

        if row is None or kept_as(db, row, now) != Kept.SAVED:
            raise NotFound()

        if row.revision != expected_revision:
            raise ImportChanged()

        draft = draft_of(db, row.id)
        draft.deleted_at = now
        draft.purge_after = now + RESTORE_WINDOW

        if row.state == ImportState.READING:
            row.state = ImportState.FAILED
            row.outcome = INTERRUPTED

        structuring = db.get(ImportStructuring, row.id)

        if structuring is not None and structuring.state == StructuringState.RUNNING:
            # Asking again is allowed after a restore; this run's answer is dropped.
            structuring.state = StructuringState.FAILED
            structuring.outcome = None
            structuring.finished_at = now

        row.revision += 1
        row.updated_at = now
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_draft_deleted", now)


def restore_draft(
    database: Database, owner_id: str, import_id: str, expected_revision: int, clock: Callable[[], datetime]
) -> None:
    """Bring a draft back from the recycle bin as it was; RestoreClosed once its deadline has passed.

    Its candidates, the user's edits and choices and where adopted ones
    went come back unchanged: an adopted candidate stays adopted and is
    never written again.
    """
    with database.transaction() as db:
        row, now = _locked(db, owner_id, import_id, clock)

        if row is None:
            raise NotFound()

        kept = kept_as(db, row, now)

        if kept != Kept.BINNED:
            draft = draft_of(db, row.id)

            if row.state == ImportState.DELETED or (draft is not None and draft.deleted_at is not None):
                raise RestoreClosed()

            raise NotFound()

        if row.revision != expected_revision:
            raise ImportChanged()

        draft = draft_of(db, row.id)
        draft.deleted_at = None
        draft.purge_after = None
        row.revision += 1
        row.updated_at = now
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_draft_restored", now)


def purge_draft(
    database: Database,
    files: FileStore,
    owner_id: str,
    import_id: str,
    expected_revision: int,
    clock: Callable[[], datetime],
) -> Ended:
    """Remove a draft in the recycle bin for good, now, at the user's request.

    It ends like any import (DELETED): its content leaves the row in this
    transaction, its file next. Only a draft still restorable is taken
    here; one past its deadline is the clean-up pass's.

    The transaction is the line: a StorageError raised from it means it was
    rolled back and the draft is still in the recycle bin. Once it is
    committed the draft is gone for good, whatever the clean-up after it
    meets: the answer is then CLEARED, or PENDING when the file, or the
    record that it is gone, is still to come (_remove_file never raises).
    """
    with database.transaction() as db:
        row, now = _locked(db, owner_id, import_id, clock)

        if row is None or kept_as(db, row, now) != Kept.BINNED:
            raise NotFound()

        if row.revision != expected_revision:
            raise ImportChanged()

        _end(db, row, ImportState.DELETED, now)
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_draft_purged", now)
        cleared = row.cleared_at is not None

    if cleared or _remove_file(database, files, import_id, owner_id, now):
        return Ended.CLEARED

    return Ended.PENDING


# --- clean-up ----------------------------------------------------------------------


@dataclass
class ImportCleanup:
    expired: int = 0
    # Deleted drafts past their restore deadline.
    purged: int = 0
    interrupted: int = 0
    cleared: int = 0
    pending: int = 0
    forgotten: int = 0


def cleanup_imports(database: Database, files: FileStore, now: datetime) -> ImportCleanup:
    """One pass: end expired imports and deleted drafts past their deadline, stop stale ones,
    remove ended imports' files, forget old rows.

    A temporary import ends at its expires_at; a saved draft never does. A
    deleted draft ends at its purge_after, unless it was restored first:
    each row is selected and ended in one transaction holding the write
    lock, so a restore either came first (and nothing is selected) or
    comes after (and finds the draft gone).
    """
    report = ImportCleanup()
    stale_receiving = now - STALE_RECEIVING_AFTER
    stale_reading = now - STALE_READING_AFTER

    with database.transaction() as db:
        for row in list(
            db.scalars(
                select(ResumeImport).where(
                    ResumeImport.state.in_(VISIBLE),
                    ResumeImport.expires_at <= now,
                    ~_draft(),
                )
            )
        ):
            _end(db, row, ImportState.EXPIRED, now)
            record_event(db, row.owner_id, row.id, IMPORT_OBJECT, row.id, "import_expired", now)
            report.expired += 1

        for row in list(
            db.scalars(
                select(ResumeImport).where(
                    ResumeImport.state.in_(VISIBLE),
                    _draft(ImportDraft.deleted_at.is_not(None), ImportDraft.purge_after <= now),
                )
            )
        ):
            _end(db, row, ImportState.DELETED, now)
            record_event(db, row.owner_id, row.id, IMPORT_OBJECT, row.id, "import_draft_expired", now)
            report.purged += 1

        for row in db.scalars(
            select(ResumeImport).where(
                ResumeImport.state == ImportState.RECEIVING,
                ResumeImport.updated_at <= stale_receiving,
            )
        ):
            _end(db, row, ImportState.DISCARDED, now)
            row.details = {"abandoned": True}

        for row in db.scalars(
            select(ResumeImport).where(
                ResumeImport.state == ImportState.READING,
                ResumeImport.updated_at <= stale_reading,
            )
        ):
            # Its request is gone (the app stopped while it read): it can be read again.
            row.state = ImportState.FAILED
            row.outcome = INTERRUPTED
            row.revision += 1
            row.updated_at = now
            report.interrupted += 1

        waiting = [
            (row.id, row.owner_id)
            for row in db.scalars(
                select(ResumeImport).where(ResumeImport.state.in_(ENDED), ResumeImport.cleared_at.is_(None))
            )
        ]

    for import_id, owner_id in waiting:
        if _remove_file(database, files, import_id, owner_id, now):
            report.cleared += 1
        else:
            report.pending += 1

    with database.transaction() as db:
        abandoned = [
            row.id
            for row in db.scalars(
                select(ResumeImport).where(
                    ResumeImport.state == ImportState.DISCARDED,
                    ResumeImport.cleared_at.is_not(None),
                )
            )
            if row.details and row.details.get("abandoned")
        ]
        forgotten = db.execute(
            delete(ResumeImport).where(
                or_(
                    ResumeImport.id.in_(abandoned),
                    and_(ResumeImport.state.in_(ENDED), ResumeImport.cleared_at <= now - FORGET_AFTER),
                )
            )
        )
        report.forgotten = forgotten.rowcount or 0

    return report


def referenced_import_files(database: Database) -> set[tuple[str, str]]:
    """Every (owner id, storage key) an import row still refers to, whatever its state."""
    with database.transaction() as db:
        return {
            (owner_id, key)
            for owner_id, key in db.execute(
                select(ResumeImport.owner_id, ResumeImport.storage_key).where(ResumeImport.storage_key.is_not(None))
            )
        }


# --- taking it away ------------------------------------------------------------------


def locator(segment: dict) -> str:
    """Where a segment was found, as people say it."""
    kind = segment["kind"]

    if kind == "page":
        return f"第 {segment['number']} 页"

    if kind == "paragraph":
        return f"第 {segment['number']} 段"

    if kind == "table":
        return f"表格 {segment['number']}"

    return "粘贴的文字"


def segment_text(segment: dict) -> str:
    if segment["kind"] == "table":
        return "\n".join(" | ".join(row) for row in segment["rows"])

    return segment["text"]


def import_text(row: ResumeImport) -> str:
    """What was read, as plain text: pages and tables marked, paragraphs apart, pasted text as it was.

    A page that could not be read, or had no text, says so in its mark
    rather than appearing as if it were read and empty.
    """
    parts = []

    for segment in row.segments or []:
        if segment["kind"] == "page" and segment.get("unreadable"):
            parts.append(f"=== {locator(segment)}（无法读取） ===")
        elif segment["kind"] == "page" and not segment["text"].strip():
            parts.append(f"=== {locator(segment)}（没有读到文字） ===")
        elif segment["kind"] in ("page", "table"):
            parts.append(f"=== {locator(segment)} ===\n{segment_text(segment)}")
        else:
            parts.append(segment_text(segment))

    return "\n\n".join(parts) + "\n"
