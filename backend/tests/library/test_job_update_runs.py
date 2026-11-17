"""U3-D4b-1: batches of checks of the postings tracked (现在核对, 定时检查), below the pages.

A batch is recorded once: its calls added up, how each posting it took ended, why it stopped and when it ended, all
written when it ends, never per posting. It takes at most 20 postings and 60 calls in 2 minutes, one budget for the
whole batch asked before every call, and never tries again; what it does not reach is left as it was. 定时检查 takes a
posting 24 hours after its latest check with a record, 现在核对 any not checked in the last 10 minutes; a check that
wrote nothing moves no time. A posting's turn re-checks that it is still one to check; a record changed meanwhile, or
during its check, gets nothing written; the user's decision is never touched. A stop or an error ends a batch as
interrupted with its guards given back; a run left open by a crash is said interrupted at start-up, never done. All
data is synthetic; nothing leaves the process and no model is called.
"""
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.discovery import records, updates
from app.discovery.sources import SourceAnomaly, SourceTimeout
from app.discovery.synthetic import CAREERS
from app.library.job_choices import batch_lines
from app.library.lifecycle import delete_job_record
from app.library.models import JobPosting, JobPostingRecheck, JobUpdateRun, JobUpdateRunCount
from update_helpers import KEPT_FACTS, Board, kept_posting, register_with, search_check

POSTINGS = ("c-bj-analyst", "c-bj-analyst-repost", "c-sh-analyst")


def keep_all(database, owner, clock, ids=POSTINGS, **overrides) -> dict[str, str]:
    """Each synthetic posting kept by the owner; its record id by posting id."""
    return {posting: kept_posting(database, owner, clock.now(), posting_id=posting, **overrides) for posting in ids}


def batch(database, owner, board, clock, trigger=updates.Trigger.SCHEDULED, *, limits=updates.BatchLimits(), seconds=None, checking=None, stop=None):
    return updates.run_batch(
        database,
        owner,
        trigger,
        keep=frozenset({CAREERS}),
        delisting=frozenset(),
        register=lambda: register_with(board),
        now=clock.now,
        monotonic=seconds or (lambda: 0.0),
        checking=checking or updates.Checking(),
        stop=stop or threading.Event(),
        limits=limits,
    )


def run_of(database, run_id) -> tuple[JobUpdateRun, JobUpdateRunCount | None]:
    with database.transaction() as db:
        return db.get(JobUpdateRun, run_id), db.get(JobUpdateRunCount, run_id)


def checks_of(database, record_id) -> list[JobPostingRecheck]:
    with database.transaction() as db:
        return updates.rechecks_of(db, db.get(JobPosting, record_id))


def counted(counts: JobUpdateRunCount) -> dict:
    return {name: getattr(counts, name) for name in ("succeeded", "failed", "discarded", "skipped", "unreached")}


# --- the batch, recorded once ---------------------------------------------------------------------------------------------


def test_a_batch_adds_up_its_calls_and_how_each_posting_ended_and_ends_once(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    board = Board()
    board.faults[("read", "c-sh-analyst")] = SourceTimeout("synthetic")
    seen = []

    def meanwhile(operation):
        # Half way through, the run is still open: nothing of it is written per posting.
        if operation == "read":
            with database.transaction() as db:
                run = db.scalar(select(JobUpdateRun))
                seen.append((run.outcome, run.finished_at, run.calls, db.get(JobUpdateRunCount, run.id)))

    board.during = meanwhile
    result = batch(database, owner, board, clock)
    run, counts = run_of(database, result.run_id)

    assert all(item == (None, None, 0, None) for item in seen) and len(seen) == 3
    assert (run.trigger, run.outcome, run.reason, run.calls, run.finished_at) == ("scheduled", "complete", None, 5, clock.now())
    assert (counts.due, counts.selected) == (3, 3) and counted(counts) == {"succeeded": 2, "failed": 1, "discarded": 0, "skipped": 0, "unreached": 0}
    assert {check.run_id for record_id in ids.values() for check in checks_of(database, record_id)} == {run.id}
    assert [check.reason for check in checks_of(database, ids["c-sh-analyst"])] == ["timeout"]
    assert batch_lines(run, counts, lambda value: value.isoformat())[1] == "全部完成"


def test_the_postings_limit_leaves_the_rest_due_for_the_next_batch(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    board = Board()

    first = batch(database, owner, board, clock, limits=updates.BatchLimits(postings=2))
    run, counts = run_of(database, first.run_id)
    left = [posting for posting, record_id in ids.items() if not checks_of(database, record_id)]

    assert (run.outcome, run.reason, counts.due, counts.selected, counts.unreached) == ("partial", "postings_limit", 3, 2, 0)
    assert len(left) == 1

    second = batch(database, owner, board, clock, limits=updates.BatchLimits(postings=2))
    run, counts = run_of(database, second.run_id)
    assert (run.outcome, counts.due, counts.selected, counts.succeeded) == ("complete", 1, 1, 1) and checks_of(database, ids[left[0]])


def test_the_calls_limit_stops_before_a_call_and_says_what_it_cut_half_way(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    board = Board()

    result = batch(database, owner, board, clock, limits=updates.BatchLimits(calls=3))
    run, counts = run_of(database, result.run_id)
    cut = [check for record_id in ids.values() for check in checks_of(database, record_id) if check.reason == "calls_used_up"]

    assert (run.outcome, run.reason, run.calls, len(board.calls)) == ("partial", "calls_limit", 3, 3)
    assert counted(counts) == {"succeeded": 1, "failed": 1, "discarded": 0, "skipped": 0, "unreached": 1}
    # The one cut half way was tried, so it says so; the one never reached has nothing written.
    assert len(cut) == 1 and cut[0].status == "unanswered" and cut[0].facts is not None
    assert sum(1 for record_id in ids.values() if not checks_of(database, record_id)) == 1


def test_one_budget_of_seconds_for_the_whole_batch_asked_before_every_call(database, owners, clock):
    owner, _ = owners
    keep_all(database, owner, clock)
    board = Board()
    now = [0.0]

    def eight_seconds_each(operation):
        now[0] += 8.0

    board.during = eight_seconds_each
    result = batch(database, owner, board, clock, limits=updates.BatchLimits(seconds=20.0), seconds=lambda: now[0])
    run, counts = run_of(database, result.run_id)

    # Each call given 15 seconds or what the batch has left, whichever is less; none once nothing is left.
    assert board.timeouts == [15.0, 12.0, 4.0]
    assert (run.outcome, run.reason) == ("partial", "time_limit")
    assert counted(counts) == {"succeeded": 1, "failed": 1, "discarded": 0, "skipped": 0, "unreached": 1}


@pytest.mark.parametrize(("fault", "asked"), [("anomaly", 1), ("timeout", 2)])
def test_a_source_that_answers_wrongly_or_times_out_twice_is_asked_nothing_more(database, owners, clock, fault, asked):
    owner, _ = owners
    keep_all(database, owner, clock)
    board = Board()

    for posting in POSTINGS:
        board.faults[("read", posting)] = SourceAnomaly("synthetic") if fault == "anomaly" else SourceTimeout("synthetic")

    result = batch(database, owner, board, clock)
    run, counts = run_of(database, result.run_id)

    assert len(board.calls) == asked and (run.outcome, run.reason) == ("partial", "source_stopped")
    assert counts.failed == asked and counts.unreached == 3 - asked


# --- time --------------------------------------------------------------------------------------------------------------------


def test_scheduled_waits_24_hours_from_the_latest_check_with_a_record_and_now_only_10_minutes(database, owners, clock):
    owner, _ = owners
    keep_all(database, owner, clock, ids=("c-bj-analyst",))
    board = Board()
    assert batch(database, owner, board, clock).run_id is not None

    # 现在核对 takes it again after 10 minutes, not before; its check moves the 24 hours of 定时检查.
    clock.advance(minutes=9)
    assert batch(database, owner, board, clock, updates.Trigger.MANUAL).run_id is None
    clock.advance(minutes=1)
    assert batch(database, owner, board, clock, updates.Trigger.MANUAL).run_id is not None
    clock.advance(hours=24, minutes=-1)
    assert batch(database, owner, board, clock).run_id is None
    clock.advance(minutes=1)
    assert batch(database, owner, board, clock).run_id is not None


def test_a_check_that_wrote_nothing_moves_no_time_and_a_failure_does(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock, ids=("c-bj-analyst", "c-sh-analyst"))
    search_check(database, owner, ids["c-sh-analyst"], "open", clock.now() - timedelta(hours=30))
    board = Board()
    board.faults[("read", "c-sh-analyst")] = SourceTimeout("synthetic")

    def another_tab(operation):
        # The first read is of c-bj-analyst: never checked, it comes before the one checked 30 hours ago.
        if operation == "read" and not board.calls:
            with database.transaction() as db:
                records.decide_on_kept(db, owner, ids["c-bj-analyst"], db.get(JobPosting, ids["c-bj-analyst"]).revision, "favorite", [], "", clock.now())

    board.during = another_tab
    first = batch(database, owner, board, clock)
    board.during = None
    _, counts = run_of(database, first.run_id)
    assert (counts.discarded, counts.failed) == (1, 1)

    # The discarded one wrote nothing, so it is still due; the failed one wrote its check, so it waits 24 hours.
    clock.advance(minutes=20)
    second = batch(database, owner, board, clock)
    _, counts = run_of(database, second.run_id)
    assert (counts.due, counts.succeeded) == (1, 1) and checks_of(database, ids["c-bj-analyst"])


def test_after_days_off_a_due_posting_is_checked_once_not_once_a_day(database, owners, clock):
    owner, _ = owners
    keep_all(database, owner, clock)
    board = Board()
    batch(database, owner, board, clock)
    clock.advance(days=3)
    board.calls.clear()

    result = batch(database, owner, board, clock)
    _, counts = run_of(database, result.run_id)

    assert (counts.due, counts.succeeded, len(board.calls)) == (3, 3, 6)
    assert batch(database, owner, board, clock).run_id is None


# --- what is checked, as each turn comes -------------------------------------------------------------------------------------


def test_only_postings_still_tracked_when_their_turn_comes_are_checked_and_no_decision_changes(database, owners, clock):
    owner, other = owners
    ids = keep_all(database, owner, clock, ids=("c-bj-analyst", "c-bj-analyst-repost", "c-sh-analyst", "c-two-cities"))
    keep_all(database, other, clock, ids=("c-choice",))
    board = Board()

    # Getting ready to apply comes first: the changes below happen while c-bj-analyst is read, the others still queued.
    with database.transaction() as db:
        db.get(JobPosting, ids["c-bj-analyst"]).decision = "apply"

    def meanwhile(operation):
        if operation != "read" or board.calls:
            return

        with database.transaction() as db:
            revision = lambda posting: db.get(JobPosting, ids[posting]).revision
            delete_job_record(db, owner, JobPosting, ids["c-bj-analyst-repost"], revision("c-bj-analyst-repost"), clock.now())
            records.decide_on_kept(db, owner, ids["c-sh-analyst"], revision("c-sh-analyst"), "skip", [], "", clock.now())
            records.decide_on_kept(db, owner, ids["c-two-cities"], revision("c-two-cities"), "apply", [], "", clock.now())

    board.during = meanwhile
    result = batch(database, owner, board, clock)
    _, counts = run_of(database, result.run_id)
    asked = {posting for _, posting in board.calls}

    # Deleted and skipped in the queue: asked nothing; changed to another decision: still tracked, checked, decision kept.
    assert asked == {"c-bj-analyst", "c-two-cities"} and counts.skipped == 2 and counts.succeeded == 2
    with database.transaction() as db:
        assert db.get(JobPosting, ids["c-sh-analyst"]).decision == "skip" and db.get(JobPosting, ids["c-two-cities"]).decision == "apply"


def test_a_posting_being_checked_from_its_page_is_left_to_that_check(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    checking = updates.Checking()
    assert checking.take(ids["c-bj-analyst"])

    result = batch(database, owner, Board(), clock, checking=checking)
    _, counts = run_of(database, result.run_id)

    assert counts.skipped == 1 and counts.succeeded == 2 and checks_of(database, ids["c-bj-analyst"]) == []
    assert checking.holds(ids["c-bj-analyst"]) and not checking.holds(ids["c-sh-analyst"])


def test_what_is_tracked(database, owners, clock):
    owner, other = owners
    ids = keep_all(database, owner, clock, ids=("c-bj-analyst", "c-bj-analyst-repost", "c-sh-analyst", "c-two-cities", "c-choice"))
    keep_all(database, other, clock, ids=("c-sz-analyst",))

    with database.transaction() as db:
        db.get(JobPosting, ids["c-bj-analyst-repost"]).decision = "skip"
        db.get(JobPosting, ids["c-sh-analyst"]).facts = {**KEPT_FACTS, "kind": "lead", "evidence": "lead"}
        db.get(JobPosting, ids["c-choice"]).source_access = "real"

    # A search's old "closed" kept no evidence: still tracked. Closed with evidence from a check: no longer.
    search_check(database, owner, ids["c-two-cities"], "closed", clock.now() - timedelta(hours=1))
    board = Board()
    board.closed.add("c-bj-analyst")
    updates.run_check(
        database, owner, ids["c-bj-analyst"], 1, updates.Trigger.RECHECK, keep=frozenset({CAREERS}), delisting=frozenset(),
        register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
    )

    with database.transaction() as db:
        assert {item.record_id for item in updates.tracked(db, owner, frozenset({CAREERS}), clock.now())} == {ids["c-two-cities"]}
        assert updates.tracked(db, owner, frozenset(), clock.now()) == []


# --- stopping, errors, start-up --------------------------------------------------------------------------------------------


def test_closing_the_app_interrupts_the_batch_and_gives_its_guards_back(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    stop, checking = threading.Event(), updates.Checking()
    board = Board()

    def closing(operation):
        if operation == "read":
            stop.set()

    board.during = closing
    result = batch(database, owner, board, clock, stop=stop, checking=checking)
    run, counts = run_of(database, result.run_id)

    assert (run.outcome, run.reason, run.finished_at) == ("interrupted", "app_closing", clock.now())
    assert counted(counts) == {"succeeded": 0, "failed": 1, "discarded": 0, "skipped": 0, "unreached": 2}
    assert len(board.calls) == 1 and not any(checking.holds(record_id) for record_id in ids.values())


def test_an_error_interrupts_the_batch_keeps_what_was_written_and_gives_its_guards_back(database, owners, clock):
    owner, _ = owners
    ids = keep_all(database, owner, clock)
    checking = updates.Checking()
    board = Board()

    def broken(operation):
        if operation == "read" and len(board.calls) == 2:
            raise RuntimeError("synthetic")

    board.during = broken

    with pytest.raises(RuntimeError):
        batch(database, owner, board, clock, checking=checking)

    with database.transaction() as db:
        run = db.scalar(select(JobUpdateRun))
        counts = db.get(JobUpdateRunCount, run.id)

    assert (run.outcome, run.reason) == ("interrupted", "error") and run.finished_at is not None
    assert counts.succeeded == 1 and counts.unreached == 2
    assert sum(len(checks_of(database, record_id)) for record_id in ids.values()) == 1
    assert not any(checking.holds(record_id) for record_id in ids.values())


def test_a_run_left_open_is_said_interrupted_at_start_up_never_done(database, owners, clock):
    owner, _ = owners

    with database.transaction() as db:
        for trigger in ("scheduled", "recheck"):
            db.add(JobUpdateRun(id=f"open-{trigger}", owner_id=owner, trigger=trigger, calls=4, outcome=None, reason=None, started_at=clock.now(), finished_at=None))

    assert updates.close_unfinished(database) == 2
    run, counts = run_of(database, "open-scheduled")
    lines = batch_lines(run, counts, lambda value: value.isoformat())

    assert (run.outcome, run.reason, run.finished_at, counts) == ("interrupted", "not_finished", None, None)
    assert "没有收尾" in lines[0] and lines[1].startswith("中断") and lines[-1] == "没有计数：这一轮没有收尾"
    assert updates.close_unfinished(database) == 0


def test_the_limits(clock):
    assert updates.BatchLimits() == updates.BatchLimits(postings=20, calls=60, seconds=120.0)
    assert (updates.DUE_SECONDS, updates.FRESH_SECONDS, updates.MAX_CALLS, updates.CALL_SECONDS) == (86400, 600, 3, 15.0)


def test_a_slot_is_given_back_only_with_its_own_ticket():
    slots = updates.Slots()
    first = slots.claim("owner")

    assert first is not None and slots.claim("owner") is None and slots.holds("owner", first)
    # Another ticket, or one given back already, never frees the slot.
    assert not slots.release("owner", "another") and slots.holds("owner")
    assert slots.release("owner", first) and not slots.holds("owner")
    second = slots.claim("owner")
    assert not slots.release("owner", first) and slots.holds("owner", second) and slots.claim("other") is not None
