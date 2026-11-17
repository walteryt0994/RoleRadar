"""U3-D4c: 每日查新 through the pages and the app.

The user starts a watch on a kept summary of theirs after a review saying what it repeats, keeps and for how long, and
whether the app checks by itself (as the app was started, never more); runs it now; stops and clears it; adds a new
posting to 我的岗位 or no longer shows it, every new posting reachable a page at a time. Every write is a POST with the
session's token; another user's are not found. A watch shares its user's slot with the batches of checks: one run of a
user's at a time, from when it is queued until it ends. All data is synthetic; nothing leaves the process and no model
is called.
"""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.discovery import watches
from app.discovery.synthetic import LEAD_POSTINGS, LEADS
from app.discovery.web import SYNTHETIC_SOURCES
from app.library import job_watches
from app.library.models import JobPosting, JobSearch, JobWatch, JobWatchPosting, JobWatchSeen, User
from job_helpers import text_of
from test_job_apply_check import post
from test_job_record_pages import finished, inline, library  # noqa: F401 (fixtures)
from test_job_refresh_pages import QueuedExecutor
from watch_helpers import Watched, kept_summary, register_of

NEW_JOB = "c-new-bj"
OFF = "现在不会自动检查"


@pytest.fixture
def boards(app):
    """The synthetic sources the app's watches ask: the careers site, and the leads."""
    careers, leads = Watched(), Watched(LEADS, LEAD_POSTINGS)
    app.state.discovery.sources = replace(SYNTHETIC_SOURCES, make_register=lambda task: register_of(careers, leads))

    return careers, leads


def user_id(app, label="合成用户 A") -> str:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(User.id).where(User.display_label == label))


def summary_of(app, clock, label="合成用户 A") -> str:
    return kept_summary(app.state.library.database, user_id(app, label), clock.now())


def page(browser, summary_id, **query) -> str:
    suffix = "&".join(f"{name}={value}" for name, value in query.items())

    return text_of(browser.get(f"/job-records/searches/{summary_id}" + (f"?{suffix}" if suffix else "")).text)


def start(browser, app, summary_id):
    with app.state.library.database.transaction() as db:
        revision = db.get(JobSearch, summary_id).revision

    return post(browser, f"/job-records/searches/{summary_id}/watch", expected_revision=str(revision), notice_version=watches.NOTICE_VERSION)


def the_watch(app, summary_id) -> JobWatch | None:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(JobWatch).where(JobWatch.search_record_id == summary_id))


def items(app, watch_id) -> list[JobWatchPosting]:
    with app.state.library.database.transaction() as db:
        return job_watches.new_postings(db, watch_id, app.state.library.now())


def run_now(browser, summary_id, watch_id):
    return post(browser, f"/job-records/searches/{summary_id}/watch/run", watch_id=watch_id)


# --- starting it, and what the pages say --------------------------------------------------------------------------------


def test_starting_a_watch_says_what_it_does_builds_the_baseline_and_says_the_app_does_not_check_by_itself(app, browser_a, inline, boards, clock):
    summary_id = summary_of(app, clock)
    before = page(browser_a, summary_id)
    review = text_of(browser_a.get(f"/job-records/searches/{summary_id}/watch").text)

    assert "每天查新…" in before and OFF in before
    assert "合成招聘官网：检索词“数据分析”，筛选 城市 北京、求职类型 实习" in review and "初筛用的范围" in review
    assert "保留 14 天" in review and "停止并清除" in review and "5000" in review and OFF in review
    # Off, the pages never say the app checks every 24 hours.
    assert "每 24 小时" not in review and "每 24 小时" not in before

    response = start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)
    after = text_of(browser_a.get(response.headers["location"]).text)

    assert response.headers["location"].endswith("notice=watch_started#watch")
    assert "已开启每日查新，正在建立基准" in after and "基准已建立（3/3 次检索）" in after and "上次查新：开始于" in after
    assert watch.last_trigger == "enable" and watch.last_outcome == watches.COMPLETE and "现在没有新增岗位" in after

    records = text_of(browser_a.get("/job-records").text)
    assert "每日查新的新增岗位" in records and "新增 0 个" in records and OFF in records


def test_a_summary_kept_from_a_search_through_the_pages_is_watched_on_the_very_searches_it_made(app, browser_a, library, inline, boards, clock):
    careers, leads = boards
    url = finished(browser_a, library)
    assert browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}).status_code == 303

    with app.state.library.database.transaction() as db:
        summary = db.scalar(select(JobSearch))
        made = [
            (entry["source_id"], " ".join(entry["keywords"].split()), entry["filters"]["city"], entry["filters"]["job_type"], entry["filters"]["direction"])
            for entry in summary.summary["searches"]
        ]

    careers.calls.clear()
    leads.calls.clear()
    response = start(browser_a, app, summary.id)
    watch = the_watch(app, summary.id)
    sent = [
        (source, query.keywords, query.city, query.job_type, query.direction)
        for source, board in (("synthetic-careers", careers), ("synthetic-leads", leads))
        for query in board.searches()
    ]

    # The baseline asked each search the search made, once, with its words and filters as they were; nothing more.
    assert response.headers["location"].endswith("notice=watch_started#watch") and watch.last_outcome == watches.COMPLETE
    assert sorted(sent) == sorted(dict.fromkeys(made)) and len(watch.baselined) == len(dict.fromkeys(made))


def test_with_the_automatic_check_on_the_pages_say_every_24_hours_while_the_app_runs(app, browser_a, inline, boards, clock):
    app.state.discovery.update_seconds = 15 * 60
    summary_id = summary_of(app, clock)
    review = text_of(browser_a.get(f"/job-records/searches/{summary_id}/watch").text)

    assert "自动检查已开启：本机应用运行时，每 15 分钟看一次哪些查新到期，每个查新每 24 小时一轮；应用关闭期间不会查新" in review
    assert OFF not in review


def test_a_start_from_an_older_page_or_under_other_words_starts_nothing(app, browser_a, inline, boards, clock):
    summary_id = summary_of(app, clock)

    stale = post(browser_a, f"/job-records/searches/{summary_id}/watch", expected_revision="0", notice_version=watches.NOTICE_VERSION)
    other = post(browser_a, f"/job-records/searches/{summary_id}/watch", expected_revision="1", notice_version="watch-0")
    bare = browser_a.post(f"/job-records/searches/{summary_id}/watch", {"expected_revision": "1", "notice_version": watches.NOTICE_VERSION})

    assert (stale.status_code, other.status_code) == (409, 409) and "说明在你打开之后有了变化" in text_of(other.text)
    assert bare.status_code in (400, 403) and the_watch(app, summary_id) is None and boards[0].calls == []


def test_on_synthetic_sources_the_page_keeps_its_offline_words_and_takes_no_start_under_the_batchs(app, browser_a, inline, boards, clock):
    """U3-D6 close-out, F1: the words a synthetic set's page says are as before (本机合成数据，不联网; watch-1), never
    those of a real source; a start posted under the D6 batch's words (watch-batch-1) is not taken, and reads nothing."""
    summary_id = summary_of(app, clock)
    html = browser_a.get(f"/job-records/searches/{summary_id}/watch").text
    review = text_of(html)

    # The summary's two synthetic sources, named as the page always named them.
    assert "来源：合成招聘官网、合成搜索线索（本机合成数据，不联网）。发给来源的只有上面的检索词和筛选值" in review
    assert "这是真实来源" not in review and "读取批准" not in review
    assert f'name="notice_version" value="{watches.NOTICE_VERSION}"' in html and watches.NOTICE_VERSION != watches.LIVE_NOTICE_VERSION

    other = post(browser_a, f"/job-records/searches/{summary_id}/watch", expected_revision="1", notice_version=watches.LIVE_NOTICE_VERSION)

    assert other.status_code == 409 and "说明在你打开之后有了变化" in text_of(other.text)
    assert the_watch(app, summary_id) is None and boards[0].calls == []


# --- the new postings, a page at a time ------------------------------------------------------------------------------------


def test_every_new_posting_is_reachable_a_page_at_a_time_and_can_be_kept_or_no_longer_shown(app, browser_a, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)

    for number in range(25):
        careers.add(f"c-many-{number:02d}", title="数据分析实习生", company=f"合成公司{number:02d}")

    clock.advance(minutes=11)
    assert run_now(browser_a, summary_id, watch.id).headers["location"].endswith("notice=watch_run_started#watch")
    first, second = page(browser_a, summary_id), page(browser_a, summary_id, page=2)
    first_html = browser_a.get(f"/job-records/searches/{summary_id}").text
    second_html = browser_a.get(f"/job-records/searches/{summary_id}?page=2").text
    listed = items(app, watch.id)

    assert len(listed) == 25 and "第 1/2 页" in first and "第 2/2 页" in second
    assert first_html.count('/keep"') == 20 and second_html.count('/keep"') == 5
    # The 21st and the 22nd: on the second page, kept and no longer shown as any other.
    kept, gone = listed[20], listed[21]
    assert kept.facts["company"] in second and gone.facts["company"] in second

    response = post(browser_a, f"/job-records/watch-postings/{kept.id}/keep")
    posting_page = text_of(browser_a.get(response.headers["location"]).text)
    assert "已加入我的岗位：这是每日查新找到的岗位" in posting_page and "这个岗位来自每日查新" in posting_page

    dismissed = post(browser_a, f"/job-records/watch-postings/{gone.id}/dismiss")
    assert dismissed.headers["location"] == f"/job-records/searches/{summary_id}?notice=watch_dismissed#watch"
    assert len(items(app, watch.id)) == 23

    # Neither comes back, and a page of them handled is not found again.
    clock.advance(minutes=11)
    run_now(browser_a, summary_id, watch.id)
    assert len(items(app, watch.id)) == 23 and post(browser_a, f"/job-records/watch-postings/{kept.id}/keep").status_code == 404


def test_a_new_posting_whose_time_has_come_is_shown_and_kept_nowhere(app, browser_a, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)
    careers.add(NEW_JOB)
    clock.advance(minutes=11)
    run_now(browser_a, summary_id, watch.id)
    [item] = items(app, watch.id)

    clock.current = item.found_at + timedelta(days=14)
    browser = type(browser_a).signed_in(app, "合成用户 A")

    assert "现在没有新增岗位" in page(browser, summary_id)
    assert post(browser, f"/job-records/watch-postings/{item.id}/keep").status_code == 404

    with app.state.library.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 0


# --- stopping it -------------------------------------------------------------------------------------------------------


def test_stop_and_clear_says_what_goes_and_keeps_what_the_user_kept(app, browser_a, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)
    careers.add(NEW_JOB)
    careers.add("c-second")
    clock.advance(minutes=11)
    run_now(browser_a, summary_id, watch.id)
    post(browser_a, f"/job-records/watch-postings/{items(app, watch.id)[0].id}/keep")
    confirm = text_of(browser_a.get(f"/job-records/searches/{summary_id}/watch/stop").text)

    assert "还没处理的新增岗位 1 个" in confirm and "不进回收站，不能恢复" in confirm and "不再放行后续的来源调用" not in confirm

    response = post(browser_a, f"/job-records/searches/{summary_id}/watch/stop", watch_id=watch.id)
    after = text_of(browser_a.get(response.headers["location"]).text)

    assert "已停止并清除每日查新" in after and "每天查新…" in after and the_watch(app, summary_id) is None

    with app.state.library.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert db.scalar(select(func.count()).select_from(JobWatchSeen)) == db.scalar(select(func.count()).select_from(JobWatchPosting)) == 0


def test_while_a_run_is_let_go_the_stop_page_says_a_call_let_go_may_still_complete_and_is_not_kept(app, browser_a, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    shown = []

    def look(query):
        careers.on_search = None
        shown.append(text_of(browser_a.get(f"/job-records/searches/{summary_id}/watch/stop").text))

    careers.on_search = look
    start(browser_a, app, summary_id)

    assert "停止以后不再放行后续的来源调用；停止前已经放行的那一次可能仍会执行或完成，它的结果不会保存" in shown[0]


def test_a_summary_deleted_takes_its_watch_with_it_and_a_summary_restored_brings_none_back(app, browser_a, inline, boards, clock):
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    confirm = text_of(browser_a.get(f"/job-records/searches/{summary_id}/delete").text)

    assert "这份摘要开启了每日查新：它会一并停止并清除" in confirm and "恢复这份摘要也不会恢复查新" in confirm

    with app.state.library.database.transaction() as db:
        revision = db.get(JobSearch, summary_id).revision

    assert post(browser_a, f"/job-records/searches/{summary_id}/delete", expected_revision=str(revision)).status_code == 303
    assert the_watch(app, summary_id) is None

    with app.state.library.database.transaction() as db:
        revision = db.get(JobSearch, summary_id).revision

    assert post(browser_a, f"/job-records/searches/{summary_id}/restore", expected_revision=str(revision)).status_code == 303
    assert the_watch(app, summary_id) is None and "每天查新…" in page(browser_a, summary_id)


def test_a_watch_stopped_while_its_baseline_is_queued_calls_nothing_and_gives_its_slot_back(app, browser_a, boards, clock):
    careers, leads = boards
    queue = QueuedExecutor()
    app.state.discovery.executor = queue
    summary_id = summary_of(app, clock)

    assert start(browser_a, app, summary_id).headers["location"].endswith("notice=watch_started#watch")
    watch = the_watch(app, summary_id)
    assert app.state.discovery.batches.holds(user_id(app))

    post(browser_a, f"/job-records/searches/{summary_id}/watch/stop", watch_id=watch.id)
    queue.run_all()

    assert careers.calls == leads.calls == [] and not app.state.discovery.batches.holds(user_id(app))


def test_stopped_and_started_again_while_the_old_call_answers_the_old_run_writes_nothing_and_the_new_baseline_follows(app, browser_a, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    restarted = []

    def stop_and_start_again(query):
        careers.on_search = None
        old = the_watch(app, summary_id)
        post(browser_a, f"/job-records/searches/{summary_id}/watch/stop", watch_id=old.id)
        # Its user's slot is still the old run's: started, the new watch's baseline waits.
        restarted.append(start(browser_a, app, summary_id).headers["location"])

    careers.on_search = stop_and_start_again
    start(browser_a, app, summary_id)
    fresh = the_watch(app, summary_id)

    assert restarted == [f"/job-records/searches/{summary_id}?notice=watch_busy#watch"]
    assert fresh.last_started_at is None and fresh.baselined == [] and "基准还没建立" in page(browser_a, summary_id)
    assert not app.state.discovery.batches.holds(user_id(app))

    assert run_now(browser_a, summary_id, fresh.id).headers["location"].endswith("notice=watch_run_started#watch")
    assert len(the_watch(app, summary_id).baselined) == 3


# --- when it runs ------------------------------------------------------------------------------------------------------


def test_now_check_waits_10_minutes_and_the_automatic_check_runs_what_is_due_only_when_on(app, browser_a, inline, boards, clock):
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)
    searches = app.state.discovery

    assert run_now(browser_a, summary_id, watch.id).headers["location"].endswith("notice=watch_fresh#watch")

    clock.advance(hours=24)
    browser = type(browser_a).signed_in(app, "合成用户 A")
    searches.run_due_updates(app.state.library.database, app.state.library.now)
    # Off: nothing runs by itself.
    assert the_watch(app, summary_id).last_trigger == "enable"

    searches.update_seconds = 15 * 60
    searches.run_due_updates(app.state.library.database, app.state.library.now)
    assert (the_watch(app, summary_id).last_trigger, the_watch(app, summary_id).last_outcome) == ("scheduled", watches.COMPLETE)
    assert "（定时检查）" in page(browser, summary_id)


def test_a_watch_that_has_seen_5000_postings_starts_no_run_and_says_why(app, browser_a, inline, boards, clock):
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)

    with app.state.library.database.transaction() as db:
        db.add_all(
            JobWatchSeen(
                watch_id=watch.id, source_id="synthetic-careers", posting_id=f"c-old-{number:05d}", owner_id=watch.owner_id,
                job_key=None, first_seen_at=clock.now(), last_seen_at=clock.now(),
            )
            for number in range(job_watches.SEEN_THRESHOLD)
        )

    clock.advance(minutes=11)
    shown = page(browser_a, summary_id)

    assert "达到 5000 个，查新已停下" in shown and "/watch/run" not in browser_a.get(f"/job-records/searches/{summary_id}").text
    assert run_now(browser_a, summary_id, watch.id).headers["location"].endswith("notice=watch_full#watch")


def test_the_app_closing_during_a_watchs_last_call_frees_its_slot_and_the_next_check_catches_up(app, browser_a, inline, boards, clock):
    careers, leads = boards
    summary_id = summary_of(app, clock)
    searches = app.state.discovery

    def close_the_app(query):
        leads.on_search = None
        searches.stopping.set()

    leads.on_search = close_the_app
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)

    # Its baseline was being built: nothing of it is written, its token and its user's slot are given back.
    assert (watch.last_outcome, watch.run_token, watch.baselined, watch.attempted_at) == (watches.INTERRUPTED, None, [], None)
    assert not searches.batches.holds(user_id(app)) and "中断（应用关闭或出错）：这一轮什么都没有写" in page(browser_a, summary_id)

    # Started again (a new app's stop signal is clear), the automatic check builds the baseline at once.
    searches.stopping.clear()
    searches.update_seconds = 15 * 60
    searches.run_due_updates(app.state.library.database, app.state.library.now)
    assert the_watch(app, summary_id).last_outcome == watches.COMPLETE and len(the_watch(app, summary_id).baselined) == 3


# --- the user's own -----------------------------------------------------------------------------------------------------


def test_another_users_summary_watch_and_new_postings_are_not_found(app, browser_a, browser_b, inline, boards, clock):
    careers, _ = boards
    summary_id = summary_of(app, clock)
    start(browser_a, app, summary_id)
    watch = the_watch(app, summary_id)
    careers.add(NEW_JOB)
    clock.advance(minutes=11)
    run_now(browser_a, summary_id, watch.id)
    [item] = items(app, watch.id)

    assert browser_b.get(f"/job-records/searches/{summary_id}/watch").status_code == 404
    assert post(browser_b, f"/job-records/searches/{summary_id}/watch", expected_revision="2", notice_version=watches.NOTICE_VERSION).status_code == 404
    assert run_now(browser_b, summary_id, watch.id).status_code == 404
    assert post(browser_b, f"/job-records/searches/{summary_id}/watch/stop", watch_id=watch.id).status_code == 404
    assert post(browser_b, f"/job-records/watch-postings/{item.id}/keep").status_code == 404
    assert post(browser_b, f"/job-records/watch-postings/{item.id}/dismiss").status_code == 404
    assert the_watch(app, summary_id) is not None and len(items(app, watch.id)) == 1
