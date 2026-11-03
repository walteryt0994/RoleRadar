"""The gate in front of real AI requests and its ledger (U2-C1), with synthetic prices only.

Nothing here reaches a real service: the providers are synthetic, the one
real provider class is built around a patched SDK client and must be refused
before anything is asked of it, and the launcher blocks the network anyway.
"""
import subprocess
import sys
import threading
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from app import live_gate
from app.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    JsonSchemaFormat,
    AIProviderError,
    AIProviderRateLimitError,
    AIProviderTimeoutError,
    GenerationResult,
    OpenAIProvider,
    OpenAIProviderConfig,
    ProviderIdentity,
    SendState,
    Usage,
    request_bytes,
    responses_request,
)
from app.job_analysis import live
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.models import LiveApproval, LiveRequest, LiveRequestState, create_schema
from app.library.sends import Call, Delivery
from app.live_gate import (
    FIXED_RESERVE_V1,
    ApprovalTerms,
    Blocked,
    FixedReserve,
    InputBound,
    LiveBlocked,
    LiveRefused,
    PriceBasis,
    charge,
    open_gate,
    reservation,
)
from app.web_main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[2]
# Made-up prices in dollars per million tokens. Cache writes cost more than
# plain input here, so a worst case that took cached input to be cheaper
# would come out too low.
PRICES = PriceBasis(
    input=Decimal("2"),
    cache_read=Decimal("0.5"),
    cache_write=Decimal("2.5"),
    output=Decimal("8"),
    reserve_input=Decimal("2.5"),
    source="synthetic",
    checked_on="2026-11-02",
    synthetic=True,
)
BOUND = InputBound(
    name="synthetic-bytes",
    tokens=lambda prompt, output_format: len(prompt.encode("utf-8")) + 50,
    source="synthetic",
    checked_on="2026-11-02",
    synthetic=True,
)
PROMPT = "x" * 100
# 150 input tokens at the dearest input item (2.5) and 100 output tokens at 8.
WORST = 375 + 800
# 40 cached at 0.5, 10 written at 2.5, the other 70 at 2, and 30 output at 8.
USED = Usage(
    input_tokens=120,
    output_tokens=30,
    cache_read_tokens=40,
    cache_write_tokens=10,
    reasoning_tokens=12,
    total_tokens=150,
    service_tier="default",
    cache_write_30m_tokens=10,
)
COST = 20 + 25 + 140 + 240
APPROVAL = "synthetic-approval-1"
# What a synthetic count says by default: within USED's 120 input tokens, so a settled count never pauses.
COUNTED = 120


def terms(**changes) -> ApprovalTerms:
    values = {
        "approval_id": APPROVAL,
        "purposes": frozenset({"analysis", "structuring"}),
        "provider": "synthetic",
        "model": "synthetic-model",
        "endpoint": "https://synthetic.invalid/v1",
        "service_tier": "default",
        "reasoning_effort": "low",
        "max_output_tokens": 100,
        "structuring_max_output_tokens": 300,
        "timeout_seconds": 30.0,
        "structuring_max_bytes": 8_000,
        "max_requests": 3,
        "max_cost_micro_usd": 10_000,
        "prices": PRICES,
        "input_bound": BOUND.name,
        "rules_checked_on": "2026-11-02",
        "request_unit": "generation",
        # The code's ceiling: no synthetic prompt here comes near it.
        "max_input_tokens": 32_000,
    }
    values.update(changes)

    return ApprovalTerms(**values)


def synthetic_prices(plain, read, write, out, *, reserve=None, count=None) -> PriceBasis:
    """Made-up prices; reservations take the dearest of the input ones unless told otherwise."""
    reserve = max(price for price in (plain, read, write) if price is not None) if reserve is None else reserve

    return PriceBasis(plain, read, write, out, reserve, "synthetic", "2026-11-02", True, count)


def answer(usage: Usage) -> GenerationResult:
    return GenerationResult(
        text="{}",
        provider="synthetic",
        requested_model="synthetic-model",
        model="synthetic-model",
        latency_seconds=0.0,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        service_tier=usage.service_tier,
        usage_malformed=usage.malformed,
        cache_write_30m_tokens=usage.cache_write_30m_tokens,
        cache_write_12h_tokens=usage.cache_write_12h_tokens,
    )


class SyntheticProvider(AIProvider):
    """Made up for these tests: answers and counts from scripts and sends nothing anywhere.

    counts holds what each count does, in order: an exception to raise, a
    callable to run (its result is the count), or the count itself; by
    default COUNTED. timeouts keeps the time each request was given, counts
    and generations alike, in order.
    """

    SYNTHETIC = True

    def __init__(self, *script, counts=()):
        self.script = list(script)
        self.counts = list(counts)
        self.calls = []
        self.counted = []
        self.timeouts = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.calls.append({"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        self.timeouts.append(timeout_seconds)
        step = self.script.pop(0) if self.script else USED

        if isinstance(step, BaseException):
            raise step

        return answer(step)

    def count_input_tokens(self, prompt, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.counted.append({"prompt": prompt, "reasoning_effort": reasoning_effort, "format": json_schema_format})
        self.timeouts.append(timeout_seconds)
        step = self.counts.pop(0) if self.counts else COUNTED

        if isinstance(step, BaseException):
            raise step

        return step() if callable(step) else step

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        # Built as the real provider builds its own, for the same arguments.
        return responses_request(
            "synthetic-model",
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            service_tier="default",
            json_schema_format=json_schema_format,
        )


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "library.db"
    engine = make_engine(path)
    create_schema(engine)
    yield Database(engine), path
    engine.dispose()


@pytest.fixture
def gates(ledger, clock):
    database, path = ledger
    opened = []

    def open_one(approval: ApprovalTerms | None = None, bound: InputBound = BOUND):
        gate = open_gate(database, path, approval or terms(), bound, clock)
        opened.append(gate)

        return gate

    yield open_one

    for gate in opened:
        gate.close()


def requests_of(database) -> list[LiveRequest]:
    with database.transaction() as db:
        return list(db.scalars(select(LiveRequest).order_by(LiveRequest.reserved_at)))


def approval_of(database, approval_id: str = APPROVAL) -> LiveApproval | None:
    with database.transaction() as db:
        return db.get(LiveApproval, approval_id)


def ask(gate, provider, prompt=PROMPT, **options):
    with gate.task("analysis", "owner-a", provider) as gated:
        return gated.generate_text(prompt, **options)


# --- prices --------------------------------------------------------------------------------------------


def test_a_worst_case_takes_every_input_token_at_the_dearest_input_item():
    assert reservation(150, 100, PRICES) == WORST == 1175
    cheaper_writes = synthetic_prices(Decimal("2"), Decimal("0.5"), None, Decimal("8"))
    assert reservation(150, 100, cheaper_writes) == 150 * 2 + 100 * 8


def test_a_charge_bills_each_item_once_and_reasoning_as_part_of_the_output():
    assert charge(USED, terms()) == COST == 425
    # A cache item that is not billed on its own is billed as plain input.
    plain_writes = terms(prices=synthetic_prices(Decimal("2"), Decimal("0.5"), None, Decimal("8")))
    assert charge(USED, plain_writes) == 20 + 160 + 240


def test_amounts_are_rounded_up_to_the_millionth_of_a_dollar():
    tiny = synthetic_prices(Decimal("0.001"), None, None, Decimal("0.001"))
    assert reservation(1, 1, tiny) == 1
    assert charge(Usage(input_tokens=1, output_tokens=0, service_tier="default"), terms(prices=tiny)) == 1


@pytest.mark.parametrize(
    "usage",
    [
        None,
        Usage(output_tokens=30, service_tier="default"),
        Usage(input_tokens=120, output_tokens=30, cache_write_tokens=10, service_tier="default"),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, service_tier="default"),
        Usage(input_tokens=20, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, service_tier="default"),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, reasoning_tokens=31, service_tier="default"),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, service_tier="priority"),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, service_tier=None),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=100, cache_write_tokens=30, service_tier="default"),
        replace(USED, total_tokens=151),
        replace(USED, input_tokens=-120),
        replace(USED, input_tokens=True),
        replace(USED, output_tokens=30.0),
        replace(USED, cache_read_tokens="40"),
        replace(USED, reasoning_tokens=-1),
        replace(USED, total_tokens=False),
        replace(USED, malformed=True),
    ],
    ids=[
        "none", "no-input", "no-cache-read", "no-cache-write", "items-above-input", "reasoning-above-output",
        "other-tier", "no-tier", "cache-parts-above-input", "total-not-input-plus-output", "negative",
        "bool", "float", "text", "negative-reasoning", "bool-total", "malformed",
    ],
)
def test_a_charge_is_unknown_when_the_usage_cannot_tell(usage):
    assert charge(usage, terms()) is None


PLAIN = replace(PRICES, cache_read=None, cache_write=None)


@pytest.mark.parametrize(
    "usage",
    [
        Usage(input_tokens=100, output_tokens=0, cache_read_tokens=101, service_tier="default"),
        Usage(input_tokens=100, output_tokens=0, cache_write_tokens=101, service_tier="default"),
        Usage(input_tokens=100, output_tokens=0, cache_read_tokens=60, cache_write_tokens=41, service_tier="default"),
        Usage(input_tokens=100, output_tokens=0, cache_read_tokens=-1, service_tier="default"),
        Usage(input_tokens=100, output_tokens=0, cache_write_tokens=True, service_tier="default"),
        Usage(input_tokens=100, output_tokens=5, reasoning_tokens=6, service_tier="default"),
        Usage(input_tokens=100, output_tokens=5, total_tokens=104, service_tier="default"),
        Usage(input_tokens=-100, output_tokens=0, service_tier="default"),
    ],
    ids=["read", "write", "both", "negative-read", "bool-write", "reasoning", "total", "negative-input"],
)
def test_counts_must_agree_even_for_items_not_billed_on_their_own(usage):
    assert charge(usage, terms(prices=PLAIN)) is None


def test_counts_an_item_does_not_need_may_be_left_out():
    bare = Usage(input_tokens=120, output_tokens=30, service_tier="default")

    assert charge(bare, terms(prices=PLAIN)) == 120 * 2 + 30 * 8
    assert charge(replace(USED, total_tokens=None, reasoning_tokens=None), terms()) == COST


# --- one app at a time, and its approval's terms ------------------------------------------------------


def test_a_second_app_is_refused_and_touches_nothing_while_the_first_holds_the_ledger(gates, ledger):
    database, path = ledger
    first = gates()
    first._reserve("analysis", "owner-a", 100)

    with pytest.raises(LiveRefused, match="另一个应用"):
        gates()

    # The second did not take over what the first left reserved.
    assert [row.state for row in requests_of(database)] == [LiveRequestState.RESERVED]
    first.close()
    gates()
    assert [row.state for row in requests_of(database)] == [LiveRequestState.UNKNOWN]


def test_another_process_cannot_take_the_lock_while_an_app_holds_it(gates, ledger):
    _, path = ledger
    gates()
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from app.live_gate import ExclusiveLock, LiveRefused\n"
        "try:\n"
        "    ExclusiveLock(Path(sys.argv[1])).acquire()\n"
        "except LiveRefused:\n"
        "    print('refused')\n"
        "else:\n"
        "    print('taken')\n"
    )
    lock_file = path.with_name(path.name + live_gate.LOCK_SUFFIX)
    child = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(lock_file)], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=60
    )

    assert child.stdout.strip() == "refused", child.stderr


@pytest.mark.parametrize(
    "changes",
    [
        {"max_cost_micro_usd": 20_000},
        {"max_requests": 4},
        {"max_output_tokens": 200},
        {"purposes": frozenset({"analysis"})},
        {"service_tier": "priority"},
        {"reasoning_effort": "high"},
        {"structuring_max_bytes": 7_000},
        {"structuring_max_output_tokens": 200},
        {"prices": synthetic_prices(Decimal("1"), Decimal("0.5"), Decimal("2.5"), Decimal("8"))},
        {"prices": synthetic_prices(Decimal("2"), None, Decimal("2.5"), Decimal("8"))},
        {"prices": synthetic_prices(Decimal("2"), Decimal("0.5"), Decimal("2.5"), Decimal("8"), reserve=Decimal("3"))},
        {"max_input_tokens": 2000},
    ],
    ids=[
        "cost",
        "requests",
        "output",
        "purposes",
        "tier",
        "effort",
        "bytes",
        "structuring-output",
        "price",
        "cache-item",
        "reserve-price",
        "input-limit",
    ],
)
def test_changed_terms_under_the_same_approval_are_refused_and_the_snapshot_kept(gates, ledger, changes):
    database, _ = ledger
    gates().close()
    kept = approval_of(database).terms

    with pytest.raises(LiveRefused, match="条款和第一次使用时不同"):
        gates(terms(**changes))

    assert approval_of(database).terms == kept
    # The lock was given back: the approved terms open again.
    gates()


def test_the_same_prices_written_differently_are_the_same_terms(gates):
    gates().close()
    same = PriceBasis(Decimal("2.0"), Decimal("0.50"), Decimal("2.500"), Decimal("8.00"), Decimal("2.50"), "synthetic", "2026-11-02", True)

    gates(terms(prices=same))


def test_a_new_approval_starts_with_its_own_limits(gates, ledger):
    database, _ = ledger
    first = gates(terms(max_requests=1))
    ask(first, SyntheticProvider())
    first.close()
    second = gates(terms(approval_id="synthetic-approval-2", max_requests=1))
    ask(second, SyntheticProvider())

    assert [row.approval_id for row in requests_of(database)] == [APPROVAL, "synthetic-approval-2"]


# --- limits --------------------------------------------------------------------------------------------


def test_requests_stop_exactly_at_the_request_limit(gates, ledger, clock):
    database, _ = ledger
    gate = gates()
    provider = SyntheticProvider()

    with gate.task("analysis", "owner-a", provider) as gated:
        for _ in range(3):
            gated.generate_text(PROMPT)
            clock.advance(seconds=1)

        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    assert blocked.value.reason is Blocked.REQUESTS
    assert (gated.attempts, gated.blocked, gated.handed, len(provider.calls)) == (4, 1, 3, 3)
    assert [row.charged_micro_usd for row in requests_of(database)] == [COST] * 3
    assert gate.status().requests_left == 0


def test_a_request_whose_worst_case_would_pass_the_cost_limit_is_not_sent(gates, ledger):
    database, _ = ledger
    # Room for one settled request and one more worst case, exactly.
    gate = gates(terms(max_cost_micro_usd=COST + WORST))
    provider = SyntheticProvider()
    ask(gate, provider)
    ask(gate, provider)

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.COST and len(provider.calls) == 2
    assert len(requests_of(database)) == 2 and gate.status().committed_micro_usd == 2 * COST


def test_every_request_asks_for_the_approved_limit_and_effort_whatever_the_caller_asks(gates):
    gate = gates()
    provider = SyntheticProvider()
    ask(gate, provider, max_output_tokens=4000, reasoning_effort="high")
    ask(gate, provider, max_output_tokens=50)

    assert provider.calls == [
        {"max_output_tokens": 100, "reasoning_effort": "low"},
        {"max_output_tokens": 50, "reasoning_effort": "low"},
    ]


def test_a_structuring_request_has_its_own_output_limit_and_is_reserved_by_it(gates, ledger):
    database, _ = ledger
    gate = gates()
    provider = SyntheticProvider()

    with gate.task("structuring", "owner-a", provider) as gated:
        gated.generate_text(PROMPT, max_output_tokens=4000)

    ask(gate, provider, max_output_tokens=4000)

    assert [call["max_output_tokens"] for call in provider.calls] == [300, 100]
    # 150 input tokens at the dearest input item (2.5), and the purpose's own output limit at 8.
    assert sorted(row.reserved_micro_usd for row in requests_of(database)) == [375 + 100 * 8, 375 + 300 * 8]


def test_one_real_task_at_a_time_and_only_for_an_approved_purpose(gates):
    gate = gates(terms(purposes=frozenset({"analysis"})))

    with gate.task("analysis", "owner-a", SyntheticProvider()):
        with pytest.raises(LiveBlocked) as busy:
            with gate.task("analysis", "owner-b", SyntheticProvider()):
                pass

    assert busy.value.reason is Blocked.BUSY

    with pytest.raises(LiveBlocked) as purpose:
        with gate.task("structuring", "owner-a", SyntheticProvider()):
            pass

    assert purpose.value.reason is Blocked.PURPOSE
    # Free again once the first task ended.
    ask(gate, SyntheticProvider())


def test_a_request_left_unsettled_keeps_the_next_one_out(gates):
    gate = gates()
    gate._reserve("analysis", "owner-a", 100)

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, SyntheticProvider())

    assert blocked.value.reason is Blocked.BUSY


# --- settling ------------------------------------------------------------------------------------------


def test_a_cost_above_its_reservation_is_charged_in_full_and_pauses_the_approval(gates, ledger):
    database, _ = ledger
    gate = gates()
    # Far more input than the bound said, though within what the prices were checked for: the worst case was not one.
    provider = SyntheticProvider(Usage(input_tokens=900, output_tokens=30, cache_read_tokens=0, cache_write_tokens=0, service_tier="default"))
    ask(gate, provider)
    (row,) = requests_of(database)

    assert row.state == LiveRequestState.OVER and row.reason == "over_reservation"
    assert row.charged_micro_usd == 900 * 2 + 30 * 8 > row.reserved_micro_usd == WORST
    assert approval_of(database).paused_reason == "over_reservation"

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.PAUSED and len(provider.calls) == 1


@pytest.mark.parametrize(
    "step, reason",
    [
        # No answer came: it may have gone out.
        (AIProviderTimeoutError("The provider request timed out."), "no_answer"),
        (
            Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, service_tier="priority"),
            "unknown_usage",
        ),
        (Usage(service_tier="default"), "unknown_usage"),
        (RuntimeError("synthetic failure"), "no_answer"),
        (replace(USED, total_tokens=9999), "unknown_usage"),
        (replace(USED, input_tokens=-10_000), "unknown_usage"),
        (replace(USED, malformed=True), "unknown_usage"),
        # Written to the cache for 12 hours: no price is approved for that.
        (replace(USED, cache_write_30m_tokens=0, cache_write_12h_tokens=10), "unknown_usage"),
    ],
    ids=["timeout", "other-tier", "no-counts", "not-a-provider-error", "total", "negative", "malformed", "kept-12h"],
)
def test_a_request_without_usable_usage_is_charged_at_its_reservation_and_pauses(gates, ledger, step, reason):
    database, _ = ledger
    gate = gates()
    provider = SyntheticProvider(step)

    with gate.task("analysis", "owner-a", provider) as gated:
        try:
            gated.generate_text(PROMPT)
        except (AIProviderError, RuntimeError):
            pass

    (row,) = requests_of(database)

    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.UNKNOWN, reason, WORST)
    assert approval_of(database).paused_reason == "unknown_usage" and gated.handed == 1

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.PAUSED


def test_an_answered_error_with_usage_is_settled_from_its_usage(gates, ledger):
    database, _ = ledger
    gate = gates()

    with pytest.raises(AIProviderError):
        ask(gate, SyntheticProvider(AIProviderError("unfinished", sent=SendState.ANSWERED, usage=USED)))

    (row,) = requests_of(database)
    assert (row.state, row.charged_micro_usd) == (LiveRequestState.SETTLED, COST)
    assert approval_of(database).paused_at is None


def test_a_request_its_provider_says_never_left_is_released_and_not_counted(gates, ledger):
    database, _ = ledger
    gate = gates(terms(max_requests=1))

    with gate.task("analysis", "owner-a", SyntheticProvider(AIProviderError("refused", sent=SendState.NOT_SENT))) as gated:
        with pytest.raises(AIProviderError):
            gated.generate_text(PROMPT)

    assert gated.handed == 0
    assert [(row.state, row.charged_micro_usd) for row in requests_of(database)] == [(LiveRequestState.RELEASED, 0)]
    # It used none of the one request allowed.
    ask(gate, SyntheticProvider())


def test_settling_again_charges_nothing_more(gates, ledger):
    database, _ = ledger
    gate = gates()
    request_id = gate._reserve("analysis", "owner-a", WORST)

    assert gate.settle(request_id, sent=SendState.ANSWERED, usage=USED) == LiveRequestState.SETTLED
    # A second settlement, with other numbers or none, is ignored.
    assert gate.settle(request_id, sent=SendState.HANDED, usage=None) == LiveRequestState.SETTLED
    assert gate.settle(request_id, sent=SendState.ANSWERED, usage=Usage(input_tokens=9_999, output_tokens=9_999, service_tier="default")) == LiveRequestState.SETTLED
    (row,) = requests_of(database)
    assert row.charged_micro_usd == COST and approval_of(database).paused_at is None
    assert gate.status().committed_micro_usd == COST


# --- restarts and the stop file ------------------------------------------------------------------------


def test_a_restart_charges_what_was_left_reserved_and_never_lifts_the_pause(gates, ledger, clock):
    database, _ = ledger
    first = gates()
    ask(first, SyntheticProvider())
    clock.advance(seconds=1)
    # The app stops between reserving and settling.
    first._reserve("analysis", "owner-a", WORST)
    first.close()
    second = gates()
    left = requests_of(database)[-1]

    assert (left.state, left.reason, left.charged_micro_usd) == (LiveRequestState.UNKNOWN, "interrupted", WORST)
    assert second.status() == live_gate.GateStatus(
        paused_reason="interrupted",
        requests_used=2,
        requests_left=1,
        committed_micro_usd=COST + WORST,
        micro_usd_left=10_000 - COST - WORST,
        # What usage accounted for and what is still to be checked, never one figure called charged.
        settled_micro_usd=COST,
        reserved_micro_usd=0,
        unknown_micro_usd=WORST,
    )

    with pytest.raises(LiveBlocked) as blocked:
        ask(second, SyntheticProvider())

    assert blocked.value.reason is Blocked.PAUSED
    second.close()
    # Nothing is reset or lifted by starting again.
    third = gates()
    assert third.status() == second.status()


def test_what_was_spent_is_still_spent_after_a_restart(gates):
    first = gates()
    ask(first, SyntheticProvider())
    first.close()

    assert gates().status().committed_micro_usd == COST


def test_the_stop_file_blocks_every_request_until_it_is_removed(gates, ledger):
    database, path = ledger
    gate = gates()
    provider = SyntheticProvider()
    gate.stop_file.write_text("")

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert gate.stop_file == path.parent / "LIVE_STOP"
    assert blocked.value.reason is Blocked.STOPPED and provider.calls == [] and requests_of(database) == []
    # Stopping is not a pause: removed, the next request goes through.
    gate.stop_file.unlink()
    ask(gate, provider)
    assert gate.status().paused_reason is None and len(provider.calls) == 1


def test_the_ledger_keeps_counts_and_amounts_never_what_a_request_carried(gates, ledger):
    database, _ = ledger
    ask(gates(), SyntheticProvider(), prompt="SYNTHETIC-RESUME-LINE " * 4)

    with database.transaction() as db:
        kept = repr([db.execute(select(table)).all() for table in (LiveApproval.__table__, LiveRequest.__table__)])

    assert "SYNTHETIC-RESUME-LINE" not in kept


# --- what may never pass --------------------------------------------------------------------------------


def real_provider() -> OpenAIProvider:
    with patch("app.ai_provider.OpenAI"), patch("app.ai_provider.DefaultHttpxClient"):
        return OpenAIProvider(
            OpenAIProviderConfig(
                api_key="synthetic-not-a-key",
                model="synthetic-model",
                max_retries=0,
                base_url="https://synthetic.invalid/v1",
                use_environment_proxies=False,
            )
        )


class Unmarked(SyntheticProvider):
    SYNTHETIC = False


class MarkedReal(OpenAIProvider):
    SYNTHETIC = True


def test_synthetic_prices_or_bounds_never_let_a_real_provider_through(gates):
    gate = gates()
    provider = real_provider()
    marked = MarkedReal.__new__(MarkedReal)
    marked.__dict__.update(provider.__dict__)

    for refused in (provider, marked, Unmarked()):
        with pytest.raises(LiveRefused, match="合成"):
            with gate.task("analysis", "owner-a", refused):
                pass

    provider._client.responses.create.assert_not_called()
    assert gate.status().requests_used == 0


def test_real_prices_are_refused_without_an_input_bound_checked_for_the_model(gates, ledger):
    database, _ = ledger
    real_prices = PriceBasis(
        Decimal("2"), None, None, Decimal("8"), Decimal("2"), "https://synthetic.invalid/pricing", "2026-11-02", count=Decimal("0")
    )
    worked_out = InputBound("bytes-plus-margin", BOUND.tokens, "https://synthetic.invalid/docs", "2026-11-02")
    counted = InputBound("counted-unchecked", None, "https://synthetic.invalid/docs", "2026-11-02", counted=True)

    assert live_gate.CERTIFIED_INPUT_BOUNDS == {}

    # A bound worked out here never serves a real service, checked or not.
    with pytest.raises(LiveRefused, match="不能用在本地算出的输入上界"):
        gates(terms(prices=real_prices, input_bound=worked_out.name), worked_out)

    with pytest.raises(LiveRefused, match="还没有核验过的输入上界"):
        gates(terms(prices=real_prices, input_bound=counted.name, request_unit="count_then_generation"), counted)

    assert approval_of(database) is None


def test_a_provider_other_than_the_approved_recipient_is_refused(gates):
    class Elsewhere(SyntheticProvider):
        def identity(self):
            return ProviderIdentity("synthetic", "another-model", "https://synthetic.invalid/v1")

    with pytest.raises(LiveRefused, match="接收方"):
        with gates().task("analysis", "owner-a", Elsewhere()):
            pass


# --- approvals read from the environment ----------------------------------------------------------------

POLICY = {
    "ROLERADAR_ANALYSIS_MODE": "live",
    "ROLERADAR_LIVE_PROVIDER": "openai",
    "ROLERADAR_LIVE_MODEL": "synthetic-model",
    "ROLERADAR_LIVE_ENDPOINT": "https://api.openai.com/v1",
    "ROLERADAR_LIVE_MAX_REQUESTS": "6",
    "ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "1500",
    "ROLERADAR_LIVE_TIMEOUT_SECONDS": "30",
    "ROLERADAR_LIVE_RULES_CHECKED_ON": "2026-11-02",
    "OPENAI_API_KEY": "synthetic-not-a-key",
}
APPROVED = {
    **POLICY,
    "ROLERADAR_LIVE_APPROVAL_ID": "synthetic-approval-1",
    "ROLERADAR_LIVE_PURPOSES": "analysis, structuring",
    "ROLERADAR_LIVE_SERVICE_TIER": "default",
    "ROLERADAR_LIVE_REASONING_EFFORT": "omit",
    "ROLERADAR_LIVE_STRUCTURING_MAX_BYTES": "8000",
    "ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS": "8000",
    "ROLERADAR_LIVE_MAX_COST_USD": "0.50",
    "ROLERADAR_LIVE_PRICE_INPUT": "2",
    "ROLERADAR_LIVE_PRICE_CACHE_READ": "0.5",
    "ROLERADAR_LIVE_PRICE_CACHE_WRITE": "none",
    "ROLERADAR_LIVE_PRICE_OUTPUT": "8",
    "ROLERADAR_LIVE_PRICE_SOURCE": "https://synthetic.invalid/pricing",
    "ROLERADAR_LIVE_PRICE_CHECKED_ON": "2026-11-02",
    "ROLERADAR_LIVE_PRICE_RESERVE_INPUT": "2",
    "ROLERADAR_LIVE_MAX_INPUT_TOKENS": "16000",
    "ROLERADAR_LIVE_BUDGET_POLICY": "fixed_reserve_v1",
    "ROLERADAR_LIVE_BUDGET_SCOPE": "application",
    "ROLERADAR_LIVE_RESERVE_INPUT_TOKENS": "16000",
    "ROLERADAR_LIVE_MAX_PAYLOAD_BYTES": "32000",
}
# A counted bound, as only a counted one may serve a real service; registered only by tests.
CHECKED = InputBound("checked-for-test", None, "https://synthetic.invalid/docs", "2026-11-02", counted=True)


def test_complete_terms_are_refused_for_a_model_and_tier_with_no_budget_policy():
    policy = live.load_live_policy(APPROVED)

    with pytest.raises(LiveRefused, match="还没有为模型 synthetic-model（服务档 default）定义预算策略"):
        live.load_live_approval(policy, APPROVED)


@pytest.mark.parametrize(
    "change, says",
    [
        ({"ROLERADAR_LIVE_APPROVAL_ID": None}, "ROLERADAR_LIVE_APPROVAL_ID"),
        ({"ROLERADAR_LIVE_PRICE_CACHE_WRITE": None}, "ROLERADAR_LIVE_PRICE_CACHE_WRITE"),
        ({"ROLERADAR_LIVE_PRICE_CACHE_READ": "free"}, "不单独计费时写 none"),
        ({"ROLERADAR_LIVE_PRICE_OUTPUT": "none"}, "输出单价"),
        ({"ROLERADAR_LIVE_PRICE_INPUT": "-1"}, "普通输入单价"),
        ({"ROLERADAR_LIVE_MAX_COST_USD": "5.01"}, "费用上限"),
        ({"ROLERADAR_LIVE_MAX_COST_USD": "0"}, "费用上限"),
        ({"ROLERADAR_LIVE_PURPOSES": "analysis,analysis"}, "用途"),
        ({"ROLERADAR_LIVE_PURPOSES": "analysis,chat"}, "用途"),
        ({"ROLERADAR_LIVE_SERVICE_TIER": "Default"}, "服务档"),
        ({"ROLERADAR_LIVE_REASONING_EFFORT": "very high"}, "推理强度"),
        ({"ROLERADAR_LIVE_PRICE_SOURCE": "http://synthetic.invalid/pricing"}, "价格来源"),
        ({"ROLERADAR_LIVE_PRICE_CHECKED_ON": "today"}, "核对日期"),
        ({"ROLERADAR_LIVE_STRUCTURING_MAX_BYTES": "8001"}, "字节上限"),
        ({"ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS": "8001"}, "简历整理输出上限"),
        ({"ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS": None}, "ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS"),
        # No count goes out: a count's price, written anyway, is never taken for one, 0 least of all.
        ({"ROLERADAR_LIVE_PRICE_COUNT": "0"}, "这条路径不计数"),
        ({"ROLERADAR_LIVE_PRICE_COUNT": "none"}, "这条路径不计数"),
        ({"ROLERADAR_LIVE_PRICE_RESERVE_INPUT": None}, "ROLERADAR_LIVE_PRICE_RESERVE_INPUT"),
        ({"ROLERADAR_LIVE_PRICE_RESERVE_INPUT": "1.5"}, "预留输入单价不能低于"),
        ({"ROLERADAR_LIVE_MAX_INPUT_TOKENS": None}, "ROLERADAR_LIVE_MAX_INPUT_TOKENS"),
        ({"ROLERADAR_LIVE_MAX_INPUT_TOKENS": "32001"}, "价格核实的输入范围"),
        ({"ROLERADAR_LIVE_BUDGET_POLICY": None}, "ROLERADAR_LIVE_BUDGET_POLICY"),
        ({"ROLERADAR_LIVE_BUDGET_SCOPE": None}, "ROLERADAR_LIVE_BUDGET_SCOPE"),
        ({"ROLERADAR_LIVE_RESERVE_INPUT_TOKENS": None}, "ROLERADAR_LIVE_RESERVE_INPUT_TOKENS"),
        ({"ROLERADAR_LIVE_MAX_PAYLOAD_BYTES": None}, "ROLERADAR_LIVE_MAX_PAYLOAD_BYTES"),
    ],
)
def test_terms_that_are_missing_or_doubtful_are_refused(change, says):
    environ = {name: value for name, value in {**APPROVED, **change}.items() if value is not None}

    with pytest.raises(LiveRefused, match=says):
        live.load_live_approval(live.load_live_policy(environ), environ)


def test_a_synthetic_policy_or_a_certified_bound_never_stands_in_for_the_budget_policy(monkeypatch):
    key = ("openai", "synthetic-model", "default")
    monkeypatch.setitem(live_gate.RESERVE_POLICIES, key, replace(live_gate.FIXED_RESERVE_V1, synthetic=True))

    with pytest.raises(LiveRefused, match="定义预算策略"):
        live.load_live_approval(live.load_live_policy(APPROVED), APPROVED)

    # A bound is no budget policy, however it is registered.
    monkeypatch.delitem(live_gate.RESERVE_POLICIES, key)
    monkeypatch.setitem(live_gate.CERTIFIED_INPUT_BOUNDS, key, CHECKED)

    with pytest.raises(LiveRefused, match="定义预算策略"):
        live.load_live_approval(live.load_live_policy(APPROVED), APPROVED)


@pytest.mark.parametrize(
    "change, says",
    [
        ({"ROLERADAR_LIVE_BUDGET_POLICY": "fixed_reserve_v2"}, "预算策略要写 fixed_reserve_v1"),
        ({"ROLERADAR_LIVE_BUDGET_SCOPE": "billing"}, "预算性质要写 application"),
        ({"ROLERADAR_LIVE_RESERVE_INPUT_TOKENS": "8000"}, "条款要照写"),
        ({"ROLERADAR_LIVE_MAX_PAYLOAD_BYTES": "64000"}, "条款要照写"),
    ],
    ids=["other-policy", "billing-scope", "other-reserve", "other-payload"],
)
def test_the_budget_policy_is_written_out_exactly_as_it_is_defined(monkeypatch, change, says):
    monkeypatch.setitem(live_gate.RESERVE_POLICIES, ("openai", "synthetic-model", "default"), live_gate.FIXED_RESERVE_V1)
    environ = {**APPROVED, **change}

    with pytest.raises(LiveRefused, match=says):
        live.load_live_approval(live.load_live_policy(environ), environ)


@pytest.mark.parametrize("count", [None, "not_applicable"], ids=["left-out", "not-applicable"])
def test_terms_read_from_the_environment_are_exact_and_never_synthetic(monkeypatch, count):
    monkeypatch.setitem(live_gate.RESERVE_POLICIES, ("openai", "synthetic-model", "default"), live_gate.FIXED_RESERVE_V1)
    environ = {**APPROVED, **({"ROLERADAR_LIVE_PRICE_COUNT": count} if count else {})}
    approval = live.load_live_approval(live.load_live_policy(environ), environ)

    assert approval.purposes == {"analysis", "structuring"} and approval.reasoning_effort is None
    assert (approval.max_cost_micro_usd, approval.max_requests, approval.max_output_tokens) == (500_000, 6, 1500)
    assert (approval.structuring_max_bytes, approval.structuring_max_output_tokens) == (8000, 8000)
    assert approval.prices == PriceBasis(
        Decimal("2"),
        Decimal("0.5"),
        None,
        Decimal("8"),
        Decimal("2"),
        "https://synthetic.invalid/pricing",
        "2026-11-02",
    )
    # Not applicable, never 0: no count goes out.
    assert approval.prices.synthetic is False and approval.prices.count is None and approval.input_bound is None
    # One request is one generation, under the policy written out, and the snapshot says so.
    snapshot = approval.snapshot()
    assert (snapshot["request_unit"], snapshot["max_input_tokens"]) == ("generation", 16000)
    assert (snapshot["budget_policy"], snapshot["budget_scope"]) == ("fixed_reserve_v1", "application")
    assert (snapshot["reserve_input_tokens"], snapshot["max_payload_bytes"]) == (16_000, 32_000)


def test_an_approved_service_tier_is_asked_for_by_the_real_provider():
    with patch("app.ai_provider.OpenAI"), patch("app.ai_provider.DefaultHttpxClient"):
        provider = live.make_live_provider(live.load_live_policy(POLICY), POLICY, service_tier="flex")

    request = {}

    def remember(**sent):
        request.update(sent)
        raise AIProviderTimeoutError("synthetic")

    provider._client.responses.create.side_effect = remember

    with pytest.raises(AIProviderTimeoutError):
        provider.generate_text(PROMPT)

    assert request["service_tier"] == "flex" and request["store"] is False


@pytest.mark.parametrize("purposes, cap", [("analysis, structuring", 8000), ("analysis", 1500)])
def test_the_real_provider_is_capped_at_the_largest_output_limit_its_approval_covers(monkeypatch, purposes, cap):
    monkeypatch.setitem(live_gate.RESERVE_POLICIES, ("openai", "synthetic-model", "default"), live_gate.FIXED_RESERVE_V1)
    environ = {**APPROVED, "ROLERADAR_ANALYSIS_MODE": "live", "ROLERADAR_LIVE_PURPOSES": purposes}

    with patch("app.ai_provider.OpenAI"), patch("app.ai_provider.DefaultHttpxClient"):
        provider = live.analysis_setup(environ).make_provider()

    # Each request is capped again by its purpose behind the gate.
    assert provider._config.max_output_tokens == cap


# --- the app ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("environ", [APPROVED, POLICY, {"ROLERADAR_ANALYSIS_MODE": "live"}, {"ROLERADAR_ANALYSIS_MODE": "chat"}])
def test_the_app_refuses_to_start_when_a_real_service_is_asked_for(make_config, tmp_path, clock, environ):
    config = make_config()

    with pytest.raises(ConfigError) as refused:
        create_app(config, clock=clock, environ=environ)

    assert "synthetic-not-a-key" not in str(refused.value)
    # Refused before any file was made.
    assert not config.db_path.exists()


@pytest.mark.parametrize("environ", [{}, {"ROLERADAR_ANALYSIS_MODE": "simulation"}])
def test_the_app_starts_with_the_simulated_service(make_config, clock, environ):
    app = create_app(make_config(), clock=clock, environ=environ)
    app.state.library.database.engine.dispose()


def test_the_ledger_keeps_no_malformed_count(gates, ledger):
    database, _ = ledger
    ask(gates(), SyntheticProvider(Usage(input_tokens=True, output_tokens=-3, cache_read_tokens=40, service_tier="default")))
    (row,) = requests_of(database)

    assert row.state == LiveRequestState.UNKNOWN and row.charged_micro_usd == WORST
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (None, None, 40)


# --- closing the gate and ending a task (U2-C1 R1) --------------------------------------------------------


class Waiting(SyntheticProvider):
    """Holds each request until released, to look at the gate while one is out."""

    def __init__(self, *script):
        super().__init__(*script)
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.entered.set()
        assert self.release.wait(10)

        return super().generate_text(prompt, max_output_tokens, reasoning_effort, json_schema_format, timeout_seconds)


def wait_until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout

    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_closed_gate_lets_nothing_more_through_but_still_tells_its_status(gates, ledger):
    database, _ = ledger
    first = gates()
    ask(first, SyntheticProvider())
    first.close()
    second = gates()
    stale = SyntheticProvider()

    with second.task("analysis", "owner-b", SyntheticProvider()):
        with pytest.raises(LiveBlocked) as closed:
            with first.task("analysis", "owner-a", stale):
                pass

    with pytest.raises(LiveBlocked) as reserving:
        first._reserve("analysis", "owner-a", 100)

    assert closed.value.reason is reserving.value.reason is Blocked.CLOSED
    assert stale.calls == [] and len(requests_of(database)) == 1
    assert first.status().committed_micro_usd == COST
    # Closing again changes nothing; the other app goes on.
    first.close()
    ask(second, SyntheticProvider())


def test_a_provider_lets_nothing_through_once_its_task_has_ended(gates, ledger):
    database, _ = ledger
    gate = gates()
    stale_provider = SyntheticProvider()

    with gate.task("analysis", "owner-a", stale_provider) as stale:
        pass

    with gate.task("analysis", "owner-b", SyntheticProvider()):
        with pytest.raises(LiveBlocked) as during:
            stale.generate_text(PROMPT)

    with pytest.raises(LiveBlocked) as after:
        stale.generate_text(PROMPT)

    assert during.value.reason is after.value.reason is Blocked.ENDED
    assert stale_provider.calls == [] and requests_of(database) == []
    assert (stale.attempts, stale.blocked, stale.handed) == (2, 2, 0)


def test_closing_waits_for_a_request_already_out_and_settles_it(gates, ledger, clock):
    database, path = ledger
    gate = gates()
    provider = Waiting()
    outcomes = []

    def run_task():
        with gate.task("analysis", "owner-a", provider) as gated:
            outcomes.append(gated.generate_text(PROMPT).text)

            try:
                gated.generate_text(PROMPT)
            except LiveBlocked as blocked:
                outcomes.append(blocked.reason)

    worker = threading.Thread(target=run_task)
    worker.start()
    assert provider.entered.wait(10)
    closer = threading.Thread(target=gate.close)
    closer.start()
    wait_until(lambda: gate._closed)

    # Still out: the lock is kept, so no other app takes the ledger over meanwhile.
    with pytest.raises(LiveRefused, match="另一个应用"):
        open_gate(database, path, terms(), BOUND, clock)

    assert closer.is_alive()
    provider.release.set()
    worker.join(10)
    closer.join(10)

    assert not worker.is_alive() and not closer.is_alive()
    # The one already out was settled from its usage; nothing more went out.
    assert outcomes == ["{}", Blocked.CLOSED] and len(provider.calls) == 1
    assert [(row.state, row.charged_micro_usd) for row in requests_of(database)] == [(LiveRequestState.SETTLED, COST)]
    gates()


def test_leaving_a_task_waits_for_its_request_out_on_another_thread(gates, ledger):
    database, _ = ledger
    gate = gates()
    provider = Waiting()
    left = threading.Event()

    def run_task():
        with gate.task("analysis", "owner-a", provider) as gated:
            sender = threading.Thread(target=gated.generate_text, args=(PROMPT,))
            sender.start()
            assert provider.entered.wait(10)

        left.set()
        sender.join(10)

    worker = threading.Thread(target=run_task)
    worker.start()
    assert provider.entered.wait(10)
    time.sleep(0.2)
    assert not left.is_set()

    with pytest.raises(LiveBlocked) as busy:
        with gate.task("analysis", "owner-b", SyntheticProvider()):
            pass

    provider.release.set()
    worker.join(10)

    assert busy.value.reason is Blocked.BUSY and left.is_set()
    assert [row.state for row in requests_of(database)] == [LiveRequestState.SETTLED]
    ask(gate, SyntheticProvider())


def test_a_request_reserved_as_the_gate_closes_is_released_and_never_sent(gates, ledger, monkeypatch):
    database, _ = ledger
    gate = gates()
    provider = SyntheticProvider()
    reserve = gate._reserve
    closers = []

    def reserve_while_closing(*args):
        request_id = reserve(*args)
        closers.append(threading.Thread(target=gate.close))
        closers[0].start()
        wait_until(lambda: gate._closed)

        return request_id

    monkeypatch.setattr(gate, "_reserve", reserve_while_closing)

    with gate.task("analysis", "owner-a", provider) as gated:
        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    closers[0].join(10)

    assert blocked.value.reason is Blocked.CLOSED and provider.calls == []
    assert (gated.attempts, gated.blocked, gated.handed) == (1, 1, 0)
    assert [(row.state, row.charged_micro_usd) for row in requests_of(database)] == [(LiveRequestState.RELEASED, 0)]
    assert not closers[0].is_alive()



# --- prices and usage, U2-C4 prerequisites ------------------------------------------------------------


def test_reservations_take_the_reservation_price_and_settlements_the_price_of_each_item():
    prices = synthetic_prices(Decimal("4"), Decimal("0.4"), Decimal("5"), Decimal("20"), reserve=Decimal("10"), count=Decimal("0.001"))
    approval = terms(prices=prices, max_input_tokens=2000)
    used = Usage(
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=600,
        cache_write_30m_tokens=600,
        cache_write_12h_tokens=0,
        service_tier="default",
    )

    assert reservation(1000, 100, prices) == 1000 * 10 + 100 * 20
    # A counted one adds its count: a thousandth of a dollar.
    assert reservation(1000, 100, prices, counted=True) == 1000 * 10 + 100 * 20 + 1000
    # 600 written at 5, the other 400 at 4 and 100 output at 20: never the reservation price.
    assert charge(used, approval) == 600 * 5 + 400 * 4 + 100 * 20


def test_a_reservation_price_below_a_price_an_input_token_may_be_billed_at_is_refused():
    with pytest.raises(ValueError, match="reservation"):
        synthetic_prices(Decimal("4"), Decimal("0.4"), Decimal("5"), Decimal("20"), reserve=Decimal("4.5"))


@pytest.mark.parametrize(
    "usage",
    [
        replace(USED, cache_write_30m_tokens=0, cache_write_12h_tokens=10),
        replace(USED, cache_write_30m_tokens=None),
        replace(USED, cache_write_30m_tokens=4),
        replace(USED, cache_write_30m_tokens=True),
        replace(USED, input_tokens=True),
        replace(USED, output_tokens=-1),
        replace(USED, input_tokens=1001, total_tokens=1031),
    ],
    ids=["kept-12h", "writes-unsplit", "writes-disagree", "split-not-a-count", "bool", "negative", "beyond-checked-prices"],
)
def test_usage_is_checked_whole_before_it_is_priced(usage):
    assert charge(usage, terms(max_input_tokens=1000)) is None


def test_usage_beyond_the_checked_prices_is_kept_at_a_conservative_amount_and_pauses(gates, ledger):
    database, _ = ledger
    gate = gates(terms(max_input_tokens=1000))
    ask(gate, SyntheticProvider(Usage(input_tokens=5000, output_tokens=30, cache_read_tokens=0, cache_write_tokens=0, service_tier="default")))
    (row,) = requests_of(database)

    # An estimate, never a settlement: the reported counts at the reservation price, as that is more than the reservation.
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.UNKNOWN, "unknown_usage", 5000 * 25 // 10 + 30 * 8)
    assert approval_of(database).paused_reason == "unknown_usage"


def test_a_request_whose_bound_is_over_the_largest_input_is_not_reserved_or_sent(gates, ledger):
    database, _ = ledger
    # The synthetic bound gives 150 for PROMPT.
    gate = gates(terms(max_input_tokens=100))
    provider = SyntheticProvider()

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.INPUT and provider.calls == [] and requests_of(database) == []


# --- counted requests: a count, then the generation (U2-C4 prerequisites) -------------------------------


COUNTED_BOUND = InputBound("synthetic-counted", None, "synthetic", "2026-11-03", synthetic=True, counted=True)
# Made up: a fifth of a thousandth of a dollar per count, so that a count shows in every amount.
COUNT_PRICE = Decimal("0.0002")
FEE = 200
# Before anything goes out: the approval's largest input (1000) at the reservation price, the output limit, and the count.
BEFORE_COUNT = FEE + 1000 * 25 // 10 + 100 * 8
# Narrowed by a count of COUNTED (120).
AFTER_COUNT = FEE + COUNTED * 25 // 10 + 100 * 8


def counted_terms(**changes) -> ApprovalTerms:
    values = {
        "prices": replace(PRICES, count=COUNT_PRICE),
        "input_bound": COUNTED_BOUND.name,
        "request_unit": "count_then_generation",
        "max_input_tokens": 1000,
    }
    values.update(changes)

    return terms(**values)


class Recording:
    """A task's sending (live_gate.Sending) that keeps what it is told, and may stop one kind of request."""

    def __init__(self, refuse: Call | None = None):
        self.told = []
        self.refuse = refuse

    def hand_on(self, call):
        self.told.append(("hand_on", call))

        if call == self.refuse:
            raise RuntimeError("the task stopped")

    def came_back(self, call, delivered):
        self.told.append(("came_back", call, delivered))


def test_a_counted_request_is_reserved_at_its_worst_then_narrowed_by_its_count_and_settled_with_it(gates, ledger):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    seen = []

    def count():
        (row,) = requests_of(database)
        seen.append((row.state, row.reason, row.reserved_micro_usd))

        return COUNTED

    provider = SyntheticProvider(counts=[count])
    sending = Recording()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    # Reserved at the worst case before the count went out; narrowed by it before the generation.
    assert seen == [(LiveRequestState.RESERVED, "counting", BEFORE_COUNT)]
    assert (row.state, row.reason, row.reserved_micro_usd, row.charged_micro_usd) == (
        LiveRequestState.SETTLED,
        None,
        AFTER_COUNT,
        FEE + COST,
    )
    # The count is of the very request the generation then sends, with the approved effort.
    assert provider.counted == [{"prompt": PROMPT, "reasoning_effort": "low", "format": None}] and len(provider.calls) == 1
    assert gated.handed == 2 and gate.status().requests_used == 1
    assert sending.told == [
        ("hand_on", Call.COUNT),
        ("came_back", Call.COUNT, Delivery.SENT),
        ("hand_on", Call.GENERATION),
        ("came_back", Call.GENERATION, Delivery.SENT),
    ]


def test_a_count_and_its_generation_share_the_approval_timeout(gates):
    gate = gates(counted_terms(), COUNTED_BOUND)
    now = [100.0]
    gate.monotonic = lambda: now[0]

    def slow():
        now[0] += 12.0

        return COUNTED

    provider = SyntheticProvider(counts=[slow])
    ask(gate, provider)

    # Together never more than the approval's 30 seconds: the generation gets what the count left.
    assert provider.timeouts == [30.0, 18.0]


@pytest.mark.parametrize("took", [30.0, 31.0], ids=["all-of-it", "more-than-all"])
def test_a_count_that_used_up_the_time_sends_no_generation_and_never_hands_it_on(gates, ledger, took):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    now = [100.0]
    gate.monotonic = lambda: now[0]

    def slowest():
        now[0] += took

        return COUNTED

    provider = SyntheticProvider(counts=[slowest])
    sending = Recording()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    assert blocked.value.reason is Blocked.COUNT and provider.calls == []
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, "count_sent", FEE)
    # The task never hears of a generation: nothing of it is out (C4 prerequisites acceptance F1).
    assert sending.told == [("hand_on", Call.COUNT), ("came_back", Call.COUNT, Delivery.SENT)]
    assert gated.handed == 1


def test_a_generation_out_of_time_once_handed_on_is_told_back_as_not_sent(gates, ledger):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    now = [100.0]
    gate.monotonic = lambda: now[0]

    class Slow(Recording):
        # The time runs out while the task takes the generation in.
        def hand_on(self, call):
            super().hand_on(call)

            if call == Call.GENERATION:
                now[0] += 30.0

    provider = SyntheticProvider()
    sending = Slow()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    assert blocked.value.reason is Blocked.COUNT and provider.calls == []
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, "count_sent", FEE)
    # Every hand_on is paired with a came_back: this one as not sent.
    assert sending.told[2:] == [("hand_on", Call.GENERATION), ("came_back", Call.GENERATION, Delivery.NOT_SENT)]
    assert gated.handed == 1


@pytest.mark.parametrize(
    "error, price, delivered, state, reason, charged, paused",
    [
        # No answer: the count may have gone out, and whether it was billed is not known.
        (AIProviderTimeoutError("The provider request timed out."), COUNT_PRICE, Delivery.MAYBE, "unknown", "count_maybe", FEE, "count_maybe"),
        # Free counts cost nothing whether or not it went out.
        (AIProviderTimeoutError("The provider request timed out."), Decimal("0"), Delivery.MAYBE, "settled", "count_maybe", 0, None),
        (
            AIProviderRateLimitError("The provider rate limit or quota was exceeded.", sent=SendState.ANSWERED),
            COUNT_PRICE,
            Delivery.SENT,
            "settled",
            "count_sent",
            FEE,
            None,
        ),
        # Stopped before it was handed to the SDK: nothing went out, and the group is released.
        (AIProviderConfigError("The provider cannot count input tokens."), COUNT_PRICE, Delivery.NOT_SENT, "released", None, 0, None),
    ],
    ids=["timeout", "timeout-free", "answered-error", "never-sent"],
)
def test_a_count_that_failed_sends_no_generation_and_keeps_what_it_may_have_cost(
    gates, ledger, error, price, delivered, state, reason, charged, paused
):
    database, _ = ledger
    gate = gates(counted_terms(prices=replace(PRICES, count=price)), COUNTED_BOUND)
    provider = SyntheticProvider(counts=[error])
    sending = Recording()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    # Neither retried nor followed by a generation.
    assert blocked.value.reason is Blocked.COUNT and len(provider.counted) == 1 and provider.calls == []
    assert (row.state, row.reason, row.charged_micro_usd) == (state, reason, charged)
    assert approval_of(database).paused_reason == paused
    assert sending.told == [("hand_on", Call.COUNT), ("came_back", Call.COUNT, delivered)]
    assert gate.status().requests_used == (0 if delivered is Delivery.NOT_SENT else 1)


@pytest.mark.parametrize(
    "count, why",
    [(True, Blocked.COUNT), (-1, Blocked.COUNT), (0, Blocked.COUNT), (1.5, Blocked.COUNT), ("7", Blocked.COUNT), (1001, Blocked.INPUT)],
    ids=["bool", "negative", "zero", "fraction", "text", "over-the-largest-input"],
)
def test_a_count_that_is_no_whole_number_or_over_the_largest_input_sends_no_generation(gates, ledger, count, why):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    provider = SyntheticProvider(counts=[count])

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    (row,) = requests_of(database)

    assert blocked.value.reason is why and provider.calls == []
    # The count went out: it is charged, never released.
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, "count_sent", FEE)
    assert approval_of(database).paused_reason is None


@pytest.mark.parametrize("stop", ["stop-file", "paused", "task"])
def test_a_stop_after_the_count_keeps_the_count_and_sends_no_generation(gates, ledger, clock, stop):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)

    def count():
        if stop == "stop-file":
            gate.stop_file.touch()
        elif stop == "paused":
            with database.transaction() as db:
                approval = db.get(LiveApproval, APPROVAL)
                approval.paused_at, approval.paused_reason = clock.now(), "synthetic"

        return COUNTED

    provider = SyntheticProvider(counts=[count])
    sending = Recording(refuse=Call.GENERATION if stop == "task" else None)

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises((LiveBlocked, RuntimeError)) as raised:
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    assert provider.calls == []
    # Narrowed, then stopped: only the count is charged.
    assert (row.state, row.reason, row.reserved_micro_usd, row.charged_micro_usd) == (
        LiveRequestState.SETTLED,
        "count_sent",
        AFTER_COUNT,
        FEE,
    )
    assert sending.told[:2] == [("hand_on", Call.COUNT), ("came_back", Call.COUNT, Delivery.SENT)]

    if stop != "task":
        assert raised.value.reason is {"stop-file": Blocked.STOPPED, "paused": Blocked.PAUSED}[stop]


def test_a_generation_its_provider_never_sent_after_a_count_keeps_the_count(gates, ledger):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    provider = SyntheticProvider(AIProviderConfigError("synthetic setup"))
    sending = Recording()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises(AIProviderConfigError):
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, "count_sent", FEE)
    assert gated.handed == 1 and sending.told[-1] == ("came_back", Call.GENERATION, Delivery.NOT_SENT)
    assert approval_of(database).paused_reason is None


def test_more_input_than_counted_is_charged_in_full_and_pauses(gates, ledger):
    database, _ = ledger
    gate = gates(counted_terms(), COUNTED_BOUND)
    ask(gate, SyntheticProvider(counts=[100]))
    (row,) = requests_of(database)

    # USED has 120 input tokens: within the narrowed reservation, but the count was wrong.
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, "above_count", FEE + COST)
    assert approval_of(database).paused_reason == "above_count"


@pytest.mark.parametrize("narrowed", [False, True], ids=["before-narrowing", "after-narrowing"])
def test_a_restart_tells_a_group_left_before_its_narrowing_from_one_left_after(gates, ledger, narrowed):
    database, _ = ledger
    first = gates(counted_terms(), COUNTED_BOUND)
    request_id = first._reserve("analysis", "owner-a", BEFORE_COUNT)

    if narrowed:
        first._narrow(request_id, AFTER_COUNT)

    # The app stops here.
    first.close()
    gates(counted_terms(), COUNTED_BOUND)
    (row,) = requests_of(database)

    # Before its narrowing only the count may have gone out; after it, the generation may have too.
    expected = ("interrupted", AFTER_COUNT) if narrowed else ("interrupted_counting", BEFORE_COUNT)

    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.UNKNOWN, *expected)
    assert approval_of(database).paused_reason == "interrupted"


def test_the_terms_say_what_one_request_is_and_an_approval_snapshotted_without_it_is_refused(gates, ledger, clock):
    database, _ = ledger
    old = terms().snapshot()
    del old["request_unit"], old["max_input_tokens"]

    with database.transaction() as db:
        db.add(LiveApproval(approval_id=APPROVAL, terms=old, created_at=clock.now(), paused_at=None, paused_reason=None))

    with pytest.raises(LiveRefused, match="条款和第一次使用时不同"):
        gates()

    assert counted_terms().snapshot()["request_unit"] == "count_then_generation"
    assert counted_terms().snapshot()["prices"]["count"] == "0.0002"


def test_a_counted_bound_needs_its_unit_and_its_count_price_written_out(gates):
    with pytest.raises(LiveRefused, match="计量单位"):
        gates(counted_terms(request_unit="generation"), COUNTED_BOUND)

    with pytest.raises(LiveRefused, match="计数的单价"):
        gates(counted_terms(prices=PRICES), COUNTED_BOUND)


# --- a budget policy: one generation, a fixed reservation, accounted from usage (U2-C cost control) ----------


# Made up at the figures of the cost-control handoff, in dollars per million tokens: input 4, cached 0.4,
# 30-minute writes 5, output 20, and reservations at 10.
BUDGET_PRICES = PriceBasis(Decimal("4"), Decimal("0.4"), Decimal("5"), Decimal("20"), Decimal("10"), "synthetic", "2026-11-03", True)
# 16,000 input tokens at 10 and the output limit at 20, in millionths: $0.24 and $0.32.
ANALYSIS_RESERVE = 16_000 * 10 + 4_000 * 20
STRUCTURING_RESERVE = 16_000 * 10 + 8_000 * 20
# USED at BUDGET_PRICES: 40 cached at 0.4, 10 written at 5, the other 70 at 4, and 30 output at 20.
BUDGET_COST = 16 + 50 + 280 + 600


def budget_terms(**changes) -> ApprovalTerms:
    values = {
        "prices": BUDGET_PRICES,
        "input_bound": None,
        "request_unit": "generation",
        "max_output_tokens": 4_000,
        "structuring_max_output_tokens": 8_000,
        "max_cost_micro_usd": 1_000_000,
        "max_input_tokens": 32_000,
        "budget_policy": "fixed_reserve_v1",
        "budget_scope": "application",
        "reserve_input_tokens": 16_000,
        "max_payload_bytes": 32_000,
    }
    values.update(changes)

    return terms(**values)


@pytest.mark.parametrize("purpose, reserved", [("analysis", ANALYSIS_RESERVE), ("structuring", STRUCTURING_RESERVE)])
def test_under_a_budget_policy_a_request_is_one_generation_reserved_at_its_fixed_amount(gates, ledger, purpose, reserved):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    # Were a count ever asked for, it would fail the request.
    provider = SyntheticProvider(counts=[AssertionError("never counted")])
    sending = Recording()

    with gate.task(purpose, "owner-a", provider, sending=sending) as gated:
        gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    assert (reserved, row.reserved_micro_usd) == ({"analysis": 240_000, "structuring": 320_000}[purpose], reserved)
    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.SETTLED, None, BUDGET_COST)
    assert provider.counted == [] and len(provider.calls) == 1 and gated.handed == 1
    assert sending.told == [("hand_on", Call.GENERATION), ("came_back", Call.GENERATION, Delivery.SENT)]
    # Nothing of it is called charged: accounted from usage, with nothing left reserved or to be checked.
    status = gate.status()
    assert (status.settled_micro_usd, status.reserved_micro_usd, status.unknown_micro_usd) == (BUDGET_COST, 0, 0)


@pytest.mark.parametrize("cap, sent", [(ANALYSIS_RESERVE, True), (ANALYSIS_RESERVE - 1, False)], ids=["just-enough", "one-short"])
def test_a_budget_just_enough_lets_a_request_out_and_one_millionth_short_sends_nothing(gates, ledger, cap, sent):
    database, _ = ledger
    gate = gates(budget_terms(max_cost_micro_usd=cap), FIXED_RESERVE_V1)
    provider = SyntheticProvider()

    if sent:
        ask(gate, provider)
        assert len(provider.calls) == 1 and len(requests_of(database)) == 1
    else:
        with pytest.raises(LiveBlocked) as blocked:
            ask(gate, provider)

        assert blocked.value.reason is Blocked.COST and provider.calls == [] and requests_of(database) == []


def test_the_second_request_waits_for_room_for_its_whole_reservation_and_the_request_limit_holds(gates, ledger):
    database, _ = ledger
    # Room for one reservation and what the first is accounted at, less a millionth.
    gate = gates(budget_terms(max_cost_micro_usd=ANALYSIS_RESERVE + BUDGET_COST - 1), FIXED_RESERVE_V1)
    provider = SyntheticProvider()

    with gate.task("analysis", "owner-a", provider) as gated:
        gated.generate_text(PROMPT)

        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    assert blocked.value.reason is Blocked.COST and len(provider.calls) == 1
    assert [(row.state, row.charged_micro_usd) for row in requests_of(database)] == [(LiveRequestState.SETTLED, BUDGET_COST)]

    # One app at a time on a ledger: the next approval opens once this gate is closed.
    gate.close()
    limited = gates(budget_terms(approval_id="synthetic-approval-2", max_requests=1), FIXED_RESERVE_V1)
    ask(limited, provider)

    with pytest.raises(LiveBlocked) as blocked:
        ask(limited, provider)

    assert blocked.value.reason is Blocked.REQUESTS and len(provider.calls) == 2


def test_while_one_request_is_out_no_other_task_starts_or_reserves(gates, ledger):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    provider = Waiting()
    worker = threading.Thread(target=ask, args=(gate, provider))
    worker.start()
    assert provider.entered.wait(10)

    with pytest.raises(LiveBlocked) as blocked:
        with gate.task("structuring", "owner-b", SyntheticProvider()):
            pass

    held = [row.state for row in requests_of(database)]
    provider.release.set()
    worker.join(10)

    assert blocked.value.reason is Blocked.BUSY and held == [LiveRequestState.RESERVED]


SCHEMA_WITH_WORDS = JsonSchemaFormat("synthetic_format", {"type": "object", "description": "中文说明，带“引号”和\"转义\"\n"})


def body_of(prompt: str) -> dict:
    return responses_request(
        "synthetic-model", prompt, max_output_tokens=4_000, reasoning_effort="low", service_tier="default", json_schema_format=SCHEMA_WITH_WORDS
    )


@pytest.mark.parametrize("size, sent", [(32_000, True), (32_001, False)])
def test_a_request_whose_whole_body_is_over_its_payload_limit_is_refused_before_anything_is_reserved(gates, ledger, size, sent):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    # Chinese, quotes and line breaks in the prompt, and the schema too, all count.
    start = "中文提示，带\"引号\"和\n换行："
    prompt = start + "x" * (size - request_bytes(body_of(start)))
    assert request_bytes(body_of(prompt)) == size
    provider = SyntheticProvider()
    sending = Recording()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        if sent:
            gated.generate_text(prompt, json_schema_format=SCHEMA_WITH_WORDS)
        else:
            with pytest.raises(LiveBlocked) as blocked:
                gated.generate_text(prompt, json_schema_format=SCHEMA_WITH_WORDS)

            assert blocked.value.reason is Blocked.PAYLOAD

    if sent:
        assert len(provider.calls) == 1 and len(requests_of(database)) == 1
    else:
        # Not cut short, not split: nothing reserved, nothing handed on, nothing counted as sent.
        assert provider.calls == [] and requests_of(database) == [] and sending.told == [] and gated.handed == 0


def test_a_request_body_with_more_than_plain_text_is_refused_before_anything_is_reserved(gates, ledger):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)

    class Tooled(SyntheticProvider):
        def request_body(self, prompt, **options):
            return {**super().request_body(prompt, **options), "tools": []}

    provider = Tooled()

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.PAYLOAD and provider.calls == [] and requests_of(database) == []


@pytest.mark.parametrize(
    "step, state, reason, charged, paused",
    [
        (USED, "settled", None, BUDGET_COST, None),
        # 500 cached at 0.4, 1,000 written at 5, the other 500 at 4, 100 output at 20: each input token once.
        (
            Usage(2_000, 100, 500, 1_000, 0, 2_100, "default", cache_write_30m_tokens=1_000, cache_write_12h_tokens=0),
            "settled",
            None,
            200 + 5_000 + 2_000 + 2_000,
            None,
        ),
        # More input than the 16,000 planned for, within what the prices were checked for: accounted as it is.
        (Usage(20_000, 300, 0, 0, 0, 20_300, "default"), "settled", None, 20_000 * 4 + 300 * 20, None),
        # Above its reservation: all of it is kept, and the approval pauses.
        (
            Usage(32_000, 5_000, 0, 32_000, 0, 37_000, "default", cache_write_30m_tokens=32_000, cache_write_12h_tokens=0),
            "over",
            "over_reservation",
            32_000 * 5 + 5_000 * 20,
            "over_reservation",
        ),
        # Cost still to be checked: kept at no less than the reservation, and the approval pauses.
        (Usage(service_tier="default"), "unknown", "unknown_usage", ANALYSIS_RESERVE, "unknown_usage"),
        (replace(USED, cache_write_30m_tokens=0, cache_write_12h_tokens=10), "unknown", "unknown_usage", ANALYSIS_RESERVE, "unknown_usage"),
        (replace(USED, service_tier="priority"), "unknown", "unknown_usage", ANALYSIS_RESERVE, "unknown_usage"),
        (AIProviderTimeoutError("The provider request timed out."), "unknown", "no_answer", ANALYSIS_RESERVE, "unknown_usage"),
        # Beyond what the prices were checked for: never priced, kept at the reported counts at the reservation price.
        (Usage(40_000, 30, 0, 0, 0, 40_030, "default"), "unknown", "unknown_usage", 40_000 * 10 + 30 * 20, "unknown_usage"),
    ],
    ids=["usage", "cache", "above-the-plan", "over", "no-usage", "kept-12h", "other-tier", "timeout", "beyond-checked-prices"],
)
def test_under_a_budget_policy_a_request_is_accounted_from_its_usage_and_anything_unknown_pauses(
    gates, ledger, step, state, reason, charged, paused
):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    provider = SyntheticProvider(step)

    with gate.task("analysis", "owner-a", provider) as gated:
        try:
            gated.generate_text(PROMPT)
        except AIProviderError:
            pass

    (row,) = requests_of(database)

    assert (row.state, row.reason, row.charged_micro_usd) == (state, reason, charged)
    assert approval_of(database).paused_reason == paused

    if paused:
        # Never retried, nor anything more sent under a paused approval.
        with pytest.raises(LiveBlocked) as blocked:
            ask(gate, provider)

        assert blocked.value.reason is Blocked.PAUSED and len(provider.calls) == 1

        if state == "unknown":
            assert gate.status().unknown_micro_usd == charged


def test_under_a_budget_policy_a_request_kept_back_before_it_goes_out_is_released(gates, ledger):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    provider = SyntheticProvider()

    with gate.task("analysis", "owner-a", provider, sending=Recording(refuse=Call.GENERATION)) as gated:
        with pytest.raises(RuntimeError):
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    # Cancelled first: nothing went out, so its reservation and its request are given back.
    assert (row.state, row.charged_micro_usd) == (LiveRequestState.RELEASED, 0) and provider.calls == []
    assert gate.status().requests_used == 0


@pytest.mark.parametrize("when", ["building", "handing_on"])
def test_under_a_budget_policy_a_request_out_of_time_before_it_goes_out_is_released_and_blames_no_count(gates, ledger, when):
    database, _ = ledger
    gate = gates(budget_terms(), FIXED_RESERVE_V1)
    now = [100.0]
    gate.monotonic = lambda: now[0]

    class Slow(SyntheticProvider):
        # All of the approval's 30 seconds go while its body is built and measured here,
        def request_body(self, *args, **options):
            if when == "building":
                now[0] += 30.0

            return super().request_body(*args, **options)

    class SlowSending(Recording):
        # or while the task takes it in.
        def hand_on(self, call):
            super().hand_on(call)

            if when == "handing_on":
                now[0] += 30.0

    provider, sending = Slow(counts=[AssertionError("never counted")]), SlowSending()

    with gate.task("analysis", "owner-a", provider, sending=sending) as gated:
        with pytest.raises(LiveBlocked) as blocked:
            gated.generate_text(PROMPT)

    (row,) = requests_of(database)

    # No count to blame and nothing out: reserved, then given back whole, never a cost to check (cost control F1).
    assert blocked.value.reason is Blocked.TIME and provider.calls == [] and provider.counted == []
    assert (row.state, row.reason, row.reserved_micro_usd, row.charged_micro_usd) == (LiveRequestState.RELEASED, None, ANALYSIS_RESERVE, 0)
    status = gate.status()
    assert (status.requests_used, status.settled_micro_usd, status.reserved_micro_usd, status.unknown_micro_usd) == (0, 0, 0, 0)
    assert status.paused_reason is None and gated.handed == 0
    # Handed on, it is told back as not sent; never handed on, the task never hears of it.
    told = [("hand_on", Call.GENERATION), ("came_back", Call.GENERATION, Delivery.NOT_SENT)]
    assert sending.told == (told if when == "handing_on" else [])


def test_under_a_budget_policy_a_restart_keeps_what_was_left_reserved_as_still_to_be_checked(gates, ledger):
    database, _ = ledger
    first = gates(budget_terms(), FIXED_RESERVE_V1)
    first._reserve("analysis", "owner-a", ANALYSIS_RESERVE)
    first.close()
    second = gates(budget_terms(), FIXED_RESERVE_V1)
    (row,) = requests_of(database)

    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.UNKNOWN, "interrupted", ANALYSIS_RESERVE)
    status = second.status()
    assert (status.paused_reason, status.unknown_micro_usd, status.reserved_micro_usd) == ("interrupted", ANALYSIS_RESERVE, 0)


def test_an_approval_first_used_otherwise_is_never_taken_on_by_a_budget_policy(gates, ledger):
    database, _ = ledger
    # The same approval id, first used with a counted bound, as the version before would have.
    gates(counted_terms(), COUNTED_BOUND).close()

    with pytest.raises(LiveRefused, match="条款和第一次使用时不同"):
        gates(budget_terms(), FIXED_RESERVE_V1)


@pytest.mark.parametrize(
    "changes, says",
    [
        ({"prices": replace(BUDGET_PRICES, count=Decimal("0"))}, "计数单价应为不适用"),
        ({"budget_scope": None}, "应用内的预算"),
        ({"budget_scope": "billing"}, "应用内的预算"),
        ({"reserve_input_tokens": 8_000}, "和 fixed_reserve_v1 不符"),
        ({"max_payload_bytes": 64_000}, "和 fixed_reserve_v1 不符"),
        ({"budget_policy": None, "reserve_input_tokens": None, "max_payload_bytes": None}, "和 fixed_reserve_v1 不符"),
        ({"request_unit": "count_then_generation"}, "不用输入上界，也不计数"),
        ({"input_bound": BOUND.name}, "不用输入上界，也不计数"),
    ],
    ids=["count-price", "no-scope", "billing-scope", "other-reserve", "other-payload", "no-policy", "counted-unit", "a-bound"],
)
def test_a_budget_policy_gate_opens_only_for_terms_that_write_it_out(gates, changes, says):
    with pytest.raises(LiveRefused, match=says):
        gates(budget_terms(**changes), FIXED_RESERVE_V1)


def test_a_budget_policy_serves_only_the_model_and_tier_it_is_defined_for(gates):
    real_prices = PriceBasis(Decimal("4"), Decimal("0.4"), Decimal("5"), Decimal("20"), Decimal("10"), "https://synthetic.invalid/pricing", "2026-11-03")

    assert live_gate.RESERVE_POLICIES == {("openai", "gpt-5.6-sol", "default"): FIXED_RESERVE_V1}
    assert live_gate.CERTIFIED_INPUT_BOUNDS == {}

    with pytest.raises(LiveRefused, match="还没有定义预算策略"):
        gates(budget_terms(prices=real_prices), FIXED_RESERVE_V1)

    # A policy made up for tests never stands in for the defined one.
    with pytest.raises(LiveRefused, match="还没有定义预算策略"):
        gates(
            budget_terms(prices=real_prices, provider="openai", model="gpt-5.6-sol", endpoint="https://api.openai.com/v1"),
            replace(FIXED_RESERVE_V1, synthetic=False, name="fixed_reserve_v1"),
        )


def test_the_real_provider_under_the_budget_policy_sends_just_what_was_measured_and_never_counts(gates, ledger):
    database, _ = ledger
    real_prices = PriceBasis(Decimal("4"), Decimal("0.4"), Decimal("5"), Decimal("20"), Decimal("10"), "https://synthetic.invalid/pricing", "2026-11-03")
    approval = budget_terms(prices=real_prices, provider="openai", model="gpt-5.6-sol", endpoint="https://api.openai.com/v1")
    gate = gates(approval, FIXED_RESERVE_V1)

    with patch("app.ai_provider.OpenAI"), patch("app.ai_provider.DefaultHttpxClient"):
        provider = OpenAIProvider(
            OpenAIProviderConfig(
                api_key="synthetic-not-a-key",
                model="gpt-5.6-sol",
                max_retries=0,
                base_url="https://api.openai.com/v1",
                use_environment_proxies=False,
                max_output_tokens=8_000,
                service_tier="default",
            )
        )

    measured = []
    original = provider.request_body

    def measure(*args, **options):
        measured.append(original(*args, **options))
        return measured[-1]

    provider.request_body = measure
    reply = MagicMock(status="completed", output=[], output_text="{}", model="gpt-5.6-sol", service_tier="default")
    reply.usage = MagicMock(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        input_tokens_details=MagicMock(cached_tokens=0, cache_write_tokens=0, cache_write_tokens_details=None),
        output_tokens_details=MagicMock(reasoning_tokens=0),
    )
    provider._client.responses.create.return_value = reply

    with gate.task("analysis", "owner-a", provider) as gated:
        gated.generate_text(PROMPT, json_schema_format=SCHEMA_WITH_WORDS)

    sent = dict(provider._client.responses.create.call_args.kwargs)
    sent.pop("timeout")
    (row,) = requests_of(database)

    # What was measured is what went out; no count was ever asked for.
    assert measured == [sent] and provider._client.responses.input_tokens.count.call_count == 0
    assert (row.reserved_micro_usd, row.state, row.charged_micro_usd) == (ANALYSIS_RESERVE, LiveRequestState.SETTLED, 120 * 4 + 30 * 20)


def test_new_budget_terms_never_get_past_the_default_simulation(monkeypatch):
    environ = {**APPROVED, "ROLERADAR_LIVE_MODEL": "gpt-5.6-sol"}
    without_mode = {name: value for name, value in environ.items() if name != "ROLERADAR_ANALYSIS_MODE"}

    assert live.analysis_setup(without_mode) is None
    assert live.analysis_setup({**without_mode, "ROLERADAR_ANALYSIS_MODE": "simulation"}) is None

    # Asked for in full, the defined policy is what a real service would be reserved by; nothing is built or sent here.
    setup = live.analysis_setup(environ)
    assert setup.basis is FIXED_RESERVE_V1 and setup.terms.budget_policy == "fixed_reserve_v1"
