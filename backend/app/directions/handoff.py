"""The kept directions a search may take (U3-C3b), read again from the library each time, never from a page.

A page names a decision by its id and revision ("<id>.<revision>", app.library.references.read_record_ref), never by
its name: the name a search takes is the decision's as it is stored now. What may be handed to a search is what C1
says may name a direction for a new search (usable_for_search): kept, in use, and resting on the user's own choice
or on sources that all stand as they did. That is the rule of the decision's own basis. It is not whether the whole
exploration it was made on could be decided on again (app.directions.actions.changeable): a direction whose own
quotes stand may be searched for although another direction of its exploration rests on something changed.

A decision chosen at one revision and changed since in any way (state, basis, made the user's own choice, undone,
made again under the same name) is not the one chosen: it is never handed on in its place. Nothing here writes: no
decision, preference or search is changed or started.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.directions.records import decisions_in_use
from app.library.models import DecisionState, DirectionDecision
from app.library.references import BasisState, decision_state, record_ref, usable_for_search


@dataclass(frozen=True)
class Kept:
    """A kept direction a search may take: the decision's id and revision, and its name as stored."""

    decision_id: str
    revision: int
    name: str

    @property
    def ref(self) -> str:
        return record_ref(self.decision_id, self.revision)


@dataclass(frozen=True)
class HeldBack:
    """A kept decision in use that no search may take now, and how its basis stands (C1's own rule)."""

    decision: DirectionDecision
    basis: BasisState


@dataclass(frozen=True)
class KeptCheck:
    """The kept directions a page chose, as they stand now.

    kept: those still usable for a search at the revision chosen, in the order chosen. changed: the names of the
    user's own decisions in use that changed since, or that no search may take now. missing: how many chosen are no
    decision of the user's in use (undone, purged, another user's or none at all): counted alike, so nothing of
    another user's shows.
    """

    kept: tuple[Kept, ...] = ()
    changed: tuple[str, ...] = ()
    missing: int = 0

    @property
    def stands(self) -> bool:
        return not self.changed and not self.missing


def kept_for_search(db: Session, owner_id: str, now: datetime) -> tuple[Kept, ...]:
    """Every kept direction of the user a search may take now, most recently changed first."""
    return tuple(
        Kept(decision.id, decision.revision, decision.name)
        for decision in decisions_in_use(db, owner_id)
        if usable_for_search(db, owner_id, decision, now)
    )


def held_back(db: Session, owner_id: str, now: datetime) -> tuple[HeldBack, ...]:
    """The user's kept decisions in use that no search may take now, with how their basis stands."""
    return tuple(
        HeldBack(decision, decision_state(db, owner_id, decision, now))
        for decision in decisions_in_use(db, owner_id)
        if decision.state == DecisionState.KEEP and not usable_for_search(db, owner_id, decision, now)
    )


def check_kept(db: Session, owner_id: str, chosen: tuple[tuple[str, int], ...], now: datetime) -> KeptCheck:
    """Each chosen (decision id, revision) read again: still the user's, in use, at that revision and usable now."""
    in_use = {decision.id: decision for decision in decisions_in_use(db, owner_id)}
    kept, changed, missing = [], [], 0

    for decision_id, revision in chosen:
        decision = in_use.get(decision_id)

        if decision is None:
            missing += 1
        elif decision.revision != revision or not usable_for_search(db, owner_id, decision, now):
            changed.append(decision.name)
        else:
            kept.append(Kept(decision.id, decision.revision, decision.name))

    return KeptCheck(tuple(kept), tuple(changed), missing)
