"""U2-A2: 岗位机会 → 岗位分析, from choosing material to reading the result.

The app's simulated service answers unless a test puts a recording, gated,
failing or scripted one in its place (job_helpers). All material is
synthetic; nothing leaves the process.
"""
import html
import json
import logging
import re

import markupsafe
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_provider import AIProviderTimeoutError
from app.job_analysis.tasks import MAX_RUN_SECONDS, TASK_TTL_SECONDS, Stage, TaskStore
from app.library.db import LibraryBase
from app.web_main import create_app
from job_helpers import (
    POSTING,
    AnsweringProvider,
    FailingProvider,
    Gate,
    InlineExecutor,
    RecordingProvider,
    confirm_form,
    confirmed,
    offered,
    review,
    start,
    task_of,
    text_of,
    use_provider,
    wait_until,
)
from library_helpers import LOCAL_BASE_URL, Browser

SENTINEL = "岗位分析外哨兵-a2-7c3f"


class Ticks:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def inline(app):
    app.state.job_analysis.executor = InlineExecutor()

    return app


@pytest.fixture
def recorder(app):
    return use_provider(app, RecordingProvider())


@pytest.fixture
def ticks(app):
    ticks = Ticks()
    app.state.job_analysis.store = TaskStore(ticks)

    return ticks


def region(html: str, start: str, end: str) -> str:
    return html[html.index(start): html.index(end, html.index(start))]


def main_of(html: str) -> str:
    return region(html, '<main id="main"', "</main>")


def finish(app) -> None:
    """Wait for the background analyses to end (the pool is not used again by the test)."""
    app.state.job_analysis.executor.shutdown(wait=True)


def project(browser, **content) -> str:
    fields = {"title": "订单看板（合成）", "actions": "用 Python 清洗订单数据。", "outcome": "看板在课程展示中使用。"}

    return confirmed(browser, **{**fields, **content})


# --- where it is, and what it offers ------------------------------------------------------


def test_job_opportunities_lead_to_job_analysis_and_mark_where_you_are(browser_a):
    def top(page):
        return re.findall(r'<a href="([^"]+)"( aria-current="true")?>([^<]+)</a>', region(page, '<nav class="nav"', "</nav>"))

    jobs = browser_a.get("/job-analysis").text
    library = browser_a.get("/experiences").text

    assert top(jobs) == [("/experiences", "", "我的资料"), ("/job-analysis", ' aria-current="true"', "岗位机会")]
    assert top(library) == [("/experiences", ' aria-current="true"', "我的资料"), ("/job-analysis", "", "岗位机会")]
    assert '<a href="/job-analysis" aria-current="page">岗位分析</a>' in region(jobs, '<nav class="subnav"', "</nav>")
    # 资料管理 and the parts of 我的资料 belong to 我的资料 only.
    assert 'class="manage"' not in jobs and 'href="/basic-profile"' not in jobs
    # Later modules still get no entry.
    assert not any(name in jobs for name in ("方向探索", "我的简历", "投递跟踪"))


def test_only_my_current_confirmed_versions_are_offered_and_none_is_ticked(browser_a, browser_b):
    kept = project(browser_a, title="确认过的项目", private_note=SENTINEL)
    browser_a.create(title="还没确认的项目", actions="合成内容")
    edited = project(browser_a, title="确认后又改过")
    assert browser_a.submit(f"/experiences/{edited}/edit", f"/experiences/{edited}/draft", actions="改过").status_code == 303
    gone = project(browser_a, title="删掉的项目")
    assert browser_a.submit(f"/experiences/{gone}/delete", f"/experiences/{gone}/delete").status_code == 303
    project(browser_b, title="B 的项目")
    page = browser_a.get("/job-analysis").text
    picks = region(page, 'class="pick-list"', "</ul>")

    assert set(offered(browser_a)) == {kept}
    assert " checked" not in picks
    assert "确认过的项目" in picks and "确认版 1" in picks
    assert not any(title in page for title in ("还没确认的项目", "确认后又改过", "删掉的项目", "B 的项目"))
    assert SENTINEL not in page
    assert "另有 2 段经历还没有确认" in page


def test_with_nothing_confirmed_the_page_leads_back_to_the_library(browser_b):
    main = main_of(browser_b.get("/job-analysis").text)

    assert "还没有已确认的经历" in main and 'href="/experiences"' in main
    assert 'name="source"' not in main


def test_without_a_session_the_job_pages_lead_to_login_and_posts_are_refused(app, browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    anonymous = Browser(app)

    for path in ("/job-analysis", task):
        assert (anonymous.get(path).status_code, anonymous.get(path).headers.get("location")) == (303, "/login"), path

    assert anonymous.get(f"{task}/status").status_code in (303, 401)

    for path in ("/job-analysis/review", f"{task}/confirm", f"{task}/cancel", f"{task}/again"):
        assert anonymous.post(path, {"csrf_token": "guess", "action": "start"}).status_code == 401, path

    assert recorder.prompts == [] and "确认并开始分析" in browser_a.get(task).text


def test_saving_from_the_leave_dialog_on_the_way_here_says_what_was_saved(browser_a):
    form = browser_a.form("/experiences/new", "/experiences")
    response = browser_a.post("/experiences", {**form.fields, "actions": "离开前保存的内容", "next": "/job-analysis"})

    assert response.headers["location"] == "/job-analysis?notice=created_before_leaving"
    assert '<p class="notice" role="status">已保存你刚才新建的记录（待核对）。</p>' in browser_a.get(response.headers["location"]).text


# --- input problems keep the input ------------------------------------------------------------


@pytest.mark.parametrize("problem", ["nothing_chosen", "no_posting", "too_long", "confidential"])
def test_a_problem_with_the_input_keeps_it_and_sends_nothing(app, browser_a, recorder, problem):
    item = project(browser_a)
    posting = {
        "no_posting": "   ",
        "too_long": "岗" * 8001,
        "confidential": POSTING + "本岗位信息为内部资料，不得外传。",
    }.get(problem, POSTING + "<b>标记</b>")
    response = review(browser_a, [] if problem == "nothing_chosen" else [item], posting)
    page = response.text

    assert response.status_code == 422
    assert '<p class="error" role="alert">' in page
    # Nothing on a page with an error waits for an animation.
    assert "data-reveal" not in main_of(page)
    # What was typed and chosen is still there, as text, and leaving asks first.
    assert str(markupsafe.escape(posting.strip() and posting)) in page
    assert ("data-initially-dirty" in page) == bool(posting.strip())

    if problem != "nothing_chosen":
        assert f'value="{offered(browser_a)[item]}" checked' in page

    assert recorder.prompts == [] and len(app.state.job_analysis.store) == 0


def test_a_version_that_changed_before_the_review_is_not_swapped_for_the_new_one(browser_a, recorder):
    item = project(browser_a)
    old = offered(browser_a)
    assert browser_a.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", actions="改过之后").status_code == 303
    assert browser_a.submit(f"/experiences/{item}", f"/experiences/{item}/confirm").status_code == 303
    response = review(browser_a, [item], values=old)
    new = offered(browser_a)[item]

    assert response.status_code == 422 and "请重新选择" in response.text
    assert new != old[item] and f'value="{new}" checked' not in response.text
    assert recorder.prompts == []


# --- the review shows what goes out, and that is what goes out ----------------------------------


def outgoing_material(browser):
    assert browser.submit("/basic-profile/edit", "/basic-profile", name="张合成", phone="13900139000").status_code == 303
    item = project(browser, actions="用 Python 清洗订单数据。电话 13800138000", private_note=SENTINEL)
    unchosen = project(browser, title="没有选的经历")

    return item, unchosen, task_of(review(browser, [item], POSTING + "简历请发 hr@example.com"))


@pytest.mark.parametrize(
    "line",
    [
        "home address: 123 Synthetic Lane, Exampleville",
        "家庭地址: 北京市合成区示例小区东园七栋五层",
        "工作地点：合成市示例路 88 号",
        "面试在合成大厦 3栋502 进行",
    ],
)
def test_a_posting_with_an_address_is_refused_before_any_preview_and_keeps_the_input(app, browser_a, recorder, line):
    item = project(browser_a)
    posting = POSTING + line
    response = review(browser_a, [item], posting)
    page = response.text

    assert response.status_code == 422 and len(app.state.job_analysis.store) == 0
    assert "像是详细地址的内容" in page and "没有生成发送预览" in page
    # Nothing was half-masked: there is no preview at all, and the input is as typed.
    assert "已隐去" not in page and 'class="review-posting' not in page
    assert str(markupsafe.escape(posting)) in page and f'value="{offered(browser_a)[item]}" checked' in page
    assert recorder.prompts == []
    # Without the address the same input goes on, and nothing of it is left.
    task = task_of(review(browser_a, [item], POSTING))
    assert line not in browser_a.get(task).text


def test_contact_details_in_a_posting_are_masked_and_the_preview_is_what_goes_out(inline, browser_a, recorder):
    contacts = "简历发 hr@example.com，电话 13800138000，联系人证件 11010119900307123X"
    task = task_of(review(browser_a, [project(browser_a)], POSTING + contacts))
    preview = html.unescape(re.search(r'<div class="review-posting text">(.*?)</div>', browser_a.get(task).text, re.S).group(1))
    assert start(browser_a, task).status_code == 303
    parse = recorder.prompts[0]

    assert "简历发 ［已隐去邮箱］，电话 ［已隐去电话号码］，联系人证件 ［已隐去证件号码］" in preview
    # The posting part of the request is exactly the text the review showed.
    assert f"<<<JOB_POSTING_START>>>\n{preview}\n<<<JOB_POSTING_END>>>" in parse
    assert not any(value in parse + recorder.prompts[1] for value in ("hr@example.com", "13800138000", "11010119900307123X"))
    assert "岗位分析结果" in browser_a.get(task).text


def test_the_review_shows_exactly_what_would_go_out(browser_a, recorder):
    _, _, task = outgoing_material(browser_a)
    page = browser_a.get(task).text
    main = main_of(page)

    assert "［已隐去邮箱］" in main and "hr@example.com" not in main
    assert "不会发送：含有疑似电话号码" in main and "13800138000" not in main
    assert "看板在课程展示中使用。" in main and "本机模拟服务" in main
    assert not any(text in page for text in (SENTINEL, "张合成", "13900139000", "没有选的经历"))
    assert recorder.prompts == []


def test_what_goes_out_is_what_the_review_showed(inline, browser_a, recorder):
    item, unchosen, task = outgoing_material(browser_a)
    assert start(browser_a, task).status_code == 303
    parse, analysis = recorder.prompts

    assert "［已隐去邮箱］" in parse and "看板在课程展示中使用。" in analysis
    for absent in ("hr@example.com", "13800138000", "用 Python 清洗订单数据", SENTINEL, "张合成", "13900139000", "没有选的经历", item, unchosen):
        assert absent not in parse + analysis, absent


@pytest.mark.parametrize("action", ["start", "update"])
def test_unticking_a_field_needs_a_new_review_before_anything_goes_out(inline, browser_a, recorder, action):
    task = task_of(review(browser_a, [project(browser_a)]))
    _, ticked, _ = confirm_form(browser_a, task)
    response = start(browser_a, task, sends=[key for key in ticked if not key.endswith(":outcome")], action=action)
    newer = task_of(response)

    assert newer != task and response.headers["location"].endswith(f"notice=scope_{'changed' if action == 'start' else 'updated'}")
    assert recorder.prompts == []
    assert browser_a.get(task).status_code == 404
    assert 'class="text review-text is-left-out">看板在课程展示中使用。' in browser_a.get(newer).text
    assert start(browser_a, newer).status_code == 303
    assert "看板在课程展示中使用。" not in recorder.prompts[1] and "用 Python 清洗订单数据。" in recorder.prompts[1]


# --- only the user's own confirmation starts a task, once ------------------------------------


def test_looking_never_sends_anything(browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))

    for path in ("/job-analysis", f"/job-analysis?from={task.rsplit('/', 1)[1]}", task, f"{task}/status"):
        assert browser_a.get(path).status_code == 200, path

    assert recorder.prompts == []


def test_a_start_without_the_session_token_or_from_another_site_starts_nothing(browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    form, ticked, _ = confirm_form(browser_a, task)
    data = {"csrf_token": form.fields["csrf_token"], "fingerprint": form.fields["fingerprint"], "action": "start", "send": ticked}

    assert start(browser_a, task, csrf="x" * 43).status_code == 403
    assert browser_a.client.post(f"{task}/confirm", data=data, headers={"origin": "http://elsewhere.example"}).status_code == 403
    assert "确认并开始分析" in browser_a.get(task).text
    assert recorder.prompts == []


def test_a_forged_or_old_fingerprint_starts_nothing(browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    response = start(browser_a, task, fingerprint="0" * 64)

    assert response.status_code == 409 and "旧的核对页面" in response.text
    assert "确认并开始分析" in browser_a.get(task).text
    assert recorder.prompts == []


def test_a_task_belongs_to_one_user_and_one_session(app, browser_a, browser_b, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    task_id = task.rsplit("/", 1)[1]
    form, ticked, _ = confirm_form(browser_a, task)
    second_session = Browser.signed_in(app, "合成用户 A")

    for other in (browser_b, second_session):
        data = {"csrf_token": other.csrf_token(), "fingerprint": form.fields["fingerprint"], "action": "start", "send": ticked}

        assert other.get(task).status_code == 404
        assert other.get(f"{task}/status").status_code == 404
        assert other.post(f"{task}/confirm", data).status_code == 404
        assert other.post(f"{task}/cancel", {"csrf_token": other.csrf_token()}).status_code == 404
        assert other.post(f"{task}/again", {"csrf_token": other.csrf_token()}).status_code == 404
        # Its input cannot be called up either.
        assert "数据分析实习生（合成岗位）" not in other.get(f"/job-analysis?from={task_id}").text

    assert recorder.prompts == []
    assert "确认并开始分析" in browser_a.get(task).text
    restored = browser_a.get(f"/job-analysis?from={task_id}").text
    assert "数据分析实习生（合成岗位）" in restored and " checked" in region(restored, 'class="pick-list"', "</ul>")


def test_logging_out_forgets_the_sessions_tasks(app, browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    assert browser_a.post("/logout", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    browser_a.sign_in("合成用户 A")

    assert browser_a.get(task).status_code == 404 and len(app.state.job_analysis.store) == 0
    assert recorder.prompts == []


def test_one_confirmation_runs_once(inline, browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    form, ticked, _ = confirm_form(browser_a, task)
    data = {"csrf_token": form.fields["csrf_token"], "fingerprint": form.fields["fingerprint"], "action": "start", "send": ticked}
    first = browser_a.post(f"{task}/confirm", data)
    again = browser_a.post(f"{task}/confirm", data)

    assert first.headers["location"] == task and again.headers["location"] == f"{task}?notice=already"
    assert len(recorder.prompts) == 2
    assert "这次确认已经开始过，没有重复执行。" in browser_a.get(again.headers["location"]).text


def test_one_task_of_a_session_runs_at_a_time(app, browser_a):
    gate = use_provider(app, Gate())
    item = project(browser_a)
    first = task_of(review(browser_a, [item]))
    assert start(browser_a, first).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    second = task_of(review(browser_a, [item]))
    response = start(browser_a, second)
    gate.release.release()
    gate.release.release()
    finish(app)

    assert response.status_code == 409 and "已经有一个分析正在进行" in response.text
    assert len(gate.prompts) == 2 and "确认并开始分析" in browser_a.get(second).text


# --- waiting, cancelling and late answers --------------------------------------------------------


def test_a_running_task_shows_its_real_step_and_can_be_cancelled_without_scripts(app, browser_a):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    page = browser_a.get(task).text
    status = browser_a.get(f"{task}/status").json()
    cancelled = browser_a.post(f"{task}/cancel", {"csrf_token": browser_a.csrf_token()})
    gate.release.release()
    finish(app)

    assert '<li aria-current="step">解析岗位</li>' in page and "取消分析" in page and "刷新状态" in page
    assert '<script src="/static/job_analysis.js"></script>' in page and "%" not in text_of(main_of(page))
    assert status == {"stage": "running", "step": "解析岗位", "requests_sent": 1}
    assert cancelled.headers["location"] == task


@pytest.mark.parametrize("at", [1, 2])
def test_cancelling_while_a_request_is_out_drops_its_late_answer(app, browser_a, at):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303

    for _ in range(at - 1):
        assert gate.arrived.acquire(timeout=10)
        gate.release.release()

    assert gate.arrived.acquire(timeout=10)
    assert browser_a.post(f"{task}/cancel", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    gate.release.release()
    finish(app)
    page = browser_a.get(task).text

    assert "分析已取消" in page and f"已交给服务的请求：{at} 次" in page and "岗位分析结果" not in page
    assert len(gate.prompts) == at
    assert browser_a.get(f"{task}/status").json()["stage"] == "cancelled"


def test_a_late_answer_never_lands_on_a_newer_task(app, browser_a):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    assert browser_a.post(f"{task}/cancel", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    newer = task_of(browser_a.post(f"{task}/again", {"csrf_token": browser_a.csrf_token()}))
    recorder = use_provider(app, RecordingProvider())
    assert start(browser_a, newer).status_code == 303
    wait_until(lambda: browser_a.get(f"{newer}/status").json()["stage"] == "done")
    gate.release.release()
    finish(app)

    assert "岗位分析结果" in browser_a.get(newer).text and len(recorder.prompts) == 2
    assert "分析已取消" in browser_a.get(task).text and len(gate.prompts) == 1


def test_logging_out_cancels_a_running_task_before_its_next_request(app, browser_a):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    assert browser_a.post("/logout", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    gate.release.release()
    finish(app)

    assert len(gate.prompts) == 1 and len(app.state.job_analysis.store) == 0


def test_a_task_expires_and_the_page_says_so(browser_a, ticks, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))
    form, ticked, _ = confirm_form(browser_a, task)
    ticks.now += TASK_TTL_SECONDS + 1
    page = browser_a.get(task)
    data = {"csrf_token": form.fields["csrf_token"], "fingerprint": form.fields["fingerprint"], "action": "start", "send": ticked}

    assert page.status_code == 404 and "已经过期" in page.text
    assert browser_a.post(f"{task}/confirm", data).status_code == 404
    assert recorder.prompts == []


def test_a_task_past_its_time_limit_is_stopped(app, browser_a, ticks):
    gate = use_provider(app, Gate())
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    ticks.now += MAX_RUN_SECONDS + 1
    page = browser_a.get(task).text
    gate.release.release()
    finish(app)

    assert "超过了运行时限" in page and len(gate.prompts) == 1


class Lagging(RecordingProvider):
    """Lets the store's clock pass the run limit while the given request is out, as a slow service would."""

    def __init__(self, ticks, at):
        super().__init__()
        self.ticks, self.at = ticks, at

    def generate_text(self, prompt, **options):
        answer = super().generate_text(prompt, **options)

        if len(self.prompts) == self.at:
            self.ticks.now += MAX_RUN_SECONDS + 1

        return answer


@pytest.mark.parametrize("at", [1, 2])
def test_a_task_out_of_time_goes_no_further_and_takes_no_late_answer_without_anyone_looking(inline, app, browser_a, ticks, at):
    provider = use_provider(app, Lagging(ticks, at))
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    # Seen from inside, without a page request whose lazy sweep could stop it instead.
    stored = app.state.job_analysis.store._tasks[task.rsplit("/", 1)[1]]

    assert (stored.stage, stored.cancelled_by, stored.result) == (Stage.CANCELLED, "timeout", None)
    assert len(provider.prompts) == at and stored.requests_sent == at
    assert "超过了运行时限" in browser_a.get(task).text


@pytest.fixture
def swept(make_config, clock):
    """An app whose lifespan sweeps the analysis tasks every 50 ms, with a store clock the test moves."""
    application = create_app(make_config(), clock=clock, job_sweep_seconds=0.05)
    ticks = Ticks()
    application.state.job_analysis.store = TaskStore(ticks)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        yield application, ticks

    # Stopping the app ends its sweeping too.
    assert application.state.job_analysis.sweeper.done()


def test_the_app_forgets_expired_tasks_with_no_page_asking(swept):
    application, ticks = swept
    browser = Browser.signed_in(application, "合成用户 A")
    store = application.state.job_analysis.store
    task_of(review(browser, [project(browser)]))
    ticks.now += TASK_TTL_SECONDS + 1

    wait_until(lambda: len(store) == 0, timeout=5)


def test_the_app_stops_an_overdue_running_task_with_no_page_asking(swept):
    application, ticks = swept
    browser = Browser.signed_in(application, "合成用户 A")
    gate = use_provider(application, Gate())
    task = task_of(review(browser, [project(browser)]))
    assert start(browser, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    stored = application.state.job_analysis.store._tasks[task.rsplit("/", 1)[1]]
    ticks.now += MAX_RUN_SECONDS + 1

    wait_until(lambda: stored.stage is Stage.CANCELLED, timeout=5)
    assert stored.cancelled_by == "timeout" and stored.cancel.is_set()
    gate.release.release()
    application.state.job_analysis.executor.shutdown(wait=True)

    assert len(gate.prompts) == 1 and stored.result is None


# --- material that changes ----------------------------------------------------------------------


def test_material_changed_after_the_review_is_not_sent(inline, browser_a, recorder):
    item = project(browser_a)
    task = task_of(review(browser_a, [item]))
    assert browser_a.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", outcome="复核后改过").status_code == 303
    assert start(browser_a, task).status_code == 303
    page = browser_a.get(task).text

    assert "所选的资料已经不能按核对时的版本使用" in page and "修改后还没有重新确认" in page
    assert "已交给服务的请求：0 次" in page and recorder.prompts == []


def test_material_changed_while_the_analysis_runs_stops_it_after_the_parse(app, browser_a):
    gate = use_provider(app, Gate())
    item = project(browser_a)
    task = task_of(review(browser_a, [item]))
    assert start(browser_a, task).status_code == 303
    assert gate.arrived.acquire(timeout=10)
    assert browser_a.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", outcome="分析时改过").status_code == 303
    gate.release.release()
    finish(app)
    page = browser_a.get(task).text

    assert "开始之后，所选的资料有了变化" in page and "已交给服务的请求：1 次" in page
    assert len(gate.prompts) == 1


def test_a_result_is_not_called_current_once_its_material_changed(inline, browser_a, recorder):
    item = project(browser_a)
    task = task_of(review(browser_a, [item]))
    assert start(browser_a, task).status_code == 303
    assert "不是当前有效的分析" not in browser_a.get(task).text
    assert browser_a.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", outcome="之后改过").status_code == 303

    assert "不是当前有效的分析" in browser_a.get(task).text


# --- the result ------------------------------------------------------------------------------------


def scripted(*, h1=(), r1=(), r2=(), p1=()):
    return json.dumps(
        {
            "assessments": [
                {"requirement_id": key, "citations": [dict(zip(("source_id", "field", "quote", "basis"), item)) for item in cited]}
                for key, cited in (("R1", r1), ("R2", r2), ("P1", p1), ("H1", h1))
            ]
        },
        ensure_ascii=False,
    )


def test_the_result_reads_conditions_support_and_preferences_with_their_sources(inline, app, browser_a):
    item = project(browser_a, actions="用 Python 清洗了订单数据。")
    school = confirmed(browser_a, "education", school="合成大学", graduation="2025 年 6 月")
    use_provider(app, AnsweringProvider(scripted(
        r1=[("S1", "actions", "用 Python 清洗了订单数据", "practice")],
        h1=[("S2", "graduation", "2025 年 6 月", "conflict")],
    )))
    task = task_of(review(browser_a, [item, school]))
    assert start(browser_a, task).status_code == 303
    main = main_of(browser_a.get(task).text)
    text = text_of(main)
    conditions = region(main, 'id="conditions-title"', "</section>")

    headings = [main.index(f'id="{name}-title"') for name in ("conditions", "skills", "preferences", "coverage")]

    assert headings == sorted(headings)
    # The conflict stands first, apart from the skill coverage.
    assert "有 1 项招聘条件和所选资料冲突" in text
    assert all(words in text_of(conditions) for words in ("2027 年及以后毕业", "冲突", "合成大学", "确认版 1", "毕业时间", "不符的事实", "2025 年 6 月"))
    assert "有经历支持" in text and "目前未找到支持材料" in text and "加分项" in text
    assert "本次未参考偏好" in text and "本机模拟服务的结果" in text
    # 1 of 2 required at 80%, 0 of 1 preferred at 20%.
    assert '<p class="coverage-score">40</p>' in main and "不是录用的可能性" in text
    # No ids, raw codes or JSON in what is read.
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)
    assert not any(code in text for code in ("needs_confirmation", "no_evidence", "practice", "complete", "{"))


def test_preferences_are_listed_for_review_and_compared_only_when_chosen(inline, browser_a, recorder):
    preferences = {"cities_ideal": "上海", "work_style_no": "频繁出差", "less_content": "大量重复的表格整理"}
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **preferences).status_code == 303
    item = project(browser_a)
    without = task_of(review(browser_a, [item]))
    assert "本次不参考求职偏好" in browser_a.get(without).text
    assert start(browser_a, without).status_code == 303
    assert "本次未参考偏好" in browser_a.get(without).text and "<<<PREFERENCES_START>>>" not in recorder.prompts[1]

    chosen = task_of(review(browser_a, [item], use_preferences=True))
    _, ticked, page = confirm_form(browser_a, chosen)
    assert {"preferences:cities_ideal", "preferences:work_style_no", "preferences:less_content"} <= set(ticked)
    narrower = task_of(start(browser_a, chosen, sends=[key for key in ticked if key != "preferences:less_content"]))
    assert start(browser_a, narrower).status_code == 303
    analysis = recorder.prompts[-1]
    result = text_of(main_of(browser_a.get(narrower).text))

    assert "上海" in analysis and "频繁出差" in analysis and "大量重复的表格整理" not in analysis
    # The posting names Shanghai; it says nothing about travelling.
    assert "目标城市（理想） 一致" in result and "工作方式（不能接受） 岗位未说明" in result
    assert "不是硬性筛选" in result
    # "Not stated" says the posting leaves the condition open, nothing more (R7).
    assert "岗位未明确说明这一条件" in result and "需要时可向招聘方确认" in result


def test_preferences_changed_after_the_review_are_not_sent(inline, browser_a, recorder):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", cities_ideal="上海").status_code == 303
    task = task_of(review(browser_a, [project(browser_a)], use_preferences=True))
    assert browser_a.submit("/job-preferences/edit", re.search(r'action="(/job-preferences/[^"]+/save)"', browser_a.get("/job-preferences/edit").text).group(1), cities_ideal="杭州").status_code == 303
    assert start(browser_a, task).status_code == 303
    page = text_of(browser_a.get(task).text)

    assert "求职偏好：选用的求职偏好已修改" in page and "已交给服务的请求：0 次" in page
    assert recorder.prompts == []


def test_a_degraded_posting_never_looks_like_a_complete_analysis(inline, app, browser_a):
    use_provider(app, FailingProvider(AIProviderTimeoutError("synthetic"), at=1))
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    text = text_of(main_of(browser_a.get(task).text))

    assert "解析岗位时服务超时" in text and "按规则找到的技能词" in text and "Python" in text
    assert "招聘条件" not in text and "技能覆盖" not in text


def test_an_answer_with_a_made_up_quote_is_not_used(inline, app, browser_a):
    use_provider(app, AnsweringProvider(scripted(r1=[("S1", "actions", "用 Python 训练了深度学习模型", "practice")])))
    task = task_of(review(browser_a, [project(browser_a)]))
    assert start(browser_a, task).status_code == 303
    text = text_of(main_of(browser_a.get(task).text))

    assert "整份回答没有采用" in text and "经历支持" not in text and "技能覆盖" not in text


def test_user_and_model_text_is_never_run_as_html(inline, app, browser_a):
    item = project(browser_a, title="<script>alert(1)</script>项目", actions="用 Python <img src=x onerror=alert(1)> 清洗数据")
    use_provider(app, AnsweringProvider(scripted(r1=[("S1", "actions", "用 Python <img src=x onerror=alert(1)> 清洗数据", "practice")])))
    task = task_of(review(browser_a, [item], POSTING + "<script>alert(2)</script>"))
    reviewed = browser_a.get(task).text
    assert start(browser_a, task).status_code == 303
    result = browser_a.get(task).text

    for page in (reviewed, result):
        assert "<script>alert" not in page and "<img src=x" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;项目" in page

    assert "&lt;img src=x onerror=alert(1)&gt;" in result


def test_an_analysis_writes_nothing_to_the_library_and_logs_no_text(inline, app, browser_a, recorder, caplog):
    caplog.set_level(logging.DEBUG)
    item = project(browser_a, title=f"合成{SENTINEL}")

    def everything():
        with app.state.library.database.transaction() as db:
            return {table.name: db.execute(select(table)).all() for table in LibraryBase.metadata.sorted_tables}

    before = everything()
    task = task_of(review(browser_a, [item], POSTING + SENTINEL))
    assert start(browser_a, task).status_code == 303
    assert "岗位分析结果" in browser_a.get(task).text

    assert everything() == before
    assert SENTINEL not in caplog.text


def test_job_pages_are_not_kept_in_the_browser_cache(browser_a, recorder):
    task = task_of(review(browser_a, [project(browser_a)]))

    for path in ("/job-analysis", task, f"{task}/status"):
        assert browser_a.get(path).headers["cache-control"] == "no-store", path
