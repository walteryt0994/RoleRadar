"""U3-A3: 岗位机会 → 找岗位, a preview on synthetic postings with simulated decisions and analyses.

From what is entered to what was found, through the pages: the scope as the user confirms it (U3 proposal, 1.2, the
six cases), the review, the running page and its progress, cancelling and running out of time, and the result. Also
what keeps it a preview (U3-A3 pre-check, 6.2): the searches never reach a real service the app may have, nothing a
page sends can widen a confirmation, only the one executor may add a step and only while the search runs, and nothing
is called a recommendation, opened or applied for. Decisions come from the app's simulated decider unless a test
scripts them; analyses from the app's simulation unless a test scripts its answers. All material is synthetic and
nothing leaves the process.
"""
import html
import json
import re
import threading
from contextlib import ExitStack
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.analysis_contracts import SIMULATION_PROVIDER
from app.discovery import synthetic
from app.discovery.agent import Finished, ScriptedDecider, TraceStep, make_action
from app.discovery.contracts import EndReason, Tool
from app.discovery.synthetic import CAREERS
from app.discovery.tasks import MAX_STEPS, DiscoveryTaskStore, Stage
from app.discovery.web import NOTICE_VERSION
from app.job_analysis.simulation import RECIPIENT, SIMULATION_MODEL
from app.library.web import PACKAGE_DIR
from app.live_gate import LiveSetup
from app.web_main import create_app
from analysis_helpers import ScriptedProvider, cite, parsed_posting
from discovery_helpers import ref
from job_helpers import InlineExecutor, confirmed, text_of, wait_until
from library_helpers import LOCAL_BASE_URL, Browser, form_for
from test_live_gate import BOUND, terms

BASE = "/discover"
PREFERENCES = dict(
    cities_ideal="北京",
    cities_ok="上海",
    cities_no="深圳",
    job_types_ideal="实习",
    directions_ideal="数据分析",
    work_content_no="销售",
    must_haves="最早 2026 年 7 月入职",
)
PROJECT = dict(title="校园订单看板（合成）", actions="用 Python 清洗订单数据，并用 SQL 汇总每周销量。", outcome="看板在课程展示中使用。")
# What the scope's open questions are answered with unless a test says otherwise.
CHOSEN = dict(targets="数据分析实习生", city_others="excluded", job_type_others="excluded", direction_others="included")
PREVIEW = "合成预览：岗位是合成样例，不是真实招聘；每一步由本机固定规则模拟决定，不是 AI。"


class Ticks:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def searches(app):
    return app.state.discovery


@pytest.fixture
def inline(searches):
    searches.executor = InlineExecutor()

    return searches


@pytest.fixture
def library(browser_a):
    """Saved preferences and one confirmed project of user A, entered through the pages."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303

    return confirmed(browser_a, **PROJECT)


def checked(page: str, name: str) -> list[str]:
    return [html.unescape(value) for value in re.findall(rf'name="{name}" value="([^"]*)" checked', page)]


def offered(page: str) -> dict[str, str]:
    return {value.split(":")[0]: value for value in re.findall(r'name="source" value="([^"]+)"', page)}


def entered(browser, *, sources=(), use_preferences=False, **changes) -> dict:
    """The input page's form as a browser posts it: what it ticked by default, the scope chosen, and changes."""
    page = browser.get(BASE).text
    data = {"csrf_token": browser.csrf_token(), **CHOSEN}

    for name in ("city", "job_type", "direction"):
        data[name] = checked(page, name)

    values = offered(page)
    data["source"] = [values[item] for item in sources]

    if use_preferences:
        data["use_preferences"] = "yes"

    data.update(changes)

    return {name: value for name, value in data.items() if value is not None}


def review(browser, **kwargs):
    return browser.post(f"{BASE}/review", entered(browser, **kwargs))


def search_of(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{BASE}/tasks/"), location

    return location.split("?")[0]


def task_id(url: str) -> str:
    return url.rsplit("/", 1)[1]


def confirm(browser, url, *, action="start", fingerprint=None, csrf=None, sends=None, extra=None):
    page = browser.get(url).text
    form = form_for(page, f"{url}/confirm")
    data = {
        "csrf_token": csrf if csrf is not None else form.fields["csrf_token"],
        "fingerprint": fingerprint if fingerprint is not None else form.fields["fingerprint"],
        "action": action,
        "send": re.findall(r'name="send" value="([^"]+)" checked', page) if sends is None else sends,
        **(extra or {}),
    }

    return browser.post(f"{url}/confirm", data)


def stored(app, browser, url):
    """The search as the store keeps it, for the test to look at."""
    store = app.state.discovery.store

    with store._lock:
        return replace(store._tasks[task_id(url)])


def section(page: str, heading: str) -> str:
    start = page.index(f">{heading}</h2>")
    end = page.find("<h2", start)

    return page[start: end if end != -1 else len(page)]


def scripted(searches, *script, provider=None):
    """Decisions from a script instead of the simulated decider; analyses from provider if given."""
    searches.decider_for = lambda guarded: ScriptedDecider(list(script))

    if provider is not None:
        searches.make_provider = lambda task: provider


def search(city, words="数据分析", job_type="实习", direction=None, note="按范围检索"):
    return make_action(
        Tool.SEARCH, source_id=CAREERS, keywords=words, city=city, job_type=job_type, direction=direction, note=note
    )


def act(tool, *posting_ids, note="下一步"):
    return make_action(tool, postings=[ref(posting_id) for posting_id in posting_ids], note=note)


def finish(claim=EndReason.ENOUGH):
    return make_action(Tool.FINISH, claim=claim, note="结束")


class Hold:
    """A decision the test holds until it lets it go: the search waits on it, then gets the action."""

    def __init__(self, action):
        self.action = action
        self.arrived = threading.Event()
        self.release = threading.Event()

    def __call__(self, observation):
        self.arrived.set()
        assert self.release.wait(10), "the test never let the decision go"

        return self.action


# --- where it is, and what it offers ------------------------------------------------------------


def test_finding_postings_sits_beside_job_analysis_and_only_for_a_clear_goal(browser_a):
    def subnav(page):
        return re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', page[page.index('<nav class="subnav"'):])[:2]

    discover = browser_a.get(BASE).text
    analysis = browser_a.get("/job-analysis").text

    assert subnav(discover) == [("/job-analysis", "", "岗位分析"), ("/discover", ' aria-current="page"', "找岗位")]
    assert subnav(analysis) == [("/job-analysis", ' aria-current="page"', "岗位分析"), ("/discover", "", "找岗位")]
    assert '<a href="/job-analysis" aria-current="true">岗位机会</a>' in discover
    # Only a clear goal: no entry for an unsure direction yet (U3-C3b), nor for later modules. 方向探索 is a part of
    # the app of its own since U3-C3a, in the top navigation only.
    assert not any(words in discover for words in ("方向还不确定", "我的简历", "投递跟踪"))
    assert discover.count("方向探索") == 1 and '<a href="/directions">方向探索</a>' in discover
    assert PREVIEW in discover


def test_the_scope_starts_from_the_preferences_and_leaves_the_rest_to_the_user(app, browser_a, library):
    page = browser_a.get(BASE).text

    # Ideal and acceptable values are ticked; what the user cannot accept is listed, never offered.
    assert checked(page, "city") == ["北京", "上海"] and checked(page, "job_type") == ["实习"]
    assert '北京<span class="value-note">理想</span>' in page
    assert "不能接受：深圳。不会检索，也不能选。" in page and 'value="深圳"' not in page
    # Whether other values count is the user's to say: nothing is chosen for them.
    assert 'type="radio"' in page and not re.search(r'type="radio"[^>]* checked', page)

    undecided = review(browser_a, city_others=None, job_type_others=None, direction_others=None)
    text = text_of(undecided.text)

    assert undecided.status_code == 422 and len(app.state.discovery.store) == 0
    assert "城市：所列以外的是否也查还没有选择（本次不限，或只看所列的）" in text
    assert "求职类型：所列以外的是否也查还没有选择" in text and "岗位方向：所列以外的是否也查还没有选择" in text
    # What was entered is kept, for the user to go on from.
    assert checked(undecided.text, "city") == ["北京", "上海"] and 'value="数据分析实习生"' in undecided.text

    # Case 2: only Beijing because the user said so, not because the system guessed.
    only_beijing = search_of(review(browser_a, city=["北京"], city_others="excluded"))
    lines = text_of(browser_a.get(only_beijing).text)

    assert "城市 北京（理想）；只看所列的；不能接受：深圳（不会检索）" in lines


def test_a_value_the_user_cannot_accept_cannot_be_put_in_the_scope(app, browser_a, library):
    # Case 3: asking for Shenzhen is refused before anything is kept, let alone searched.
    added = review(browser_a, city_added="深圳")

    assert added.status_code == 422 and len(app.state.discovery.store) == 0
    assert "城市：“深圳”是不能接受的，不能选" in text_of(added.text)

    # A tick the page never offered is not taken: the scope stays what the page offered.
    forged = search_of(review(browser_a, city=["北京", "上海", "深圳", "广州"]))
    scope = stored(app, browser_a, forged).confirmation.scope

    assert scope.city.values == ("北京", "上海") and scope.city.excluded == ("深圳",)


def test_the_text_boxes_take_the_shared_input_style(browser_a, library):
    """R1, F2: the browser's default input type is not the attribute the shared style selects (input[type=text])."""
    page = browser_a.get(BASE).text
    inputs = re.findall(r"<input\b[^>]*>", page)
    text_boxes = {re.search(r'name="([^"]+)"', tag).group(1): tag for tag in inputs if 'type="text"' in tag}
    shared = (PACKAGE_DIR / "static" / "library.css").read_text(encoding="utf-8")

    # Every input of the page says what it is, so none falls back to the bare browser style unnoticed.
    assert all(re.search(r'\btype="[a-z]+"', tag) for tag in inputs), [tag for tag in inputs if "type=" not in tag]
    assert set(text_boxes) == {"targets", "city_added", "job_type_added", "direction_added"}
    rule = shared[shared.index("input[type=text], textarea {"):]
    rule = rule[: rule.index("}")]

    assert all(part in rule for part in ("font-size: 16px;", "padding: 10px 12px;", "border-radius: var(--radius-control);"))


# --- one search, from the review to what it found -------------------------------------------------


def test_a_search_runs_on_synthetic_postings_with_simulated_decisions_and_says_so(app, browser_a, library, inline):
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    task = stored(app, browser_a, url)
    reviewed = browser_a.get(url).text
    text = text_of(reviewed)

    # The server's sources, recipient and notice, nothing else: the simulation, never a real service.
    assert task.confirmation.sources == synthetic.GRANTS
    assert task.confirmation.recipient == RECIPIENT and task.confirmation.recipient.provider == SIMULATION_PROVIDER
    assert task.confirmation.notice_version == NOTICE_VERSION
    # The preferences the scope is made of are held against postings, never sent with an analysis.
    assert {preference.field for preference in task.confirmation.preferences} == {"work_content_no", "must_haves"}
    assert PREVIEW in reviewed
    assert "合成招聘官网（官方原始发布的合成样例）、合成搜索线索（待核验线索的合成样例）" in text
    assert "本机模拟决策：RoleRadar 内置的固定规则，不是 AI。" in text
    assert "检索 4 次、读取 10 个、核对 10 个、分析 3 个（完整分析结果最多 3 条）；最多 8 步" in text
    assert "确认并开始查找" in reviewed

    response = confirm(browser_a, url)
    assert (response.status_code, response.headers["location"]) == (303, url)
    found = browser_a.get(url).text
    text = text_of(found)

    assert "找岗位结果" in found and PREVIEW in found
    # Results by the rules' categories: the conflict comes last, under its own words.
    results = section(found, "完整分析结果")
    headings = re.findall(r'<h3 class="result-group">([^<]+)</h3>', results)
    titles = re.findall(r"<h4>([^<]+)</h4>", results)

    # The must-have the posting does not speak to is to be confirmed: a gap, not enough to rest on.
    assert headings == ["有基础但有差距或待确认", "有明确冲突，需谨慎考虑"]
    # The three checked open, analysed with one decision, each on its own (U3-Q1).
    assert titles == [
        "数据分析实习生（合成岗位，不是真实招聘）",
        "数据分析实习生（合成岗位，不是真实招聘）",
        "商业数据分析实习生（合成岗位，不是真实招聘）",
    ]
    assert "申请前先确认 现实必要条件：最早 2026 年 7 月入职" in text
    # Each with where it came from, its three times and a check that is no real one.
    assert "原始链接 https://careers.synthetic.invalid/postings/c-bj-analyst（合成示例地址，不能访问）" in text
    assert "截止 2026-11-30（synthetic-careers/c-bj-analyst）；2026-12-10（synthetic-careers/c-bj-analyst-repost）" in text
    assert "（合成来源在本机回答，不是真实核验）" in text
    # Case 3 on the page: Shenzhen only is out; a stated choice is judged by Beijing; no stated choice is pending.
    assert "超出条件（只在不能接受或不在范围里的城市、类型或方向）：2 个" in text
    assert "可选工作地点，按北京判断；深圳（不能接受）" in text
    assert "工作地点待确认：写了北京、深圳，没有说明可以选择；深圳（不能接受）" in text
    # Case 5: a preference that was not filled in is said so, neither met nor in conflict.
    assert "没有判断的偏好 工作方式：未填写，无法判断" in text
    # What was searched, and the steps taken.
    assert "合成招聘官网 · 检索词“数据分析” · 城市 北京 · 求职类型 实习 · 岗位方向 不限 · 返回 10 个" in text
    assert "结束原因 够了" in text and "第 1 步 · 检索岗位 · 完成" in text and "第 6 步 · 分析 · 完成" in text
    assert "第 7 步 · 结束查找 · 完成" in text and "第 8 步" not in text
    assert "本机模拟服务处理的请求：6 次（模拟，没有联网，不产生费用）" in text and "可能已发出" not in found

    for page in (browser_a.get(BASE).text, reviewed, found):
        # Nothing is called a recommendation, nothing synthetic is linked, nothing applies anywhere.
        assert "推荐" not in page
        assert not re.search(r'href="[^"]*\.invalid', page)
        assert not re.search(r"<button[^>]*>[^<]*(申请|投递)", page)


def test_ideal_cities_come_first_and_acceptable_ones_still_show(app, browser_a, library, inline, searches):
    """Case 1: Beijing first, Shanghai still there; ideal only orders."""
    # Both cities searched and nothing analysed: "scope done" ends it ("enough" needs a result, U3-Q2).
    scripted(searches, search("上海"), search("北京"), finish(EndReason.SCOPE_DONE))
    url = search_of(review(browser_a, sources=[library]))
    confirm(browser_a, url)
    leads = section(browser_a.get(url).text, "未分析线索")
    titles = re.findall(r'<span>(合成[^<]+)</span><span>(北京|上海)', leads)

    # Shanghai was searched first, and still comes after every Beijing posting.
    cities = [city for _, city in titles]
    assert "上海" in cities and "北京" in cities
    assert cities == sorted(cities, key=lambda city: city != "北京")


def test_running_out_of_steps_says_so_and_names_what_was_not_searched(app, browser_a, library, inline, searches):
    """Case 4: only Beijing searched when the steps ran out: 到了上限, with Shanghai left, never "nothing suitable"."""
    scripted(
        searches,
        search("北京"),
        *(act(Tool.READ, posting_id) for posting_id in ("c-bj-analyst", "c-bj-sales", "c-choice", "c-two-cities", "c-closed", "c-early-start", "c-no-requirements")),
    )
    url = search_of(review(browser_a, sources=[library]))
    confirm(browser_a, url)
    page = browser_a.get(url).text
    text = text_of(page)

    assert "结束原因 到了上限" in text
    assert "没查的组合 上海 · 实习 · 数据分析；上海 · 实习 · 所列以外的岗位方向" in text
    assert "没有合适结果" not in page


def test_a_start_date_the_user_cannot_make_is_shown_as_a_conflict_never_as_a_recommendation(app, browser_a, library, inline, searches):
    """Case 6: the must-have goes with every analysis, and the conflict found is shown apart, under its own words."""
    parse = parsed_posting(
        job_title="数据分析实习生（合成岗位，不是真实招聘）",
        company="合成咨询",
        location="北京",
        work_mode=None,
        education_requirement=None,
        required_skills=[{"text": "Python", "evidence_text": "熟练使用 Python 进行数据处理"}],
        preferred_skills=[],
        hard_constraints=[],
        uncertain_requirements=[],
    )
    answer = json.dumps(
        {
            "assessments": [{"requirement_id": "R1", "citations": [cite("S1", "actions", "用 Python 清洗订单数据", "practice")]}],
            "preferences": [
                {"preference_id": "F1", "aspect": "work_content", "relation": "not_stated", "job_quotes": [], "job_quotes_aspect": "work_content"},
                {
                    "preference_id": "F2",
                    "aspect": "start_date",
                    "relation": "conflicts",
                    "job_quotes": ["到岗时间：需在 2026 年 3 月前到岗"],
                    "job_quotes_aspect": "start_date",
                },
            ],
        },
        ensure_ascii=False,
    )
    provider = ScriptedProvider(parse, answer, model=SIMULATION_MODEL)
    scripted(
        searches,
        search("北京"),
        act(Tool.READ, "c-early-start"),
        act(Tool.CHECK, "c-early-start"),
        act(Tool.ANALYZE, "c-early-start"),
        finish(),
        provider=provider,
    )
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    confirm(browser_a, url)
    page = browser_a.get(url).text
    results = section(page, "完整分析结果")

    assert "最早 2026 年 7 月入职" in provider.prompts[1]
    assert '<h3 class="result-group">有明确冲突，需谨慎考虑</h3>' in results
    assert "依据较充分" not in results and "推荐" not in page
    assert "到岗时间：需在 2026 年 3 月前到岗" in results


def test_no_experience_chosen_means_no_analysis_and_no_preference(app, browser_a, library, inline):
    url = search_of(review(browser_a, use_preferences=True))
    task = stored(app, browser_a, url)

    assert task.confirmation.selection == () and task.confirmation.preferences == ()
    assert "没有选择经历：只查找、读取和核对岗位，不做个性化分析。" in text_of(browser_a.get(url).text)

    confirm(browser_a, url)
    text = text_of(browser_a.get(url).text)

    assert "未进行个性化分析：没有选择经历，只按检索范围查找、读取和核对岗位" in text
    assert "这次没有完整分析的岗位。" in text and "本机模拟服务处理的请求：0 次" in text


# --- the running page, its progress, cancelling and its time --------------------------------------


def test_the_running_page_shows_each_step_as_it_is_taken_and_cancelling_adds_none(app, browser_a, library, searches):
    held = Hold(act(Tool.READ, "c-bj-analyst"))
    scripted(searches, search("北京"), held)
    url = search_of(review(browser_a, sources=[library]))

    assert confirm(browser_a, url).status_code == 303
    assert held.arrived.wait(10)
    wait_until(lambda: len(stored(app, browser_a, url).steps) == 1)

    running = browser_a.get(url).text
    status = browser_a.get(f"{url}/status")
    body = status.json()

    assert "正在查找" in running and PREVIEW in running and "data-task-running" in running
    assert "第 1 步 · 检索岗位 · 完成" in running and "取消查找" in running
    # Only what the page needs: no token, session, fingerprint or material.
    assert set(body) == {"stage", "status", "steps"} and body["stage"] == "running"
    assert [set(step) for step in body["steps"]] == [{"title", "params", "summary", "note"}]
    assert body["steps"][0]["title"] == "第 1 步 · 检索岗位 · 完成"
    assert body["status"].startswith("正在查找：已完成 1 步。本机模拟服务处理的请求：")
    task = stored(app, browser_a, url)
    raw = status.text

    for secret in (task.worker, task.session, task.fingerprint, task.owner_id, library, PROJECT["actions"]):
        assert secret not in raw

    cancelled = browser_a.post(f"{url}/cancel", {"csrf_token": browser_a.csrf_token()})
    assert (cancelled.status_code, cancelled.headers["location"]) == (303, url)
    held.release.set()
    wait_until(lambda: stored(app, browser_a, url).over)

    # The decision came back after the stop: nothing it said was done, and the page gained no step.
    task = stored(app, browser_a, url)
    assert task.stage is Stage.CANCELLED and len(task.steps) == 1
    assert task.finished is not None and task.finished.outcome.end is EndReason.CANCELLED
    page = browser_a.get(url).text

    assert "查找已取消" in page and "你取消了这次查找，之后没有再开始新的步骤。" in text_of(page)
    assert "data-task-running" not in page and browser_a.get(f"{url}/status").json()["stage"] == "cancelled"


def test_a_late_result_after_cancelling_is_not_shown_and_nothing_is_made_up(app, browser_a, library, inline):
    """Pre-check 6.2.3: a search that came back complete after the stop is dropped; the page shows the steps it had."""
    finished_url = search_of(review(browser_a, sources=[library]))
    confirm(browser_a, finished_url)
    complete = stored(app, browser_a, finished_url).finished
    assert complete.outcome.end is EndReason.ENOUGH and complete.outcome.results

    url = search_of(review(browser_a, sources=[library]))
    form = form_for(browser_a.get(url).text, f"{url}/confirm")
    store = app.state.discovery.store
    task = stored(app, browser_a, url)
    store.begin(task.id, task.owner_id, task.session, form.fields["fingerprint"])
    worker, _ = store.claim(task.id)
    assert store.progress(task.id, worker, complete.trace.steps[0])

    browser_a.post(f"{url}/cancel", {"csrf_token": browser_a.csrf_token()})
    # The same search, finished in full, comes back after the stop: late, dropped.
    store.finish(task.id, worker=worker, finished=complete)
    page = browser_a.get(url).text
    text = text_of(page)

    assert stored(app, browser_a, url).finished is None
    assert "查找已取消" in page and "停止前完成的步骤" in page and "第 1 步 · 检索岗位 · 完成" in text
    assert "停止时没有可以展示的结果。" in text and "完整分析结果" not in page


def test_a_search_out_of_its_time_ends_as_a_timeout(app, browser_a, library, searches):
    ticks = Ticks()
    searches.store = DiscoveryTaskStore(ticks)
    held = Hold(act(Tool.READ, "c-bj-analyst"))
    scripted(searches, search("北京"), held)
    url = search_of(review(browser_a, sources=[library]))
    confirm(browser_a, url)
    assert held.arrived.wait(10)

    ticks.now += 600
    held.release.set()
    wait_until(lambda: stored(app, browser_a, url).over)
    task = stored(app, browser_a, url)
    page = browser_a.get(url).text

    assert (task.stage, task.cancelled_by, task.finished.outcome.end) == (Stage.CANCELLED, "timeout", EndReason.TIMEOUT)
    assert "查找已超时" in page and "这次查找超过了运行时限，之后没有再开始新的步骤。" in text_of(page)


def test_only_the_executor_adds_steps_and_only_while_the_search_runs(app, browser_a, library):
    """Pre-check 6.2.2: the token holder, running within its time, at most MAX_STEPS; nothing after a stop or the end."""
    ticks = Ticks()
    store = DiscoveryTaskStore(ticks)
    app.state.discovery.store = store
    step = TraceStep(1, Tool.SEARCH, (), "done", "合成招聘官网返回 1 个岗位。", "按范围检索", None)

    def running():
        url = search_of(review(browser_a, sources=[library]))
        task = stored(app, browser_a, url)
        store.begin(task.id, task.owner_id, task.session, task.fingerprint)
        worker, _ = store.claim(task.id)

        return task.id, worker

    first, worker = running()
    assert not store.progress(first, None, step) and not store.progress(first, "another-token", step)
    assert all(store.progress(first, worker, replace(step, number=number)) for number in range(1, MAX_STEPS + 1))
    assert not store.progress(first, worker, replace(step, number=MAX_STEPS + 1))
    store.finish(first, worker=worker, problem="internal_error")
    assert not store.progress(first, worker, step)
    assert len(store._tasks[first].steps) == MAX_STEPS

    cancelled, worker = running()
    browser_a.post(f"{BASE}/tasks/{cancelled}/cancel", {"csrf_token": browser_a.csrf_token()})
    assert not store.progress(cancelled, worker, step) and store._tasks[cancelled].steps == ()

    late, worker = running()
    ticks.now += 600
    assert not store.progress(late, worker, step)
    assert (store._tasks[late].stage, store._tasks[late].cancelled_by, store._tasks[late].steps) == (Stage.CANCELLED, "timeout", ())


# --- what keeps it a preview, and the user's -------------------------------------------------------


def test_a_real_service_the_app_has_is_never_reached_from_a_search(make_config, clock):
    """Pre-check 6.2.1: even with a real service approved for searches too, a search runs the simulation only."""
    built = []

    def make():
        built.append("built")
        raise AssertionError("a search reached the real service")

    setup = LiveSetup(terms(purposes=frozenset({"analysis", "discovery"})), BOUND, make)

    with ExitStack() as stack:
        application = create_app(make_config(), clock=clock, live=setup)
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))
        application.state.discovery.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
        project = confirmed(browser, **PROJECT)

        # Nothing a page sends names a service, a source or an address that is taken.
        smuggled = {"recipient": "openai", "provider": "openai", "model": "gpt", "source_url": "https://example.invalid/jobs", "live": "1"}
        url = search_of(browser.post(f"{BASE}/review?live=1", entered(browser, sources=[project], **smuggled)))
        response = confirm(browser, url, extra=smuggled)
        task = application.state.discovery.store._tasks[task_id(url)]
        gate = application.state.job_analysis.live.gate

        assert response.status_code == 303 and task.stage is Stage.DONE and task.finished.outcome.results
        assert task.confirmation.recipient == RECIPIENT and task.confirmation.sources == synthetic.GRANTS
        assert built == [] and gate.status().requests_used == 0
        assert "本机模拟服务处理的请求" in text_of(browser.get(url).text)


def test_tracing_variables_keep_a_search_from_starting(app, browser_a, library, inline, searches):
    searches.environ = {"LANGSMITH_TRACING": "true", "PATH": "/usr/bin"}
    page = browser_a.get(BASE).text
    url = search_of(review(browser_a, sources=[library]))
    reviewed = browser_a.get(url).text

    for shown in (page, reviewed):
        assert "环境里设置了可能打开 LangSmith 追踪的变量（LANGSMITH_TRACING），查找不会开始。" in text_of(shown)

    assert "确认并开始查找" not in reviewed
    refused = confirm(browser_a, url)

    assert refused.status_code == 409 and "所以没有开始，什么都没有发送" in text_of(refused.text)
    assert stored(app, browser_a, url).stage is Stage.REVIEW


def test_a_search_starts_once_and_only_from_the_review_it_was_confirmed_on(app, browser_a, library, inline):
    url = search_of(review(browser_a, sources=[library]))

    stale = confirm(browser_a, url, fingerprint="0" * 64)
    assert stale.status_code == 409 and "这个确认来自旧的核对页面，所以没有开始" in text_of(stale.text)
    assert stored(app, browser_a, url).stage is Stage.REVIEW

    form = form_for(browser_a.get(url).text, f"{url}/confirm")
    assert confirm(browser_a, url).status_code == 303
    sent = stored(app, browser_a, url).handed

    # A second click, another tab, a replayed form: nothing runs again.
    again = browser_a.post(f"{url}/confirm", {"csrf_token": form.fields["csrf_token"], "fingerprint": form.fields["fingerprint"], "action": "start"})
    assert (again.status_code, again.headers["location"]) == (303, f"{url}?notice=already")
    assert stored(app, browser_a, url).handed == sent
    assert "这次确认已经开始过，没有重复执行。" in browser_a.get(again.headers["location"]).text


def test_unticking_a_field_makes_a_new_review_and_the_old_one_starts_nothing(app, browser_a, library, inline):
    url = search_of(review(browser_a, sources=[library]))
    kept = f"{library}:title"
    changed = confirm(browser_a, url, sends=[kept])
    fresh = search_of(changed)

    assert changed.headers["location"].endswith("?notice=scope_changed") and fresh != url
    assert stored(app, browser_a, fresh).confirmation.selection[0].fields == ("title",)
    assert "你改动了发送范围，所以还没有开始" in browser_a.get(changed.headers["location"]).text
    # The review it replaced is gone: its page and its form start nothing.
    assert browser_a.get(url).status_code == 404 and confirm_gone(browser_a, url).status_code == 404


def confirm_gone(browser, url):
    return browser.post(f"{url}/confirm", {"csrf_token": browser.csrf_token(), "fingerprint": "0" * 64, "action": "start"})


def test_material_changed_after_the_review_is_refused_when_the_search_starts(app, browser_a, library, inline):
    url = search_of(review(browser_a, sources=[library]))
    assert browser_a.submit(f"/experiences/{library}/edit", f"/experiences/{library}/draft", actions="改过的内容").status_code == 303

    confirm(browser_a, url)
    task = stored(app, browser_a, url)
    text = text_of(browser_a.get(url).text)

    assert task.problem == "start_refused" and task.handed.total == 0
    assert "开始前的检查没有通过，所以没有开始，什么都没有发送：" in text
    assert "所选经历已经修改、重新确认或删除" in text


def test_a_search_is_its_users_and_sessions_and_every_post_carries_the_sessions_token(app, browser_a, browser_b, library, searches):
    held = Hold(finish())
    scripted(searches, held)
    url = search_of(review(browser_a, sources=[library]))
    confirm(browser_a, url)
    assert held.arrived.wait(10)
    other_session = Browser.signed_in(app, "合成用户 A")

    for stranger in (browser_b, other_session):
        assert stranger.get(url).status_code == 404 and "找不到这次查找" in stranger.get(url).text
        assert stranger.get(f"{url}/status").json() == {"stage": "gone"}
        assert stranger.post(f"{url}/cancel", {"csrf_token": stranger.csrf_token()}).status_code == 404
        assert confirm_gone(stranger, url).status_code == 404
        assert stranger.get(f"{BASE}?from={task_id(url)}").status_code == 200

    for path in (f"{url}/cancel", f"{url}/confirm", f"{BASE}/review"):
        for token in (None, "forged-token", browser_b.csrf_token()):
            data = {} if token is None else {"csrf_token": token}
            assert browser_a.post(path, data).status_code == 403, (path, token)

    assert stored(app, browser_a, url).stage is Stage.RUNNING
    # Logging out ends the session's search: it is stopped and forgotten.
    task = stored(app, browser_a, url)
    browser_a.post("/logout", {"csrf_token": browser_a.csrf_token()})
    held.release.set()

    assert task.cancel.is_set() and task_id(url) not in app.state.discovery.store._tasks
