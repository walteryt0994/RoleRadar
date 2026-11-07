"""The gate every real AI request goes through, and the ledger of what each one cost (U2-C1).

Nothing here sends anything by itself: U2-C2 and U2-C3 put it in front of
the real provider. An app opens one gate, for one approval (open_gate):

- First an exclusive lock on a file next to the library database: one app
  at a time may use its ledger. A second app is refused before it reads or
  writes any of it.
- Holding the lock, requests a previous run left reserved are kept at
  their reservation as unknown, and their approvals paused for good. Then
  the approval's terms are compared with the snapshot taken when they were
  first used: any difference is refused, so neither a restart nor a changed
  setting widens an approval, nor turns it into another kind.
- One real task at a time (task()), and its provider lets requests through
  only while that task runs and the gate is open. Before each request, in
  one write transaction: no stop file, the approval not paused, no other
  request unsettled, and room under both the request limit and the cost
  limit for what this request is reserved at; then the task and the gate
  are checked once more, the caller's own task last of all (task(),
  sending), and only then is the request handed on.
- Closing the gate, or leaving a task, lets nothing more through and waits
  for the requests already let through to be settled; only then is the lock
  given up or the next task allowed. status() still answers afterwards.
- After it, the reservation is settled once: the reported usage priced item
  by item; above the reservation it is charged in full and the approval
  paused; without usable usage it is kept at a conservative amount, as cost
  still to be checked, and the approval paused; a request its provider says
  never left is released. Settling it again changes nothing. The ledger row
  keeps which check the usage failed, or that its cache writes were priced
  by a documented default, and the log says the same with the cache counts
  (C4 R1); neither keeps anything the request or the answer carried.

What a request is reserved at comes from the approval's basis. A real
service uses a budget policy (FixedReserve, U2-C cost control): every
request is one generation, reserved at a fixed number of input tokens at
the reservation price plus its output limit, and refused before anything
is reserved or sent when its whole body, measured here, is too large.
That reservation is an engineering choice, not an input bound and not a
count: the cost limit is a budget this app checks before each request,
never a hard limit on the provider's bill, and a request already out may
cost more than its reservation. A counted input bound (U2-C4 prerequisites,
kept; none is registered) makes each request a group of two, one ledger
row: the service counts the request's input first, then at most one
generation, the reservation taken at the approval's largest input and
narrowed by the count; the row's reason says how far a reserved group got
(COUNTING, COUNTED), so a restart can tell. Bounds worked out here are for
tests only.

Amounts are in millionths of a US dollar, rounded up. Prices come from the
approval's terms only, never from this code: a reservation takes every
input token it plans for at the reservation price, the dearest on the
price list, never used to settle. Usage is checked whole before it is
priced, and priced only within what the approval's prices were checked for
(max_input_tokens, a range for accounting, never a promise made before a
request goes out). A real service needs its model and service tier given a
budget policy in RESERVE_POLICIES, or a counted bound in
CERTIFIED_INPUT_BOUNDS, each a reviewed change of this code, never a
setting; and even then nothing goes out without an approval the Owner
writes out in full. Synthetic prices, policies and bounds exist for tests:
a gate with any of them refuses any provider that is not synthetic itself
(a provider made up for tests says so with SYNTHETIC = True; OpenAIProvider
never does).
"""
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from app.ai_provider import (
    AIProvider,
    CacheWriteSplit,
    GenerationResult,
    JsonSchemaFormat,
    OpenAIProvider,
    SendState,
    Usage,
    how_far,
    is_count,
    request_bytes,
    went_out,
)
from app.library.clock import Clock
from app.library.db import Database
from app.library.models import LiveApproval, LiveRequest, LiveRequestState
from app.library.sends import Call, Delivery

try:
    import fcntl
except ImportError:  # no such locks on this system: a real service is refused
    fcntl = None

logger = logging.getLogger("roleradar.live")

# Next to the library database: its presence stops every request after the one in flight.
STOP_FILE = "LIVE_STOP"
LOCK_SUFFIX = ".live.lock"
# Dollars per request, as millionths of a dollar, the unit amounts are kept in.
PER_REQUEST = Decimal(1_000_000)
# What a ledger row says beyond its state (LiveRequest.reason). While a counted group is reserved:
COUNTING = "counting"  # its count may be out; its generation is not
COUNTED = "counted"  # counted and narrowed; its generation may be out
# Once settled, of a group whose generation never went out:
COUNT_SENT = "count_sent"  # its count went out
COUNT_MAYBE = "count_maybe"  # its count may have gone out
# Of a request whose usage tells nothing usable, or that a restart found reserved:
UNKNOWN_USAGE = "unknown_usage"  # it was answered (the approval's pause; rows before C4 R1 said only this)
NO_ANSWER = "no_answer"  # no answer came: it may have gone out
INTERRUPTED = "interrupted"  # a generation may have gone out (a counted group: after its count)
INTERRUPTED_COUNTING = "interrupted_counting"  # only its count may have gone out
# Of an answered request, why its usage could not be priced (account), in place of UNKNOWN_USAGE:
USAGE_MISSING = "usage_missing"  # no usage, or no input or output count
USAGE_MALFORMED = "usage_malformed"  # a count that is no count
TIER_MISMATCH = "tier_mismatch"  # run on another service tier than approved
INPUT_OVER_RANGE = "input_over_range"  # more input than the approval's prices were checked for
CACHE_OVER_INPUT = "cache_over_input"  # cache reads and writes together more than the input
CACHE_COUNT_MISSING = "cache_count_missing"  # a cache item billed on its own, its count not given
CACHE_SPLIT_ABSENT = "cache_split_absent"  # writes, nothing said of how long they are kept, no default to go by
CACHE_SPLIT_UNRECOGNIZED = "cache_split_unrecognized"  # said under other names or in another form
CACHE_SPLIT_MALFORMED = "cache_split_malformed"  # said with something that is no count
CACHE_SPLIT_INCOMPLETE = "cache_split_incomplete"  # a breakdown with neither count in it
CACHE_SPLIT_MISMATCH = "cache_split_mismatch"  # the two not adding up to the writes
CACHE_WRITE_12H = "cache_write_12h"  # kept 12 hours: no price is approved for that
REASONING_OVER_OUTPUT = "reasoning_over_output"
TOTAL_MISMATCH = "total_mismatch"  # a total that is not input plus output
# Of a request whose usage could be priced:
OVER_RESERVATION = "over_reservation"
ABOVE_COUNT = "above_count"  # its input came out above its count
# Writes said nothing of how long they are kept, priced as kept for the documented default (DefaultCacheTtl).
CACHE_TTL_DEFAULT = "cache_ttl_default"
# How long the writes an approval's cache-write price is for are kept.
APPROVED_WRITE_TTL = "30m"


class RequestUnit(StrEnum):
    """What one request of an approval is: its terms say, so that no approval is read in another unit."""

    # One generation, its input bound worked out here (synthetic bounds only).
    GENERATION = "generation"
    # A count of the input by the service, then at most one generation (a counted bound).
    COUNTED_GENERATION = "count_then_generation"


class LiveRefused(Exception):
    """A real service may not be used; the message says why, in words for the Owner."""


class Purpose(StrEnum):
    ANALYSIS = "analysis"
    STRUCTURING = "structuring"
    # A whole search for postings (U3-A2): its decisions and the analyses within it, under one task and one
    # ledger. Its requests take the analysis output limit; a decision asks for less (app.discovery.agent).
    DISCOVERY = "discovery"


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
    # What this request is reserved at would pass the approval's cost limit.
    COST = "cost"
    # The gate was closed: nothing more goes out through it.
    CLOSED = "closed"
    # The task this provider belonged to has ended.
    ENDED = "ended"
    # The count failed, was no whole number, or left no time for the generation.
    COUNT = "count"
    # With no count before it, the work here (its body built and measured, its reservation, the last
    # checks) used up the generation's time before it was handed on: nothing of it went out.
    TIME = "time"
    # The request's input is above the approval's largest (counted, or worked out here).
    INPUT = "input"
    # The request's whole body, measured here, is larger than its budget policy allows, or is no plain text.
    PAYLOAD = "payload"


class LiveBlocked(Exception):
    """The gate did not let a generation through: nothing of it was sent or charged.

    A counted group's count may have gone out before (the task's sending
    says), and the ledger keeps what that cost; otherwise nothing was sent
    and the reservation, if any, was released.
    """

    def __init__(self, reason: Blocked):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class PriceBasis:
    """US dollars per million tokens for each item a request may be billed for, on the approval's tier.

    cache_read and cache_write are None where the service does not bill them
    as items of their own: such tokens are then billed as ordinary input;
    cache_write is the price of the writes kept the default time (30
    minutes), the only ones a request here asks for. output covers reasoning
    tokens. Each input token is taken to be billed as one item only. These
    four settle what a request cost.

    reserve_input is for reservations only, never to settle: the dearest
    price any input token may be billed at on the price list, items not
    asked for included, so never below the three input prices above. count
    is the price of one count of a request's input, in dollars per count;
    None where it is not known, which a counted bound refuses. source and
    checked_on say where and when the prices were read; synthetic prices are
    made up for tests.
    """

    input: Decimal
    cache_read: Decimal | None
    cache_write: Decimal | None
    output: Decimal
    reserve_input: Decimal
    source: str
    checked_on: str
    synthetic: bool = False
    count: Decimal | None = None

    def __post_init__(self) -> None:
        for price in (self.input, self.cache_read, self.cache_write, self.output, self.reserve_input, self.count):
            if price is not None and (not isinstance(price, Decimal) or not price.is_finite() or price < 0):
                raise ValueError("a price is a finite Decimal of 0 or more")

        if any(
            price is not None and price > self.reserve_input for price in (self.input, self.cache_read, self.cache_write)
        ):
            raise ValueError("the reservation's input price is below a price an input token may be billed at")

    def snapshot(self) -> dict:
        return {
            "input": _amount(self.input),
            "cache_read": _amount(self.cache_read),
            "cache_write": _amount(self.cache_write),
            "output": _amount(self.output),
            "reserve_input": _amount(self.reserve_input),
            "count": _amount(self.count),
            "source": self.source,
            "checked_on": self.checked_on,
            "synthetic": self.synthetic,
        }


@dataclass(frozen=True)
class InputBound:
    """A way to tell, before a request goes out, at least as many input tokens as the service will count.

    A bound worked out here gives tokens(prompt, output_format); a counted
    one has none, and asks the service itself for each request, which is a
    request too (the gate's counted groups). Only a counted one may serve a
    real service. source and checked_on say which documents showed it to be
    a bound; synthetic ones are made up for tests.
    """

    name: str
    tokens: Callable[[str, JsonSchemaFormat | None], int] | None
    source: str
    checked_on: str
    synthetic: bool = False
    counted: bool = False

    def __post_init__(self) -> None:
        if self.counted == (self.tokens is not None):
            raise ValueError("a bound is counted by the service, or worked out here by tokens, not both")


# Input bounds checked against the provider's documents, by (provider, model,
# service tier); only counted bounds belong here. Empty: no bound has been
# registered (U2-C1; the counting it needs exists since the U2-C4
# prerequisites, unregistered). A real service now goes by a budget policy
# (RESERVE_POLICIES) instead.
CERTIFIED_INPUT_BOUNDS: dict[tuple[str, str, str], InputBound] = {}
# What an approval's cost limit is under a budget policy: a budget this app checks before each request,
# never a hard limit on the provider's bill.
APPLICATION = "application"
# What a request body under a budget policy may hold: plain text in, a format out; never tools, files,
# images, earlier responses or conversations, which a fixed reservation was never made for.
PLAIN_TEXT_REQUEST = frozenset({"model", "input", "store", "max_output_tokens", "reasoning", "service_tier", "text"})


@dataclass(frozen=True)
class DefaultCacheTtl:
    """Why writes reported with nothing said of how long they are kept may be priced as kept for ttl (C4 R1).

    A request under a budget policy sets nothing about caching: its body has
    only the names of PLAIN_TEXT_REQUEST and its input is plain text, so no
    cache options and no breakpoints. For its model the provider documents
    how long writes are kept then (source, read on checked_on). A reviewed
    basis of the policy it belongs to, never a setting, and only for writes
    of which nothing at all is said: a breakdown given in whatever way, a
    12-hour write among them, is never replaced by it.
    """

    ttl: str
    source: str
    checked_on: str


# GPT-5.6 and later: "Omitting both settings preserves the default "30m" behavior".
GPT_5_6_DEFAULT_TTL = DefaultCacheTtl("30m", "https://developers.openai.com/api/docs/guides/prompt-caching", "2026-11-03")


@dataclass(frozen=True)
class FixedReserve:
    """A budget policy (U2-C cost control): what each request is reserved at, and how large it may be.

    Every request is one generation, reserved at reserve_input_tokens at the
    reservation price plus its output limit at the output price, whatever
    its input: an engineering choice, neither a count nor a bound any
    documents give. Before anything is reserved or sent, the whole body the
    request goes out with (AIProvider.request_body, measured by
    ai_provider.request_bytes) must be plain text of at most
    max_payload_bytes: a limit on what is seen here, not on tokens. What a
    request cost is then accounted from its usage. The cost limit it serves
    is an application-side budget (APPLICATION), checked before each
    request: a request already out may cost more than it was reserved at.
    name is its version; synthetic ones are made up for tests.
    default_cache_ttl, where given, is how long the model keeps writes of
    which nothing is said (DefaultCacheTtl); without it such writes are
    unknown.
    """

    name: str
    reserve_input_tokens: int
    max_payload_bytes: int
    synthetic: bool = False
    default_cache_ttl: DefaultCacheTtl | None = None


FIXED_RESERVE_V1 = FixedReserve(
    "fixed_reserve_v1", reserve_input_tokens=16_000, max_payload_bytes=32_000, default_cache_ttl=GPT_5_6_DEFAULT_TTL
)
# Budget policies by (provider, model, service tier): a reviewed change of this code, never a setting. Being
# listed lets nothing out by itself: a real service also needs ROLERADAR_ANALYSIS_MODE=live and an approval
# written out in full, and the default stays the simulation.
RESERVE_POLICIES: dict[tuple[str, str, str], FixedReserve] = {("openai", "gpt-5.6-sol", "default"): FIXED_RESERVE_V1}


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
    # The output limit of an analysis request; a structuring request has its own (U2-C3).
    max_output_tokens: int
    structuring_max_output_tokens: int
    timeout_seconds: float
    # The most a structuring request may send of a resume: UTF-8 bytes of its masked text (U2-C3).
    structuring_max_bytes: int
    # How many requests, each one of request_unit.
    max_requests: int
    max_cost_micro_usd: int
    prices: PriceBasis
    # The name of the input bound reservations are worked out with; None under a budget policy.
    input_bound: str | None
    rules_checked_on: str
    # What one of max_requests is (RequestUnit): its basis decides.
    request_unit: str
    # The most input tokens the prices were checked for: usage above it is not priced (unknown). A
    # range for accounting, never a promise made before a request goes out; a counted request is also
    # never let through with more.
    max_input_tokens: int
    # The budget policy and its terms (FixedReserve), or None for an input bound; written out, never taken
    # from another approval.
    budget_policy: str | None = None
    budget_scope: str | None = None
    reserve_input_tokens: int | None = None
    max_payload_bytes: int | None = None

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
            "structuring_max_output_tokens": self.structuring_max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "structuring_max_bytes": self.structuring_max_bytes,
            "max_requests": self.max_requests,
            "max_cost_micro_usd": self.max_cost_micro_usd,
            "prices": self.prices.snapshot(),
            "input_bound": self.input_bound,
            "rules_checked_on": self.rules_checked_on,
            "request_unit": self.request_unit,
            "max_input_tokens": self.max_input_tokens,
            "budget_policy": self.budget_policy,
            "budget_scope": self.budget_scope,
            "reserve_input_tokens": self.reserve_input_tokens,
            "max_payload_bytes": self.max_payload_bytes,
        }

    def output_limit(self, purpose: str) -> int:
        """The output limit of a request for this purpose."""
        return self.structuring_max_output_tokens if purpose == Purpose.STRUCTURING else self.max_output_tokens


@dataclass(frozen=True)
class LiveSetup:
    """What an app needs for a real service (U2-C2): an approval, its basis and how to build its provider.

    The basis is its budget policy (a real service, U2-C cost control) or an
    input bound. web_main opens the gate for it. Tests pass synthetic ones,
    and the gate never lets those reach a provider that is not synthetic
    itself.
    """

    terms: ApprovalTerms
    basis: "FixedReserve | InputBound"
    make_provider: Callable[[], AIProvider]


@dataclass(frozen=True)
class GateStatus:
    """Where an approval stands: what it has used, what is left, and why it is paused if it is."""

    paused_reason: str | None
    requests_used: int
    requests_left: int
    # All that counts against the budget: settled, reserved and still to be checked together.
    committed_micro_usd: int
    micro_usd_left: int
    # Accounted from the usage reported (settled, or above its reservation); none of it is a bill.
    settled_micro_usd: int = 0
    # Reservations of requests not settled yet.
    reserved_micro_usd: int = 0
    # Kept at a conservative amount because what they cost could not be known: still to be checked.
    unknown_micro_usd: int = 0


def reservation(input_tokens: int, output_tokens: int, prices: PriceBasis, *, counted: bool = False) -> int:
    """What a request of these sizes is reserved at: every input token at the reservation price; a counted one adds its count."""
    amount = input_tokens * prices.reserve_input + output_tokens * prices.output

    if counted:
        amount += prices.count * PER_REQUEST

    return _micro(amount)


def count_price(prices: PriceBasis) -> int:
    """What one count of a request's input is charged at."""
    return _micro(prices.count * PER_REQUEST)


def delivery(sent: SendState, usage: Usage | None) -> Delivery:
    """How sure it is that a request handed on reached its service (app.library.sends), by the rule of went_out.

    Sent when it was answered or something was reported for it; maybe when
    no answer came; not sent when its provider stopped it first and nothing
    was reported.
    """
    if usage is not None or sent is SendState.ANSWERED:
        return Delivery.SENT

    return Delivery.MAYBE if sent is SendState.HANDED else Delivery.NOT_SENT


@dataclass(frozen=True)
class Accounting:
    """What a generation's usage comes to by the approval's prices (account).

    amount is its cost, or None when the usage cannot tell, and then problem
    says why (USAGE_MISSING, TIER_MISMATCH, CACHE_SPLIT_ABSENT, …); basis is
    CACHE_TTL_DEFAULT when its writes were priced by a documented default.
    Neither holds anything the request or the answer carried.
    """

    amount: int | None
    problem: str | None = None
    basis: str | None = None


def account(usage: Usage | None, terms: ApprovalTerms, default_ttl: DefaultCacheTtl | None = None) -> Accounting:
    """What a generation cost by the approval's prices, rounded up, or why its usage cannot tell.

    The usage is checked whole before anything is priced. Every count given
    must be a whole number of 0 or more, and the counts must agree, whether
    or not their items are billed on their own: the two cache items are
    parts of the input and together no more than it, the writes by how long
    they are kept add up to the writes, reasoning is part of the output, and
    a total, when given, is input plus output. Input, output and the
    approved service tier must be given, the input no larger than the
    approval's prices were checked for, and so must the count of a cache
    item billed on its own; the other counts may be left out. Where writes
    are billed on their own, the service must say how long they are kept,
    under the documented names and nothing else, and none may be kept
    longer than the default: no price is approved for that. Only where
    it says nothing at all, and default_ttl (the policy's DefaultCacheTtl)
    gives the approved write's lifetime, are the writes priced as kept that
    long, and the basis says so. A cache item billed on its own is taken out
    of the input and billed once at its own price; reasoning is billed as
    output, never again. Anything else, or usage its provider found
    malformed, is unknown, with the first check it failed.
    """
    if usage is None:
        return Accounting(None, USAGE_MISSING)

    split = usage.write_split

    if usage.malformed or split is CacheWriteSplit.MALFORMED:
        return Accounting(None, CACHE_SPLIT_MALFORMED if split is CacheWriteSplit.MALFORMED else USAGE_MALFORMED)

    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.reasoning_tokens,
        usage.total_tokens,
        usage.cache_write_30m_tokens,
        usage.cache_write_12h_tokens,
    )

    if not all(count is None or is_count(count) for count in counts):
        return Accounting(None, USAGE_MALFORMED)

    if usage.input_tokens is None or usage.output_tokens is None:
        return Accounting(None, USAGE_MISSING)

    if usage.service_tier != terms.service_tier:
        return Accounting(None, TIER_MISMATCH)

    if usage.input_tokens > terms.max_input_tokens:
        # Beyond what the approval's prices were checked for.
        return Accounting(None, INPUT_OVER_RANGE)

    writes = usage.cache_write_tokens or 0

    if (usage.cache_read_tokens or 0) + writes > usage.input_tokens:
        return Accounting(None, CACHE_OVER_INPUT)

    if split is CacheWriteSplit.OTHER:
        # Whatever else it says may be writes kept otherwise: none of it is guessed at.
        return Accounting(None, CACHE_SPLIT_UNRECOGNIZED)

    kept = (usage.cache_write_30m_tokens, usage.cache_write_12h_tokens)

    if any(count is not None for count in kept) and sum(count or 0 for count in kept) != writes:
        return Accounting(None, CACHE_SPLIT_MISMATCH)

    basis = None

    if terms.prices.cache_write is not None and writes:
        if split is CacheWriteSplit.ABSENT:
            if default_ttl is None or default_ttl.ttl != APPROVED_WRITE_TTL:
                return Accounting(None, CACHE_SPLIT_ABSENT)

            basis = CACHE_TTL_DEFAULT
        elif usage.cache_write_12h_tokens:
            return Accounting(None, CACHE_WRITE_12H)
        elif all(count is None for count in kept):
            # A breakdown with neither count; one count adding up to the writes says the other is 0.
            return Accounting(None, CACHE_SPLIT_INCOMPLETE)

    if usage.reasoning_tokens is not None and usage.reasoning_tokens > usage.output_tokens:
        return Accounting(None, REASONING_OVER_OUTPUT)

    if usage.total_tokens is not None and usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return Accounting(None, TOTAL_MISMATCH)

    prices = terms.prices
    plain = usage.input_tokens
    total = Decimal(0)

    for count, price in ((usage.cache_read_tokens, prices.cache_read), (usage.cache_write_tokens, prices.cache_write)):
        if price is None:
            # Not an item of its own: these tokens stay in the input.
            continue

        if count is None:
            return Accounting(None, CACHE_COUNT_MISSING)

        total += count * price
        plain -= count

    return Accounting(_micro(total + plain * prices.input + usage.output_tokens * prices.output), basis=basis)


def charge(usage: Usage | None, terms: ApprovalTerms) -> int | None:
    """What a generation cost by the approval's prices, rounded up; None when its usage cannot tell (account,
    with no default for writes said nothing of)."""
    return account(usage, terms).amount


def _cache_facts(usage: Usage, terms: ApprovalTerms) -> str:
    """What a usage says of its cache and tier, for the log: counts and how they were given, nothing it carried."""
    return (
        f"cache read {usage.cache_read_tokens}, cache write {usage.cache_write_tokens}, "
        f"by lifetime {usage.write_split.value} (30m {usage.cache_write_30m_tokens}, 12h {usage.cache_write_12h_tokens}), "
        f"tier {'as approved' if usage.service_tier == terms.service_tier else 'not as approved'}"
    )


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


def open_gate(
    database: Database, db_path: Path, terms: ApprovalTerms, basis: "FixedReserve | InputBound", clock: Clock
) -> "LiveGate":
    """The gate for one approval, or LiveRefused saying why not (see the module docstring for the order).

    basis is the approval's budget policy or its input bound, each checked
    against what the approval wrote out.
    """
    key = (terms.provider, terms.model, terms.service_tier)

    if isinstance(basis, FixedReserve):
        _check_policy(terms, basis)

        if not (terms.prices.synthetic or basis.synthetic) and RESERVE_POLICIES.get(key) is not basis:
            raise LiveRefused("这个模型和服务档还没有定义预算策略，不能启用真实服务。")
    else:
        _check_bound(terms, basis)
        synthetic = terms.prices.synthetic or basis.synthetic

        if not synthetic and not basis.counted:
            raise LiveRefused("真实服务不能用在本地算出的输入上界：只能用预算策略，或由服务计数的输入上界。")

        if not synthetic and CERTIFIED_INPUT_BOUNDS.get(key) is not basis:
            raise LiveRefused("这个模型和服务档还没有核验过的输入上界，不能启用真实服务。")

    lock = ExclusiveLock(db_path.with_name(db_path.name + LOCK_SUFFIX))
    lock.acquire()

    try:
        _recover(database, clock)
        _register(database, terms, clock)
    except BaseException:
        lock.release()
        raise

    return LiveGate(database, terms, basis, clock, lock, db_path.parent / STOP_FILE)


def _check_policy(terms: ApprovalTerms, policy: FixedReserve) -> None:
    """An approval under a budget policy must write out that very policy and its meaning, and no input bound."""
    written = (terms.budget_policy, terms.reserve_input_tokens, terms.max_payload_bytes)

    if written != (policy.name, policy.reserve_input_tokens, policy.max_payload_bytes):
        raise LiveRefused(f"批准写明的预算策略或它的参数和 {policy.name} 不符。")

    if terms.budget_scope != APPLICATION:
        raise LiveRefused("批准要写明费用上限是应用内的预算（application），不是服务商账单的硬上限。")

    if terms.input_bound is not None or terms.request_unit != RequestUnit.GENERATION:
        raise LiveRefused("预算策略下每个请求就是一次生成，不用输入上界，也不计数。")

    if terms.prices.count is not None:
        raise LiveRefused("预算策略下不计数，计数单价应为不适用，不能写成金额。")


def _check_bound(terms: ApprovalTerms, bound: InputBound) -> None:
    """An approval under an input bound must name that bound, its unit and, for a counted one, the count's price."""
    if terms.budget_policy is not None:
        raise LiveRefused("批准写明的是预算策略，这里却用输入上界预留。")

    if terms.input_bound != bound.name:
        raise LiveRefused("批准写明的输入上界和这里用来预留的不是同一个。")

    if terms.request_unit != (RequestUnit.COUNTED_GENERATION if bound.counted else RequestUnit.GENERATION):
        raise LiveRefused("批准写明的请求计量单位和这里的输入上界不符。")

    if bound.counted and terms.prices.count is None:
        raise LiveRefused("每个请求都要先计数，但批准没有写明计数的单价；单价未知时不能当作 0。")


def _recover(database: Database, clock: Clock) -> int:
    """Charge what a previous run left reserved at its reservation and pause its approvals; how many there were."""
    with database.transaction() as db:
        left = db.scalars(select(LiveRequest).where(LiveRequest.state == LiveRequestState.RESERVED)).all()
        now = clock.now()

        for row in left:
            # It may have gone out, and nothing says what it used. A counted group not narrowed yet
            # never handed its generation on, but may have sent its count.
            row.state = LiveRequestState.UNKNOWN
            row.charged_micro_usd = row.reserved_micro_usd
            row.reason = INTERRUPTED_COUNTING if row.reason == COUNTING else INTERRUPTED
            row.settled_at = now
            _pause(db, row.approval_id, INTERRUPTED, now)

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

    def __init__(
        self, database: Database, terms: ApprovalTerms, basis: "FixedReserve | InputBound", clock: Clock, lock, stop_file: Path
    ):
        self._database = database
        self.terms = terms
        # One of the two: a budget policy, or an input bound.
        self.policy = basis if isinstance(basis, FixedReserve) else None
        self.bound = basis if isinstance(basis, InputBound) else None
        self._clock = clock
        self._lock = lock
        self.stop_file = stop_file
        # Seconds, for the time a group may take; a test may give its own.
        self.monotonic = time.monotonic
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
    def task(
        self, purpose: str, owner_id: str, provider: AIProvider, *, sending: "Sending | None" = None
    ) -> Iterator["GatedProvider"]:
        """One real task: its provider behind the gate, letting requests through only inside this block.

        Another task meanwhile, or any once the gate is closed, is refused with
        nothing sent. Leaving the block, the provider lets nothing more
        through, and the next task may start once the requests it let through
        are settled. sending, if given, is where the task counts its requests
        (see Sending): a request the gate stopped never reaches it.
        """
        self._check_provider(provider)

        if purpose not in self.terms.purposes:
            raise LiveBlocked(Blocked.PURPOSE)

        with self._guard:
            if self._closed:
                raise LiveBlocked(Blocked.CLOSED)

            if self._current is not None:
                raise LiveBlocked(Blocked.BUSY)

            gated = GatedProvider(self, purpose, owner_id, provider, sending)
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

    @property
    def counted(self) -> bool:
        """Whether each request is a count and then a generation (a counted input bound)."""
        return self.bound is not None and self.bound.counted

    def _check_provider(self, provider: AIProvider) -> None:
        if self.terms.prices.synthetic or (self.policy or self.bound).synthetic:
            if isinstance(provider, OpenAIProvider) or not getattr(provider, "SYNTHETIC", False):
                raise LiveRefused("合成的价格、预算策略或输入上界只能配合合成服务商使用，不能放行真实服务。")

        identity = provider.identity()
        approved = (self.terms.provider, self.terms.model, self.terms.endpoint)

        if identity is None or (identity.provider, identity.requested_model, identity.endpoint) != approved:
            raise LiveRefused("这个服务商不是批准里写明的接收方。")

    def reserve_for(
        self, provider: AIProvider, prompt: str, output_format: JsonSchemaFormat | None, output_limit: int
    ) -> int:
        """What a request is reserved at before anything goes out, or LiveBlocked with nothing reserved or sent.

        Under a budget policy: its fixed input tokens and its output limit,
        once the body provider would send has been measured (PAYLOAD when it
        is too large or no plain text). A counted request is taken at the
        approval's largest input, and its count's price added; a bound worked
        out here gives its own tokens (INPUT when they are too many).
        """
        terms = self.terms

        if self.policy is not None:
            body = provider.request_body(
                prompt,
                max_output_tokens=output_limit,
                reasoning_effort=terms.reasoning_effort,
                json_schema_format=output_format,
            )

            if not set(body) <= PLAIN_TEXT_REQUEST or not isinstance(body.get("input"), str):
                raise LiveBlocked(Blocked.PAYLOAD)

            if request_bytes(body) > self.policy.max_payload_bytes:
                raise LiveBlocked(Blocked.PAYLOAD)

            return reservation(self.policy.reserve_input_tokens, output_limit, terms.prices)

        if self.bound.counted:
            return reservation(terms.max_input_tokens, output_limit, terms.prices, counted=True)

        tokens = self.bound.tokens(prompt, output_format)

        if tokens > terms.max_input_tokens:
            raise LiveBlocked(Blocked.INPUT)

        return reservation(tokens, output_limit, terms.prices)

    def _reserve(self, purpose: str, owner_id: str, micro_usd: int) -> str:
        """Reserve what one request is reserved at; its id, or LiveBlocked with nothing written."""
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
                    reason=COUNTING if self.counted else None,
                    reserved_at=self._clock.now(),
                    settled_at=None,
                )
            )

        return request_id

    def _narrow(self, request_id: str, micro_usd: int) -> None:
        """Narrow a counted group's reservation to what its count showed, then check it may still go on.

        In one write transaction: the reservation becomes the smaller of the
        two and the row says it was counted; a paused approval then is
        LiveBlocked(PAUSED), and so, after it, is a stop file (STOPPED). Either
        way the row stays counted and reserved, for the caller to settle.
        """
        with self._database.transaction() as db:
            row = db.get(LiveRequest, request_id)
            row.reserved_micro_usd = min(row.reserved_micro_usd, micro_usd)
            row.reason = COUNTED
            approval = db.get(LiveApproval, row.approval_id)
            paused = approval is None or approval.paused_at is not None

        if paused:
            raise LiveBlocked(Blocked.PAUSED)

        if self.stop_file.exists():
            raise LiveBlocked(Blocked.STOPPED)

    def settle(
        self,
        request_id: str,
        *,
        sent: SendState,
        usage: Usage | None,
        count: Delivery | None = None,
        input_count: int | None = None,
    ) -> str | None:
        """Settle a reservation once; one settled already is left as it is. Its state, or None if there is none.

        sent and usage are its generation's; count, for a counted group, how
        far its count got, and input_count what the count said. A group whose
        generation never went out is released only if its count never did
        either; otherwise it is charged its count, as unknown (and paused)
        when that count only may have gone out and has a price. A generation
        that went out is priced from its usage (account, with the policy's
        default for writes said nothing of) with its count's price added:
        within the group's reservation it is settled, above it, or with more
        input than counted, charged in full and paused. Usage that cannot be
        priced keeps a conservative amount, an estimate and no settlement: the
        reservation, or the reported counts at the reservation price if more;
        its row says which check it failed.
        """
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

            fee = 0 if count is None else count_price(self.terms.prices)

            if not went_out(sent, usage):
                if count is None or count is Delivery.NOT_SENT:
                    row.state = LiveRequestState.RELEASED
                    row.charged_micro_usd = 0
                    row.reason = None
                else:
                    sure = count is Delivery.SENT
                    # A count that only may have gone out and has a price: whether it was billed is not known.
                    row.state = LiveRequestState.SETTLED if sure or not fee else LiveRequestState.UNKNOWN
                    row.charged_micro_usd = fee
                    row.reason = COUNT_SENT if sure else COUNT_MAYBE

                    if row.state == LiveRequestState.UNKNOWN:
                        _pause(db, row.approval_id, COUNT_MAYBE, now)
            else:
                accounted = account(usage, self.terms, self.policy.default_cache_ttl if self.policy else None)
                cost = accounted.amount

                if usage is not None and (cost is None or accounted.basis):
                    logger.warning(
                        "a real request's usage %s: %s",
                        f"could not be priced ({accounted.problem})" if cost is None else f"was priced by {accounted.basis}",
                        _cache_facts(usage, self.terms),
                    )

                if cost is None:
                    row.state = LiveRequestState.UNKNOWN
                    row.charged_micro_usd = self._conservative(row.reserved_micro_usd, fee, usage)
                    row.reason = accounted.problem if delivery(sent, usage) is Delivery.SENT else NO_ANSWER
                    _pause(db, row.approval_id, UNKNOWN_USAGE, now)
                else:
                    total = fee + cost
                    above = input_count is not None and usage.input_tokens > input_count

                    if above or total > row.reserved_micro_usd:
                        # The reservation did not cover it, or the count was wrong: charge what is known, all of it, and stop.
                        row.state = LiveRequestState.OVER if total > row.reserved_micro_usd else LiveRequestState.SETTLED
                        row.charged_micro_usd = total
                        row.reason = ABOVE_COUNT if above else OVER_RESERVATION
                        _pause(db, row.approval_id, row.reason, now)
                    else:
                        row.state = LiveRequestState.SETTLED
                        row.charged_micro_usd = total
                        row.reason = accounted.basis

            row.settled_at = now

            return row.state

    def _conservative(self, reserved: int, fee: int, usage: Usage | None) -> int:
        """What a request whose usage cannot be priced is kept at: an estimate, never a settlement."""
        if usage is None or usage.malformed or not is_count(usage.input_tokens) or not is_count(usage.output_tokens):
            return reserved

        return max(reserved, fee + reservation(usage.input_tokens, usage.output_tokens, self.terms.prices))

    def status(self) -> GateStatus:
        terms = self.terms

        with self._database.transaction() as db:
            approval = db.get(LiveApproval, terms.approval_id)
            rows = _counted(db, terms.approval_id)

        settled = sum(row.charged_micro_usd for row in rows if row.state in (LiveRequestState.SETTLED, LiveRequestState.OVER))
        reserved = sum(row.reserved_micro_usd for row in rows if row.state == LiveRequestState.RESERVED)
        unknown = sum(row.charged_micro_usd for row in rows if row.state == LiveRequestState.UNKNOWN)
        committed = settled + reserved + unknown

        return GateStatus(
            paused_reason=None if approval is None else approval.paused_reason,
            requests_used=len(rows),
            requests_left=max(terms.max_requests - len(rows), 0),
            committed_micro_usd=committed,
            micro_usd_left=max(terms.max_cost_micro_usd - committed, 0),
            settled_micro_usd=settled,
            reserved_micro_usd=reserved,
            unknown_micro_usd=unknown,
        )


def _kept(count: object) -> int | None:
    return count if is_count(count) else None


def _counted(db, approval_id: str) -> list[LiveRequest]:
    """The requests that count against an approval: all but those released."""
    return list(
        db.scalars(
            select(LiveRequest).where(
                LiveRequest.approval_id == approval_id, LiveRequest.state != LiveRequestState.RELEASED
            )
        )
    )


def _committed(db, approval_id: str) -> tuple[int, int]:
    """Requests that count against an approval, and what they are charged (reservations at full)."""
    rows = _counted(db, approval_id)
    committed = sum(
        row.reserved_micro_usd if row.state == LiveRequestState.RESERVED else row.charged_micro_usd for row in rows
    )

    return len(rows), committed


class Sending(Protocol):
    """Where a task counts its requests, told by its provider (GatedProvider; service._Announcing for one of this process).

    hand_on(call) is told right before a request is handed to the provider,
    a count or a generation (app.library.sends.Call), after every other
    check: it has the last word, and by raising it keeps the request back,
    unsent (behind the gate, a group that sent nothing is released; one whose
    count went out keeps what that cost). came_back(call, delivered) is told
    once a request handed on has come back, answered or failed, with how
    sure it is that it reached the service (delivery): the rule the ledger is
    settled by. Every hand_on that did not raise is paired with one
    came_back, NOT_SENT for a request the gate kept back after all.
    """

    def hand_on(self, call: Call) -> None: ...

    def came_back(self, call: Call, delivered: Delivery) -> None: ...


@dataclass
class _Group:
    """One request of an approval on its way through the gate: its ledger row and how far its count got."""

    request_id: str
    counted: bool
    count: Delivery = Delivery.NOT_SENT
    # What the count said, once it said a usable number.
    input_count: int | None = None


class GatedProvider(AIProvider):
    """A provider behind the gate, for one task: each request reserved before it is handed on, settled after.

    It lets requests through only while its task runs and the gate is open,
    checked before reserving and again right before handing on, where the
    task's sending has the last word; behind a counted bound the same goes
    for the count, and for the generation once the count has narrowed the
    reservation (see the module docstring). attempts counts the requests
    asked for; blocked those whose generation the gate stopped, which never
    went out; handed every count and generation that went out or may have
    (ai_provider.went_out), which the ledger never releases. Every request
    asks for the approval's reasoning effort and at most its output limit
    for the task's purpose, whatever the caller asks; a count and its
    generation share the approval's timeout.
    """

    def __init__(
        self,
        gate: LiveGate,
        purpose: str,
        owner_id: str,
        provider: AIProvider,
        sending: Sending | None = None,
    ):
        self._gate = gate
        self._purpose = purpose
        self._owner_id = owner_id
        self._provider = provider
        self._sending = sending
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
        limit = gate.terms.output_limit(self._purpose)
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
        terms = gate.terms
        # The time the group may take, count and generation together.
        deadline = gate.monotonic() + terms.timeout_seconds

        try:
            group = _Group(
                gate._reserve(
                    self._purpose, self._owner_id, gate.reserve_for(self._provider, prompt, json_schema_format, limit)
                ),
                gate.counted,
            )
        except LiveBlocked:
            self.blocked += 1
            raise

        if group.counted:
            self._count(group, prompt, limit, json_schema_format, deadline)

        # What stops a generation out of time before it goes out: behind a count, the count, which had its
        # share of the time; with none, only the work here had it.
        late = Blocked.COUNT if group.counted else Blocked.TIME

        if deadline - gate.monotonic() <= 0:
            # The time the group had is used up: the generation is never handed on.
            self._stop(group, late)

        self._hand_on(group, Call.GENERATION)
        left = deadline - gate.monotonic()

        if left <= 0:
            # Out of time while it was being handed on: nothing went out, and the task is told so.
            self._came_back(Call.GENERATION, Delivery.NOT_SENT)
            self._stop(group, late)

        try:
            result = self._provider.generate_text(
                prompt,
                max_output_tokens=limit,
                reasoning_effort=terms.reasoning_effort,
                json_schema_format=json_schema_format,
                timeout_seconds=left,
            )
        except BaseException as error:
            # Whatever failed, the request may have gone out, unless its provider says it did not.
            self._settled(group, *how_far(error))
            raise

        self._settled(group, SendState.ANSWERED, result.usage, quiet=False)

        return result

    def _count(self, group: _Group, prompt: str, limit: int, json_schema_format, deadline: float) -> None:
        """Have the service count the group's input and narrow its reservation, or settle the group and stop it."""
        gate = self._gate
        terms = gate.terms
        self._hand_on(group, Call.COUNT)

        try:
            count = self._provider.count_input_tokens(
                prompt,
                reasoning_effort=terms.reasoning_effort,
                json_schema_format=json_schema_format,
                timeout_seconds=deadline - gate.monotonic(),
            )
        except BaseException as error:
            # Whatever failed, the count may have gone out, unless its provider says it did not.
            group.count = delivery(how_far(error)[0], None)
            self._came_back(Call.COUNT, group.count)

            if not isinstance(error, Exception):
                self._unsent(group)
                raise

            self._stop(group, Blocked.COUNT)

        group.count = Delivery.SENT
        self._came_back(Call.COUNT, Delivery.SENT)

        if not is_count(count) or count < 1:
            self._stop(group, Blocked.COUNT)

        if count > terms.max_input_tokens:
            self._stop(group, Blocked.INPUT)

        group.input_count = count

        try:
            gate._narrow(group.request_id, reservation(count, limit, terms.prices, counted=True))
        except LiveBlocked:
            self._unsent(group)
            self.blocked += 1
            raise

    def _hand_on(self, group: _Group, call: Call) -> None:
        """The last checks before a request is handed on; failing them, the group is settled and nothing more goes."""
        refusal = self._gate._still_open(self)

        if refusal is not None:
            # The gate closed or the task ended meanwhile: nothing more goes out.
            self._stop(group, refusal)

        if self._sending is not None:
            try:
                # The task may have stopped meanwhile: then nothing more is handed on.
                self._sending.hand_on(call)
            except BaseException:
                self._unsent(group)
                raise

    def _stop(self, group: _Group, why: Blocked):
        """Settle a group whose generation does not go out, and say why."""
        self._unsent(group)
        self.blocked += 1

        raise LiveBlocked(why) from None

    def _unsent(self, group: _Group) -> None:
        # Released if nothing went out; a count that went out, or may have, keeps its price.
        self._gate.settle(
            group.request_id,
            sent=SendState.NOT_SENT,
            usage=None,
            count=group.count if group.counted else None,
            input_count=group.input_count,
        )

    def _settled(self, group: _Group, sent: SendState, usage: Usage | None, *, quiet: bool = True) -> None:
        delivered = delivery(sent, usage)

        try:
            self._gate.settle(
                group.request_id,
                sent=sent,
                usage=usage,
                count=group.count if group.counted else None,
                input_count=group.input_count,
            )
        except Exception as error:
            if not quiet:
                raise

            # Left reserved, it keeps the gate busy until a restart charges it as unknown.
            logger.error("a real request could not be settled (%s)", type(error).__name__)
        finally:
            # Counted by the rule the ledger is settled by: one it releases is never counted as sent.
            self._came_back(Call.GENERATION, delivered)

    def _came_back(self, call: Call, delivered: Delivery) -> None:
        if delivered is not Delivery.NOT_SENT:
            self.handed += 1

        if self._sending is not None:
            self._sending.came_back(call, delivered)
