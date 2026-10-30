"""Read-only views of the library for the modules that come after it (v0.1 U1-D).

Nothing here writes, calls anything outside the library, or decides what may
be sent anywhere.

project_confirmed_library copies the owner's confirmed experiences as they
were confirmed: what a later module may take as confirmed material at this
moment. It is a copy for one use, not a profile, a stored record or a
permission. Before U2 uses any of it for a task, the user has to choose what
that task may use, and each entry has to be checked again: still the user's,
still the confirmed version, not deleted. An old snapshot never keeps a
record in use that the user has since changed or deleted. Free text in it
can still hold personal details the user wrote; it is not filtered for
sending anywhere.

list_references says what the library knows about other modules using a
record. No other module keeps references yet, so the answer says exactly
that, never "no references".
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import CATEGORY_FIELDS, EXPERIENCE_CATEGORIES
from app.library.errors import InconsistentRecord, NotFound
from app.library.models import ExperienceItem, ExperienceVersion, ItemState, VersionKind
from app.library.service import is_expired, new_id

NOT_ESTABLISHED = "not_established"
# The modules that will use records once they exist: analysis (U2) and résumés (U4).
DOWNSTREAM_MODULES = ("analysis", "resume")


@dataclass(frozen=True)
class ConfirmedExperience:
    """One experience as it was confirmed.

    content holds the fields of its category that were filled in, as the
    confirmed version has them; a field that is missing was not written yet,
    never "the user lacks this". The private note is never part of it.
    """

    item_id: str
    category: str
    version_id: str
    version_number: int
    confirmed_at: datetime
    # The revision of the experience whose content was confirmed.
    source_revision: int
    content: dict[str, str]


@dataclass(frozen=True)
class ConfirmedLibrarySnapshot:
    snapshot_id: str
    owner_id: str
    generated_at: datetime
    experiences: tuple[ConfirmedExperience, ...]


def project_confirmed_library(db: Session, owner_id: str, now: datetime) -> ConfirmedLibrarySnapshot:
    """The owner's confirmed experiences, read in db's one transaction.

    Only experiences in use (not in the recycle bin) whose state is confirmed
    now are taken, each with the content of the version its confirmation
    points to: never a later draft, never an earlier confirmation. One saved
    again after its confirmation is a draft, and is left out until it is
    confirmed again. A confirmation that points nowhere, or to anything but
    a kept, confirmed version of the same experience and owner, raises
    InconsistentRecord rather than being guessed at.

    now is the clock's time of this reading (generated_at); it is not a way
    to read the library as it was at another time. Entries come in category
    order, then by confirmation time. The profile, the preferences and
    attachments are not part of it.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    items = db.scalars(
        select(ExperienceItem).where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(EXPERIENCE_CATEGORIES),
            ExperienceItem.deleted_at.is_(None),
        )
    )
    confirmed = [item for item in items if _is_confirmed(item)]
    versions = {}

    if confirmed:
        versions = {
            version.id: version
            for version in db.scalars(
                select(ExperienceVersion).where(
                    ExperienceVersion.owner_id == owner_id,
                    ExperienceVersion.id.in_([item.confirmed_version_id for item in confirmed]),
                )
            )
        }

    experiences = sorted(
        (_as_confirmed(item, versions.get(item.confirmed_version_id)) for item in confirmed),
        key=lambda entry: (EXPERIENCE_CATEGORIES.index(entry.category), entry.confirmed_at, entry.item_id),
    )

    return ConfirmedLibrarySnapshot(
        snapshot_id=new_id(),
        owner_id=owner_id,
        generated_at=now,
        experiences=tuple(experiences),
    )


def _is_confirmed(item: ExperienceItem) -> bool:
    if item.state == ItemState.CONFIRMED and item.confirmed_version_id:
        return True

    if item.state == ItemState.DRAFT and item.confirmed_version_id is None:
        return False

    raise InconsistentRecord(item.id)


def _as_confirmed(item: ExperienceItem, version: ExperienceVersion | None) -> ConfirmedExperience:
    if (
        version is None
        or version.item_id != item.id
        or version.kind != VersionKind.CONFIRMED
        or version.deleted_at is not None
        or not isinstance(version.content, dict)
    ):
        raise InconsistentRecord(item.id)

    content = {}

    for field in CATEGORY_FIELDS[item.category]:
        value = version.content.get(field.name, "")

        if not isinstance(value, str):
            raise InconsistentRecord(item.id)

        if value:
            content[field.name] = value

    return ConfirmedExperience(
        item_id=item.id,
        category=item.category,
        version_id=version.id,
        version_number=version.number,
        confirmed_at=version.created_at,
        source_revision=version.source_revision,
        content=content,
    )


@dataclass(frozen=True)
class ReferenceStatus:
    """What the library knows about other modules using a record.

    NOT_ESTABLISHED: the modules keep no references to the library yet, so
    it cannot tell whether they used the record. That is not the same as
    knowing that they did not.
    """

    status: str
    modules: tuple[str, ...]


def list_references(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
    kinds: tuple[str, ...] = EXPERIENCE_CATEGORIES,
) -> ReferenceStatus:
    """The reference status of one of the owner's records, in use or still restorable.

    A record of another owner or kind, or past its restore deadline, raises
    NotFound like every other lookup: no status is given without the record.
    """
    item = db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.id == item_id,
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(kinds),
        )
    )

    if item is None or is_expired(item, now):
        raise NotFound()

    return ReferenceStatus(status=NOT_ESTABLISHED, modules=DOWNSTREAM_MODULES)
