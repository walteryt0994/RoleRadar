"""U3-D4b-1: 现在核对 and the automatic check, through the pages and the app.

现在核对 starts one batch of the user's own tracked postings (a POST with the session's token), never a second while one
runs; the records page says what is due now apart from what the last batch actually did, and that nothing is checked
while the app is not running. The automatic check is off unless the app is started for it, refused for any set of
sources not all synthetic, and a run left open when the app stopped is said interrupted when it starts again. Synthetic
postings only; nothing leaves the process and no model is called.
"""
from concurrent.futures import Future
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.discovery import updates
from app.discovery.contracts import Access, PostingRef
from app.discovery.synthetic import CAREERS, POSTINGS
from app.discovery.web import SYNTHETIC_SOURCES
from app.library.errors import ConfigError
from app.library.models import JobPostingRecheck, JobUpdateRun, User
from app.web_main import create_app
from job_helpers import confirmed, text_of
from test_discovery_pages import PREFERENCES, PROJECT, stored
from test_job_apply_check import board, post  # noqa: F401 (fixture)
from test_job_record_pages import finished, inline, keep, library, rows  # noqa: F401 (fixtures)
from update_helpers import Board, kept_posting, register_with


def kept_results(app, browser, item) -> list:
    """A finished search's results, every one kept."""
    url = finished(browser, item)
    results = stored(app, browser, url).finished.outcome.results

    for result in results:
        assert keep(browser, url, result).status_code == 303

    return results


def refresh(browser):
    return post(browser, "/job-records/refresh")


def listed(browser) -> str:
    return text_of(browser.get("/job-records").text)


def batches(app) -> list[JobUpdateRun]:
    return [run for run in rows(app, JobUpdateRun) if run.trigger in ("manual", "scheduled")]


def test_now_check_runs_one_batch_and_the_page_keeps_what_is_due_apart_from_the_last_batch(app, browser_a, library, inline, board, clock):
    results = kept_results(app, browser_a, library)
    count = len(results)
    before = listed(browser_a)

    # Just checked by the search: none is due, and 现在核对 waits 10 minutes.
    assert f"现在跟踪 {count} 个" in before and "现在没有超过 24 小时没核对的岗位" in before
    assert "每 24 小时的自动检查没有开启" in before and "应用关闭期间不会检查" in before and "上一轮" not in before
    assert refresh(browser_a).headers["location"].endswith("notice=refresh_none") and batches(app) == []

    clock.advance(minutes=11)
    response = refresh(browser_a)
    after = text_of(browser_a.get(response.headers["location"]).text)
    [run] = batches(app)

    assert response.headers["location"].endswith("notice=refresh_started") and "已开始核对跟踪中的岗位" in after
    assert (run.trigger, run.outcome, run.calls) == ("manual", "complete", 2 * count)
    assert "现在核对：开始于" in after and "全部完成" in after and f"成功 {count} 个" in after and "现在没有超过 24 小时没核对的岗位" in after

    # A day on (signed in again: a session lasts 12 hours), what is due is said now; what the last batch did, as it was.
    clock.advance(hours=25)
    later = listed(type(browser_a).signed_in(app, "合成用户 A"))
    assert f"现在有 {count} 个超过 24 小时没有核对" in later and f"成功 {count} 个" in later


def test_a_second_start_while_one_runs_starts_nothing(app, browser_a, library, inline, board, clock):
    kept_results(app, browser_a, library)
    clock.advance(minutes=11)
    again = []

    def double_click(operation):
        if operation == "read" and not again:
            again.append(refresh(browser_a).headers["location"])

    board.during = double_click
    assert refresh(browser_a).status_code == 303

    assert again == ["/job-records?notice=refresh_busy"] and len(batches(app)) == 1


def test_a_users_batch_never_touches_another_users_postings(app, browser_a, browser_b, library, inline, board, clock):
    kept_results(app, browser_a, library)
    assert browser_b.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    theirs = kept_results(app, browser_b, confirmed(browser_b, **PROJECT))
    clock.advance(minutes=11)

    assert refresh(browser_a).status_code == 303
    [run] = batches(app)
    mine = {row.posting_record_id for row in rows(app, JobPostingRecheck)}

    with app.state.library.database.transaction() as db:
        owner = db.get(User, run.owner_id).display_label

    assert owner == "合成用户 A" and theirs and "上一轮" not in listed(browser_b)
    assert all(row.run_id == run.id for row in rows(app, JobPostingRecheck)) and len(mine) == len(rows(app, JobPostingRecheck))


def test_now_check_is_a_post_with_the_sessions_token(app, browser_a, library, inline, board, clock):
    kept_results(app, browser_a, library)
    clock.advance(minutes=11)

    response = browser_a.post("/job-records/refresh", {})

    assert response.status_code in (400, 403) and batches(app) == []


def test_the_automatic_check_is_off_by_default_and_runs_due_postings_when_on(app, browser_a, library, inline, board, clock):
    kept_results(app, browser_a, library)
    searches = app.state.discovery
    database = app.state.library.database
    clock.advance(hours=25)

    assert searches.update_seconds is None
    searches.run_due_updates(database, clock.now)
    assert batches(app) == []

    searches.update_seconds = 15 * 60
    searches.run_due_updates(database, clock.now)
    [run] = batches(app)
    assert (run.trigger, run.outcome) == ("scheduled", "complete")
    assert "定时检查：开始于" in listed(type(browser_a).signed_in(app, "合成用户 A"))

    # Due ones only: run again at once, nothing more is checked.
    searches.run_due_updates(database, clock.now)
    assert len(batches(app)) == 1


def test_the_automatic_check_is_refused_for_sources_not_all_synthetic(make_config, clock):
    real = replace(SYNTHETIC_SOURCES, grants=tuple(replace(grant, access=Access.LOCAL_FETCH) for grant in SYNTHETIC_SOURCES.grants))

    with pytest.raises(ConfigError, match="只能在合成来源上开启"):
        create_app(make_config(), clock=clock, discovery_sources=real, job_update_seconds=900)

    with pytest.raises(ConfigError):
        create_app(make_config(), clock=clock, job_update_seconds=0)

    app = create_app(make_config(), clock=clock, job_update_seconds=900)
    assert app.state.discovery.update_seconds == 900
    app.state.library.database.engine.dispose()


def test_a_run_left_open_when_the_app_stopped_is_said_interrupted_when_it_starts(make_config, clock):
    app = create_app(make_config(), clock=clock)
    database = app.state.library.database

    with database.transaction() as db:
        owner = db.scalar(select(User.id).order_by(User.id))
        db.add(JobUpdateRun(id="left-open", owner_id=owner, trigger="scheduled", calls=3, outcome=None, reason=None, started_at=clock.now(), finished_at=None))

    with TestClient(app):
        pass

    with database.transaction() as db:
        run = db.get(JobUpdateRun, "left-open")

    assert (run.outcome, run.reason, run.finished_at) == ("interrupted", "not_finished", None)
    database.engine.dispose()


def test_the_automatic_check_skips_a_user_whose_batch_is_running(app, browser_a, library, inline, board, clock):
    kept_results(app, browser_a, library)
    searches = app.state.discovery
    searches.update_seconds = 15 * 60
    clock.advance(hours=25)
    again = type(browser_a).signed_in(app, "合成用户 A")
    tried = []

    def automatic_meanwhile(operation):
        # The automatic check comes round while 现在核对 runs: it starts nothing for this user.
        if operation == "read" and not tried:
            tried.append(True)
            searches.run_due_updates(app.state.library.database, clock.now)

    board.during = automatic_meanwhile
    assert refresh(again).status_code == 303

    assert tried == [True] and [run.trigger for run in batches(app)] == ["manual"]


# --- F1: one batch of a user's, from the moment it is queued until it ends ----------------------------------------------


class QueuedExecutor:
    """Takes what is submitted and runs it only when released (run_all); refuses everything when told to."""

    def __init__(self, refuse: bool = False):
        self.pending = []
        self.refuse = refuse

    def submit(self, function, *args):
        if self.refuse:
            raise RuntimeError("synthetic: the queue takes nothing")

        future = Future()
        self.pending.append((function, args, future))

        return future

    def run_all(self):
        while self.pending:
            function, args, future = self.pending.pop(0)

            if future.set_running_or_notify_cancel():
                future.set_result(function(*args))

    def shutdown(self, wait=True, cancel_futures=False):
        for _, _, future in self.pending:
            future.cancel()

        self.pending.clear()


def tracked_many(app, clock, count) -> tuple[str, Board]:
    """count synthetic postings kept by 合成用户 A, all never checked, and the board the app's sources ask."""
    database = app.state.library.database

    with database.transaction() as db:
        owner = db.scalar(select(User.id).where(User.display_label == "合成用户 A"))

    postings = tuple(
        replace(POSTINGS[0], posting=replace(POSTINGS[0].posting, ref=PostingRef(CAREERS, f"queued-{number}"))) for number in range(count)
    )
    shared = Board(postings=postings)
    app.state.discovery.sources = replace(SYNTHETIC_SOURCES, make_register=lambda task: register_with(shared))

    for item in postings:
        kept_posting(database, owner, clock.now(), posting_id=item.posting.ref.posting_id)

    return owner, shared


def test_a_double_click_while_the_first_is_queued_starts_one_batch_only(app, browser_a, clock):
    owner, shared = tracked_many(app, clock, 21)
    searches = app.state.discovery
    searches.executor = queue = QueuedExecutor()

    first, second = refresh(browser_a), refresh(browser_a)

    assert first.headers["location"].endswith("refresh_started") and second.headers["location"].endswith("refresh_busy")
    assert searches.batches.holds(owner) and len(queue.pending) == 1

    queue.run_all()
    [run] = batches(app)

    # One batch of 20 (the limit), 40 calls; the 21st waits for the next one, which nobody started.
    assert len(shared.calls) == 40 and run.calls == 40 and not searches.batches.holds(owner)


def test_the_automatic_check_finds_a_queued_batch_busy(app, browser_a, clock):
    owner, shared = tracked_many(app, clock, 2)
    searches = app.state.discovery
    searches.executor = queue = QueuedExecutor()
    searches.update_seconds = 15 * 60

    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    searches.run_due_updates(app.state.library.database, clock.now)
    assert batches(app) == [] and shared.calls == []

    queue.run_all()
    assert [run.trigger for run in batches(app)] == ["manual"] and not searches.batches.holds(owner)


def test_a_queue_that_takes_nothing_gives_the_slot_back(app, browser_a, clock):
    owner, _ = tracked_many(app, clock, 2)
    searches = app.state.discovery
    searches.executor = QueuedExecutor(refuse=True)

    assert refresh(browser_a).headers["location"].endswith("refresh_failed") and not searches.batches.holds(owner)

    searches.executor = queue = QueuedExecutor()
    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    queue.run_all()
    assert len(batches(app)) == 1


def test_a_batch_cancelled_while_queued_gives_the_slot_back_and_a_new_one_starts(app, browser_a, clock):
    owner, shared = tracked_many(app, clock, 2)
    searches = app.state.discovery
    searches.executor = queue = QueuedExecutor()

    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    queue.shutdown(cancel_futures=True)
    assert not searches.batches.holds(owner) and shared.calls == []

    searches.executor = queue = QueuedExecutor()
    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    queue.run_all()
    assert len(batches(app)) == 1 and len(shared.calls) == 4


def test_an_error_in_a_batch_gives_the_slot_back(app, browser_a, clock):
    owner, shared = tracked_many(app, clock, 2)
    searches = app.state.discovery
    searches.executor = queue = QueuedExecutor()

    def broken(operation):
        raise RuntimeError("synthetic")

    shared.during = broken
    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    queue.run_all()
    [run] = batches(app)
    assert (run.outcome, run.reason) == ("interrupted", "error") and not searches.batches.holds(owner)

    shared.during = None
    assert refresh(browser_a).headers["location"].endswith("refresh_started")
    queue.run_all()
    assert len(batches(app)) == 2
