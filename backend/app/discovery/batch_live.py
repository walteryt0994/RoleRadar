"""U3-D6: one batch of real reads and real model requests, under the Owner's approvals, shared by every operation of
one run day in one run directory (D6 pre-check, revision 1, section 3). Built offline; nothing here reads or sends
by itself, and nothing here admits a source or grants a permission: the approvals are the Owner's, given in the
environment the app is started with (load), and checked at start, before every operation and before every request.

Two launches use one run directory and its library (the D6 plan, 1.2): the direction launch with a model approval
for direction alone (u3d6-dir-<date>-<letter>), then the discovery launch with the source's admission (u3d6-admit-),
one read approval (u3d6-src-) and a model approval for discovery alone (u3d6-ai-). No other ids are taken: the
approvals of earlier batches (u3d2e-, u3d5a-) are never used again.

What makes it a batch, and not one approval a search as validation B had (app.discovery.lever_live, model_live):

- Each approval is still claimed once and never given back: a record made atomically under its id in the ledger
  under HOME (LEDGER_IN_HOME), now naming the batch: the run directory, the batch's id, the user it is bound to, the
  digest of its terms. The same record is kept in the run directory (APPROVALS_DIR). A record of another run
  directory, batch, user or terms keeps the app from starting, and every operation and request from going.
- Each operation is the user's own (begin): a search confirmed on its review, a 现在核对, one posting's check, a
  申请前核对, a 现在查新, a link's read, a direction exploration. One runs at a time; it may start only when what is
  left of the batch holds its own most (OPERATION_REQUESTS, and for a search or an exploration the model requests and
  the worst cost its reservations could reach), so no operation depends on anyone pressing cancel in time. robots is
  read once a launch, before the first request of its first operation that reads, and charged to the batch on top of
  that operation's own most: an operation's reservation never pays for it.
- What was used is counted from what is on disk, never from memory alone: the source's requests from the request
  record (REQUEST_LOG; a request counts once its pre-send record is written, whether or not it then went), the model's
  from the gate's ledger (app.live_gate). A cancellation, a crash or a restart gives nothing back.
- The spacing between requests (INTERVAL) and the failures in a row (FAILURES_IN_A_ROW) are the batch's, not an
  operation's: they run on across operations and, from the request record, across a restart.
- The batch stops for good (STOP_RECORD, written once) on LIVE_STOP, an anomaly of the source (robots, a refusal, a
  status, a reply not as documented), FAILURES_IN_A_ROW failures, a record that cannot be written, or the model's
  approval paused by its gate; a stop of either side stops both. A restart does not lift it.
- A restart is allowed only in the same run directory, on the run date, with the same approvals and terms, for the
  same user, the records whole and consistent, nothing stopped and the model's approval not paused (load): what an
  operation running at the crash used stays used, and that operation is recorded as interrupted.
- The records are checked whole and against each other (D6-0 R1, F2): at start every claim of the batch, both
  launches', in HOME and in the run directory alike; the task record against the request record (each operation's
  requests); both against HOME's progress (PROGRESS_SUFFIX), which only grows. While the app runs, the bytes the
  batch itself wrote to each of them are what must be there: checked before an operation starts and before every
  request of the source or the model is handed on. Anything else (a line cut, damaged, added or taken away, a claim
  changed or gone) stops the batch for good, in the run directory and in HOME.
- The model's ledger is the library's, and the batch's (D6-0 R1, F1): every claim names the library it was made with
  (its id, app.library.models.read_library_id), and the ledger must show at least what HOME's progress says the
  batch's model approvals, this launch's and the other's, have settled. A library swapped, or put back from a copy
  made before, does not start.
- One process at a time: the run directory's lock (LOCK_FILE) is held for the app's life.

The records are metadata only: kinds of address, page numbers, codes, counts and times; never an address, a posting
id, a posting's words, a prompt or a key.
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

from app.discovery import lever_live
from app.discovery.contracts import Access, EvidenceLevel, Limits, PostingRef
from app.discovery.lever import (
    MIN_REQUEST_SECONDS,
    REQUEST_SECONDS,
    STOP_STATUSES,
    BadReply,
    HostStopped,
    LeverBoard,
    TimeUsedUp,
    Transport,
    TransportError,
    TransportHeldBack,
    TransportRefused,
    TransportTimeout,
    TransportTooLarge,
)
from app.discovery.sources import HeldBack, Permission, Register, SourceAnomaly, SourceEntry, SourceError, SourceTimeout
from app.job_analysis.service import LIVE_NOTICE
from app.live_gate import ApprovalTerms, Purpose, reservation

KEY = "lever-palantir-batch"
# How the app is started for a launch of the batch: unset, nothing of this module is used.
LAUNCH_VARIABLE = "ROLERADAR_BATCH_LAUNCH"
# The boards an admission may name: the code's allow-list, never widened by the environment.
BOARDS = {"palantir": lever_live.BOARD}
# What one read approval of a batch may allow at most (D6 plan, 8.2: 45 by the real paths, 5 to spare).
MAX_REQUESTS = 50
MAX_ADMISSION_DAYS = 31
INTERVAL = 2.0
FAILURES_IN_A_ROW = 2
# A search on the admitted source reads and checks four postings at most and fully analyses one (D6 plan, 8.1).
LIMITS = Limits(reads=4, checks=4, analyses=1)
STOP_FILE = lever_live.STOP_FILE
REQUEST_LOG = lever_live.REQUEST_LOG
TASK_LOG = "tasks.jsonl"
TRAJECTORY_LOG = "trajectories.jsonl"
STOP_RECORD = "batch-stop.json"
LOCK_FILE = "batch.lock"
APPROVALS_DIR = "approvals"
LEDGER_IN_HOME = Path(".roleradar", "batch-approvals")
ADMISSION_ID = re.compile(r"u3d6-admit-(\d{4}-\d{2}-\d{2})-[a-z]")
SOURCE_APPROVAL_ID = re.compile(r"u3d6-src-(\d{4}-\d{2}-\d{2})-[a-z]")
MODEL_APPROVAL_IDS = {
    Purpose.DISCOVERY: re.compile(r"u3d6-ai-(\d{4}-\d{2}-\d{2})-[a-z]"),
    Purpose.DIRECTION: re.compile(r"u3d6-dir-(\d{4}-\d{2}-\d{2})-[a-z]"),
}
MODEL_ID_FORMS = {Purpose.DISCOVERY: "u3d6-ai-<日期>-<字母>", Purpose.DIRECTION: "u3d6-dir-<日期>-<字母>"}
COMMON_VARIABLES = {"run_date": "ROLERADAR_BATCH_RUN_DATE", "run_dir": "ROLERADAR_BATCH_RUN_DIR"}
SOURCE_VARIABLES = {
    "admission_id": "ROLERADAR_BATCH_ADMISSION_ID",
    "admission_until": "ROLERADAR_BATCH_ADMISSION_UNTIL",
    "approval_id": "ROLERADAR_BATCH_SOURCE_APPROVAL_ID",
    "board": "ROLERADAR_BATCH_BOARD",
    "max_requests": "ROLERADAR_BATCH_MAX_REQUESTS",
}
# The save permission of the admitted source (D3 plan, 2; D6 plan, 1.3): unset is none, so nothing is kept unless the
# admission says so; any other value stops the app from starting.
SAVE_VARIABLE = "ROLERADAR_BATCH_SAVE"
REPO = lever_live.REPO
# A decision's own limits, as validation B's (B pre-check, 7.2), and the decisions a search may make.
DECISION_OUTPUT_TOKENS = 2_000
DECISION_REQUEST_BYTES = 15_000
DECISIONS_PER_SEARCH = 8
# The generations of one full analysis: the posting parsed, then compared (D6 plan, 8.1).
GENERATIONS_PER_ANALYSIS = 2
# What the review says of the model and of the batch (D6). Changing a word changes the version, and with it every
# confirmation's fingerprint; its second and third paragraphs are the job analysis pages' own (live-3).
NOTICE_VERSION = "discovery-batch-1"
NOTICE = (
    "确认后，这次查找每一步的决策请求和每个岗位的分析请求，都会发给上面写明的外部 AI 服务，只用于这一次查找；"
    "岗位来源另按读取批准受控读取。按服务商的政策，API 数据默认不用于训练模型。",
    LIVE_NOTICE[1],
    LIVE_NOTICE[2],
    "这一批的读取批准和模型批准，只在这一个运行日、这一个运行目录里，由你的各项操作共用：用掉的次数和费用，停止、"
    "失败或重启都不会恢复，不补跑。每次查找仍要先核对、再确认；核对之后这一批的剩余次数或预算变了，原来的核对作废。",
)
_REQUEST_STATES = frozenset({"claimed", "handed", "withdrawn", "refused", "answered", "failed"})
_RESULT_STATES = frozenset({"withdrawn", "answered", "failed"})
# In HOME's ledger, beside the claims, two records of each batch (D6-0 R1, F1 and F2), never in the run directory, so
# that a run directory or a library put back from a copy cannot take the batch back with them:
# - PROGRESS: one line each time the batch's use of an approval moves on, before it is sent (the source's requests
#   handed on; the model's requests settled or of unknown cost, and what they are charged): it only ever grows. A run
#   directory or a ledger showing less than it has been rolled back or cut, and the batch does not go on;
# - HOME_STOP: the batch's stop, beside the run directory's own (STOP_RECORD): one of the two is enough to stop it.
PROGRESS_SUFFIX = ".progress.jsonl"
HOME_STOP_SUFFIX = ".stop.json"
# What a progress line says of each kind of approval: the source's requests handed on; the model's requests settled or
# of unknown cost, and what they are charged (never a reservation: one can be released, so it may go down).
PROGRESS_KEYS = {"source": ("handed",), "model": ("final_requests", "final_micro_usd")}
# And each model request, noted right before it is handed on (D6-0 R2): its id in the gate's ledger and what it is
# reserved at. Whatever comes of it (settled, of unknown cost, released as never sent, or nothing at all, at a crash),
# the ledger must still hold it: a library without it is one from before it, and does not start.
REQUEST_KIND = "model_request"
# The states of a request in the gate's ledger that mean its approval has been paused for good (app.live_gate).
PAUSING_STATES = frozenset({"over", "unknown"})


class Launch(StrEnum):
    DISCOVERY = "discovery"
    DIRECTION = "direction"


class SavePermission(StrEnum):
    """What may be kept of the admitted source's postings (D3 plan, 2), as its admission says."""

    NONE = "none"
    FACTS = "facts"
    FACTS_QUOTES = "facts_quotes"


class OperationKind(StrEnum):
    SEARCH = "search"
    RECHECK = "recheck"
    CHECK = "check"
    APPLY = "apply"
    WATCH = "watch"
    SUPPLY = "supply"
    DIRECTION = "direction"


# The most requests to the source an operation may send (D6 plan, 8.2): a search reads three pages, four postings and
# checks four; 现在核对 checks at most three postings, three requests each; a check, a 申请前核对 or a link's read is one
# posting, three at most; 现在查新 reads one list, four at most. An exploration reads nothing.
OPERATION_REQUESTS = {
    OperationKind.SEARCH: 11,
    OperationKind.RECHECK: 9,
    OperationKind.CHECK: 3,
    OperationKind.APPLY: 3,
    OperationKind.WATCH: 4,
    OperationKind.SUPPLY: 3,
    OperationKind.DIRECTION: 0,
}
# How long an operation may take, from its start; its requests are given no more.
OPERATION_SECONDS = {
    OperationKind.SEARCH: 600.0,
    OperationKind.RECHECK: 180.0,
    OperationKind.CHECK: 60.0,
    OperationKind.APPLY: 60.0,
    OperationKind.WATCH: 60.0,
    OperationKind.SUPPLY: 60.0,
    OperationKind.DIRECTION: 300.0,
}
# The postings 现在核对 checks on the admitted source at most; the rest wait (D6 plan, 8.2).
RECHECK_POSTINGS = 3
# The operations that use the model, and those that read the source.
MODEL_OPERATIONS = frozenset({OperationKind.SEARCH, OperationKind.DIRECTION})
SOURCE_OPERATIONS = frozenset(kind for kind, most in OPERATION_REQUESTS.items() if most)


class BatchTermsError(ValueError):
    """The batch may not be used: problems says why, every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = tuple(problems)


class BatchRefused(Exception):
    """An operation may not start, or a request may not go, now: code says why in a word, the message in words.
    Nothing was sent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OperationSpent(HostStopped):
    """The operation used all its own requests or its time: it sends nothing more; the batch goes on."""


class BatchKeptBack(TransportRefused):
    """A request the batch kept back, and why (code). handed: its pre-send record was written, so it counts as used."""

    def __init__(self, code: str, message: str, *, handed: bool = False):
        super().__init__(message)
        self.code = code
        self.handed = handed


def today_here() -> date:
    return lever_live.today_here()


def launch_of(environ: Mapping[str, str]) -> Launch | None:
    """The launch the app is started for, or None; any other value stops it from starting (ValueError)."""
    value = (environ.get(LAUNCH_VARIABLE) or "").strip()

    if not value:
        return None

    try:
        return Launch(value)
    except ValueError:
        raise ValueError(
            f"{LAUNCH_VARIABLE} 只能不设，或者设为 {Launch.DIRECTION.value}（方向探索那次启动）或 {Launch.DISCOVERY.value}"
            "（找岗那次启动）；其他值不启用任何批准，网页没有启动。"
        ) from None


@dataclass(frozen=True)
class SourceTerms:
    """The admission and the read approval of a discovery launch, and the ledger their use goes in."""

    admission_id: str
    admission_until: date
    save: SavePermission
    approval_id: str
    run_date: date
    board: LeverBoard
    max_requests: int
    run_dir: Path
    ledger: Path

    @property
    def digest(self) -> str:
        payload = {
            "admission": [self.admission_id, self.admission_until.isoformat(), self.save.value],
            "approval": [self.approval_id, self.run_date.isoformat(), self.board.site, self.max_requests],
            "run_dir": str(self.run_dir),
            "robots": [lever_live.ROBOTS_URL, list(lever_live.ROBOTS_RULES)],
        }

        return _digest(payload)

    @property
    def claim_path(self) -> Path:
        return self.ledger / f"{self.approval_id}.claim"

    def expired(self, today: date) -> str | None:
        if today != self.run_date:
            return f"读取批准 {self.approval_id} 只在 {self.run_date}（美东）有效，今天是 {today}"

        if today > self.admission_until:
            return f"来源准入 {self.admission_id} 到 {self.admission_until} 为止，已经过期"

        return None


@dataclass(frozen=True)
class ModelTerms:
    """A model approval of the batch, for one purpose alone, and the ledger its use goes in."""

    approval_id: str
    purpose: Purpose
    run_date: date
    digest: str
    run_dir: Path
    ledger: Path

    @property
    def claim_path(self) -> Path:
        return self.ledger / f"{self.approval_id}.claim"

    def expired(self, today: date) -> str | None:
        if today != self.run_date:
            return f"模型批准 {self.approval_id} 只在 {self.run_date}（美东）有效，今天是 {today}"

        return None


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def model_digest(approval: ApprovalTerms) -> str:
    return _digest(approval.snapshot())


@dataclass
class Operation:
    """One operation of the batch, as begin made it: whose, which kind, its own most and what it has used."""

    id: str
    kind: OperationKind
    owner_id: str
    cancel: object
    requests: int
    seconds: float
    began: float
    robots: int = 0
    sent: int = 0
    ended: bool = False

    def cancelled(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    @property
    def budget(self) -> int:
        """Its own most and, if it reads robots for the launch, that one request on top."""
        return self.requests + self.robots


def load(
    environ: Mapping[str, str],
    launch: Launch,
    approval: ApprovalTerms | None,
    *,
    today: Callable[[], date] = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], datetime] | None = None,
    create: Callable[[], Transport] | None = None,
) -> "Batch":
    """The batch the environment names for this launch, checked as a start must be: the terms, the run directory, the
    approvals' records and the run's own, the lock. BatchTermsError with every problem found; nothing is read from the
    source, nothing sent, and nothing written but the lock and, after a crash, an interrupted operation's end."""
    clock = today or today_here
    today = clock()
    problems: list[str] = []
    home = (environ.get("HOME") or "").strip()
    common = {key: (environ.get(name) or "").strip() for key, name in COMMON_VARIABLES.items()}
    problems += [f"缺少 {COMMON_VARIABLES[key]}" for key, value in common.items() if not value]

    if not home or not Path(home).is_absolute():
        problems.append("缺少 HOME 或它不是绝对路径：用过的批准记在主目录下")

    if approval is None:
        problems.append("这次启动要用真实模型，但应用没有可用的模型批准：要设 ROLERADAR_ANALYSIS_MODE=live 和它的批准条款")

    if problems:
        raise BatchTermsError(problems)

    run_date = _day(common["run_date"], COMMON_VARIABLES["run_date"], problems)
    run_dir = Path(common["run_dir"])

    if not run_dir.is_absolute() or not run_dir.is_dir():
        problems.append(f"{COMMON_VARIABLES['run_dir']} 要写一个已经存在的目录的绝对路径")
    elif run_dir.resolve() == REPO or REPO in run_dir.resolve().parents:
        problems.append("运行目录不能放在仓库里")

    purpose = Purpose.DISCOVERY if launch is Launch.DISCOVERY else Purpose.DIRECTION

    if approval.purposes != frozenset({purpose.value}):
        problems.append(f"这次启动的模型批准用途只能写 {purpose.value}：写了别的用途，其他页面也会用到这份批准")

    matched = MODEL_APPROVAL_IDS[purpose].fullmatch(approval.approval_id)

    if matched is None:
        problems.append(f"模型批准编号应写成 {MODEL_ID_FORMS[purpose]}")
    elif run_date is not None and matched[1] != run_date.isoformat():
        problems.append("模型批准编号里的日期和运行日不一致")

    if problems:
        raise BatchTermsError(problems)

    ledger = Path(home).resolve() / LEDGER_IN_HOME
    run_dir = run_dir.resolve()
    model = ModelTerms(approval.approval_id, purpose, run_date, model_digest(approval), run_dir, ledger)
    source = _source_terms(environ, today, run_date, run_dir, ledger, problems) if launch is Launch.DISCOVERY else None

    if problems:
        raise BatchTermsError(problems)

    for late in (model.expired(today), None if source is None else source.expired(today)):
        if late is not None:
            problems.append(late)

    if problems:
        raise BatchTermsError(problems)

    batch = Batch(launch, run_dir, run_date, model, source, today=clock, monotonic=monotonic, sleep=sleep, wall=wall, create=create)
    batch.attach()

    return batch


def _source_terms(environ, today, run_date, run_dir, ledger, problems) -> SourceTerms | None:
    values = {key: (environ.get(name) or "").strip() for key, name in SOURCE_VARIABLES.items()}
    missing = [f"缺少 {SOURCE_VARIABLES[key]}" for key, value in values.items() if not value]

    if missing:
        problems += missing

        return None

    save_text = (environ.get(SAVE_VARIABLE) or "").strip() or SavePermission.NONE.value

    try:
        save = SavePermission(save_text)
    except ValueError:
        problems.append(f"{SAVE_VARIABLE} 只能写 none、facts 或 facts_quotes（不设就是 none，不保存）")
        save = SavePermission.NONE

    admitted = ADMISSION_ID.fullmatch(values["admission_id"])
    until = _day(values["admission_until"], SOURCE_VARIABLES["admission_until"], problems)

    if admitted is None:
        problems.append(f"{SOURCE_VARIABLES['admission_id']} 应写成 u3d6-admit-<日期>-<字母>")
    elif until is not None:
        given = _day(admitted[1], SOURCE_VARIABLES["admission_id"], problems)

        if given is not None and (given > today or until > given + timedelta(days=MAX_ADMISSION_DAYS)):
            problems.append(f"来源准入的期限不对：从 {given} 起，最长 {MAX_ADMISSION_DAYS} 天，而且不能晚于今天开始")

    approved = SOURCE_APPROVAL_ID.fullmatch(values["approval_id"])

    if approved is None:
        problems.append(f"{SOURCE_VARIABLES['approval_id']} 应写成 u3d6-src-<日期>-<字母>")
    elif run_date is not None and approved[1] != run_date.isoformat():
        problems.append("读取批准编号里的日期和运行日不一致")

    board = BOARDS.get(values["board"])

    if board is None:
        problems.append(f"只能读取 {'、'.join(sorted(BOARDS))} 这个职位站，不能读 {values['board']}")

    try:
        most = int(values["max_requests"])
    except ValueError:
        most = 0

    if not 1 <= most <= MAX_REQUESTS:
        problems.append(f"{SOURCE_VARIABLES['max_requests']} 应在 1 到 {MAX_REQUESTS} 之间")

    if problems:
        return None

    return SourceTerms(values["admission_id"], until, save, values["approval_id"], run_date, board, most, run_dir, ledger)


def _day(text: str, name: str, problems: list[str]) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        problems.append(f"{name} 应是一个日期（YYYY-MM-DD）")

        return None


class Batch:
    """One launch's approvals in one run directory, and every operation and request made under them (module doc)."""

    def __init__(
        self,
        launch: Launch,
        run_dir: Path,
        run_date: date,
        model: ModelTerms,
        source: SourceTerms | None,
        *,
        today: Callable[[], date] = today_here,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], datetime] | None = None,
        create: Callable[[], Transport] | None = None,
    ):
        self.launch = launch
        self.run_dir = run_dir
        self.run_date = run_date
        self.model = model
        self.source = source
        self.today = today
        self.monotonic = monotonic
        self.sleep = sleep
        self.wall = wall or (lambda: datetime.now(timezone.utc))
        self._create = create or lever_live._real_transport
        # The batch's own state; the request record and the task record are what it is restored from.
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.batch_id: str | None = None
        self.owner_id: str | None = None
        self.claimed: set[str] = set()
        self.sent = 0
        self.seq = 0
        self.failures = 0
        self.last_send: float | None = None
        self.robots_read = False
        self.current: Operation | None = None
        self.unrecorded = False
        self.halted: str | None = None
        # How the model's approval stands now (app.live_gate.GateStatus), once the gate is open (web_main).
        self.model_status: Callable[[], object] | None = None
        self.gate = None
        # The library the gate's ledger is in (its id), once the gate is open: every claim names it.
        self.library_id: str | None = None
        self.reserve_input_tokens = 16_000
        self.prices = None
        # The output limit of the model's requests but a decision's: an analysis's for discovery, an exploration's.
        self.output_tokens = 4_000
        self._lock_handle: int | None = None
        self.transport: Transport | None = None
        # Every claim of the batch, both launches', by approval id: as restored, and as claimed since.
        self.batch_claims: dict[str, dict] = {}
        # What HOME's progress says each approval of the batch reached: its last line, by approval id.
        self.anchors: dict[str, dict] = {}
        # The model requests HOME's progress notes as handed on, by approval id: request id → its reservation.
        self.handed_requests: dict[str, dict[str, int]] = {}
        # The bytes of each record the batch keeps, as it read them whole at start and wrote them since: a path's
        # [size, sha256] or None for one that must not be there (D6-0 R1, F2).
        self._expected: dict[Path, list | None] = {}
        self._records_lock = threading.RLock()
        # The source's requests counted at a restart but not yet noted in HOME (a crash between the two): noted when the
        # next operation begins.
        self._unnoted: int | None = None

    # --- start ------------------------------------------------------------------------------------------------

    @property
    def approvals(self) -> tuple:
        return (self.model,) if self.source is None else (self.model, self.source)

    @property
    def progress_path(self) -> Path:
        return self.model.ledger / f"{self.batch_id}{PROGRESS_SUFFIX}"

    @property
    def home_stop_path(self) -> Path:
        return self.model.ledger / f"{self.batch_id}{HOME_STOP_SUFFIX}"

    def attach(self) -> None:
        """Take the run directory's lock and restore the batch from its records, as a start or a restart may: every
        problem found keeps the app from starting (BatchTermsError)."""
        problems: list[str] = []
        self._take_lock(problems)

        if problems:
            raise BatchTermsError(problems)

        try:
            self._restore(problems)
        except BaseException:
            self.close()
            raise

        if problems:
            # A refusal at the start changes no record: they stay as they were found, as evidence.
            self.close()
            raise BatchTermsError(list(dict.fromkeys(problems)))

    def _take_lock(self, problems: list[str]) -> None:
        path = self.run_dir / LOCK_FILE

        try:
            handle = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            problems.append("运行目录里的锁文件打不开")

            return

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(handle)
            problems.append("同一个运行目录已经有一个应用在用这批批准：一份批准同一时间只能由一个进程使用")

            return

        self._lock_handle = handle

    def close(self) -> None:
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_handle)
                self._lock_handle = None

    def _restore(self, problems: list[str]) -> None:
        """The batch as its records left it, each read whole and checked against the others (D6-0 R1, F2): every claim
        of the batch, both launches', in HOME and in the run directory alike; the stop records, the run directory's and
        HOME's; the task record and the request record, each operation's requests counted in both; HOME's progress,
        which the request record may not fall short of. What the batch holds then is what it must find from now on;
        only after that does a restart write anything (an interrupted operation's end, a request's progress noted)."""
        for terms in self.approvals:
            home = _read_json(terms.claim_path)
            local = _read_json(self.run_dir / APPROVALS_DIR / f"{terms.approval_id}.json")

            if home is False or local is False:
                problems.append(f"批准 {terms.approval_id} 的占用记录读不出来（损坏），不能继续")
                continue

            if home is None and local is None:
                continue

            # Named first, whatever this folder holds: the approval was used for another run folder's batch.
            if isinstance(home, dict) and home.get("run_dir") != str(self.run_dir):
                problems.append(f"批准 {terms.approval_id} 已在别的运行目录用过（{home.get('run_dir')}），不能再用")
                continue

            if home is None or local is None or home != local:
                problems.append(f"批准 {terms.approval_id} 在主目录和运行目录里的占用记录对不上，不能继续")
                continue

            if home.get("digest") != terms.digest:
                problems.append(f"批准 {terms.approval_id} 的条款和第一次使用时不同，不能再用")
                continue

            self.claimed.add(terms.approval_id)

        # Every claim of the batch, this launch's and the other's: the batch is all of them (one stop, one user).
        claims = self._read_batch_claims(problems)
        stop = _read_json(self.run_dir / STOP_RECORD)

        if stop is False:
            problems.append("运行目录里的停止记录读不出来（损坏），不能继续")
        elif stop is not None:
            problems.append(f"这批已经停止（{stop.get('reason', '原因没有写下')}），重启也不会恢复")

        if (self.run_dir / STOP_FILE).exists():
            problems.append(f"运行目录里有 {STOP_FILE}，这批已经停止")

        if problems:
            return

        tasks = _read_lines(self.run_dir / TASK_LOG)
        requests = _read_lines(self.run_dir / REQUEST_LOG)

        if tasks is False or requests is False:
            problems.append("运行目录里的任务记录或请求记录有损坏的行，不能继续")

            return

        if claims and not tasks:
            problems.append("批准已经用过，但运行目录里没有任务记录（缺失），不能继续")

            return

        batches = {record.get("batch_id") for record in claims.values()} | {line.get("batch_id") for line in tasks + requests}

        if None in batches or len(batches) > 1:
            problems.append("占用记录、任务记录和请求记录不属于同一个批次，不能继续")

            return

        # The batch is bound to the user of its first operation (D6 plan, 3.2); every record says so.
        owner = next((line.get("owner_id") for line in tasks if line.get("state") == "begin"), None)

        if any(line.get("owner_id") != owner for line in tasks) or any(record.get("owner_id") != owner for record in claims.values()):
            problems.append("任务记录或占用记录里有别的用户，不能继续")

            return

        self.batch_id = batches.pop() if batches else secrets.token_hex(8)
        self.owner_id = owner
        self.batch_claims = claims

        if self.home_stop_path.exists():
            problems.append("这批已经停止（主目录里有这批的停止记录），重启也不会恢复")

            return

        crashed = self._restore_records(tasks, requests, problems)

        if problems:
            return

        noted = self._restore_progress(problems)

        if problems:
            return

        # From here on the records must hold what they hold now, and what the batch writes to them.
        self._track_records(problems)

        if problems:
            return

        if crashed is not None:
            # The operation running at the crash: what it used stays used, and it ends here as interrupted.
            try:
                self._task_line({"op_id": crashed[0], "state": "interrupted", "kind": crashed[1]})
            except OSError:
                problems.append("任务记录写不进去，不能继续")

                return

        # A request handed on at the crash before its progress was noted in HOME (never sent: the note comes before
        # the request goes): it counts, as a request of unknown outcome does, and is noted when the next operation
        # begins, as nothing is written in HOME before then (a check before a launch writes nothing there).
        self._unnoted = noted

    def _read_batch_claims(self, problems: list[str]) -> dict[str, dict]:
        """Every claim of this run directory's batch, both launches' (D6-0 R1, F2): each whole in the run directory and
        in HOME, the same, naming this run directory; and none in HOME naming this run directory without its copy here.
        A claim is never taken from one side alone, and never made again from the other."""
        claims: dict[str, dict] = {}
        folder = self.run_dir / APPROVALS_DIR
        ledger = self.model.ledger

        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            approval_id = path.stem
            local, home = _read_json(path), _read_json(ledger / f"{approval_id}.claim")

            if local is False or home is False:
                problems.append(f"批准 {approval_id} 的占用记录读不出来（损坏），不能继续")
            elif home is None:
                problems.append(f"批准 {approval_id} 的占用记录在运行目录里有、主目录里没有（缺失），不能继续")
            elif local != home or local.get("approval_id") != approval_id or local.get("run_dir") != str(self.run_dir):
                problems.append(f"批准 {approval_id} 在主目录和运行目录里的占用记录对不上，不能继续")
            else:
                claims[approval_id] = local

        for path in sorted(ledger.glob("*.claim")) if ledger.is_dir() else []:
            home = _read_json(path)

            if home is False:
                problems.append(f"主目录里批准 {path.stem} 的占用记录读不出来（损坏），不能继续")
            elif isinstance(home, dict) and home.get("run_dir") == str(self.run_dir) and not (folder / f"{path.stem}.json").exists():
                problems.append(f"批准 {path.stem} 的占用记录在主目录里有、运行目录里没有（缺失），不能继续")

        return claims

    def _restore_records(self, tasks: list[dict], requests: list[dict], problems: list[str]) -> tuple[str, str] | None:
        """The task record and the request record, checked against each other and the claims: every operation ended
        says how many requests it handed on, and the request record holds exactly those (D6-0 R1, F2). The operation
        running at a crash, if one was (its id and kind), to be ended as interrupted once the records are held."""
        opened, ended, kinds, said = [], set(), {}, {}

        for line in tasks:
            op, state = line.get("op_id"), line.get("state")

            if not isinstance(op, str) or state not in ("begin", "end", "interrupted"):
                problems.append("任务记录里有不认识的行，不能继续")

                return None

            if state == "begin":
                if op in kinds:
                    problems.append("任务记录里同一个操作开始了两次，不能继续")

                    return None

                kinds[op] = line.get("kind")
                opened.append(op)
            else:
                if op not in kinds or op in ended:
                    problems.append("任务记录里有没有开始就结束、或结束两次的操作，不能继续")

                    return None

                ended.add(op)

                if state == "end":
                    said[op] = line.get("sent")

        open_ops = [op for op in opened if op not in ended]

        if len(open_ops) > 1 or (open_ops and open_ops[0] != opened[-1]):
            problems.append("任务记录里同时有几个没有结束的操作，不能继续")

            return None

        source = self.source

        if source is None and requests:
            problems.append("这次启动没有读取批准，运行目录里却有请求记录，不能继续")

            return None

        handed, seqs, per_op = 0, set(), {}

        for line in requests:
            if line.get("approval_id") != source.approval_id:
                problems.append("请求记录里有不属于这份读取批准的行，不能继续")

                return None

            state, seq, op = line.get("state"), line.get("seq"), line.get("op_id")

            if state not in _REQUEST_STATES or not isinstance(seq, int):
                problems.append("请求记录里有不认识的行，不能继续")

                return None

            if op not in kinds:
                problems.append("请求记录里有任务记录里没有的操作，不能继续")

                return None

            if state == "claimed":
                continue

            self.seq = max(self.seq, seq)

            if state == "handed":
                if seq in seqs:
                    problems.append("请求记录里同一个请求记了两次，不能继续")

                    return None

                seqs.add(seq)
                handed += 1
                per_op[op] = per_op.get(op, 0) + 1

        if source is not None and (source.approval_id in self.claimed) != any(line.get("state") == "claimed" for line in requests):
            problems.append("读取批准的占用记录和请求记录对不上（缺失），不能继续")

            return None

        if source is not None and handed > source.max_requests:
            problems.append("请求记录里的请求比读取批准允许的多，不能继续")

            return None

        # Each operation ended: the requests it says it handed on are those the request record holds for it.
        short = sorted(op for op, sent in said.items() if sent != per_op.get(op, 0))

        if short:
            problems.append(f"任务记录和请求记录对不上：{len(short)} 项操作记下的请求次数和请求记录不一致（被截断、改动或缺失），不能继续")

            return None

        # A request handed with no result is one whose outcome is not known: it counts, and nothing is assumed of it.
        self.sent = handed
        self.failures = _failures_in_a_row(requests)

        if self.failures >= FAILURES_IN_A_ROW:
            problems.append(f"请求记录最后有 {self.failures} 个请求连续失败，这批应已停止，不能继续")

            return None

        self.last_send = self._restored_last_send(requests)

        return (open_ops[0], kinds[open_ops[0]]) if open_ops else None

    def _restore_progress(self, problems: list[str]) -> int | None:
        """HOME's progress for the batch (PROGRESS_SUFFIX), read whole: of this batch, each approval's growing only,
        every approval claimed in it (a claim is made after its first note, so one without a note was not made by the
        batch); an approval noted but not claimed only at nothing used (a crash between the note and the claim); each
        model request noted before it went (REQUEST_KIND, D6-0 R2) once, of a claimed approval, at a reservation. The
        source's request record may not fall short of it, nor pass it by more than the one request a crash between
        the request's record and its note leaves: that one's count, to be noted now, or None."""
        lines = _read_lines(self.progress_path)

        if lines is False:
            problems.append("主目录里这批的进度记录有损坏的行，不能继续")

            return None

        last: dict[str, dict] = {}
        handed: dict[str, dict[str, int]] = {}

        for line in lines:
            approval_id, kind = line.get("approval_id"), line.get("kind")

            if kind == REQUEST_KIND:
                # A model request noted right before it was handed on (D6-0 R2): its id in the ledger and its reservation.
                request_id, reserved = line.get("request_id"), line.get("reserved_micro_usd")
                mine = handed.setdefault(approval_id, {}) if isinstance(approval_id, str) else None

                if (
                    line.get("batch_id") != self.batch_id
                    or mine is None
                    or approval_id not in self.batch_claims
                    or not isinstance(request_id, str)
                    or not request_id
                    or request_id in mine
                    or not isinstance(reserved, int)
                    or isinstance(reserved, bool)
                    or reserved < 0
                ):
                    problems.append("主目录里这批的进度记录有不认识的请求行，不能继续")

                    return None

                mine[request_id] = reserved
                continue

            keys = PROGRESS_KEYS.get(kind)

            if line.get("batch_id") != self.batch_id or not isinstance(approval_id, str) or keys is None:
                problems.append("主目录里这批的进度记录有不认识的行，不能继续")

                return None

            if any(not isinstance(line.get(key), int) or isinstance(line.get(key), bool) or line[key] < 0 for key in keys):
                problems.append("主目录里这批的进度记录有不认识的值，不能继续")

                return None

            before = last.get(approval_id)

            if before is not None and (before["kind"] != kind or any(line[key] < before[key] for key in keys)):
                problems.append(f"主目录里批准 {approval_id} 的进度变少了（被改动），不能继续")

                return None

            last[approval_id] = line

        missing = sorted(approval_id for approval_id in self.batch_claims if approval_id not in last)

        if missing:
            problems.append(f"主目录里没有批准 {missing[0]} 的进度（缺失），不能继续")

            return None

        used = sorted(
            approval_id
            for approval_id, line in last.items()
            if approval_id not in self.batch_claims and any(line[key] for key in PROGRESS_KEYS[line["kind"]])
        )

        if used:
            problems.append(f"主目录里批准 {used[0]} 的进度有用量，却没有占用记录，不能继续")

            return None

        self.anchors = {approval_id: line for approval_id, line in last.items() if approval_id in self.batch_claims}
        self.handed_requests = handed

        if self.source is None or self.source.approval_id not in self.claimed:
            return None

        noted = self.anchors[self.source.approval_id]["handed"]

        if self.sent < noted:
            problems.append(f"请求记录只有 {self.sent} 次，比主目录记下的 {noted} 次少（被截断或回滚），不能继续")

            return None

        if self.sent > noted + 1:
            problems.append(f"请求记录有 {self.sent} 次，比主目录记下的 {noted} 次多出不止一次，对不上，不能继续")

            return None

        return self.sent if self.sent == noted + 1 else None

    # --- what the batch holds of its records (D6-0 R1, F2) ---------------------------------------------------------

    def _track_records(self, problems: list[str]) -> None:
        """From now on, the task and request records, HOME's progress and every claim of the batch (both copies) must
        hold what they hold now and what the batch writes to them; anything else is a change it did not make."""
        paths = [self.run_dir / TASK_LOG, self.run_dir / REQUEST_LOG, self.progress_path]

        for approval_id in self.batch_claims:
            paths += [self.run_dir / APPROVALS_DIR / f"{approval_id}.json", self.model.ledger / f"{approval_id}.claim"]

        with self._records_lock:
            for path in paths:
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    self._expected[path] = None
                    continue
                except OSError:
                    problems.append(f"{path.name} 读不出来，不能继续")

                    return

                self._expected[path] = [len(data), hashlib.sha256(data)]

    def _records_problem(self) -> str | None:
        """What of the records the batch holds is not as it read and wrote them, if anything: a record cut, damaged,
        changed, added to or gone, a claim more or fewer in the run directory."""
        with self._records_lock:
            for path, held in self._expected.items():
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    data = None
                except OSError:
                    return f"{path.name} 读不出来"

                if held is None:
                    if data is not None:
                        return f"{path.name} 出现了不是这批写下的内容"
                elif data is None:
                    return f"{path.name} 缺失"
                elif len(data) != held[0] or hashlib.sha256(data).hexdigest() != held[1].hexdigest():
                    return f"{path.name} 和这批写下的不一致（被截断、损坏或改动）"

            if self._expected:
                folder = self.run_dir / APPROVALS_DIR
                present = {path.stem for path in folder.glob("*.json")} if folder.is_dir() else set()

                if present != set(self.batch_claims):
                    return "运行目录里的占用记录多了或少了"

        return None

    def _records_hold(self) -> tuple[str, str] | None:
        """None while the records hold what the batch read and wrote; otherwise the batch stops for good, and why."""
        problem = self._records_problem()

        if problem is None:
            return None

        self.stop("records", problem)

        return "records", f"运行记录和这批写下的不一致（{problem}），这批停止，什么都没有发出"

    def _noted(self, path: Path, data: bytes) -> None:
        with self._records_lock:
            held = self._expected.get(path)

            if held is None:
                held = [0, hashlib.sha256()]
                self._expected[path] = held

            held[0] += len(data)
            held[1].update(data)

    def _write_line(self, path: Path, line: dict) -> None:
        """One line at the end of a record the batch holds, whole or not at all (OSError), and held as written."""
        data = _line_bytes(line)

        with self._records_lock:
            _append_bytes(path, data)
            self._noted(path, data)

    def _note_progress(self, approval_id: str, kind: str, **values) -> None:
        """One line of HOME's progress (PROGRESS_SUFFIX): how far an approval's use has come, or a model request about
        to be handed on (REQUEST_KIND). OSError if not written."""
        line = {"batch_id": self.batch_id, "approval_id": approval_id, "kind": kind, **values, "at": _stamp(self.wall())}
        self.model.ledger.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._write_line(self.progress_path, line)

        if kind in PROGRESS_KEYS:
            self.anchors[approval_id] = line

    def _standing(self, approval_id: str):
        """How the gate's ledger holds a model approval (app.live_gate.Standing), or None before the gate is open."""
        if self.gate is None:
            return None

        return self.gate.standing_among([approval_id]).get(approval_id)

    def _model_settled(self, approval_id: str) -> tuple[int, int] | None:
        """What the gate's ledger holds settled (or of unknown cost) for a model approval: (requests, micro-USD)."""
        standing = self._standing(approval_id)

        return None if standing is None else (standing.final_requests, standing.final_micro_usd)

    def _ledger_problem(self, approval_id: str, held) -> str | None:
        """What of HOME's progress the gate's ledger does not hold for a model approval, if anything (D6-0 R1 F1, R2):
        its final requests and their charge never fewer than noted; every request noted as handed on still there, at
        the reservation noted, in whatever state (settled, of unknown cost, released as never sent, reserved still);
        and a request settled over its reservation or of unknown cost only with its approval paused."""
        anchor = self.anchors.get(approval_id)

        if anchor is None or held is None:
            return f"主目录或账本里没有批准 {approval_id} 的记录"

        if held.final_requests < anchor["final_requests"] or held.final_micro_usd < anchor["final_micro_usd"]:
            return f"账本里批准 {approval_id} 的用量比主目录记下的少（经历库被换掉或回滚）"

        rows = held.requests
        noted = self.handed_requests.get(approval_id, {})
        gone = sorted(request_id for request_id in noted if request_id not in rows)

        if gone:
            return f"账本里少了批准 {approval_id} 发出前记下的 {len(gone)} 个请求（经历库被换掉或回滚）"

        changed = sorted(request_id for request_id, reserved in noted.items() if rows[request_id][1] != reserved)

        if changed:
            return f"账本里批准 {approval_id} 有 {len(changed)} 个请求的预留和发出前记下的对不上（被改动）"

        if not held.paused_reason and any(state in PAUSING_STATES for state, _ in rows.values()):
            return f"账本里批准 {approval_id} 有超出预留或费用待核实的请求，却没有暂停（被改动）"

        return None

    def _note_model(self, approval_id: str, *, handing: bool = False) -> tuple[str, str] | None:
        """Note in HOME how far the model approval's use has come, the ledger first checked against all HOME noted
        before (_ledger_problem): if it holds less, it was swapped or put back, and the batch stops for good. Handing
        a request on (handing): the one reservation the gate made for it is noted first, by its id, so that whatever
        comes of the request, even a crash before it settles or before its operation ends, no library without it
        starts again (D6-0 R2). None when noted."""
        held = self._standing(approval_id)

        if held is None:
            return "not_ready", "模型服务还没有准备好，没有发出"

        problem = self._ledger_problem(approval_id, held)

        if problem is not None:
            self.stop("ledger", problem)

            return "ledger", f"{problem}，这批停止，没有发出"

        reserved = sorted((request_id, amount) for request_id, (state, amount) in held.requests.items() if state == "reserved")

        if len(reserved) > 1:
            reason = f"账本里批准 {approval_id} 同时有 {len(reserved)} 个请求在预留（对不上）"
            self.stop("ledger", reason)

            return "ledger", f"{reason}，这批停止，没有发出"

        noted = self.handed_requests.setdefault(approval_id, {})
        anchor = self.anchors.get(approval_id)

        try:
            if handing:
                for request_id, amount in reserved:
                    if request_id not in noted:
                        self._note_progress(approval_id, REQUEST_KIND, request_id=request_id, reserved_micro_usd=amount)
                        noted[request_id] = amount

            if anchor is None or (held.final_requests, held.final_micro_usd) != (anchor["final_requests"], anchor["final_micro_usd"]):
                self._note_progress(approval_id, "model", final_requests=held.final_requests, final_micro_usd=held.final_micro_usd)
        except OSError:
            self.unrecorded = True

            return "unrecorded", "主目录里的进度记录写不进去，没有发出"

        return None

    def _restored_last_send(self, requests: list[dict]) -> float | None:
        stamps = [line.get("at") for line in requests if line.get("state") in ("handed", "answered", "failed")]

        if not stamps:
            return None

        try:
            last = datetime.fromisoformat(stamps[-1])
        except (TypeError, ValueError):
            return self.monotonic()

        since = (self.wall() - last).total_seconds()

        return self.monotonic() - max(since, 0.0)

    # --- the model's side, once the gate is open -------------------------------------------------------------

    def attach_gate(self, gate, library_id: str | None) -> None:
        """The model's approval as its gate holds it: its status (requests left, budget left, a pause) and the prices
        an operation's worst cost is reckoned with; the library its ledger is in (library_id). Its ledger must be the
        batch's (D6-0 R1, F1): the library every claim names, holding at least what HOME's progress says each model
        approval of the batch, this launch's and the other's, has settled, and every request HOME noted before it was
        handed on (D6-0 R2), whatever came of it. A pause, another library or a ledger showing less stops the batch
        for good (BatchTermsError); nothing in the records is changed but the stop."""
        self.model_status = gate.status
        self.gate = gate
        self.library_id = library_id
        policy = getattr(gate, "policy", None)

        if policy is not None:
            self.reserve_input_tokens = policy.reserve_input_tokens

        self.prices = gate.terms.prices
        self.output_tokens = gate.terms.output_limit(self.model.purpose.value)

        status = gate.status()

        if status.paused_reason is not None:
            self.stop("model_paused", f"模型批准已经暂停（{status.paused_reason}）")

            raise BatchTermsError([f"模型批准 {self.model.approval_id} 已经暂停，这批不能继续"])

        # Every claim names the library it was made with: another library (a new one, another copy's) is not the batch's.
        elsewhere = sorted(approval_id for approval_id, record in self.batch_claims.items() if record.get("library_id") != library_id)

        if library_id is None or elsewhere:
            reason = "经历库不是这批占用批准时用的那一个（编号对不上）"
            self.stop("ledger", reason)

            raise BatchTermsError([f"{reason}，这批不能继续"])

        # Every model approval of the batch (this launch's and the other's): its ledger at least what HOME noted, and
        # not paused. The other launch's may have been paused by its own run, or by this gate's recovery of a request
        # it left out at a crash: it stops this launch too (one batch, D6 plan 1.2 and 3.2).
        models = sorted({self.model.approval_id, *self._other_model_approvals()} & set(self.batch_claims))
        standing = gate.standing_among(models) if models else {}

        for approval_id in models:
            # Its final use never less than noted, and every request noted as handed on still there (D6-0 R2): a
            # request out at a crash, settled or not, before its operation ended, is in HOME before it went.
            problem = self._ledger_problem(approval_id, standing.get(approval_id))

            if problem is not None:
                self.stop("ledger", problem)

                raise BatchTermsError([f"{problem}，这批不能继续"])

        paused = {
            approval_id: standing[approval_id].paused_reason
            for approval_id in models
            if approval_id != self.model.approval_id and standing[approval_id].paused_reason
        }

        if paused:
            approval_id, reason = sorted(paused.items())[0]
            self.stop("model_paused", f"模型批准 {approval_id} 已经暂停（{reason}）")

            raise BatchTermsError([f"这一批的模型批准 {approval_id} 已经暂停，这批不能继续"])

    def _other_model_approvals(self) -> list[str]:
        """The model approvals of the other launch this run directory's records name (APPROVALS_DIR), by id."""
        folder = self.run_dir / APPROVALS_DIR

        if not folder.is_dir():
            return []

        return sorted(
            path.stem
            for path in folder.glob("*.json")
            if path.stem != self.model.approval_id and any(form.fullmatch(path.stem) for form in MODEL_APPROVAL_IDS.values())
        )

    def model_need(self, kind: OperationKind) -> tuple[int, int]:
        """The model requests an operation may send at most, and the worst its reservations could reach (micro-USD)."""
        if kind not in MODEL_OPERATIONS or self.prices is None:
            return 0, 0

        if kind is OperationKind.DIRECTION:
            return 1, reservation(self.reserve_input_tokens, self.output_tokens, self.prices)

        analyses = LIMITS.analyses * GENERATIONS_PER_ANALYSIS
        decisions = DECISIONS_PER_SEARCH
        worst = decisions * reservation(self.reserve_input_tokens, DECISION_OUTPUT_TOKENS, self.prices) + analyses * reservation(
            self.reserve_input_tokens, self.output_tokens, self.prices
        )

        return decisions + analyses, worst

    # --- what stops everything ---------------------------------------------------------------------------------

    def problem(self) -> tuple[str, str] | None:
        """Why nothing may go now, if nothing may: (code, words). A stop found here is written down for good."""
        if self.unrecorded:
            return "unrecorded", "运行记录写不进去，这批之后不再发送"

        if self.halted is not None:
            return "stopped", f"这批已经停止（{self.halted}）"

        stop = _read_json(self.run_dir / STOP_RECORD)

        if stop is not None:
            self.halted = "停止记录损坏" if stop is False else str(stop.get("reason"))

            return "stopped", f"这批已经停止（{self.halted}）"

        if (self.run_dir / STOP_FILE).exists():
            self.stop("live_stop", f"运行目录里出现了 {STOP_FILE}")

            return "live_stop", f"运行目录里有 {STOP_FILE}，这批已经停止"

        for terms in self.approvals:
            late = terms.expired(self.today())

            if late is not None:
                return "not_today", late

        status = self.model_status() if self.model_status is not None else None

        if status is not None and status.paused_reason is not None:
            self.stop("model_paused", f"模型批准已经暂停（{status.paused_reason}）")

            return "model_paused", "模型批准已经暂停，这批之后不再发送"

        return None

    def stop(self, code: str, reason: str) -> None:
        """Stop the batch for good: the first stop is written down, in the run directory (STOP_RECORD) and in HOME
        (HOME_STOP_SUFFIX), and a restart does not lift it, whichever of the two is still there."""
        with self.lock:
            if self.halted is None:
                self.halted = reason

            record = {"batch_id": self.batch_id, "code": code, "reason": reason, "at": _stamp(self.wall())}
            places = [self.run_dir / STOP_RECORD]

            if self.batch_id is not None:
                places.append(self.home_stop_path)

            for path in places:
                try:
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    _create_once(path, record)
                except FileExistsError:
                    pass
                except OSError:
                    self.unrecorded = True

    # --- operations ---------------------------------------------------------------------------------------------

    def _allowance(self, kind: OperationKind) -> tuple[tuple[str, str] | None, int, int]:
        """What one operation of kind would reserve of the batch's allowances, and why they cannot cover it, if they
        cannot: its single-operation cap of source requests (and robots, once a launch) against the read approval; for
        an operation of the model, its most requests and its worst reservations against the model approval. Reads
        only: begin() asks it under the lock before it takes anything, and availability() for a review page, so a
        review says what a confirmation would find (D6-1 R3, L3)."""
        requests = OPERATION_REQUESTS[kind]
        robots = 1 if requests and not self.robots_read else 0

        if requests and (self.source is None or self.sent + requests + robots > self.source.max_requests):
            left = 0 if self.source is None else max(self.source.max_requests - self.sent, 0)

            return ("requests", f"读取批准剩下 {left} 次请求，不够这项操作最多要用的 {requests + robots} 次"), requests, robots

        if kind in MODEL_OPERATIONS:
            status = self.model_status() if self.model_status is not None else None
            need, worst = self.model_need(kind)

            if status is None:
                return ("model", "模型服务还没有准备好"), requests, robots

            if status.requests_left < need or status.micro_usd_left < worst:
                return (
                    (
                        "model",
                        f"模型批准还剩 {status.requests_left} 次、${status.micro_usd_left / 1_000_000:.2f}，"
                        f"不够这项操作最多要用的 {need} 次、${worst / 1_000_000:.2f}",
                    ),
                    requests,
                    robots,
                )

        return None, requests, robots

    def availability(self, kind: OperationKind, owner_id: str) -> tuple[str, str] | None:
        """Whether one of the user's operations of kind could begin now, said without writing, claiming, taking or
        sending anything (D6-1 R3, L3): the run directory's lock, the launch it is for, the batch's user and the
        allowances its caps would reserve (_allowance). A review page shows it in place of its start button. Whether a
        stop was found is refusal()'s to say (problem), and another operation going on and the records are begin()'s:
        begin() asks all of it again, under the lock, when the user confirms, and only then takes anything."""
        kind = OperationKind(kind)

        with self.lock:
            if self._lock_handle is None:
                return "closed", "这批的运行目录锁已经释放，不再开始新的操作"

            if (kind is OperationKind.DIRECTION) != (self.launch is Launch.DIRECTION):
                return "purpose", "这次启动的批准不是给这种操作用的"

            if self.owner_id is not None and owner_id != self.owner_id:
                return "owner", "这批批准属于另一个用户，你的操作不能用它"

            return self._allowance(kind)[0]

    def begin(self, kind: OperationKind, owner_id: str, op_id: str, cancel=None) -> Operation:
        """Start one of the user's operations, or say why it may not (BatchRefused): nothing is sent, and nothing taken."""
        kind = OperationKind(kind)

        with self.lock:
            if self._lock_handle is None:
                raise BatchRefused("closed", "这批的运行目录锁已经释放，不再开始新的操作")

            if (kind is OperationKind.DIRECTION) != (self.launch is Launch.DIRECTION):
                raise BatchRefused("purpose", "这次启动的批准不是给这种操作用的")

            problem = self.problem()

            if problem is not None:
                raise BatchRefused(*problem)

            if self.owner_id is not None and owner_id != self.owner_id:
                raise BatchRefused("owner", "这批批准属于另一个用户，你的操作不能用它")

            if self.current is not None:
                raise BatchRefused("busy", "这批里另一项操作还在进行，等它结束再开始；什么都没有发送")

            held = self._records_hold()

            if held is not None:
                raise BatchRefused(*held)

            if self._unnoted is not None:
                try:
                    self._note_progress(self.source.approval_id, "source", handed=self._unnoted)
                except OSError:
                    self.unrecorded = True
                    raise BatchRefused("unrecorded", "主目录里的进度记录写不进去，没有开始") from None

                self._unnoted = None

            short, requests, robots = self._allowance(kind)

            if short is not None:
                raise BatchRefused(short[0], f"{short[1]}；没有开始")

            operation = Operation(op_id, kind, owner_id, cancel, requests, OPERATION_SECONDS[kind], self.monotonic(), robots=robots)
            bound = self.owner_id
            # The batch is the user's of its first operation, from now on (D6 plan, 3.2).
            self.owner_id = owner_id

            try:
                self._task_line({"op_id": op_id, "state": "begin", "kind": kind.value, "requests": requests, "robots": robots})
            except OSError:
                self.owner_id = bound
                self.unrecorded = True
                raise BatchRefused("unrecorded", "任务记录写不进去，没有开始") from None

            self.current = operation

            return operation

    def end(self, operation: Operation, outcome: str = "done") -> None:
        """End an operation once, whatever came of it: what it used stays used."""
        with self.lock:
            if operation.ended:
                return

            operation.ended = True

            try:
                self._task_line({"op_id": operation.id, "state": "end", "kind": operation.kind.value, "sent": operation.sent, "outcome": outcome})
            except OSError:
                self.unrecorded = True

            if self.current is operation:
                self.current = None

            if operation.kind in MODEL_OPERATIONS:
                # What the gate made of its last request (a use over its reservation pauses the approval) is seen
                # now, not only at the next operation: a pause is written down for good, so the other launch, which
                # reads only its own approval's status, does not start either. And how far the approval's use has
                # come, settled, is noted in HOME (D6-0 R1, F1).
                if self.model.approval_id in self.claimed and self._records_problem() is None:
                    self._note_model(self.model.approval_id)

                self.problem()

    def operation(self, op_id: str) -> Operation | None:
        with self.lock:
            return self.current if self.current is not None and self.current.id == op_id else None

    def _task_line(self, line: dict) -> None:
        line = {**line, "batch_id": self.batch_id, "owner_id": self.owner_id, "at": _stamp(self.wall())}
        self._write_line(self.run_dir / TASK_LOG, line)

    # --- claims -------------------------------------------------------------------------------------------------

    def claim(self, operation: Operation, terms) -> None:
        """Claim one approval for the batch, once: bound here to the batch, its run directory and the operation's user.
        A record of anything else keeps it back (BatchRefused); a claim made is never given back."""
        with self.lock:
            if terms.approval_id in self.claimed:
                return

            if operation.owner_id != self.owner_id:
                raise BatchRefused("owner", "这批批准属于另一个用户")

            if self.gate is None or self.library_id is None:
                raise BatchRefused("not_ready", "模型服务和经历库还没有接上，什么都没有发送")

            held = self._records_hold()

            if held is not None:
                raise BatchRefused(*held)

            record = {
                "approval_id": terms.approval_id,
                "digest": terms.digest,
                "run_dir": str(self.run_dir),
                "batch_id": self.batch_id,
                "owner_id": self.owner_id,
                "launch": self.launch.value,
                "library_id": self.library_id,
            }
            terms.ledger.mkdir(mode=0o700, parents=True, exist_ok=True)

            # Noted first in HOME's progress, at nothing used: a claim is never there without its first note, and a
            # note left by a crash before its claim is one of nothing used (D6-0 R1).
            try:
                if terms is self.source:
                    self._note_progress(terms.approval_id, "source", handed=self.sent)
                else:
                    settled = self._model_settled(terms.approval_id) or (0, 0)
                    self._note_progress(terms.approval_id, "model", final_requests=settled[0], final_micro_usd=settled[1])
            except OSError:
                self.unrecorded = True
                raise BatchRefused("unrecorded", "主目录里的进度记录写不进去，什么都没有发送") from None

            for path, failure in (
                (terms.claim_path, "批准的占用记录写不进去"),
                (self.run_dir / APPROVALS_DIR / f"{terms.approval_id}.json", "运行目录里的占用记录写不进去"),
            ):
                try:
                    path.parent.mkdir(mode=0o700, exist_ok=True)
                    self._noted(path, _create_once(path, record))
                except FileExistsError:
                    # Made by no one but a batch claiming it: another batch's, or this one's changed since.
                    self.stop("claim_mismatch", f"批准 {terms.approval_id} 的占用记录已经在了")
                    raise BatchRefused("claimed", f"批准 {terms.approval_id} 已经被别的批次或用户占用，什么都没有发送") from None
                except OSError:
                    self.unrecorded = True
                    raise BatchRefused("unrecorded", f"{failure}，什么都没有发送") from None

            self.batch_claims[terms.approval_id] = record

            if terms is self.source:
                try:
                    self._request_line({"seq": 0, "state": "claimed", "op_id": operation.id})
                except OSError:
                    self.unrecorded = True
                    raise BatchRefused("unrecorded", "请求记录写不进去，什么都没有发送") from None

            self.claimed.add(terms.approval_id)

    def claims_hold(self) -> bool:
        """Every approval claimed is still recorded as this batch's, in HOME and in the run directory."""
        for terms in self.approvals:
            if terms.approval_id not in self.claimed:
                continue

            home = _read_json(terms.claim_path)

            if not isinstance(home, dict) or home.get("batch_id") != self.batch_id or home.get("run_dir") != str(self.run_dir):
                return False

            if home != _read_json(self.run_dir / APPROVALS_DIR / f"{terms.approval_id}.json"):
                return False

        return True

    def model_refusal_before_claim(self, operation: Operation) -> tuple[str, str] | None:
        """Why the operation's approvals may not be claimed now, if they may not."""
        with self.lock:
            if self.current is not operation:
                return "not_current", "这项操作已经不在进行，什么都没有发送"

            if operation.cancelled():
                return "cancelled", "操作已经停止，什么都没有发送"

            return self.problem()

    def model_refusal(self, operation: Operation) -> tuple[str, str] | None:
        """Why the operation's next model request may not be handed on, if it may not: (code, words)."""
        with self.lock:
            if self.current is not operation:
                return "not_current", "这项操作已经不在进行，没有发出"

            if operation.cancelled():
                return "cancelled", "操作已经停止，没有发出"

            problem = self.problem()

            if problem is not None:
                return problem

            if self.model.approval_id not in self.claimed:
                return "unbound", "模型批准还没有占用，没有发出"

            if not self.claims_hold():
                self.stop("claim_mismatch", "批准的占用记录被改动")

                return "claim_mismatch", "批准的占用记录对不上，没有发出"

            held = self._records_hold()

            if held is not None:
                return held

            # Right before it is handed on (its reservation made): this request noted in HOME by its id and reservation
            # (D6-0 R2), and how far the approval's use has come, settled; a ledger showing less than noted before
            # stops the batch (D6-0 R1, F1).
            return self._note_model(self.model.approval_id, handing=True)

    # --- the source's requests ---------------------------------------------------------------------------------

    def _request_line(self, line: dict) -> None:
        line = {**line, "approval_id": self.source.approval_id, "batch_id": self.batch_id, "at": _stamp(self.wall())}
        self._write_line(self.run_dir / REQUEST_LOG, line)

    def failed(self) -> None:
        self.failures += 1

        if self.failures >= FAILURES_IN_A_ROW:
            self.stop("failures", f"连续 {self.failures} 个请求失败")

    def register(self) -> Register:
        """The admitted board for the operation running now: whatever asks for it, each of its requests is the
        current operation's, within its own most and the batch's."""
        source = self.source

        if source is None:
            raise BatchRefused("purpose", "这次启动没有读取批准")

        transport = BatchTransport(self)
        guard = BatchGuard(self)

        return Register((entry(source),), {source.board.source_id: BatchLeverSource(source.board, transport=transport, guard=guard, batch=self)})


def entry(terms: SourceTerms) -> SourceEntry:
    basis = (
        f"有期限、有限用途的来源准入 {terms.admission_id}（到 {terms.admission_until} 为止，只供 Owner 本机有限 Demo，只读公开职位接口；"
        f"保存许可：{SAVE_TEXT[terms.save]}）；读取批准 {terms.approval_id}（{terms.run_date}，这一批最多 {terms.max_requests} 次请求）"
    )

    return SourceEntry(terms.board.source_id, terms.board.name, Access.LOCAL_FETCH, Permission.VERIFIED, EvidenceLevel.OFFICIAL, basis, terms.run_date.isoformat())


SAVE_TEXT = {
    SavePermission.NONE: "不许保存",
    SavePermission.FACTS: "只存事实和链接",
    SavePermission.FACTS_QUOTES: "事实、链接，再加分析引文",
}


def keep_of(terms: SourceTerms | None) -> tuple[frozenset[str], frozenset[str]]:
    """The sources whose results may be kept, and whose analyses too, by the admission's save permission alone."""
    if terms is None or terms.save is SavePermission.NONE:
        return frozenset(), frozenset()

    kept = frozenset({terms.board.source_id})

    return kept, (kept if terms.save is SavePermission.FACTS_QUOTES else frozenset())


class BatchGuard:
    """Every request of the batch to its host, in turn (app.discovery.lever.HostGuard's rules): within the current
    operation's own most and time, within the batch's requests, once INTERVAL has passed since the batch's last
    request, whichever operation sent it, and failures in a row counted for the batch."""

    def __init__(self, batch: Batch):
        self.batch = batch
        self.monotonic = batch.monotonic
        self.log: list[tuple[str, str]] = []

    @property
    def stopped(self) -> str | None:
        problem = self.batch.problem()

        return None if problem is None else problem[1]

    def get(self, transport, url: str, deadline: float):
        batch = self.batch

        with batch.send_lock:
            operation = batch.current

            if operation is None:
                raise OperationSpent("没有正在进行的操作，没有发出")

            problem = batch.problem()

            if problem is not None:
                # Held back for the batch (a stop written down, LIVE_STOP, the day, a pause): nothing went out.
                raise TransportHeldBack(f"没有发出这个请求：{problem[1]}")

            if operation.sent >= operation.budget:
                raise OperationSpent(f"这项操作的请求次数用完了（{operation.budget} 次），没有发出")

            if batch.sent >= batch.source.max_requests:
                raise HostStopped(f"读取批准的请求次数用完了（{batch.source.max_requests} 次），没有发出")

            now = batch.monotonic()
            wait = 0.0 if batch.last_send is None else max(0.0, batch.last_send + INTERVAL - now)
            deadline = min(deadline, operation.began + operation.seconds)

            if deadline - now - wait < MIN_REQUEST_SECONDS:
                raise TimeUsedUp("这项操作剩下的时间不够再发一个请求，没有发出")

            if wait:
                batch.sleep(wait)
                now = batch.monotonic()

                if deadline - now < MIN_REQUEST_SECONDS:
                    raise TimeUsedUp("等待间隔以后时间不够了，没有发出")

            batch.sent += 1
            operation.sent += 1

            try:
                reply = transport.get(url, min(REQUEST_SECONDS, deadline - now))
            except BatchKeptBack as kept:
                # Kept back before its pre-send record: not used. After it: used, and nothing gives it back.
                if not kept.handed:
                    batch.sent -= 1
                    operation.sent -= 1

                self.log.append((url, "refused"))

                raise TransportHeldBack(f"没有发出这个请求：{kept}") from None
            except TransportError as error:
                batch.last_send = batch.monotonic()
                self.log.append((url, "error"))
                batch.failed()

                if isinstance(error, TransportTimeout):
                    raise SourceTimeout("来源没有在时限内回答") from None

                if isinstance(error, TransportTooLarge):
                    raise BadReply("返回超过大小上限，没有读完") from None

                raise SourceError("请求没有成功，没有收到回答") from None

            batch.last_send = batch.monotonic()
            self.log.append((url, str(reply.status)))

            if reply.status in STOP_STATUSES:
                batch.stop("refused", f"来源回应 {reply.status}（要求登录、付费或限流）")

                raise HostStopped(f"来源回应 {reply.status}（要求登录、付费或限流），之后不再向它发请求")

            if reply.status in (200, 404):
                batch.failures = 0
            else:
                batch.failed()

            return reply


class BatchTransport:
    """How each request of the batch leaves, if it does (app.discovery.lever.Transport), as validation B's controlled
    transport does (lever_live.ControlledTransport), for the batch: checked before and after its pre-send record,
    claimed for the batch at its first request, recorded with the operation it belongs to."""

    def __init__(self, batch: Batch):
        self.batch = batch
        self.call = "listing"

    def problem(self) -> str | None:
        kept = self._kept()

        return None if kept is None else kept[1]

    def _kept(self) -> tuple[str, str] | None:
        batch = self.batch
        operation = batch.current

        if operation is None:
            return "no_operation", "没有正在进行的操作，没有发出"

        if operation.cancelled():
            return "cancelled", "操作已经停止，没有发出"

        problem = batch.problem()

        if problem is not None:
            return problem

        if batch.source.approval_id in batch.claimed and not batch.claims_hold():
            batch.stop("claim_mismatch", "批准的占用记录被改动")

            return "claim_mismatch", "批准的占用记录对不上，没有发出"

        # The records as the batch read and wrote them, before and after this request's own record (D6-0 R1, F2).
        return batch._records_hold()

    def get(self, url: str, timeout: float):
        batch = self.batch
        began = batch.monotonic()
        operation = batch.current
        where = {"call": self.call, **_address_kind(url, batch.source.board.site)}
        handed = False

        with batch.lock:
            batch.seq += 1
            seq = batch.seq

        try:
            self._hold_back(handed)

            if batch.source.approval_id not in batch.claimed:
                try:
                    batch.claim(operation, batch.source)
                except BatchRefused as refused:
                    raise BatchKeptBack(refused.code, str(refused)) from None

            if batch.transport is None:
                batch.transport = batch._create()

            self._hold_back(handed)
            planned = self._left(timeout, began, handed)

            try:
                batch._request_line({"seq": seq, "state": "handed", "op_id": operation.id, **where, "planned_seconds": round(planned, 3)})
            except OSError:
                batch.unrecorded = True
                raise BatchKeptBack("unrecorded", "请求记录写不进去，没有发出") from None

            handed = True

            try:
                # Then noted in HOME, before it is sent: what the run directory may never show less of (D6-0 R1).
                batch._note_progress(batch.source.approval_id, "source", handed=batch.sent)
            except OSError:
                batch.unrecorded = True
                raise BatchKeptBack("unrecorded", "主目录里的进度记录写不进去，没有发出", handed=True) from None
            self._hold_back(handed)
            left = self._left(timeout, began, handed)
        except BatchKeptBack as kept:
            self._quietly({"seq": seq, "state": "withdrawn" if handed else "refused", "op_id": getattr(operation, "id", None), **where, "reason": kept.code})
            raise

        sent = batch.monotonic()

        try:
            reply = batch.transport.get(url, left)
        except TransportError as error:
            self._quietly({"seq": seq, "state": "failed", "op_id": operation.id, "outcome": lever_live._error_kind(error), "seconds": round(batch.monotonic() - sent, 3)})
            raise

        self._quietly({"seq": seq, "state": "answered", "op_id": operation.id, "outcome": str(reply.status), "bytes": len(reply.body), "seconds": round(batch.monotonic() - sent, 3)})

        return reply

    def _hold_back(self, handed: bool) -> None:
        kept = self._kept()

        if kept is not None:
            raise BatchKeptBack(*kept, handed=handed)

    def _left(self, timeout: float, began: float, handed: bool) -> float:
        left = timeout - (self.batch.monotonic() - began)

        if left < MIN_REQUEST_SECONDS:
            raise BatchKeptBack("late", f"准备发出用掉了这次请求的时间（只剩 {max(left, 0.0):.1f} 秒），没有发出", handed=handed)

        return left

    def _quietly(self, line: dict) -> None:
        try:
            self.batch._request_line(line)
        except OSError:
            self.batch.unrecorded = True


class BatchLeverSource(lever_live.ControlledLeverSource):
    """The board as the batch reads it: robots once a launch, for the batch (Batch.robots_read); an anomaly or a stop
    of the host stops the batch for good, an operation's own most or time only that operation."""

    def __init__(self, board: LeverBoard, *, transport: BatchTransport, guard: BatchGuard, batch: Batch):
        super().__init__(board, transport=transport, guard=guard)
        self.batch = batch
        self.robots_checked = batch.robots_read

    def _open(self, kind: str, call, timeout: float):
        try:
            return super()._open(kind, call, timeout)
        except HeldBack:
            # Nothing went out: the batch or the operation kept it back, and said so where it did.
            raise
        except SourceAnomaly as anomaly:
            # The source not as recorded or documented (robots, a reply, an identity): the batch stops for good.
            self.batch.stop("source", str(anomaly))
            raise

    def _robots(self, timeout: float) -> None:
        if self.batch.robots_read:
            self.robots_checked = True

            return

        super()._robots(timeout)
        self.batch.robots_read = True


class Binding:
    """The model's side of one operation (a search or an exploration): its approvals claimed for the batch inside
    the gate before anything is sent (bind), asked before each model request (refusal), its operation ended once,
    whatever came of the task (release)."""

    def __init__(self, batch: Batch, operation: Operation):
        self.batch = batch
        self.operation = operation

    def bind(self) -> None:
        from app.discovery.runner import ApprovalsHeld

        if self.operation is None:
            raise ApprovalsHeld("not_current", "这次查找不是这一批正在进行的操作，什么都没有发送")

        refusal = self.batch.model_refusal_before_claim(self.operation)

        if refusal is not None:
            raise ApprovalsHeld(*refusal)

        try:
            self.batch.claim(self.operation, self.batch.model)

            if self.batch.source is not None:
                self.batch.claim(self.operation, self.batch.source)
        except BatchRefused as refused:
            raise ApprovalsHeld(refused.code, str(refused)) from None

    def refusal(self) -> tuple[str, str] | None:
        if self.operation is None:
            return "not_current", "这次查找不是这一批正在进行的操作，没有发出"

        return self.batch.model_refusal(self.operation)

    def release(self, outcome: str = "done") -> None:
        if self.operation is not None:
            self.batch.end(self.operation, outcome)


def analysis_record(batch: Batch, task_id: str) -> Callable[[dict], None]:
    """Where one search notes what came of each analysis it tried (as validation B, U3-D2-E3): codes, counts, lengths,
    kinds masked; never a posting's words, its id or a prompt. A line that cannot be written is left out."""

    def record(line: dict) -> None:
        line = {**line, "op_id": task_id, "batch_id": batch.batch_id, "at": _stamp(batch.wall())}

        try:
            _append(batch.run_dir / lever_live.ANALYSIS_LOG, line)
        except OSError:
            pass

    return record


def state_of(batch: Batch) -> str:
    """The batch as it stands, in one value for a confirmation (D6 plan, 3.2): what the source and the model have used
    and have left. Any change voids a review made before it."""
    status = batch.model_status() if batch.model_status is not None else None
    model = "none" if status is None else f"{status.requests_left}:{status.micro_usd_left}:{status.paused_reason or ''}"
    source = "none" if batch.source is None else f"{batch.sent}/{batch.source.max_requests}"

    return f"{batch.batch_id}|src={source}|model={model}|stopped={batch.halted or ''}"


class Trajectory:
    """One search's compact trajectory, kept in the run directory when it ends (D6 plan, 2; the plan's line 169): each
    step's tool, conditions, outcome and what it newly found, read, checked and analysed (app.discovery.compare.
    StepRecorder), each decision's action, how it ended, and the adjustments judge v4 finds in it (compare.adjustments).
    No experience, preference or prompt; postings by their source and id. A trajectory that cannot be made or written
    is left out: the search goes on as it would."""

    def __init__(self, batch: Batch, task_id: str):
        self.batch = batch
        self.task_id = task_id
        self.recorder = None
        self.actions: list[dict] = []

    def start(self, run) -> None:
        from app.discovery.compare import StepRecorder

        try:
            self.recorder = StepRecorder(run)
        except Exception:
            self.recorder = None

    def step(self, step) -> None:
        if self.recorder is not None:
            try:
                self.recorder(step)
            except Exception:
                self.recorder = None

    def decider(self, inner):
        return _RecordedDecider(inner, self.actions)

    def finish(self, finished, problem: str | None) -> None:
        from app.discovery.compare import adjustments

        steps = [] if self.recorder is None else self.recorder.steps
        record = {"steps": steps, "actions": self.actions}

        try:
            found = adjustments(record)
        except Exception as error:
            found = [{"kind": "not_judged", "why": type(error).__name__}]

        outcome = getattr(finished, "outcome", None)
        line = {
            "task_id": self.task_id,
            "end": None if outcome is None or outcome.end is None else outcome.end.value,
            "problem": problem,
            "decisions": None if finished is None else finished.trace.decisions,
            "steps": steps,
            "actions": self.actions,
            "adjustments": found,
        }
        operation = SimpleNamespace(id=self.task_id)
        note_trajectory(self.batch, operation, line)


class _RecordedDecider:
    """A decider whose every action is kept for the trajectory, as the comparison keeps them (compare.RecordingDecider),
    with nothing stopped for it."""

    def __init__(self, inner, actions: list):
        self._inner = inner
        self._actions = actions
        self.kind = getattr(inner, "kind", "model")

    def decide(self, observation) -> str:
        from app.discovery.agent import parse_action

        try:
            raw = self._inner.decide(observation)
        except BaseException as error:
            self._actions.append({"number": observation.number, "status": f"failed_{type(error).__name__}"})
            raise

        action, tool, problem = parse_action(raw)
        self._actions.append(
            {
                "number": observation.number,
                "status": "decided",
                "tool": tool.value if tool is not None else None,
                "action": None if action is None else action.model_dump(mode="json"),
                "problem": problem,
            }
        )

        return raw


def note_trajectory(batch: Batch, operation: Operation, line: dict) -> None:
    """One search's compact trajectory (D6 plan, 2): tools, key conditions, what came back, how it ended, and the
    adjustments judge v4 finds in it. A line that cannot be written is left out; nothing else changes."""
    line = {**line, "op_id": operation.id, "batch_id": batch.batch_id, "at": _stamp(batch.wall())}

    try:
        _append(batch.run_dir / TRAJECTORY_LOG, line)
    except OSError:
        pass


# --- records ------------------------------------------------------------------------------------------------------


def _address_kind(url: str, site: str) -> dict:
    """What a request asked for, as the record keeps it: the kind of address and, for the list, its page. Never the
    address, nor a posting's id."""
    from urllib.parse import parse_qs, urlsplit

    from app.discovery.lever import PAGE_SIZE

    parts = urlsplit(url)

    if parts.path == "/robots.txt":
        return {"kind": "robots", "page": None}

    if parts.path == f"/v0/postings/{site}":
        skip = parse_qs(parts.query).get("skip", ["0"])[0]

        return {"kind": "list", "page": int(skip) // PAGE_SIZE + 1 if skip.isdigit() else None}

    return {"kind": "posting", "page": None}


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _append(path: Path, line: dict) -> None:
    lever_live._append_line(path, line)


def _line_bytes(line: dict) -> bytes:
    """One record line as it is written (as lever_live._append_line writes one)."""
    return (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _append_bytes(path: Path, data: bytes) -> None:
    """data at the end of a record, whole or not at all: written through and synced, or what was written of it cut off
    again and OSError raised (lever_live._append_line's way, with the bytes the batch holds the record to)."""
    with lever_live._RECORD_LOCK:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

        try:
            start = os.fstat(handle).st_size

            try:
                lever_live._write_whole(handle, data)
                os.fsync(handle)
            except BaseException:
                lever_live._take_back(handle, start)
                raise
        finally:
            os.close(handle)


def _create_once(path: Path, record: dict) -> bytes:
    """Write a record that is written once: atomically created, whole and synced; FileExistsError if it is there.
    The bytes written."""
    data = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(handle, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())

    return data


def _read_json(path: Path):
    """A record's object; None when it is not there; False when it cannot be read whole (damaged)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return False

    try:
        value = json.loads(text)
    except ValueError:
        return False

    return value if isinstance(value, dict) else False


def _read_lines(path: Path):
    """A record's lines as objects; [] when it is not there; False when a line cannot be read (damaged)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        return False

    lines = []

    for raw in text.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            return False

        if not isinstance(value, dict):
            return False

        lines.append(value)

    return lines


def _failures_in_a_row(requests: list[dict]) -> int:
    """The failures at the end of the request record, as the guard counts them: a reply but a page, a posting or a
    404 is a failure, as is no reply; anything else of the batch's resets it."""
    failures = 0

    for line in requests:
        state = line.get("state")

        if state == "failed":
            failures += 1
        elif state == "answered":
            failures = 0 if line.get("outcome") in ("200", "404") else failures + 1

    return failures
