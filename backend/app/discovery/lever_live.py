"""Palantir's Lever board as a real source, under control (U3-D2-E1). Built here; read only with approvals.

Nothing here admits the board or reads it by itself. The default Lever register stays unverified
(app.discovery.lever.register), and this module grants nothing: a search reads the board only when the app was
started for it (app.discovery.web.sources_from, LEVER_PALANTIR) with two inputs of the Owner's, checked at start and
again before every request, and kept apart:

- the source's admission (ADMISSION_ID, ADMISSION_UNTIL): limited in time (MAX_ADMISSION_DAYS) and in use (a local
  demo of the Owner's, read-only), given by the Owner, never by this code;
- a read approval for one search (APPROVAL_ID, RUN_DATE, BOARD, MAX_REQUESTS, WINDOW_SECONDS, RUN_DIR): its date in
  America/New_York, the one board, the most requests, the search's network window and its run directory.

A model approval is not one of them: decisions and analyses are not touched here. The search pages keep running their
simulation whatever else the app may have, unless validation B is chosen (app.discovery.model_live, U3-D2-E3): then
a model approval of its own is bound with this read approval to one search, and claims this one for the search before
anything is sent; the first request finds the claim made, its own, and goes on.

What a search may do with them:

- Nothing reads at start, on a page or on a refresh. The register is made when a reviewed search is confirmed, so its
  network window runs from then (HostGuard.window); the real transport (app.discovery.lever_http) is imported and made
  only when the first request is about to leave (ControlledTransport).
- Right before that first request the approval is claimed for the search: a record made atomically under the
  approval's own id in the ledger of used approvals (LEDGER_IN_HOME, under the HOME the app was started with; claim),
  naming the approval, the digest of its terms, the run directory and the search. Wherever the run directory is, a
  record already there refuses the request, whoever made it and whenever: one approval reads for one search, through
  concurrent confirmations, refreshes, restarts and other run directories alike, and a record is never taken back,
  not after a cancellation, a stop or a failure either. The app does not start with an approval already used. The
  ledger keeps an approval from being used twice by mistake; it is not a permission system.
- Every request, robots first (ControlledLeverSource), then the list's pages, reads and checks, goes through the one
  host guard of the search: one budget for all of them, spacing, the window, the stops of app.discovery.lever. Right
  before it is handed to the transport, after any wait and again once the transport is made ready, it is checked:
  the search not cancelled, no LIVE_STOP in the run directory, the admission and the approval still valid today, the
  claim still this search's, enough of its time left. Making ready (the claim, the transport) takes from the
  request's time, so from the window too: nothing restarts either. A request refused there is not sent, and it stops
  the source for the rest of the search, its host for good once the window has run out: nothing is sent after it.
- robots.txt different from what was recorded (ROBOTS_RULES, 2026-11-10), not a 200, or a redirect stops the source
  before any posting is asked for. A stop is never made up for: what was not sent is not sent later.
- Every request is recorded in the run directory (REQUEST_LOG, U3-D2-E2), metadata only, each line written whole or
  not at all: a pre-send record before it may go ("handed", with the time it was planned to get); if it then does not
  go, "withdrawn" and why; if it goes, what came of it, with when it was in fact handed on and with what time; a
  request kept back before its pre-send record, "refused" and why. Kinds of address and page numbers, never an
  address, a posting id or anything a posting says. A request whose pre-send record cannot be written is not sent,
  and once a line cannot be written nothing more is: what was sent stays provable. A pre-send record is not proof
  that a request went; one with neither a result nor a withdrawal is a request whose outcome is not known.
- What came of each analysis a search tried is noted in the run directory too (ANALYSIS_LOG, U3-D2-E3, analysis_record):
  what stopped it or how it ended, as codes, the lengths and the kinds masked; never a posting's words, its id or a
  prompt. A note that cannot be written is left out: it proves nothing sent, and stops nothing.
"""
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from app.discovery.contracts import Access, EvidenceLevel, Limits, PostingRef
from app.discovery.lever import (
    API_HOST,
    MIN_REQUEST_SECONDS,
    PAGE_SIZE,
    HostGuard,
    HostStopped,
    LeverBoard,
    LeverSource,
    Transport,
    TransportError,
    TransportRefused,
    TransportTimeout,
    TransportTooLarge,
)
from app.discovery.sources import HeldBack, Permission, Register, SourceAnomaly, SourceEntry
from app.i18n import t

KEY = "lever-palantir"
BOARD = LeverBoard("palantir", "Palantir")
NAME = "Palantir（Lever 公开职位接口）"
ZONE = ZoneInfo("America/New_York")
# What one read approval may allow at most (D2-E pre-check, 6.3): robots 1, the list's 3 pages, 4 reads, 4 checks.
MAX_REQUESTS = 12
MAX_WINDOW_SECONDS = 600.0
INTERVAL = 2.0
FAILURES_IN_A_ROW = 2
# A search with the real source reads and checks no more postings than its requests allow.
LIMITS = Limits(reads=4, checks=4)
# An admission is limited in time: at most this many days from the day it was given.
MAX_ADMISSION_DAYS = 31
ROBOTS_URL = f"https://{API_HOST}/robots.txt"
ROBOTS_RULES = ("user-agent: *", "allow: /", "crawl-delay: 1")
ROBOTS_CHECKED_ON = "2026-11-10"
STOP_FILE = "LIVE_STOP"
VARIABLES = {
    "admission_id": "ROLERADAR_SOURCE_ADMISSION_ID",
    "admission_until": "ROLERADAR_SOURCE_ADMISSION_UNTIL",
    "approval_id": "ROLERADAR_SOURCE_APPROVAL_ID",
    "run_date": "ROLERADAR_SOURCE_RUN_DATE",
    "board": "ROLERADAR_SOURCE_BOARD",
    "max_requests": "ROLERADAR_SOURCE_MAX_REQUESTS",
    "window_seconds": "ROLERADAR_SOURCE_WINDOW_SECONDS",
    "run_dir": "ROLERADAR_SOURCE_RUN_DIR",
}
ADMISSION_ID = re.compile(r"u3d2e-admit-(\d{4}-\d{2}-\d{2})-[a-z]")
APPROVAL_ID = re.compile(r"u3d2e-src-(\d{4}-\d{2}-\d{2})-[a-z]")
REPO = Path(__file__).resolve().parents[3]
# Where used read approvals are recorded, one record per approval id, under the HOME the app is started with: the
# same place whatever the run directory, so a new run directory never makes an approval unused.
LEDGER_IN_HOME = Path(".roleradar", "source-approvals")
# The record of the run's requests, one JSON object a line, in the run directory (U3-D2-E2).
REQUEST_LOG = "requests.jsonl"
# What came of each analysis a search tried, metadata only, one JSON object a line (U3-D2-E3).
ANALYSIS_LOG = "analyses.jsonl"
# One line at a time, whole, in this process: a line cut short is taken back before the next is written.
_RECORD_LOCK = threading.Lock()


class SourceTermsError(ValueError):
    """The real source may not be used: problems says why, every reason found."""

    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = tuple(problems)


class RobotsChanged(SourceAnomaly):
    """robots.txt is not as recorded, or could not be read: no posting is asked for."""

    said_as_is = True


class SourceHalted(HeldBack):
    """The source stopped earlier in this search (an anomaly, a refusal, a stop of its host): nothing more is sent.
    Told as held back, with what stopped it first (U3-D2-E2)."""

    said_as_is = True


class KeptBack(TransportRefused):
    """A request the controlled transport kept back, and why, in a word the request record keeps (code)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def today_here() -> date:
    """Today in America/New_York, the zone every approval's date is written in."""
    return datetime.now(ZONE).date()


@dataclass(frozen=True)
class SourceTerms:
    """The admission and the one read approval a search with the real source runs under, and the ledger its use is
    recorded in (a place, not one of the terms: their digest leaves it out)."""

    admission_id: str
    admission_until: date
    approval_id: str
    run_date: date
    max_requests: int
    window_seconds: float
    run_dir: Path
    ledger: Path

    @property
    def digest(self) -> str:
        payload = {
            "admission": [self.admission_id, self.admission_until.isoformat()],
            "approval": [self.approval_id, self.run_date.isoformat(), BOARD.site, self.max_requests, self.window_seconds],
            "run_dir": str(self.run_dir),
            "robots": [ROBOTS_URL, list(ROBOTS_RULES)],
        }

        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @property
    def claim_path(self) -> Path:
        # By the approval's id alone: whichever run directory, one record.
        return self.ledger / f"{self.approval_id}.claim"

    def expired(self, today: date) -> str | None:
        """Why the terms do not hold on this day, if they do not."""
        if today != self.run_date:
            return f"读取批准 {self.approval_id} 只在 {self.run_date}（美东）有效，今天是 {today}"

        if today > self.admission_until:
            return f"来源准入 {self.admission_id} 到 {self.admission_until} 为止，已经过期"

        return None


def load_terms(environ: Mapping[str, str], *, today: date | None = None) -> SourceTerms:
    """The admission and read approval the environment names, or SourceTermsError with every problem found.

    Read once at start: a missing, malformed, mismatched, expired or already used approval stops the app from
    starting for the real source, whatever its run directory: its use is looked for in the ledger under HOME, by its
    id. Nothing is read from the board, and nothing is written, here."""
    today = today or today_here()
    values = {key: (environ.get(name) or "").strip() for key, name in VARIABLES.items()}
    home = (environ.get("HOME") or "").strip()
    problems = [f"缺少 {VARIABLES[key]}" for key, value in values.items() if not value]

    if not home:
        problems.append("缺少 HOME：用过的读取批准记在主目录下，找不到主目录就不能确认批准有没有用过")

    if problems:
        raise SourceTermsError(problems)

    admitted = ADMISSION_ID.fullmatch(values["admission_id"])
    approved = APPROVAL_ID.fullmatch(values["approval_id"])
    admission_until = _day(values["admission_until"], VARIABLES["admission_until"], problems)
    run_date = _day(values["run_date"], VARIABLES["run_date"], problems)

    if admitted is None:
        problems.append(f"{VARIABLES['admission_id']} 应写成 u3d2e-admit-<日期>-<字母>")
    elif admission_until is not None:
        given = _day(admitted[1], VARIABLES["admission_id"], problems)

        if given is not None and (given > today or admission_until > given + timedelta(days=MAX_ADMISSION_DAYS)):
            problems.append(f"来源准入的期限不对：从 {given} 起，最长 {MAX_ADMISSION_DAYS} 天，而且不能晚于今天开始")

    if approved is None:
        problems.append(f"{VARIABLES['approval_id']} 应写成 u3d2e-src-<日期>-<字母>")
    elif run_date is not None and approved[1] != run_date.isoformat():
        problems.append("读取批准编号里的日期和运行日不一致")

    if values["board"] != BOARD.site:
        problems.append(f"只能读取 {BOARD.site} 这一个职位站，不能读 {values['board']}")

    max_requests = _number(values["max_requests"], int, 1, MAX_REQUESTS, VARIABLES["max_requests"], problems)
    window = _number(values["window_seconds"], float, 1.0, MAX_WINDOW_SECONDS, VARIABLES["window_seconds"], problems)
    run_dir = Path(values["run_dir"])

    if not run_dir.is_absolute() or not run_dir.is_dir():
        problems.append(f"{VARIABLES['run_dir']} 要写一个已经存在的目录的绝对路径")
    elif run_dir.resolve() == REPO or REPO in run_dir.resolve().parents:
        problems.append("运行目录不能放在仓库里")

    if not Path(home).is_absolute():
        problems.append("HOME 要写绝对路径")

    if problems:
        raise SourceTermsError(problems)

    terms = SourceTerms(
        values["admission_id"],
        admission_until,
        values["approval_id"],
        run_date,
        max_requests,
        window,
        run_dir.resolve(),
        Path(home).resolve() / LEDGER_IN_HOME,
    )
    late = terms.expired(today)

    if late is not None:
        problems.append(late)

    if terms.claim_path.exists():
        problems.append(
            f"读取批准 {terms.approval_id} 已经用过（记录在 {terms.claim_path}），不能再用；换运行目录、重启都不会让它变回没用过"
        )

    if problems:
        raise SourceTermsError(problems)

    return terms


def _day(text: str, name: str, problems: list[str]) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        problems.append(f"{name} 应是一个日期（YYYY-MM-DD）")

        return None


def _number(text: str, kind, least, most, name: str, problems: list[str]):
    try:
        value = kind(text)
    except ValueError:
        value = None

    if value is None or not least <= value <= most:
        problems.append(f"{name} 应在 {least} 到 {most} 之间")

        return least

    return value


def entry(terms: SourceTerms) -> SourceEntry:
    """The board as this search's source: verified only under these terms, and saying which."""
    basis = (
        f"有期限、有限用途的来源准入 {terms.admission_id}（到 {terms.admission_until} 为止，只供 Owner 本机有限 Demo，只读公开职位接口）；"
        f"单次读取批准 {terms.approval_id}（{terms.run_date}，最多 {terms.max_requests} 次请求）"
    )

    return SourceEntry(BOARD.source_id, NAME, Access.LOCAL_FETCH, Permission.VERIFIED, EvidenceLevel.OFFICIAL, basis, terms.run_date.isoformat())


def claim(terms: SourceTerms, task_id: str, *, now: datetime | None = None) -> None:
    """Take the approval for one search, atomically, under its id in the ledger: made once, never released, whatever
    the run directory. FileExistsError when taken."""
    record = _claim_record(terms, task_id)
    record["claimed_at"] = (now or datetime.now(timezone.utc)).isoformat()
    terms.ledger.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = os.open(terms.claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(record, out, ensure_ascii=False, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())


def _claim_record(terms: SourceTerms, task_id: str) -> dict:
    return {"approval_id": terms.approval_id, "digest": terms.digest, "run_dir": str(terms.run_dir), "task_id": task_id}


def _real_transport() -> Transport:
    # The one place the transport that reaches Lever is loaded, and only when a request is about to leave.
    from app.discovery.lever_http import LeverHttpTransport

    return LeverHttpTransport()


class ControlledTransport:
    """The way every request of one search to the real board leaves, if it does (app.discovery.lever.Transport).

    Right before each request, after any wait of the host guard, it checks the search, the stop file, the terms and
    the claim; a request that may not leave is refused (KeptBack, a TransportRefused): not sent, not counted, and the
    source is stopped for the rest of the search. The first request that may leave claims the approval, then the real
    transport is made: never before. Making ready takes time, during which a stop may come: everything is checked again
    once it is done, and its time is taken from the request's (monotonic, the guard's clock), never given back.

    Each request is recorded in the run directory (REQUEST_LOG, U3-D2-E2), every line whole or not at all (_write):
    "handed", the pre-send record, written first or the request does not go, with when it was written and the time
    planned; then, the record down, everything is checked once more and its time counted again from the start (R1):
    "withdrawn" with its code if it does not go after all; else "answered" or "failed", with when it was in fact
    handed on (sent_since_confirm), with what time (given_seconds) and how long it took. "refused", with its code,
    for one kept back before its pre-send record. began is when the search was confirmed, which every line's
    since_confirm counts from; call is what the source is doing (robots, listing, read, check), set by
    ControlledLeverSource before each call.
    """

    def __init__(
        self,
        terms: SourceTerms,
        task_id: str,
        cancel=None,
        *,
        create: Callable[[], Transport] = _real_transport,
        today: Callable[[], date] = today_here,
        monotonic: Callable[[], float] = time.monotonic,
        began: float | None = None,
    ):
        self.terms = terms
        self.task_id = task_id
        self.cancel = cancel
        self._create = create
        self._today = today
        self._monotonic = monotonic
        self._began = monotonic() if began is None else began
        self._transport: Transport | None = None
        self._sequence = 0
        # Set once what came of a request could not be recorded: nothing more is sent.
        self._unrecorded = False
        self.claimed = False
        self.call = "listing"

    @property
    def created(self) -> bool:
        return self._transport is not None

    def get(self, url: str, timeout: float):
        began = self._monotonic()
        self._sequence += 1
        seq, where = self._sequence, {"call": self.call, **_address_kind(url)}
        recorded = False

        try:
            self._hold_back()

            if not self.claimed:
                try:
                    claim(self.terms, self.task_id)
                except FileExistsError:
                    # Claimed already: only this very search's own record, made when validation B bound its
                    # approvals (app.discovery.model_live), lets it read; anyone else's keeps it back.
                    if _read_claim(self.terms) != _claim_record(self.terms, self.task_id):
                        raise KeptBack("claimed", f"读取批准 {self.terms.approval_id} 已经被占用，没有发出") from None

                self.claimed = True
                self._first({"seq": 0, "state": "claimed", "approval_id": self.terms.approval_id})

            if self._transport is None:
                self._transport = self._create()

            # Made ready, maybe after a while: the search, the stop file, the day and the claim read again, and what
            # is left of the request's time is all it gets. The host guard reads its window again after a refusal.
            self._hold_back()
            planned = self._left(timeout, began)
            # The pre-send record: what is about to go and the time it is planned to get. Written whole first, or
            # nothing goes; it is not proof that the request went.
            self._first({"seq": seq, "state": "handed", **where, "planned_seconds": round(planned, 3)})
            recorded = True
            # Writing it took time, in which a stop may have come: checked once more right before the hand-off, and
            # what is left of the request's time, counted from its start, is all it gets (U3-D2-E2 R1).
            self._hold_back()
            left = self._left(timeout, began)
        except KeptBack as kept:
            # Not sent: refused before its pre-send record, withdrawn after it.
            self._quietly({"seq": seq, "state": "withdrawn" if recorded else "refused", **where, "reason": kept.code})
            raise

        sent = self._monotonic()

        try:
            reply = self._transport.get(url, left)
        except TransportError as error:
            self._after({"seq": seq, "state": "failed", "outcome": _error_kind(error), **self._handoff(sent, left)})
            raise

        self._after({"seq": seq, "state": "answered", "outcome": str(reply.status), "bytes": len(reply.body), **self._handoff(sent, left)})

        return reply

    def _left(self, timeout: float, began: float) -> float:
        """What is left of a request's time, counted from when it was asked for: too little, and it does not go."""
        left = timeout - (self._monotonic() - began)

        if left < MIN_REQUEST_SECONDS:
            raise KeptBack("late", f"准备发出用掉了这次请求的时间（只剩 {max(left, 0.0):.1f} 秒），没有发出")

        return left

    def _handoff(self, sent: float, left: float) -> dict:
        """The evidence a request went: when it was in fact handed on, with what time, and how long it took."""
        return {"sent_since_confirm": round(sent - self._began, 3), "given_seconds": round(left, 3), "seconds": self._since(sent)}

    def _hold_back(self) -> None:
        kept = self._kept()

        if kept is not None:
            raise KeptBack(*kept)

    def problem(self) -> str | None:
        """Why the next request may not leave now, if it may not."""
        kept = self._kept()

        return None if kept is None else kept[1]

    def _kept(self) -> tuple[str, str] | None:
        if self.cancel is not None and self.cancel.is_set():
            return "cancelled", "查找已经停止，没有发出"

        if (self.terms.run_dir / STOP_FILE).exists():
            return "live_stop", f"运行目录里有 {STOP_FILE}，没有发出"

        late = self.terms.expired(self._today())

        if late is not None:
            return "not_today", f"{late}，没有发出"

        if self.claimed and _read_claim(self.terms) != _claim_record(self.terms, self.task_id):
            return "claim_mismatch", "批准的占用记录不是这次查找的，没有发出"

        if self._unrecorded:
            return "unrecorded", "请求记录写不进去，之后不再发请求"

        return None

    def _since(self, mark: float) -> float:
        return round(self._monotonic() - mark, 3)

    def _write(self, line: dict) -> None:
        """One line of the request record, whole or not at all (R1): written through to its end and synced, or what
        was written of it is cut off again and OSError raised. A part of a line is never left as if written."""
        line = {**line, "task_id": self.task_id, "since_confirm": self._since(self._began)}
        line["at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        _append_line(self.terms.run_dir / REQUEST_LOG, line)

    def _first(self, line: dict) -> None:
        # Written before anything goes: if it cannot be, nothing goes, now or after.
        try:
            self._write(line)
        except OSError:
            self._unrecorded = True

            raise KeptBack("unrecorded", "请求记录写不进去，没有发出") from None

    def _quietly(self, line: dict) -> None:
        # A request kept back stays kept back whether or not its line can be written; a record that cannot be
        # written stops what comes after.
        try:
            self._write(line)
        except OSError:
            self._unrecorded = True

    def _after(self, line: dict) -> None:
        # The request went: what came of it is returned whatever happens here, but if it cannot be recorded, nothing
        # more is sent, and the request stays on record as handed on with its outcome unknown.
        try:
            self._write(line)
        except OSError:
            self._unrecorded = True


def _append_line(path: Path, line: dict) -> None:
    """One JSON line at the end of a record of the run directory, whole or not at all: written through to its end
    and synced, or what was written of it is cut off again and OSError raised. One line at a time in this process."""
    data = (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    with _RECORD_LOCK:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)

        try:
            start = os.fstat(handle).st_size

            try:
                _write_whole(handle, data)
                os.fsync(handle)
            except BaseException:
                _take_back(handle, start)
                raise
        finally:
            os.close(handle)


def analysis_record(terms: SourceTerms, task_id: str) -> Callable[[dict], None]:
    """Where one search with the real source notes what came of each analysis it tried (U3-D2-E3, ANALYSIS_LOG):
    the line app.discovery.tools.SearchRun.note_analysis gives (codes, counts, lengths, kinds masked), with the
    search's id and the time. A line that cannot be written whole is left out; nothing else changes."""

    def record(line: dict) -> None:
        line = {**line, "task_id": task_id, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}

        try:
            _append_line(terms.run_dir / ANALYSIS_LOG, line)
        except OSError:
            pass

    return record


def _write_whole(handle: int, data: bytes) -> None:
    """All of data, through as many writes as it takes. A write that makes no progress is a failure, so this ends:
    every turn either writes at least one byte or raises."""
    done = 0

    while done < len(data):
        written = os.write(handle, data[done:])

        if not isinstance(written, int) or written <= 0:
            raise OSError(f"请求记录只写进了 {done} / {len(data)} 字节")

        done += written


def _take_back(handle: int, start: int) -> None:
    # What was written of a line that could not be finished is cut off, so the record holds whole lines only. If
    # even that fails, the caller still treats the line as not written.
    try:
        os.ftruncate(handle, start)
        os.fsync(handle)
    except OSError:
        pass


def _address_kind(url: str) -> dict:
    """What a request asked for, as its record keeps it: the kind of address and, for the list, its page. Never the
    address itself, nor a posting's id."""
    parts = urlsplit(url)

    if parts.path == "/robots.txt":
        return {"kind": "robots", "page": None}

    if parts.path == f"/v0/postings/{BOARD.site}":
        skip = parse_qs(parts.query).get("skip", ["0"])[0]

        return {"kind": "list", "page": int(skip) // PAGE_SIZE + 1 if skip.isdigit() else None}

    return {"kind": "posting", "page": None}


def _error_kind(error: TransportError) -> str:
    if isinstance(error, TransportTimeout):
        return "timeout"

    if isinstance(error, TransportTooLarge):
        return "too_large"

    if isinstance(error, TransportRefused):
        return "not_sent"

    return "error"


def _read_claim(terms: SourceTerms) -> dict | None:
    try:
        record = json.loads(terms.claim_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return {key: record.get(key) for key in ("approval_id", "digest", "run_dir", "task_id")}


def robots_rules(body: bytes) -> list[str]:
    """robots.txt as rules to compare: comments and blank lines dropped, names folded, spaces evened out."""
    rules = []

    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()

        if line:
            name, _, value = line.partition(":")
            rules.append(f"{name.strip().casefold()}: {' '.join(value.split())}")

    return rules


class ControlledLeverSource(LeverSource):
    """The board as a search with the real source reads it: robots.txt first, once, through the same guard and
    budget as every other request; rules not as recorded stop the source before any posting is asked for. Once it
    stopped (an anomaly, a refusal before sending, a stop of its host), every later call is refused (SourceHalted)
    and sends nothing: a stop is never made up for."""

    def __init__(self, board: LeverBoard, *, transport: Transport, guard: HostGuard):
        super().__init__(board, transport=transport, guard=guard)
        self.robots_checked = False
        self.halted: str | None = None

    def stopped(self) -> str | None:
        """Why this board can send nothing more in this search, if it cannot (U3-D2-E3 R1): it stopped itself (an
        anomaly, robots, a request kept back, its host's refusal, its budget or window); its host's guard stopped
        (failures in a row); or its transport keeps every request back now (the search stopped, LIVE_STOP, the day,
        the claim, a record that cannot be written). Validation B ends the whole search on it. Its budget used up is
        not a stop by itself: the next request asked for is refused, and that is."""
        return self.halted or self.guard.stopped or self._transport.problem()

    def listing(self, timeout: float):
        return self._open("listing", lambda: LeverSource.listing(self, timeout), timeout)

    def read(self, ref: PostingRef, timeout: float):
        return self._open("read", lambda: LeverSource.read(self, ref, timeout), timeout)

    def check(self, ref: PostingRef, timeout: float):
        return self._open("check", lambda: LeverSource.check(self, ref, timeout), timeout)

    def _open(self, kind: str, call, timeout: float):
        if self.halted is not None:
            raise SourceHalted(t("disc.live.halted", why=self.halted))

        try:
            self._robots(timeout)
            # What the requests of this call are for, as the request record keeps them (U3-D2-E2).
            self._transport.call = kind

            return call()
        except (SourceAnomaly, HostStopped) as stop:
            self.halted = str(stop)
            raise

    def _robots(self, timeout: float) -> None:
        if self.robots_checked:
            return

        self._transport.call = "robots"
        reply = self.guard.get(self._transport, ROBOTS_URL, self.guard.monotonic() + timeout)

        if reply.status != 200:
            raise RobotsChanged(t("disc.live.robots_status", status=reply.status))

        if robots_rules(reply.body) != list(ROBOTS_RULES):
            raise RobotsChanged(t("disc.live.robots_changed", day=ROBOTS_CHECKED_ON))

        self.robots_checked = True


def register(
    terms: SourceTerms,
    task,
    *,
    create: Callable[[], Transport] = _real_transport,
    today: Callable[[], date] = today_here,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Register:
    """The board for one confirmed search: its own guard, whose window runs from now, and its own controlled
    transport, which makes nothing until its first request may leave and keeps time by the guard's clock. task gives
    the search's id and cancel signal."""
    clocks = {name: value for name, value in (("monotonic", monotonic), ("sleep", sleep)) if value is not None}
    guard = HostGuard(
        budget=terms.max_requests, interval=INTERVAL, window=terms.window_seconds, failures_in_a_row=FAILURES_IN_A_ROW, **clocks
    )
    # The window runs from the guard's making: the request record counts from the same moment.
    transport = ControlledTransport(
        terms, task.id, getattr(task, "cancel", None), create=create, today=today, monotonic=guard.monotonic,
        began=guard.window_ends - terms.window_seconds,
    )

    return Register((entry(terms),), {BOARD.source_id: ControlledLeverSource(BOARD, transport=transport, guard=guard)})
