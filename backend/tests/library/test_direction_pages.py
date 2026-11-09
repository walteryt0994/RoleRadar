"""U3-C3a: 方向探索 through the pages, on the local simulation only.

From choosing what to send to comparing the directions: the review, the run, cancelling, the time limits and the
result; what the user saves and decides, and nothing else; the records, the recycle bin and the export. Also what
the review of the C3 plan asked for (C precheck, 17.15): every view checks the sources of a result again, a complete
one and a stale one alike, and a result whose sources were deleted is withdrawn for good; every write checks the
library again in its own transaction, whatever the page showed; a decision resting on an exploration becomes the
user's own choice only when they confirm it. All material is synthetic and nothing leaves the process; building a
real service's client fails the test.
"""
import io
import re
import zipfile

import openai
import pytest
from sqlalchemy import func, select

from app.analysis_contracts import SIMULATION_PROVIDER
from app.directions.actions import EXPLORATION_MOVED, GONE_FOR_DECIDING, NEEDS_OWN_CHOICE, RESULT_MOVED
from app.directions.presentation import SAVED_GONE, STALE, WITHDRAWN
from app.directions.records import SAVED_AND_PURGED, SAVED_IN_BIN
from app.directions.tasks import MAX_RUN_SECONDS, TASK_TTL_SECONDS, Claim, ExplorationTaskStore
from app.directions.web import WRITING
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.library.models import DirectionDecision, DirectionExploration, DirectionSource, ExperienceItem
from app.library.sends import Sends, sends_text
from job_helpers import Gate, InlineExecutor, confirmed, text_of
from library_helpers import forms_in

BASE = "/directions"
PREFS = dict(directions_ideal="数据分析或者用户研究", directions_no="销售", work_content_ideal="希望多分析数据", cities_ideal="北京")
PROJECT = dict(title="校园订单看板（合成）", actions="用 Python 清洗订单数据，并用 SQL 汇总每周销量。", outcome="看板在课程展示中使用。")
SCORES = ("分数", "排名", "最适合", "匹配度", "%")


class Ticks:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    """Building a real service's client fails the test: C3a runs on the local simulation only."""

    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


@pytest.fixture
def directions(app):
    return app.state.directions


@pytest.fixture
def inline(directions):
    directions.executor = InlineExecutor()

    return directions


@pytest.fixture
def ticks(directions):
    ticks = Ticks()
    directions.store = ExplorationTaskStore(ticks)

    return ticks


@pytest.fixture
def library(browser_a):
    """User A's preferences and one confirmed project, entered through the pages; the project's id."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFS).status_code == 303

    return confirmed(browser_a, **PROJECT)


# --- helpers ---------------------------------------------------------------------------------------------------------


def offered(browser) -> dict[str, str]:
    page = browser.get(BASE).text

    return {value.split(":")[0]: value for value in re.findall(r'name="source" value="([^"]+)"', page)}


def review(browser, items=(), *, skip_preferences=False):
    values = offered(browser) if items else {}
    data = {"csrf_token": browser.csrf_token(), "source": [values[item] for item in items]}

    if skip_preferences:
        data["skip_preferences"] = "yes"

    return browser.post(f"{BASE}/review", data)


def task_of(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{BASE}/tasks/"), location

    return location.split("?")[0]


def start(browser, task, *, sends=None, action="start", fingerprint=None):
    page = browser.get(task).text
    form = next(form for form in forms_in(page) if form.action == f"{task}/confirm")
    ticked = re.findall(r'name="send" value="([^"]+)" checked', page)
    data = {
        "csrf_token": form.fields["csrf_token"],
        "fingerprint": form.fields["fingerprint"] if fingerprint is None else fingerprint,
        "action": action,
        "send": ticked if sends is None else sends,
    }

    return browser.post(f"{task}/confirm", data)


def explored(browser, items=(), **choices) -> str:
    """Review and start an exploration; the address of its page (the inline executor has run it)."""
    task = task_of(review(browser, items, **choices))
    response = start(browser, task)
    assert response.status_code == 303 and response.headers["location"] == task, response.text

    return task


def kept(app) -> tuple[int, int, int]:
    with app.state.library.database.transaction() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(table)) for table in (DirectionExploration, DirectionDecision, DirectionSource)
        )


def rows(app, model):
    with app.state.library.database.transaction() as db:
        return [
            {column: getattr(row, column) for column in ("id", "name", "state", "basis", "exploration_id", "deleted_at", "revision") if hasattr(row, column)}
            for row in db.scalars(select(model).order_by(model.id))
        ]


def main_of(page: str) -> str:
    start_at = page.index('<main id="main"')

    return page[start_at: page.index("</main>", start_at)]


def decision_form(page: str, name: str):
    return next(form for form in forms_in(page) if form.fields.get("name") == name and form.action.endswith("/decide"))


def decide(browser, page_url: str, name: str, state: str):
    form = decision_form(browser.get(page_url).text, name)

    return browser.post(form.action, {**form.fields, "state": state})


def directions_shown(page: str) -> list[str]:
    return re.findall(r'<h2 id="direction-\d+-name" class="direction-name">([^<]+)</h2>', page)


def edit(browser, item: str, **changes) -> None:
    assert browser.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", **changes).status_code == 303
    assert browser.submit(f"/experiences/{item}", f"/experiences/{item}/confirm").status_code == 303


def delete(browser, item: str) -> None:
    assert browser.submit(f"/experiences/{item}/delete", f"/experiences/{item}/delete").status_code == 303


def restore(browser, item: str) -> None:
    assert browser.submit("/experiences?tab=trash", f"/experiences/{item}/restore").status_code == 303


def saved_id(app) -> str:
    [row] = rows(app, DirectionExploration)

    return row["id"]


def use_provider(app, provider):
    app.state.directions.make_provider = lambda task: provider

    return provider


def finish(app) -> None:
    app.state.directions.executor.shutdown(wait=True)


# --- where it is ------------------------------------------------------------------------------------------------------


def test_direction_exploration_sits_between_my_material_and_job_opportunities(browser_a):
    def top(page):
        return re.findall(r'<a href="([^"]+)"( aria-current="true")?>([^<]+)</a>', page[page.index('aria-label="主要导航"'):page.index("</nav>")])

    def sub(page):
        return re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', page[page.index('<nav class="subnav"'):])[:2]

    explore, records = browser_a.get(BASE).text, browser_a.get(f"{BASE}/records").text

    assert [name for _, _, name in top(explore)] == ["我的资料", "方向探索", "岗位机会"]
    assert top(explore)[1] == ("/directions", ' aria-current="true"', "方向探索")
    assert sub(explore) == [("/directions", ' aria-current="page"', "探索方向"), ("/directions/records", "", "探索记录")]
    assert sub(records) == [("/directions", "", "探索方向"), ("/directions/records", ' aria-current="page"', "探索记录")]
    # Since U3-C3b, 找岗位 has an entry for an unsure direction; the hand-off shows only once a direction is kept
    # (test_direction_handoff.py), so a user with no decision sees none.
    assert "用这些方向找岗位" not in explore + records
    assert '<a href="/directions">方向还不确定？先去探索方向</a>' in browser_a.get("/discover").text


# --- choose, review, run, compare --------------------------------------------------------------------------------------


def test_choose_review_run_and_compare_on_the_local_simulation(app, inline, browser_a, library):
    page = browser_a.get(BASE).text

    assert "校园订单看板（合成）" in page and "已填写：行业兴趣与意愿、理想工作内容和方式、现实条件" in page
    assert "使用本机模拟服务" in page

    task = task_of(review(browser_a, [library]))
    review_page = text_of(browser_a.get(task).text)

    # The review: who gets it, what goes out field by field, the three groups, and what never goes out.
    assert "本机模拟服务：RoleRadar 内置的固定规则，不是 AI" in review_page and "只临时保留 30 分钟" in review_page
    for group in ("行业兴趣与意愿", "理想工作内容和方式", "现实条件"):
        assert group in review_page
    assert "尚未填写：" in review_page and "以前的探索和方向决定" in review_page
    assert kept(app) == (0, 0, 0)

    assert start(browser_a, task).headers["location"] == task
    result = browser_a.get(task).text
    shown = directions_shown(result)

    assert 3 <= len(shown) <= 5 and shown[0] == "数据分析"
    assert "用 Python 清洗订单数据，并用 SQL 汇总每周销量" in result and "校园订单看板（合成） · " in result
    assert "下面都是一般性介绍，未经岗位来源核验；没有分数，也不排名。" in result
    # Beside the one line saying there are none, no score, rank or "best fit" anywhere.
    assert not any(word in text_of(main_of(result)).replace("没有分数，也不排名", "") for word in SCORES)
    assert "保存这次探索" in result and "之后清除" in result
    assert sends_text(Sends(generation_sent=1)) in result
    assert browser_a.get(f"{task}/status").json() == {"stage": "done", "step": None, "sends": sends_text(Sends(generation_sent=1))}
    # Nothing is saved by running.
    assert kept(app) == (0, 0, 0)


def test_without_experiences_or_preferences_an_exploration_is_general(app, inline, browser_b):
    page = browser_b.get(BASE).text

    assert "还没有已确认的经历。不选经历也可以探索" in page and "还没有填写求职偏好；不填也可以探索。" in page

    task = task_of(review(browser_b))
    assert "这次没有选择经历：结果只是一般性介绍，没有个人依据。" in browser_b.get(task).text
    assert start(browser_b, task).status_code == 303
    result = browser_b.get(task).text

    assert "这次发出 0 段经历、0 条偏好，没有经历依据" in result
    assert result.count("暂无经历依据，不代表不适合。") == len(directions_shown(result)) >= 3


def test_leaving_the_preferences_out_sends_none_of_them(inline, browser_a, library):
    task = task_of(review(browser_a, [library], skip_preferences=True))
    page = browser_a.get(task).text

    assert re.findall(r'name="send" value="preferences:[^"]+" checked', page) == []
    assert 'value="preferences:directions_ideal"' in page
    assert start(browser_a, task).status_code == 303
    assert "这次发出 1 段经历、0 条偏好" in browser_a.get(task).text


@pytest.mark.parametrize("action", ["update", "start"])
def test_unticking_a_field_needs_a_new_review_before_anything_goes_out(app, inline, browser_a, library, action):
    task = task_of(review(browser_a, [library]))
    response = start(browser_a, task, sends=[f"{library}:actions"], action=action)
    newer = task_of(response)
    notice = "scope_changed" if action == "start" else "scope_updated"

    assert newer != task and response.headers["location"] == f"{newer}?notice={notice}"
    assert browser_a.get(task).status_code == 404
    newer_page = browser_a.get(newer).text

    # Still a review: nothing went out.
    assert re.findall(r'name="send" value="([^"]+)" checked', newer_page) == [f"{library}:actions"]
    assert "确认并开始探索" in newer_page and kept(app) == (0, 0, 0)


def test_a_forged_or_old_fingerprint_or_a_second_start_runs_nothing_more(inline, browser_a, library):
    task = task_of(review(browser_a, [library]))
    forged = start(browser_a, task, fingerprint="0" * 64)

    assert forged.status_code == 409 and "来自旧的核对页面" in forged.text

    page = browser_a.get(task).text
    form = next(form for form in forms_in(page) if form.action == f"{task}/confirm")
    data = {"csrf_token": form.fields["csrf_token"], "fingerprint": form.fields["fingerprint"], "action": "start",
            "send": re.findall(r'name="send" value="([^"]+)" checked', page)}
    first, again = browser_a.post(f"{task}/confirm", data), browser_a.post(f"{task}/confirm", data)

    assert first.headers["location"] == task and again.headers["location"] == f"{task}?notice=already"
    assert "这次确认已经开始过，没有重复执行。" in browser_a.get(again.headers["location"]).text


def test_a_task_is_one_users_and_one_sessions(app, inline, browser_a, browser_b, library):
    task = explored(browser_a, [library])
    token = browser_b.csrf_token()

    assert browser_b.get(task).status_code == 404 and browser_b.get(f"{task}/status").status_code == 404
    for action in ("save", "cancel", "again"):
        assert browser_b.post(f"{task}/{action}", {"csrf_token": token}).status_code == 404
    assert browser_b.post(f"{task}/decide", {"csrf_token": token, "name": "数据分析", "state": "keep"}).status_code == 404
    # Without the session's token nothing is done, even by the owner.
    assert browser_a.post(f"{task}/save", {"csrf_token": "forged"}).status_code == 403
    assert kept(app) == (0, 0, 0)


def test_one_exploration_of_a_session_runs_at_a_time(app, browser_a, library):
    gate = use_provider(app, Gate(SimulatedProvider()))
    first = task_of(review(browser_a, [library]))
    assert start(browser_a, first).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    second = task_of(review(browser_a, [library]))
    busy = start(browser_a, second)
    gate.release.release()
    finish(app)

    assert busy.status_code == 409 and "已经有一个探索正在进行" in busy.text
    assert len(gate.prompts) == 1


# --- cancelling, failing, time limits --------------------------------------------------------------------------------


def test_cancelling_before_the_start_sends_nothing(app, browser_a, library):
    task = task_of(review(browser_a, [library]))
    review_page = browser_a.get(task).text
    form = next(form for form in forms_in(review_page) if form.action == f"{task}/confirm")
    data = {**form.fields, "action": "start", "send": re.findall(r'name="send" value="([^"]+)" checked', review_page)}
    response = browser_a.post(f"{task}/cancel", {"csrf_token": browser_a.csrf_token()})
    page = browser_a.get(task).text
    # The confirmation the review page held starts nothing once the task was cancelled.
    late = browser_a.post(f"{task}/confirm", data)

    assert response.headers["location"] == task
    assert "探索已取消" in page and "你取消了这次探索。" in page and sends_text(Sends()) in page
    assert late.headers["location"] == task and "探索已取消" in browser_a.get(task).text
    # Reviewing again reads the library again, as a new task to confirm.
    again = task_of(browser_a.post(f"{task}/again", {"csrf_token": browser_a.csrf_token()}))
    assert again != task and "确认并开始探索" in browser_a.get(again).text
    assert kept(app) == (0, 0, 0)


def test_cancelling_while_the_request_is_out_takes_no_answer(app, browser_a, library):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [library]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    running = browser_a.get(task).text
    status = browser_a.get(f"{task}/status").json()
    assert browser_a.post(f"{task}/cancel", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    gate.release.release()
    finish(app)
    page = browser_a.get(task).text

    assert "data-task-running" in running and "取消探索" in running and '<script src="/static/job_analysis.js"></script>' in running
    # Still out: only maybe sent.
    assert status == {"stage": "running", "step": "探索方向", "sends": sends_text(Sends(generation_maybe=1))}
    assert "探索已取消" in page and sends_text(Sends(generation_sent=1)) in page and "它的回答不会采用" in page
    assert directions_shown(page) == [] and browser_a.get(f"{task}/status").json()["stage"] == "cancelled"
    assert len(gate.prompts) == 1 and kept(app) == (0, 0, 0)


def test_an_exploration_past_its_run_time_is_stopped_and_its_late_answer_dropped(app, browser_a, library, ticks):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [library]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    ticks.now += MAX_RUN_SECONDS + 1
    status = browser_a.get(f"{task}/status").json()
    gate.release.release()
    finish(app)
    page = browser_a.get(task).text

    assert status["stage"] == "cancelled"
    assert "这次探索超过了运行时限，已停止。" in page and directions_shown(page) == []


def test_logging_out_cancels_and_forgets_the_sessions_explorations(app, browser_a, library):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [library]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    assert browser_a.post("/logout", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    gate.release.release()
    finish(app)
    browser_a.sign_in("合成用户 A")

    assert len(app.state.directions.store) == 0 and browser_a.get(task).status_code == 404
    assert kept(app) == (0, 0, 0)


def test_an_unsaved_result_is_kept_thirty_minutes_after_it_came_and_looking_does_not_extend_it(app, inline, browser_a, library, ticks):
    task = explored(browser_a, [library])
    ticks.now += 20 * 60
    assert browser_a.get(task).status_code == 200
    ticks.now += 11 * 60
    gone = browser_a.get(task)

    assert gone.status_code == 404 and "已经过期（临时结果保留 30 分钟）" in gone.text
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 404
    assert kept(app) == (0, 0, 0) and TASK_TTL_SECONDS == 30 * 60


def test_a_failed_request_says_why_and_is_never_tried_again(app, inline, browser_a, library):
    from app.ai_provider import AIProviderTimeoutError
    from job_helpers import FailingProvider

    failing = use_provider(app, FailingProvider(AIProviderTimeoutError("synthetic")))
    task = explored(browser_a, [library])
    page = browser_a.get(task).text

    assert "服务没有在限定时间内回答。" in page and directions_shown(page) == [] and "保存这次探索" not in page
    assert len(failing.prompts) == 1 and kept(app) == (0, 0, 0)


# --- every view and every write checks the sources again ---------------------------------------------------------------


def preferences_id(app) -> str:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(ExperienceItem.id).where(ExperienceItem.category == "preferences", ExperienceItem.deleted_at.is_(None)))


def test_a_result_whose_material_was_edited_since_cannot_be_saved_or_decided_on(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    opened = browser_a.get(task).text
    form = decision_form(opened, "数据分析")
    edit(browser_a, library, actions="改成了别的说法。")
    page = browser_a.get(task).text
    saved = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    decided = browser_a.post(form.action, {**form.fields, "state": "keep"})

    assert "保存这次探索" in opened and "这次结果用到的资料之后改过" in page
    assert "保存这次探索" not in page and not re.search(r'name="state" value="keep"', page)
    assert saved.status_code == 409 and RESULT_MOVED in text_of(saved.text)
    assert decided.status_code == 409 and RESULT_MOVED in text_of(decided.text)
    assert kept(app) == (0, 0, 0)


@pytest.mark.parametrize("source", ["experience", "preferences"])
def test_a_result_whose_material_was_deleted_is_withdrawn_for_good(app, inline, browser_a, library, source):
    task = explored(browser_a, [library])
    item = library if source == "experience" else preferences_id(app)
    delete(browser_a, item)
    withdrawn = browser_a.get(task).text
    restore(browser_a, item)
    after_restoring = browser_a.get(task).text
    saved = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})

    for page in (withdrawn, after_restoring):
        assert WITHDRAWN in text_of(page) and directions_shown(page) == []
        assert "用 Python 清洗订单数据" not in page and "数据分析" not in text_of(main_of(page))
    assert saved.status_code == 409 and kept(app) == (0, 0, 0)


def test_a_stale_result_is_checked_on_every_view_and_never_becomes_savable(app, browser_a, library):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [library]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    # Edited while the request is out: the answer comes back stale (app.directions.explore).
    edit(browser_a, library, actions="请求期间改过。")
    gate.release.release()
    finish(app)
    stale = browser_a.get(task).text
    # Edited back: what it rests on reads as it did, yet a stale result has no trusted snapshot to save.
    edit(browser_a, library, actions=PROJECT["actions"])
    reverted = browser_a.get(task).text
    delete(browser_a, library)
    withdrawn = browser_a.get(task).text
    restore(browser_a, library)
    restored = browser_a.get(task).text

    for page in (stale, reverted):
        assert STALE in text_of(page) and directions_shown(page)
        assert "保存这次探索" not in page and not re.search(r'name="state" value="keep"', page)
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 409
    for page in (withdrawn, restored):
        assert WITHDRAWN in text_of(page) and directions_shown(page) == [] and "请求期间改过" not in page
    assert kept(app) == (0, 0, 0)


def test_deciding_on_a_saved_exploration_checks_its_sources_again(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    page_url = f"{BASE}/explorations/{saved_id(app)}"
    form = decision_form(browser_a.get(page_url).text, "数据分析")
    edit(browser_a, library, actions="保存之后改过。")
    decided = browser_a.post(form.action, {**form.fields, "state": "keep"})
    page = browser_a.get(page_url).text

    assert decided.status_code == 409 and EXPLORATION_MOVED in text_of(decided.text)
    assert "依据的资料之后改过：这是当时的结果，不能再根据它做决定。" in page and not re.search(r'name="state" value="keep"', page)
    assert rows(app, DirectionDecision) == []


def test_a_change_on_the_records_page_keeps_the_basis_and_is_checked_again_after_the_page_was_opened(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    [decision] = rows(app, DirectionDecision)
    state_change = f"{BASE}/decisions/{decision['id']}/state"
    # Resting on a saved exploration that stands as it was: changed as it is, still resting on it.
    assert browser_a.submit(f"{BASE}/records", state_change, state="defer").status_code == 303
    [deferred] = rows(app, DirectionDecision)
    records = browser_a.get(f"{BASE}/records").text
    form = next(form for form in forms_in(records) if form.action == state_change)
    edit(browser_a, library, actions="改过之后再改状态。")
    changed = browser_a.post(form.action, {**form.fields, "state": "exclude"})

    assert (deferred["state"], deferred["basis"], deferred["exploration_id"]) == ("defer", "saved_exploration", saved_id(app))
    assert "改为排除" in records
    assert changed.status_code == 409 and NEEDS_OWN_CHOICE in text_of(changed.text)
    assert rows(app, DirectionDecision)[0]["state"] == "defer"


# --- saving and deciding: the user's acts, each once --------------------------------------------------------------------


def test_saving_is_the_users_act_and_saving_again_returns_the_same_record(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert kept(app) == (0, 0, 0)
    first = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    again = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    page = browser_a.get(task).text

    assert first.headers["location"] == again.headers["location"] == f"{task}?notice=saved"
    assert kept(app)[:2] == (1, 0)
    assert f'<a href="{BASE}/explorations/{saved_id(app)}">查看保存的探索</a>' in page and "保存这次探索" not in page


def test_saving_again_neither_restores_a_deleted_exploration_nor_lets_a_moved_basis_through(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    exploration = saved_id(app)
    assert browser_a.submit(f"{BASE}/explorations/{exploration}/delete", f"{BASE}/explorations/{exploration}/delete").status_code == 303
    in_bin = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})

    assert in_bin.status_code == 409 and SAVED_IN_BIN in text_of(in_bin.text)
    assert rows(app, DirectionExploration)[0]["deleted_at"] is not None

    assert browser_a.submit("/experiences?tab=trash", f"{BASE}/explorations/{exploration}/restore").status_code == 303
    edit(browser_a, library, actions="保存之后改过。")
    moved = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})

    # A record saved before is no sign that what the result rests on still stands.
    assert moved.status_code == 409 and RESULT_MOVED in text_of(moved.text)


def test_a_decision_rests_on_the_unsaved_result_then_on_the_exploration_saved_from_it(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert decide(browser_a, task, "数据分析", "keep").headers["location"] == f"{task}?notice=decided"
    [unsaved] = rows(app, DirectionDecision)
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "defer").status_code == 303
    [saved] = rows(app, DirectionDecision)
    page = browser_a.get(task).text

    assert (unsaved["state"], unsaved["basis"], unsaved["exploration_id"]) == ("keep", "unsaved_exploration", None)
    assert (saved["id"], saved["state"], saved["basis"], saved["exploration_id"]) == (unsaved["id"], "defer", "saved_exploration", saved_id(app))
    assert "你的决定：暂缓" in page


def test_of_two_tabs_changing_one_decision_the_later_is_refused(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    undecided = decision_form(browser_a.get(task).text, "数据分析")
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    # A tab that saw no decision, then two tabs that saw the same one.
    late = browser_a.post(undecided.action, {**undecided.fields, "state": "exclude"})
    one, two = (decision_form(browser_a.get(task).text, "数据分析") for _ in range(2))
    first = browser_a.post(one.action, {**one.fields, "state": "defer"})
    second = browser_a.post(two.action, {**two.fields, "state": "exclude"})

    assert late.status_code == 409 and "在别处改过" in late.text
    assert first.status_code == 303 and second.status_code == 409 and "在别处改过" in second.text
    assert rows(app, DirectionDecision)[0]["state"] == "defer"


def test_undoing_a_decision_sends_it_to_the_bin_and_it_comes_back_unless_a_newer_one_took_its_name(app, inline, browser_a, library):
    from app.library.lifecycle import SAME_NAME_REASON

    task = explored(browser_a, [library])
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    [decision] = rows(app, DirectionDecision)
    undo = f"{BASE}/decisions/{decision['id']}/undo"
    assert browser_a.submit(task, undo).headers["location"] == f"{task}?notice=undone"
    bin_page = text_of(browser_a.get("/experiences?tab=trash").text)

    assert "你的决定：未决定" in browser_a.get(task).text
    assert "删除的方向探索和决定" in bin_page and "数据分析 方向决定：保留" in bin_page

    assert browser_a.submit("/experiences?tab=trash", f"{BASE}/decisions/{decision['id']}/restore").headers["location"] == "/experiences?tab=trash"
    assert rows(app, DirectionDecision)[0]["deleted_at"] is None
    assert browser_a.submit(task, undo).status_code == 303
    assert decide(browser_a, task, "数据分析", "exclude").status_code == 303
    refused = browser_a.submit("/experiences?tab=trash", f"{BASE}/decisions/{decision['id']}/restore")

    assert refused.status_code == 409 and SAME_NAME_REASON in text_of(refused.text)

    purge = f"{BASE}/decisions/{decision['id']}/purge"
    assert "这条决定会被永久删除，不能恢复。" in browser_a.get(purge).text
    assert browser_a.submit(purge, purge).headers["location"] == "/experiences?tab=trash"
    assert "一条方向决定：你已于" in text_of(browser_a.get("/experiences?tab=trash").text)


def test_excluding_or_keeping_a_direction_never_writes_the_preferences(app, inline, browser_a, library):
    def preferences():
        with app.state.library.database.transaction() as db:
            item = db.get(ExperienceItem, preferences_id(app))

            return dict(item.content), item.revision

    before = preferences()
    task = explored(browser_a, [library])
    assert decide(browser_a, task, "数据分析", "exclude").status_code == 303
    assert decide(browser_a, task, "用户研究", "keep").status_code == 303

    assert preferences() == before


def test_a_direction_excluded_before_comes_last_and_says_so(app, inline, browser_a, library):
    first = explored(browser_a, [library])
    assert directions_shown(browser_a.get(first).text)[0] == "数据分析"
    assert decide(browser_a, first, "数据分析", "exclude").status_code == 303
    page = browser_a.get(explored(browser_a, [library])).text

    assert directions_shown(page)[-1] == "数据分析" and "你之前排除过这个方向，可以随时改。" in page
    assert '数据分析</a><span class="value-note">排除</span>' in page


# --- the records: the basis a decision keeps ---------------------------------------------------------------------------


def test_only_a_confirmed_change_makes_a_decision_on_an_exploration_the_users_own_choice(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    [decision] = rows(app, DirectionDecision)
    with app.state.library.database.transaction() as db:
        history = db.scalar(select(func.count()).select_from(DirectionSource).where(DirectionSource.holder_id == decision["id"]))
    records = browser_a.get(f"{BASE}/records").text
    direct = browser_a.post(f"{BASE}/decisions/{decision['id']}/state", {"csrf_token": browser_a.csrf_token(), "expected_revision": "1", "state": "defer"})
    after_direct = rows(app, DirectionDecision)[0]["state"]
    ask = browser_a.get(f"{BASE}/decisions/{decision['id']}/own").text
    own = f"{BASE}/decisions/{decision['id']}/own"
    stale = browser_a.submit(own, own, expected_revision="0", state="defer")
    confirmed_change = browser_a.submit(own, own, state="defer")
    [mine] = rows(app, DirectionDecision)

    # Resting on a result that was not saved: no plain change is offered, nor taken.
    assert f'href="{own}">重新定为我的选择…</a>' in records and f'action="{BASE}/decisions/{decision["id"]}/state"' not in records
    assert direct.status_code == 409 and NEEDS_OWN_CHOICE in text_of(direct.text) and after_direct == "keep"
    assert "原来的依据留作历史，不会删除" in ask and "根据没有保存的方向探索" in ask
    assert stale.status_code == 409 and "这条记录刚刚变了" in stale.text
    assert confirmed_change.headers["location"] == f"{BASE}/records?notice=own"
    assert (mine["state"], mine["basis"]) == ("defer", "personal") and history > 0
    with app.state.library.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(DirectionSource).where(DirectionSource.holder_id == decision["id"])) == history

    # The user's own choice changes as it is, without asking again.
    assert browser_a.submit(f"{BASE}/records", f"{BASE}/decisions/{decision['id']}/state", state="keep").headers["location"] == f"{BASE}/records?notice=decided"
    assert (rows(app, DirectionDecision)[0]["state"], rows(app, DirectionDecision)[0]["basis"]) == ("keep", "personal")


def test_the_records_and_saved_explorations_are_each_users_own(app, inline, browser_a, browser_b, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    exploration, [decision] = saved_id(app), rows(app, DirectionDecision)
    token = browser_b.csrf_token()
    before = (rows(app, DirectionExploration), rows(app, DirectionDecision))

    assert "数据分析" not in text_of(main_of(browser_b.get(f"{BASE}/records").text))
    assert browser_b.get(f"{BASE}/explorations/{exploration}").status_code == 404
    assert browser_b.get(f"{BASE}/decisions/{decision['id']}/own").status_code == 404
    for action, data in (
        (f"{BASE}/explorations/{exploration}/decide", {"name": "数据分析", "state": "defer"}),
        (f"{BASE}/explorations/{exploration}/delete", {"expected_revision": "1"}),
        (f"{BASE}/decisions/{decision['id']}/state", {"expected_revision": "1", "state": "defer"}),
        (f"{BASE}/decisions/{decision['id']}/own", {"expected_revision": "1", "state": "defer"}),
        (f"{BASE}/decisions/{decision['id']}/undo", {"expected_revision": "1"}),
    ):
        assert browser_b.post(action, {"csrf_token": token, **data}).status_code == 404, action
    assert (rows(app, DirectionExploration), rows(app, DirectionDecision)) == before


# --- deleting, restoring, purging, exporting --------------------------------------------------------------------------


def test_a_saved_exploration_is_deleted_restored_and_purged_through_the_pages(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    exploration = saved_id(app)
    delete_page = f"{BASE}/explorations/{exploration}/delete"
    assert "根据它做的决定会保留" in browser_a.get(delete_page).text
    assert browser_a.submit(delete_page, delete_page).headers["location"] == f"{BASE}/records?notice=deleted"
    records = text_of(browser_a.get(f"{BASE}/records").text)
    bin_page = browser_a.get("/experiences?tab=trash").text

    assert "还没有保存的探索" in records and "依据的方向探索已删除（还能恢复）" in records and "重新定为我的选择…" in records
    assert "方向探索</span>" in bin_page and f"{BASE}/explorations/{exploration}/restore" in bin_page

    assert browser_a.submit("/experiences?tab=trash", f"{BASE}/explorations/{exploration}/restore").status_code == 303
    assert rows(app, DirectionExploration)[0]["deleted_at"] is None
    assert browser_a.submit(delete_page, delete_page).status_code == 303
    purge = f"{BASE}/explorations/{exploration}/purge"
    assert "永久删除，不能恢复" in browser_a.get(purge).text
    assert browser_a.submit(purge, purge).headers["location"] == "/experiences?tab=trash"
    waiting = text_of(browser_a.get("/experiences?tab=trash").text)
    app.state.library.run_cleanup()
    finished = text_of(browser_a.get("/experiences?tab=trash").text)

    assert "一次方向探索：你已于" in waiting and "正在等待清理" in waiting
    assert "一次方向探索（你申请的永久删除）" in finished and "清理完成" in finished
    assert rows(app, DirectionExploration) == [] and "依据的方向探索已永久删除" in text_of(browser_a.get(f"{BASE}/records").text)


def test_the_export_holds_what_the_pages_saved(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    records = browser_a.get(f"{BASE}/records").text
    package = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    [markdown] = [name for name in package.namelist() if name.endswith("directions.md")]
    text = package.read(markdown).decode("utf-8")

    assert '<a href="/export/all.zip" download>导出全部资料</a>' in records
    assert "数据分析" in text and "保留" in text


# --- the local simulation only ------------------------------------------------------------------------------------------


def test_only_the_local_simulation_answers_and_no_real_service_is_reachable(app, browser_a, library):
    import ast
    import inspect

    from app.directions import actions, runner, tasks, web

    directions = app.state.directions
    task = directions.store.create("owner", "session", None, None, None, "fingerprint")

    with directions.provider_for(task, runner._Sending(directions.store, task.id)) as provider:
        identity = provider.identity()

    assert directions.recipient == RECIPIENT and identity.provider == SIMULATION_PROVIDER
    # The pages, the runner, the store and the writes import no gate and no way to build a real service.
    for module in (web, runner, tasks, actions):
        imported = set()

        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.ImportFrom):
                imported |= {node.module} | {alias.name for alias in node.names}
            elif isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}

        assert not imported & {"app.live_gate", "app.job_analysis.live", "LiveService", "make_live_provider", "LiveSetup", "openai"}, module
    assert "本机模拟服务" in browser_a.get(task_of(review(browser_a, [library]))).text


def test_without_a_session_the_direction_pages_lead_to_login_and_posts_are_refused(app, browser_a, library):
    from library_helpers import all_routes
    from test_library_web import DIRECTION_ROUTES

    anonymous = type(browser_a)(app)
    paths = set(DIRECTION_ROUTES)

    assert paths == {
        (method, route.path)
        for route in all_routes(app)
        if getattr(route, "methods", None) and route.path.startswith(BASE)
        for method in route.methods - {"HEAD"}
    }
    for method, path in sorted(paths):
        filled = re.sub(r"\{[^}]+\}", "not-a-real-id", path)

        if method == "GET":
            response = anonymous.get(filled)
            assert (response.status_code, response.headers.get("location")) == (303, "/login"), path
        else:
            assert anonymous.post(filled, {"csrf_token": "guess", "expected_revision": "1", "state": "keep"}).status_code == 401, path
    assert kept(app) == (0, 0, 0)


def test_the_records_page_offers_a_plain_change_only_when_deciding_on_the_exploration_again_would_take_it(app, inline, browser_a, library):
    # Two experiences; 数据分析 quotes only the first. The second changing still changes the exploration it rests on.
    other = confirmed(browser_a, title="访谈记录（合成）", actions="和同学一起访谈了五位校园店主。")
    task = explored(browser_a, [library, other])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    [decision] = rows(app, DirectionDecision)
    before = browser_a.get(f"{BASE}/records").text
    edit(browser_a, other, actions="访谈之后又改过。")
    after = browser_a.get(f"{BASE}/records").text

    assert f'action="{BASE}/decisions/{decision["id"]}/state"' in before
    assert f'action="{BASE}/decisions/{decision["id"]}/state"' not in after and "重新定为我的选择…" in after
    assert "根据已保存的方向探索；依据的资料之后改过" in text_of(after)


# --- R1: a result saved and deleted since is never saved again, nor taken for one never saved ------------------------


def purge_saved(app, browser, task: str, *, cleaned: bool) -> str:
    """Save the task's result, delete it, ask to purge it, and (cleaned) run the clean-up; the exploration's id."""
    assert browser.post(f"{task}/save", {"csrf_token": browser.csrf_token()}).status_code == 303
    exploration = saved_id(app)
    delete_url, purge_url = f"{BASE}/explorations/{exploration}/delete", f"{BASE}/explorations/{exploration}/purge"
    assert browser.submit(delete_url, delete_url).status_code == 303
    assert browser.submit(purge_url, purge_url).status_code == 303

    if cleaned:
        app.state.library.run_cleanup()
        assert rows(app, DirectionExploration) == []

    return exploration


def actions_of(page: str) -> tuple[bool, bool]:
    """Whether a result page offers saving, and deciding."""
    return "保存这次探索</button>" in page, bool(re.search(r'name="state" value="keep"', page))


@pytest.mark.parametrize("cleaned", [False, True], ids=["waiting_for_cleanup", "cleaned_up"])
def test_an_old_save_never_brings_back_an_exploration_deleted_for_good(app, inline, browser_a, library, cleaned):
    task = explored(browser_a, [library])
    # The page an old tab still shows: saving offered.
    old = browser_a.get(task).text
    purge_saved(app, browser_a, task, cleaned=cleaned)
    response = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    page = browser_a.get(task).text

    assert actions_of(old) == (True, True)
    assert response.status_code == 409 and SAVED_AND_PURGED in text_of(response.text)
    assert [row["deleted_at"] is not None for row in rows(app, DirectionExploration)] == ([] if cleaned else [True])
    assert SAVED_GONE in text_of(page) and actions_of(page) == (False, False)


@pytest.mark.parametrize("cleaned", [False, True], ids=["waiting_for_cleanup", "cleaned_up"])
def test_an_old_decision_never_turns_an_exploration_deleted_for_good_into_an_unsaved_basis(app, inline, browser_a, library, cleaned):
    task = explored(browser_a, [library])
    # An old tab's form, taken before the result was saved: it would decide on the unsaved result.
    form = decision_form(browser_a.get(task).text, "数据分析")
    purge_saved(app, browser_a, task, cleaned=cleaned)
    response = browser_a.post(form.action, {**form.fields, "state": "keep"})

    assert response.status_code == 409 and GONE_FOR_DECIDING in text_of(response.text)
    assert rows(app, DirectionDecision) == []


def test_a_saved_exploration_deleted_and_restored_is_the_tasks_saved_exploration_again(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    exploration = saved_id(app)
    form = decision_form(browser_a.get(task).text, "数据分析")
    delete_url = f"{BASE}/explorations/{exploration}/delete"
    assert browser_a.submit(delete_url, delete_url).status_code == 303
    in_bin = browser_a.get(task).text
    saved_in_bin = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    decided_in_bin = browser_a.post(form.action, {**form.fields, "state": "keep"})

    assert "这次探索保存过，现在在" in text_of(in_bin) and actions_of(in_bin) == (False, False)
    assert saved_in_bin.status_code == 409 and SAVED_IN_BIN in text_of(saved_in_bin.text)
    assert decided_in_bin.status_code == 409 and SAVED_IN_BIN in text_of(decided_in_bin.text)
    assert rows(app, DirectionDecision) == []

    # Restored on purpose from the bin: the task's saved exploration again, decided on as such.
    assert browser_a.submit("/experiences?tab=trash", f"{BASE}/explorations/{exploration}/restore").status_code == 303
    restored = browser_a.get(task).text
    assert f'<a href="{BASE}/explorations/{exploration}">查看保存的探索</a>' in restored and actions_of(restored) == (False, True)
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    [decision], [kept_one] = rows(app, DirectionDecision), rows(app, DirectionExploration)

    assert (decision["basis"], decision["exploration_id"]) == ("saved_exploration", exploration) and kept_one["id"] == exploration


def test_reviewing_again_after_a_purge_gives_a_new_result_that_saves_as_a_new_exploration(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    purged = purge_saved(app, browser_a, task, cleaned=True)
    newer = task_of(browser_a.post(f"{task}/again", {"csrf_token": browser_a.csrf_token()}))
    assert start(browser_a, newer).status_code == 303
    fresh = browser_a.get(newer).text
    assert browser_a.post(f"{newer}/save", {"csrf_token": browser_a.csrf_token()}).headers["location"] == f"{newer}?notice=saved"
    assert decide(browser_a, newer, "数据分析", "keep").status_code == 303
    [exploration], [decision] = rows(app, DirectionExploration), rows(app, DirectionDecision)

    assert actions_of(fresh) == (True, True)
    assert exploration["id"] != purged and (decision["basis"], decision["exploration_id"]) == ("saved_exploration", exploration["id"])
    # The old task still says what became of its own result.
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 409
    assert len(rows(app, DirectionExploration)) == 1


def test_saving_and_deciding_on_one_task_are_one_write_at_a_time(app, inline, browser_a, library):
    task = explored(browser_a, [library])
    store = app.state.directions.store
    # A write of the task in progress, as a save between its commit and the task remembering it would be.
    held = next(iter(store._tasks.values()))
    claim, _ = store.claim_write(held.id, held.owner_id, held.session)
    form = decision_form(browser_a.get(task).text, "数据分析")
    busy_save = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    busy_decide = browser_a.post(form.action, {**form.fields, "state": "keep"})
    after_busy = kept(app)
    store.release_write(held.id)

    assert claim is Claim.CLAIMED
    assert busy_save.status_code == busy_decide.status_code == 409 and WRITING in text_of(busy_save.text) + text_of(busy_decide.text)
    assert after_busy == (0, 0, 0)
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303


def test_the_store_keeps_the_saved_exploration_and_lets_one_write_at_a_time():
    store = ExplorationTaskStore(Ticks())
    task = store.create("owner", "session", None, None, None, "fingerprint")

    first, _ = store.claim_write(task.id, "owner", "session")
    second, _ = store.claim_write(task.id, "owner", "session")
    elsewhere, _ = store.claim_write(task.id, "owner", "another session")
    store.release_write(task.id, "exploration-1")
    store.release_write(task.id, None)
    store.release_write(task.id, "exploration-2")
    kept_task = store.get(task.id, "owner", "session")

    assert (first, second, elsewhere) == (Claim.CLAIMED, Claim.BUSY, Claim.GONE)
    # Never cleared, never replaced, and no write left claimed.
    assert (kept_task.saved_id, kept_task.writing) == ("exploration-1", False)


# --- R2: a task past its time takes no write, whatever came first -------------------------------------------------------


def test_an_expired_task_refuses_a_direct_save_with_no_look_first(app, inline, browser_a, library, ticks):
    task = explored(browser_a, [library])
    payload = {"csrf_token": browser_a.csrf_token()}
    ticks.now += TASK_TTL_SECONDS + 1
    # No page, no sweep and no new review between the time running out and the old form's post.
    response = browser_a.post(f"{task}/save", payload)

    assert response.status_code == 404 and "已经过期（临时结果保留 30 分钟）" in response.text
    assert kept(app) == (0, 0, 0) and len(app.state.directions.store) == 0


def test_an_expired_task_refuses_a_direct_decision_with_no_look_first(app, inline, browser_a, library, ticks):
    task = explored(browser_a, [library])
    form = decision_form(browser_a.get(task).text, "数据分析")
    ticks.now += TASK_TTL_SECONDS + 1
    response = browser_a.post(form.action, {**form.fields, "state": "keep"})

    assert response.status_code == 404 and "已经过期（临时结果保留 30 分钟）" in response.text
    assert kept(app) == (0, 0, 0) and len(app.state.directions.store) == 0


def test_a_task_in_time_takes_writes_and_neither_looking_nor_writing_extends_its_time(app, inline, browser_a, library, ticks):
    task = explored(browser_a, [library])
    ticks.now += 20 * 60
    assert browser_a.get(task).status_code == 200
    assert decide(browser_a, task, "数据分析", "keep").status_code == 303
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    form = decision_form(browser_a.get(task).text, "用户研究")
    # Thirty minutes and a second after the result came, though it was looked at and written to since.
    ticks.now += 10 * 60 + 1
    late = browser_a.post(form.action, {**form.fields, "state": "keep"})

    assert late.status_code == 404
    assert kept(app)[:2] == (1, 1)
