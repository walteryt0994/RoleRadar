"""U3-D4a: checks of a kept posting made from its record, below the pages.

What a check may conclude: open; closed only with its evidence (the source's own deadline before today, a 404 with a
complete unfiltered list without it, a synthetic source's own close); everything else to be confirmed. A failure never
moves the last success; a search's old checks are merged without being upgraded; what changed is kept apart and never
holds the text; the user's decision is never touched; a record deleted or changed while a check runs gets nothing; one
check of a record at a time, at most three calls each given fifteen seconds and none made again; only synthetic
official postings; the text a check read stays in memory under its own rules. All data is synthetic, nothing leaves the
process and no model is called.
"""
import json
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.discovery import records, updates
from app.discovery.contracts import CheckAnswer, CheckStatus, PostingRef
from app.discovery.sources import HeldBack, SourceAnomaly, SourceTimeout
from app.library.errors import NotFound, RecordConflict
from app.library.job_choices import summarize, summary_text
from app.library.job_export import collect_job_records, jobs_markdown
from app.library.lifecycle import delete_job_record, purge_due, request_job_record_purge, restore_job_record
from app.library.models import AuditEvent, JobPosting, JobPostingCheck, JobPostingRecheck, JobUpdateRun
from update_helpers import KEPT_FACTS, POSTING, Board, ListedBoard, kept_posting, register_with, search_check


def run(database, owner, record_id, board, clock, *, revision=None, trigger=updates.Trigger.RECHECK, keep=None, delisting=frozenset(), checking=None):
    return updates.run_check(
        database,
        owner,
        record_id,
        row(database, record_id).revision if revision is None else revision,
        trigger,
        keep=frozenset({board.source_id}) if keep is None else keep,
        delisting=delisting,
        register=lambda: register_with(board),
        now=clock.now,
        checking=checking or updates.Checking(),
    )


def row(database, record_id) -> JobPosting:
    with database.transaction() as db:
        return db.get(JobPosting, record_id)


def rechecks(database, record_id) -> list[JobPostingRecheck]:
    with database.transaction() as db:
        return updates.rechecks_of(db, db.get(JobPosting, record_id))


def all_checks(database, record_id) -> list:
    with database.transaction() as db:
        record = db.get(JobPosting, record_id)

        return [*records.checks_of(db, record), *updates.rechecks_of(db, record)]


def count(database, model) -> int:
    with database.transaction() as db:
        return db.scalar(select(func.count()).select_from(model))


def iso(value) -> str:
    return value.isoformat()


# --- what a check concludes ---------------------------------------------------------------------------------------------


def test_an_open_posting_is_a_success_with_its_facts_kept_apart_and_nothing_else_changed(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()
    before = row(database, record_id)

    finished = run(database, owner, record_id, board, clock)
    [check] = rechecks(database, record_id)
    after = row(database, record_id)

    assert finished.written and check.status == "open" and check.evidence is None and check.reason is None
    # The read's public facts, as kept facts are, and which text it was: never the text itself.
    assert check.facts["deadline"] == "2026-11-30" and check.content_sha256 and check.changes == []
    assert board.text_of(POSTING) not in json.dumps(check.facts, ensure_ascii=False)
    # Read once, checked once, each given its fifteen seconds.
    assert board.calls == [("read", POSTING), ("check", POSTING)] and board.timeouts == [15.0, 15.0] and finished.reading.calls == 2
    # The record's own facts and decision stay as they were; the check is a change of the record (its revision).
    assert after.facts == before.facts == KEPT_FACTS and after.decision is None and after.revision == before.revision + 1
    assert summary_text(summarize(all_checks(database, record_id)), iso).startswith("核对时仍公开发布（不代表企业还有名额）")

    with database.transaction() as db:
        [run_row] = db.scalars(select(JobUpdateRun))
        events = list(db.scalars(select(AuditEvent.action).where(AuditEvent.object_id == record_id)))

    assert (run_row.trigger, run_row.calls, run_row.outcome) == ("recheck", 2, "done") and "job_posting_rechecked" in events


@pytest.mark.parametrize(("days", "status"), [(-1, "closed"), (0, "open")], ids=["yesterday", "today"])
def test_a_deadline_before_today_closes_it_with_its_evidence_and_today_does_not(database, owners, clock, days, status):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()
    today = clock.now().astimezone().date()
    board.revise(POSTING, deadline=today + timedelta(days=days))

    run(database, owner, record_id, board, clock)
    [check] = rechecks(database, record_id)

    assert check.status == status

    if status == "closed":
        # The read and the check gave the same deadline: both, as the evidence says.
        assert check.evidence == {
            "kind": "deadline", "deadline": (today - timedelta(days=1)).isoformat(), "from": "both", "answered_at": iso(clock.now())
        }
        assert check.facts["deadline"] == check.evidence["deadline"]
        assert "来源已明确关闭：来源写的截止日期" in summary_text(summarize([check]), iso)


def test_a_404_with_the_complete_list_without_it_closes_it_with_both_pieces_of_evidence(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = ListedBoard()
    board.delisted.add(POSTING)

    finished = run(database, owner, record_id, board, clock, delisting=frozenset({board.source_id}))
    [check] = rechecks(database, record_id)

    assert check.status == "closed" and check.facts is None and finished.reading.calls == 2
    assert check.evidence["kind"] == "delisted" and check.evidence["status_code"] == 404 and check.evidence["listing_complete"]
    assert check.evidence["listing_count"] == len(board._postings) - 1 and check.evidence["not_found_at"] and check.evidence["listing_read_at"]
    assert board.calls == [("read", POSTING), ("listing", None)]


@pytest.mark.parametrize(
    ("scenario", "status", "reason", "calls"),
    [
        ("404_alone", "unknown", "not_found", 1),
        ("404_but_listed", "unknown", "not_found_but_listed", 2),
        ("404_list_in_part", "unknown", "listing_partial", 2),
        ("timeout", "unanswered", "timeout", 1),
        ("held_back", "unanswered", "not_sent", 1),
        ("anomaly", "unanswered", "anomaly", 1),
        ("check_times_out", "unanswered", "timeout", 2),
        ("source_not_run_on", "unanswered", "source_unavailable", 0),
    ],
)
def test_what_settles_nothing_is_to_be_confirmed_and_the_last_success_keeps_its_time(database, owners, clock, scenario, status, reason, calls):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = ListedBoard()
    run(database, owner, record_id, board, clock)
    succeeded_at = clock.now()
    clock.advance(hours=1)
    board.calls.clear()
    keep, delisting = None, frozenset()

    if scenario == "404_alone":
        board.delisted.add(POSTING)
    elif scenario == "404_but_listed":
        board.missing.add(POSTING)
        delisting = frozenset({board.source_id})
    elif scenario == "404_list_in_part":
        board.delisted.add(POSTING)
        board.partial = True
        delisting = frozenset({board.source_id})
    elif scenario == "check_times_out":
        board.faults[("check", POSTING)] = SourceTimeout("synthetic")
    elif scenario == "source_not_run_on":
        keep = frozenset()
    else:
        board.faults[("read", POSTING)] = {"timeout": SourceTimeout, "held_back": HeldBack, "anomaly": SourceAnomaly}[scenario]("synthetic")

    finished = run(database, owner, record_id, board, clock, keep=keep, delisting=delisting)
    latest, first = rechecks(database, record_id)
    summary = summarize(all_checks(database, record_id))
    text = summary_text(summary, iso)

    assert (latest.status, latest.reason, finished.reading.calls, len(board.calls)) == (status, reason, calls, calls)
    # To be confirmed now, with the time of the last success kept as it was; never closed, never open.
    assert summary.attempted.id == latest.id and summary.succeeded.id == first.id and first.checked_at == succeeded_at
    assert text.startswith(f"岗位状态待确认：最近一次核对（{iso(clock.now())}）") and f"最近成功核对：{iso(succeeded_at)}" in text
    assert "明确关闭" not in text.split("上次成功核对")[0]


def test_a_close_without_evidence_settles_nothing_outside_synthetic_data(clock):
    class Says:
        def read(self, ref, timeout):
            return Board().read(ref, timeout)

        def check(self, ref, timeout):
            return CheckAnswer(CheckStatus.CLOSED)

    ref = PostingRef("synthetic-careers", POSTING)
    today = clock.now().astimezone().date()
    real = updates.check_posting(Says(), ref, synthetic=False, delisting=False, today=today, now=clock.now)
    simulated = updates.check_posting(Says(), ref, synthetic=True, delisting=False, today=today, now=clock.now)

    assert (real.status, real.reason, real.evidence) == ("unknown", "closed_without_evidence", None)
    assert simulated.status == "closed" and simulated.evidence["kind"] == "synthetic"


def test_a_check_calls_its_source_at_most_three_times(clock):
    calls = updates._Calls()

    for _ in range(updates.MAX_CALLS):
        calls(lambda timeout: timeout)

    with pytest.raises(RuntimeError):
        calls(lambda timeout: timeout)


# --- a search's old checks and the record's new ones, together ---------------------------------------------------------


def test_a_searchs_old_checks_merge_without_being_upgraded(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    search_check(database, owner, record_id, "unknown", clock.now() - timedelta(hours=3))
    search_check(database, owner, record_id, "closed", clock.now() - timedelta(hours=2))
    old = summarize(all_checks(database, record_id))
    old_text = summary_text(old, iso)

    # A search's "closed" kept no evidence and an old unknown is no success: to be confirmed, never a confirmed close.
    assert old.succeeded is None and old.answered is old.attempted and old.attempted.status == "closed"
    assert old_text.startswith("岗位状态待确认：") and "不算确认关闭" in old_text and "还没有成功核对过" in old_text

    board = Board()
    run(database, owner, record_id, board, clock)
    opened = clock.now()
    clock.advance(hours=1)
    board.faults[("read", POSTING)] = SourceTimeout("synthetic")
    run(database, owner, record_id, board, clock)
    text = summary_text(summarize(all_checks(database, record_id)), iso)

    assert text.startswith(f"岗位状态待确认：最近一次核对（{iso(clock.now())}）没有得到回答") and f"最近成功核对：{iso(opened)}" in text


def test_an_old_open_answer_stays_the_last_success_when_a_new_check_fails(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    answered = clock.now() - timedelta(hours=5)
    search_check(database, owner, record_id, "open", answered)
    board = Board()
    board.faults[("read", POSTING)] = SourceTimeout("synthetic")

    run(database, owner, record_id, board, clock)
    summary = summarize(all_checks(database, record_id))

    assert isinstance(summary.succeeded, JobPostingCheck) and summary.succeeded.checked_at == answered
    assert f"最近成功核对：{iso(answered)}，核对时仍公开发布" in summary_text(summary, iso)


# --- what changed, and the text ---------------------------------------------------------------------------------------------


def test_what_changed_is_kept_apart_with_both_values_and_never_the_text(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()
    run(database, owner, record_id, board, clock)
    marker = "合成正文里的一句新要求：只在内存里显示"
    board.revise(POSTING, deadline=date(2026, 12, 15), text=board.text_of(POSTING) + "\n" + marker)
    clock.advance(minutes=5)

    run(database, owner, record_id, board, clock)
    latest, first = rechecks(database, record_id)

    assert first.changes == []
    assert {"field": "deadline", "before": "2026-11-30", "after": "2026-12-15"} in latest.changes
    assert {"field": "text", "before": None, "after": None} in latest.changes and latest.content_sha256 != first.content_sha256
    # The record's facts stay as kept; the text is nowhere in what was written.
    assert row(database, record_id).facts == KEPT_FACTS

    with database.transaction() as db:
        written = json.dumps(
            [[check.facts, check.changes, check.evidence, check.reason] for check in db.scalars(select(JobPostingRecheck))], ensure_ascii=False
        )
        data = collect_job_records(db, owner, clock.now())

    readable = jobs_markdown(data, clock.now())

    for text in (written, json.dumps(data.postings, ensure_ascii=False), readable):
        assert marker not in text and "熟练使用 Python 进行数据处理" not in text


# --- the user's decision, and a record that changes while a check runs ----------------------------------------------------


@pytest.mark.parametrize("found", ["open", "closed", "unanswered"])
def test_the_users_decision_is_never_touched(database, owners, clock, found):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now(), decision="apply", reasons=[], note="合成备注", decided_at=clock.now())
    board = Board()

    if found == "closed":
        board.closed.add(POSTING)
    elif found == "unanswered":
        board.faults[("read", POSTING)] = SourceTimeout("synthetic")

    run(database, owner, record_id, board, clock, trigger=updates.Trigger.APPLY)
    after = row(database, record_id)
    [check] = rechecks(database, record_id)

    assert check.status == found and (after.decision, after.note, after.decided_at) == ("apply", "合成备注", clock.now())


@pytest.mark.parametrize("change", ["deleted", "decided", "deleted_and_restored"])
def test_a_record_deleted_or_changed_while_its_check_runs_gets_nothing_written(database, owners, clock, change):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()

    def meanwhile(operation):
        if operation != "read":
            return

        with database.transaction() as db:
            revision = db.get(JobPosting, record_id).revision

            if change == "decided":
                records.decide_on_kept(db, owner, record_id, revision, "skip", [], "", clock.now())
            else:
                delete_job_record(db, owner, JobPosting, record_id, revision, clock.now())

        if change == "deleted_and_restored":
            with database.transaction() as db:
                restore_job_record(db, owner, JobPosting, record_id, revision + 1, clock.now())

    board.during = meanwhile
    finished = run(database, owner, record_id, board, clock)

    with database.transaction() as db:
        [run_row] = db.scalars(select(JobUpdateRun))

    assert not finished.written and rechecks(database, record_id) == []
    assert (run_row.outcome, run_row.reason, run_row.calls) == ("discarded", "record_deleted" if change == "deleted" else "record_changed", 2)
    # What the user did meanwhile stands: the late result overwrote nothing.
    assert row(database, record_id).decision == ("skip" if change == "decided" else None)


def test_one_check_of_a_record_at_a_time(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board, checking, refused = Board(), updates.Checking(), []

    def again(operation):
        if operation == "read":
            with pytest.raises(updates.CheckRefused) as second:
                run(database, owner, record_id, board, clock, checking=checking)

            refused.append(second.value.reason)

    board.during = again
    assert run(database, owner, record_id, board, clock, checking=checking).written
    board.during = None

    assert refused == [updates.BUSY] and count(database, JobUpdateRun) == 1
    assert run(database, owner, record_id, board, clock, checking=checking).written


# --- what may be checked at all ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"source_access": "real"}, updates.NOT_TRACKABLE),
        ({"facts": {**KEPT_FACTS, "kind": "lead", "evidence": "lead"}}, updates.LEAD),
    ],
    ids=["real_source", "lead"],
)
def test_only_synthetic_official_postings_are_checked_and_nothing_is_sent_otherwise(database, owners, clock, overrides, reason):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now(), **overrides)
    board = Board()

    with pytest.raises(updates.CheckRefused) as refused:
        run(database, owner, record_id, board, clock)

    assert refused.value.reason == reason and board.calls == []
    assert count(database, JobUpdateRun) == 0 and count(database, JobPostingRecheck) == 0


def test_a_page_that_saw_an_older_revision_starts_nothing_and_another_user_reaches_nothing(database, owners, clock):
    owner, other = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()

    with pytest.raises(RecordConflict):
        run(database, owner, record_id, board, clock, revision=row(database, record_id).revision + 1)

    with pytest.raises(NotFound):
        run(database, other, record_id, board, clock, revision=1)

    assert board.calls == [] and count(database, JobUpdateRun) == 0


# --- with the record: the bin, purge and export -------------------------------------------------------------------------


def test_its_checks_go_to_the_bin_come_back_and_are_purged_with_the_record(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    run(database, owner, record_id, Board(), clock)

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, record_id, db.get(JobPosting, record_id).revision, clock.now())

    assert len(rechecks(database, record_id)) == 1
    [entry] = collect_job_records_now(database, owner, clock)
    assert entry["kept_as"] == "recycle_bin" and len(entry["rechecks"]) == 1

    with database.transaction() as db:
        request_job_record_purge(db, owner, JobPosting, record_id, db.get(JobPosting, record_id).revision, clock.now())

    purge_due(database, clock.now())

    assert count(database, JobPostingRecheck) == 0 and row(database, record_id) is None
    assert collect_job_records_now(database, owner, clock) == []


def collect_job_records_now(database, owner, clock) -> list[dict]:
    with database.transaction() as db:
        return collect_job_records(db, owner, clock.now()).postings


def test_the_export_says_each_check_and_the_status_now(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()
    run(database, owner, record_id, board, clock, trigger=updates.Trigger.APPLY)
    clock.advance(minutes=1)
    board.faults[("read", POSTING)] = SourceTimeout("synthetic")
    run(database, owner, record_id, board, clock)

    [entry] = collect_job_records_now(database, owner, clock)

    with database.transaction() as db:
        readable = jobs_markdown(collect_job_records(db, owner, clock.now()), clock.now())

    assert [(check["trigger"], check["status"]) for check in entry["rechecks"]] == [("apply", "open"), ("recheck", "unanswered")]
    assert entry["rechecks"][1]["unanswered_at"] and entry["rechecks"][1]["source_answered_at"] is None
    assert entry["check_state"].startswith("岗位状态待确认：最近一次核对") and "最近成功核对：" in entry["check_state"]
    assert "申请前核对（来源回答" in readable and "再核对一次（尝试核对" in readable and "共 2 条核对记录" in readable


# --- the text a 申请前核对 read, in memory only ---------------------------------------------------------------------------


def test_the_text_is_for_its_session_alone_for_a_while_and_gone_with_its_record_or_session(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    finished = run(database, owner, record_id, Board(), clock, trigger=updates.Trigger.APPLY)
    seconds = [1000.0]
    store = updates.ApplyChecks(clock=lambda: seconds[0])

    def shown_now():
        assert store.reserve(owner, "session-a", record_id) is not None
        return store.put(owner, "session-a", record_id, finished)

    token = shown_now()
    shown = store.get(token, owner, "session-a", record_id)

    assert shown is not None and "熟练使用 Python 进行数据处理" in shown.reading.text
    # Another user, another session, another record: nothing.
    assert store.get(token, "someone-else", "session-a", record_id) is None
    assert store.get(token, owner, "session-b", record_id) is None
    assert store.get(token, owner, "session-a", "another-record") is None

    # A look never keeps it longer: gone once its time is up from when it was made.
    seconds[0] += updates.TEXT_TTL_SECONDS - 1
    assert store.get(token, owner, "session-a", record_id) is not None
    seconds[0] += 1
    assert store.get(token, owner, "session-a", record_id) is None and len(store) == 0

    # Gone as soon as its record is deleted, or its session ends.
    token = shown_now()
    store.forget_record(record_id)
    assert store.get(token, owner, "session-a", record_id) is None
    token = shown_now()
    store.end_session("session-a")
    assert store.get(token, owner, "session-a", record_id) is None and len(store) == 0


# --- F2: a text is shown only for a check reserved before it began, with nothing ended since -----------------------------


def test_a_text_is_shown_only_for_a_reserved_check_and_never_after_its_record_or_session_ended(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    finished = run(database, owner, record_id, Board(), clock, trigger=updates.Trigger.APPLY)
    store = updates.ApplyChecks(clock=lambda: 1000.0)

    # Never reserved: never shown.
    assert store.put(owner, "session-a", record_id, finished) is None and len(store) == 0

    # The record deleted (and maybe restored) after the check began: its late result is never shown...
    ticket = store.reserve(owner, "session-a", record_id)
    store.forget_record(record_id)
    assert store.put(owner, "session-a", record_id, finished) is None and len(store) == 0
    store.cancel(ticket)
    # ...while a check begun after it is, as any other.
    assert store.reserve(owner, "session-a", record_id) is not None
    assert store.put(owner, "session-a", record_id, finished) is not None and len(store) == 1

    # The session ended after the check began: nothing is shown, nor reserved, nor read for it any more.
    assert store.reserve(owner, "session-b", record_id) is not None
    store.end_session("session-b")
    assert store.put(owner, "session-b", record_id, finished) is None
    assert store.reserve(owner, "session-b", record_id) is None

    # One check of a record at a time; another session's reservation is not this one's to publish.
    ticket = store.reserve(owner, "session-a", record_id)
    assert ticket is not None and store.reserve(owner, "session-a", record_id) is None
    assert store.put(owner, "session-c", record_id, finished) is None
    store.cancel(ticket)
    assert store.reserve(owner, "session-a", record_id) is not None

    # A check that ends without publishing (refused, discarded, failed) gives its reservation back.
    store = updates.ApplyChecks(clock=lambda: 1000.0)
    ticket = store.reserve(owner, "session-a", record_id)
    store.cancel(ticket)
    assert store.reserve(owner, "session-a", record_id) is not None


def test_a_late_result_after_the_record_was_deleted_is_never_shown_whatever_the_order(database, owners, clock):
    """Two threads, ordered by events: the check reserves, waits until the record is deleted elsewhere, then publishes;
    and the other way round, published first and deleted after. Either way nothing is left to show."""
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    finished = run(database, owner, record_id, Board(), clock, trigger=updates.Trigger.APPLY)
    store = updates.ApplyChecks(clock=lambda: 1000.0)
    reserved, deleted, published = threading.Event(), threading.Event(), []

    def check_then_publish():
        assert store.reserve(owner, "session-a", record_id) is not None
        reserved.set()
        assert deleted.wait(5)
        published.append(store.put(owner, "session-a", record_id, finished))

    worker = threading.Thread(target=check_then_publish)
    worker.start()
    assert reserved.wait(5)
    store.forget_record(record_id)
    deleted.set()
    worker.join(5)

    assert published == [None] and len(store) == 0

    # Published first, then the record deleted: what was shown goes at once.
    assert store.reserve(owner, "session-a", record_id) is not None
    token = store.put(owner, "session-a", record_id, finished)
    store.forget_record(record_id)
    assert store.get(token, owner, "session-a", record_id) is None and len(store) == 0


# --- F1: the deadline of two answers, said the same way everywhere ------------------------------------------------------


@pytest.mark.parametrize(
    ("read_days", "check_days", "status", "settled_days", "given"),
    [
        (None, -1, "closed", -1, "check"),
        (-1, -1, "closed", -1, "both"),
        (30, -1, "unknown", None, None),
        (30, 40, "unknown", None, None),
        (None, 20, "open", 20, None),
    ],
    ids=["only_the_check_past", "both_alike_past", "differ_one_past", "differ_both_ahead", "only_the_check_ahead"],
)
def test_the_deadline_of_the_read_and_the_check_is_said_alike_in_status_facts_changes_and_export(
    database, owners, clock, read_days, check_days, status, settled_days, given
):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    today = clock.now().astimezone().date()

    def day(days):
        return None if days is None else today + timedelta(days=days)

    board = Board()
    board.read_deadline, board.check_deadline = day(read_days), day(check_days)

    run(database, owner, record_id, board, clock)
    [check] = rechecks(database, record_id)
    settled = None if settled_days is None else day(settled_days).isoformat()
    answers = {"read": None if read_days is None else day(read_days).isoformat(), "check": day(check_days).isoformat()}
    [entry] = collect_job_records_now(database, owner, clock)
    text = summary_text(summarize(all_checks(database, record_id)), iso)
    [change] = [change for change in check.changes if change["field"] == "deadline"]

    # Each answer kept as it was given, and one deadline said wherever it is said: or, when they differ, none.
    assert check.status == status and check.facts["deadline_answers"] == answers and check.facts["deadline"] == settled
    assert entry["rechecks"][0]["facts"] == check.facts and entry["rechecks"][0]["changes"] == check.changes
    assert change["before"] == "2026-11-30"

    if status == "closed":
        assert check.evidence["deadline"] == settled and check.evidence["from"] == given
        assert text.startswith(f"来源已明确关闭：来源写的截止日期 {settled} 已过") and change["after"] == settled
    elif settled is None:
        # Two answers that differ: to be confirmed, both said, no date taken for the deadline, no close.
        assert check.reason == "deadline_conflict" and check.evidence is None
        assert change["after"] == {"read": answers["read"], "check": answers["check"]}
        assert text.startswith("岗位状态待确认") and "截止日期不一致" in text and "已过" not in text
    else:
        assert change["after"] == settled and text.startswith("核对时仍公开发布")


def test_a_deadline_changed_between_checks_is_recorded_from_the_one_each_settled_on(database, owners, clock):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now())
    board = Board()
    run(database, owner, record_id, board, clock)
    clock.advance(minutes=5)
    board.read_deadline, board.check_deadline = None, date(2026, 12, 15)

    run(database, owner, record_id, board, clock)
    latest, first = rechecks(database, record_id)

    assert first.facts["deadline"] == "2026-11-30" and latest.facts["deadline"] == "2026-12-15"
    assert {"field": "deadline", "before": "2026-11-30", "after": "2026-12-15"} in latest.changes
    assert latest.facts["deadline_answers"] == {"read": None, "check": "2026-12-15"}
