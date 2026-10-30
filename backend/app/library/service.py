"""Saving, confirming and reading records, always within one owner.

Every function takes the owner id from its caller (the web layer passes the
id from the server-side session) and filters every query by it, so another
user's ids are simply not found. A change to an existing record must name
the revision the page was showing. If the stored revision has moved on,
nothing is written and Conflict is raised. The revision goes up with every
successful change: saving, confirming, deleting and restoring.

Lookups also name the kinds of record they accept. Experience pages accept
only the six experience categories, so the profile and the preferences,
which share the same tables, never show up as experiences.
"""
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import (
    CATEGORY_FIELDS,
    DOCUMENT_KINDS,
    EXPERIENCE_CATEGORIES,
    ExperienceInput,
)
from app.library.errors import Conflict, NotFound
from app.library.models import (
    AuditEvent,
    ExperienceItem,
    ExperienceVersion,
    ItemState,
    VersionKind,
)


def new_id() -> str:
    return str(uuid.uuid4())


def is_expired(row, now: datetime) -> bool:
    """True once a deleted object has passed its restore deadline."""
    return row.deleted_at is not None and now >= row.purge_after


def record_event(
    db: Session,
    owner_id: str,
    item_id: str,
    object_type: str,
    object_id: str,
    action: str,
    now: datetime,
) -> None:
    db.add(
        AuditEvent(
            owner_id=owner_id,
            item_id=item_id,
            object_type=object_type,
            object_id=object_id,
            action=action,
            at=now,
        )
    )


def active_item(
    db: Session,
    owner_id: str,
    item_id: str,
    kinds: tuple[str, ...] = EXPERIENCE_CATEGORIES,
) -> ExperienceItem:
    item = db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.id == item_id,
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(kinds),
            ExperienceItem.deleted_at.is_(None),
        )
    )

    if item is None:
        raise NotFound()

    return item


@dataclass(frozen=True)
class ItemSnapshot:
    """A plain copy of an item that stays readable after its transaction rolls back."""

    id: str
    category: str
    revision: int
    state: str
    content: dict
    private_note: str
    deleted_at: datetime | None

    @classmethod
    def of(cls, item: ExperienceItem) -> "ItemSnapshot":
        return cls(
            id=item.id,
            category=item.category,
            revision=item.revision,
            state=item.state,
            content=dict(item.content),
            private_note=item.private_note,
            deleted_at=item.deleted_at,
        )


def check_revision(item: ExperienceItem, expected_revision: int) -> None:
    if item.revision != expected_revision:
        # The raise rolls the transaction back, which expires the ORM object;
        # the conflict page needs a copy it can still read.
        raise Conflict(ItemSnapshot.of(item))


def create_experience(
    db: Session,
    owner_id: str,
    entry: ExperienceInput,
    now: datetime,
) -> ExperienceItem:
    if entry.category not in EXPERIENCE_CATEGORIES:
        raise ValueError(f"not an experience category: {entry.category!r}")

    item = ExperienceItem(
        id=new_id(),
        owner_id=owner_id,
        category=entry.category,
        state=ItemState.DRAFT,
        revision=1,
        content=dict(entry.content),
        private_note=entry.private_note,
        confirmed_version_id=None,
        confirmation_count=0,
        last_confirmed_at=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(item)
    db.flush()
    db.add(
        ExperienceVersion(
            id=new_id(),
            item_id=item.id,
            owner_id=owner_id,
            kind=VersionKind.INITIAL,
            number=1,
            content=dict(entry.content),
            private_note=entry.private_note,
            source_revision=1,
            created_at=now,
            deleted_at=None,
            purge_after=None,
            deletion_id=None,
        )
    )
    record_event(db, owner_id, item.id, "experience", item.id, "created", now)

    return item


def current_document(db: Session, owner_id: str, kind: str) -> ExperienceItem | None:
    """The owner's profile or preferences outside the recycle bin, if any."""
    return db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category == kind,
            ExperienceItem.deleted_at.is_(None),
        )
    )


def create_document(
    db: Session,
    owner_id: str,
    entry: ExperienceInput,
    now: datetime,
) -> ExperienceItem:
    """Start the owner's profile or preferences, which has no private note.

    There is one of each at a time: if another page created it meanwhile,
    nothing is written and Conflict shows the one that exists.
    """
    if entry.category not in DOCUMENT_KINDS:
        raise ValueError(f"not a document kind: {entry.category!r}")

    existing = current_document(db, owner_id, entry.category)

    if existing is not None:
        raise Conflict(ItemSnapshot.of(existing))

    item = ExperienceItem(
        id=new_id(),
        owner_id=owner_id,
        category=entry.category,
        state=ItemState.DRAFT,
        revision=1,
        content=dict(entry.content),
        private_note="",
        confirmed_version_id=None,
        confirmation_count=0,
        last_confirmed_at=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(item)
    db.flush()
    _add_saved_version(db, item, now)
    record_event(db, owner_id, item.id, "experience", item.id, "created", now)

    return item


def _add_saved_version(db: Session, item: ExperienceItem, now: datetime) -> None:
    """Keep this save of the profile or preferences as a numbered version."""
    item.confirmation_count += 1
    db.add(
        ExperienceVersion(
            id=new_id(),
            item_id=item.id,
            owner_id=item.owner_id,
            kind=VersionKind.SAVED,
            number=item.confirmation_count,
            content=dict(item.content),
            private_note="",
            source_revision=item.revision,
            created_at=now,
            deleted_at=None,
            purge_after=None,
            deletion_id=None,
        )
    )


def save_draft(
    db: Session,
    owner_id: str,
    item_id: str,
    expected_revision: int,
    entry: ExperienceInput,
    now: datetime,
) -> tuple[ExperienceItem, bool]:
    """Replace the current content; return the item and whether anything changed.

    The item must be of the entry's kind. Any saved change makes an
    experience a draft again: an earlier confirmation stays in the history
    but no longer stands for current facts. A changed profile or preferences
    keeps this save as a version.
    """
    item = active_item(db, owner_id, item_id, kinds=(entry.category,))
    check_revision(item, expected_revision)

    if item.content == entry.content and item.private_note == entry.private_note:
        return item, False

    item.content = dict(entry.content)
    item.private_note = entry.private_note
    item.state = ItemState.DRAFT
    item.confirmed_version_id = None
    item.revision += 1
    item.updated_at = now

    if item.category in DOCUMENT_KINDS:
        _add_saved_version(db, item, now)

    record_event(db, owner_id, item.id, "experience", item.id, "saved", now)

    return item, True


def confirm_experience(
    db: Session,
    owner_id: str,
    item_id: str,
    expected_revision: int,
    now: datetime,
) -> ExperienceItem:
    """Record the content the page showed as a fixed, confirmed version."""
    item = active_item(db, owner_id, item_id)
    check_revision(item, expected_revision)

    if item.state == ItemState.CONFIRMED:
        return item

    version = ExperienceVersion(
        id=new_id(),
        item_id=item.id,
        owner_id=owner_id,
        kind=VersionKind.CONFIRMED,
        number=item.confirmation_count + 1,
        content=dict(item.content),
        private_note=item.private_note,
        source_revision=item.revision,
        created_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(version)
    item.state = ItemState.CONFIRMED
    item.confirmed_version_id = version.id
    item.confirmation_count += 1
    item.last_confirmed_at = now
    item.revision += 1
    record_event(db, owner_id, item.id, "experience_version", version.id, "confirmed", now)

    return item


def list_experiences(
    db: Session,
    owner_id: str,
    *,
    category: str | None = None,
    query: str = "",
) -> list[ExperienceItem]:
    """The owner's current experiences, newest save first.

    category narrows them to one category. query keeps those whose current
    fields contain every word of it, ignoring case and full-width forms.
    The private note, history versions and the recycle bin are never
    searched.
    """
    categories = (category,) if category in EXPERIENCE_CATEGORIES else EXPERIENCE_CATEGORIES
    statement = (
        select(ExperienceItem)
        .where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(categories),
            ExperienceItem.deleted_at.is_(None),
        )
        .order_by(ExperienceItem.updated_at.desc(), ExperienceItem.id)
    )
    items = list(db.scalars(statement))
    words = _searchable(query).split()

    if not words:
        return items

    return [item for item in items if all(word in _item_text(item) for word in words)]


def _searchable(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _item_text(item: ExperienceItem) -> str:
    return _searchable(
        "\n".join(item.content.get(field.name, "") for field in CATEGORY_FIELDS[item.category])
    )


def item_versions(
    db: Session,
    owner_id: str,
    item: ExperienceItem,
    now: datetime,
) -> list[ExperienceVersion]:
    """Kept versions plus versions still restorable from the bin, newest first."""
    statement = (
        select(ExperienceVersion)
        .where(
            ExperienceVersion.item_id == item.id,
            ExperienceVersion.owner_id == owner_id,
        )
        .order_by(ExperienceVersion.created_at.desc(), ExperienceVersion.id)
    )

    return [version for version in db.scalars(statement) if not is_expired(version, now)]


@dataclass
class History:
    item: ExperienceItem
    versions: list[ExperienceVersion]
    events: list[AuditEvent]


def experience_history(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
    kinds: tuple[str, ...] = EXPERIENCE_CATEGORIES,
) -> History:
    item = active_item(db, owner_id, item_id, kinds)
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.item_id == item.id, AuditEvent.owner_id == owner_id)
        .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
    )

    return History(
        item=item,
        versions=item_versions(db, owner_id, item, now),
        events=list(events),
    )


def get_version(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str,
    kinds: tuple[str, ...] = EXPERIENCE_CATEGORIES,
) -> tuple[ExperienceItem, ExperienceVersion]:
    """A kept (not deleted) version of an active record of one of these kinds."""
    item = active_item(db, owner_id, item_id, kinds)
    version = db.scalar(
        select(ExperienceVersion).where(
            ExperienceVersion.id == version_id,
            ExperienceVersion.item_id == item.id,
            ExperienceVersion.owner_id == owner_id,
            ExperienceVersion.deleted_at.is_(None),
        )
    )

    if version is None:
        raise NotFound()

    return item, version


def is_current_confirmed(item: ExperienceItem, version: ExperienceVersion) -> bool:
    return item.state == ItemState.CONFIRMED and item.confirmed_version_id == version.id
