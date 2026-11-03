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
the preferences, and nothing is sent anywhere from here: the local
simulation answers in this process, and a real service (U2-C3) is asked
only by app.live_structuring, in the background, through the same run records
(start_structuring, still_current, finish_structuring, end_attempt).
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
    INVALID_ANSWER,
    KIND_LABELS,
    MAX_CANDIDATES,
    NONE_VALID,
    NOTHING_FOUND,
    PROBLEM_TEXTS,
    REFERENCE_KINDS,
    SERVICE_FAILED,
    TOO_MANY,
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
from app.library.imports import IMPORT_OBJECT, STALE_STRUCTURING_AFTER, draft_of, is_open, locator, open_import
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
from app.library.sends import Call, Delivery, Sends, sends_text
from app.library.service import current_document, list_experiences, new_id, record_event
from app.library.structuring_runs import Stop

logger = logging.getLogger("roleradar.library")

STRUCTURABLE = (ImportState.READ, ImportState.PARTIAL)
STRUCTURER_LABELS = {SIMULATION_NAME: "本机模拟整理规则（不是 AI）"}
# A real service's structurer is named live:<provider>/<model>/<prompt version> (app.live_structuring, U2-C3).
LIVE_PREFIX = "live:"
# What came of a real run that gave no candidates. Its outcome ends in what the run handed on
# (sent_suffix), so each text says whether anything was sent, which kind, and how sure that is.
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_CONFIG = "provider_config"
PROVIDER_TIMEOUT = "provider_timeout"
PROVIDER_FAILED = "provider_failed"
INTERNAL_ERROR = "internal_error"
# A run of a process that is gone, cancelled after a restart: whether it was sent is not known here.
INTERRUPTED = "interrupted"
LIVE_FAILURE_TEXTS = {
    "cancelled": "这次整理已取消。",
    "logged_out": "退出登录时，这次整理停止了。",
    "expired": "这次导入已经到期，整理停止了。",
    "run_timeout": f"这次整理超过了 {int(STALE_STRUCTURING_AFTER.total_seconds() // 60)} 分钟的运行时限，停止了。",
    "ended": "这次导入已经结束或有了变化，整理停止了。",
    "replaced": "另一次整理已经开始，这次停止了。",
    "shutdown": "应用停止时，这次整理中断了。",
    "gate_stopped": "真实服务已停用。",
    "gate_paused": "这批批准已暂停，继续需要新的批准。",
    "gate_busy": "另一个真实任务正在进行，请等它结束后再整理。",
    "gate_purpose": "这批批准不包括简历整理。",
    "gate_requests": "这批批准的请求次数已经用完。",
    "gate_cost": "这次请求的保守预留会超出这批批准剩下的应用内预算。",
    "gate_closed": "服务正在停止。",
    "gate_ended": "这次整理已经结束。",
    "gate_count": "发送前的计数没有完成，或者结果不能用、用完了这次请求的时限，所以没有发出生成请求。",
    "gate_time": "本机的准备和发送前检查用完了这次请求的时限，所以没有发出生成请求。",
    "gate_input": "计数显示这次请求的输入超过了这批批准的单次输入上限，所以没有发出生成请求。",
    "gate_payload": "这次请求的完整内容超过了这批批准允许的大小，没有发出，也不会被截短。可以只粘贴简历里需要的部分，重新导入后再整理。",
    PROVIDER_UNAVAILABLE: "外部服务无法使用（无法建立，或和批准的接收方不符）。",
    PROVIDER_CONFIG: "外部服务的本机设置有问题，请求在发出之前就停止了。",
    PROVIDER_TIMEOUT: "外部服务没有在时限内回答。",
    # An unfinished answer, an error response, a refusal or no text: which one is not said.
    PROVIDER_FAILED: "外部服务没有返回可用的整理结果。",
    INTERNAL_ERROR: "整理时程序出错。",
    INVALID_ANSWER: "外部服务返回的整理结果不是约定的格式。",
    # The service's candidates and the local profile together: more than the contract allows is refused whole.
    TOO_MANY: f"整理出的条目（连同本机规则整理的基本资料）超过了 {MAX_CANDIDATES} 条的上限。可以只粘贴简历里需要的部分，重新导入后再整理。",
    # A text with too little in it may end so (an expected outcome, no failure of the service): ask for more.
    NOTHING_FOUND: "外部服务没有整理出任何条目：这份文字里可能没有足够的经历信息，可以补充更完整的内容后重新导入。",
    NONE_VALID: "外部服务整理出的条目都没有通过原文核对。",
}
SENT_TEXT = "已交给外部服务的请求：{}。这些请求可能已经产生费用；什么都没有保存。"
NOT_SENT_TEXT = "没有发出请求，什么都没有保存。"
INTERRUPTED_TEXT = "这次整理已经中断（应用可能重启过），结果不会保存；中断之前，计数请求和生成请求都可能已经发出。"
# The marks of sent_suffix: sent, maybe sent, not sent.
SENT_MARKS = {"s": Delivery.SENT, "m": Delivery.MAYBE, "-": Delivery.NOT_SENT}
# What an outcome ended in before counts existed (U2-C3, structure version 8 all along): its one
# request, a generation, went out or may have. It is read, never written.
LEGACY_SENT = "_sent"
# The outcomes of a stopped run (structuring_runs.Stop): a cancel writes its own at once, before
# its request has come back, so only these may still be set right by what the request showed.
STOPPED = frozenset(stop.value for stop in Stop)
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
    # When it began, and when its import stops being in use (None: a saved draft does not expire).
    started_at: datetime | None = None
    valid_until: datetime | None = None


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
    database: Database,
    owner_id: str,
    import_id: str,
    structurer_name: str,
    clock: Callable[[], datetime],
    *,
    prepare: Callable[[ResumeImport, ImportStructuring | None], None] | None = None,
    begin: Callable[[Attempt], None] | None = None,
) -> tuple[Asked | None, Attempt | None]:
    """Begin a run, or say why none begins; NotFound if the import is not open (or is another's).

    The time is read once the transaction holds the write lock, so a request
    that waited for it is judged by the moment it acts. prepare(row, current),
    if given, runs once a run may begin and before anything is written, with
    the record of the run this one would replace (or None): whatever it raises
    begins nothing. A real run's confirmation is checked there (U2-C3): it
    names the run it replaces, so the run begun here uses it up. begin(attempt), if given, runs once the run is written and
    before it is committed: a real run is entered in its registry there, so
    no stop can miss it once it exists.
    """
    with database.transaction() as db:
        # The first statement takes the write lock; the clock is read after it.
        db.get(ResumeImport, import_id)
        now = clock()
        row = open_import(db, owner_id, import_id, now)

        if row.state not in STRUCTURABLE or not row.segments:
            return Asked.NOT_READ, None

        current = db.get(ImportStructuring, row.id)

        if current is not None:
            if current.state == StructuringState.DONE:
                return Asked.ALREADY, None

            if current.state == StructuringState.RUNNING and now < current.started_at + STALE_STRUCTURING_AFTER:
                return Asked.RUNNING, None

        if prepare is not None:
            prepare(row, current)

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
        attempt = Attempt(
            current.attempt,
            row.revision,
            copy.deepcopy(row.segments),
            started_at=now,
            valid_until=None if draft_of(db, row.id) is not None else row.expires_at,
        )

        if begin is not None:
            begin(attempt)

        return None, attempt


def still_current(database: Database, owner_id: str, import_id: str, attempt: Attempt, clock: Callable[[], datetime]) -> bool:
    """Whether a run may still send: its import open and unchanged, and the run the import's, running and in time.

    Read once the write lock is held. A real run asks this before its gate
    reserves anything (U2-C3); the last word before its request is handed on
    is its registry's (structuring_runs).
    """
    with database.transaction() as db:
        row = db.get(ResumeImport, import_id)
        now = clock()
        current = db.get(ImportStructuring, import_id)

        return (
            row is not None
            and row.owner_id == owner_id
            and is_open(db, row, now)
            and row.revision == attempt.import_revision
            and current is not None
            and current.state == StructuringState.RUNNING
            and current.attempt == attempt.id
            and now < current.started_at + STALE_STRUCTURING_AFTER
        )


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
            # A run past its time is over: asking again may already be allowed (U2-C3).
            or now >= current.started_at + STALE_STRUCTURING_AFTER
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
    asked, attempt = start_structuring(database, owner_id, import_id, structurer.name, clock)

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


def end_attempt(
    database: Database, owner_id: str, import_id: str, attempt_id: str, outcome: str, clock: Callable[[], datetime]
) -> bool:
    """End a run that gave no candidates with its outcome; False if it is no longer the import's running run."""
    with database.transaction() as db:
        current = db.get(ImportStructuring, import_id)
        now = clock()

        if (
            current is None
            or current.owner_id != owner_id
            or current.state != StructuringState.RUNNING
            or current.attempt != attempt_id
        ):
            return False

        current.state = StructuringState.FAILED
        current.outcome = outcome
        current.problems = None
        current.finished_at = now

        return True


def reconcile_sent(database: Database, owner_id: str, import_id: str, attempt_id: str, sends: Sends) -> bool:
    """Make a stopped run's outcome say what its requests showed once they came back (U2-C3); True if it changed.

    A cancel is written at once, while a request of the run may still be
    out, and counts that one as maybe sent. When it comes back, its
    provider's evidence decides (app.live_gate.delivery): an answer makes it
    sent; only one explicitly not sent, with nothing reported, takes it off;
    a timeout or anything unknown keeps it as maybe. The stop stays the outcome. Only
    that run's own record is changed, while a stop still ends it: never a
    newer run's, and a record that is gone stays gone.
    """
    with database.transaction() as db:
        current = db.get(ImportStructuring, import_id)

        if (
            current is None
            or current.owner_id != owner_id
            or current.attempt != attempt_id
            or current.state != StructuringState.FAILED
            or current.outcome is None
            or split_outcome(current.outcome)[0] not in STOPPED
        ):
            return False

        outcome = split_outcome(current.outcome)[0] + sent_suffix(sends)

        if outcome == current.outcome:
            return False

        current.outcome = outcome

        return True


def is_live(structurer: str) -> bool:
    return structurer.startswith(LIVE_PREFIX)


def running_here(runs, structuring: ImportStructuring | None, now: datetime) -> Sends | None:
    """For a real run of this process still going on: its requests handed on so far (runs.handed); else None."""
    if structuring is None or structuring.state != StructuringState.RUNNING or not is_live(structuring.structurer):
        return None

    if can_ask_again(structuring, now):
        return None

    return runs.handed(structuring.attempt)


def structurer_label(structurer: str) -> str:
    """Who structured an import, as the pages and the export say it."""
    if structurer in STRUCTURER_LABELS:
        return STRUCTURER_LABELS[structurer]

    if is_live(structurer):
        provider, _, rest = structurer.removeprefix(LIVE_PREFIX).partition("/")
        model = rest.rpartition("/")[0]

        return f"外部 AI 服务（{provider} {model}）"

    return "整理服务"


def failure_text(structurer: str, outcome: str | None) -> str | None:
    """What the outcome of a run says, for the local rules or for a real service."""
    if outcome is None:
        return None

    if not is_live(structurer):
        return FAILURE_TEXTS.get(outcome)

    if outcome == INTERRUPTED:
        return INTERRUPTED_TEXT

    base, sends = split_outcome(outcome)
    text = LIVE_FAILURE_TEXTS.get(base)

    if text is None:
        return None

    return text + (SENT_TEXT.format(sends_text(sends)) if sends.total else NOT_SENT_TEXT)


def sent_suffix(sends: Sends) -> str:
    """What a real run handed on, written after its outcome: "_", a mark for its count, then one for
    its generation, each "s" sent, "m" maybe sent or "-" not sent (SENT_MARKS); nothing when nothing went
    out. A run hands on one of each at most."""
    if not sends.total:
        return ""

    return "_" + _mark(sends.count_sent, sends.count_maybe) + _mark(sends.generation_sent, sends.generation_maybe)


def split_outcome(outcome: str) -> tuple[str, Sends]:
    """An outcome's own code, and what its run handed on (sent_suffix).

    One written before counts existed (LEGACY_SENT) says only that its
    generation went out or may have: it is read as maybe sent, and as no
    count, which runs of then never made.
    """
    if outcome.endswith(LEGACY_SENT):
        return outcome.removesuffix(LEGACY_SENT), Sends(generation_maybe=1)

    base, underscore, marks = outcome.rpartition("_")

    if not underscore or len(marks) != 2 or marks == "--" or not all(mark in SENT_MARKS for mark in marks):
        return outcome, Sends()

    return base, Sends().plus(Call.COUNT, SENT_MARKS[marks[0]]).plus(Call.GENERATION, SENT_MARKS[marks[1]])


def _mark(sent: int, maybe: int) -> str:
    return "s" if sent else "m" if maybe else "-"


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
        "structured_by": structurer_label(structuring.structurer),
        "structurer": structuring.structurer,
        "contract_version": structuring.contract_version,
        "state": structuring.state,
        "outcome": structuring.outcome,
        "outcome_text": failure_text(structuring.structurer, structuring.outcome),
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
