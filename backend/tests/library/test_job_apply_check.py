"""U3-D4a: 再核对一次 and 申请前核对 from a kept posting's record, through the pages.

A check writes what its source said, apart from the facts kept and from the user's decision, which it never changes;
"准备申请" is the user's own decision, never that anything was checked or applied for. 申请前核对 shows the text it read
to the session that asked only, for thirty minutes at most and never past the record's deletion, and the text is kept
nowhere: not in the database, not in the export. A record changed while its check runs gets nothing; an older page
starts nothing; only synthetic sources are checked, the server deciding. The search and the checks run on synthetic
postings with the app's simulated decisions and analyses; nothing leaves the process and no model is called.
"""
import json
import re
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.discovery import records, updates
from app.discovery.sources import SourceTimeout
from app.discovery.web import SYNTHETIC_SOURCES
from app.library.models import AuditEvent, JobAnalysis, JobPosting, JobPostingRecheck, JobUpdateRun
from job_helpers import InlineExecutor, text_of  # noqa: F401
from test_discovery_pages import stored
from test_job_record_pages import exported, finished, inline, keep, library, rows  # noqa: F401 (fixtures)
from update_helpers import Board, register_with

MARKER = "合成正文里的一句新要求-只临时显示-d4a"


@pytest.fixture
def board(app):
    """The synthetic official source the search and the checks of the app both ask."""
    board = Board()
    app.state.discovery.sources = replace(SYNTHETIC_SOURCES, make_register=lambda task: register_with(board))

    return board


def post(browser, path, **fields):
    return browser.post(path, {"csrf_token": browser.csrf_token(), **fields})


def kept_result(app, browser, item, **fields):
    """A finished search's first result, kept (with a decision when fields say one): its result and record."""
    url = finished(browser, item)
    result = stored(app, browser, url).finished.outcome.results[0]
    assert keep(browser, url, result, **fields).status_code == 303
    [record] = [row for row in rows(app, JobPosting) if row.posting_id == result.ref.posting_id]

    return url, result, record


def current(app, record_id) -> JobPosting:
    with app.state.library.database.transaction() as db:
        return db.get(JobPosting, record_id)


def check(browser, app, record_id, action="recheck", revision=None):
    revision = current(app, record_id).revision if revision is None else revision

    return post(browser, f"/job-records/postings/{record_id}/{action}", revision=str(revision))


def page(browser, record_id) -> str:
    return text_of(browser.get(f"/job-records/postings/{record_id}").text)


# --- 再核对一次 ---------------------------------------------------------------------------------------------------------


def test_a_recheck_writes_what_the_source_said_and_never_the_decision(app, browser_a, library, inline, board):
    _, result, record = kept_result(app, browser_a, library, action="decide", decision="apply")
    before = page(browser_a, record.id)

    assert "准备申请" in before and "再核对一次" in before and "申请前核对" in before
    assert "“准备申请”是你自己的决定：不代表已经投递，也不代表岗位已经核实" in before

    response = check(browser_a, app, record.id)
    after = text_of(browser_a.get(response.headers["location"]).text)
    [made] = rows(app, JobPostingRecheck)

    assert response.status_code == 303 and response.headers["location"].endswith("?notice=rechecked")
    assert made.status == "open" and current(app, record.id).decision == "apply"
    assert "已核对一次" in after and "你的决定没有变" in after and "再核对一次 · 记录于" in after
    assert re.search(r"核对时仍公开发布（不代表企业还有名额）（来源回答 ", after)
    assert "已投递" not in after and "已验证" not in after


def test_a_page_that_saw_an_older_state_starts_no_check(app, browser_a, library, inline, board):
    _, _, record = kept_result(app, browser_a, library)
    asked = len(board.calls)

    response = check(browser_a, app, record.id, revision=current(app, record.id).revision - 1)

    assert response.status_code == 409 and "这条记录在你打开页面之后变了" in text_of(response.text)
    assert rows(app, JobUpdateRun) == [] and len(board.calls) == asked


# --- 申请前核对 and its text --------------------------------------------------------------------------------------------


def test_the_apply_check_shows_the_text_to_its_session_only_and_keeps_it_nowhere(app, browser_a, browser_b, library, inline, board):
    _, result, record = kept_result(app, browser_a, library)
    posting = result.ref.posting_id
    board.revise(posting, text=board.text_of(posting) + "\n" + MARKER)
    seconds = [5000.0]
    app.state.discovery.apply_checks.clock = lambda: seconds[0]

    response = check(browser_a, app, record.id, "apply-check")
    location = response.headers["location"]
    shown = text_of(browser_a.get(location).text)

    assert response.status_code == 303 and re.fullmatch(rf"/job-records/postings/{record.id}/apply-check/[\w-]+", location)
    assert MARKER in shown and "没有投递" in shown and "你的决定：没有决定" in shown and "RoleRadar 不替你提交" in shown
    assert "核对时仍公开发布" in shown and "已投递" not in shown and "已验证" not in shown
    [made] = rows(app, JobPostingRecheck)
    assert made.status == "open" and {"field": "text", "before": None, "after": None} not in made.changes

    # Another user, or another session of the same user: nothing.
    assert browser_b.get(location).status_code == 404
    again = type(browser_a).signed_in(app, "合成用户 A")
    assert again.get(location).status_code == 404 and "已经不在了" in text_of(again.get(location).text)

    # The text is nowhere it was written: not a check, a run, an event, the record, nor the export.
    with app.state.library.database.transaction() as db:
        written = json.dumps(
            [
                [[row.facts, row.changes, row.evidence, row.reason] for row in db.scalars(select(JobPostingRecheck))],
                [[row.trigger, row.outcome, row.reason] for row in db.scalars(select(JobUpdateRun))],
                [row.action for row in db.scalars(select(AuditEvent))],
                [row.facts for row in db.scalars(select(JobPosting))],
            ],
            ensure_ascii=False,
        )

    data, jobs = exported(browser_a)

    for text in (written, json.dumps(data, ensure_ascii=False), jobs):
        assert MARKER not in text

    [entry] = data["job_records"]["postings"]
    assert data["format_version"] == 14 and [item["trigger"] for item in entry["rechecks"]] == ["apply"]

    # Thirty minutes from when it was read, a look never keeping it longer.
    seconds[0] += updates.TEXT_TTL_SECONDS - 60
    assert browser_a.get(location).status_code == 200
    seconds[0] += 60
    assert browser_a.get(location).status_code == 404 and len(app.state.discovery.apply_checks) == 0


def test_deleting_the_record_ends_its_text_at_once_and_restoring_never_brings_it_back(app, browser_a, library, inline, board):
    _, _, record = kept_result(app, browser_a, library)
    location = check(browser_a, app, record.id, "apply-check").headers["location"]
    assert browser_a.get(location).status_code == 200

    assert post(browser_a, f"/job-records/postings/{record.id}/delete", expected_revision=str(current(app, record.id).revision)).status_code == 303
    assert browser_a.get(location).status_code == 404 and len(app.state.discovery.apply_checks) == 0

    assert post(browser_a, f"/job-records/postings/{record.id}/restore", expected_revision=str(current(app, record.id).revision)).status_code == 303
    assert current(app, record.id).deleted_at is None and browser_a.get(location).status_code == 404


def test_a_failed_check_is_to_be_confirmed_never_verified_and_keeps_the_saved_entry(app, browser_a, library, inline, board, clock):
    _, result, record = kept_result(app, browser_a, library)
    assert check(browser_a, app, record.id).status_code == 303
    # Two checks are never at one instant: the later one is the latest.
    clock.advance(minutes=1)
    board.faults[("read", result.ref.posting_id)] = SourceTimeout("synthetic")

    shown = text_of(browser_a.get(check(browser_a, app, record.id, "apply-check").headers["location"]).text)
    now = page(browser_a, record.id)

    assert "状态待确认：核对没有得到回答" in shown and "来源没有在时限内回答" in shown and "这次没有读到岗位正文" in shown
    assert record.facts["apply_url"] in shown and "保存时记下的，保留给你" in shown
    assert "仍公开发布" not in shown and "已验证" not in shown and "已投递" not in shown
    assert now.count("状态待确认：最近一次核对") == 1 and "上次成功核对是" in now


def test_a_record_changed_while_its_check_runs_keeps_nothing_and_shows_no_text(app, browser_a, library, inline, board):
    _, _, record = kept_result(app, browser_a, library)
    database = app.state.library.database

    def another_tab(operation):
        if operation == "read":
            with database.transaction() as db:
                records.decide_on_kept(db, record.owner_id, record.id, db.get(JobPosting, record.id).revision, "skip", [], "", app.state.library.now())

    board.during = another_tab
    response = check(browser_a, app, record.id, "apply-check")

    assert response.status_code == 409 and "这次核对的结果没有保存" in text_of(response.text)
    assert rows(app, JobPostingRecheck) == [] and len(app.state.discovery.apply_checks) == 0
    assert current(app, record.id).decision == "skip"
    [run] = rows(app, JobUpdateRun)
    assert (run.trigger, run.outcome, run.reason) == ("apply", "discarded", "record_changed")

    # The discarded check gave its reservation back: the next one runs and shows its text.
    board.during = None
    response = check(browser_a, app, record.id, "apply-check")
    assert response.status_code == 303 and browser_a.get(response.headers["location"]).status_code == 200


# --- what the server lets check ---------------------------------------------------------------------------------------------


def test_only_synthetic_sources_are_checked_and_a_source_not_run_on_now_is_asked_nothing(app, browser_a, library, inline, board):
    _, _, record = kept_result(app, browser_a, library)
    asked = len(board.calls)
    app.state.discovery.sources = replace(app.state.discovery.sources, keep=frozenset())

    assert check(browser_a, app, record.id).status_code == 303
    [made] = rows(app, JobPostingRecheck)
    assert (made.status, made.reason) == ("unanswered", "source_unavailable") and len(board.calls) == asked
    assert "这个来源现在没有接入，没有发出请求" in page(browser_a, record.id)

    with app.state.library.database.transaction() as db:
        db.get(JobPosting, record.id).source_access = "real"

    shown = page(browser_a, record.id)
    response = check(browser_a, app, record.id)

    assert updates.NOT_TRACKABLE in shown and "再核对一次</button>" not in browser_a.get(f"/job-records/postings/{record.id}").text
    assert response.status_code == 403 and updates.NOT_TRACKABLE in text_of(response.text)
    assert len(rows(app, JobPostingRecheck)) == 1 and len(board.calls) == asked


# --- "准备申请" as a decision ---------------------------------------------------------------------------------------------


def test_apply_is_a_decision_of_its_own_with_no_reasons(app, browser_a, library, inline, board):
    _, _, record = kept_result(app, browser_a, library)
    path = f"/job-records/postings/{record.id}/decide"

    shown = page(browser_a, record.id)
    refused = post(browser_a, path, revision=str(current(app, record.id).revision), decision="apply", reason="other")
    assert "准备申请…" in shown and "只记下你的决定，不投递，也不核对来源" in shown
    assert refused.status_code == 422 and current(app, record.id).decision is None

    assert post(browser_a, path, revision=str(current(app, record.id).revision), decision="apply").status_code == 303
    listed = text_of(browser_a.get("/job-records").text)

    assert current(app, record.id).decision == "apply" and rows(app, JobPostingRecheck) == []
    assert re.search(r"准备申请\s*1 个", listed)


# --- an analysis kept of it ---------------------------------------------------------------------------------------------


def test_an_analysis_of_another_version_is_said_to_be_maybe_out_of_date_and_never_changed(app, browser_a, library, inline, board, clock):
    url, result, record = kept_result(app, browser_a, library)
    assert keep(browser_a, url, result, action="analysis").status_code == 303
    [analysis] = rows(app, JobAnalysis)

    assert check(browser_a, app, record.id).status_code == 303
    assert "结论可能过时" not in text_of(browser_a.get(f"/job-records/analyses/{analysis.id}").text)

    posting = result.ref.posting_id
    board.revise(posting, text=board.text_of(posting) + "\n" + MARKER)
    clock.advance(minutes=1)
    assert check(browser_a, app, record.id).status_code == 303
    [after] = rows(app, JobAnalysis)

    assert "结论可能过时" in text_of(browser_a.get(f"/job-records/analyses/{analysis.id}").text)
    assert "结论可能过时" in page(browser_a, record.id)
    assert (after.content, after.content_sha256, after.revision) == (analysis.content, analysis.content_sha256, analysis.revision)


# --- F1: the deadline two answers give, said as the check settled it ----------------------------------------------------


def test_the_apply_check_page_says_which_answer_gave_the_deadline_or_that_they_differed(app, browser_a, library, inline, board, clock):
    _, _, record = kept_result(app, browser_a, library)
    today = clock.now().astimezone().date()
    yesterday, ahead = today - timedelta(days=1), today + timedelta(days=30)

    # Only the check gives one, and it is past: closed on it, and the page says the same date and where it came from.
    board.read_deadline, board.check_deadline = None, yesterday
    shown = text_of(browser_a.get(check(browser_a, app, record.id, "apply-check").headers["location"]).text)

    assert f"来源已明确关闭：来源写的截止日期 {yesterday.isoformat()} 已过（核对时来源给出，详情里没有写）" in shown
    assert f"{yesterday.isoformat()}（核对时来源给出，详情里没有写）" in shown and ahead.isoformat() not in shown

    # The read and the check differ: to be confirmed, both said, neither taken for the deadline.
    clock.advance(minutes=1)
    board.read_deadline, board.check_deadline = ahead, yesterday
    shown = text_of(browser_a.get(check(browser_a, app, record.id, "apply-check").headers["location"]).text)
    now = page(browser_a, record.id)
    data, jobs = exported(browser_a)

    assert f"两次回答不一致：详情里写的是 {ahead.isoformat()}，核对时来源给出 {yesterday.isoformat()}；不据此判断" in shown
    assert "状态待确认" in shown and "已过" not in shown.split("截止时间和申请入口")[0]
    assert now.count("状态待确认：最近一次核对") == 1 and "截止日期不一致" in now
    assert "两次回答不一致（详情" in now and "两次回答不一致（详情" in jobs
    assert data["job_records"]["postings"][0]["rechecks"][-1]["facts"]["deadline_answers"] == {
        "read": ahead.isoformat(), "check": yesterday.isoformat()
    }


# --- F2: a text read before its record was deleted or its session ended is never shown -----------------------------------


def test_a_record_deleted_and_restored_before_its_text_is_published_never_shows_it(app, browser_a, library, inline, board, monkeypatch):
    _, _, record = kept_result(app, browser_a, library)
    store = app.state.discovery.apply_checks
    publish, interleaved = store.put, []

    def another_tab_first(user_id, session, record_id, finished):
        # The check is written; before its text is published, another tab deletes the record and restores it.
        for action in ("delete", "restore"):
            assert post(browser_a, f"/job-records/postings/{record_id}/{action}", expected_revision=str(current(app, record_id).revision)).status_code == 303

        interleaved.append(True)

        return publish(user_id, session, record_id, finished)

    monkeypatch.setattr(store, "put", another_tab_first)
    response = check(browser_a, app, record.id, "apply-check")

    assert interleaved == [True] and response.status_code == 409 and "不再显示" in text_of(response.text)
    assert len(store) == 0 and len(rows(app, JobPostingRecheck)) == 1 and current(app, record.id).deleted_at is None

    # A check begun after the restore shows its own text, as any other.
    monkeypatch.setattr(store, "put", publish)
    response = check(browser_a, app, record.id, "apply-check")
    assert response.status_code == 303 and browser_a.get(response.headers["location"]).status_code == 200


def test_a_session_ended_before_or_after_its_text_is_published_never_shows_it(app, browser_a, library, inline, board, monkeypatch):
    _, _, record = kept_result(app, browser_a, library)
    store, session = app.state.discovery.apply_checks, None
    publish = store.put

    def logged_out_first(user_id, token_hash, record_id, finished):
        nonlocal session
        session = token_hash
        # The same listeners logging out runs, run before the check's text is published.
        app.state.library.session_ended(token_hash)

        return publish(user_id, token_hash, record_id, finished)

    monkeypatch.setattr(store, "put", logged_out_first)
    response = check(browser_a, app, record.id, "apply-check")

    assert response.status_code == 409 and "不再显示" in text_of(response.text) and len(store) == 0
    # Nothing more begins, nor shows, for that session; a new sign-in checks as before.
    assert check(browser_a, app, record.id, "apply-check").status_code == 409 and len(store) == 0

    sessions = []

    def noted(user_id, token_hash, record_id, finished):
        sessions.append(token_hash)

        return publish(user_id, token_hash, record_id, finished)

    monkeypatch.setattr(store, "put", noted)
    again = type(browser_a).signed_in(app, "合成用户 A")
    response = check(again, app, record.id, "apply-check")
    location = response.headers["location"]
    assert response.status_code == 303 and again.get(location).status_code == 200 and sessions[-1] != session

    # Published, then the session ends: what was shown goes at once, and is never read again.
    app.state.library.session_ended(sessions[-1])
    assert again.get(location).status_code == 404 and len(store) == 0
