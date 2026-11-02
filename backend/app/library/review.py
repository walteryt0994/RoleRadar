"""Candidates read from an import, and the user's review of them (U2-B2a).

The user asks for candidates from an import that was read, fully or in
part. structure_import runs the structurer on a copy of what was read,
outside any transaction, and stores the checked answer only if the run is
still the one the import waits for and the import is still open, unexpired
and unchanged; otherwise the answer is dropped. Candidates are asked for
once: when some were kept, asking again changes nothing, so a second run
never replaces the user's edits or choices; when none could be kept, asking
again is allowed. A run whose request is gone (the app stopped meanwhile)
may be started again after STALE_STRUCTURING_AFTER.

A candidate can be edited, left out and taken back; each change names the
revision the page showed, and CandidateConflict says another page changed
it first. Edits are kept apart from what was found. compare reads the
user's current experiences and profile to say which candidates look new,
like one already there (by name only), or like one with differences; it
writes nothing. All of it is part of the import: it is kept as the import
is (for seven days, or as a saved draft), exported with it, and goes with
it (imports._end). Nothing here writes to the experiences, the profile or
the preferences, and nothing is sent anywhere: in this batch the only
structurer is the local simulation.
"""
import copy
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.candidates import (
    CANDIDATE_FIELDS,
    CAUTION_TEXTS,
    CONTRACT_VERSION,
    FAILURE_TEXTS,
    INTENTION,
    KIND_LABELS,
    PROBLEM_TEXTS,
    REFERENCE_KINDS,
    SERVICE_FAILED,
    Checked,
    Structurer,
    changed_fields,
    check_answer,
    current_values,
    extracted_value,
    where,
)
from app.library.content import CATEGORY_FIELDS, EXPERIENCE_CATEGORIES, PROFILE, TITLE_FIELDS, Field, display_title
from app.library.db import Database
from app.library.errors import NotFound
from app.library.imports import IMPORT_OBJECT, STALE_STRUCTURING_AFTER, is_open, locator, open_import
from app.library.models import (
    AdoptionAction,
    CandidateState,
    ExperienceItem,
    ImportAdoption,
    ImportCandidate,
    ImportDraft,
    ImportState,
    ImportStructuring,
    ResumeImport,
    StructuringState,
)
from app.library.resume_rules import SIMULATION_NAME
from app.library.service import current_document, list_experiences, new_id, record_event

logger = logging.getLogger("roleradar.library")

# A run takes well under a second; one this old lost its request.
STRUCTURABLE = (ImportState.READ, ImportState.PARTIAL)
STRUCTURER_LABELS = {SIMULATION_NAME: "本机模拟整理规则（不是 AI）"}
STATE_LABELS = {CandidateState.PENDING: "待核对", CandidateState.EXCLUDED: "不加入", CandidateState.ADOPTED: "已加入"}
ACTION_LABELS = {
    AdoptionAction.NEW: "作为新的经历草稿加入",
    AdoptionAction.SUPPLEMENT: "补充到已有经历",
    AdoptionAction.PROFILE: "写入基本资料",
}
# What the history of a record says when an adoption wrote it (U2-B2b): events, never the words.
ADOPTED_FROM_IMPORT = "adopted_from_import"
SUPPLEMENTED_FROM_IMPORT = "supplemented_from_import"
IMPORT_EVENTS = (ADOPTED_FROM_IMPORT, SUPPLEMENTED_FROM_IMPORT)
# A shorter name inside a longer one counts only from this many characters.
CONTAINED_FROM = 4
# A space next to a Chinese character or mark carries no meaning: 使用 Python is 使用Python.
CJK_SPACE = re.compile(r"(?<=[\u3000-\u303f\u3400-\u9fff]) | (?=[\u3000-\u303f\u3400-\u9fff])")
# The only characters made narrow before fields are compared: the full-width forms of ASCII
# (！ to ～, so Ｃ＋＋ is C++) and the ideographic space. Nothing else is folded.
FULL_WIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}


class Asked(StrEnum):
    """What asking for candidates did."""

    STORED = "stored"
    # Nothing usable came back; asking again is allowed.
    FAILED = "failed"
    # Candidates were kept before; nothing changed.
    ALREADY = "already"
    # Another run is going on; nothing changed.
    RUNNING = "running"
    # Nothing was read that could be structured.
    NOT_READ = "not_read"
    # The import ended, expired or changed meanwhile, or a newer run took over: the answer was dropped.
    DROPPED = "dropped"


@dataclass(frozen=True)
class Attempt:
    id: str
    import_revision: int
    segments: list


class CandidateConflict(Exception):
    """A change named a revision the candidate has left behind; nothing was written."""

    def __init__(self, candidate: "CandidateSnapshot"):
        self.candidate = candidate


class NotEditable(Exception):
    """A candidate is changed only while it waits: one left out is taken back first, one adopted never."""

    def __init__(self, state: str):
        self.state = state


@dataclass(frozen=True)
class CandidateSnapshot:
    """A plain copy of a candidate that stays readable after its transaction rolls back."""

    id: str
    kind: str
    state: str
    revision: int
    values: dict

    @classmethod
    def of(cls, candidate: ImportCandidate) -> "CandidateSnapshot":
        return cls(
            id=candidate.id,
            kind=candidate.kind,
            state=candidate.state,
            revision=candidate.revision,
            values=current_values(candidate.kind, candidate.extracted, candidate.edits),
        )


# --- asking for candidates ------------------------------------------------------------


def structuring_of(db: Session, import_id: str) -> ImportStructuring | None:
    return db.get(ImportStructuring, import_id)


def start_structuring(
    database: Database, owner_id: str, import_id: str, structurer_name: str, now: datetime
) -> tuple[Asked | None, Attempt | None]:
    """Begin a run, or say why none begins; NotFound if the import is not open (or is another's)."""
    with database.transaction() as db:
        row = open_import(db, owner_id, import_id, now)

        if row.state not in STRUCTURABLE or not row.segments:
            return Asked.NOT_READ, None

        current = db.get(ImportStructuring, row.id)

        if current is not None:
            if current.state == StructuringState.DONE:
                return Asked.ALREADY, None

            if current.state == StructuringState.RUNNING and now < current.started_at + STALE_STRUCTURING_AFTER:
                return Asked.RUNNING, None

        if current is None:
            current = ImportStructuring(import_id=row.id, owner_id=owner_id)
            db.add(current)

        current.state = StructuringState.RUNNING
        current.attempt = new_id()
        current.import_revision = row.revision
        current.structurer = structurer_name
        current.contract_version = CONTRACT_VERSION
        current.outcome = None
        current.problems = None
        current.started_at = now
        current.finished_at = None

        return None, Attempt(current.attempt, row.revision, copy.deepcopy(row.segments))


def finish_structuring(
    database: Database,
    owner_id: str,
    import_id: str,
    attempt: Attempt,
    checked: Checked,
    clock: Callable[[], datetime],
) -> Asked:
    """Store a checked answer if its run is still the import's; DROPPED otherwise."""
    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        # Read once the write lock is held: an answer that waited past a deadline is judged as it is stored.
        now = clock()
        current = db.get(ImportStructuring, import_id)

        if (
            row is None
            or row.owner_id != owner_id
            or not is_open(db, row, now)
            or row.revision != attempt.import_revision
            or current is None
            or current.state != StructuringState.RUNNING
            or current.attempt != attempt.id
        ):
            return Asked.DROPPED

        current.problems = checked.problems or None
        current.finished_at = now

        if not checked.candidates:
            current.state = StructuringState.FAILED
            current.outcome = checked.failure

            return Asked.FAILED

        for position, found in enumerate(checked.candidates, start=1):
            db.add(
                ImportCandidate(
                    id=new_id(),
                    import_id=row.id,
                    owner_id=owner_id,
                    position=position,
                    kind=found.kind,
                    extracted=found.extracted,
                    edits=None,
                    cautions=found.cautions,
                    state=CandidateState.PENDING,
                    revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )

        current.state = StructuringState.DONE
        current.outcome = None
        record_event(db, owner_id, row.id, IMPORT_OBJECT, row.id, "import_structured", now)

        return Asked.STORED


def structure_import(
    database: Database, structurer: Structurer, owner_id: str, import_id: str, clock: Callable[[], datetime]
) -> Asked:
    """Ask for candidates once: start a run, let the structurer answer, check and store the answer."""
    asked, attempt = start_structuring(database, owner_id, import_id, structurer.name, clock())

    if attempt is None:
        return asked

    try:
        answer = structurer.structure(attempt.segments)
    except Exception as error:  # noqa: BLE001 - any failure keeps nothing; its message may hold the text
        logger.warning("structuring an import failed (%s)", type(error).__name__)
        checked = Checked([], [], SERVICE_FAILED)
    else:
        checked = check_answer(answer, attempt.segments)

    return finish_structuring(database, owner_id, import_id, attempt, checked, clock)


def can_ask_again(structuring: ImportStructuring | None, now: datetime) -> bool:
    if structuring is None or structuring.state == StructuringState.FAILED:
        return True

    return structuring.state == StructuringState.RUNNING and now >= structuring.started_at + STALE_STRUCTURING_AFTER


# --- reviewing them ------------------------------------------------------------------


def candidates_of(db: Session, owner_id: str, import_id: str) -> list[ImportCandidate]:
    return list(
        db.scalars(
            select(ImportCandidate)
            .where(ImportCandidate.import_id == import_id, ImportCandidate.owner_id == owner_id)
            .order_by(ImportCandidate.position)
        )
    )


def open_candidate(
    db: Session, owner_id: str, import_id: str, candidate_id: str, now: datetime
) -> tuple[ResumeImport, ImportCandidate]:
    """A candidate of an open import of this owner; NotFound otherwise."""
    row = open_import(db, owner_id, import_id, now)
    candidate = db.scalar(
        select(ImportCandidate).where(
            ImportCandidate.id == candidate_id,
            ImportCandidate.import_id == row.id,
            ImportCandidate.owner_id == owner_id,
        )
    )

    if candidate is None:
        raise NotFound()

    return row, candidate


def save_edits(
    database: Database,
    owner_id: str,
    import_id: str,
    candidate_id: str,
    expected_revision: int,
    values: dict[str, str],
    now: datetime,
) -> bool:
    """Keep the user's words for the fields that differ from what was found; False if nothing changed."""
    with database.transaction() as db:
        _, candidate = open_candidate(db, owner_id, import_id, candidate_id, now)

        if candidate.revision != expected_revision:
            raise CandidateConflict(CandidateSnapshot.of(candidate))

        if candidate.state != CandidateState.PENDING:
            raise NotEditable(candidate.state)

        edits = changed_fields(candidate.kind, candidate.extracted, values) or None

        if edits == candidate.edits:
            return False

        candidate.edits = edits
        candidate.revision += 1
        candidate.updated_at = now

    return True


def set_candidate_state(
    database: Database,
    owner_id: str,
    import_id: str,
    candidate_id: str,
    expected_revision: int,
    state: str,
    now: datetime,
) -> bool:
    """Leave a candidate out or take it back; False if it already was (a second click).

    An adopted candidate stays adopted: what it wrote is in the user's records.
    """
    with database.transaction() as db:
        _, candidate = open_candidate(db, owner_id, import_id, candidate_id, now)

        if candidate.state == CandidateState.ADOPTED:
            raise NotEditable(candidate.state)

        if candidate.state == state:
            return False

        if candidate.revision != expected_revision:
            raise CandidateConflict(CandidateSnapshot.of(candidate))

        candidate.state = state
        candidate.revision += 1
        candidate.updated_at = now

    return True


# --- where adopted candidates went --------------------------------------------------------------


def record_kind(kind: str) -> str:
    """The kind of record a candidate of this kind is written into: its own, or the profile."""
    return kind if kind in EXPERIENCE_CATEGORIES else PROFILE


@dataclass(frozen=True)
class AdoptionView:
    """Where an adopted candidate went, as a page or an export says it."""

    action: str
    item_id: str
    category: str
    # None once that record is in the recycle bin or gone: no link leads to it then.
    title: str | None
    # The labels of the fields written.
    fields: tuple[str, ...]
    at: datetime


def adoption_views(db: Session, owner_id: str, candidates: list[ImportCandidate]) -> dict[str, AdoptionView]:
    """For each adopted candidate, where it went and whether that record is still there."""
    kinds = {candidate.id: candidate.kind for candidate in candidates if candidate.state == CandidateState.ADOPTED}

    if not kinds:
        return {}

    views = {}

    for adoption in db.scalars(
        select(ImportAdoption).where(ImportAdoption.owner_id == owner_id, ImportAdoption.candidate_id.in_(list(kinds)))
    ):
        category = record_kind(kinds[adoption.candidate_id])
        item = db.get(ExperienceItem, adoption.item_id)
        here = item is not None and item.owner_id == owner_id and item.deleted_at is None
        labels = {entry.name: entry.label for entry in CATEGORY_FIELDS[category]}
        views[adoption.candidate_id] = AdoptionView(
            action=adoption.action,
            item_id=adoption.item_id,
            category=category,
            title=display_title(item.content, item.category) if here else None,
            fields=tuple(labels.get(name, name) for name in adoption.fields),
            at=adoption.created_at,
        )

    return views


# --- comparing with what the user has -------------------------------------------------------


NEW = "new"
DUPLICATE = "duplicate"
CONFLICTING = "conflicting"
SAME = "same"
CONTAINED = "contained"
FILLS = "fills"
DIFFERS = "differs"


@dataclass
class Match:
    """An experience (or the profile) a candidate may be about, field by field."""

    item_id: str
    title: str
    category: str
    # (field name, SAME/CONTAINED/FILLS/DIFFERS, what the record says there)
    fields: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class Comparison:
    label: str
    matches: list[Match] = field(default_factory=list)


def compare(db: Session, owner_id: str, candidates: list[ImportCandidate]) -> dict[str, Comparison]:
    """For each candidate still waiting, the records it may be about; nothing is written.

    Finding them and comparing them are two steps. A record is found by its
    name alone (school, organisation, title), loosely (name_key): the same
    name, or one name inside the other from CONTAINED_FROM characters on.
    That is a clue, not a finding, so every record found is listed. Its
    fields are then compared strictly (field_state), and so is the profile.
    """
    experiences = list_experiences(db, owner_id)
    profile = current_document(db, owner_id, PROFILE)
    found = {}

    for candidate in candidates:
        if candidate.state != CandidateState.PENDING or candidate.kind in REFERENCE_KINDS:
            continue

        values = current_values(candidate.kind, candidate.extracted, candidate.edits)

        if candidate.kind == PROFILE:
            found[candidate.id] = _compare_profile(values, profile)
        else:
            found[candidate.id] = _compare_experience(candidate.kind, values, experiences)

    return found


def name_key(text: str) -> str:
    """A name as matched to find records: full and half width alike, case, spaces and punctuation ignored.

    Only for finding: it makes C++ and C alike, so it never says whether
    two fields agree.
    """
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def content_key(text: str) -> str:
    """A field's words as compared: only full-width ASCII, spacing and line breaks made alike.

    Every other character stays as written, so each symbol, digit, letter
    case and word counts: C++ is not C, -1 is not 1, 10⁵ is not 105, and
    没有使用 Docker is not 使用 Docker. No compatibility folding (NFKC)
    happens here: it would turn superscripts, subscripts, circled numbers
    and fractions into plain digits.
    """
    return CJK_SPACE.sub("", " ".join(text.translate(FULL_WIDTH).split()))


def field_state(entry: Field, value: str, existing: str) -> str:
    """How a candidate's field stands against a record's: FILLS, SAME, CONTAINED or DIFFERS.

    CONTAINED only for a field of several lines, when each of its lines is
    a whole line of the record's field; words found anywhere else in it
    prove nothing. What is not plainly the same differs, and the page shows
    both texts for the user to judge.
    """
    if not existing:
        return FILLS

    if content_key(existing) == content_key(value):
        return SAME

    lines = {content_key(line) for line in value.split("\n") if line.strip()}

    if entry.multiline and lines and lines <= {content_key(line) for line in existing.split("\n")}:
        return CONTAINED

    return DIFFERS


def _compare_experience(kind: str, values: dict[str, str], experiences: list[ExperienceItem]) -> Comparison:
    key = name_key(values.get(TITLE_FIELDS[kind], ""))
    matches = []

    if key:
        for item in experiences:
            other = name_key(item.content.get(TITLE_FIELDS[kind], "")) if item.category == kind else ""

            if other and (
                other == key or (min(len(other), len(key)) >= CONTAINED_FROM and (other in key or key in other))
            ):
                matches.append(
                    Match(item.id, display_title(item.content, kind), kind, _field_states(kind, values, item.content))
                )

    return Comparison(_label(matches), matches)


def _compare_profile(values: dict[str, str], profile: ExperienceItem | None) -> Comparison:
    if profile is None:
        return Comparison(NEW)

    matches = [Match(profile.id, KIND_LABELS[PROFILE], PROFILE, _field_states(PROFILE, values, profile.content))]

    return Comparison(_label(matches), matches)


def _field_states(kind: str, values: dict[str, str], content: dict) -> list[tuple[str, str, str]]:
    states = []

    for entry in CANDIDATE_FIELDS[kind]:
        value, existing = values.get(entry.name, ""), content.get(entry.name, "")

        if value:
            states.append((entry.name, field_state(entry, value, existing), existing))

    return states


def _label(matches: list[Match]) -> str:
    if not matches:
        return NEW

    if any(state == DIFFERS for match in matches for _, state, _ in match.fields):
        return CONFLICTING

    return DUPLICATE


# --- taking it away ---------------------------------------------------------------------


def review_record(
    row: ResumeImport,
    structuring: ImportStructuring,
    candidates: list[ImportCandidate],
    adoptions: dict[str, AdoptionView] | None = None,
    draft: ImportDraft | None = None,
) -> dict:
    """The candidates of an import and the user's review, as data a person or a tool can read.

    What was found and what the user wrote stay apart, each with its
    sources; the dropped candidates are listed by place and reason only. An
    adopted candidate says where it went and which fields were written.
    How the import is kept comes from its draft row, if it was saved.
    """
    adoptions = adoptions or {}
    segments = row.segments or []

    return {
        "import_id": row.id,
        "note": (
            "这些是从导入的简历文字里整理出的候选，没有经过核实；只有你明确选择加入的，才写进了你的资料（见 adoption）。"
            "found 是逐字摘自原文的内容，sources 是它的出处；edited 是你自己改写的内容，不是原文引用。"
            "技能只在你选择时作为自述写进基本资料；当时的求职意向只作参考，不会改动求职偏好。"
        ),
        "structured_by": STRUCTURER_LABELS.get(structuring.structurer, structuring.structurer),
        "structurer": structuring.structurer,
        "contract_version": structuring.contract_version,
        "state": structuring.state,
        "outcome": structuring.outcome,
        "outcome_text": FAILURE_TEXTS.get(structuring.outcome) if structuring.outcome else None,
        "started_at": _iso(structuring.started_at),
        "finished_at": _iso(structuring.finished_at),
        **kept_record(row, draft),
        "dropped": [
            {
                "position": problem["position"],
                "kind": problem["kind"],
                "kind_label": KIND_LABELS.get(problem["kind"]) if problem["kind"] else None,
                "where": [locator(segments[number - 1]) for number in problem["segments"] if number <= len(segments)],
                "reason": problem["reason"],
                "reason_text": PROBLEM_TEXTS.get(problem["reason"], problem["reason"]),
            }
            for problem in structuring.problems or []
        ],
        "candidates": [_candidate_record(candidate, segments, adoptions.get(candidate.id)) for candidate in candidates],
    }


def kept_record(row: ResumeImport, draft: ImportDraft | None) -> dict:
    """How an import is kept, for an export: temporary until its expiry, saved, or in the recycle bin (U2-B3a)."""
    if draft is None:
        return {"kept_as": "temporary", "saved_at": None, "kept_until": _iso(row.expires_at)}

    if draft.deleted_at is None:
        return {"kept_as": "saved", "saved_at": _iso(draft.saved_at), "kept_until": None}

    return {"kept_as": "binned", "saved_at": _iso(draft.saved_at), "kept_until": _iso(draft.purge_after)}


def _candidate_record(candidate: ImportCandidate, segments: list, adoption: AdoptionView | None) -> dict:
    edits = candidate.edits or {}
    fields = {}

    for entry in CANDIDATE_FIELDS[candidate.kind]:
        found = extracted_value(candidate.extracted, entry.name)

        if not found and entry.name not in edits:
            continue

        fields[entry.name] = {
            "label": entry.label,
            "found": found or None,
            "sources": [
                {"where": where(segments, piece), "segment": piece["segment"], "quote": piece["quote"]}
                for piece in candidate.extracted.get(entry.name, [])
            ],
            "edited": edits.get(entry.name),
        }

    return {
        "id": candidate.id,
        "position": candidate.position,
        "kind": candidate.kind,
        "kind_label": KIND_LABELS[candidate.kind],
        "reference_only": candidate.kind == INTENTION,
        "state": candidate.state,
        "state_label": STATE_LABELS.get(candidate.state, candidate.state),
        "adoption": None if adoption is None else {
            "action": adoption.action,
            "action_label": ACTION_LABELS.get(adoption.action, adoption.action),
            "item_id": adoption.item_id,
            "item_title": adoption.title,
            "item_gone": adoption.title is None,
            "fields": list(adoption.fields),
            "at": _iso(adoption.at),
        },
        "fields": fields,
        "cautions": [
            {"field": caution["field"], "code": caution["code"], "text": CAUTION_TEXTS.get(caution["code"], "")}
            for caution in candidate.cautions
        ],
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
