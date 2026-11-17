"""U3-D3a: keeping postings and a search's summary from a finished search, and the records kept, through the pages.

Nothing is kept until the user asks; what is kept of a posting is the search's in memory, never what a form says; every
entry refuses a search whose sources the server does not let keep; an expired, unfinished or other session's search
keeps nothing; a page that saw an older state changes nothing; the summary is a whitelist kept once; the records are
their owner's alone, and the recycle bin keeps them for seven days. The search runs on synthetic postings with the
app's simulated decisions and analyses; nothing leaves the process.
"""
import io
import json
import re
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.discovery import synthetic
from app.discovery.agent import ScriptedDecider
from app.discovery.contracts import EndReason, Tool
from app.discovery.sources import SourceTimeout
from app.discovery.synthetic import CAREERS
from app.discovery.web import SYNTHETIC_SOURCES
from app.library.models import JobFeedback, JobPosting, JobPostingCheck, JobSearch
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, text_of
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_pages import PREFERENCES, PROJECT, act, confirm, finish, review, search, search_of, stored

TABLES = (JobPosting, JobPostingCheck, JobFeedback, JobSearch)


@pytest.fixture
def inline(app):
    app.state.discovery.executor = InlineExecutor()

    return app.state.discovery


@pytest.fixture
def library(browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303

    return confirmed(browser_a, **PROJECT)


def finished(browser, library) -> str:
    """A search on the synthetic postings, run to its end; its page's address."""
    url = search_of(review(browser, sources=[library], use_preferences=True))
    assert confirm(browser, url).status_code == 303

    return url


def counts(app) -> tuple[int, ...]:
    with app.state.library.database.transaction() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in TABLES)


def rows(app, model) -> list:
    with app.state.library.database.transaction() as db:
        return list(db.scalars(select(model)))


def keep(browser, page, item, *, action="keep", record="", revision="", **fields):
    """Post the keep form of one posting of the search at page, with whatever else a form may add."""
    data = {
        "csrf_token": browser.csrf_token(),
        "source": item.ref.source_id,
        "posting": item.ref.posting_id,
        "action": action,
        "record": record,
        "revision": revision,
        **fields,
    }

    return browser.post(f"{page}/keep", data)


def keep_link(item) -> str:
    return f"/keep?source={item.ref.source_id}&amp;posting={item.ref.posting_id}"


# --- nothing until the user asks ------------------------------------------------------------------------------------


def test_a_finished_search_keeps_nothing_until_the_user_asks(app, browser_a, library, inline):
    url = finished(browser_a, library)
    task = stored(app, browser_a, url)
    result, lead = task.finished.outcome.results[0], task.finished.outcome.leads[0]
    page = browser_a.get(url).text
    keep_page = browser_a.get(f"{url}/keep?source={result.ref.source_id}&posting={result.ref.posting_id}")

    # Seeing the results and the keep page writes nothing.
    assert counts(app) == (0, 0, 0, 0) and keep_page.status_code == 200
    assert keep_link(result) in page and keep_link(lead) in page and "加入我的岗位或做决定…" in page
    assert "保存这次查找的摘要" in page and "不会自动保存，也不会投递" in text_of(page)
    assert "只有你点了下面的按钮才会保存" in text_of(keep_page.text)

    # Keeping a posting keeps it, not the search's summary; keeping the summary keeps no posting.
    response = keep(browser_a, url, result)
    assert (response.status_code, response.headers["location"]) == (303, f"{url}?notice=kept")
    assert counts(app)[0] == 1 and counts(app)[3] == 0
    assert "已加入我的岗位：没有决定" in text_of(browser_a.get(url).text)

    response = browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""})
    assert (response.status_code, response.headers["location"]) == (303, f"{url}?notice=summary")
    assert counts(app)[0] == 1 and counts(app)[3] == 1
    assert "这次查找的摘要已保存" in browser_a.get(url).text


def test_what_is_kept_comes_from_the_search_never_from_the_form(app, browser_a, library, inline, clock):
    url = finished(browser_a, library)
    task = stored(app, browser_a, url)
    result = task.finished.outcome.results[0]
    clock.advance(minutes=10)
    forged = dict(title="伪造的职位", company="伪造的公司", url="https://forged.example/jobs", access="synthetic", checked_at="2030-01-01")

    assert keep(browser_a, url, result, **forged).status_code == 303

    [row] = rows(app, JobPosting)
    [check] = rows(app, JobPostingCheck)

    assert (row.facts["title"], row.facts["company"], row.facts["url"]) == (result.title, result.company, result.url)
    assert "伪造" not in json.dumps(row.facts, ensure_ascii=False) and "forged" not in json.dumps(row.facts)
    # Its check is the source's answer in the search, open as a result's last answer is (F1: from the search's own
    # status, never from the time); the keep time is the user's, ten minutes later.
    assert check.checked_at == result.checked_at and row.kept_at == clock.now() != check.checked_at
    assert result.answered_status.value == check.status == "open"
    # Nothing of the analysis, its category or what to confirm before applying (made with the preferences) is kept.
    assert {"analysis", "category", "first_checks", "notes"}.isdisjoint(row.facts)
    assert "最早 2026 年 7 月入职" not in json.dumps(row.facts, ensure_ascii=False)

    # A posting the search did not find, or one named by another source's id, is not kept.
    for source, posting in (("synthetic-careers", "not-in-this-search"), ("synthetic-leads", result.ref.posting_id)):
        response = browser_a.post(
            f"{url}/keep", {"csrf_token": browser_a.csrf_token(), "source": source, "posting": posting, "action": "keep", "record": "", "revision": ""}
        )
        assert response.status_code == 404

    assert counts(app) == (1, 1, 0, 0)


# --- deciding, the page seen, feedback ------------------------------------------------------------------------------


def test_deciding_on_both_pages_names_what_was_seen_and_an_older_page_changes_nothing(app, browser_a, library, inline, clock):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    clock.advance(minutes=10)

    response = keep(browser_a, url, result, action="decide", decision="skip", reason=["place", "type"], note="地点太远")
    assert (response.status_code, response.headers["location"]) == (303, f"{url}?notice=decided")
    assert "已加入我的岗位：跳过" in text_of(browser_a.get(url).text)

    # The same form again: its page saw no record, and one was kept since.
    stale = keep(browser_a, url, result, action="decide", decision="favorite")
    assert stale.status_code == 409 and "这条记录在你打开页面之后变了" in text_of(stale.text)

    [row] = rows(app, JobPosting)
    assert (row.decision, row.reasons, row.note) == ("skip", ["type", "place"], "地点太远")

    record = f"/job-records/postings/{row.id}"
    page = text_of(browser_a.get(record).text)
    # When it was kept, and when its source answered, side by side and never one for the other.
    assert "保存于" in page and "你加入我的岗位的时间，不是来源核对的时间" in page and "来源回答：" in page
    assert "原因：类型不对（比如不是实习）、地点不对" in page and "备注：地点太远" in page

    changed = browser_a.post(f"{record}/decide", {"csrf_token": browser_a.csrf_token(), "revision": row.revision, "decision": "favorite", "reason": ["company"]})
    assert (changed.status_code, changed.headers["location"]) == (303, f"{record}?notice=decided")
    older = browser_a.post(f"{record}/decide", {"csrf_token": browser_a.csrf_token(), "revision": row.revision, "decision": "defer"})
    assert older.status_code == 409

    [row] = rows(app, JobPosting)
    assert (row.decision, row.reasons, row.note) == ("favorite", ["company"], None)

    feedback = browser_a.post(f"{record}/feedback", {"csrf_token": browser_a.csrf_token(), "revision": row.revision, "field": "place", "feedback_reason": "wrong", "note": "合成：其实在上海"})
    assert feedback.status_code == 303
    assert "地点：认错了：合成：其实在上海" in text_of(browser_a.get(record).text)

    [row] = rows(app, JobPosting)
    undone = browser_a.post(f"{record}/decide", {"csrf_token": browser_a.csrf_token(), "revision": row.revision, "decision": ""})
    assert undone.status_code == 303 and rows(app, JobPosting)[0].decision is None

    # A form that is not one of the choices changes nothing.
    [row] = rows(app, JobPosting)
    rejected = browser_a.post(f"{record}/decide", {"csrf_token": browser_a.csrf_token(), "revision": row.revision, "decision": "skip", "reason": "reachable"})
    assert rejected.status_code == 422 and rows(app, JobPosting)[0].revision == row.revision


def test_a_post_without_the_sessions_token_keeps_nothing(app, browser_a, library, inline):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    data = {"source": result.ref.source_id, "posting": result.ref.posting_id, "action": "keep", "record": "", "revision": ""}

    assert browser_a.post(f"{url}/keep", data).status_code == 403
    assert browser_a.post(f"{url}/summary", {"record": "", "revision": ""}).status_code == 403
    assert counts(app) == (0, 0, 0, 0)


# --- what may be kept, and from which search -----------------------------------------------------------------------------


def test_every_entry_refuses_a_search_whose_sources_may_not_be_kept(make_config, clock):
    application = create_app(make_config(), clock=clock, discovery_sources=replace(SYNTHETIC_SOURCES, keep=frozenset()))

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        application.state.discovery.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
        url = finished(browser, confirmed(browser, **PROJECT))
        task = stored(application, browser, url)
        result = task.finished.outcome.results[0]
        page = browser.get(url).text

        assert "保存或做决定…" not in page and "保存这次查找的摘要" not in page
        assert "这次查找用到的来源没有保存许可，结果只临时显示" in text_of(page)
        assert browser.get(f"{url}/keep?source={result.ref.source_id}&posting={result.ref.posting_id}").status_code == 403

        for action, fields in (("keep", {}), ("decide", {"decision": "skip"}), ("feedback", {"field": "place", "feedback_reason": "wrong"})):
            response = keep(browser, url, result, action=action, **fields)
            assert response.status_code == 403 and "没有保存许可" in text_of(response.text), action

        assert browser.post(f"{url}/summary", {"csrf_token": browser.csrf_token(), "record": "", "revision": ""}).status_code == 403
        assert counts(application) == (0, 0, 0, 0)


def test_an_expired_unfinished_or_other_sessions_search_keeps_nothing(app, browser_a, browser_b, library, inline):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    data = {"source": result.ref.source_id, "posting": result.ref.posting_id, "action": "keep", "record": "", "revision": ""}

    # Another user's session cannot even find it.
    assert browser_b.post(f"{url}/keep", {"csrf_token": browser_b.csrf_token(), **data}).status_code == 404
    assert browser_b.post(f"{url}/summary", {"csrf_token": browser_b.csrf_token(), "record": "", "revision": ""}).status_code == 404

    # A search under review has nothing to keep yet.
    reviewing = search_of(review(browser_a, sources=[library], use_preferences=True))
    response = browser_a.post(f"{reviewing}/keep", {"csrf_token": browser_a.csrf_token(), **data})
    assert response.status_code == 409 and "结束以后才能保存" in text_of(response.text)

    # Past its time the result is gone, and so is keeping it.
    store = app.state.discovery.store
    store.clock = lambda: 10**9

    response = browser_a.post(f"{url}/keep", {"csrf_token": browser_a.csrf_token(), **data})
    assert response.status_code == 404 and "过期以后不能再保存" in text_of(response.text)
    assert browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}).status_code == 404
    assert counts(app) == (0, 0, 0, 0)


# --- the summary -------------------------------------------------------------------------------------------------------


def test_the_summary_is_a_whitelist_and_is_kept_once(app, browser_a, library, inline):
    url = finished(browser_a, library)
    task = stored(app, browser_a, url)
    post = {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}

    assert browser_a.post(f"{url}/summary", post).status_code == 303
    # The same page again: one is kept since, so nothing more is.
    assert browser_a.post(f"{url}/summary", post).status_code == 409

    [row] = rows(app, JobSearch)
    summary = json.dumps(row.summary, ensure_ascii=False)

    assert set(row.summary) == {
        "version", "mode", "stage", "cancelled_by", "sources", "scope", "experiences", "preference_fields", "recipient",
        "notice_version", "limits", "end", "personalised", "counts", "searches", "partial", "unsearched", "unjudged",
        "steps", "requests",
    }
    # Never a decider's own words, a posting's facts, an experience's or a preference's words: names and ids only.
    for step in task.finished.trace.steps:
        assert not step.note or step.note not in summary

    for item in (*task.finished.outcome.results, *task.finished.outcome.leads):
        assert item.title not in summary

    for words in (*PROJECT.values(), "最早 2026 年 7 月入职", "销售"):
        assert words not in summary

    assert row.summary["preference_fields"] == ["must_haves", "work_content_no"]
    assert [entry["item_id"] for entry in row.summary["experiences"]] == [library]
    assert row.summary["counts"]["results"] == len(task.finished.outcome.results)

    page = text_of(browser_a.get(f"/job-records/searches/{row.id}").text)
    assert "查找摘要" in page and "不含岗位原文、分析、经历内容、偏好原文和每一步的说明" in page
    assert "参考的偏好 现实必要条件" in page or "现实必要条件" in page


# --- the records and the recycle bin -------------------------------------------------------------------------------------


def test_the_records_are_their_owners_alone(app, browser_a, browser_b, library, inline):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    assert keep(browser_a, url, result, action="decide", decision="favorite").status_code == 303
    [row] = rows(app, JobPosting)
    record = f"/job-records/postings/{row.id}"

    assert result.title in browser_a.get("/job-records").text
    assert browser_b.get(record).status_code == 404 and result.title not in browser_b.get("/job-records").text

    for path, data in ((f"{record}/decide", {"decision": "skip"}), (f"{record}/feedback", {"field": "place", "feedback_reason": "wrong"}), (f"{record}/delete", {})):
        response = browser_b.post(path, {"csrf_token": browser_b.csrf_token(), "revision": row.revision, "expected_revision": row.revision, **data})
        assert response.status_code == 404, path

    assert rows(app, JobPosting)[0].revision == row.revision and counts(app)[2] == 0


def test_a_deleted_record_waits_seven_days_in_the_bin_then_is_neither_restorable_nor_exported(app, browser_a, library, inline, clock):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    assert keep(browser_a, url, result, action="decide", decision="skip").status_code == 303
    [row] = rows(app, JobPosting)
    record = f"/job-records/postings/{row.id}"

    assert "删除这条岗位记录" in browser_a.get(f"{record}/delete").text
    assert browser_a.post(f"{record}/delete", {"csrf_token": browser_a.csrf_token(), "expected_revision": row.revision}).status_code == 303

    trash = text_of(browser_a.get("/experiences?tab=trash").text)
    assert "删除的岗位记录" in trash and result.title in trash and result.title not in browser_a.get("/job-records").text

    [row] = rows(app, JobPosting)
    restored = browser_a.post(f"{record}/restore", {"csrf_token": browser_a.csrf_token(), "expected_revision": row.revision})
    assert restored.status_code == 303 and rows(app, JobPosting)[0].deleted_at is None

    [row] = rows(app, JobPosting)
    assert browser_a.post(f"{record}/delete", {"csrf_token": browser_a.csrf_token(), "expected_revision": row.revision}).status_code == 303
    [row] = rows(app, JobPosting)
    clock.advance(days=7)
    # Seven days outlast the sign-in too.
    browser_a.sign_in("合成用户 A")

    assert result.title not in browser_a.get("/experiences?tab=trash").text
    assert browser_a.post(f"{record}/restore", {"csrf_token": browser_a.csrf_token(), "expected_revision": row.revision}).status_code == 404
    assert browser_a.get(f"{record}/purge").status_code == 404

    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    data = json.loads(next(archive.read(name) for name in archive.namelist() if name.endswith("/data.json")))

    # Format 10 since U3-D3b: the kept analyses too, none here.
    assert data["format_version"] == 14 and data["job_records"] == {"postings": [], "searches": [], "analyses": [], "recommendations": [], "watches": []}
    assert not any(name.endswith("/jobs.md") for name in archive.namelist())


def exported(browser) -> tuple[dict, str]:
    archive = zipfile.ZipFile(io.BytesIO(browser.get("/export/all.zip").content))
    data = json.loads(next(archive.read(name) for name in archive.namelist() if name.endswith("/data.json")))
    jobs = next((archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith("/jobs.md")), "")

    return data, jobs


# --- F1: a check said as it was, through the pages ------------------------------------------------------------------------


def test_a_posting_closed_at_its_last_answer_then_unanswered_is_never_said_to_be_open(app, browser_a, library, inline, clock):
    made = []

    def register(task):
        made.append(synthetic.register())

        return made[-1]

    def check_fails_now(observation):
        # The source answered closed before; now its check of the same posting gets no answer.
        made[-1].adapter(CAREERS).faults[("check", "c-closed")] = SourceTimeout("synthetic")
        clock.advance(minutes=1)

        return act(Tool.CHECK, "c-closed")

    app.state.discovery.sources = replace(SYNTHETIC_SOURCES, make_register=register)
    app.state.discovery.decider_for = lambda guarded: ScriptedDecider(
        [
            search("北京", words="数据分析 实习生"),
            act(Tool.READ, "c-closed"),
            act(Tool.CHECK, "c-closed"),
            check_fails_now,
            finish(EndReason.ENOUGH),
            finish(EndReason.SCOPE_DONE),
        ]
    )
    url = search_of(review(browser_a))
    assert confirm(browser_a, url).status_code == 303

    lead = next(item for item in stored(app, browser_a, url).finished.outcome.leads if item.ref.posting_id == "c-closed")
    keep_page = text_of(browser_a.get(f"{url}/keep?source={CAREERS}&posting=c-closed").text)

    assert "核对时已关闭" in keep_page and "最近一次核对没有得到回答" in keep_page and "状态待确认" in keep_page
    assert "核对时仍公开发布" not in keep_page

    assert keep(browser_a, url, lead).status_code == 303
    [row] = rows(app, JobPosting)
    statuses = sorted((check.status, check.checked_at) for check in rows(app, JobPostingCheck))

    assert statuses == sorted([("closed", lead.checked_at), ("unanswered", lead.check_failed_at)])

    record = text_of(browser_a.get(f"/job-records/postings/{row.id}").text)
    listed = text_of(browser_a.get("/job-records").text)
    data, jobs = exported(browser_a)
    [entry] = data["job_records"]["postings"]

    for text in (record, listed, entry["check_state"], jobs):
        assert "状态待确认：最近一次核对" in text and "核对时仍公开发布" not in text, text

    assert "核对没有得到回答" in record and "核对时已关闭" in record
    assert [check["status"] for check in entry["checks"]] == ["closed", "unanswered"]


# --- F2, F3: a kept summary said as the search's pages said it ---------------------------------------------------------


def test_a_kept_summary_says_each_search_and_the_whole_scope_as_the_search_did(app, browser_a, library, inline):
    url = finished(browser_a, library)
    assert browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}).status_code == 303
    [row] = rows(app, JobSearch)
    page = text_of(browser_a.get(f"/job-records/searches/{row.id}").text)

    # F3: ideal Beijing, Shanghai acceptable, Shenzhen not, and whether values outside were searched too.
    assert "城市 北京（理想）、上海；只看所列的；不能接受：深圳（不会检索）" in page
    assert "求职类型 实习（理想）；只看所列的" in page
    assert "岗位方向 数据分析（理想）；所列以外的也查（仍然排除不能接受的）" in page

    # F2: a search answered with none, one that failed, one answered after the search had stopped, one from the list
    # read before and one read in part: each said as the result page said it.
    base = dict(row.summary["searches"][0])
    searches = [
        dict(base, found=0, outcome="done", reused=False, partial=False, listed=None, failure=None),
        dict(base, found=0, outcome="failed", reused=False, partial=False, listed=None, failure="来源没有按时回答"),
        dict(base, found=0, outcome="stopped", reused=False, partial=False, listed=None, failure=None),
        dict(base, found=3, outcome="done", reused=True, partial=False, listed=None, failure=None),
        dict(base, found=5, outcome="done", reused=False, partial=True, listed=300, failure=None),
    ]

    with app.state.library.database.transaction() as db:
        db.get(JobSearch, row.id).summary = dict(row.summary, searches=searches)

    page = browser_a.get(f"/job-records/searches/{row.id}").text
    lines = [text_of(line) for line in re.findall(r"<li>(.*?)</li>", page[page.index(">检索<"):page.index(">结果<")], re.S)]

    assert len(lines) == 5
    assert lines[0].endswith("返回 0 个")
    assert lines[1].endswith("没有完成（来源没有按时回答）") and "返回" not in lines[1]
    assert lines[2].endswith("查找停止后才回来，没有采用") and "返回" not in lines[2]
    assert lines[3].endswith("从这次已经读过的列表筛出 3 个（没有再发请求）")
    assert lines[4].endswith("返回 5 个；列表只读了前 300 个，没有读完，这个条件不算查过")


def test_the_jobs_area_has_its_records_beside_the_search(browser_a):
    page = browser_a.get("/job-records").text
    links = re.findall(r'<a href="([^"]+)"( aria-current="page")?>([^<]+)</a>', page[page.index('<nav class="subnav"'):])[:3]

    assert links == [("/job-analysis", "", "岗位分析"), ("/discover", "", "找岗位"), ("/job-records", ' aria-current="page"', "岗位记录")]
    assert "还没有加入我的岗位的岗位" in page and "还没有保存的查找摘要" in page
