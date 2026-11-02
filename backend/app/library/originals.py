"""Keeping a resume import's original file as an attachment of an experience (U2-B3b).

The user picks one of their own experiences in use, a draft or a confirmed
one; never the profile, the preferences, one in the recycle bin or someone
else's, and nothing is picked for them. The whole original is copied as it
is: it may hold contact details and other experiences, it stays private,
is sent nowhere and proves nothing. From then on the copy is an ordinary
attachment (attachments.py, lifecycle.py): ending, expiring or deleting the
import never touches it, and deleting it never touches the import or the
experience. A PDF stays a PDF; a Word file becomes an attachment only this
way (attachments.KEPT_DOCX), never through the upload form. Pasted text
has no original, and none is made up for it.

The copy goes through the upload's states (attachments.store_upload) with
the import's file as its source:

1. The source is looked at: in use (a temporary import in its seven days,
   or a saved draft), with a file; the experience is the owner's and in use.
2. The file is copied into a staging file, checked as it is read: the size
   and checksum the import recorded, its type's first bytes, the size limit.
3. One transaction checks source and experience again and reserves a
   pending attachment (attachments._reserve: the form's one-time key, a
   free place).
4. The staging file gets its kept name.
5. One transaction checks the source once more and makes the attachment
   ready (attachments._activate checks the experience and its places).

Each check reads the time once it holds the write lock. An attachment is
made ready only from a source still in use, unchanged, to an experience
still in use; anything else is undone (attachments.abandon_upload) and
leaves no row or file that a page shows. The import itself is never
changed here: whatever happens, its original stays where it was.

Sending the same form again finds the same attachment (its one-time key);
an experience that already has this file, ready or pending, also in its
recycle bin, gets no second copy.

A database row is not a file (Codex U2B3B-F1): whether a copy can be relied
on is found by reading its file now, with the size and checksum it was kept
with (copies_of). Only a copy in use whose file passes is "kept"; a missing,
changed or unreadable one is said to be so, never deleted or overwritten
here, and does not stand in the way of a new copy. The finish page binds its
form to the copies it called kept (keeping_token); an ending checks them
again, and in its own transaction that nothing in use changed since
(unchanged_since), so an older page never clears the last original on the
strength of a copy that is gone (Codex U2B3B-F2).
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.attachments import (
    KEPT_DOCX,
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    PDF,
    TAKES_A_PLACE,
    FileType,
    ReceivedFile,
    UploadOutcome,
    _activate,
    _reserve,
    abandon_upload,
    places_taken,
)
from app.library.content import EXPERIENCE_CATEGORIES
from app.library.db import Database
from app.library.errors import ChangedFile, FileStoreError, MissingFile, NotFound, StorageError
from app.library.files import CHUNK_BYTES, IMPORTS, FileStore
from app.library.imports import _locked, is_open
from app.library.models import Attachment, AttachmentState, ExperienceItem, ImportAdoption, ImportSource, ResumeImport
from app.library.service import active_item, is_expired

logger = logging.getLogger("roleradar.library")

KEPT_TYPES = {ImportSource.PDF: PDF, ImportSource.DOCX: KEPT_DOCX}


class KeptFile(StrEnum):
    ADDED = "added"
    # This form was sent before, and that request kept the file.
    ALREADY_ADDED = "already_added"
    # This form was sent before, and that request is still keeping it.
    IN_PROGRESS = "in_progress"
    # The experience already has this file (ready or pending, also in its recycle bin).
    ALREADY_THERE = "already_there"
    # The form's one-time key belongs to another request.
    FORM_USED = "form_used"
    FULL = "full"
    # The experience is not the owner's, not in use, or not an experience.
    TARGET_GONE = "target_gone"
    # The import ended, expired or was deleted meanwhile.
    SOURCE_GONE = "source_gone"
    # The import's file is missing, unreadable or not the one that was read.
    SOURCE_CHANGED = "source_changed"
    # This form was sent before and kept the file, but that attachment's file cannot be used now.
    EARLIER_UNUSABLE = "earlier_unusable"
    FAILED = "failed"


# What the upload's own steps answer, in these terms.
FROM_UPLOAD = {
    UploadOutcome.ADDED: KeptFile.ADDED,
    UploadOutcome.ALREADY_ADDED: KeptFile.ALREADY_ADDED,
    UploadOutcome.IN_PROGRESS: KeptFile.IN_PROGRESS,
    UploadOutcome.FORM_USED: KeptFile.FORM_USED,
    UploadOutcome.PARENT_GONE: KeptFile.TARGET_GONE,
    UploadOutcome.FULL: KeptFile.FULL,
    UploadOutcome.FAILED: KeptFile.FAILED,
}


class NoOriginal(Exception):
    """The import has no original file to keep (pasted text)."""


@dataclass(frozen=True)
class Original:
    """An import's file as the copy starts: what every later check compares with."""

    key: str
    filename: str
    size: int
    sha256: str
    file_type: FileType


def original_of(row: ResumeImport) -> Original | None:
    """The import's original file, or None for pasted text (or a file already gone)."""
    if row.storage_key is None or row.source not in KEPT_TYPES:
        return None

    return Original(row.storage_key, row.filename, row.size, row.sha256, KEPT_TYPES[row.source])


def _same(row: ResumeImport | None, original: Original) -> bool:
    return row is not None and row.storage_key == original.key and row.sha256 == original.sha256


def adopted_into(db: Session, owner_id: str, import_id: str) -> set[str]:
    """The records this import's candidates were written into."""
    return set(
        db.scalars(
            select(ImportAdoption.item_id).where(
                ImportAdoption.owner_id == owner_id, ImportAdoption.import_id == import_id
            )
        )
    )


def keep_targets(db: Session, owner_id: str, import_id: str) -> list[ExperienceItem]:
    """The owner's experiences in use, the ones this import's candidates went into first, then the newest."""
    adopted = adopted_into(db, owner_id, import_id)
    items = list(
        db.scalars(
            select(ExperienceItem)
            .where(
                ExperienceItem.owner_id == owner_id,
                ExperienceItem.category.in_(EXPERIENCE_CATEGORIES),
                ExperienceItem.deleted_at.is_(None),
            )
            .order_by(ExperienceItem.updated_at.desc(), ExperienceItem.id)
        )
    )

    return sorted(items, key=lambda item: item.id not in adopted)


class Place(StrEnum):
    # Ready, and it and its experience are in use.
    IN_USE = "in_use"
    # Ready, but it or its experience is in the recycle bin.
    BINNED = "binned"
    # A request is still keeping it.
    PENDING = "pending"


class Health(StrEnum):
    # The file is there, with the size and checksum it was kept with.
    USABLE = "usable"
    MISSING = "missing"
    CHANGED = "changed"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Copy:
    """One attachment holding the import's bytes: where it is, and whether its file can be used now."""

    attachment: Attachment
    item: ExperienceItem
    place: Place
    # None while pending: there is no file to check yet.
    health: Health | None

    @property
    def kept(self) -> bool:
        """Whether a page may call it a kept original: in use, and its file checked now."""
        return self.place == Place.IN_USE and self.health == Health.USABLE

    @property
    def blocks(self) -> bool:
        """Whether it stands in the way of a second copy in its experience: pending, or with a usable file."""
        return self.place == Place.PENDING or self.health == Health.USABLE


class KeepingChanged(Exception):
    """The copies in use changed since they were checked for a finish page's form; nothing was ended."""


def _same_file_rows(
    db: Session, owner_id: str, original: Original, now: datetime, item_id: str | None = None
) -> list[tuple[Attachment, ExperienceItem]]:
    """The owner's attachments with these bytes that take a place, with their experiences; none past a deadline."""
    query = (
        select(Attachment, ExperienceItem)
        .join(ExperienceItem, ExperienceItem.id == Attachment.item_id)
        .where(
            Attachment.owner_id == owner_id,
            Attachment.sha256 == original.sha256,
            Attachment.size == original.size,
            Attachment.state.in_(TAKES_A_PLACE),
        )
        .order_by(Attachment.created_at, Attachment.id)
    )

    if item_id is not None:
        query = query.where(Attachment.item_id == item_id)

    return [
        (attachment, item)
        for attachment, item in db.execute(query)
        if not is_expired(attachment, now) and not is_expired(item, now)
    ]


def _health(files: FileStore, attachment: Attachment) -> Health:
    """Read the attachment's file now, against the size and checksum it was kept with."""
    try:
        with files.open_checked(attachment.owner_id, attachment.storage_key, attachment.size, attachment.sha256):
            return Health.USABLE
    except MissingFile:
        return Health.MISSING
    except ChangedFile:
        return Health.CHANGED
    except (FileStoreError, OSError, ValueError):
        return Health.UNREADABLE


def _copy(files: FileStore, attachment: Attachment, item: ExperienceItem) -> Copy:
    if attachment.state == AttachmentState.PENDING:
        return Copy(attachment, item, Place.PENDING, None)

    binned = attachment.deleted_at is not None or item.deleted_at is not None

    return Copy(attachment, item, Place.BINNED if binned else Place.IN_USE, _health(files, attachment))


def copies_of(
    database: Database, files: FileStore, owner_id: str, original: Original | None, clock: Callable[[], datetime]
) -> list[Copy]:
    """Every attachment holding the import's bytes, each file read now; no lock is held while they are read."""
    if original is None:
        return []

    with database.transaction() as db:
        rows = _same_file_rows(db, owner_id, original, clock())

    return [_copy(files, attachment, item) for attachment, item in rows]


def keeping_token(copies: list[Copy]) -> str:
    """What a finish page called kept, for its form: the attachments, or "none"."""
    return ",".join(sorted(copy.attachment.id for copy in copies if copy.kept)) or "none"


def unchanged_since(
    owner_id: str, original: Original, copies: list[Copy], clock: Callable[[], datetime]
) -> Callable[[Session], None]:
    """A check for the transaction that ends the import: the copies in use are the ones just checked.

    It runs under that transaction's write lock, so a delete, a purge or a
    new copy made by any other request either came before (and is found)
    or comes after the import has ended.
    """
    seen = {copy.attachment.id for copy in copies if copy.place == Place.IN_USE}

    def check(db: Session) -> None:
        current = {
            attachment.id
            for attachment, item in _same_file_rows(db, owner_id, original, clock())
            if attachment.state == AttachmentState.READY and attachment.deleted_at is None and item.deleted_at is None
        }

        if current != seen:
            raise KeepingChanged()

    return check


def full_targets(db: Session, items: list[ExperienceItem]) -> set[str]:
    """The experiences among these that have no free attachment place."""
    return {item.id for item in items if places_taken(db, item.id) >= MAX_ATTACHMENTS}


def _looked_at(
    database: Database,
    files: FileStore,
    owner_id: str,
    import_id: str,
    item_id: str,
    upload_key: str,
    clock: Callable[[], datetime],
) -> tuple[Original | None, frozenset[str], KeptFile | None]:
    """Step 1: what to copy and the experience's same-file attachments found unusable; or the answer.

    The files are read after the transaction, so no lock is held meanwhile.
    """
    with database.transaction() as db:
        row, now = _locked(db, owner_id, import_id, clock)

        if row is None or not is_open(db, row, now):
            raise NotFound()

        original = original_of(row)

        if original is None:
            raise NoOriginal()

        try:
            item = active_item(db, owner_id, item_id)
        except NotFound:
            return None, frozenset(), KeptFile.TARGET_GONE

        earlier = db.scalar(
            select(Attachment).where(Attachment.owner_id == owner_id, Attachment.upload_key == upload_key)
        )
        same = _same_file_rows(db, owner_id, original, now, item.id)
        full = places_taken(db, item.id) >= MAX_ATTACHMENTS

    if earlier is not None:
        if earlier.item_id == item_id and earlier.sha256 == original.sha256:
            if earlier.state == AttachmentState.READY:
                # Said to be done only while its file is there as it was kept.
                usable = _health(files, earlier) == Health.USABLE

                return None, frozenset(), KeptFile.ALREADY_ADDED if usable else KeptFile.EARLIER_UNUSABLE

            if earlier.state == AttachmentState.PENDING:
                return None, frozenset(), KeptFile.IN_PROGRESS

        return None, frozenset(), KeptFile.FORM_USED

    copies = [_copy(files, attachment, kept_in) for attachment, kept_in in same]

    if any(copy.blocks for copy in copies):
        return None, frozenset(), KeptFile.ALREADY_THERE

    if full:
        return None, frozenset(), KeptFile.FULL

    # What is left is unusable: no reason to refuse a copy that can be used, and never removed here.
    return original, frozenset(copy.attachment.id for copy in copies), None


def keep_original(
    database: Database,
    files: FileStore,
    owner_id: str,
    import_id: str,
    item_id: str,
    upload_key: str,
    clock: Callable[[], datetime],
) -> KeptFile:
    """Copy an import's original into a new attachment of the chosen experience; see the module docstring.

    NotFound when the import is not the owner's or not in use, NoOriginal
    when it has no file. Every other outcome is the answer; only ADDED
    (and ALREADY_ADDED, for a repeated form) means an attachment is there.
    """
    original, unusable, answer = _looked_at(database, files, owner_id, import_id, item_id, upload_key, clock)

    if answer is not None:
        return answer

    staging = files.new_staging()

    try:
        try:
            with files.open_checked(owner_id, original.key, original.size, original.sha256, area=IMPORTS) as source:
                while chunk := source.read(CHUNK_BYTES):
                    staging.write(chunk)

            staging.finish()
        except (FileStoreError, OSError, ValueError) as error:
            logger.warning("an import's original could not be copied (%s)", type(error).__name__)
            return KeptFile.SOURCE_CHANGED

        # Checked again as copied: not trusted because the import once checked it.
        if (
            staging.size != original.size
            or staging.sha256 != original.sha256
            or staging.size > MAX_FILE_BYTES
            or not staging.head.startswith(original.file_type.signature)
        ):
            return KeptFile.SOURCE_CHANGED

        received = ReceivedFile(upload_key, original.filename, original.file_type, staging)

        try:
            with database.transaction() as db:
                row, now = _locked(db, owner_id, import_id, clock)

                if not _same(row, original) or not is_open(db, row, now):
                    return KeptFile.SOURCE_GONE

                # Anything holding the file now that was not found unusable above stands in the way.
                holding = _same_file_rows(db, owner_id, original, now, item_id)

                if any(attachment.id not in unusable for attachment, _ in holding):
                    return KeptFile.ALREADY_THERE

                outcome, attachment = _reserve(db, owner_id, item_id, received, now)
        except NotFound:
            return KeptFile.TARGET_GONE
        except StorageError as error:
            logger.error("reserving a kept original failed (%s)", type(error.__cause__).__name__)
            return KeptFile.FAILED

        if attachment is None:
            return FROM_UPLOAD[outcome]

        try:
            files.place(staging, owner_id)
        except (OSError, ValueError, FileStoreError) as error:
            logger.error("placing a kept original failed (%s)", type(error).__name__)
            abandon_upload(database, files, owner_id, attachment.id, attachment.storage_key, clock())
            return KeptFile.FAILED

        try:
            with database.transaction() as db:
                row, now = _locked(db, owner_id, import_id, clock)

                if not _same(row, original) or not is_open(db, row, now):
                    answer = KeptFile.SOURCE_GONE
                else:
                    answer = FROM_UPLOAD[_activate(db, owner_id, attachment.id, now)]
        except StorageError as error:
            logger.error("making a kept original ready failed (%s)", type(error.__cause__).__name__)
            answer = KeptFile.FAILED

        if answer != KeptFile.ADDED:
            abandon_upload(database, files, owner_id, attachment.id, attachment.storage_key, clock())

        return answer
    finally:
        staging.discard()

