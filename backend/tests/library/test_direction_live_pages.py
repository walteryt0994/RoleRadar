"""方向探索 through the pages on a real service's control path, offline (the direction pages on the real service).

Everything of a real service but the provider: a synthetic approval under the real budget policy opens the gate on
the test's own library, and a synthetic provider with the real recipient's name (OpenAI's gpt-5.6-sol at its
official address) answers by the local simulation's rules. Nothing leaves the process: building a real service's
client fails the test, and the launcher blocks the network. What is checked is what C precheck §28 asks of the
offline batch: which service every page names and from where, one request per confirmation through the shared gate
and ledger, the gate's refusals and the approval's limits, stops before and after the request went out, the app
stopping with a request out, costs the ledger could not settle, a ledger that could not be read or written, and
that the pages' other boundaries hold. None of this is a real service's validation.
"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace

import openai
import pytest
from fastapi.testclient import TestClient

from app.ai_provider import AIProvider, ProviderIdentity, SendState, responses_request
from app.directions import presentation
from app.directions.tasks import MAX_RUN_SECONDS, ExplorationTaskStore
from app.job_analysis.simulation import SimulatedProvider
from app.library.errors import ConfigError, StorageError
from app.library.models import DirectionExploration, LiveRequestState
from app.library.sends import Sends, sends_text
from app.live_gate import FIXED_RESERVE_V1, LiveSetup
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, text_of, wait_until
from job_helpers import review as job_review
from job_helpers import start as job_start
from job_helpers import task_of as job_task_of
from library_helpers import LOCAL_BASE_URL, Browser, forms_in
from sqlalchemy import select
from test_direction_pages import PREFS, PROJECT, delete, directions_shown, edit, review, start, task_of
from test_live_gate import USED, answer, budget_terms, requests_of

OPENAI = ("openai", "gpt-5.6-sol", "https://api.openai.com/v1")
SYNTHETIC = ("synthetic", "synthetic-model", "https://synthetic.invalid/v1")
# One request of an approval for direction alone: 16,000 input tokens at 10 and 8,000 output tokens at 20 (the
# made-up prices of test_live_gate.BUDGET_PRICES), in millionths of a dollar: $0.32.
RESERVE = 16_000 * 10 + 8_000 * 20
# USED at those prices: 40 cached at 0.4, 10 written at 5, the other 70 at 4, and 30 output at 20.
COST = 16 + 50 + 280 + 600
# Usage the ledger cannot price (another tier than the approval's), and usage it prices above the reservation: the
# most input the prices were checked for and more output than was asked for, $0.327866 at those prices.
UNPRICED = replace(USED, service_tier="priority")
ABOVE = replace(USED, input_tokens=32_000, output_tokens=10_000, total_tokens=42_000)
START_BUTTON = "确认并开始探索"


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    """Building a real service's client fails the test: nothing here may reach one."""

    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


class Answering(AIProvider):
    """Made up: answers as the local simulation answers, under the name it is given (the real recipient's by
    default). script holds what each call does before it answers, in order: an exception to raise, a callable to run
    (it may wait), or None. usage is what each answer reports."""

    SYNTHETIC = True

    def __init__(self, *script, identity=OPENAI, usage=USED):
        self.script = list(script)
        self.calls = []
        self._identity = identity
        self.usage = usage

    def identity(self):
        return ProviderIdentity(*self._identity)

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        return responses_request(
            self._identity[1],
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            service_tier="default",
            json_schema_format=json_schema_format,
        )

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.calls.append({"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        step = self.script.pop(0) if self.script else None

        if isinstance(step, BaseException):
            raise step

        if callable(step):
            step()

        text = SimulatedProvider().generate_text(prompt, json_schema_format=json_schema_format).text
        provider, model = self._identity[:2]

        return replace(answer(self.usage), text=text, provider=provider, requested_model=model, model=model)


class Ticks:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def approval(purposes=("direction",), recipient=OPENAI, **changes):
    """A made-up approval under the real budget policy, with made-up prices, for these purposes."""
    provider, model, endpoint = recipient
    values = {
        "purposes": frozenset(purposes),
        "provider": provider,
        "model": model,
        "endpoint": endpoint,
        "max_output_tokens": 8_000 if set(purposes) == {"direction"} else 4_000,
        "max_requests": 4,
        "max_cost_micro_usd": 1_300_000,
        "timeout_seconds": 120.0,
    }
    values.update(changes)

    return budget_terms(**values)


@pytest.fixture
def live(make_config, clock):
    """build(provider, ...) -> an app with a real service on the test's library, and user A signed in."""
    stack = ExitStack()

    def build(provider=None, *, purposes=("direction",), inline=True, **changes):
        provider = provider or Answering()
        setup = LiveSetup(approval(purposes, **changes), FIXED_RESERVE_V1, lambda: provider)
        application = create_app(make_config(), clock=clock, live=setup)
        # The client keeps the app's lifespan running; leaving it closes the gate.
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))

        if inline:
            application.state.directions.executor = InlineExecutor()

        return application, Browser.signed_in(application, "合成用户 A")

    yield build
    stack.close()


def material(browser) -> str:
    """User A's preferences and one confirmed project, entered through the pages; the project's id."""
    assert browser.submit("/job-preferences/edit", "/job-preferences", **PREFS).status_code == 303

    return confirmed(browser, **PROJECT)


def ledger(app):
    return requests_of(app.state.library.database)


def rows(app):
    return [(row.state, row.charged_micro_usd) for row in ledger(app)]


def page_text(browser, path) -> str:
    return text_of(browser.get(path).text)


def sent(generated=0, maybe=0) -> str:
    return "已交给服务的请求：" + sends_text(Sends(generation_sent=generated, generation_maybe=maybe))


def stop(how, store, ticks, task_url):
    task = store._tasks[task_url.rsplit("/", 1)[1]]

    if how == "cancel":
        store.cancel(task.id, task.owner_id, task.session)
    elif how == "logout":
        store.end_session(task.session)
    else:
        ticks.now += MAX_RUN_SECONDS + 1


def held():
    """A step that waits until released, and the events to know it arrived and to release it."""
    arrived, release = threading.Event(), threading.Event()

    def hold():
        arrived.set()
        assert release.wait(10)

    return hold, arrived, release


def saved_page(browser, task) -> str:
    """Save a finished task's result as the user asks; the saved exploration's page."""
    assert browser.post(f"{task}/save", {"csrf_token": browser.csrf_token()}).status_code == 303
    (exploration,) = re.findall(r'href="(/directions/explorations/[^"]+)"', browser.get(task).text)

    return exploration


# --- which service every page names -----------------------------------------------------------------------------------


def test_without_a_real_service_every_page_says_simulation_and_nothing_names_an_approval(app, browser_a):
    app.state.directions.executor = InlineExecutor()
    item = material(browser_a)
    task = task_of(review(browser_a, [item]))
    (stored,) = app.state.directions.store._tasks.values()
    before = page_text(browser_a, task)
    assert start(browser_a, task).status_code == 303
    after = page_text(browser_a, task)

    assert app.state.directions.live is None and stored.request.notice_version == "direction-1"
    assert "使用本机模拟服务：由固定规则生成结果，不是 AI。" in page_text(browser_a, "/directions")
    assert "本机模拟服务：RoleRadar 内置的固定规则，不是 AI。" in before and "本批批准" not in before
    assert "（本机模拟，没有联网，不产生费用）" in after and "费用状态" not in after and "真实服务" not in after


def test_an_approval_without_direction_leaves_the_directions_on_the_simulation_and_is_never_borrowed(live):
    provider = Answering()
    app, browser = live(provider, purposes=("analysis",))
    item = material(browser)
    task = task_of(review(browser, [item]))
    assert start(browser, task).status_code == 303

    assert app.state.directions.live is None and app.state.job_analysis.live is not None
    assert "使用本机模拟服务" in page_text(browser, "/directions") and "使用真实服务" in page_text(browser, "/job-analysis")
    assert "（本机模拟，没有联网，不产生费用）" in page_text(browser, task)
    assert provider.calls == [] and ledger(app) == []

    # Were the analyses' service handed to the directions all the same, its gate would still let nothing through.
    app.state.directions.live = app.state.job_analysis.live
    borrowed = task_of(review(browser, [item]))
    assert start(browser, borrowed).status_code == 303
    text = page_text(browser, borrowed)

    assert presentation.REJECTED["gate_purpose"] in text and sent() in text
    assert provider.calls == [] and ledger(app) == []


@pytest.mark.parametrize(
    "how, says",
    [
        ("incomplete_live_mode", "要求使用真实服务，但不能启用"),
        ("direction_for_an_uncovered_service", "方向探索的处理说明只适用于 OpenAI 的官方地址"),
    ],
)
def test_a_real_service_asked_for_and_not_usable_stops_the_app_from_starting(make_config, clock, how, says):
    config = make_config()

    with pytest.raises(ConfigError) as refused:
        if how == "incomplete_live_mode":
            create_app(config, clock=clock, environ={"ROLERADAR_ANALYSIS_MODE": "live"})
        else:
            setup = LiveSetup(approval(recipient=SYNTHETIC), FIXED_RESERVE_V1, lambda: Answering(identity=SYNTHETIC))
            create_app(config, clock=clock, live=setup)

    assert says in str(refused.value)
    # Refused before anything was created: no library, so no ledger and no lock either.
    assert not config.db_path.exists()


def test_the_live_pages_name_the_service_its_notice_and_the_approval_and_opening_them_sends_nothing(live):
    provider = Answering()
    app, browser = live(provider)
    entry = page_text(browser, "/directions")
    task = task_of(review(browser, [material(browser)]))
    text = page_text(browser, task)
    (stored,) = app.state.directions.store._tasks.values()

    assert "使用真实的外部 AI 服务：OpenAI 的 gpt-5.6-sol。" in entry and "本机模拟服务" not in entry
    assert "外部 AI 服务：OpenAI 的 gpt-5.6-sol，请求地址 https://api.openai.com/v1。" in text
    assert "一次探索只发一个请求，不重试" in text and "不等于不留存" in text and "最多保留 30 天" in text
    assert "仍然是留存" in text and "无法撤回" in text and "要不要保存由你决定" in text
    assert "synthetic-approval-1：还剩 4 次请求。应用内预算 $1.30" in text and "不是服务商账单的硬上限" in text
    assert START_BUTTON in text and "本机模拟服务" not in text and "不产生费用" not in text
    # The fingerprint binds the recipient and the notice the page showed.
    assert (stored.request.recipient.provider, stored.request.recipient.model, stored.request.recipient.endpoint) == OPENAI
    assert stored.request.notice_version == "direction-live-2"
    assert provider.calls == [] and ledger(app) == []


# --- one confirmation, one request --------------------------------------------------------------------------------------


def confirmation(browser, task) -> dict:
    """The fields the review page's form would post to start, to post again later as an old form would."""
    page = browser.get(task).text
    form = next(form for form in forms_in(page) if form.action == f"{task}/confirm")

    return {**form.fields, "action": "start", "send": re.findall(r'name="send" value="([^"]+)" checked', page)}


def test_one_confirmation_sends_one_request_through_the_gate_and_the_user_decides_what_is_kept(live):
    provider = Answering()
    app, browser = live(provider)
    task = task_of(review(browser, [material(browser)]))
    form = confirmation(browser, task)
    assert browser.post(f"{task}/confirm", form).status_code == 303
    text = page_text(browser, task)
    (row,) = ledger(app)

    assert "真实服务的结果：由外部 AI 模型（OpenAI 的 gpt-5.6-sol）生成，引文已在本机逐条核对。" in text
    assert sent(1) in text and "费用按服务商报告的用量在应用内核算" in text and "不是这一次探索单独的费用" in text
    assert "本机模拟" not in text and "不产生费用" not in text and "没有联网" not in text
    assert (row.state, row.charged_micro_usd, row.reserved_micro_usd, row.purpose) == (LiveRequestState.SETTLED, COST, RESERVE, "direction")
    # The approval's output limit for direction alone and its reasoning effort, whatever the workflow asks.
    assert provider.calls == [{"max_output_tokens": 8_000, "reasoning_effort": "low"}]

    # A second click, or the same form posted again, starts nothing; nothing is kept until the user saves.
    again = browser.post(f"{task}/confirm", form)
    assert again.status_code == 303 and again.headers["location"].endswith("?notice=already")
    assert len(provider.calls) == 1 and len(ledger(app)) == 1

    with app.state.library.database.transaction() as db:
        assert db.scalars(select(DirectionExploration)).all() == []

    record = page_text(browser, saved_page(browser, task))

    with app.state.library.database.transaction() as db:
        (saved,) = db.scalars(select(DirectionExploration)).all()
        generation = dict(saved.generation)

    assert (generation["provider"], generation["model"], generation["endpoint"]) == OPENAI
    assert (generation["prompt_version"], generation["notice_version"]) == ("3", "direction-live-2")
    assert "openai 的 gpt-5.6-sol；提示词版本 3，处理说明版本 direction-live-2" in record and "本机模拟服务" not in record


def test_an_old_form_a_changed_notice_or_material_gone_before_sending_sends_nothing(live, monkeypatch):
    provider = Answering()
    app, browser = live(provider)
    item = material(browser)
    task = task_of(review(browser, [item]))

    stale = start(browser, task, fingerprint="0" * 64)
    assert stale.status_code == 409 and "旧的核对页面" in stale.text

    # Reviewed under another notice than the app's now: that review is redone, never started.
    monkeypatch.setattr(presentation, "LIVE_NOTICE_VERSION", "direction-live-test")
    redone = start(browser, task)
    assert redone.status_code == 303 and redone.headers["location"].endswith("?notice=service_changed")
    assert "接收方或处理说明已经变化，所以没有开始" in page_text(browser, redone.headers["location"])
    monkeypatch.undo()

    gone = task_of(review(browser, [item]))
    delete(browser, item)
    assert start(browser, gone).status_code == 303

    assert "所选的经历已经不能按核对时的版本使用，所以没有发送" in page_text(browser, gone)
    assert provider.calls == [] and ledger(app) == []


# --- what the gate and the approval refuse ------------------------------------------------------------------------------


@pytest.mark.parametrize("why", ["stopped", "paused", "requests"])
def test_while_the_gate_refuses_the_review_offers_no_start_and_a_start_posted_anyway_sends_nothing(live, why):
    provider = Answering(usage=UNPRICED if why == "paused" else USED)
    app, browser = live(provider, **({"max_requests": 1} if why == "requests" else {}))
    item = material(browser)

    if why == "stopped":
        app.state.directions.live.gate.stop_file.touch()
    else:
        # One exploration first: it uses up the only request, or leaves a cost to be checked that pauses the approval.
        assert start(browser, task_of(review(browser, [item]))).status_code == 303

    before = (len(provider.calls), rows(app))
    task = task_of(review(browser, [item]))
    page = browser.get(task).text
    posted = start(browser, task)

    assert presentation.REFUSALS[why] in text_of(page) and START_BUTTON not in page
    assert posted.status_code == 409 and presentation.REFUSALS[why] in text_of(posted.text)
    assert (len(provider.calls), rows(app)) == before


def test_a_budget_too_small_for_one_reservation_sends_nothing_and_says_why(live):
    provider = Answering()
    app, browser = live(provider, max_cost_micro_usd=RESERVE - 1)
    task = task_of(review(browser, [material(browser)]))
    assert start(browser, task).status_code == 303
    text = page_text(browser, task)

    assert presentation.REJECTED["gate_cost"] in text and sent() in text and directions_shown(browser.get(task).text) == []
    assert provider.calls == [] and ledger(app) == []


def test_the_directions_and_the_analyses_share_one_gate_and_ledger_and_keep_each_other_waiting(live):
    hold, arrived, release = held()
    provider = Answering(hold)
    app, browser = live(provider, purposes=("analysis", "direction"), inline=False)
    jobs, directions = app.state.job_analysis, app.state.directions
    jobs.executor = InlineExecutor()
    item = material(browser)

    assert directions.live is jobs.live
    # An exploration's request is out: an analysis meanwhile is refused by the gate, with nothing sent.
    task = task_of(review(browser, [item]))
    assert start(browser, task).status_code == 303 and arrived.wait(10)
    job = job_task_of(job_review(browser, [item]))
    assert job_start(browser, job).status_code == 303
    assert "另一个真实分析正在进行" in page_text(browser, job) and len(provider.calls) == 1
    release.set()
    wait_until(lambda: "方向探索结果" in browser.get(task).text)

    # And the other way round: an analysis's request is out, an exploration is refused.
    hold, arrived, release = held()
    provider.script = [hold]
    jobs.executor = ThreadPoolExecutor(max_workers=1)
    directions.executor = InlineExecutor()
    job = job_task_of(job_review(browser, [item]))
    assert job_start(browser, job).status_code == 303 and arrived.wait(10)
    refused = task_of(review(browser, [item]))
    assert start(browser, refused).status_code == 303
    assert presentation.REJECTED["gate_busy"] in page_text(browser, refused) and len(provider.calls) == 2
    release.set()
    wait_until(lambda: len(ledger(app)) == 3 and all(row.state == LiveRequestState.SETTLED for row in ledger(app)))
    jobs.executor.shutdown(wait=True)

    # One ledger, one approval: the exploration's request and the analysis's two.
    assert sorted(row.purpose for row in ledger(app)) == ["analysis", "analysis", "direction"]
    assert {row.approval_id for row in ledger(app)} == {"synthetic-approval-1"}


# --- stops before and after the request went out ------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["cancel", "time_limit", "logout"])
def test_a_stop_before_the_request_is_handed_on_sends_nothing_and_releases_its_reservation(live, monkeypatch, how):
    provider = Answering()
    app, browser = live(provider)
    ticks = Ticks()
    store = app.state.directions.store = ExplorationTaskStore(ticks)
    gate = app.state.directions.live.gate
    task = task_of(review(browser, [material(browser)]))
    reserve = gate._reserve

    def reserve_then_stop(*args):
        reserved = reserve(*args)
        # The stop comes after the request is reserved and before it is handed on.
        stop(how, store, ticks, task)

        return reserved

    monkeypatch.setattr(gate, "_reserve", reserve_then_stop)
    assert start(browser, task).status_code == 303

    assert provider.calls == [] and rows(app) == [(LiveRequestState.RELEASED, 0)]

    if how == "logout":
        assert len(store) == 0
    else:
        text = page_text(browser, task)
        assert "探索已取消" in text and sent() in text and "方向探索结果" not in text


@pytest.mark.parametrize("how", ["cancel", "time_limit", "logout"])
def test_a_stop_after_the_request_went_out_settles_it_and_drops_its_answer(live, how):
    hold, arrived, release = held()
    provider = Answering(hold)
    app, browser = live(provider, inline=False)
    ticks = Ticks()
    store = app.state.directions.store = ExplorationTaskStore(ticks)
    task = task_of(review(browser, [material(browser)]))
    assert start(browser, task).status_code == 303 and arrived.wait(10)
    stop(how, store, ticks, task)

    if how != "logout":
        # While its request is out the task already says it was handed on: it may have gone out.
        assert browser.get(f"{task}/status").json()["sends"] == sends_text(Sends(generation_maybe=1))

    release.set()
    wait_until(lambda: rows(app) == [(LiveRequestState.SETTLED, COST)])
    app.state.directions.executor.shutdown(wait=True)

    assert len(provider.calls) == 1

    if how == "logout":
        assert len(store) == 0
    else:
        page = browser.get(task).text
        assert "探索已取消" in text_of(page) and sent(1) in text_of(page) and directions_shown(page) == []
        assert "费用照常核算" in text_of(page)


def test_at_shutdown_explorations_stop_first_the_request_out_is_settled_and_nothing_more_goes_out(make_config, clock):
    hold, arrived, release = held()
    provider = Answering(hold)
    setup = LiveSetup(approval(), FIXED_RESERVE_V1, lambda: provider)
    application = create_app(make_config(), clock=clock, live=setup)
    client = TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000))
    client.__enter__()
    directions, live = application.state.directions, application.state.job_analysis.live
    order, submitted = [], []
    stop_directions, close_gate = directions.shutdown, live.gate.close

    def directions_shutdown():
        order.append("directions")
        stop_directions()

    def gate_close():
        order.append("gate")
        close_gate()

    directions.shutdown = directions_shutdown
    live.gate.close = gate_close
    # One worker: a second user's exploration waits in its queue while the first one's request is out.
    executor = directions.executor = ThreadPoolExecutor(max_workers=1)
    submit = executor.submit
    executor.submit = lambda *args: submitted.append(submit(*args)) or submitted[-1]
    browser_a, browser_b = Browser.signed_in(application, "合成用户 A"), Browser.signed_in(application, "合成用户 B")
    first = task_of(review(browser_a, [material(browser_a)]))
    assert start(browser_a, first).status_code == 303 and arrived.wait(10)
    queued = task_of(review(browser_b))
    assert start(browser_b, queued).status_code == 303

    stopping = threading.Thread(target=client.__exit__, args=(None, None, None))
    stopping.start()
    # The gate waits for the request still out before it gives up its lock.
    wait_until(lambda: order == ["directions", "gate"])
    assert stopping.is_alive()
    release.set()
    stopping.join(10)

    assert not stopping.is_alive() and order == ["directions", "gate"]
    assert submitted[1].cancelled() and len(provider.calls) == 1
    assert rows(application) == [(LiveRequestState.SETTLED, COST)]


# --- costs the ledger could not settle, and a ledger that failed ------------------------------------------------------


@pytest.mark.parametrize(
    "usage, says, figures",
    [
        (UNPRICED, "当前批准费用待核实，后续调用已暂停", "费用待核实 $0.32"),
        (ABOVE, "当前批准超出预留，后续调用已暂停", "按用量核算 $0.33"),
    ],
    ids=["unknown", "above_reservation"],
)
def test_a_cost_the_ledger_cannot_settle_keeps_the_checked_result_and_pauses_the_approval(live, usage, says, figures):
    provider = Answering(usage=usage)
    app, browser = live(provider)
    item = material(browser)
    task = task_of(review(browser, [item]))
    assert start(browser, task).status_code == 303
    page = browser.get(task).text

    # The answer passed every check of its own: it is shown and may be saved. Its cost is the approval's concern.
    assert directions_shown(page) and "保存这次探索" in page
    assert says in text_of(page) and figures in text_of(page) and "不是这一次探索单独的费用" in text_of(page)

    later = task_of(review(browser, [item]))
    assert START_BUTTON not in browser.get(later).text and start(browser, later).status_code == 409
    assert len(provider.calls) == 1 and len(ledger(app)) == 1


@pytest.mark.parametrize("at", ["reserve", "settle", "status"])
def test_a_ledger_that_cannot_be_written_or_read_leaves_no_result_to_save_and_still_counts_what_went_out(live, monkeypatch, at):
    provider = Answering()
    app, browser = live(provider)
    gate = app.state.directions.live.gate
    task = task_of(review(browser, [material(browser)]))

    def fail(*args, **kwargs):
        raise StorageError("database operation failed")

    if at == "reserve":
        monkeypatch.setattr(gate, "_reserve", fail)
    elif at == "settle":
        settle = gate.settle
        # Only the settling of the answer fails; releasing a reservation would still work.
        monkeypatch.setattr(gate, "settle", lambda request_id, **kwargs: fail() if kwargs["sent"] is SendState.ANSWERED else settle(request_id, **kwargs))
    else:
        monkeypatch.setattr(gate, "status", fail)
        page = browser.get(task).text
        posted = start(browser, task)

        assert presentation.REFUSALS["ledger"] in text_of(page) and START_BUTTON not in page
        assert posted.status_code == 409 and provider.calls == [] and ledger(app) == []

        return

    assert start(browser, task).status_code == 303
    page = browser.get(task).text

    assert presentation.FAILED["ledger_failed"] in text_of(page) and directions_shown(page) == [] and "保存这次探索" not in page
    # A request that was answered counts as sent, whatever its ledger said; one never handed on does not.
    assert sent(1 if at == "settle" else 0) in text_of(page) and len(provider.calls) == (1 if at == "settle" else 0)


# --- the pages' other boundaries, unchanged ---------------------------------------------------------------------------


def test_another_user_or_a_forged_form_starts_nothing(live):
    provider = Answering()
    app, browser = live(provider)
    task = task_of(review(browser, [material(browser)]))
    other = Browser.signed_in(app, "合成用户 B")

    assert other.post(f"{task}/confirm", {"csrf_token": other.csrf_token(), "action": "start"}).status_code == 404
    assert browser.post(f"{task}/confirm", {"csrf_token": "forged", "action": "start"}).status_code == 403
    assert provider.calls == [] and ledger(app) == []


def test_a_live_result_whose_material_changed_or_went_cannot_be_saved(live):
    app, browser = live()
    item = material(browser)
    task = task_of(review(browser, [item]))
    assert start(browser, task).status_code == 303
    edit(browser, item, actions="结果出来之后改过。")
    changed = browser.get(task).text

    assert presentation.CHANGED_SINCE in text_of(changed) and "保存这次探索" not in changed
    delete(browser, item)
    gone = browser.get(task).text
    assert presentation.WITHDRAWN in text_of(gone) and directions_shown(gone) == []


def test_saved_simulated_history_and_new_live_results_each_keep_their_own_service(make_config, clock):
    config = make_config()

    with TestClient(create_app(config, clock=clock), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        simulated_app = client.app
        simulated_app.state.directions.executor = InlineExecutor()
        browser = Browser.signed_in(simulated_app, "合成用户 A")
        item = material(browser)
        task = task_of(review(browser, [item]))
        assert start(browser, task).status_code == 303
        simulated = saved_page(browser, task)

    provider = Answering()
    setup = LiveSetup(approval(), FIXED_RESERVE_V1, lambda: provider)

    with TestClient(create_app(config, clock=clock, live=setup), base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        live_app = client.app
        live_app.state.directions.executor = InlineExecutor()
        browser = Browser.signed_in(live_app, "合成用户 A")
        task = task_of(review(browser, [item]))
        assert start(browser, task).status_code == 303
        real = saved_page(browser, task)
        old, new = page_text(browser, simulated), page_text(browser, real)

    # Each saved exploration says the service of its own snapshot, whatever the app runs now.
    assert "本机模拟服务（固定规则，不是 AI）；提示词版本 3，处理说明版本 direction-1" in old and "openai" not in old
    assert "openai 的 gpt-5.6-sol；提示词版本 3，处理说明版本 direction-live-2" in new
    assert len(provider.calls) == 1


def test_the_live_notice_is_its_own_version_and_describes_one_service_only():
    # Not the analysis's notice, which speaks of two requests; and only for the service whose handling it describes.
    words = " ".join(text for _, text in presentation.LIVE_NOTICE)

    assert presentation.LIVE_NOTICE_VERSION == "direction-live-2" and "两个请求" not in words and "一个请求" in words
    assert presentation.LIVE_NOTICE_COVERS == {"openai": "https://api.openai.com/v1"} and "OpenAI" in words
