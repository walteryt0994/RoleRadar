"""Resume imports: reading an old resume and keeping it for at most seven days (U2-B1).

A user uploads one text PDF or DOCX, or pastes text, after agreeing that it
is kept here for at most IMPORT_WINDOW. What is read is shown as it was
found; nothing becomes an experience, a profile or a preference, and nothing
is sent anywhere. Later batches (B2, B3) turn it into candidates to check.

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
"""
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from sqlalchemy import and_, delete, func, or_, select
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
from app.library.models import ImportSource, ImportState, ResumeImport
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
FORGET_AFTER = timedelta(days=30)
# Bytes a pasted text may take in the form: four per character, and line breaks sent as two.
MAX_TEXT_BYTES = 6 * MAX_TEXT_CHARS
MAX_IMPORT_REQUEST_BYTES = MAX_FILE_BYTES + MAX_TEXT_BYTES + MULTIPART_OVERHEAD
IMPORT_OBJECT = "resume_import"

logger = logging.getLogger("roleradar.library")

VISIBLE = (ImportState.READING, ImportState.READ, ImportState.PARTIAL, ImportState.FAILED)
ENDED = (ImportState.DISCARDED, ImportState.EXPIRED)
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


def is_open(row: ResumeImport, now: datetime) -> bool:
    return row.state in VISIBLE and now < row.expires_at


def open_imports(db: Session, owner_id: str, now: datetime) -> list[ResumeImport]:
    """The owner's imports that can be opened, newest first."""
    return list(
        db.scalars(
            select(ResumeImport)
            .where(
                ResumeImport.owner_id == owner_id,
                ResumeImport.state.in_(VISIBLE),
                ResumeImport.expires_at > now,
            )
            .order_by(ResumeImport.received_at.desc(), ResumeImport.id)
        )
    )


def open_import(db: Session, owner_id: str, import_id: str, now: datetime) -> ResumeImport:
    """One import of this owner that can still be opened; NotFound otherwise."""
    row = db.scalar(
        select(ResumeImport).where(ResumeImport.id == import_id, ResumeImport.owner_id == owner_id)
    )

    if row is None or not is_open(row, now):
        raise NotFound()

    return row


def taken_places(db: Session, owner_id: str, now: datetime) -> int:
    """Imports that count against MAX_OPEN_IMPORTS: open ones, and uploads still being placed."""
    return db.scalar(
        select(func.count())
        .select_from(ResumeImport)
        .where(
            ResumeImport.owner_id == owner_id,
            ResumeImport.state.in_((ImportState.RECEIVING, *VISIBLE)),
            ResumeImport.expires_at > now,
        )
    )


def clearing_count(db: Session, owner_id: str, now: datetime) -> int:
    """Ended or expired imports of the owner whose clean-up has not finished."""
    return db.scalar(
        select(func.count())
        .select_from(ResumeImport)
        .where(
            ResumeImport.owner_id == owner_id,
            or_(
                and_(ResumeImport.state.in_(ENDED), ResumeImport.cleared_at.is_(None)),
                and_(ResumeImport.state.in_(VISIBLE), ResumeImport.expires_at <= now),
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
            if earlier.source == ImportSource.TEXT and earlier.sha256 == digest and is_open(earlier, now):
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
            and (is_open(earlier, now) or earlier.state == ImportState.RECEIVING)
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
                _end(row, ImportState.DISCARDED, now)
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

    None when the import is not reading any more (ended, expired, or read
    by another request meanwhile): then the result is dropped and nothing
    is written.
    """
    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)

        if row is None or row.owner_id != owner_id or row.state != ImportState.READING or clock() >= row.expires_at:
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

        if row is None or row.state != ImportState.READING or row.revision != revision or now >= row.expires_at:
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
    is what the user asked for, so it goes ahead.
    """
    with database.transaction() as db:
        row = open_import(db, owner_id, import_id, now)
        _end(row, ImportState.DISCARDED, now)
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_discarded", now)
        cleared = row.cleared_at is not None

    # A reading still running stops now; its result would be dropped anyway.
    readings.stop(import_id)

    if cleared or _remove_file(database, files, import_id, owner_id, now):
        return Ended.CLEARED

    return Ended.PENDING


def _end(row: ResumeImport, state: str, now: datetime) -> None:
    """End an import: from now on it holds no content; its file is removed next."""
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
    """Remove an ended import's file under both its names; True once it is gone."""
    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)

        if row is None or row.storage_key is None or row.cleared_at is not None:
            return True

        key = row.storage_key

    try:
        files.remove_file(owner_id, key, area=IMPORTS)
    except (FileStoreError, OSError, ValueError) as error:
        logger.warning("an import file could not be removed yet (%s)", type(error).__name__)

        with database.transaction() as db:
            row = db.get(ResumeImport, import_id)

            if row is not None:
                row.cleanup_attempts += 1
                row.cleanup_error = type(error).__name__

        return False

    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)

        if row is not None:
            row.storage_key = None
            row.cleared_at = now
            row.cleanup_error = None
            record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_cleared", now)

    return True


@dataclass
class ImportCleanup:
    expired: int = 0
    interrupted: int = 0
    cleared: int = 0
    pending: int = 0
    forgotten: int = 0


def cleanup_imports(database: Database, files: FileStore, now: datetime) -> ImportCleanup:
    """One pass: end expired imports, stop stale ones, remove ended imports' files, forget old rows."""
    report = ImportCleanup()
    stale_receiving = now - STALE_RECEIVING_AFTER
    stale_reading = now - STALE_READING_AFTER

    with database.transaction() as db:
        for row in db.scalars(
            select(ResumeImport).where(
                ResumeImport.state.in_(VISIBLE),
                ResumeImport.expires_at <= now,
            )
        ):
            _end(row, ImportState.EXPIRED, now)
            record_event(db, row.owner_id, row.id, IMPORT_OBJECT, row.id, "import_expired", now)
            report.expired += 1

        for row in db.scalars(
            select(ResumeImport).where(
                ResumeImport.state == ImportState.RECEIVING,
                ResumeImport.updated_at <= stale_receiving,
            )
        ):
            _end(row, ImportState.DISCARDED, now)
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
