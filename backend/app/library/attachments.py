"""Attachments of saved experiences: checking, storing and listing them.

An attachment is a PDF, PNG or JPEG file of at most MAX_FILE_BYTES, kept for
one saved experience of its owner; an experience keeps at most
MAX_ATTACHMENTS, counting those still restorable from the recycle bin. The
profile and the job preferences have none. Checking a file's type is not a
virus scan, and nothing here reads what a file says.

The bytes are in the file store and the facts in the database, and the two
cannot change in one transaction. An upload therefore moves through states
that are true at every moment (store_upload):

1. The body is received into a staging file (upload.py) and checked here:
   the name, the declared type and the file's first bytes must agree.
2. reserve: one transaction checks that the experience is the owner's and in
   use, that the form's one-time key is new, and that a place is free, and
   adds a pending row. Pending rows take a place, so two uploads at once
   cannot both take the last one.
3. place: the staging file gets its kept name (a hard link: nothing is
   overwritten).
4. activate: one transaction checks the experience again and makes the row
   ready. Only ready attachments are listed, downloaded or exported.

The staging name is removed at the end, but until then, or if that fails,
the bytes have two names. Both come from the row's storage key, so whoever
removes an attachment's file (abandon_upload, a permanent deletion) removes
both, and only then the row: no name of the file outlives its record.

A failed step is undone at once where possible (abandon_upload): the row is
marked failed, which no step accepts any more, then the file and the row
are removed. Whatever cannot be undone at once (the process stopped, the
disk or the database failed again) is finished by the clean-up pass
(recover_uploads): failed rows, and pending rows older than
STALE_UPLOAD_AFTER, lose their file first and their row last.
"""
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.library.db import Database
from app.library.errors import FileStoreError, NotFound, StorageError
from app.library.files import FileStore, StagingFile
from app.library.models import Attachment, AttachmentState, ExperienceItem, TargetType
from app.library.service import active_item, is_expired, new_id, record_event

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_FILENAME_LENGTH = 255
STALE_UPLOAD_AFTER = timedelta(hours=1)
UPLOAD_KEY = re.compile(r"[A-Za-z0-9_-]{20,64}")
TAKES_A_PLACE = (AttachmentState.PENDING, AttachmentState.READY)

logger = logging.getLogger("roleradar.library")


@dataclass(frozen=True)
class FileType:
    media_type: str
    label: str
    # The first bytes every file of this type starts with.
    signature: bytes
    # The types a browser may declare for such a file.
    declared: frozenset[str]


PDF = FileType("application/pdf", "PDF", b"%PDF-", frozenset({"application/pdf"}))
PNG = FileType("image/png", "PNG", b"\x89PNG\r\n\x1a\n", frozenset({"image/png"}))
JPEG = FileType("image/jpeg", "JPEG", b"\xff\xd8\xff", frozenset({"image/jpeg", "image/jpg", "image/pjpeg"}))
FILE_TYPES = {".pdf": PDF, ".png": PNG, ".jpg": JPEG, ".jpeg": JPEG}
TYPE_LABELS = {kind.media_type: kind.label for kind in FILE_TYPES.values()}

AGAIN = "请重新选择文件后再上传（浏览器不会自动保留刚才选择的文件）。"
NO_NAME = "这个文件没有可用的文件名，没有上传。" + AGAIN
UNSUPPORTED = "只能上传 PDF、PNG 或 JPEG 文件（扩展名 .pdf、.png、.jpg 或 .jpeg），这个文件没有上传。"
EMPTY = "这个文件是空的（0 字节），没有上传。" + AGAIN
TOO_LARGE = "文件超过 10 MB 的上限，没有上传。"
TYPE_MISMATCH = (
    "文件的扩展名和浏览器报告的文件类型对不上，没有上传。请确认文件没有被改过扩展名，" + AGAIN
)
CONTENT_MISMATCH = (
    "文件内容和它的扩展名对不上（例如把图片改名成了 .pdf），没有上传。请确认文件没有被改过扩展名，" + AGAIN
)


class UploadRejected(Exception):
    """The upload cannot be kept. message says why, in Chinese; nothing was stored.

    drain: the rest of the request may still be read and dropped, so the
    browser gets to show the answer. False when the request itself is
    broken or too large, where reading on would defeat the limit.
    """

    def __init__(self, message: str, status_code: int, *, drain: bool = True):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.drain = drain


class UploadTokenRejected(UploadRejected):
    """The upload did not carry this session's CSRF token before its file."""

    def __init__(self):
        super().__init__("csrf", 403)


def clean_filename(raw: str) -> str:
    """A name fit to show and to offer as the download name.

    No folders, no control or direction-changing characters (which can make
    "x.exe" look like "x.pdf"), no leading or trailing dots and spaces, at
    most MAX_FILENAME_LENGTH characters with the extension kept.
    """
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf"))
    name = " ".join(name.split()).strip(" .")

    if len(name) > MAX_FILENAME_LENGTH:
        stem, extension = os.path.splitext(name)
        name = stem[: MAX_FILENAME_LENGTH - len(extension)].rstrip(" .") + extension

    return name


def check_announced(raw_name: str, declared_type: str) -> tuple[str, FileType]:
    """Check what the upload says about its file, before any byte of it is kept."""
    name = clean_filename(raw_name)

    if not name:
        raise UploadRejected(NO_NAME, 422)

    kind = FILE_TYPES.get(os.path.splitext(name)[1].lower())

    if kind is None:
        raise UploadRejected(UNSUPPORTED, 415)

    if declared_type.split(";", 1)[0].strip().lower() not in kind.declared:
        raise UploadRejected(TYPE_MISMATCH, 415)

    return name, kind


def check_content(kind: FileType, head: bytes, size: int) -> None:
    """Check the received bytes: not empty, and starting the way the type requires."""
    if size == 0:
        raise UploadRejected(EMPTY, 422)

    if not head.startswith(kind.signature):
        raise UploadRejected(CONTENT_MISMATCH, 415)


@dataclass
class ReceivedFile:
    """A received and checked upload, waiting in its staging file."""

    upload_key: str
    filename: str
    file_type: FileType
    staging: StagingFile


class UploadOutcome(StrEnum):
    ADDED = "added"
    # The same form was sent again with the same file after it had been stored.
    ALREADY_ADDED = "already_added"
    # The same form was sent again while its first post is still being stored.
    IN_PROGRESS = "in_progress"
    # The form's key belongs to another file or to an upload that failed.
    FORM_USED = "form_used"
    PARENT_GONE = "parent_gone"
    FULL = "full"
    FAILED = "failed"


# --- reading ---------------------------------------------------------------------


def places_taken(db: Session, item_id: str) -> int:
    """Attachments that count against the limit: pending and ready ones, also those in the bin."""
    return db.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(Attachment.item_id == item_id, Attachment.state.in_(TAKES_A_PLACE))
    )


def kept_attachments(db: Session, owner_id: str, item: ExperienceItem) -> list[Attachment]:
    """The ready attachments of an experience that are not in the recycle bin, oldest first."""
    return list(
        db.scalars(
            select(Attachment)
            .where(
                Attachment.item_id == item.id,
                Attachment.owner_id == owner_id,
                Attachment.state == AttachmentState.READY,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.created_at, Attachment.id)
        )
    )


def binned_attachments(db: Session, owner_id: str, item: ExperienceItem, now: datetime) -> list[Attachment]:
    """Ready attachments of an experience in the recycle bin that can still be restored."""
    rows = db.scalars(
        select(Attachment)
        .where(
            Attachment.item_id == item.id,
            Attachment.owner_id == owner_id,
            Attachment.state == AttachmentState.READY,
            Attachment.deleted_at.is_not(None),
        )
        .order_by(Attachment.deleted_at.desc(), Attachment.id)
    )

    return [row for row in rows if not is_expired(row, now)]


def kept_attachment(db: Session, owner_id: str, item_id: str, attachment_id: str) -> Attachment:
    """A ready attachment, not in the bin, of an experience of this owner that is in use."""
    item = active_item(db, owner_id, item_id)
    attachment = db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.item_id == item.id,
            Attachment.owner_id == owner_id,
            Attachment.state == AttachmentState.READY,
            Attachment.deleted_at.is_(None),
        )
    )

    if attachment is None:
        raise NotFound()

    return attachment


def has_free_place(db: Session, owner_id: str, item_id: str) -> bool:
    """Whether this saved experience of the owner can take one more attachment."""
    item = active_item(db, owner_id, item_id)

    return places_taken(db, item.id) < MAX_ATTACHMENTS


# --- storing an upload -------------------------------------------------------------


def store_upload(
    database: Database,
    files: FileStore,
    owner_id: str,
    item_id: str,
    received: ReceivedFile,
    now: datetime,
) -> UploadOutcome:
    """Reserve, place and activate one received file (see the module docstring).

    The staging name is removed at the end (if the disk refuses, it goes
    later with the attachment's file); when the upload was added, its bytes
    stay under their kept name.
    """
    try:
        try:
            with database.transaction() as db:
                outcome, attachment = _reserve(db, owner_id, item_id, received, now)
        except NotFound:
            return UploadOutcome.PARENT_GONE
        except StorageError as error:
            logger.error("reserving an attachment failed (%s)", type(error.__cause__).__name__)
            return UploadOutcome.FAILED

        if attachment is None:
            return outcome

        try:
            files.place(received.staging, owner_id)
        except (OSError, ValueError, FileStoreError) as error:
            logger.error("placing an attachment file failed (%s)", type(error).__name__)
            abandon_upload(database, files, owner_id, attachment.id, attachment.storage_key, now)
            return UploadOutcome.FAILED

        try:
            with database.transaction() as db:
                outcome = _activate(db, owner_id, attachment.id, now)
        except StorageError as error:
            logger.error("activating an attachment failed (%s)", type(error.__cause__).__name__)
            outcome = UploadOutcome.FAILED

        if outcome != UploadOutcome.ADDED:
            abandon_upload(database, files, owner_id, attachment.id, attachment.storage_key, now)

        return outcome
    finally:
        received.staging.discard()


def _reserve(
    db: Session,
    owner_id: str,
    item_id: str,
    received: ReceivedFile,
    now: datetime,
) -> tuple[UploadOutcome | None, Attachment | None]:
    item = active_item(db, owner_id, item_id)
    staging = received.staging
    earlier = db.scalar(
        select(Attachment).where(
            Attachment.owner_id == owner_id,
            Attachment.upload_key == received.upload_key,
        )
    )

    if earlier is not None:
        same_file = (
            earlier.item_id == item.id
            and earlier.filename == received.filename
            and earlier.size == staging.size
            and earlier.sha256 == staging.sha256
        )

        if same_file and earlier.state == AttachmentState.READY:
            return UploadOutcome.ALREADY_ADDED, None

        if same_file and earlier.state == AttachmentState.PENDING:
            return UploadOutcome.IN_PROGRESS, None

        return UploadOutcome.FORM_USED, None

    if places_taken(db, item.id) >= MAX_ATTACHMENTS:
        return UploadOutcome.FULL, None

    attachment = Attachment(
        id=new_id(),
        owner_id=owner_id,
        item_id=item.id,
        # The staging file's key: the record names both of the file's names.
        storage_key=staging.key,
        filename=received.filename,
        media_type=received.file_type.media_type,
        size=staging.size,
        sha256=staging.sha256,
        state=AttachmentState.PENDING,
        upload_key=received.upload_key,
        revision=1,
        created_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(attachment)

    return None, attachment


def _activate(db: Session, owner_id: str, attachment_id: str, now: datetime) -> UploadOutcome:
    attachment = db.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.owner_id == owner_id,
            Attachment.state == AttachmentState.PENDING,
            Attachment.deleted_at.is_(None),
        )
    )

    if attachment is None:
        # Removed meanwhile, with its experience or by the clean-up pass.
        return UploadOutcome.PARENT_GONE

    try:
        item = active_item(db, owner_id, attachment.item_id)
    except NotFound:
        return UploadOutcome.PARENT_GONE

    others = db.scalar(
        select(func.count())
        .select_from(Attachment)
        .where(
            Attachment.item_id == item.id,
            Attachment.state.in_(TAKES_A_PLACE),
            Attachment.id != attachment.id,
        )
    )

    if others >= MAX_ATTACHMENTS:
        return UploadOutcome.FULL

    attachment.state = AttachmentState.READY
    attachment.updated_at = now
    record_event(db, owner_id, item.id, TargetType.ATTACHMENT, attachment.id, "attachment_added", now)

    return UploadOutcome.ADDED


def abandon_upload(
    database: Database,
    files: FileStore,
    owner_id: str,
    attachment_id: str,
    storage_key: str,
    now: datetime,
    *,
    stale_before: datetime | None = None,
) -> bool:
    """Undo an upload that did not become ready; return whether it is completely gone.

    The row is first marked failed, so no later step can make it ready; then
    the file is removed under both its names, and the row last. A ready row
    is never touched, and with stale_before only a pending row not touched
    since then is taken. Anything left over is found again by recover_uploads.
    """
    try:
        with database.transaction() as db:
            attachment = db.get(Attachment, attachment_id)

            if attachment is not None and attachment.state == AttachmentState.PENDING:
                if stale_before is not None and attachment.updated_at > stale_before:
                    return False

                attachment.state = AttachmentState.FAILED
                attachment.updated_at = now
            elif attachment is not None and attachment.state != AttachmentState.FAILED:
                return False

        files.remove_file(owner_id, storage_key)

        with database.transaction() as db:
            attachment = db.get(Attachment, attachment_id)

            if attachment is not None:
                db.delete(attachment)
    except (StorageError, FileStoreError, OSError, ValueError) as error:
        logger.warning("an unfinished upload is left for the clean-up pass (%s)", type(error).__name__)
        return False

    return True


def recover_uploads(database: Database, files: FileStore, now: datetime) -> int:
    """Remove what unfinished uploads left behind; return how many were removed.

    These are failed rows, and pending rows that nobody touched for
    STALE_UPLOAD_AFTER: an upload stays pending only while its own request
    places the file, which takes a moment, so such a row has no request left.
    """
    stale_before = now - STALE_UPLOAD_AFTER

    with database.transaction() as db:
        leftovers = [
            (row.id, row.owner_id, row.storage_key)
            for row in db.scalars(
                select(Attachment).where(
                    or_(
                        Attachment.state == AttachmentState.FAILED,
                        and_(
                            Attachment.state == AttachmentState.PENDING,
                            Attachment.updated_at <= stale_before,
                        ),
                    )
                )
            )
        ]

    return sum(
        abandon_upload(database, files, owner_id, attachment_id, key, now, stale_before=stale_before)
        for attachment_id, owner_id, key in leftovers
    )


def referenced_files(database: Database) -> set[tuple[str, str]]:
    """Every (owner id, storage key) an attachment row refers to, whatever its state."""
    with database.transaction() as db:
        return {
            (owner_id, key)
            for owner_id, key in db.execute(select(Attachment.owner_id, Attachment.storage_key))
        }
