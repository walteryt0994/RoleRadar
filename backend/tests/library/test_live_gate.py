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
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app import live_gate
from app.ai_provider import (
    AIProvider,
    AIProviderError,
    AIProviderTimeoutError,
    GenerationResult,
    OpenAIProvider,
    OpenAIProviderConfig,
    ProviderIdentity,
    SendState,
    Usage,
)
from app.job_analysis import live
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.models import LiveApproval, LiveRequest, LiveRequestState, create_schema
from app.live_gate import (
    ApprovalTerms,
    Blocked,
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
)
COST = 20 + 25 + 140 + 240
APPROVAL = "synthetic-approval-1"


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
    }
    values.update(changes)

    return ApprovalTerms(**values)


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
    )


class SyntheticProvider(AIProvider):
    """Made up for these tests: answers from a script and sends nothing anywhere."""

    SYNTHETIC = True

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.calls.append({"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        step = self.script.pop(0) if self.script else USED

        if isinstance(step, BaseException):
            raise step

        return answer(step)


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
    cheaper_writes = PriceBasis(Decimal("2"), Decimal("0.5"), None, Decimal("8"), "synthetic", "2026-11-02", True)
    assert reservation(150, 100, cheaper_writes) == 150 * 2 + 100 * 8


def test_a_charge_bills_each_item_once_and_reasoning_as_part_of_the_output():
    assert charge(USED, terms()) == COST == 425
    # A cache item that is not billed on its own is billed as plain input.
    plain_writes = terms(prices=PriceBasis(Decimal("2"), Decimal("0.5"), None, Decimal("8"), "synthetic", "2026-11-02", True))
    assert charge(USED, plain_writes) == 20 + 160 + 240


def test_amounts_are_rounded_up_to_the_millionth_of_a_dollar():
    tiny = PriceBasis(Decimal("0.001"), None, None, Decimal("0.001"), "synthetic", "2026-11-02", True)
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
        {"prices": PriceBasis(Decimal("1"), Decimal("0.5"), Decimal("2.5"), Decimal("8"), "synthetic", "2026-11-02", True)},
        {"prices": PriceBasis(Decimal("2"), None, Decimal("2.5"), Decimal("8"), "synthetic", "2026-11-02", True)},
    ],
    ids=["cost", "requests", "output", "purposes", "tier", "effort", "bytes", "structuring-output", "price", "cache-item"],
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
    same = PriceBasis(Decimal("2.0"), Decimal("0.50"), Decimal("2.500"), Decimal("8.00"), "synthetic", "2026-11-02", True)

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
    # Far more input than the bound said: the worst case was not one.
    provider = SyntheticProvider(Usage(input_tokens=10_000, output_tokens=30, cache_read_tokens=0, cache_write_tokens=0, service_tier="default"))
    ask(gate, provider)
    (row,) = requests_of(database)

    assert row.state == LiveRequestState.OVER and row.reason == "over_reservation"
    assert row.charged_micro_usd == 10_000 * 2 + 30 * 8 > row.reserved_micro_usd == WORST
    assert approval_of(database).paused_reason == "over_reservation"

    with pytest.raises(LiveBlocked) as blocked:
        ask(gate, provider)

    assert blocked.value.reason is Blocked.PAUSED and len(provider.calls) == 1


@pytest.mark.parametrize(
    "step",
    [
        AIProviderTimeoutError("The provider request timed out."),
        Usage(input_tokens=120, output_tokens=30, cache_read_tokens=40, cache_write_tokens=10, service_tier="priority"),
        Usage(service_tier="default"),
        RuntimeError("synthetic failure"),
        replace(USED, total_tokens=9999),
        replace(USED, input_tokens=-10_000),
        replace(USED, malformed=True),
    ],
    ids=["timeout", "other-tier", "no-counts", "not-a-provider-error", "total", "negative", "malformed"],
)
def test_a_request_without_usable_usage_is_charged_at_its_reservation_and_pauses(gates, ledger, step):
    database, _ = ledger
    gate = gates()
    provider = SyntheticProvider(step)

    with gate.task("analysis", "owner-a", provider) as gated:
        try:
            gated.generate_text(PROMPT)
        except (AIProviderError, RuntimeError):
            pass

    (row,) = requests_of(database)

    assert (row.state, row.reason, row.charged_micro_usd) == (LiveRequestState.UNKNOWN, "unknown_usage", WORST)
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
        paused_reason="interrupted", requests_used=2, requests_left=1, committed_micro_usd=COST + WORST, micro_usd_left=10_000 - COST - WORST
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
    real_prices = PriceBasis(Decimal("2"), None, None, Decimal("8"), "https://synthetic.invalid/pricing", "2026-11-02")
    unchecked = InputBound("bytes-plus-margin", BOUND.tokens, "https://synthetic.invalid/docs", "2026-11-02")

    assert live_gate.CERTIFIED_INPUT_BOUNDS == {}

    with pytest.raises(LiveRefused, match="还没有核验过的输入上界"):
        gates(terms(prices=real_prices, input_bound=unchecked.name), unchecked)

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
}


def test_complete_terms_are_still_refused_while_no_input_bound_has_been_checked():
    policy = live.load_live_policy(APPROVED)

    with pytest.raises(LiveRefused, match="还没有为模型 synthetic-model（服务档 default）核验过输入上界"):
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
    ],
)
def test_terms_that_are_missing_or_doubtful_are_refused(change, says):
    environ = {name: value for name, value in {**APPROVED, **change}.items() if value is not None}

    with pytest.raises(LiveRefused, match=says):
        live.load_live_approval(live.load_live_policy(environ), environ)


def test_a_synthetic_bound_is_never_taken_as_a_checked_one(monkeypatch):
    monkeypatch.setitem(live_gate.CERTIFIED_INPUT_BOUNDS, ("openai", "synthetic-model", "default"), BOUND)

    with pytest.raises(LiveRefused, match="核验过输入上界"):
        live.load_live_approval(live.load_live_policy(APPROVED), APPROVED)


def test_terms_read_from_the_environment_are_exact_and_never_synthetic(monkeypatch):
    checked = InputBound("checked-for-test", BOUND.tokens, "https://synthetic.invalid/docs", "2026-11-02")
    monkeypatch.setitem(live_gate.CERTIFIED_INPUT_BOUNDS, ("openai", "synthetic-model", "default"), checked)
    approval = live.load_live_approval(live.load_live_policy(APPROVED), APPROVED)

    assert approval.purposes == {"analysis", "structuring"} and approval.reasoning_effort is None
    assert (approval.max_cost_micro_usd, approval.max_requests, approval.max_output_tokens) == (500_000, 6, 1500)
    assert (approval.structuring_max_bytes, approval.structuring_max_output_tokens) == (8000, 8000)
    assert approval.prices == PriceBasis(
        Decimal("2"), Decimal("0.5"), None, Decimal("8"), "https://synthetic.invalid/pricing", "2026-11-02"
    )
    assert approval.prices.synthetic is False and approval.input_bound == "checked-for-test"


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
    checked = InputBound("checked-for-test", BOUND.tokens, "https://synthetic.invalid/docs", "2026-11-02")
    monkeypatch.setitem(live_gate.CERTIFIED_INPUT_BOUNDS, ("openai", "synthetic-model", "default"), checked)
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

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.entered.set()
        assert self.release.wait(10)

        return super().generate_text(prompt, max_output_tokens, reasoning_effort, json_schema_format)


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

