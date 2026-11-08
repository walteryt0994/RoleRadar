"""The internal entries that save direction explorations and decisions, and tell how they stand (U3-C1).

No page or route reaches these yet: the fixed workflow (C2) and the pages (C3) will call them with what the
server itself holds. Every entry takes the owner from its caller (the web layer passes the session's user)
and filters every lookup by it, so another user's ids are simply not found.

save_exploration keeps a checked result the user chose to keep. It takes a TrustedExploration, never a
page's JSON, and checks it against the library again: the owner, every record and version it names as one
of the owner's records of that kind (a confirmed version of an experience, a saved version of the
preferences) still in use, the fields chosen as fields of that record, each still reading as it did, and
every quote standing in the field it names in the version it names. Anything else and nothing is saved, and
no newer material is put in its place: an old result never passes for the current one. Saving the same
result again finds the one kept; one in the recycle bin has to be restored instead.

A decision (keep, defer, exclude) is a user's choice of its own and never saves the result it was made on:
- decide_on_saved: made on a saved exploration, linked to it, resting on the records that direction quotes;
- decide_on_unsaved: made on a result that was not saved. The result is checked as for saving, only the
  ids, versions and field names the direction rests on are kept, and the decision says so (the full
  analysis was not kept);
- decide_personal: the user's own choice. On an existing decision it keeps the link and the sources as
  history, so that a decision whose basis was deleted is never made to look new by dropping them.
A user has one decision in use per direction name (contracts.name_key). Changing one names the decision the
caller saw, by its id and its revision (SeenDecision): a decision made again under the same name after the
one seen was deleted is another decision, whatever its revision, and the change is refused (U3-C1 R1). A
caller that saw no decision gets a conflict when one exists meanwhile, and one that saw a decision gets a
conflict when it is no longer in use.

Deleting an exploration deletes no decision: one made on it then stands as resting on an exploration in the
bin or gone (app.library.references) and is not used for a new search until the user makes it their own
choice again.
"""
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.directions.contracts import (
    NAME_CHARS,
    DecisionBasis,
    DecisionState,
    Direction,
    ExplorationResult,
    TrustedExploration,
    name_key,
)
from app.library.content import CATEGORY_FIELDS
from app.library.errors import NotAllowed, NotFound, RecordConflict
from app.library.lifecycle import (
    active_direction_record,
    check_record_revision,
    decision_in_use,
    delete_direction_record,
    request_direction_record_purge,
    restore_direction_record,
)
from app.library.models import (
    DirectionDecision,
    DirectionExploration,
    DirectionSource,
    ExperienceItem,
    ExperienceVersion,
    SourceHolder,
    TargetType,
)
from app.library.references import (
    BasisState,
    SourceState,
    decision_state,
    exploration_state,
    sources_of,
    state_of,
    usable_for_search,
)
from app.library.service import is_expired, new_id, record_event

REFUSED = {
    SourceState.CHANGED: "这次探索用到的资料之后改过，所用的内容和当时不一样了：结果不能作为当前结果保存，也不会换用最新的资料。",
    SourceState.VERSION_IN_BIN: "这次探索用到的那个版本已经删除：结果不能作为当前结果保存。",
    SourceState.RECORD_IN_BIN: "这次探索用到的一份资料已经删除：结果不能作为当前结果保存。",
    SourceState.GONE: "这次探索用到的一份资料找不到了：结果不能保存。",
}
UNKNOWN_FIELD = "这次探索的来源里有这份资料没有的栏目：结果不能保存。"
QUOTE_MISMATCH = "结果里有一条引文和资料原文对不上：结果不能保存。"
SAVED_IN_BIN = "这次探索结果已经保存过，现在在回收站里；要找回请从回收站恢复。"
SAVED_AND_PURGED = "这次探索结果以前保存过，已经永久删除；请重新探索后再保存。"
NO_SUCH_DIRECTION = "这次探索里没有这个方向。"
BAD_NAME = f"方向名称要写 1 到 {NAME_CHARS} 个字。"


@dataclass(frozen=True)
class SeenDecision:
    """The decision a caller saw and means to change: its id and the revision it had then.

    Ids are never reused, so a decision deleted and made again under the same name is never taken for the one
    seen, though its revision starts again from 1.
    """

    decision_id: str
    revision: int


def save_exploration(db: Session, owner_id: str, trusted: TrustedExploration, now: datetime) -> DirectionExploration:
    """Keep a checked result the user chose to keep; the one kept already, if it was saved before."""
    _check_owner(owner_id, trusted)
    existing = db.scalar(
        select(DirectionExploration).where(
            DirectionExploration.owner_id == owner_id,
            DirectionExploration.result_id == trusted.result_id,
        )
    )

    if existing is not None:
        if existing.deleted_at is None:
            return existing

        raise NotAllowed(SAVED_AND_PURGED if is_expired(existing, now) else SAVED_IN_BIN)

    _check_sources(db, owner_id, trusted, now)
    exploration = DirectionExploration(
        id=new_id(),
        owner_id=owner_id,
        result_id=trusted.result_id,
        content=trusted.result.model_dump(mode="json"),
        generation=trusted.generation.model_dump(mode="json"),
        revision=1,
        saved_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(exploration)
    db.flush()
    _add_sources(
        db,
        owner_id,
        SourceHolder.EXPLORATION,
        exploration.id,
        [(source.kind, source.item_id, source.version_id, list(source.fields)) for source in trusted.sources],
    )
    record_event(
        db, owner_id, exploration.id, TargetType.DIRECTION_EXPLORATION, exploration.id, "direction_exploration_saved", now
    )

    return exploration


def decide_on_saved(
    db: Session,
    owner_id: str,
    exploration_id: str,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
) -> DirectionDecision:
    """Decide about a direction of a saved exploration, in use; the decision stays linked to it."""
    exploration = active_direction_record(db, owner_id, DirectionExploration, exploration_id)
    direction = ExplorationResult.model_validate(exploration.content).direction(name)

    if direction is None:
        raise NotAllowed(NO_SUCH_DIRECTION)

    kinds = {(row.item_id, row.version_id): row.kind for row in sources_of(db, owner_id, SourceHolder.EXPLORATION, exploration.id)}

    return _decide(
        db, owner_id, direction.name, state, seen, now,
        DecisionBasis.SAVED_EXPLORATION, exploration.id, _cited(direction, kinds),
    )


def decide_on_unsaved(
    db: Session,
    owner_id: str,
    trusted: TrustedExploration,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
) -> DirectionDecision:
    """Decide about a direction of a result that was not saved: the result is checked as for saving and not kept."""
    _check_owner(owner_id, trusted)
    _check_sources(db, owner_id, trusted, now)
    direction = trusted.result.direction(name)

    if direction is None:
        raise NotAllowed(NO_SUCH_DIRECTION)

    kinds = {(source.item_id, source.version_id): source.kind for source in trusted.sources}

    return _decide(
        db, owner_id, direction.name, state, seen, now,
        DecisionBasis.UNSAVED_EXPLORATION, None, _cited(direction, kinds),
    )


def decide_personal(
    db: Session,
    owner_id: str,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
) -> DirectionDecision:
    """The user's own choice about a direction; on an existing decision, what it rested on stays as history."""
    return _decide(db, owner_id, name, state, seen, now, DecisionBasis.PERSONAL, None, None)


def delete_exploration(db: Session, owner_id: str, exploration_id: str, expected_revision: int, now: datetime):
    return delete_direction_record(db, owner_id, DirectionExploration, exploration_id, expected_revision, now)


def restore_exploration(db: Session, owner_id: str, exploration_id: str, expected_revision: int, now: datetime):
    return restore_direction_record(db, owner_id, DirectionExploration, exploration_id, expected_revision, now)


def request_exploration_purge(db: Session, owner_id: str, exploration_id: str, expected_revision: int, now: datetime) -> str:
    return request_direction_record_purge(db, owner_id, DirectionExploration, exploration_id, expected_revision, now)


def delete_decision(db: Session, owner_id: str, decision_id: str, expected_revision: int, now: datetime):
    return delete_direction_record(db, owner_id, DirectionDecision, decision_id, expected_revision, now)


def restore_decision(db: Session, owner_id: str, decision_id: str, expected_revision: int, now: datetime):
    return restore_direction_record(db, owner_id, DirectionDecision, decision_id, expected_revision, now)


def request_decision_purge(db: Session, owner_id: str, decision_id: str, expected_revision: int, now: datetime) -> str:
    return request_direction_record_purge(db, owner_id, DirectionDecision, decision_id, expected_revision, now)


def explorations_in_use(db: Session, owner_id: str) -> list[DirectionExploration]:
    return list(
        db.scalars(
            select(DirectionExploration)
            .where(DirectionExploration.owner_id == owner_id, DirectionExploration.deleted_at.is_(None))
            .order_by(DirectionExploration.saved_at.desc(), DirectionExploration.id)
        )
    )


def decisions_in_use(db: Session, owner_id: str) -> list[DirectionDecision]:
    return list(
        db.scalars(
            select(DirectionDecision)
            .where(DirectionDecision.owner_id == owner_id, DirectionDecision.deleted_at.is_(None))
            .order_by(DirectionDecision.updated_at.desc(), DirectionDecision.id)
        )
    )


def exploration_basis(db: Session, owner_id: str, exploration_id: str, now: datetime) -> SourceState:
    """How what a saved exploration in use rests on stands now."""
    return exploration_state(db, owner_id, active_direction_record(db, owner_id, DirectionExploration, exploration_id), now)


def decision_basis(db: Session, owner_id: str, decision_id: str, now: datetime) -> BasisState:
    return decision_state(db, owner_id, active_direction_record(db, owner_id, DirectionDecision, decision_id), now)


def directions_for_search(db: Session, owner_id: str, now: datetime) -> tuple[str, ...]:
    """The names a new search may take from the user's decisions: kept, resting on the user's own choice or
    on records that all stand as they did. The user still chooses among them for each search."""
    return tuple(
        decision.name for decision in decisions_in_use(db, owner_id) if usable_for_search(db, owner_id, decision, now)
    )


# --- checks and writes ----------------------------------------------------------------------------------------


def _check_owner(owner_id: str, trusted: TrustedExploration) -> None:
    if trusted.owner_id != owner_id:
        raise NotFound()


def _check_sources(db: Session, owner_id: str, trusted: TrustedExploration, now: datetime) -> None:
    """Every record named still the owner's and as it was, every field one of its own, every quote in its text."""
    texts = {}

    for source in trusted.sources:
        state = state_of(db, owner_id, source.kind, source.item_id, source.version_id, source.fields, now)

        if state is not SourceState.CURRENT:
            raise NotAllowed(REFUSED[state])

        item = db.scalar(select(ExperienceItem).where(ExperienceItem.id == source.item_id, ExperienceItem.owner_id == owner_id))
        version = db.scalar(
            select(ExperienceVersion).where(
                ExperienceVersion.id == source.version_id,
                ExperienceVersion.item_id == item.id,
                ExperienceVersion.owner_id == owner_id,
            )
        )

        if not set(source.fields) <= {field.name for field in CATEGORY_FIELDS[item.category]}:
            raise NotAllowed(UNKNOWN_FIELD)

        texts[(source.item_id, source.version_id)] = version.content if isinstance(version.content, dict) else {}

    for direction in trusted.result.directions:
        for quote in direction.quotes():
            field_text = texts[(quote.item_id, quote.version_id)].get(quote.field, "")

            if not isinstance(field_text, str) or not _stands_in(quote.text, field_text):
                raise NotAllowed(QUOTE_MISMATCH)


def _stands_in(quote: str, text: str) -> bool:
    """Whether the quote stands in the text; only whitespace may differ (as the job analysis reads quotes)."""
    return re.search(r"\s+".join(re.escape(word) for word in quote.split()), text) is not None


def _cited(direction: Direction, kinds: dict) -> list[tuple[str, str, str, list[str]]]:
    """What a direction rests on: each record and version it quotes, with the fields it quotes from them."""
    fields: dict[tuple[str, str], set[str]] = {}

    for quote in direction.quotes():
        fields.setdefault((quote.item_id, quote.version_id), set()).add(quote.field)

    return [(kinds[key], key[0], key[1], sorted(names)) for key, names in sorted(fields.items())]


def _decide(
    db: Session,
    owner_id: str,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
    basis: DecisionBasis,
    exploration_id: str | None,
    sources: list | None,
) -> DirectionDecision:
    if seen is not None and not isinstance(seen, SeenDecision):
        # A revision on its own names no decision: it starts again from 1 for one made again under the name.
        raise TypeError("seen must be a SeenDecision (the decision's id and revision) or None")

    state = DecisionState(state)
    name = name.strip()
    key = name_key(name)

    if not key or len(name) > NAME_CHARS:
        raise NotAllowed(BAD_NAME)

    decision = decision_in_use(db, owner_id, key)

    if decision is None:
        if seen is not None:
            # The caller saw a decision that is no longer in use.
            raise RecordConflict(seen.decision_id)

        decision = DirectionDecision(
            id=new_id(),
            owner_id=owner_id,
            name=name,
            name_key=key,
            state=state,
            basis=basis,
            exploration_id=exploration_id,
            revision=1,
            created_at=now,
            updated_at=now,
            deleted_at=None,
            purge_after=None,
            deletion_id=None,
        )
        db.add(decision)
        db.flush()
    else:
        if seen is None or seen.decision_id != decision.id:
            # The caller saw no decision, or another one: this one was made (again) meanwhile.
            raise RecordConflict(decision.id)

        check_record_revision(decision, seen.revision)
        decision.name = name
        decision.state = state
        decision.basis = basis
        decision.revision += 1
        decision.updated_at = now

        if basis is not DecisionBasis.PERSONAL:
            # Decided again on another result, by the user: it rests on that one from now on.
            decision.exploration_id = exploration_id
            db.execute(
                delete(DirectionSource).where(
                    DirectionSource.owner_id == owner_id,
                    DirectionSource.holder_type == SourceHolder.DECISION,
                    DirectionSource.holder_id == decision.id,
                )
            )

    if sources is not None:
        _add_sources(db, owner_id, SourceHolder.DECISION, decision.id, sources)

    record_event(db, owner_id, decision.id, TargetType.DIRECTION_DECISION, decision.id, "direction_decided", now)

    return decision


def _add_sources(db: Session, owner_id: str, holder_type: str, holder_id: str, sources) -> None:
    for kind, item_id, version_id, fields in sources:
        db.add(
            DirectionSource(
                id=new_id(),
                owner_id=owner_id,
                holder_type=holder_type,
                holder_id=holder_id,
                kind=kind,
                item_id=item_id,
                version_id=version_id,
                fields=list(fields),
            )
        )
