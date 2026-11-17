"""U3-D4c: 每日查新 below the pages.

A watch makes its summary's searches again by a fixed flow and keeps, in one write at the end of each run, every
posting its answered searches gave as seen and the new ones as new: a search's first answer is its baseline, a posting
seen once is never new again, and skips, kept postings, postings out of the scope and the same posting twice are only
counted. Every new posting is kept, however many, for 14 days. 定时检查 takes a watch 24 hours after its last run that
was not interrupted began, failed or not; a watch that has seen 5,000 postings starts no new run, and the run that
crosses it is written whole. A watch is stopped by removing it: before each call one transaction lets that one call go
if the watch still stands with the run's token, so a removal committed first refuses it and a call let go first writes
nothing. All data is synthetic; nothing leaves the process and no model is called.
"""
import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.discovery import records, updates, watches
from app.discovery.contracts import PostingRef
from app.discovery.sources import HeldBack, SourceAnomaly, SourceTimeout
from app.discovery.synthetic import CAREERS, LEAD_POSTINGS, LEADS
from app.library import job_watches
from app.library.errors import NotAllowed, NotFound
from app.library.job_export import collect_job_records
from app.library.lifecycle import delete_job_record, restore_job_record
from app.library.models import JobPosting, JobPostingCheck, JobSearch, JobWatch, JobWatchPosting, JobWatchSeen
from update_helpers import kept_posting
from watch_helpers import BEIJING, KEEP, SCOPE, SEARCHES, SHANGHAI, Watched, WatchedList, kept_summary, register_of

NEW_JOB = "c-new-bj"


def watch_on(database, owner, clock, sources, summary_id=None) -> str:
    """A watch started on a kept summary (made if none is given); its id."""
    summary_id = summary_id or kept_summary(database, owner, clock.now())

    with database.transaction() as db:
        revision = db.get(JobSearch, summary_id).revision

        return watches.enable(db, owner, summary_id, revision, watches.NOTICE_VERSION, clock.now(), keep=KEEP, register=sources).id


def run(database, owner, watch_id, sources, clock, trigger=watches.Trigger.MANUAL, *, stop=None, limits=watches.RunLimits()):
    return watches.run(
        database, owner, watch_id, trigger, keep=KEEP, register=lambda: sources, now=clock.now, monotonic=lambda: 0.0,
        stop=stop or threading.Event(), limits=limits,
    )


def watch_of(database, watch_id) -> JobWatch | None:
    with database.transaction() as db:
        return db.get(JobWatch, watch_id)


def seen_of(database, watch_id) -> set[tuple[str, str]]:
    with database.transaction() as db:
        return {(row.source_id, row.posting_id) for row in db.scalars(select(JobWatchSeen).where(JobWatchSeen.watch_id == watch_id))}


def new_of(database, watch_id) -> list[JobWatchPosting]:
    with database.transaction() as db:
        return list(db.scalars(select(JobWatchPosting).where(JobWatchPosting.watch_id == watch_id).order_by(JobWatchPosting.position)))


def started(database, owner, clock, careers=None, leads=None):
    """A watch with its baseline built; its id, the register and its two sources."""
    careers = careers or Watched()
    leads = leads or Watched(LEADS, LEAD_POSTINGS)
    sources = register_of(careers, leads)
    watch_id = watch_on(database, owner, clock, sources)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)
    assert result.written and result.counts["new"] == 0

    return watch_id, sources, careers, leads


def later(clock, minutes=11):
    clock.advance(minutes=minutes)


# --- the baseline, and what is new ----------------------------------------------------------------------------------------


def test_the_first_run_is_the_baseline_and_a_posting_added_later_is_new_once(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    first = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)
    watch = watch_of(database, watch_id)

    # Everything given was there before: seen, none new, out of the scope or not.
    assert first.outcome == watches.COMPLETE and first.counts["new"] == 0 and first.counts["baseline"] == first.counts["returned"] > 0
    assert (CAREERS, "c-sz-analyst") in seen_of(database, watch_id) and (CAREERS, "c-bj-senior") in seen_of(database, watch_id)
    assert watch.baseline_at == clock.now() and len(watch.baselined) == 3 and watch.completed_at == clock.now()
    assert new_of(database, watch_id) == []

    later(clock)
    careers.add(NEW_JOB)
    second = run(database, owner, watch_id, sources, clock)
    [item] = new_of(database, watch_id)

    assert second.counts["new"] == 1 and item.posting_id == NEW_JOB and item.found_at == clock.now()
    assert item.until == clock.now() + timedelta(days=14) and item.partial_read is None and item.screening["status"] == "in"
    # The kept postings' whitelist: no text, no link, no analysis.
    assert item.facts["kind"] == "lead" and item.facts["url"] is None and item.facts["title"].startswith("数据分析实习生")
    assert not {"text", "analysis", "category"} & set(item.facts)

    later(clock)
    third = run(database, owner, watch_id, sources, clock)

    assert third.counts["new"] == 0 and third.counts["seen_before"] == third.counts["returned"] and len(new_of(database, watch_id)) == 1


def test_a_search_answering_for_the_first_time_later_only_builds_its_baseline(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    careers.failing["上海"] = SourceTimeout("synthetic")
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    first = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    assert first.outcome == watches.PARTIAL and len(watch_of(database, watch_id).baselined) == 2

    # Posted meanwhile, found only by the search that never answered before: its baseline, not new.
    careers.add("c-new-sh", cities=("上海",))
    del careers.failing["上海"]
    later(clock)
    second = run(database, owner, watch_id, sources, clock)

    assert second.outcome == watches.COMPLETE and second.counts["new"] == 0 and second.counts["baseline"] >= 1
    assert (CAREERS, "c-new-sh") in seen_of(database, watch_id) and len(watch_of(database, watch_id).baselined) == 3

    later(clock)
    assert run(database, owner, watch_id, sources, clock).counts["new"] == 0


def test_skipped_kept_out_of_scope_and_the_same_job_again_are_not_new_but_counted(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    # The same numbered job again, under another id: not new.
    careers.add("c-bj-analyst-3", company="合成数据科技", identity="SYN-JOB-0001")
    careers.add("c-skip")
    careers.add("c-kept")
    careers.add("c-bin")
    # Out of the scope, and given whatever is searched for.
    careers.add("c-out", job_type="全职", listed=True)
    careers.add("c-dup-1", company="合成重复公司", identity="SYN-NEW-9")
    careers.add("c-dup-2", company="合成重复公司", identity="SYN-NEW-9")
    careers.add("c-pending", cities=("北京", "深圳"))
    careers.add("c-both", cities=("北京", "上海"), place="北京或上海（入职时可以选择）", choice=True)
    kept_posting(database, owner, clock.now(), posting_id="c-skip", decision="skip")
    kept_posting(database, owner, clock.now(), posting_id="c-kept")
    kept_posting(database, owner, clock.now(), posting_id="c-bin", deleted_at=clock.now(), purge_after=clock.now() + timedelta(days=7))
    later(clock)
    result = run(database, owner, watch_id, sources, clock)
    listed = {item.posting_id: item for item in new_of(database, watch_id)}

    assert set(listed) == {"c-dup-1", "c-pending", "c-both"}
    assert listed["c-pending"].screening["status"] == "pending" and listed["c-pending"].screening["reasons"]
    assert (result.counts["skipped"], result.counts["kept"], result.counts["out_of_scope"], result.counts["duplicates"]) == (1, 2, 1, 1)
    # Whatever came of them, all are seen now: none is ever new again.
    assert {"c-bj-analyst-3", "c-skip", "c-kept", "c-bin", "c-out", "c-dup-2"} <= {posting for _, posting in seen_of(database, watch_id)}

    # The skip taken back, the posting is still not new: seen once is seen.
    with database.transaction() as db:
        db.execute(JobPosting.__table__.delete().where(JobPosting.posting_id == "c-skip"))

    later(clock)
    again = run(database, owner, watch_id, sources, clock)

    assert again.counts["new"] == 0 and again.counts["skipped"] == 0 and len(new_of(database, watch_id)) == 3


def test_a_lead_and_the_official_posting_it_points_to_are_one_posting(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, leads = started(database, owner, clock)
    careers.add("c-new-sh", cities=("上海",))
    leads.add("l-new", title="数据分析实习生", cities=("上海",), official=PostingRef(CAREERS, "c-new-sh"))
    # A lead to an official posting seen at the baseline: not new.
    leads.add("l-old", title="数据分析实习生", cities=("上海",), official=PostingRef(CAREERS, "c-sh-analyst"))
    later(clock)
    result = run(database, owner, watch_id, sources, clock)

    assert [item.posting_id for item in new_of(database, watch_id)] == ["c-new-sh"]
    assert result.counts["duplicates"] == 1 and (LEADS, "l-old") in seen_of(database, watch_id)


def test_a_lead_to_a_posting_the_user_skipped_is_not_new(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, leads = started(database, owner, clock)
    kept_posting(database, owner, clock.now(), posting_id="c-gone", decision="skip")
    leads.add("l-to-skipped", title="数据分析实习生", cities=("上海",), official=PostingRef(CAREERS, "c-gone"))
    later(clock)
    result = run(database, owner, watch_id, sources, clock)

    assert result.counts["skipped"] == 1 and new_of(database, watch_id) == []


def test_every_new_posting_is_kept_however_many_for_14_days_and_never_made_again(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)

    for number in range(25):
        careers.add(f"c-many-{number:02d}", company=f"合成公司{number:02d}")

    later(clock)
    found_at = clock.now()
    assert run(database, owner, watch_id, sources, clock).counts["new"] == 25

    with database.transaction() as db:
        first = job_watches.new_postings(db, watch_id, clock.now(), limit=20)
        rest = job_watches.new_postings(db, watch_id, clock.now(), offset=20, limit=20)

    # The 21st to the 25th are kept as the first 20 are: a page is only what is shown.
    assert len(first) == 20 and len(rest) == 5 and not {item.id for item in first} & {item.id for item in rest}

    later(clock)
    assert run(database, owner, watch_id, sources, clock).counts["new"] == 0 and len(new_of(database, watch_id)) == 25

    clock.current = found_at + timedelta(days=14) - timedelta(microseconds=1)
    with database.transaction() as db:
        assert job_watches.new_count(db, watch_id, clock.now()) == 25 and job_watches.new_posting(db, owner, rest[0].id, clock.now())

    clock.current = found_at + timedelta(days=14)
    with database.transaction() as db:
        # Its time come, it is shown, kept and exported nowhere, removed or not.
        assert job_watches.new_count(db, watch_id, clock.now()) == 0 and job_watches.new_posting(db, owner, rest[0].id, clock.now()) is None
        assert collect_job_records(db, owner, clock.now()).watches[0]["new_postings"] == []
        assert job_watches.remove_expired(db, clock.now()) == 25

    assert new_of(database, watch_id) == [] and (CAREERS, "c-many-24") in seen_of(database, watch_id)
    assert run(database, owner, watch_id, sources, clock).counts["new"] == 0


def test_a_list_read_in_part_marks_what_it_first_read_and_is_read_once_a_run(database, owners, clock):
    owner, _ = owners
    careers = WatchedList()
    careers.partial = True
    watch_id, sources, careers, leads = started(database, owner, clock, careers=careers)
    careers.add(NEW_JOB)
    careers.calls.clear()
    later(clock)
    result = run(database, owner, watch_id, sources, clock)
    [item] = new_of(database, watch_id)

    # One call for the list, however many of the summary's searches it answers; the search of the leads, one more.
    assert careers.calls == [("listing", None)] and careers.timeouts[-1] == 15.0
    assert watch_of(database, watch_id).last_calls == 2 and leads.searches()
    assert item.partial_read == result.counts["partial"][CAREERS] and result.counts["new_partial"] == 1


def test_a_failed_search_keeps_nothing_and_a_source_that_answers_wrongly_is_asked_nothing_more(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    careers.failing["北京"] = SourceAnomaly("synthetic")
    careers.add(NEW_JOB)
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)
    watch = watch_of(database, watch_id)

    # 北京 failed, so 上海 on the same source was never asked; the leads answered.
    assert [query.city for query in careers.searches()] == ["北京"]
    assert (result.outcome, result.counts["answered"], result.counts["failed"], result.counts["unreached"]) == (watches.PARTIAL, 1, 1, 1)
    assert (CAREERS, NEW_JOB) not in seen_of(database, watch_id) and watch.completed_at is None and watch.attempted_at == clock.now()


def test_two_timeouts_in_a_row_or_a_request_held_back_stop_the_source_for_the_run(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    careers.failing["北京"] = SourceTimeout("synthetic")
    careers.failing["上海"] = SourceTimeout("synthetic")
    leads = Watched(LEADS, LEAD_POSTINGS)
    leads.failing["上海"] = HeldBack("synthetic")
    sources = register_of(careers, leads)
    words = {**BEIJING, "keywords": "数据"}
    summary_id = kept_summary(database, owner, clock.now(), (BEIJING, SHANGHAI, words, SEARCHES[2]))
    watch_id = watch_on(database, owner, clock, sources, summary_id)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    # Two timeouts of the careers site in a row: its third search is never asked.
    assert [query.keywords for query in careers.searches()] == ["数据分析", "数据分析"]
    assert (result.outcome, result.counts["failed"], result.counts["unreached"]) == (watches.FAILED, 3, 1)
    assert seen_of(database, watch_id) == set() and watch_of(database, watch_id).last_reason == "source_stopped"
    assert watch_of(database, watch_id).baselined == [] and watch_of(database, watch_id).attempted_at == clock.now()


def test_a_search_the_checks_refuse_is_never_sent(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    # Words naming a city the user cannot accept: a search refuses them, and so does a run.
    barred = {"source_id": CAREERS, "keywords": "深圳 数据分析", "filters": {"city": "北京", "job_type": "实习", "direction": None}}
    summary_id = kept_summary(database, owner, clock.now(), (BEIJING, barred))
    watch_id = watch_on(database, owner, clock, sources, summary_id)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    assert [query.keywords for query in careers.searches()] == ["数据分析"]
    assert (result.outcome, result.counts["refused"]) == (watches.PARTIAL, 1)


def test_only_the_summarys_words_and_filters_are_sent(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    leads = Watched(LEADS, LEAD_POSTINGS)
    sources = register_of(careers, leads)
    watch_id = watch_on(database, owner, clock, sources)
    run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    sent = [(query.keywords, query.city, query.job_type, query.direction) for query in careers.searches() + leads.searches()]
    assert sent == [
        (search["keywords"], search["filters"]["city"], search["filters"]["job_type"], search["filters"]["direction"])
        for search in SEARCHES
    ]


# --- when a run begins --------------------------------------------------------------------------------------------------


def test_a_failed_baseline_is_tried_again_24_hours_after_it_began_and_now_check_after_10_minutes(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    leads = Watched(LEADS, LEAD_POSTINGS)

    for source in (careers, leads):
        source.failing["北京"] = source.failing["上海"] = SourceTimeout("synthetic")

    sources = register_of(careers, leads)
    watch_id = watch_on(database, owner, clock, sources)

    # Never run: due at once.
    with database.transaction() as db:
        assert watches.due_watches(db, clock.now(), keep=KEEP, register=sources) == [(owner, watch_id)]

    began = clock.now()
    assert run(database, owner, watch_id, sources, clock, watches.Trigger.SCHEDULED).outcome == watches.FAILED

    def startable(trigger):
        with database.transaction() as db:
            return watches.startable(db, db.get(JobWatch, watch_id), trigger, clock.now(), keep=KEEP, register=sources)

    # A failed baseline is a run as any other: 15 minutes on, 定时检查 does not try it again; 现在查新 may after 10.
    clock.current = began + timedelta(minutes=15)
    assert startable(watches.Trigger.SCHEDULED) == "not_due" and startable(watches.Trigger.MANUAL) is None
    assert run(database, owner, watch_id, sources, clock, watches.Trigger.SCHEDULED).watch_id is None
    clock.current = began + timedelta(minutes=9)
    assert startable(watches.Trigger.MANUAL) == "fresh"

    clock.current = began + timedelta(hours=23, minutes=59)
    assert startable(watches.Trigger.SCHEDULED) == "not_due"
    clock.current = began + timedelta(hours=24)
    assert startable(watches.Trigger.SCHEDULED) is None


def test_an_interrupted_run_writes_nothing_and_counts_for_nothing_so_the_app_catches_up_once(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    closing = threading.Event()

    def close_the_app(query):
        closing.set()

    careers.on_search = close_the_app
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE, stop=closing)
    watch = watch_of(database, watch_id)

    assert result.outcome == watches.INTERRUPTED and not result.written and seen_of(database, watch_id) == set()
    assert (watch.last_outcome, watch.attempted_at, watch.run_token) == (watches.INTERRUPTED, None, None)

    # A run the app stopped in half way, as a crash leaves it: said interrupted at start-up, then due once.
    with database.transaction() as db:
        begun = watches.begin(db, owner, watch_id, watches.Trigger.SCHEDULED, clock.now(), keep=KEEP, register=sources)

    assert begun is not None and watches.close_unfinished(database) == 1
    assert watch_of(database, watch_id).last_reason == "not_finished"

    clock.advance(days=3)
    careers.on_search = None
    assert run(database, owner, watch_id, sources, clock, watches.Trigger.SCHEDULED).outcome == watches.COMPLETE

    with database.transaction() as db:
        assert watches.due_watches(db, clock.now(), keep=KEEP, register=sources) == []


class OpensTheWrite:
    """The database as a run sees it (F1): once armed, the next transaction the run opens, its one write, finds the app
    closing as it opens: after the last call came back, before anything is written."""

    def __init__(self, database, closing: threading.Event):
        self.database, self.closing, self.armed = database, closing, False

    def transaction(self):
        if self.armed:
            self.closing.set()

        return self.database.transaction()


@pytest.mark.parametrize("when", ["during_the_last_call", "before_the_write"])
@pytest.mark.parametrize("existing_baseline", [False, True])
@pytest.mark.parametrize("one_search", [False, True])
def test_the_app_closing_at_the_last_call_or_before_the_write_interrupts_the_run_and_writes_nothing(
    database, owners, clock, one_search, existing_baseline, when
):
    owner, _ = owners
    careers = Watched()
    leads = Watched(LEADS, LEAD_POSTINGS)
    sources = register_of(careers, leads)
    summary_id = kept_summary(database, owner, clock.now(), (BEIJING,) if one_search else SEARCHES)
    watch_id = watch_on(database, owner, clock, sources, summary_id)

    if existing_baseline:
        assert run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE).written
        clock.advance(days=1)

    before, seen = watch_of(database, watch_id), seen_of(database, watch_id)
    careers.add(NEW_JOB)
    closing = threading.Event()
    seen_by_the_run = OpensTheWrite(database, closing)
    last = careers if one_search else leads

    def at_the_last_call(query):
        # No call is let go after this one: nothing but the write can find the app closing.
        last.on_search = None

        if when == "during_the_last_call":
            closing.set()
        else:
            seen_by_the_run.armed = True

    last.on_search = at_the_last_call
    result = run(seen_by_the_run, owner, watch_id, sources, clock, watches.Trigger.SCHEDULED, stop=closing)
    after = watch_of(database, watch_id)

    # Nothing it found is written and it counts for nothing; what was committed before stays as it was.
    assert (result.outcome, result.written) == (watches.INTERRUPTED, False) and closing.is_set()
    assert seen_of(database, watch_id) == seen and new_of(database, watch_id) == []
    assert (after.baselined, after.baseline_at, after.attempted_at, after.completed_at) == (
        before.baselined, before.baseline_at, before.attempted_at, before.completed_at
    )
    assert (after.run_token, after.last_outcome, after.last_reason) == (None, watches.INTERRUPTED, "app_closing")

    # Started again, the app catches up at once, and what the interrupted run read is found then, not lost.
    with database.transaction() as db:
        assert watches.due_watches(db, clock.now(), keep=KEEP, register=sources) == [(owner, watch_id)]

    assert run(database, owner, watch_id, sources, clock, watches.Trigger.SCHEDULED).outcome == watches.COMPLETE
    assert [item.posting_id for item in new_of(database, watch_id)] == ([NEW_JOB] if existing_baseline else [])


def test_the_app_closing_once_the_write_is_decided_finds_it_made_and_nothing_goes_back(database, owners, clock, monkeypatch):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    careers.add(NEW_JOB)
    later(clock)
    closing = threading.Event()
    real = watches._write

    def closing_as_it_writes(*args, **kwargs):
        # Asked in the write's own transaction already: the write is made, and stays.
        closing.set()

        return real(*args, **kwargs)

    monkeypatch.setattr(watches, "_write", closing_as_it_writes)
    result = run(database, owner, watch_id, sources, clock, stop=closing)

    assert (result.outcome, result.written) == (watches.COMPLETE, True)
    assert [item.posting_id for item in new_of(database, watch_id)] == [NEW_JOB] and watch_of(database, watch_id).run_token is None


def test_the_seen_threshold_stops_the_next_run_and_the_run_crossing_it_is_written_whole(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    seen = len(seen_of(database, watch_id))

    with database.transaction() as db:
        db.add_all(
            JobWatchSeen(
                watch_id=watch_id, source_id=CAREERS, posting_id=f"c-old-{number:05d}", owner_id=owner, job_key=None,
                first_seen_at=clock.now(), last_seen_at=clock.now(),
            )
            for number in range(job_watches.SEEN_THRESHOLD - 1 - seen)
        )

    careers.add(NEW_JOB)
    careers.add("c-new-sh", cities=("上海",))
    later(clock)
    result = run(database, owner, watch_id, sources, clock)

    # 4,999 seen before it: it runs, and all it found is written, past 5,000.
    assert result.counts["new"] == 2 and len(seen_of(database, watch_id)) == job_watches.SEEN_THRESHOLD + 1
    careers.add("c-later")
    careers.calls.clear()
    later(clock)

    with database.transaction() as db:
        assert watches.startable(db, db.get(JobWatch, watch_id), watches.Trigger.MANUAL, clock.now(), keep=KEEP, register=sources) == "full"
        assert watches.due_watches(db, clock.now() + timedelta(days=1), keep=KEEP, register=sources) == []

    assert run(database, owner, watch_id, sources, clock).watch_id is None and careers.calls == []
    # Nothing seen is forgotten to make room.
    assert (CAREERS, "c-old-00000") in seen_of(database, watch_id)


def test_one_admit_lets_one_call_go_and_pays_for_it_once(database, owners, clock, monkeypatch):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    admitted = []
    real = watches.admit

    def counting(database_, begun, budget):
        seconds = real(database_, begun, budget)
        admitted.append((seconds, budget.calls_left))

        return seconds

    monkeypatch.setattr(watches, "admit", counting)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE, limits=watches.RunLimits(calls=2))

    # Two calls let go, each paid once; the third search never asked.
    assert admitted == [(15.0, 1), (15.0, 0)] and len(careers.searches()) == 2
    assert result.counts["unreached"] == 1 and watch_of(database, watch_id).last_reason == "calls_limit"


# --- stopping: the admit point --------------------------------------------------------------------------------------------


def stop_now(database, owner, watch_id, clock):
    with database.transaction() as db:
        watch = db.get(JobWatch, watch_id)
        watches.stop(db, owner, watch.search_record_id, watch_id, clock.now())


def test_a_watch_stopped_before_its_run_begins_calls_nothing(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    stop_now(database, owner, watch_id, clock)

    assert run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE).watch_id is None and careers.calls == []


def test_a_removal_committed_before_a_call_is_let_go_refuses_it_and_every_later_one(database, owners, clock, monkeypatch):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    real = watches.admit

    def removed_first(database_, begun, budget):
        # The removal commits first; only then is the call asked to be let go.
        stop_now(database, owner, watch_id, clock)

        return real(database_, begun, budget)

    monkeypatch.setattr(watches, "admit", removed_first)
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    assert result.outcome is None and not result.written and careers.calls == []
    assert watch_of(database, watch_id) is None and seen_of(database, watch_id) == set()


@pytest.mark.parametrize("how", ["stop", "delete_summary"])
def test_a_call_let_go_before_a_removal_completes_and_nothing_of_it_is_written(database, owners, clock, how):
    owner, _ = owners
    careers = Watched()
    careers.add(NEW_JOB)
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    summary_id = watch_of(database, watch_id).search_record_id

    def removed_meanwhile(query):
        # Let go already, and answering: the removal commits now, then the call goes on to its answer.
        careers.on_search = None

        if how == "stop":
            stop_now(database, owner, watch_id, clock)
        else:
            with database.transaction() as db:
                delete_job_record(db, owner, JobSearch, summary_id, db.get(JobSearch, summary_id).revision, clock.now())

    careers.on_search = removed_meanwhile
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    # The call let go answered; the next was never let go; nothing came of the run.
    assert len(careers.searches()) == 1 and result.outcome is None and not result.written
    assert watch_of(database, watch_id) is None and seen_of(database, watch_id) == set() and new_of(database, watch_id) == []

    if how == "delete_summary":
        # Restored, the summary brings back no watch.
        with database.transaction() as db:
            restore_job_record(db, owner, JobSearch, summary_id, db.get(JobSearch, summary_id).revision, clock.now())
            assert job_watches.watch_of(db, owner, summary_id) is None


def test_a_removal_during_the_last_call_is_caught_by_the_one_write(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    leads = Watched(LEADS, LEAD_POSTINGS)
    sources = register_of(careers, leads)
    watch_id = watch_on(database, owner, clock, sources)

    def removed_at_the_last_call(query):
        # No call is let go after this one: only the write itself can see the watch is gone.
        leads.on_search = None
        stop_now(database, owner, watch_id, clock)

    leads.on_search = removed_at_the_last_call
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    assert len(careers.searches()) == 2 and len(leads.searches()) == 1 and result.outcome is None and not result.written

    with database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobWatchSeen)) == 0
        assert db.scalar(select(func.count()).select_from(JobWatchPosting)) == 0


def test_stopped_and_started_again_at_once_the_old_run_never_touches_the_new_watch(database, owners, clock):
    owner, _ = owners
    careers = Watched()
    sources = register_of(careers)
    watch_id = watch_on(database, owner, clock, sources)
    summary_id = watch_of(database, watch_id).search_record_id
    again = []

    def stop_and_start_again(query):
        careers.on_search = None
        stop_now(database, owner, watch_id, clock)
        again.append(watch_on(database, owner, clock, sources, summary_id))

    careers.on_search = stop_and_start_again
    old = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)
    [new_id] = again
    fresh = watch_of(database, new_id)

    # The old run's answer came back late: nothing of it is written, to the old watch or the new one.
    assert old.outcome is None and fresh.last_started_at is None and fresh.baselined == [] and seen_of(database, new_id) == set()

    result = run(database, owner, new_id, sources, clock, watches.Trigger.ENABLE)
    assert result.outcome == watches.COMPLETE and len(watch_of(database, new_id).baselined) == 3


def test_a_list_read_when_the_watch_is_stopped_is_read_to_its_end_and_nothing_more_is_asked(database, owners, clock):
    owner, _ = owners
    careers = WatchedList()
    leads = Watched(LEADS, LEAD_POSTINGS)
    sources = register_of(careers, leads)
    watch_id = watch_on(database, owner, clock, sources)

    def stopped_while_listing(operation):
        if operation == "listing":
            careers.during = None
            stop_now(database, owner, watch_id, clock)

    careers.during = stopped_while_listing
    result = run(database, owner, watch_id, sources, clock, watches.Trigger.ENABLE)

    assert careers.calls == [("listing", None)] and leads.calls == [] and result.outcome is None


# --- starting, and the user's own --------------------------------------------------------------------------------------


def test_a_watch_starts_only_on_the_users_own_summary_of_sources_the_app_keeps_and_at_most_three(database, owners, clock):
    owner, other = owners
    sources = register_of()
    summary_id = kept_summary(database, owner, clock.now())

    with database.transaction() as db:
        with pytest.raises(NotFound):
            watches.enable(db, other, summary_id, 1, watches.NOTICE_VERSION, clock.now(), keep=KEEP, register=sources)

        with pytest.raises(watches.NoticeChanged):
            watches.enable(db, owner, summary_id, 1, "watch-0", clock.now(), keep=KEEP, register=sources)

        with pytest.raises(watches.Unavailable):
            watches.enable(db, owner, summary_id, 1, watches.NOTICE_VERSION, clock.now(), keep=frozenset({CAREERS}), register=sources)

    watch_on(database, owner, clock, sources, summary_id)

    with database.transaction() as db:
        with pytest.raises(NotAllowed):
            watches.enable(db, owner, summary_id, 2, watches.NOTICE_VERSION, clock.now(), keep=KEEP, register=sources)

    for _ in range(2):
        watch_on(database, owner, clock, sources)

    fourth = kept_summary(database, owner, clock.now())

    with database.transaction() as db:
        with pytest.raises(NotAllowed):
            watches.enable(db, owner, fourth, 1, watches.NOTICE_VERSION, clock.now(), keep=KEEP, register=sources)


def test_two_users_watching_the_same_postings_keep_apart(database, owners, clock):
    owner, other = owners
    careers = Watched()
    sources = register_of(careers)
    mine = watch_on(database, owner, clock, sources)
    theirs = watch_on(database, other, clock, sources)
    run(database, owner, mine, sources, clock, watches.Trigger.ENABLE)
    run(database, other, theirs, sources, clock, watches.Trigger.ENABLE)
    careers.add(NEW_JOB)
    # Skipped by the other user only.
    kept_posting(database, other, clock.now(), posting_id=NEW_JOB, decision="skip")
    later(clock)
    run(database, owner, mine, sources, clock)
    run(database, other, theirs, sources, clock)

    assert [item.posting_id for item in new_of(database, mine)] == [NEW_JOB] and new_of(database, theirs) == []
    my_summary = watch_of(database, mine).search_record_id

    with database.transaction() as db:
        item = job_watches.new_postings(db, mine, clock.now())[0]
        assert job_watches.new_posting(db, other, item.id, clock.now()) is None

        with pytest.raises(NotFound):
            records.keep_watched(db, other, item.id, clock.now(), forget=lambda record_id: None)

        with pytest.raises(NotFound):
            watches.stop(db, other, my_summary, mine, clock.now())


def test_a_new_posting_added_to_mine_is_kept_by_the_whitelist_and_then_tracked(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    careers.add(NEW_JOB)
    careers.add("c-dismiss")
    later(clock)
    run(database, owner, watch_id, sources, clock)
    items = {item.posting_id: item for item in new_of(database, watch_id)}

    with database.transaction() as db:
        row = records.keep_watched(db, owner, items[NEW_JOB].id, clock.now(), forget=lambda record_id: None)
        record_id = row.id
        search_record = db.get(JobWatch, watch_id).search_record_id
        assert records.dismiss_watched(db, owner, items["c-dismiss"].id, clock.now()) == search_record

    with database.transaction() as db:
        row = db.get(JobPosting, record_id)
        checks = db.scalar(select(func.count()).select_from(JobPostingCheck).where(JobPostingCheck.posting_record_id == record_id))
        tracked = [item.record_id for item in updates.tracked(db, owner, KEEP, clock.now())]

    assert row.facts["via"] == {"kind": "watch", "first_read_at": items[NEW_JOB].found_at.isoformat()} and row.facts["url"] is None
    assert row.search_id.startswith("search-") and checks == 0 and record_id in tracked
    assert new_of(database, watch_id) == []

    # Neither comes back as new.
    later(clock)
    assert run(database, owner, watch_id, sources, clock).counts["new"] == 0

    with database.transaction() as db:
        with pytest.raises(NotFound):
            records.keep_watched(db, owner, items[NEW_JOB].id, clock.now(), forget=lambda record_id: None)


def test_a_new_posting_kept_in_the_bin_is_not_kept_again(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    careers.add(NEW_JOB)
    later(clock)
    run(database, owner, watch_id, sources, clock)
    [item] = new_of(database, watch_id)
    kept_posting(database, owner, clock.now(), posting_id=NEW_JOB, deleted_at=clock.now(), purge_after=clock.now() + timedelta(days=7))

    with database.transaction() as db:
        with pytest.raises(records.InputRejected):
            records.keep_watched(db, owner, item.id, clock.now(), forget=lambda record_id: None)

    assert len(new_of(database, watch_id)) == 1


def test_stopped_and_cleared_everything_of_the_watch_goes_and_what_the_user_kept_stays(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    careers.add(NEW_JOB)
    careers.add("c-second")
    later(clock)
    run(database, owner, watch_id, sources, clock)
    kept = new_of(database, watch_id)[0]

    with database.transaction() as db:
        record_id = records.keep_watched(db, owner, kept.id, clock.now(), forget=lambda record_id: None).id

    summary_id = watch_of(database, watch_id).search_record_id
    stop_now(database, owner, watch_id, clock)

    with database.transaction() as db:
        assert db.get(JobPosting, record_id) is not None and db.get(JobSearch, summary_id).deleted_at is None
        assert db.scalar(select(func.count()).select_from(JobWatchPosting)) == 0
        assert db.scalar(select(func.count()).select_from(JobWatchSeen)) == 0


def test_the_export_holds_each_watch_what_it_has_seen_and_its_new_postings_never_a_text(database, owners, clock):
    owner, _ = owners
    watch_id, sources, careers, _ = started(database, owner, clock)
    careers.add(NEW_JOB)
    later(clock)
    run(database, owner, watch_id, sources, clock)

    with database.transaction() as db:
        content = collect_job_records(db, owner, clock.now())

    [entry] = content.watches
    assert content.counts()["job_watches"] == 1 and content.counts()["job_watch_postings"] == 1
    assert entry["last_run"]["outcome"] == watches.COMPLETE and {"source_id": CAREERS, "posting_id": NEW_JOB} in [
        {"source_id": item["source_id"], "posting_id": item["posting_id"]} for item in entry["seen"]
    ]
    assert [item["posting_id"] for item in entry["new_postings"]] == [NEW_JOB] and "text" not in entry["new_postings"][0]["facts"]


def test_a_summary_without_its_searches_or_with_another_sets_sources_cannot_be_watched(database, owners, clock):
    owner, _ = owners
    sources = register_of()

    assert watches.unavailable(watches.conditions_of({"sources": []}), KEEP, sources) == watches.NOT_READ
    summary_id = kept_summary(database, owner, clock.now(), ())

    with database.transaction() as db:
        found = watches.conditions_of(db.get(JobSearch, summary_id).summary)

    assert watches.unavailable(found, KEEP, sources) == watches.NO_SEARCH
    assert "现在不能用于每日查新" in watches.unavailable(watches.conditions_of({
        "sources": [{"source_id": "lever-palantir", "name": "Palantir", "access": "official_api", "evidence": "official"}],
        "scope": SCOPE,
        "searches": [{"source_id": "lever-palantir", "keywords": "Analyst", "filters": {"city": None, "job_type": None, "direction": None}}],
    }), KEEP, sources)
