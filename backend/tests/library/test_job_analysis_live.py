"""U2-C2: the job analysis pages on a real service's control path, offline.

Everything of a real service but the provider: a synthetic approval, input
bound and prices open the gate on the test's own library, and a synthetic
provider answers by the simulation's rules under the approved recipient's
name. Nothing leaves the process, and the launcher blocks the network.
"""
import threading
from contextlib import ExitStack
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    AIProviderConnectionError,
    AIProviderTimeoutError,
    GenerationResult,
    ProviderIdentity,
    responses_request,
)
from app.job_analysis.simulation import SimulatedProvider
from app.job_analysis.tasks import MAX_RUN_SECONDS, TaskStore
from app.library.models import LiveRequestState
from app.library.sends import Sends, sends_text
from app.live_gate import FIXED_RESERVE_V1, LOCK_SUFFIX, FixedReserve, InputBound, LiveSetup
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, handed, review, start, task_of, text_of, wait_until
from library_helpers import LOCAL_BASE_URL, Browser
from test_live_gate import (
    ANALYSIS_RESERVE,
    BOUND,
    BUDGET_PRICES,
    COUNT_PRICE,
    COUNTED_BOUND,
    FEE,
    PRICES,
    requests_of,
    terms,
)

RECIPIENT = ("synthetic", "synthetic-model", "https://synthetic.invalid/v1")
# 1000 input tokens at 2 and 200 output tokens at 8 (test_live_gate.PRICES), in millionths of a dollar.
COST = 1000 * 2 + 200 * 8
# Every request reserves 1000 input tokens at the dearest input item (2.5) and 400 output tokens at 8.
FIXED = InputBound("synthetic-fixed-1000", lambda prompt, output_format: 1000, "synthetic", "2026-11-02", synthetic=True)
FIXED_WORST = 1000 * 2.5 + 400 * 8


def approval(**changes):
    values = {
        "purposes": frozenset({"analysis"}),
        "max_requests": 6,
        "max_cost_micro_usd": 1_000_000,
        "max_output_tokens": 400,
    }
    values.update(changes)

    return terms(**values)


class LiveAnswering(AIProvider):
    """Made up for these tests: answers by the simulation's rules under the approved recipient's name.

    script holds what each call does before it answers, in order: an
    exception to raise, a callable to run (it may wait), or None.
    """

    SYNTHETIC = True

    def __init__(self, *script, identity=RECIPIENT, counts=()):
        self.script = list(script)
        self.calls = []
        self._identity = identity
        # What each count does, behind a counted bound: an exception to raise, or the count.
        self.counts = list(counts)
        self.counted = []

    def identity(self):
        return ProviderIdentity(*self._identity)

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.calls.append({"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        step = self.script.pop(0) if self.script else None

        if isinstance(step, BaseException):
            raise step

        if callable(step):
            step()

        return GenerationResult(
            text=SimulatedProvider().generate_text(prompt, json_schema_format=json_schema_format).text,
            provider="synthetic",
            requested_model="synthetic-model",
            model="synthetic-model",
            latency_seconds=0.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
            input_tokens=1000,
            output_tokens=200,
            total_tokens=1200,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            service_tier="default",
        )

    def count_input_tokens(self, prompt, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.counted.append(prompt)
        step = self.counts.pop(0) if self.counts else 1000

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


class Ticks:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def live(make_config, clock):
    """build(provider, ...) -> an app with a real service on the test's library, and a signed-in browser."""
    stack = ExitStack()

    def build(provider=None, *, make=None, approval_terms=None, bound=BOUND, inline=True):
        provider = provider or LiveAnswering()
        setup = LiveSetup(approval_terms or approval(), bound, make or (lambda: provider))
        application = create_app(make_config(), clock=clock, live=setup)
        # The client keeps the app's lifespan running; leaving it closes the gate.
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))

        if inline:
            application.state.job_analysis.executor = InlineExecutor()

        return application, Browser.signed_in(application, "合成用户 A")

    yield build
    stack.close()


def project(browser) -> str:
    return confirmed(browser, title="订单看板（合成）", actions="用 Python 清洗订单数据。", outcome="看板在课程展示中使用。")


def run(browser) -> str:
    task = task_of(review(browser, [project(browser)]))
    assert start(browser, task).status_code == 303

    return task


def ledger(app):
    return requests_of(app.state.library.database)


def page_text(browser, task) -> str:
    return text_of(browser.get(task).text)


# --- the review and the result ---------------------------------------------------------------------------


def test_a_live_review_names_its_recipient_notice_and_what_is_left(live):
    app, browser = live()
    task = task_of(review(browser, [project(browser)]))
    text = page_text(browser, task)
    (stored,) = app.state.job_analysis.store._tasks.values()

    assert "真实的外部 AI 服务：synthetic 的 synthetic-model，经 https://synthetic.invalid/v1 发送" in text
    assert "不等于不留存" in text and "最多保留 30 天" in text and "无法撤回" in text
    assert "还剩 6 次请求" in text and "确认并开始分析" in text and "本机模拟服务" not in text
    # The fingerprint binds the recipient and the notice the page showed.
    assert (stored.request.recipient.provider, stored.request.recipient.model) == ("synthetic", "synthetic-model")
    assert stored.request.notice_version == "live-3"


def test_a_live_input_page_names_the_real_service_not_the_simulation(live):
    _, browser = live()
    text = page_text(browser, "/job-analysis")

    # The words of the running page (presentation.service_label), never the simulation's fixed text (U3-A0).
    assert "使用真实服务：请求发给 synthetic 的 synthetic-model。" in text and "本机模拟服务" not in text


def test_a_live_task_goes_through_the_gate_and_says_whose_result_it_is(live):
    provider = LiveAnswering()
    app, browser = live(provider)
    text = page_text(browser, run(browser))

    assert "真实服务的结果：由外部 AI 模型生成" in text and handed(2) in text
    assert [(row.state, row.charged_micro_usd) for row in ledger(app)] == [(LiveRequestState.SETTLED, COST)] * 2
    # Both requests ask for the approval's output limit and reasoning effort.
    assert provider.calls == [{"max_output_tokens": 400, "reasoning_effort": "low"}] * 2


def test_a_confirmation_does_not_carry_over_to_another_service_or_notice(live):
    provider = LiveAnswering()
    app, browser = live(provider)
    jobs = app.state.job_analysis
    service = jobs.live
    # Reviewed while the simulation answered, confirmed once the real service is there.
    jobs.live = None
    simulated = task_of(review(browser, [project(browser)]))
    jobs.live = service
    response = start(browser, simulated)

    assert response.status_code == 303 and "notice=service_changed" in response.headers["location"]
    fresh = response.headers["location"].split("?")[0]
    assert fresh != simulated and "真实的外部 AI 服务" in page_text(browser, fresh)

    # A notice that changed after the review is no different.
    service.notice_version = "live-4"
    again = start(browser, fresh)

    assert again.status_code == 303 and "notice=service_changed" in again.headers["location"]
    assert provider.calls == [] and ledger(app) == []


# --- what the gate stops ------------------------------------------------------------------------------------

STOPPED_AFTER_ONE = {
    "stopped": "真实服务已停用，之后的生成请求没有发出",
    "requests": "这批批准的请求次数已经用完，这次请求没有发出",
    "cost": "超出这批批准剩下的应用内预算，所以没有发出",
}


@pytest.mark.parametrize("case", list(STOPPED_AFTER_ONE))
def test_a_request_the_gate_stops_is_not_counted_and_the_one_before_it_still_is(live, case):
    gates = []
    # In the stop case the Owner puts the stop file in place while the first request is out.
    provider = LiveAnswering((lambda: gates[0].stop_file.write_text("")) if case == "stopped" else None)
    limits = {"requests": {"max_requests": 1}, "cost": {"max_cost_micro_usd": 9_000, "input_bound": FIXED.name}}
    app, browser = live(provider, approval_terms=approval(**limits.get(case, {})), bound=FIXED if case == "cost" else BOUND)
    gates.append(app.state.job_analysis.live.gate)
    text = page_text(browser, run(browser))

    assert STOPPED_AFTER_ONE[case] in text and handed(1) in text
    assert len(provider.calls) == 1
    assert [(row.state, row.charged_micro_usd) for row in ledger(app)] == [(LiveRequestState.SETTLED, COST)]
    # The cost case: room for one worst case, not for a second one beside the first's charge.
    assert case != "cost" or COST + FIXED_WORST > 9_000 >= FIXED_WORST


def test_the_review_offers_no_start_while_the_gate_refuses_and_a_start_posted_anyway_sends_nothing(live):
    provider = LiveAnswering()
    app, browser = live(provider)
    task = task_of(review(browser, [project(browser)]))
    app.state.job_analysis.live.gate.stop_file.write_text("")
    page = browser.get(task).text

    assert "确认并开始分析" not in page and "真实服务已停用（库目录里有停用文件）" in page

    response = start(browser, task)

    assert response.status_code == 409 and "真实服务已停用" in text_of(response.text)
    assert provider.calls == [] and ledger(app) == []


def test_another_live_task_running_keeps_this_one_from_sending(live):
    provider = LiveAnswering()
    app, browser = live(provider)
    gate = app.state.job_analysis.live.gate

    with gate.task("analysis", "someone-else", LiveAnswering()):
        task = run(browser)

    text = page_text(browser, task)

    assert "另一个真实分析正在进行" in text and handed() in text
    assert provider.calls == [] and ledger(app) == []


# --- failures, timeouts, cancelling ------------------------------------------------------------------------


def broken():
    raise RuntimeError("synthetic: cannot build the provider")


@pytest.mark.parametrize(
    "setup",
    [{"make": broken}, {"provider": LiveAnswering(identity=("synthetic", "another-model", RECIPIENT[2]))}],
    ids=["cannot-build", "not-the-approved-recipient"],
)
def test_a_provider_that_cannot_be_used_ends_the_task_at_once_with_nothing_sent(live, setup):
    app, browser = live(**setup)
    task = run(browser)
    text = page_text(browser, task)

    assert "真实服务无法使用" in text and handed() in text
    assert browser.get(f"{task}/status").json()["stage"] == "done" and ledger(app) == []
    assert "provider" not in setup or setup["provider"].calls == []


def test_a_parse_that_times_out_falls_back_to_rules_is_never_called_ai_and_pauses_the_batch(live):
    provider = LiveAnswering(AIProviderTimeoutError("The provider request timed out."))
    app, browser = live(provider)
    text = page_text(browser, run(browser))

    assert "规则降级的结果" in text and "不是 AI 的判断" in text and "按规则找到的技能词" in text
    # No answer came: it may have gone out, and the page says only that.
    assert "真实服务的结果：" not in text and handed(maybe=1) in text
    (row,) = ledger(app)
    assert (row.state, row.reason) == (LiveRequestState.UNKNOWN, "no_answer")

    # Its cost could not be known, so the batch is paused: the next review offers no start.
    page = browser.get(task_of(review(browser, [project(browser)]))).text

    assert "已暂停：有一次请求的费用无法确认" in text_of(page) and "确认并开始分析" not in page


@pytest.mark.parametrize("how", ["cancel", "time_limit"])
def test_a_request_out_when_the_task_stops_is_settled_and_its_answer_dropped(live, how):
    arrived, release = threading.Event(), threading.Event()

    def hold():
        arrived.set()
        assert release.wait(10)

    provider = LiveAnswering(hold)
    app, browser = live(provider, inline=False)
    ticks = Ticks()
    app.state.job_analysis.store = TaskStore(ticks)
    task = run(browser)
    assert arrived.wait(10)

    if how == "cancel":
        assert browser.post(f"{task}/cancel", {"csrf_token": browser.csrf_token()}).status_code == 303
    else:
        ticks.now += MAX_RUN_SECONDS + 1

    # While its request is out the task already says it was handed on: it may have gone out.
    assert browser.get(f"{task}/status").json()["sends"] == sends_text(Sends(generation_maybe=1))
    release.set()
    wait_until(lambda: [row.state for row in ledger(app)] == [LiveRequestState.SETTLED])
    app.state.job_analysis.executor.shutdown(wait=True)
    page = browser.get(task).text

    assert "分析已取消" in page and handed(1) in page and "岗位分析结果" not in page
    assert len(provider.calls) == 1


# --- where a stop and a request meet, and what counts as sent ------------------------------------------------


def stop(how, store, ticks, task_id):
    task = store._tasks[task_id]

    if how == "cancel":
        store.cancel(task_id, task.owner_id, task.session)
    elif how == "logout":
        store.end_session(task.session)
    else:
        ticks.now += MAX_RUN_SECONDS + 1


def states_of(app):
    return sorted((row.state, row.charged_micro_usd) for row in ledger(app))


@pytest.mark.parametrize("how", ["cancel", "time_limit", "logout"])
@pytest.mark.parametrize("at", [1, 2])
def test_a_task_that_stops_while_its_request_is_reserved_hands_nothing_on(live, monkeypatch, how, at):
    provider = LiveAnswering()
    app, browser = live(provider)
    ticks = Ticks()
    store = app.state.job_analysis.store = TaskStore(ticks)
    gate = app.state.job_analysis.live.gate
    task = task_of(review(browser, [project(browser)]))
    reserve, reserved = gate._reserve, []

    def reserve_then_stop(*args):
        reserved.append(reserve(*args))

        # The stop comes after request `at` is reserved and before it is handed on.
        if len(reserved) == at:
            stop(how, store, ticks, task.rsplit("/", 1)[1])

        return reserved[-1]

    monkeypatch.setattr(gate, "_reserve", reserve_then_stop)
    assert start(browser, task).status_code == 303

    assert len(provider.calls) == at - 1
    # Its reservation is released; a request sent before it is settled as usual.
    assert states_of(app) == sorted([(LiveRequestState.SETTLED, COST)] * (at - 1) + [(LiveRequestState.RELEASED, 0)])

    if how == "logout":
        assert len(store) == 0
    else:
        text = page_text(browser, task)
        assert "分析已取消" in text and handed(at - 1) in text


@pytest.mark.parametrize("at", [1, 2])
def test_a_request_its_provider_stopped_before_sending_is_not_counted_and_one_before_it_still_is(live, at):
    provider = LiveAnswering(*[None] * (at - 1), AIProviderConfigError("synthetic: stopped before sending"))
    app, browser = live(provider)
    task = run(browser)
    stored = app.state.job_analysis.store._tasks[task.rsplit("/", 1)[1]]
    text = page_text(browser, task)

    assert (stored.sends, stored.result.requests_sent, stored.out) == (Sends(generation_sent=at - 1), at - 1, Sends())
    assert handed(at - 1) in text and "这次请求在发出之前就停止了" in text
    # It speaks of this request only: never of nothing sent, nor of a failure of the program.
    assert "没有发送任何请求" not in text and "程序出错" not in text
    assert states_of(app) == sorted([(LiveRequestState.SETTLED, COST)] * (at - 1) + [(LiveRequestState.RELEASED, 0)])
    assert len(provider.calls) == at


@pytest.mark.parametrize(
    "error", [AIProviderTimeoutError("synthetic"), AIProviderConnectionError("synthetic")], ids=["timeout", "connection"]
)
def test_a_request_that_may_have_gone_out_stays_counted_whatever_failed(live, error):
    app, browser = live(LiveAnswering(None, error))
    text = page_text(browser, run(browser))

    # One answered, one that may have gone out: never counted as sure.
    assert handed(1, 1) in text
    assert [state for state, _ in states_of(app)] == sorted([LiveRequestState.SETTLED, LiveRequestState.UNKNOWN])


def test_without_a_live_setup_the_simulation_answers_and_no_gate_is_opened(app, browser_a, make_config):
    path = make_config().db_path

    assert app.state.job_analysis.live is None
    assert not path.with_name(path.name + LOCK_SUFFIX).exists()
    assert "本机模拟服务" in page_text(browser_a, task_of(review(browser_a, [project(browser_a)])))


# --- behind a counted bound: a count, then each generation (U2-C4 prerequisites) ---------------------------


def counted(**changes):
    values = {
        "prices": replace(PRICES, count=COUNT_PRICE),
        "input_bound": COUNTED_BOUND.name,
        "request_unit": "count_then_generation",
        "max_input_tokens": 1000,
    }
    values.update(changes)

    return approval(**values)


def test_a_counted_analysis_counts_before_each_generation_and_says_both(live):
    provider = LiveAnswering()
    app, browser = live(provider, approval_terms=counted(), bound=COUNTED_BOUND)
    text = page_text(browser, run(browser))

    assert len(provider.counted) == len(provider.calls) == 2
    assert "真实服务的结果：由外部 AI 模型生成" in text and handed(2, counted=2) in text
    assert states_of(app) == [(LiveRequestState.SETTLED, FEE + COST)] * 2


@pytest.mark.parametrize(
    "error, says",
    [
        (AIProviderTimeoutError("synthetic"), handed(counted_maybe=1)),
        (AIProviderConfigError("synthetic: cannot count"), handed()),
    ],
    ids=["count-timed-out", "count-never-sent"],
)
def test_a_count_that_failed_before_the_parse_rejects_the_task_and_never_falls_back_to_rules(live, error, says):
    provider = LiveAnswering(counts=[error])
    app, browser = live(provider, approval_terms=counted(), bound=COUNTED_BOUND)
    text = page_text(browser, run(browser))

    assert provider.calls == [] and "发送前的计数没有完成" in text and says in text
    assert "规则降级的结果" not in text and "真实服务的结果：" not in text


def test_a_count_over_the_largest_input_stops_the_analysis_after_the_parse(live):
    provider = LiveAnswering(counts=[1000, 1001])
    app, browser = live(provider, approval_terms=counted(), bound=COUNTED_BOUND)
    text = page_text(browser, run(browser))

    # The parse went out; of the analysis only its count.
    assert len(provider.calls) == 1 and handed(1, counted=2) in text
    assert "超过了这批批准的单次输入上限，所以没有发出生成请求" in text
    assert states_of(app) == sorted([(LiveRequestState.SETTLED, FEE + COST), (LiveRequestState.SETTLED, FEE)])


def test_the_review_says_what_goes_out_and_that_the_budget_is_no_bill_limit(live):
    app, browser = live()
    page = text_of(browser.get(task_of(review(browser, [project(browser)]))).text)

    # Generations only, two to an analysis; no count, and no promise about the provider's bill.
    assert "一次分析通常发出两个请求" in page and "计数" not in page
    assert "不是服务商账单的硬上限" in page and "法律要求或为防止伤害时可能更长" in page


@pytest.mark.parametrize("took", [30.0, 31.0], ids=["all-of-it", "more-than-all"])
def test_a_count_that_used_up_the_time_leaves_no_generation_out_on_the_task_or_its_page(live, took):
    now = [100.0]

    def slowest():
        now[0] += took

        return 1000

    provider = LiveAnswering(counts=[slowest])
    app, browser = live(provider, approval_terms=counted(), bound=COUNTED_BOUND)
    app.state.job_analysis.live.gate.monotonic = lambda: now[0]
    task = run(browser)
    stored = app.state.job_analysis.store._tasks[task.rsplit("/", 1)[1]]
    text = page_text(browser, task)

    # Count 1, generation 0, nothing out: the task, its page and the ledger agree (C4 prerequisites acceptance F1).
    assert provider.calls == [] and (stored.sends, stored.out) == (Sends(count_sent=1), Sends())
    assert handed(counted=1) in text and "发送前的计数没有完成" in text
    assert states_of(app) == [(LiveRequestState.SETTLED, FEE)]


def test_a_task_cancelled_while_its_count_is_out_counts_the_count_and_no_generation(live):
    held = {}

    def count_then_cancel():
        stop("cancel", held["store"], None, held["task"])

        return 1000

    provider = LiveAnswering(counts=[count_then_cancel])
    app, browser = live(provider, approval_terms=counted(), bound=COUNTED_BOUND)
    held["store"] = app.state.job_analysis.store
    task = task_of(review(browser, [project(browser)]))
    held["task"] = task.rsplit("/", 1)[1]
    assert start(browser, task).status_code == 303
    stored = held["store"]._tasks[held["task"]]
    text = page_text(browser, task)

    assert provider.calls == [] and (stored.sends, stored.out) == (Sends(count_sent=1), Sends())
    assert "分析已取消" in text and handed(counted=1) in text
    assert states_of(app) == [(LiveRequestState.SETTLED, FEE)]


# --- under the budget policy: generations only, a fixed reservation, accounted from usage (U2-C cost control) -


# What LiveAnswering reports (1,000 in, 200 out) at BUDGET_PRICES, in millionths of a dollar.
BUDGET_COST = 1_000 * 4 + 200 * 20


def budgeted(**changes):
    values = {
        "prices": BUDGET_PRICES,
        "input_bound": None,
        "request_unit": "generation",
        "max_output_tokens": 4_000,
        "max_input_tokens": 32_000,
        "budget_policy": "fixed_reserve_v1",
        "budget_scope": "application",
        "reserve_input_tokens": 16_000,
        "max_payload_bytes": 32_000,
    }
    values.update(changes)

    return approval(**values)


def test_under_the_budget_policy_an_analysis_generates_twice_and_never_counts(live):
    # Were a count ever asked for, it would fail the task.
    provider = LiveAnswering(counts=[AssertionError("never counted")])
    app, browser = live(provider, approval_terms=budgeted(), bound=FIXED_RESERVE_V1)
    text = page_text(browser, run(browser))

    assert provider.counted == [] and len(provider.calls) == 2
    assert "真实服务的结果：由外部 AI 模型生成" in text and handed(2) in text
    assert [(row.reserved_micro_usd, row.state, row.charged_micro_usd) for row in ledger(app)] == [
        (ANALYSIS_RESERVE, LiveRequestState.SETTLED, BUDGET_COST)
    ] * 2


def test_the_second_step_of_an_analysis_stops_when_the_budget_cannot_take_its_reservation(live):
    provider = LiveAnswering()
    app, browser = live(provider, approval_terms=budgeted(max_cost_micro_usd=ANALYSIS_RESERVE + BUDGET_COST - 1), bound=FIXED_RESERVE_V1)
    text = page_text(browser, run(browser))

    # The parse went out and is accounted; the analysis never did.
    assert len(provider.calls) == 1 and handed(1) in text
    assert "保守预留会超出这批批准剩下的应用内预算" in text
    assert states_of(app) == [(LiveRequestState.SETTLED, BUDGET_COST)]


def test_a_request_too_large_for_its_budget_policy_is_refused_and_nothing_goes_out(live):
    small = FixedReserve("synthetic_small", reserve_input_tokens=16_000, max_payload_bytes=100, synthetic=True)
    provider = LiveAnswering()
    app, browser = live(provider, approval_terms=budgeted(budget_policy=small.name, max_payload_bytes=100), bound=small)
    text = page_text(browser, run(browser))

    assert provider.calls == [] and ledger(app) == [] and handed() in text
    assert "完整内容超过了这批批准允许的大小" in text and "不会被截短或拆开重发" in text


@pytest.mark.parametrize("when", ["building", "handing_on"])
def test_under_the_budget_policy_a_request_out_of_time_before_it_goes_out_blames_no_count_or_service(live, monkeypatch, when):
    ticks = Ticks()

    class Slow(LiveAnswering):
        # All of the approval's 30 seconds go while the parse is built and measured here,
        def request_body(self, *args, **options):
            if when == "building":
                ticks.now += 30.0

            return super().request_body(*args, **options)

    provider = Slow(counts=[AssertionError("never counted")])
    app, browser = live(provider, approval_terms=budgeted(), bound=FIXED_RESERVE_V1)
    app.state.job_analysis.live.gate.monotonic = ticks
    store = app.state.job_analysis.store
    hand_on = store.hand_on

    def slow_hand_on(task_id, call):
        # or while the task takes it in.
        handed_on = hand_on(task_id, call)

        if when == "handing_on":
            ticks.now += 30.0

        return handed_on

    monkeypatch.setattr(store, "hand_on", slow_hand_on)
    task = run(browser)
    stored = store._tasks[task.rsplit("/", 1)[1]]
    text = page_text(browser, task)

    # Nothing out and nothing left out; its reservation given back (cost control F1).
    assert provider.calls == [] and provider.counted == [] and (stored.sends, stored.out) == (Sends(), Sends())
    assert states_of(app) == [(LiveRequestState.RELEASED, 0)] and handed() in text
    assert "本机的准备和发送前检查用完了这次请求的时限，所以没有发出生成请求。" in text
    # Neither a count nor the service failed, and the parse never falls back to rules for it.
    assert "计数没有完成" not in text and "没有在限定时间内回答" not in text and "服务超时" not in text


def test_under_the_budget_policy_a_request_out_when_the_task_is_cancelled_is_accounted_and_its_answer_dropped(live):
    arrived, release = threading.Event(), threading.Event()

    def hold():
        arrived.set()
        assert release.wait(10)

    provider = LiveAnswering(hold)
    app, browser = live(provider, approval_terms=budgeted(), bound=FIXED_RESERVE_V1, inline=False)
    task = run(browser)
    assert arrived.wait(10)
    assert browser.post(f"{task}/cancel", {"csrf_token": browser.csrf_token()}).status_code == 303
    release.set()
    wait_until(lambda: [row.state for row in ledger(app)] == [LiveRequestState.SETTLED])
    app.state.job_analysis.executor.shutdown(wait=True)
    page = browser.get(task).text

    assert "分析已取消" in page and handed(1) in page and "岗位分析结果" not in page
    assert states_of(app) == [(LiveRequestState.SETTLED, BUDGET_COST)] and len(provider.calls) == 1


def test_a_request_whose_cost_is_unknown_keeps_its_reservation_still_to_be_checked_and_the_paused_review_says_so(live):
    provider = LiveAnswering(AIProviderTimeoutError("The provider request timed out."))
    app, browser = live(provider, approval_terms=budgeted(), bound=FIXED_RESERVE_V1)
    page_text(browser, run(browser))
    page = text_of(browser.get(task_of(review(browser, [project(browser)]))).text)

    # Kept at its whole reservation, never settled, never free; the analysis step was not let through.
    assert states_of(app) == [(LiveRequestState.UNKNOWN, ANALYSIS_RESERVE)] and len(provider.calls) == 1
    assert "已暂停：有一次请求的费用无法确认" in page and "确认并开始分析" not in page
    assert "还剩约 $0.76（按用量核算 $0.00、预留占用 $0.00、费用待核实 $0.24）" in page


def test_the_review_tells_what_is_accounted_reserved_or_still_to_be_checked_and_promises_no_bill_limit(live):
    app, browser = live(approval_terms=budgeted(), bound=FIXED_RESERVE_V1)
    before = text_of(browser.get(task_of(review(browser, [project(browser)]))).text)
    page_text(browser, run(browser))
    after = text_of(browser.get(task_of(review(browser, [project(browser)]))).text)

    assert "应用内预算 $1.00，还剩约 $1.00（按用量核算 $0.00、预留占用 $0.00、费用待核实 $0.00）" in before
    # Two requests accounted from their usage, $0.008 each: never called charged or billed.
    assert "还剩约 $0.98（按用量核算 $0.02、预留占用 $0.00、费用待核实 $0.00）" in after
    assert "不是服务商账单的硬上限" in after and "已扣款" not in after
