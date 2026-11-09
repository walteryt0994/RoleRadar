"""How what a direction record rests on stands now, and which records rest on what (U3-C1).

A saved direction exploration or a direction decision names the records it rests on (DirectionSource): an
experience or the job preferences, the version used and the fields it was used for. Nothing there is ever
updated when the records change, and no saved exploration is ever rewritten: how each source stands is
worked out here, from the library as it is now.

- CURRENT: the record is in use and the fields used read now as they did then.
- CHANGED: in use, but a field used reads differently now; an experience changed and waiting to be
  confirmed again counts as changed too. Changes to fields that were not used do not count.
- VERSION_IN_BIN: the version used was deleted on its own and can still be restored.
- RECORD_IN_BIN: the record is in the recycle bin and can still be restored.
- GONE: the record or the version used is past its restore deadline or purged; or it never was this
  owner's, of this kind, or a version of the kind a task uses (a confirmed one of an experience, a saved
  one of the preferences).

Something resting on several sources stands as the worst of them, and on none as current. Only what is
current may be used for anything new: restoring one source does not bring a record back into use while
another still keeps it out. A decision the user made their own choice stands as personal whatever it
rested on before, and one made on a saved exploration also stands as that exploration does.

Every lookup is filtered by the owner, so another user's ids are simply not found.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import EXPERIENCE_CATEGORIES, PREFERENCES
from app.library.models import (
    DecisionBasis,
    DecisionState,
    DirectionDecision,
    DirectionExploration,
    DirectionSource,
    ExperienceItem,
    ExperienceVersion,
    ItemState,
    SourceHolder,
    SourceKind,
    VersionKind,
)
from app.library.service import is_expired


class SourceState(StrEnum):
    CURRENT = "current"
    CHANGED = "changed"
    VERSION_IN_BIN = "version_in_bin"
    RECORD_IN_BIN = "record_in_bin"
    GONE = "gone"


class BasisState(StrEnum):
    """How a decision's basis stands: a SourceState, the user's own choice, or the state of its exploration."""

    PERSONAL = "personal"
    CURRENT = "current"
    CHANGED = "changed"
    VERSION_IN_BIN = "version_in_bin"
    RECORD_IN_BIN = "record_in_bin"
    GONE = "gone"
    EXPLORATION_IN_BIN = "exploration_in_bin"
    EXPLORATION_GONE = "exploration_gone"


# Worst last.
SEVERITY = (SourceState.CURRENT, SourceState.CHANGED, SourceState.VERSION_IN_BIN, SourceState.RECORD_IN_BIN, SourceState.GONE)
# The kinds of record and of version each kind of source may name.
CATEGORIES = {SourceKind.EXPERIENCE: EXPERIENCE_CATEGORIES, SourceKind.PREFERENCES: (PREFERENCES,)}
VERSION_KINDS = {SourceKind.EXPERIENCE: VersionKind.CONFIRMED, SourceKind.PREFERENCES: VersionKind.SAVED}


def state_of(
    db: Session,
    owner_id: str,
    kind: str,
    item_id: str,
    version_id: str,
    fields,
    now: datetime,
) -> SourceState:
    """How one source stands now: the record and version it names, compared on the fields it was used for."""
    if kind not in CATEGORIES:
        return SourceState.GONE

    item = db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.id == item_id,
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category.in_(CATEGORIES[kind]),
        )
    )

    if item is None or is_expired(item, now):
        return SourceState.GONE

    if item.deleted_at is not None:
        return SourceState.RECORD_IN_BIN

    version = db.scalar(
        select(ExperienceVersion).where(
            ExperienceVersion.id == version_id,
            ExperienceVersion.item_id == item.id,
            ExperienceVersion.owner_id == owner_id,
            ExperienceVersion.kind == VERSION_KINDS[kind],
        )
    )

    if version is None or is_expired(version, now):
        return SourceState.GONE

    if version.deleted_at is not None:
        return SourceState.VERSION_IN_BIN

    if kind == SourceKind.PREFERENCES:
        # The preferences read now as their latest save: what the fields say now is the record's content.
        now_content = item.content if isinstance(item.content, dict) else {}
    elif item.state != ItemState.CONFIRMED or item.confirmed_version_id is None:
        # Changed since its confirmation and not confirmed again: nothing stands for its facts now.
        return SourceState.CHANGED
    elif item.confirmed_version_id == version.id:
        return SourceState.CURRENT
    else:
        confirmed = db.scalar(
            select(ExperienceVersion).where(
                ExperienceVersion.id == item.confirmed_version_id,
                ExperienceVersion.owner_id == owner_id,
            )
        )

        if confirmed is None:
            return SourceState.CHANGED

        now_content = confirmed.content if isinstance(confirmed.content, dict) else {}

    used = version.content if isinstance(version.content, dict) else {}

    if all(_field(used, name) == _field(now_content, name) for name in fields):
        return SourceState.CURRENT

    return SourceState.CHANGED


def _field(content: dict, name: str) -> str:
    value = content.get(name, "")

    return value if isinstance(value, str) else ""


def source_state(db: Session, owner_id: str, row: DirectionSource, now: datetime) -> SourceState:
    return state_of(db, owner_id, row.kind, row.item_id, row.version_id, row.fields or [], now)


def worst(states) -> SourceState:
    """The worst of these states; CURRENT for none."""
    return max(states, key=SEVERITY.index, default=SourceState.CURRENT)


def sources_of(db: Session, owner_id: str, holder_type: str, holder_id: str) -> list[DirectionSource]:
    return list(
        db.scalars(
            select(DirectionSource)
            .where(
                DirectionSource.owner_id == owner_id,
                DirectionSource.holder_type == holder_type,
                DirectionSource.holder_id == holder_id,
            )
            .order_by(DirectionSource.kind, DirectionSource.item_id, DirectionSource.id)
        )
    )


def holder_state(db: Session, owner_id: str, holder_type: str, holder_id: str, now: datetime) -> SourceState:
    return worst(source_state(db, owner_id, row, now) for row in sources_of(db, owner_id, holder_type, holder_id))


def exploration_state(db: Session, owner_id: str, exploration: DirectionExploration, now: datetime) -> SourceState:
    """How the sources of a saved exploration stand now (its own deletion state is the caller's to tell)."""
    return holder_state(db, owner_id, SourceHolder.EXPLORATION, exploration.id, now)


def decision_state(db: Session, owner_id: str, decision: DirectionDecision, now: datetime) -> BasisState:
    """How a decision's basis stands now; a decision made on a saved exploration stands as it does too."""
    if decision.basis == DecisionBasis.PERSONAL:
        return BasisState.PERSONAL

    if decision.basis == DecisionBasis.SAVED_EXPLORATION:
        exploration = db.scalar(
            select(DirectionExploration).where(
                DirectionExploration.id == decision.exploration_id,
                DirectionExploration.owner_id == owner_id,
            )
        )

        if exploration is None or is_expired(exploration, now):
            return BasisState.EXPLORATION_GONE

        if exploration.deleted_at is not None:
            return BasisState.EXPLORATION_IN_BIN

    return BasisState(holder_state(db, owner_id, SourceHolder.DECISION, decision.id, now).value)


def usable_for_search(db: Session, owner_id: str, decision: DirectionDecision, now: datetime) -> bool:
    """Whether a decision may name a direction for a new search: kept, in use, and resting on the user's own
    choice or on sources that are all current."""
    return (
        decision.deleted_at is None
        and decision.state == DecisionState.KEEP
        and decision_state(db, owner_id, decision, now) in (BasisState.PERSONAL, BasisState.CURRENT)
    )


@dataclass(frozen=True)
class DirectionReferences:
    """The direction records in use that rest on a record (or one version of it): ids only."""

    explorations: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()


def referencing(
    db: Session,
    owner_id: str,
    item_id: str,
    version_id: str | None = None,
) -> DirectionReferences:
    """The saved explorations and the decisions in use that rest on this record, or on this version of it.

    A decision made the user's own choice is listed no more: what it rested on no longer decides whether it
    may be used.
    """
    conditions = [DirectionSource.owner_id == owner_id, DirectionSource.item_id == item_id]

    if version_id is not None:
        conditions.append(DirectionSource.version_id == version_id)

    holders = {
        holder_type: {
            row.holder_id
            for row in db.scalars(select(DirectionSource).where(*conditions, DirectionSource.holder_type == holder_type))
        }
        for holder_type in SourceHolder
    }
    explorations = db.scalars(
        select(DirectionExploration.id)
        .where(
            DirectionExploration.owner_id == owner_id,
            DirectionExploration.id.in_(holders[SourceHolder.EXPLORATION]),
            DirectionExploration.deleted_at.is_(None),
        )
        .order_by(DirectionExploration.saved_at, DirectionExploration.id)
    )
    decisions = db.scalars(
        select(DirectionDecision.id)
        .where(
            DirectionDecision.owner_id == owner_id,
            DirectionDecision.id.in_(holders[SourceHolder.DECISION]),
            DirectionDecision.deleted_at.is_(None),
            DirectionDecision.basis != DecisionBasis.PERSONAL,
        )
        .order_by(DirectionDecision.created_at, DirectionDecision.id)
    )

    return DirectionReferences(explorations=tuple(explorations), decisions=tuple(decisions))


def resting_explorations(
    db: Session, owner_id: str, item_id: str, version_id: str | None = None
) -> list[DirectionExploration]:
    """The saved explorations in use resting on a record, or on this version of it, oldest first (U3-C3b: what a
    deletion page lists and offers to delete along)."""
    wanted = referencing(db, owner_id, item_id, version_id).explorations
    rows = {
        row.id: row
        for row in db.scalars(
            select(DirectionExploration).where(DirectionExploration.owner_id == owner_id, DirectionExploration.id.in_(wanted))
        )
    }

    return [rows[exploration_id] for exploration_id in wanted if exploration_id in rows]


# A direction record as a page names it (U3-C3b): "<id>.<revision>", the id as the library makes them.
RECORD_REF = re.compile(r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?P<revision>0|[1-9][0-9]{0,8})")


def record_ref(record_id: str, revision: int) -> str:
    return f"{record_id}.{revision}"


def read_record_ref(text: str) -> tuple[str, int] | None:
    """(id, revision) from "<id>.<revision>", or None for anything else."""
    match = RECORD_REF.fullmatch(text)

    return (match["id"], int(match["revision"])) if match else None


def read_record_refs(texts) -> tuple[tuple[str, int], ...] | None:
    """Every piece read as a record ref, the same one given twice taken once; None if any piece is not one, or if
    one id comes with two revisions: a page that says two things of one record is not read either way."""
    refs = {}

    for text in texts:
        ref = read_record_ref(text)

        if ref is None or refs.get(ref[0], ref[1]) != ref[1]:
            return None

        refs[ref[0]] = ref[1]

    return tuple(refs.items())
