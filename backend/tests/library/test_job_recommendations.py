"""U3-D4b-2: current recommendations held for tracking (临时留存跟踪), below the pages.

A search the user ticked, ending as it should, holds its recommendations (full results of the first two categories) for
seven days, as postings with a row in job_recommendations; DONE alone is not enough. A later search replaces them; an
empty, failed or stopped one leaves them; the same search again, or an older one coming back late, changes nothing,
even once they are gone. 我的岗位, the recycle bin and kept analyses are never touched. From the moment its time comes a
recommendation is shown nowhere, never checked and never written to, removed yet or not; every removal drops what
memory holds of it (the text a 申请前核对 read, and its reservation) before it commits. Interleavings are driven by
events and the test's clock, never by sleeping. All data is synthetic; nothing leaves the process, no model is called.
"""
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.discovery import records, synthetic, updates
from app.discovery.contracts import Access, Category, CheckStatus, EndReason, EvidenceLevel, PostingRef, Result
from app.discovery.records import Found, Seen
from app.discovery.synthetic import CAREERS
from app.discovery.tasks import DiscoveryTaskStore
from app.discovery.web import SYNTHETIC_SOURCES, DiscoveryRuntime
from app.library import job_recommendations
from app.library.errors import NotAllowed, NotFound
from app.library.job_export import collect_job_records, jobs_markdown
from app.library.job_recommendations import KEEP_FOR, RecState
from app.library.lifecycle import HELD_REASON, delete_job_record, restore_job_record
from app.library.models import (
    AuditEvent,
    JobAnalysis,
    JobFeedback,
    JobPosting,
    JobPostingCheck,
    JobPostingRecheck,
    JobRecommendation,
    JobRecommendationLatest,
)
from app.library.service import new_id
from job_helpers import InlineExecutor
from update_helpers import KEPT_FACTS, Board, kept_posting, register_with

KEEP = SYNTHETIC_SOURCES.keep
AT = datetime(2026, 10, 29, 15, tzinfo=timezone.utc)
SESSION = "session-1"


def result(posting_id: str, category=Category.STRONG, *, title=None, status=CheckStatus.OPEN, evidence=EvidenceLevel.OFFICIAL) -> Result:
    """A full result of a synthetic search, as its outcome holds it: answered open at AT."""
    ref = PostingRef(CAREERS, posting_id)

    return Result(
        ref=ref,
        title=title or f"合成岗位 {posting_id}（不是真实招聘）",
        company="合成数据科技",
        source="合成招聘官网",
        evidence=evidence,
        url=f"{synthetic.ADDRESSES[CAREERS]}/{posting_id}",
        apply_url=None,
        posted_on=date(2026, 10, 20),
        deadlines=((ref, date(2026, 11, 30)),),
        read_at=AT,
        content_sha256="a" * 64,
        checked_at=AT,
        category=category,
        first_checks=(),
        notes=("合成说明：不该被留存",),
        analysis=None,
        cities=("北京",),
        job_type="实习",
        status=status,
        answered_status=status,
    )


def ticked(owner, results=(), *, started_at, task_id=None, end=EndReason.ENOUGH, stage="done", problem=None, problems=(),
           cancelled_by=None, finished=True, track=True, chosen=True, grants=synthetic.GRANTS):
    """A finished search as the store hands it to the retention: what the user ticked, how it ended, its results."""
    return SimpleNamespace(
        id=task_id or new_id(),
        owner_id=owner,
        stage=stage,
        cancelled_by=cancelled_by,
        problem=problem,
        problems=problems,
        started_at=started_at,
        confirmation=SimpleNamespace(track_recommendations=track, sources=grants, selection=("synthetic-experience",) if chosen else ()),
        finished=SimpleNamespace(outcome=SimpleNamespace(end=end, results=tuple(results))) if finished else None,
    )


def retain(database, task, clock, forgotten=None) -> records.Retention:
    with database.transaction() as db:
        return records.keep_recommendations(db, task, KEEP, clock.now(), forget=(forgotten.append if forgotten is not None else (lambda _: None)))


def held_ids(database, owner) -> dict[str, str]:
    """The owner's current recommendations: record id by posting id."""
    with database.transaction() as db:
        return {db.get(JobPosting, marker.posting_record_id).posting_id: marker.posting_record_id for marker in job_recommendations.markers(db, owner)}


def held(database, owner, now, posting_id="c-bj-analyst", *, until=None, **overrides) -> str:
    """A posting held as a current recommendation, written directly: its record id."""
    record_id = kept_posting(database, owner, now, posting_id=posting_id, **overrides)

    with database.transaction() as db:
        db.add(JobRecommendation(posting_record_id=record_id, owner_id=owner, search_id="search-1", recommended_at=now, until=until or now + KEEP_FOR))

    return record_id


def count(database, model) -> int:
    with database.transaction() as db:
        return db.scalar(select(func.count()).select_from(model))


def row_of(database, record_id) -> JobPosting | None:
    with database.transaction() as db:
        return db.get(JobPosting, record_id)


def state(database, record_id, now) -> RecState:
    with database.transaction() as db:
        return job_recommendations.state_of(db, db.get(JobPosting, record_id), now)


def published(checks, owner, record_id) -> str:
    """A 申请前核对 text shown for the record: reserved, then published."""
    assert checks.reserve(owner, SESSION, record_id) is not None

    return checks.put(owner, SESSION, record_id, updates.Finished("run-1", True, None, updates.Reading(status="open")))


# --- what is held, and when ---------------------------------------------------------------------------------------------


def test_a_ticked_search_holds_its_recommendations_and_nothing_else(database, owners, clock):
    owner, _ = owners
    started = clock.now()
    results = [
        result("c-bj-analyst"),
        result("c-sh-analyst", Category.PARTIAL),
        # A conflict is never called a recommendation, nor is a result not open as the search ended or not official.
        result("c-two-cities", Category.CONFLICT),
        result("c-choice", status=CheckStatus.UNKNOWN),
        result("c-bj-senior", evidence=EvidenceLevel.LEAD),
    ]
    task = ticked(owner, results, started_at=started)
    clock.advance(minutes=3)

    retention = retain(database, task, clock)
    ids = held_ids(database, owner)

    assert retention == records.Retention(kept=2, until=clock.now() + KEEP_FOR)
    assert set(ids) == {"c-bj-analyst", "c-sh-analyst"} and count(database, JobPosting) == 2

    with database.transaction() as db:
        for record_id in ids.values():
            row, marker = db.get(JobPosting, record_id), db.get(JobRecommendation, record_id)
            # The public facts by whitelist, never a note, a category or the analysis; one check, as the search answered.
            assert set(row.facts) == set(KEPT_FACTS) and "合成说明" not in str(row.facts)
            assert (marker.search_id, marker.recommended_at, marker.until) == (task.id, clock.now(), clock.now() + KEEP_FOR)
            assert [check.status for check in db.scalars(select(JobPostingCheck).where(JobPostingCheck.posting_record_id == record_id))] == ["open"]
            # Held by the system, not kept by the user: no event of a keep, no decision.
            assert row.decision is None and db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.object_id == record_id)) == 0

        latest = db.get(JobRecommendationLatest, owner)
        assert (latest.search_id, latest.search_started_at) == (task.id, started)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"track": False}, "not_ticked"),
        ({"stage": "cancelled", "cancelled_by": "user"}, "stopped"),
        ({"stage": "cancelled", "cancelled_by": "timeout"}, "stopped"),
        ({"problem": "internal_error"}, "failed"),
        ({"problems": ("所选经历已经修改",)}, "failed"),
        ({"finished": False}, "failed"),
        ({"started_at": None}, "failed"),
        ({"end": EndReason.TIMEOUT}, "ended_early"),
        ({"end": EndReason.CANCELLED}, "ended_early"),
        ({"end": EndReason.GATE}, "ended_early"),
        ({"end": EndReason.REFUSED}, "ended_early"),
        ({"end": EndReason.SOURCES_UNAVAILABLE}, "ended_early"),
        ({"end": EndReason.SOURCES_CHANGED}, "ended_early"),
        ({"chosen": False}, "not_offered"),
        # An access never kept (a search service's), whatever a set lists (U3-D6: a source reached by our own requests
        # may hold, where its set lists it; see test_discovery_batch_pages).
        ({"grants": tuple(replace(grant, access=Access.SEARCH_SERVICE) for grant in synthetic.GRANTS)}, "not_offered"),
    ],
)
def test_done_alone_is_not_enough(database, owners, clock, changes, reason):
    owner, _ = owners
    task = ticked(owner, [result("c-bj-analyst")], **{"started_at": clock.now(), **changes})

    assert retain(database, task, clock) == records.Retention(reason=reason)
    # Nothing written: not a posting, not a row in either table.
    assert (count(database, JobPosting), count(database, JobRecommendation), count(database, JobRecommendationLatest)) == (0, 0, 0)


def test_the_ends_that_hold_and_an_empty_search_that_leaves_the_ones_held(database, owners, clock):
    owner, _ = owners

    for end in (EndReason.SCOPE_DONE, EndReason.LIMIT):
        clock.advance(minutes=1)
        assert retain(database, ticked(owner, [result("c-bj-analyst")], started_at=clock.now(), end=end), clock).reason is None

    before = held_ids(database, owner)
    clock.advance(minutes=1)
    # Taken, with nothing to hold: the one held before stays as it was.
    empty = ticked(owner, [result("c-two-cities", Category.CONFLICT)], started_at=clock.now(), end=EndReason.PARTLY_NO_RESULTS)

    assert retain(database, empty, clock) == records.Retention(reason="none_recommended")
    assert held_ids(database, owner) == before

    with database.transaction() as db:
        assert db.get(JobRecommendationLatest, owner).search_id == empty.id


def test_a_later_search_replaces_them_keeping_what_both_recommend(database, owners, clock):
    owner, _ = owners
    first = ticked(owner, [result("c-bj-analyst"), result("c-sh-analyst")], started_at=clock.now())
    retain(database, first, clock)
    ids = held_ids(database, owner)
    revision = row_of(database, ids["c-sh-analyst"]).revision
    clock.advance(hours=2)
    later = ticked(owner, [result("c-sh-analyst", title="改过的标题"), result("c-two-cities", Category.PARTIAL)], started_at=clock.now())
    forgotten = []

    retention = retain(database, later, clock, forgotten)
    now_ids = held_ids(database, owner)

    assert retention == records.Retention(kept=1, renewed=1, removed=1, until=clock.now() + KEEP_FOR)
    assert set(now_ids) == {"c-sh-analyst", "c-two-cities"} and forgotten == [ids["c-bj-analyst"]]
    # Removed with its checks, never through the bin.
    assert row_of(database, ids["c-bj-analyst"]) is None

    with database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobPostingCheck).where(JobPostingCheck.posting_record_id == ids["c-bj-analyst"])) == 0
        kept_again = db.get(JobPosting, ids["c-sh-analyst"])
        marker = db.get(JobRecommendation, ids["c-sh-analyst"])
        # The same record, held again for the later search, seven days from now; its facts as first held.
        assert now_ids["c-sh-analyst"] == ids["c-sh-analyst"] and kept_again.revision > revision
        assert (marker.search_id, marker.until) == (later.id, clock.now() + KEEP_FOR)
        assert kept_again.facts["title"] != "改过的标题"
        assert {check.search_id for check in db.scalars(select(JobPostingCheck).where(JobPostingCheck.posting_record_id == ids["c-sh-analyst"]))} == {first.id, later.id}


def test_the_same_search_again_or_an_older_one_late_never_brings_one_back(database, owners, clock):
    owner, _ = owners
    older = ticked(owner, [result("c-bj-analyst")], started_at=clock.now())
    clock.advance(minutes=5)
    newer = ticked(owner, [result("c-sh-analyst")], started_at=clock.now())

    assert retain(database, newer, clock).kept == 1
    assert retain(database, newer, clock) == records.Retention(reason="superseded")
    # The older search, coming back after the newer one: refused, whichever ended first.
    assert retain(database, older, clock) == records.Retention(reason="superseded")
    assert set(held_ids(database, owner)) == {"c-sh-analyst"}

    record_id = held_ids(database, owner)["c-sh-analyst"]

    with database.transaction() as db:
        assert records.stop_tracking(db, owner, record_id, db.get(JobPosting, record_id).revision, clock.now(), forget=lambda _: None) == "stopped"

    # Gone; the same search, or the older one, again: nothing comes back, and the latest search taken stays.
    assert retain(database, newer, clock).reason == retain(database, older, clock).reason == "superseded"
    assert held_ids(database, owner) == {} and count(database, JobPosting) == 0

    with database.transaction() as db:
        assert db.get(JobRecommendationLatest, owner).search_id == newer.id


# --- what is never touched -----------------------------------------------------------------------------------------------


def test_my_jobs_are_never_held_touched_or_removed(database, owners, clock):
    owner, _ = owners
    mine = kept_posting(database, owner, clock.now(), decision="favorite", note="合成备注")

    with database.transaction() as db:
        db.add(JobFeedback(id=new_id(), owner_id=owner, posting_record_id=mine, search_id="search-1", field="place", reason="wrong", note=None, created_at=clock.now()))

    before = row_of(database, mine)
    retention = retain(database, ticked(owner, [result("c-bj-analyst"), result("c-sh-analyst")], started_at=clock.now()), clock)

    assert (retention.kept, retention.mine) == (1, 1) and set(held_ids(database, owner)) == {"c-sh-analyst"}
    clock.advance(days=8)

    with database.transaction() as db:
        assert records.remove_lapsed(db, clock.now(), forget=lambda _: None) == 1

    after = row_of(database, mine)
    assert (after.revision, after.decision, after.note, after.kept_at) == (before.revision, "favorite", "合成备注", before.kept_at)
    assert count(database, JobFeedback) == 1 and count(database, JobPostingCheck) == 0
    assert state(database, mine, clock.now()) is RecState.MINE


def test_a_record_in_the_bin_is_not_held_and_can_still_be_restored(database, owners, clock):
    owner, _ = owners
    mine = kept_posting(database, owner, clock.now())

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, mine, 1, clock.now())

    retention = retain(database, ticked(owner, [result("c-bj-analyst")], started_at=clock.now()), clock)

    assert (retention.kept, retention.in_bin) == (0, 1) and held_ids(database, owner) == {}

    with database.transaction() as db:
        restore_job_record(db, owner, JobPosting, mine, 2, clock.now())

    assert row_of(database, mine).deleted_at is None


def test_a_kept_analysis_outlives_its_recommendation(database, owners, clock):
    owner, _ = owners
    task = ticked(owner, [result("c-bj-analyst")], started_at=clock.now())
    retain(database, task, clock)
    record_id = held_ids(database, owner)["c-bj-analyst"]

    with database.transaction() as db:
        db.add(
            JobAnalysis(
                id=new_id(), owner_id=owner, search_id=task.id, source_id=CAREERS, posting_id="c-bj-analyst", source_access="synthetic",
                facts={"title": "合成岗位"}, content={"category": "strong"}, generation={}, content_sha256="a" * 64, analysed_at=AT,
                revision=1, saved_at=clock.now(), updated_at=clock.now(), deleted_at=None, purge_after=None, deletion_id=None,
            )
        )

    # Saving its analysis does not add it to 我的岗位; removing it leaves the analysis.
    assert state(database, record_id, clock.now()) is RecState.CURRENT

    with database.transaction() as db:
        records.stop_tracking(db, owner, record_id, db.get(JobPosting, record_id).revision, clock.now(), forget=lambda _: None)

    assert row_of(database, record_id) is None and count(database, JobAnalysis) == 1


def test_deciding_saying_not_right_or_adding_makes_it_mine_and_a_check_does_not(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    decided, said, added, checked = (held(database, owner, start, posting) for posting in ("c-bj-analyst", "c-sh-analyst", "c-two-cities", "c-choice"))
    clock.advance(hours=1)
    now = clock.now()

    with database.transaction() as db:
        records.decide_on_kept(db, owner, decided, 1, "favorite", [], "", now)
        records.feedback_on_kept(db, owner, said, 1, "place", "wrong", "", now)
        records.add_to_mine(db, owner, added, 1, now)

    for record_id in (decided, said, added):
        row = row_of(database, record_id)
        # In 我的岗位 from that very write: kept now by the user, its revision up, never lapsing again.
        assert state(database, record_id, now + timedelta(days=30)) is RecState.MINE and row.kept_at == now and row.revision >= 2

        with database.transaction() as db:
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.object_id == record_id, AuditEvent.action == "job_posting_kept")) == 1

    # A check from its record is not a decision: it stays a recommendation.
    board = Board()
    updates.run_check(
        database, owner, checked, 1, updates.Trigger.RECHECK, keep=frozenset({CAREERS}), delisting=frozenset(),
        register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
    )
    assert state(database, checked, clock.now()) is RecState.CURRENT

    with database.transaction() as db, pytest.raises(records.InputRejected):
        # Nothing to take back: it has no decision of the user's.
        records.decide_on_kept(db, owner, checked, db.get(JobPosting, checked).revision, "", [], "", clock.now())

    with database.transaction() as db, pytest.raises(records.InputRejected):
        records.add_to_mine(db, owner, decided, db.get(JobPosting, decided).revision, clock.now())


# --- from the moment its time comes (F1) -------------------------------------------------------------------------------


def test_from_its_time_a_recommendation_is_shown_nowhere_and_written_to_never(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    record_id = held(database, owner, start)
    clock.current = start + KEEP_FOR
    now = clock.now()

    assert state(database, record_id, now) is RecState.LAPSED
    # Not removed yet, and gone all the same: in no list, no lookup a page makes and no export.
    with database.transaction() as db:
        assert records.postings_by_state(db, owner, now) == ([], [], [])
        assert records.kept_states(db, owner, [PostingRef(CAREERS, "c-bj-analyst")], now) == {}
        assert updates.tracked(db, owner, frozenset({CAREERS}), now) == []
        export = collect_job_records(db, owner, now)
        assert export.postings == export.recommendations == []

    # Old forms write nothing and add nothing to 我的岗位.
    for write in (
        lambda db: records.decide_on_kept(db, owner, record_id, 1, "favorite", [], "", now),
        lambda db: records.feedback_on_kept(db, owner, record_id, 1, "place", "wrong", "", now),
        lambda db: records.add_to_mine(db, owner, record_id, 1, now),
    ):
        with database.transaction() as db, pytest.raises(records.RecommendationLapsed):
            write(db)

    assert (row_of(database, record_id).revision, row_of(database, record_id).decision, count(database, JobFeedback)) == (1, None, 0)

    # A check is refused before anything is sent.
    board = Board()
    with pytest.raises(updates.CheckRefused) as refused:
        updates.run_check(
            database, owner, record_id, 1, updates.Trigger.APPLY, keep=frozenset({CAREERS}), delisting=frozenset(),
            register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
        )
    assert refused.value.reason == updates.LAPSED and board.calls == []

    # 不再跟踪 on it says it went, and removes it.
    forgotten = []
    with database.transaction() as db:
        assert records.stop_tracking(db, owner, record_id, 1, now, forget=forgotten.append) == "lapsed"

    assert row_of(database, record_id) is None and forgotten == [record_id]


def test_a_batch_skips_one_whose_time_comes_while_it_waits(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    first, second = sorted(held(database, owner, start, posting) for posting in ("c-bj-analyst", "c-sh-analyst"))

    with database.transaction() as db:
        # The one taken second lapses an hour from now.
        db.get(JobRecommendation, second).until = start + timedelta(hours=1)

    board = Board()
    board.during = lambda operation: clock.current == start and clock.advance(hours=2)

    outcome = updates.run_batch(
        database, owner, updates.Trigger.MANUAL, keep=frozenset({CAREERS}), delisting=frozenset(), register=lambda: register_with(board),
        now=clock.now, monotonic=lambda: 0.0, checking=updates.Checking(), stop=threading.Event(),
    )
    posting_of = {first: row_of(database, first).posting_id, second: row_of(database, second).posting_id}

    assert outcome.counts["skipped"] == 1 and outcome.counts["succeeded"] + outcome.counts["failed"] == 1
    # Nothing sent for it when its turn came, nothing written.
    assert all(posting != posting_of[second] for _, posting in board.calls)

    with database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobPostingRecheck).where(JobPostingRecheck.posting_record_id == second)) == 0


def test_a_check_writes_nothing_for_one_whose_time_comes_while_it_is_checked(database, owners, clock):
    owner, _ = owners
    in_batch = held(database, owner, clock.now(), "c-bj-analyst", until=clock.now() + timedelta(minutes=30))
    board = Board()
    # Each read takes an hour of the test's clock.
    board.during = lambda operation: operation == "read" and clock.advance(hours=1)

    outcome = updates.run_batch(
        database, owner, updates.Trigger.MANUAL, keep=frozenset({CAREERS}), delisting=frozenset(), register=lambda: register_with(board),
        now=clock.now, monotonic=lambda: 0.0, checking=updates.Checking(), stop=threading.Event(),
    )

    assert outcome.counts["discarded"] == 1 and count(database, JobPostingRecheck) == 0
    # A single check (再核对一次, 申请前核对) of one whose time comes during its read: nothing written, said why.
    on_page = held(database, owner, clock.now(), "c-sh-analyst", until=clock.now() + timedelta(minutes=30))
    finished = updates.run_check(
        database, owner, on_page, 1, updates.Trigger.APPLY, keep=frozenset({CAREERS}), delisting=frozenset(),
        register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
    )

    assert (finished.written, finished.reason) == (False, updates.LAPSED_REASON)
    assert count(database, JobPostingRecheck) == 0 and row_of(database, in_batch).revision == row_of(database, on_page).revision == 1


def test_one_added_to_my_jobs_while_it_is_checked_gets_nothing_written(database, owners, clock):
    owner, _ = owners
    record_id = held(database, owner, clock.now())
    board = Board()

    def decide(operation):
        if operation == "read":
            with database.transaction() as db:
                records.decide_on_kept(db, owner, record_id, 1, "apply", [], "", clock.now())

    board.during = decide
    finished = updates.run_check(
        database, owner, record_id, 1, updates.Trigger.RECHECK, keep=frozenset({CAREERS}), delisting=frozenset(),
        register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
    )

    assert (finished.written, finished.reason) == (False, "record_changed")
    assert state(database, record_id, clock.now()) is RecState.MINE and row_of(database, record_id).decision == "apply"


def test_keeping_from_a_result_after_its_time_removes_it_and_keeps_anew(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    old = held(database, owner, start)
    clock.advance(days=8)
    found = Found("search-2", result("c-bj-analyst"), "synthetic", "合成招聘官网")

    # Nothing to drop its memory with: nothing written.
    with database.transaction() as db, pytest.raises(records.RecommendationLapsed):
        records.keep_posting(db, owner, found, Seen(), clock.now())

    forgotten = []
    with database.transaction() as db:
        kept = records.keep_posting(db, owner, found, Seen(), clock.now(), forget=forgotten.append)
        kept_id = kept.id

    assert kept_id != old and row_of(database, old) is None and forgotten == [old]
    assert state(database, kept_id, clock.now()) is RecState.MINE


# --- every removal drops the text before it commits (F2) -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["expired", "replaced", "untracked"])
def test_every_removal_drops_the_text_and_its_reservation_before_it_commits(database, owners, clock, path):
    owner, other = owners
    start = clock.now()
    task = ticked(owner, [result("c-bj-analyst")], started_at=start)
    retain(database, task, clock)
    record_id = held_ids(database, owner)["c-bj-analyst"]
    mine = kept_posting(database, owner, start, posting_id="c-sh-analyst", decision="favorite")
    theirs = held(database, other, start)
    checks = updates.ApplyChecks()
    shown = published(checks, owner, record_id)
    kept_text, their_text = published(checks, owner, mine), published(checks, other, theirs)
    assert checks.reserve(owner, SESSION, record_id) is not None

    with database.transaction() as db:
        if path == "expired":
            clock.current = start + KEEP_FOR
            assert records.remove_lapsed(db, clock.now(), forget=checks.forget_record, owner_id=owner) == 1
        elif path == "replaced":
            clock.advance(minutes=1)
            later = ticked(owner, [result("c-two-cities")], started_at=clock.now())
            assert records.keep_recommendations(db, later, KEEP, clock.now(), forget=checks.forget_record).removed == 1
        else:
            assert records.stop_tracking(db, owner, record_id, db.get(JobPosting, record_id).revision, clock.now(), forget=checks.forget_record) == "stopped"

        # Before the removal commits: the text is gone, and the reservation can publish nothing.
        assert checks.get(shown, owner, SESSION, record_id) is None
        assert checks.put(owner, SESSION, record_id, updates.Finished("run-2", True, None, updates.Reading(status="open"))) is None

    assert row_of(database, record_id) is None
    # 我的岗位 and another user's held posting keep theirs.
    assert checks.get(kept_text, owner, SESSION, mine) is not None and checks.get(their_text, other, SESSION, theirs) is not None


def test_a_removal_that_fails_keeps_the_rows_and_has_dropped_the_text(database, owners, clock):
    owner, _ = owners
    record_id = held(database, owner, clock.now())
    checks = updates.ApplyChecks()
    shown = published(checks, owner, record_id)

    with pytest.raises(RuntimeError), database.transaction() as db:
        records.remove_recommendations(db, owner, [record_id], forget=checks.forget_record)
        raise RuntimeError("the commit fails")

    # Rolled back: still held; only the temporary text went, and a new 申请前核对 may begin.
    assert state(database, record_id, clock.now()) is RecState.CURRENT and checks.get(shown, owner, SESSION, record_id) is None
    assert checks.reserve(owner, SESSION, record_id) is not None


def test_a_check_reserved_after_the_text_went_but_before_the_removal_commits_publishes_nothing(database, owners, clock):
    owner, _ = owners
    record_id = held(database, owner, clock.now())
    checks = updates.ApplyChecks()
    board = Board()
    reserved = threading.Event()
    outcome = {}

    def apply_check():
        ticket = checks.reserve(owner, SESSION, record_id)
        outcome["ticket"] = ticket
        reserved.set()

        try:
            # Its begin takes the write lock, which the removal holds until it commits.
            finished = updates.run_check(
                database, owner, record_id, 1, updates.Trigger.APPLY, keep=frozenset({CAREERS}), delisting=frozenset(),
                register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
            )

            if finished.written:
                outcome["token"] = checks.put(owner, SESSION, record_id, finished)
        except NotFound:
            outcome["refused"] = True
        finally:
            checks.cancel(ticket)

    worker = threading.Thread(target=apply_check)

    with database.transaction() as db:
        records.remove_recommendations(db, owner, [record_id], forget=checks.forget_record)
        worker.start()
        assert reserved.wait(10)

    worker.join(10)

    assert not worker.is_alive() and outcome["ticket"] is not None and outcome.get("refused") is True
    assert "token" not in outcome and len(checks) == 0 and board.calls == []


def test_a_check_that_comes_back_after_its_recommendation_went_publishes_nothing(database, owners, clock):
    owner, _ = owners
    record_id = held(database, owner, clock.now())
    checks = updates.ApplyChecks()
    board = Board()

    def untrack(operation):
        if operation == "read":
            with database.transaction() as db:
                records.stop_tracking(db, owner, record_id, 1, clock.now(), forget=checks.forget_record)

    board.during = untrack
    ticket = checks.reserve(owner, SESSION, record_id)
    finished = updates.run_check(
        database, owner, record_id, 1, updates.Trigger.APPLY, keep=frozenset({CAREERS}), delisting=frozenset(),
        register=lambda: register_with(board), now=clock.now, checking=updates.Checking(),
    )

    assert (finished.written, finished.reason) == (False, "record_deleted")
    # However the route goes on, nothing is published: its reservation was spoilt before the removal committed.
    assert checks.put(owner, SESSION, record_id, finished) is None and len(checks) == 0
    checks.cancel(ticket)


# --- a text goes at its recommendation's time, where it is published and where it is read (F1) --------------------------


def check(database, owner, record_id, clock) -> updates.Finished:
    """One 申请前核对 of the record at the clock's time, begun and written as its route has it."""
    return updates.run_check(
        database, owner, record_id, row_of(database, record_id).revision, updates.Trigger.APPLY, keep=frozenset({CAREERS}),
        delisting=frozenset(), register=lambda: register_with(Board()), now=clock.now, checking=updates.Checking(),
    )


def apply_check(database, checks, owner, record_id, clock, session=SESSION) -> str:
    """A 申请前核对 as its route makes it: reserved, checked and written, then published; its token."""
    ticket = checks.reserve(owner, session, record_id)

    try:
        finished = check(database, owner, record_id, clock)
        assert finished.written

        return checks.put(owner, session, record_id, finished)
    finally:
        checks.cancel(ticket)


def test_a_check_written_before_its_time_publishes_nothing_once_it_has_come(database, owners, clock):
    owner, _ = owners
    until = clock.now() + timedelta(seconds=1)
    record_id = held(database, owner, clock.now(), until=until)
    checks = updates.ApplyChecks(now=clock.now)

    # Just before its time: published, with its time.
    shown = apply_check(database, checks, owner, record_id, clock)
    assert checks.get(shown, owner, SESSION, record_id).until == until

    # Another check, written while it is current; its time comes before the text is published.
    ticket = checks.reserve(owner, SESSION, record_id)
    finished = check(database, owner, record_id, clock)
    assert finished.written and finished.until == until
    clock.current = until

    with pytest.raises(updates.TextLapsed):
        checks.put(owner, SESSION, record_id, finished)

    checks.cancel(ticket)
    # Nothing published, the text before gone as well, and nothing stuck: the reservation went with the refusal.
    assert len(checks) == 0 and checks.get(shown, owner, SESSION, record_id) is None
    assert checks.reserve(owner, SESSION, record_id) is not None


@pytest.mark.parametrize("look", ["get", "sweep", "reserve", "ended"])
def test_a_text_goes_the_moment_its_time_comes_whatever_looks(database, owners, clock, look):
    owner, other = owners
    until = clock.now() + timedelta(minutes=10)
    record_id = held(database, owner, clock.now(), until=until)
    mine = kept_posting(database, owner, clock.now(), posting_id="c-sh-analyst")
    theirs = held(database, other, clock.now(), "c-sh-analyst", until=until + timedelta(minutes=5))
    checks = updates.ApplyChecks(now=clock.now)
    shown, kept_text = apply_check(database, checks, owner, record_id, clock), apply_check(database, checks, owner, mine, clock)
    their_text = apply_check(database, checks, other, theirs, clock, session="session-2")

    clock.current = until - timedelta(microseconds=1)
    assert checks.get(shown, owner, SESSION, record_id) is not None

    clock.current = until

    if look == "get":
        with pytest.raises(updates.TextLapsed):
            checks.get(shown, owner, SESSION, record_id)
    elif look == "sweep":
        checks.sweep()
    elif look == "reserve":
        checks.cancel(checks.reserve(owner, SESSION, mine))
    else:
        assert checks.ended(SESSION) is False

    # Gone from that moment, never shown again; 我的岗位's text, and another user's whose time is later, stay.
    assert len(checks) == 2 and checks.get(shown, owner, SESSION, record_id) is None
    assert checks.get(kept_text, owner, SESSION, mine).until is None
    assert checks.get(their_text, other, "session-2", theirs) is not None


def test_the_store_reads_the_time_only_under_its_lock(database, owners, clock):
    """No gap between deciding and using: the time that decides is read under the lock the text is published and read
    under, never before it by a caller."""
    owner, _ = owners
    record_id = held(database, owner, clock.now(), until=clock.now() + timedelta(minutes=10))
    locked = []
    checks = updates.ApplyChecks(now=lambda: (locked.append(checks._lock.locked()), clock.now())[1])
    token = apply_check(database, checks, owner, record_id, clock)
    checks.get(token, owner, SESSION, record_id)
    checks.sweep()

    assert len(locked) >= 4 and all(locked)


def test_kept_again_a_text_read_since_lives_by_the_new_time_one_read_before_by_its_own(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    until = start + timedelta(minutes=20)
    record_id = held(database, owner, start, until=until)
    # The thirty minutes on the test's clock as well: neither text is old enough to go by them here.
    checks = updates.ApplyChecks(clock=lambda: clock.now().timestamp(), now=clock.now)
    before = apply_check(database, checks, owner, record_id, clock)

    clock.advance(minutes=10)
    kept_again = retain(database, ticked(owner, [result("c-bj-analyst")], started_at=clock.now()), clock)
    assert kept_again.renewed == 1 and held_ids(database, owner)["c-bj-analyst"] == record_id
    after = apply_check(database, checks, owner, record_id, clock)

    clock.current = until

    # Still current. The text read before goes by the time it had then, never later; the new check's lives on.
    assert state(database, record_id, clock.now()) is RecState.CURRENT

    with pytest.raises(updates.TextLapsed):
        checks.get(before, owner, SESSION, record_id)

    assert checks.get(after, owner, SESSION, record_id).until == kept_again.until > until


# --- the sweep, the bin, each user, the export ---------------------------------------------------------------------------


def test_the_sweep_removes_those_whose_time_came_with_their_text(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    lapsing, staying = held(database, owner, start, "c-bj-analyst", until=start + timedelta(hours=1)), held(database, owner, start, "c-sh-analyst")
    searches = DiscoveryRuntime(store=DiscoveryTaskStore(), executor=InlineExecutor(), database=database, now=clock.now)
    shown = published(searches.apply_checks, owner, lapsing)

    searches.sweep()
    assert row_of(database, lapsing) is not None

    clock.advance(hours=1)
    searches.sweep()

    assert row_of(database, lapsing) is None and row_of(database, staying) is not None
    assert searches.apply_checks.get(shown, owner, SESSION, lapsing) is None


def test_a_recommendation_never_goes_to_the_bin(database, owners, clock):
    owner, _ = owners
    record_id = held(database, owner, clock.now())

    with database.transaction() as db, pytest.raises(NotAllowed) as refused:
        delete_job_record(db, owner, JobPosting, record_id, 1, clock.now())

    assert refused.value.reason == HELD_REASON and row_of(database, record_id).deleted_at is None


def test_each_user_holds_and_removes_only_their_own(database, owners, clock):
    owner, other = owners
    retain(database, ticked(owner, [result("c-bj-analyst")], started_at=clock.now()), clock)
    record_id = held_ids(database, owner)["c-bj-analyst"]

    assert held_ids(database, other) == {}

    for write in (
        lambda db: records.stop_tracking(db, other, record_id, 1, clock.now(), forget=lambda _: None),
        lambda db: records.add_to_mine(db, other, record_id, 1, clock.now()),
        lambda db: records.decide_on_kept(db, other, record_id, 1, "favorite", [], "", clock.now()),
    ):
        with database.transaction() as db, pytest.raises(NotFound):
            write(db)

    with database.transaction() as db:
        assert records.remove_recommendations(db, other, [record_id], forget=lambda _: None) == []

    assert state(database, record_id, clock.now()) is RecState.CURRENT


def test_the_export_lists_recommendations_apart_from_my_jobs(database, owners, clock):
    owner, _ = owners
    start = clock.now()
    kept_posting(database, owner, start, posting_id="c-sh-analyst", decision="favorite")
    current = held(database, owner, start)
    held(database, owner, start, "c-two-cities", until=start + timedelta(hours=1))
    clock.advance(hours=2)

    with database.transaction() as db:
        content = collect_job_records(db, owner, clock.now())

    (entry,) = content.recommendations
    assert [item["posting_id"] for item in content.postings] == ["c-sh-analyst"]
    assert entry["id"] == current and entry["held"]["state"] == "current" and entry["held"]["until"] == (start + KEEP_FOR).isoformat().replace("+00:00", "Z")
    assert content.counts()["job_postings"] == 1 and content.counts()["job_recommendations"] == 1

    text = jobs_markdown(content, clock.now())
    assert "## 我的岗位" in text and "## 当前推荐（临时留存跟踪）" in text and "还没加入我的岗位" in text
