"""What a direction exploration result, what it rests on, and a decision about a direction are (U3-C1).

An exploration result is what the fixed workflow (C2) will hand back once it has been checked: three to five
directions to compare, each with a general description of the main work, the evidence quoted from the user's
own records, how it relates to what the user wrote about the work they want, the gaps, what cannot be told
yet, and how it differs from the others. No score, no ranking. A direction may quote nothing at all: missing
experience is no verdict, and a user with no confirmed experience or no preferences may still explore.

How a direction relates to a user condition keeps two things apart: the condition, quoted from the user's
preferences, and the basis on the direction's side. A quoted condition on its own proves nothing about the
direction, so only a direction-side basis can make a relation "fits" or "may conflict"; without one it is
"unknown" (U3-C pre-check 12.4).

A TrustedExploration is a result together with what the server itself knows of how it came about: whose it
is, the id the server gave the result, the records the task was allowed to use (record, version, the fields
chosen) and how it was generated. The server builds it from its own task state, never from what a page or a
model sends; saving it (app.directions.records) checks every part against the library again anyway.
"""
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.library.models import DecisionBasis, DecisionState, SourceKind

__all__ = [
    "DecisionBasis",
    "DecisionState",
    "Direction",
    "ExplorationResult",
    "Generation",
    "IdealLink",
    "Quote",
    "Relation",
    "SourceKind",
    "SourceRef",
    "TrustedExploration",
    "name_key",
]

MIN_DIRECTIONS, MAX_DIRECTIONS = 3, 5
NAME_CHARS = 40
MAIN_WORK_CHARS = 600
DIFFERENCE_CHARS = 400
QUOTE_CHARS = 300
LINE_CHARS = 200
# A task never uses more records than this (U2 allows ten experiences, plus the preferences).
MAX_SOURCES = 20


def _text(limit: int):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=limit)]


Id = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
FieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Label = _text(200)
Line = _text(LINE_CHARS)


def name_key(name: str) -> str:
    """A direction name as names are compared: full-width forms, case and spaces aside."""
    return "".join(unicodedata.normalize("NFKC", name).casefold().split())


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Quote(_Contract):
    """Words quoted from one field of one version of one of the user's records."""

    item_id: Id
    version_id: Id
    field: FieldName
    text: _text(QUOTE_CHARS)


class Relation(StrEnum):
    FITS = "fits"
    MAY_CONFLICT = "may_conflict"
    UNKNOWN = "unknown"


class IdealLink(_Contract):
    """How a direction relates to one condition the user wrote: the user's condition and, apart from it, the
    basis on the direction's side. Without such a basis the relation is unknown."""

    condition: Quote
    relation: Relation
    basis: Line | None = None

    @model_validator(mode="after")
    def _basis_on_the_directions_side(self):
        if self.relation is Relation.UNKNOWN and self.basis is not None:
            raise ValueError("an unknown relation has no basis")

        if self.relation is not Relation.UNKNOWN and self.basis is None:
            raise ValueError("fits and may_conflict need a basis on the direction's side")

        return self


def _distinct(values: tuple) -> tuple:
    if len(set(values)) != len(values):
        raise ValueError("the same entry twice")

    return values


class Direction(_Contract):
    name: _text(NAME_CHARS)
    main_work: _text(MAIN_WORK_CHARS)
    evidence: Annotated[tuple[Quote, ...], AfterValidator(_distinct)] = Field(default=(), max_length=8)
    ideal: tuple[IdealLink, ...] = Field(default=(), max_length=8)
    gaps: tuple[Line, ...] = Field(default=(), max_length=6)
    unknowns: tuple[Line, ...] = Field(default=(), max_length=6)
    difference: _text(DIFFERENCE_CHARS)
    next_steps: tuple[Line, ...] = Field(default=(), max_length=3)

    def quotes(self) -> tuple[Quote, ...]:
        """Every quote the direction rests on: its evidence and the user conditions it relates to."""
        return self.evidence + tuple(link.condition for link in self.ideal)


class ExplorationResult(_Contract):
    directions: tuple[Direction, ...] = Field(min_length=MIN_DIRECTIONS, max_length=MAX_DIRECTIONS)

    @model_validator(mode="after")
    def _names_differ(self):
        keys = [name_key(direction.name) for direction in self.directions]

        if len(set(keys)) != len(keys):
            raise ValueError("two directions with the same name")

        return self

    def direction(self, name: str) -> Direction | None:
        key = name_key(name)

        return next((direction for direction in self.directions if name_key(direction.name) == key), None)


class SourceRef(_Contract):
    """One record a task was allowed to use: its kind, the version used, and the fields chosen from it."""

    kind: SourceKind
    item_id: Id
    version_id: Id
    fields: Annotated[tuple[FieldName, ...], AfterValidator(_distinct)] = Field(min_length=1, max_length=40)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")

    return value


class Generation(_Contract):
    """How a result was made, as the server recorded it: who answered, with which prompt and notice, on what."""

    provider: Label
    model: Label
    endpoint: Label
    prompt_version: _text(20)
    notice_version: _text(40)
    input_fingerprint: Sha256
    generated_at: Annotated[datetime, AfterValidator(_aware)]


class TrustedExploration(_Contract):
    """A checked result with what the server knows of how it came about (see the module docstring)."""

    owner_id: Id
    result_id: Id
    sources: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_SOURCES)
    result: ExplorationResult
    generation: Generation

    @model_validator(mode="after")
    def _quotes_stand_in_chosen_sources(self):
        records = [(source.kind, source.item_id) for source in self.sources]

        if len(set(records)) != len(records):
            raise ValueError("a record named twice")

        if sum(source.kind is SourceKind.PREFERENCES for source in self.sources) > 1:
            raise ValueError("more than one preferences")

        chosen = {(source.kind, source.item_id, source.version_id): set(source.fields) for source in self.sources}

        for direction in self.result.directions:
            for kind, quotes in (
                (SourceKind.EXPERIENCE, direction.evidence),
                (SourceKind.PREFERENCES, tuple(link.condition for link in direction.ideal)),
            ):
                for quote in quotes:
                    if quote.field not in chosen.get((kind, quote.item_id, quote.version_id), ()):
                        raise ValueError("a quote outside the records and fields the task was allowed to use")

        return self

    def source_of(self, quote: Quote) -> SourceRef:
        return next(
            source
            for source in self.sources
            if (source.item_id, source.version_id) == (quote.item_id, quote.version_id) and quote.field in source.fields
        )
