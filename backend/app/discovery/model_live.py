"""Validation B's model approval (U3-D2-E3): a search's decisions and analyses through a real service, chosen
explicitly, bound with the real source's read approval to one search, and used once.

Nothing here builds a provider or sends anything: the gate (app.live_gate) and the runner (app.discovery.runner) do,
and only for a search the user confirmed on the pages.

- Chosen explicitly, VARIABLE set to LIVE. Unset, the search pages run their simulation whatever service the app has:
  a real service there for other pages is never borrowed. Chosen, the app starts only with all of it as it must be
  (load_terms), or not at all: the real source (app.discovery.lever_live) under its admission and read approval; a
  real service whose approval names discovery alone; that approval's id written u3d2e-ai-<date>-<letter>, its date
  the read approval's run date and today in America/New_York; neither approval used before, each looked for by its
  id in its ledger under HOME. It never falls back to the simulation.
- Bound to one search (Binding.bind). Once a confirmed search has entered the gate, which lets one real task run at a
  time, and before anything is sent, the model approval is claimed for it, atomically under its own id in its ledger
  (LEDGER_IN_HOME), naming the read approval, the digest of its own terms, the run directory and the search; then
  the read approval in its own ledger, for the same search (lever_live.claim), whose first request finds that claim
  its own. A record already there keeps the search from running, with nothing sent: one pair of approvals for one
  search, through a second confirmation, concurrent searches, a search after the first ended, a restart and another
  run directory alike. A record is never taken back: not after a cancellation, a stop or a failure, nor when the
  search could not go on after its model approval was claimed. The ledgers keep approvals from being used twice by
  mistake; they are not a permission system.
- Asked before each request (Binding.refusal), right before it is handed on and after the gate's own checks (the
  approval's requests and budget, a pause, the gate's stop file): the search not cancelled, no LIVE_STOP in the run
  directory, both approvals valid today, both claims still this search's. A request kept back ends the search at the
  gate (app.discovery.runner): nothing more is sent.

A decision asks for at most DECISION_OUTPUT_TOKENS and sends at most DECISION_REQUEST_BYTES (B pre-check §7), within
what the gate allows a discovery request. The review names the three recipients and this notice (NOTICE_VERSION):
another notice, or other approvals, are another confirmation.
"""
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from app.discovery import lever_live
from app.discovery.runner import ApprovalsHeld
from app.job_analysis.service import LIVE_NOTICE
from app.live_gate import ApprovalTerms, Purpose

VARIABLE = "ROLERADAR_DISCOVERY_MODEL"
LIVE = "live"
APPROVAL_ID = re.compile(r"u3d2e-ai-(\d{4}-\d{2}-\d{2})-[a-z]")
# Where used model approvals of validation B are recorded, one record per approval id, under the HOME the app is
# started with, beside the read approvals' ledger: the same wherever the run directory is.
LEDGER_IN_HOME = Path(".roleradar", "model-approvals")
# A decision's own limits under this approval (B pre-check §7.2).
DECISION_OUTPUT_TOKENS = 2_000
DECISION_REQUEST_BYTES = 15_000
# What the review says of the model and of what is kept (U3-D2-E3). Changing a word changes the version, and with it
# every confirmation's fingerprint. Its second and third paragraphs are the job analysis pages' own (live-3).
NOTICE_VERSION = "discovery-live-1"
NOTICE = (
    "确认后，这次查找每一步的决策请求和每个岗位的分析请求，都会发给上面写明的外部 AI 服务，只用于这一次查找；"
    "岗位来源另按读取批准受控读取。按服务商的政策，API 数据默认不用于训练模型。",
    LIVE_NOTICE[1],
    LIVE_NOTICE[2],
    "这次查找的模型批准和读取批准都只能用一次：确认后开始时就算用掉，中途停止、失败也不会恢复，不补跑。"
    "处理说明的版本或任一批准变了，原来的核对作废，要重新核对后再确认。",
)
_CLAIM_KEYS = ("approval_id", "digest", "source_approval_id", "run_dir", "task_id")


class ModelTermsError(ValueError):
    """Validation B may not be used: problems says why, every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = tuple(problems)


def chosen(environ: Mapping[str, str]) -> bool:
    """Whether the app is started for validation B; any other value than unset or LIVE stops it (ValueError)."""
    value = (environ.get(VARIABLE) or "").strip()

    if not value:
        return False

    if value == LIVE:
        return True

    raise ValueError(
        f"{VARIABLE} 只能不设，或者设为 {LIVE}（验证 B：真实来源加真实模型，需要来源和模型两份批准）；"
        "其他值不启用任何模型，网页没有启动。"
    )


@dataclass(frozen=True)
class ModelTerms:
    """The model approval of validation B, with the read terms it is bound with and the ledger its use goes in."""

    approval_id: str
    run_date: date
    # The digest of the gate's terms of the approval (ApprovalTerms.snapshot): other terms under the same id are not
    # this approval.
    terms_digest: str
    source: lever_live.SourceTerms
    ledger: Path

    @property
    def claim_path(self) -> Path:
        return self.ledger / f"{self.approval_id}.claim"

    def record(self, task_id: str) -> dict:
        return {
            "approval_id": self.approval_id,
            "digest": self.terms_digest,
            "source_approval_id": self.source.approval_id,
            "run_dir": str(self.source.run_dir),
            "task_id": task_id,
        }

    def expired(self, today: date) -> str | None:
        """Why either approval does not hold on this day, if it does not."""
        if today != self.run_date:
            return f"模型批准 {self.approval_id} 只在 {self.run_date}（美东）有效，今天是 {today}"

        return self.source.expired(today)


def terms_digest(approval: ApprovalTerms) -> str:
    payload = json.dumps(approval.snapshot(), sort_keys=True, ensure_ascii=False, default=str)

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_terms(approval: ApprovalTerms | None, source: lever_live.SourceTerms | None, *, today: date | None = None) -> ModelTerms:
    """The model approval of validation B, bound with these read terms, or ModelTermsError with every problem found.

    Read once at start: nothing is claimed or written here."""
    today = today or lever_live.today_here()
    problems = []

    if source is None:
        problems.append("验证 B 只能和真实来源一起用：要同时设 ROLERADAR_DISCOVERY_DEMO=lever-palantir 和它的来源准入、读取批准")

    if approval is None:
        problems.append("验证 B 要用真实模型，但没有可用的模型批准：要设 ROLERADAR_ANALYSIS_MODE=live 和它的批准条款")

    if problems:
        raise ModelTermsError(problems)

    if approval.purposes != frozenset({Purpose.DISCOVERY.value}):
        problems.append("验证 B 的模型批准用途只能写 discovery：写了别的用途，其他页面也会用到这份批准")

    matched = APPROVAL_ID.fullmatch(approval.approval_id)
    run_date = None

    if matched is None:
        problems.append("验证 B 的模型批准编号应写成 u3d2e-ai-<日期>-<字母>")
    else:
        try:
            run_date = date.fromisoformat(matched[1])
        except ValueError:
            problems.append("模型批准编号里的日期不对")
        else:
            if run_date != source.run_date:
                problems.append("模型批准编号里的日期和读取批准的运行日不一致")

    if problems:
        raise ModelTermsError(problems)

    terms = ModelTerms(approval.approval_id, run_date, terms_digest(approval), source, _home(source) / LEDGER_IN_HOME)
    late = terms.expired(today)

    if late is not None:
        problems.append(late)

    if terms.claim_path.exists():
        problems.append(
            f"模型批准 {terms.approval_id} 已经用过（记录在 {terms.claim_path}），不能再用；换运行目录、重启都不会让它变回没用过"
        )

    if source.claim_path.exists():
        problems.append(f"读取批准 {source.approval_id} 已经用过（记录在 {source.claim_path}），不能再用")

    if problems:
        raise ModelTermsError(problems)

    return terms


def check_service(terms: ModelTerms, approval: ApprovalTerms) -> None:
    """The app's real service is the very approval validation B was read for, for discovery alone; else
    ModelTermsError."""
    if approval.approval_id != terms.approval_id or terms_digest(approval) != terms.terms_digest:
        raise ModelTermsError(["应用的真实服务和验证 B 的模型批准不是同一份"])

    if approval.purposes != frozenset({Purpose.DISCOVERY.value}):
        raise ModelTermsError(["验证 B 的模型批准用途只能写 discovery"])


def _home(source: lever_live.SourceTerms) -> Path:
    # The read approvals' ledger is HOME/.roleradar/source-approvals: the same HOME.
    return source.ledger.parents[len(lever_live.LEDGER_IN_HOME.parts) - 1]


def claim(terms: ModelTerms, task_id: str, *, now: datetime | None = None) -> None:
    """Take the model approval for one search, atomically, under its id in its ledger: made once, never released,
    whatever the run directory. FileExistsError when taken."""
    record = terms.record(task_id)
    record["claimed_at"] = (now or datetime.now(timezone.utc)).isoformat()
    terms.ledger.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(terms.claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(record, out, ensure_ascii=False, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())


def read_claim(terms: ModelTerms) -> dict | None:
    try:
        record = json.loads(terms.claim_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return {key: record.get(key) for key in _CLAIM_KEYS}


def start_problem(terms: ModelTerms, today: date) -> str | None:
    """Why a search with these approvals may not start now, in words, if it may not; nothing is claimed here. The
    binding checks it all again, and claims, once the search has entered the gate."""
    late = terms.expired(today)

    if late is not None:
        return late

    if (terms.source.run_dir / lever_live.STOP_FILE).exists():
        return f"运行目录里有 {lever_live.STOP_FILE}"

    if terms.claim_path.exists():
        return f"模型批准 {terms.approval_id} 已经用过，一次查找只能用一次"

    if terms.source.claim_path.exists():
        return f"读取批准 {terms.source.approval_id} 已经用过，一次查找只能用一次"

    return None


class Binding:
    """The two approvals of validation B, held for one search (see the module docstring)."""

    def __init__(self, terms: ModelTerms, task_id: str, cancel=None, *, today: Callable[[], date] = lever_live.today_here):
        self.terms = terms
        self.task_id = task_id
        self.cancel = cancel
        self._today = today
        self.bound = False

    def bind(self) -> None:
        """Claim both approvals for this search, the model's first, before anything is sent: ApprovalsHeld when one
        may not be claimed, nothing sent, and what was claimed kept."""
        kept = self._kept()

        if kept is not None:
            raise ApprovalsHeld(*kept)

        try:
            claim(self.terms, self.task_id)
        except FileExistsError:
            raise ApprovalsHeld("model_claimed", f"模型批准 {self.terms.approval_id} 已经被占用，什么都没有发送") from None

        try:
            lever_live.claim(self.terms.source, self.task_id)
        except FileExistsError:
            raise ApprovalsHeld("source_claimed", f"读取批准 {self.terms.source.approval_id} 已经被占用，什么都没有发送") from None

        self.bound = True

    def refusal(self) -> tuple[str, str] | None:
        """Why the search's next request may not be handed on, if it may not: (code, words)."""
        if not self.bound:
            return "unbound", "这次查找的批准还没有占用，没有发出"

        kept = self._kept()

        if kept is not None:
            return kept

        if read_claim(self.terms) != self.terms.record(self.task_id):
            return "model_claim_mismatch", "模型批准的占用记录不是这次查找的，没有发出"

        source = self.terms.source

        if lever_live._read_claim(source) != lever_live._claim_record(source, self.task_id):
            return "source_claim_mismatch", "读取批准的占用记录不是这次查找的，没有发出"

        return None

    def _kept(self) -> tuple[str, str] | None:
        if self.cancel is not None and self.cancel.is_set():
            return "cancelled", "查找已经停止，没有发出"

        if (self.terms.source.run_dir / lever_live.STOP_FILE).exists():
            return "live_stop", f"运行目录里有 {lever_live.STOP_FILE}，没有发出"

        late = self.terms.expired(self._today())

        if late is not None:
            return "not_today", f"{late}，没有发出"

        return None
