"""What the direction pages write, each checked on the server before anything is written (U3-C3a).

The pages call these inside one transaction of the library: every check below and the write it guards see the
library as it is in that transaction, never as a page showed it. A button on a page is no permission: a post made
after the page was opened, or made without the page, is checked just the same.

- A result of a task (a complete one: only it has a trusted snapshot) is saved, or decided on, only while every
  source it rests on stands as it did (app.library.references): an edit or a deletion since, and nothing is
  written. Saving the same result again finds the one kept (app.directions.records.save_exploration), but only
  after this check: finding it is no sign that its basis still stands, and one in the recycle bin stays there.
- What became of the exploration saved from a result (saved_of) is told by what the task remembers (the id it
  saved) as well as by the database: a task that saved one whose record is gone has it deleted for good, never
  "not saved", so neither saving again nor deciding brings the result back as new or as unsaved (U3-C3a R1).
- A decision on a saved exploration needs that exploration in use, of this owner, with every source it rests on
  standing as it did; app.directions.records.decide_on_saved does not check the sources again, so this does.
- A decision keeps what it rests on. Changing its state keeps its basis: on its saved exploration, as above, or as
  the user's own choice when it is one already. A decision on an exploration that can no longer be used (not
  saved, deleted, or resting on records changed since) changes only once the user makes it their own choice
  (make_own_choice), which the pages ask them to confirm; its links stay as history (decide_personal).

Every change of a decision names the decision the user saw, by its id and revision (SeenDecision).
"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.directions.contracts import SourceRef, TrustedExploration
from app.directions.records import (
    SAVED_AND_PURGED,
    SAVED_IN_BIN,
    SeenDecision,
    decide_on_saved,
    decide_on_unsaved,
    decide_personal,
    exploration_for_result,
    save_exploration,
)
from app.library.errors import NotAllowed
from app.library.lifecycle import active_direction_record
from app.library.models import DecisionBasis, DirectionDecision, DirectionExploration
from app.library.references import SourceState, exploration_state, state_of, worst
from app.library.service import is_expired

RESULT_MOVED = "这次结果用到的资料在这之后改过或删除了：结果不能保存，也不能据此做决定。可以重新核对后再探索一次。"
EXPLORATION_MOVED = "这次探索依据的资料在保存之后改过或删除了：不能再根据它做决定。"
GONE_FOR_DECIDING = "这次探索保存以后已经永久删除，不能再根据这次结果做决定。要重新探索，请重新核对。"
NEEDS_OWN_CHOICE = (
    "这条决定依据的探索已经不能使用（没有保存、已删除，或者依据的资料改过），所以不能直接改。"
    "要改变它，请先把它重新定为你自己的选择；也可以撤销这条决定。"
)


class Saved(StrEnum):
    """What became of the exploration saved from a result."""

    NEVER = "never"
    IN_USE = "in_use"
    # Deleted and still restorable from the recycle bin.
    IN_BIN = "in_bin"
    # Deleted for good: past restoring (a purge asked for, or its time out), or removed already.
    GONE = "gone"


def basis_now(db: Session, owner_id: str, refs: tuple[SourceRef, ...], now: datetime) -> SourceState:
    """How what a result rests on stands now: the worst of its sources, field by field, as C1 checks a save."""
    return worst(state_of(db, owner_id, ref.kind, ref.item_id, ref.version_id, ref.fields, now) for ref in refs)


def saved_of(
    db: Session, owner_id: str, result_id: str, saved_id: str | None, now: datetime
) -> tuple[Saved, DirectionExploration | None]:
    """What became of the exploration saved from a result, and its record while there is one.

    saved_id: the exploration the task remembers saving from this result, or None. The record is looked up by it
    and by the result's id, so one found either way counts; one the task saved and the database no longer holds
    was deleted for good."""
    row = None

    if saved_id is not None:
        row = db.scalar(
            select(DirectionExploration).where(DirectionExploration.id == saved_id, DirectionExploration.owner_id == owner_id)
        )

    if row is None:
        row = exploration_for_result(db, owner_id, result_id)

    if row is None:
        return (Saved.GONE if saved_id is not None else Saved.NEVER), None

    if row.deleted_at is None:
        return Saved.IN_USE, row

    return (Saved.GONE if is_expired(row, now) else Saved.IN_BIN), row


def save_result(
    db: Session, owner_id: str, trusted: TrustedExploration, now: datetime, saved_id: str | None = None
) -> DirectionExploration:
    """Keep a complete result the user chose to keep: never one saved before and deleted since, and only while
    what it rests on stands as it did. saved_id: the exploration the task remembers saving from it."""
    saved, _ = saved_of(db, owner_id, trusted.result_id, saved_id, now)

    if saved is Saved.GONE:
        raise NotAllowed(SAVED_AND_PURGED)

    if saved is Saved.IN_BIN:
        raise NotAllowed(SAVED_IN_BIN)

    if basis_now(db, owner_id, trusted.sources, now) is not SourceState.CURRENT:
        raise NotAllowed(RESULT_MOVED)

    return save_exploration(db, owner_id, trusted, now)


def decide_on_result(
    db: Session,
    owner_id: str,
    trusted: TrustedExploration,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
    saved_id: str | None = None,
) -> DirectionDecision:
    """Decide about a direction of a complete result: on the exploration saved from it, while it is in use, or on
    the unsaved result when it was never saved; never on one saved and deleted since. Either way only while what
    the result rests on stands as it did. saved_id: the exploration the task remembers saving from it."""
    if basis_now(db, owner_id, trusted.sources, now) is not SourceState.CURRENT:
        raise NotAllowed(RESULT_MOVED)

    saved, row = saved_of(db, owner_id, trusted.result_id, saved_id, now)

    if saved is Saved.NEVER:
        return decide_on_unsaved(db, owner_id, trusted, name, state, seen, now)

    if saved is Saved.GONE:
        raise NotAllowed(GONE_FOR_DECIDING)

    if saved is Saved.IN_BIN:
        raise NotAllowed(SAVED_IN_BIN)

    return decide_on_saved_exploration(db, owner_id, row.id, name, state, seen, now)


def decide_on_saved_exploration(
    db: Session,
    owner_id: str,
    exploration_id: str,
    name: str,
    state: str,
    seen: SeenDecision | None,
    now: datetime,
) -> DirectionDecision:
    """Decide about a direction of a saved exploration: of this owner, in use, every source as it was."""
    exploration = active_direction_record(db, owner_id, DirectionExploration, exploration_id)

    if exploration_state(db, owner_id, exploration, now) is not SourceState.CURRENT:
        raise NotAllowed(EXPLORATION_MOVED)

    return decide_on_saved(db, owner_id, exploration.id, name, state, seen, now)


def changeable(db: Session, owner_id: str, decision: DirectionDecision, now: datetime) -> bool:
    """Whether a decision's state may change as it is, keeping its basis (change_decision): the user's own choice,
    or one on a saved exploration in use whose every source stands as it was, as deciding on it again checks."""
    if decision.basis == DecisionBasis.PERSONAL:
        return True

    if decision.basis != DecisionBasis.SAVED_EXPLORATION or decision.exploration_id is None:
        return False

    exploration = db.scalar(
        select(DirectionExploration).where(
            DirectionExploration.id == decision.exploration_id,
            DirectionExploration.owner_id == owner_id,
            DirectionExploration.deleted_at.is_(None),
        )
    )

    return exploration is not None and exploration_state(db, owner_id, exploration, now) is SourceState.CURRENT


def change_decision(db: Session, owner_id: str, decision_id: str, revision: int, state: str, now: datetime) -> DirectionDecision:
    """Change a decision's state and keep what it rests on; never turns it into the user's own choice."""
    decision = active_direction_record(db, owner_id, DirectionDecision, decision_id)
    seen = SeenDecision(decision.id, revision)

    if decision.basis == DecisionBasis.PERSONAL:
        return decide_personal(db, owner_id, decision.name, state, seen, now)

    if not changeable(db, owner_id, decision, now):
        raise NotAllowed(NEEDS_OWN_CHOICE)

    return decide_on_saved_exploration(db, owner_id, decision.exploration_id, decision.name, state, seen, now)


def make_own_choice(db: Session, owner_id: str, decision_id: str, revision: int, state: str, now: datetime) -> DirectionDecision:
    """Make a decision the user's own choice, as they confirmed it: what it rested on stays as history."""
    decision = active_direction_record(db, owner_id, DirectionDecision, decision_id)

    return decide_personal(db, owner_id, decision.name, state, SeenDecision(decision.id, revision), now)
