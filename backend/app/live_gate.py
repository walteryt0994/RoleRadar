"""The gate every real AI request goes through, and the ledger of what each one cost (U2-C1).

Nothing here sends anything by itself, and the pages do not use it yet:
U2-C2 and U2-C3 put it in front of the real provider. An app opens one gate,
for one approval (open_gate):

- First an exclusive lock on a file next to the library database: one app
  at a time may use its ledger. A second app is refused before it reads or
  writes any of it.
- Holding the lock, requests a previous run left reserved are charged at
  their reservation as unknown, and their approvals paused for good. Then
  the approval's terms are compared with the snapshot taken when they were
  first used: any difference is refused, so neither a restart nor a changed
  setting widens an approval.
- One real task at a time (task()), and its provider lets requests through
  only while that task runs and the gate is open. Before each request, in
  one write transaction: no stop file, the approval not paused, no other
  request unsettled, and room under both the request and the cost limit for
  this request's worst case, which is reserved; then the task and the gate
  are checked once more, and only then is the request handed on.
- Closing the gate, or leaving a task, lets nothing more through and waits
  for the requests already let through to be settled; only then is the lock
  given up or the next task allowed. status() still answers afterwards.
- After it, the reservation is settled once: the reported usage priced item
  by item; above the reservation it is charged in full and the approval
  paused; without usable usage it is charged at the reservation and the
  approval paused; a request its provider says never left is released.
  Settling it again changes nothing.

Amounts are in millionths of a US dollar, rounded up. Prices come from the
approval's terms only, never from this code. A worst case is only as good as
its input bound: real use needs a bound checked against the provider's
documents for the model and the service tier, added to CERTIFIED_INPUT_BOUNDS
as a reviewed change of this code, never by a setting. It is empty, so no
real request can pass. Synthetic prices and bounds exist for tests: a gate
with either refuses any provider that is not synthetic itself (a provider
made up for tests says so with SYNTHETIC = True; OpenAIProvider never does).
"""
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select

from app.ai_provider import (
    AIProvider,
    AIProviderError,
    GenerationResult,
    JsonSchemaFormat,
    OpenAIProvider,
    SendState,
    Usage,
    is_count,
)
from app.library.clock import Clock
from app.library.db import Database
from app.library.models import LiveApproval, LiveRequest, LiveRequestState

try:
    import fcntl
except ImportError:  # no such locks on this system: a real service is refused
    fcntl = None

logger = logging.getLogger("roleradar.live")

# Next to the library database: its presence stops every request after the one in flight.
STOP_FILE = "LIVE_STOP"
LOCK_SUFFIX = ".live.lock"


class LiveRefused(Exception):
    """A real service may not be used; the message says why, in words for the Owner."""


class Purpose(StrEnum):
    ANALYSIS = "analysis"
    STRUCTURING = "structuring"


class Blocked(StrEnum):
    # The stop file is there.
    STOPPED = "stopped"
    # The approval is paused (a cost unknown or above its reservation); going on takes a new approval.
    PAUSED = "paused"
    # Another real task is running, or a request is not settled yet.
    BUSY = "busy"
    # The approval does not cover this purpose.
    PURPOSE = "purpose"
    # The approval has no request left.
    REQUESTS = "requests"
    # This request's worst case would pass the approval's cost limit.
    COST = "cost"
    # The gate was closed: nothing more goes out through it.
    CLOSED = "closed"
    # The task this provider belonged to has ended.
    ENDED = "ended"


class LiveBlocked(Exception):
    """The gate did not let a request through: nothing was sent, reserved or charged."""

    def __init__(self, reason: Blocked):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class PriceBasis:
    """US dollars per million tokens for each item a request may be billed for, on the approval's tier.

    cache_read and cache_write are None where the service does not bill them
    as items of their own: such tokens are then billed as ordinary input.
    output covers reasoning tokens. Each input token is taken to be billed
    as one item only. source and checked_on say where and when the prices
    were read; synthetic prices are made up for tests.
    """

    input: Decimal
    cache_read: Decimal | None
    cache_write: Decimal | None
    output: Decimal
    source: str
    checked_on: str
    synthetic: bool = False

    def __post_init__(self) -> None:
        for price in (self.input, self.cache_read, self.cache_write, self.output):
            if price is not None and (not isinstance(price, Decimal) or not price.is_finite() or price < 0):
                raise ValueError("a price is a finite Decimal of 0 or more")

    def snapshot(self) -> dict:
        return {
            "input": _amount(self.input),
            "cache_read": _amount(self.cache_read),
            "cache_write": _amount(self.cache_write),
            "output": _amount(self.output),
            "source": self.source,
            "checked_on": self.checked_on,
            "synthetic": self.synthetic,
        }


@dataclass(frozen=True)
class InputBound:
    """A way to tell, before a request goes out, at least as many input tokens as the service will count.

    tokens(prompt, output_format) is that number. source and checked_on say
    which documents showed it to be a bound; synthetic ones are made up for
    tests.
    """

    name: str
    tokens: Callable[[str, JsonSchemaFormat | None], int]
    source: str
    checked_on: str
    synthetic: bool = False


# Input bounds checked against the provider's documents, by (provider, model,
# service tier). A real service is refused for anything not listed here.
# Empty: no bound has been checked yet (U2-C1).
CERTIFIED_INPUT_BOUNDS: dict[tuple[str, str, str], InputBound] = {}


@dataclass(frozen=True)
class ApprovalTerms:
    """Everything one approval covers. snapshot() is what the ledger keeps and compares."""

    approval_id: str
    purposes: frozenset[str]
    provider: str
    model: str
    endpoint: str
    service_tier: str
    # None: requests send no reasoning effort at all.
    reasoning_effort: str | None
    max_output_tokens: int
    timeout_seconds: float
    structuring_max_chars: int
    max_requests: int
    max_cost_micro_usd: int
    prices: PriceBasis
    # The name of the input bound the worst cases are worked out with.
    input_bound: str
    rules_checked_on: str

    def snapshot(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "purposes": sorted(self.purposes),
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint,
            "service_tier": self.service_tier,
            "reasoning_effort": self.reasoning_effort,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "structuring_max_chars": self.structuring_max_chars,
            "max_requests": self.max_requests,
            "max_cost_micro_usd": self.max_cost_micro_usd,
            "prices": self.prices.snapshot(),
            "input_bound": self.input_bound,
            "rules_checked_on": self.rules_checked_on,
        }


@dataclass(frozen=True)
class GateStatus:
    """Where an approval stands: what it has used, what is left, and why it is paused if it is."""

    paused_reason: str | None
    requests_used: int
    requests_left: int
    # Charged, with any reservation not settled yet counted at its full amount.
    committed_micro_usd: int
    micro_usd_left: int


def reservation(input_tokens: int, output_tokens: int, prices: PriceBasis) -> int:
    """The most a request of these sizes may cost: every input token at the dearest input item."""
    dearest = max(price for price in (prices.input, prices.cache_read, prices.cache_write) if price is not None)

    return _micro(input_tokens * dearest + output_tokens * prices.output)


def charge(usage: Usage | None, terms: ApprovalTerms) -> int | None:
    """What a request cost by the approval's prices, rounded up; None when its usage cannot tell.

    Every count given must be a whole number of 0 or more, and the counts
    must agree, whether or not their items are billed on their own: the two
    cache items are parts of the input and together no more than it,
    reasoning is part of the output, and a total, when given, is input plus
    output. Input, output and the approved service tier must be given, and
    so must the count of a cache item billed on its own; the other counts may
    be left out. A cache item billed on its own is taken out of the input and
    billed once at its own price; reasoning is billed as output, never again.
    Anything else, or usage its provider found malformed, is unknown.
    """
    if usage is None or usage.malformed:
        return None

    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
    )

    if not all(count is None or is_count(count) for count in counts):
        return None

    if usage.input_tokens is None or usage.output_tokens is None or usage.service_tier != terms.service_tier:
        return None

    if (usage.cache_read_tokens or 0) + (usage.cache_write_tokens or 0) > usage.input_tokens:
        return None

    if usage.reasoning_tokens is not None and usage.reasoning_tokens > usage.output_tokens:
        return None

    if usage.total_tokens is not None and usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return None

    prices = terms.prices
    plain = usage.input_tokens
    total = Decimal(0)

    for count, price in ((usage.cache_read_tokens, prices.cache_read), (usage.cache_write_tokens, prices.cache_write)):
        if price is None:
            # Not an item of its own: these tokens stay in the input.
            continue

        if count is None:
            return None

        total += count * price
        plain -= count

    return _micro(total + plain * prices.input + usage.output_tokens * prices.output)


def _micro(amount: Decimal) -> int:
    # Tokens times dollars per million tokens are millionths of a dollar.
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def _amount(price: Decimal | None) -> str | None:
    # One spelling per value, so that "0.50" and "0.5" are the same term.
    return None if price is None else format(price.normalize(), "f")


class ExclusiveLock:
    """An exclusive lock on one file, held until release() or until the process ends."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        if fcntl is None:
            raise LiveRefused("这台机器上取不到独占锁，不能启用真实服务。")

        handle = open(self.path, "ab")

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise LiveRefused("另一个应用正在使用这个经历库的真实服务账本；同一时间只能有一个应用使用。") from None

        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            # Closing the file gives up its lock.
            self._handle.close()
            self._handle = None


def open_gate(database: Database, db_path: Path, terms: ApprovalTerms, bound: InputBound, clock: Clock) -> "LiveGate":
    """The gate for one approval, or LiveRefused saying why not (see the module docstring for the order)."""
    if terms.input_bound != bound.name:
        raise LiveRefused("批准写明的输入上界和这里用来预留的不是同一个。")

    synthetic = terms.prices.synthetic or bound.synthetic

    if not synthetic and CERTIFIED_INPUT_BOUNDS.get((terms.provider, terms.model, terms.service_tier)) is not bound:
        raise LiveRefused("这个模型和服务档还没有核验过的输入上界，不能启用真实服务。")

    lock = ExclusiveLock(db_path.with_name(db_path.name + LOCK_SUFFIX))
    lock.acquire()

    try:
        _recover(database, clock)
        _register(database, terms, clock)
    except BaseException:
        lock.release()
        raise

    return LiveGate(database, terms, bound, clock, lock, db_path.parent / STOP_FILE)


def _recover(database: Database, clock: Clock) -> int:
    """Charge what a previous run left reserved at its reservation and pause its approvals; how many there were."""
    with database.transaction() as db:
        left = db.scalars(select(LiveRequest).where(LiveRequest.state == LiveRequestState.RESERVED)).all()
        now = clock.now()

        for row in left:
            # It may have gone out, and nothing says what it used.
            row.state = LiveRequestState.UNKNOWN
            row.charged_micro_usd = row.reserved_micro_usd
            row.reason = "interrupted"
            row.settled_at = now
            _pause(db, row.approval_id, "interrupted", now)

    return len(left)


def _register(database: Database, terms: ApprovalTerms, clock: Clock) -> None:
    snapshot = terms.snapshot()

    with database.transaction() as db:
        approval = db.get(LiveApproval, terms.approval_id)

        if approval is None:
            db.add(
                LiveApproval(
                    approval_id=terms.approval_id, terms=snapshot, created_at=clock.now(), paused_at=None, paused_reason=None
                )
            )
        elif approval.terms != snapshot:
            raise LiveRefused(
                f"批准 {terms.approval_id} 的条款和第一次使用时不同；同一个批准不能改动或放宽，请使用新的批准编号。"
            )


def _pause(db, approval_id: str, reason: str, now) -> None:
    approval = db.get(LiveApproval, approval_id)

    if approval is not None and approval.paused_at is None:
        approval.paused_at = now
        approval.paused_reason = reason


class LiveGate:
    """An open gate: one approval, its ledger, the lock and the stop file. Made by open_gate()."""

    def __init__(self, database: Database, terms: ApprovalTerms, bound: InputBound, clock: Clock, lock, stop_file: Path):
        self._database = database
        self.terms = terms
        self.bound = bound
        self._clock = clock
        self._lock = lock
        self.stop_file = stop_file
        # Guards whether the gate is closed, the task running, and how many requests are out.
        self._guard = threading.Condition()
        self._closed = False
        self._current: GatedProvider | None = None
        self._out = 0

    def close(self) -> None:
        """Let nothing more through; give up the lock once the requests already let through are settled.

        Closing again changes nothing. status() still answers afterwards.
        """
        with self._guard:
            self._closed = True

            while self._out:
                self._guard.wait()

        self._lock.release()

    @contextmanager
    def task(self, purpose: str, owner_id: str, provider: AIProvider) -> Iterator["GatedProvider"]:
        """One real task: its provider behind the gate, letting requests through only inside this block.

        Another task meanwhile, or any once the gate is closed, is refused with
        nothing sent. Leaving the block, the provider lets nothing more
        through, and the next task may start once the requests it let through
        are settled.
        """
        self._check_provider(provider)

        if purpose not in self.terms.purposes:
            raise LiveBlocked(Blocked.PURPOSE)

        with self._guard:
            if self._closed:
                raise LiveBlocked(Blocked.CLOSED)

            if self._current is not None:
                raise LiveBlocked(Blocked.BUSY)

            gated = GatedProvider(self, purpose, owner_id, provider)
            self._current = gated

        try:
            yield gated
        finally:
            with self._guard:
                gated._over = True

                while gated._out:
                    self._guard.wait()

                self._current = None
                self._guard.notify_all()

    def _refusal(self, gated: "GatedProvider") -> Blocked | None:
        """Why this task's provider may not send now, if it may not; call with the guard held."""
        if self._closed:
            return Blocked.CLOSED

        if gated._over or self._current is not gated:
            return Blocked.ENDED

        return None

    def _admit(self, gated: "GatedProvider") -> None:
        """Count one request of this task as out, or LiveBlocked if the gate or the task is over."""
        with self._guard:
            refusal = self._refusal(gated)

            if refusal is not None:
                raise LiveBlocked(refusal)

            self._out += 1
            gated._out += 1

    def _still_open(self, gated: "GatedProvider") -> Blocked | None:
        with self._guard:
            return self._refusal(gated)

    def _done(self, gated: "GatedProvider") -> None:
        with self._guard:
            self._out -= 1
            gated._out -= 1
            self._guard.notify_all()

    def _check_provider(self, provider: AIProvider) -> None:
        if self.terms.prices.synthetic or self.bound.synthetic:
            if isinstance(provider, OpenAIProvider) or not getattr(provider, "SYNTHETIC", False):
                raise LiveRefused("合成的价格或输入上界只能配合合成服务商使用，不能放行真实服务。")

        identity = provider.identity()
        approved = (self.terms.provider, self.terms.model, self.terms.endpoint)

        if identity is None or (identity.provider, identity.requested_model, identity.endpoint) != approved:
            raise LiveRefused("这个服务商不是批准里写明的接收方。")

    def worst_case(self, prompt: str, output_format: JsonSchemaFormat | None, output_limit: int) -> int:
        return reservation(self.bound.tokens(prompt, output_format), output_limit, self.terms.prices)

    def _reserve(self, purpose: str, owner_id: str, micro_usd: int) -> str:
        """Reserve one request's worst case; its id, or LiveBlocked with nothing written."""
        with self._guard:
            if self._closed:
                raise LiveBlocked(Blocked.CLOSED)

        if self.stop_file.exists():
            raise LiveBlocked(Blocked.STOPPED)

        terms = self.terms
        request_id = str(uuid.uuid4())

        with self._database.transaction() as db:
            approval = db.get(LiveApproval, terms.approval_id)

            if approval is None or approval.paused_at is not None:
                raise LiveBlocked(Blocked.PAUSED)

            if db.scalars(select(LiveRequest.id).where(LiveRequest.state == LiveRequestState.RESERVED)).first():
                raise LiveBlocked(Blocked.BUSY)

            used, committed = _committed(db, terms.approval_id)

            if used + 1 > terms.max_requests:
                raise LiveBlocked(Blocked.REQUESTS)

            if committed + micro_usd > terms.max_cost_micro_usd:
                raise LiveBlocked(Blocked.COST)

            db.add(
                LiveRequest(
                    id=request_id,
                    approval_id=terms.approval_id,
                    purpose=purpose,
                    owner_id=owner_id,
                    state=LiveRequestState.RESERVED,
                    reserved_micro_usd=micro_usd,
                    charged_micro_usd=None,
                    input_tokens=None,
                    cache_read_tokens=None,
                    cache_write_tokens=None,
                    output_tokens=None,
                    service_tier=None,
                    reason=None,
                    reserved_at=self._clock.now(),
                    settled_at=None,
                )
            )

        return request_id

    def settle(self, request_id: str, *, sent: SendState, usage: Usage | None) -> str | None:
        """Settle a reservation once; one settled already is left as it is. Its state, or None if there is none."""
        with self._database.transaction() as db:
            row = db.get(LiveRequest, request_id)

            if row is None or row.state != LiveRequestState.RESERVED:
                return None if row is None else row.state

            now = self._clock.now()

            if usage is not None:
                # What was reported as it was, where it is a count; a malformed one is not kept.
                row.input_tokens = _kept(usage.input_tokens)
                row.cache_read_tokens = _kept(usage.cache_read_tokens)
                row.cache_write_tokens = _kept(usage.cache_write_tokens)
                row.output_tokens = _kept(usage.output_tokens)
                row.service_tier = usage.service_tier if isinstance(usage.service_tier, str) else None

            if sent is SendState.NOT_SENT and usage is None:
                row.state = LiveRequestState.RELEASED
                row.charged_micro_usd = 0
            else:
                cost = charge(usage, self.terms)

                if cost is None:
                    row.state = LiveRequestState.UNKNOWN
                    row.charged_micro_usd = row.reserved_micro_usd
                    row.reason = "unknown_usage"
                    _pause(db, row.approval_id, "unknown_usage", now)
                elif cost > row.reserved_micro_usd:
                    # The worst case was not one: charge what is known, all of it, and stop.
                    row.state = LiveRequestState.OVER
                    row.charged_micro_usd = cost
                    row.reason = "over_reservation"
                    _pause(db, row.approval_id, "over_reservation", now)
                else:
                    row.state = LiveRequestState.SETTLED
                    row.charged_micro_usd = cost

            row.settled_at = now

            return row.state

    def status(self) -> GateStatus:
        terms = self.terms

        with self._database.transaction() as db:
            approval = db.get(LiveApproval, terms.approval_id)
            used, committed = _committed(db, terms.approval_id)

        return GateStatus(
            paused_reason=None if approval is None else approval.paused_reason,
            requests_used=used,
            requests_left=max(terms.max_requests - used, 0),
            committed_micro_usd=committed,
            micro_usd_left=max(terms.max_cost_micro_usd - committed, 0),
        )


def _kept(count: object) -> int | None:
    return count if is_count(count) else None


def _committed(db, approval_id: str) -> tuple[int, int]:
    """Requests that count against an approval, and what they are charged (reservations at full)."""
    rows = db.scalars(
        select(LiveRequest).where(LiveRequest.approval_id == approval_id, LiveRequest.state != LiveRequestState.RELEASED)
    ).all()
    committed = sum(
        row.reserved_micro_usd if row.state == LiveRequestState.RESERVED else row.charged_micro_usd for row in rows
    )

    return len(rows), committed


class GatedProvider(AIProvider):
    """A provider behind the gate, for one task: each request reserved before it is handed on, settled after.

    It lets requests through only while its task runs and the gate is open,
    checked before reserving and again right before handing on. attempts
    counts the requests asked for; blocked those the gate stopped, which
    never went out; handed those passed on to the provider, which may have
    gone out whatever came back. Every request asks for the approval's
    reasoning effort and at most its output limit, whatever the caller asks.
    """

    def __init__(self, gate: LiveGate, purpose: str, owner_id: str, provider: AIProvider):
        self._gate = gate
        self._purpose = purpose
        self._owner_id = owner_id
        self._provider = provider
        self.attempts = 0
        self.blocked = 0
        self.handed = 0
        # Under the gate's guard: whether its task has ended, and its requests out.
        self._over = False
        self._out = 0

    def identity(self):
        return self._provider.identity()

    def generate_text(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
    ) -> GenerationResult:
        gate = self._gate
        limit = gate.terms.max_output_tokens
        limit = limit if max_output_tokens is None else min(max_output_tokens, limit)
        self.attempts += 1

        try:
            gate._admit(self)
        except LiveBlocked:
            self.blocked += 1
            raise

        try:
            return self._send(prompt, limit, json_schema_format)
        finally:
            gate._done(self)

    def _send(self, prompt: str, limit: int, json_schema_format: JsonSchemaFormat | None) -> GenerationResult:
        gate = self._gate

        try:
            request_id = gate._reserve(self._purpose, self._owner_id, gate.worst_case(prompt, json_schema_format, limit))
        except LiveBlocked:
            self.blocked += 1
            raise

        refusal = gate._still_open(self)

        if refusal is not None:
            # The gate closed or the task ended while it was reserved: it does not go out.
            gate.settle(request_id, sent=SendState.NOT_SENT, usage=None)
            self.blocked += 1

            raise LiveBlocked(refusal)

        try:
            result = self._provider.generate_text(
                prompt,
                max_output_tokens=limit,
                reasoning_effort=gate.terms.reasoning_effort,
                json_schema_format=json_schema_format,
            )
        except AIProviderError as error:
            self._settled(request_id, error.sent, error.usage)
            raise
        except BaseException:
            # Whatever failed, the request may have gone out.
            self._settled(request_id, SendState.HANDED, None)
            raise

        self._settled(request_id, SendState.ANSWERED, result.usage, quiet=False)

        return result

    def _settled(self, request_id: str, sent: SendState, usage: Usage | None, *, quiet: bool = True) -> None:
        if sent is not SendState.NOT_SENT or usage is not None:
            self.handed += 1

        try:
            self._gate.settle(request_id, sent=sent, usage=usage)
        except Exception as error:
            if not quiet:
                raise

            # Left reserved, it keeps the gate busy until a restart charges it as unknown.
            logger.error("a real request could not be settled (%s)", type(error).__name__)
