"""U3-D3a: postings and search summaries the user keeps, below the pages.

What a kept posting holds (a whitelist of its public facts, its answered checks apart from when it was kept), how a
write names what its page saw so that an older page never overwrites a newer record, what a decision and feedback
take, that only synthetic sources may be kept, that each user has their own records, and the recycle bin's rules:
restorable for seven days and exported meanwhile, then neither restorable nor exported and purged with its checks
and feedback; deleted for good at once on request. All data is synthetic; nothing leaves the process.
"""
import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.discovery import records, tools
from app.discovery.contracts import Access, CheckStatus, EndReason, EvidenceLevel, Lead, Outcome, PostingRef, SourceGrant
from app.discovery.records import Found, Seen
from app.discovery.sources import SourceTimeout
from app.discovery.synthetic import CAREERS
from app.library.job_choices import check_state, state_text
from discovery_helpers import WITHIN, begin, confirmation_for, make_library, ref
from app.library.errors import NotAllowed, NotFound, RecordConflict
from app.library.job_export import collect_job_records, jobs_markdown
from app.library.lifecycle import (
    RESTORE_WINDOW,
    SAME_POSTING_REASON,
    SAME_SEARCH_REASON,
    delete_job_record,
    job_bin,
    purge_due,
    request_job_record_purge,
    restore_job_record,
    trashed_job_record,
)
from app.library.models import JobFeedback, JobPosting, JobPostingCheck, JobSearch
from app.library.service import new_id

SOURCE = "synthetic-careers"
FACT_KEYS = {
    "kind", "source_name", "evidence", "title", "company", "cities", "places_stated", "job_type", "type_stated",
    "posted_on", "deadlines", "url", "apply_url", "read_at",
}


def lead(
    posting_id="c-001", *, checked_at=None, answered_status=None, check_failed_at=None, title="数据分析实习生（合成岗位，不是真实招聘）"
) -> Lead:
    """A synthetic lead; answered with OPEN when it has an answered check and no status is said (F1: always given)."""
    if checked_at is not None and answered_status is None:
        answered_status = CheckStatus.OPEN

    return Lead(
        ref=PostingRef(SOURCE, posting_id),
        title=title,
        company="合成公司甲",
        cities=("北京",),
        source="合成招聘官网",
        evidence=EvidenceLevel.OFFICIAL,
        posted_on=date(2026, 10, 1),
        deadline=date(2026, 11, 30),
        checked_at=checked_at,
        why="这次没有读取详情",
        notes=("合成说明：不该被保存",),
        job_type="实习",
        places_stated=("北京市海淀区",),
        type_stated="实习",
        status=answered_status if check_failed_at is None or (checked_at and checked_at > check_failed_at) else CheckStatus.UNKNOWN,
        answered_status=answered_status,
        check_failed_at=check_failed_at,
    )


def found(item, search="search-1", access="synthetic") -> Found:
    return Found(search, item, access, "合成招聘官网")


def count(database, model) -> int:
    with database.transaction() as db:
        return db.scalar(select(func.count()).select_from(model))


def kept(database, owner, item, *, search="search-1", seen=Seen(), now):
    with database.transaction() as db:
        row = records.keep_posting(db, owner, found(item, search), seen, now)

        return row.id, row.revision


def row_of(database, record_id) -> JobPosting:
    with database.transaction() as db:
        return db.get(JobPosting, record_id)


# --- what is kept --------------------------------------------------------------------------------------------------


def test_a_kept_posting_holds_its_public_facts_and_its_source_answer_apart_from_the_keep_time(database, owners, clock):
    a, _ = owners
    answered = clock.now() - timedelta(minutes=7)
    record_id, _ = kept(database, a, lead(checked_at=answered), now=clock.now())

    with database.transaction() as db:
        row = db.get(JobPosting, record_id)
        checks = records.checks_of(db, row)

    assert set(row.facts) == FACT_KEYS
    # A lead's facts: no address and no time of reading, as it was never read; never its notes or why.
    assert row.facts["kind"] == "lead" and row.facts["url"] is None and row.facts["read_at"] is None
    assert "合成说明" not in json.dumps(row.facts, ensure_ascii=False) and "没有读取详情" not in json.dumps(row.facts, ensure_ascii=False)
    assert row.facts["places_stated"] == ["北京市海淀区"] and row.facts["deadlines"] == [
        {"source_id": SOURCE, "posting_id": "c-001", "deadline": "2026-11-30"}
    ]
    # When the user kept it, and when its source answered: two times, never one put for the other.
    assert row.kept_at == clock.now() and row.search_id == "search-1" and row.source_access == "synthetic"
    assert [(check.checked_at, check.recorded_at, check.status, check.search_id) for check in checks] == [
        (answered, clock.now(), "open", "search-1")
    ]
    assert (row.decision, row.reasons, row.note) == (None, [], None)


def test_keeping_again_adds_each_answered_check_once_and_never_a_second_record(database, owners, clock):
    a, _ = owners
    first = clock.now() - timedelta(hours=2)
    record_id, revision = kept(database, a, lead(checked_at=first), search="search-1", now=clock.now())
    clock.advance(hours=1)
    later = clock.now() - timedelta(minutes=3)
    _, revision = kept(database, a, lead(checked_at=later), search="search-2", seen=Seen(record_id, revision), now=clock.now())
    # The same check of the same search again, and a search whose check went unanswered: nothing new, nothing changed.
    _, same = kept(database, a, lead(checked_at=later), search="search-2", seen=Seen(record_id, revision), now=clock.now())
    _, unanswered = kept(database, a, lead(checked_at=None), search="search-3", seen=Seen(record_id, revision), now=clock.now())

    with database.transaction() as db:
        checks = records.checks_of(db, db.get(JobPosting, record_id))

    assert count(database, JobPosting) == 1 and same == unanswered == revision
    assert [(check.search_id, check.checked_at) for check in checks] == [("search-2", later), ("search-1", first)]
    # The facts stay those of the search it was first kept from.
    assert row_of(database, record_id).search_id == "search-1"


def test_a_check_its_source_did_not_answer_is_not_recorded(database, owners, clock):
    a, _ = owners
    record_id, _ = kept(database, a, lead(checked_at=None), now=clock.now())

    assert count(database, JobPostingCheck) == 0 and row_of(database, record_id).kept_at == clock.now()


# --- what a page saw --------------------------------------------------------------------------------------------


def test_an_older_page_never_overwrites_a_newer_record(database, owners, clock):
    a, _ = owners
    record_id, revision = kept(database, a, lead(), now=clock.now())

    with database.transaction() as db:
        # A page that saw no record, after one was kept since: refused, nothing made.
        with pytest.raises(RecordConflict):
            records.keep_posting(db, a, found(lead()), Seen(), clock.now())

        with pytest.raises(RecordConflict):
            records.decide_on_found(db, a, found(lead()), Seen(record_id, revision - 1), "skip", [], "", clock.now())

        with pytest.raises(RecordConflict):
            records.decide_on_kept(db, a, record_id, revision + 1, "favorite", [], "", clock.now())

    with database.transaction() as db:
        delete_job_record(db, a, JobPosting, record_id, revision, clock.now())

    with database.transaction() as db:
        # The record that page saw is gone: never another one made in its place unseen.
        with pytest.raises(RecordConflict):
            records.keep_posting(db, a, found(lead()), Seen(record_id, revision + 1), clock.now())

    again, again_revision = kept(database, a, lead(), now=clock.now())

    with database.transaction() as db:
        # A page of the old record, with a revision the new one happens to have: still not the record it saw.
        with pytest.raises(RecordConflict):
            records.decide_on_found(db, a, found(lead()), Seen(record_id, again_revision), "skip", [], "", clock.now())

    assert again != record_id and row_of(database, again).decision is None


# --- decisions and feedback -------------------------------------------------------------------------------------


def test_a_decision_takes_its_own_listed_reasons_and_a_short_note_and_can_be_taken_back(database, owners, clock):
    a, _ = owners

    for decision, reasons, note in (("maybe", [], ""), ("skip", ["reachable"], ""), ("skip", [], "长" * 201)):
        with database.transaction() as db, pytest.raises(records.InputRejected):
            records.decide_on_found(db, a, found(lead()), Seen(), decision, reasons, note, clock.now())

    # Nothing of a rejected form was kept, not even the posting.
    assert count(database, JobPosting) == 0

    with database.transaction() as db:
        row = records.decide_on_found(db, a, found(lead()), Seen(), "skip", ["place", "type", "place"], "  地点   太远 ", clock.now())
        record_id, revision = row.id, row.revision

    row = row_of(database, record_id)
    assert (row.decision, row.reasons, row.note, row.decided_at) == ("skip", ["type", "place"], "地点 太远", clock.now())

    with database.transaction() as db:
        revision = records.decide_on_kept(db, a, record_id, revision, "favorite", ["company"], "", clock.now()).revision

    assert (row_of(database, record_id).decision, row_of(database, record_id).reasons) == ("favorite", ["company"])

    with database.transaction() as db:
        records.decide_on_kept(db, a, record_id, revision, "", [], "", clock.now())

    row = row_of(database, record_id)
    assert (row.decision, row.reasons, row.note, row.decided_at) == (None, [], None, None)
    assert count(database, JobPosting) == 1


def test_feedback_takes_listed_facts_and_reasons_and_leaves_the_decision_alone(database, owners, clock):
    a, _ = owners

    with database.transaction() as db:
        row = records.decide_on_found(db, a, found(lead()), Seen(), "defer", ["timing"], "", clock.now())
        record_id, revision = row.id, row.revision

    for field, reason in (("analysis", "wrong"), ("place", "angry")):
        with database.transaction() as db, pytest.raises(records.InputRejected):
            records.feedback_on_kept(db, a, record_id, revision, field, reason, "", clock.now())

    with database.transaction() as db:
        revision = records.feedback_on_kept(db, a, record_id, revision, "place", "wrong", "其实在上海", clock.now()).revision

    clock.advance(minutes=1)

    with database.transaction() as db:
        records.feedback_on_kept(db, a, record_id, revision, "job_type", "wording", "", clock.now())

    with database.transaction() as db:
        row = db.get(JobPosting, record_id)
        feedback = [(item.field, item.reason, item.note, item.search_id) for item in records.feedback_of(db, row)]

    assert feedback == [("place", "wrong", "其实在上海", "search-1"), ("job_type", "wording", None, "search-1")]
    assert (row.decision, row.reasons) == ("defer", ["timing"])


# --- what may be kept -----------------------------------------------------------------------------------------------


def test_only_sources_the_set_lists_with_a_keepable_access_are_kept(database, owners, clock):
    """D3a kept synthetic sources alone; since U3-D6 a source reached by RoleRadar's own requests is kept too, but only
    where the set the app runs on lists it (the D6 batch, by its admission's save permission). A source not listed is
    never kept, and an access that is neither (a search service's) never is, whatever a set or a caller says."""
    a, _ = owners
    synthetic = SourceGrant(SOURCE, "合成招聘官网", Access.SYNTHETIC, EvidenceLevel.OFFICIAL)
    real = SourceGrant("lever-palantir", "Palantir（Lever 公开职位接口）", Access.LOCAL_FETCH, EvidenceLevel.OFFICIAL)
    searched = SourceGrant("search-x", "某搜索服务", Access.SEARCH_SERVICE, EvidenceLevel.LEAD)

    assert records.keepable_grant(synthetic, frozenset({SOURCE}))
    assert records.keepable_grant(real, frozenset({"lever-palantir"}))
    assert not records.keepable_grant(real, frozenset())
    assert not records.keepable_grant(synthetic, frozenset())
    assert not records.keepable_grant(searched, frozenset({"search-x"}))

    finished = SimpleNamespace(outcome=SimpleNamespace(results=(), leads=(lead(),)))
    mixed = SimpleNamespace(id="search-9", confirmation=SimpleNamespace(sources=(synthetic, real)), finished=finished)

    # The real source not listed by the set: the search is not kept, nothing of it.
    assert not records.search_keepable(mixed, frozenset({SOURCE}))

    with pytest.raises(records.KeepRefused):
        records.found_in(mixed, frozenset({SOURCE}), SOURCE, "c-001")

    # Whatever a caller passes, a posting reached by a search service is not written.
    with database.transaction() as db, pytest.raises(records.KeepRefused):
        records.keep_posting(db, a, found(lead(), access="search_service"), Seen(), clock.now())

    # A record of another access, were there one, takes no decision and no feedback either.
    with database.transaction() as db:
        row = JobPosting(
            id=new_id(), owner_id=a, source_id="search-x", posting_id="p-1", source_access="search_service",
            facts={}, search_id="search-0", decision=None, reasons=[], note=None, decided_at=None, revision=1,
            kept_at=clock.now(), updated_at=clock.now(), deleted_at=None, purge_after=None, deletion_id=None,
        )
        db.add(row)

    for write in (
        lambda db: records.decide_on_kept(db, a, row.id, 1, "skip", [], "", clock.now()),
        lambda db: records.feedback_on_kept(db, a, row.id, 1, "place", "wrong", "", clock.now()),
    ):
        with database.transaction() as db, pytest.raises(records.KeepRefused):
            write(db)

    assert row_of(database, row.id).revision == 1 and count(database, JobFeedback) == 0


def test_each_user_keeps_and_changes_only_their_own_records(database, owners, clock):
    a, b = owners
    mine, revision = kept(database, a, lead(), now=clock.now())
    theirs, _ = kept(database, b, lead(), now=clock.now())

    assert mine != theirs

    for write in (
        lambda db: records.decide_on_kept(db, b, mine, revision, "skip", [], "", clock.now()),
        lambda db: records.feedback_on_kept(db, b, mine, revision, "place", "wrong", "", clock.now()),
        lambda db: delete_job_record(db, b, JobPosting, mine, revision, clock.now()),
    ):
        with database.transaction() as db, pytest.raises(NotFound):
            write(db)

    with database.transaction() as db:
        assert [row.id for row in records.postings_in_use(db, a)] == [mine]
        assert [row.id for row in records.postings_in_use(db, b)] == [theirs]


# --- the recycle bin --------------------------------------------------------------------------------------------------


def binned_posting(database, owner, clock, **kwargs):
    """A kept posting with a check, a decision and feedback, moved to the recycle bin; returns its id and revision."""
    answered = clock.now() - timedelta(minutes=5)

    with database.transaction() as db:
        row = records.decide_on_found(db, owner, found(lead(checked_at=answered, **kwargs)), Seen(), "skip", ["place"], "", clock.now())
        row = records.feedback_on_kept(db, owner, row.id, row.revision, "place", "wrong", "", clock.now())
        row = delete_job_record(db, owner, JobPosting, row.id, row.revision, clock.now())

        return row.id, row.revision


def test_a_deleted_record_can_be_restored_for_seven_days_and_is_exported_meanwhile(database, owners, clock):
    a, _ = owners
    record_id, revision = binned_posting(database, a, clock)

    with database.transaction() as db:
        listed = job_bin(db, a, clock.now())
        exported = collect_job_records(db, a, clock.now())

    assert [row.id for row in listed.postings] == [record_id]
    assert row_of(database, record_id).purge_after == clock.now() + RESTORE_WINDOW
    [entry] = exported.postings
    assert entry["kept_as"] == "recycle_bin" and len(entry["checks"]) == 1 and len(entry["feedback"]) == 1

    clock.advance(days=6, hours=23)

    with database.transaction() as db:
        restore_job_record(db, a, JobPosting, record_id, revision, clock.now())

    row = row_of(database, record_id)
    assert row.deleted_at is None and row.decision == "skip"
    assert (count(database, JobPostingCheck), count(database, JobFeedback)) == (1, 1)


def test_past_its_deadline_a_record_is_neither_restorable_nor_exported_and_goes_with_its_checks(database, owners, clock):
    a, _ = owners
    record_id, revision = binned_posting(database, a, clock)
    clock.advance(days=7)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            trashed_job_record(db, a, JobPosting, record_id, clock.now())

        with pytest.raises(NotFound):
            restore_job_record(db, a, JobPosting, record_id, revision, clock.now())

        assert not job_bin(db, a, clock.now()) and not collect_job_records(db, a, clock.now())

    report = purge_due(database, clock.now())

    assert report.done == 1
    assert (count(database, JobPosting), count(database, JobPostingCheck), count(database, JobFeedback)) == (0, 0, 0)

    with database.transaction() as db:
        assert [job.target_type for job in job_bin(db, a, clock.now()).finished_cleanups] == ["job_posting"]


def test_deleting_for_good_before_the_deadline_takes_effect_at_once(database, owners, clock):
    a, _ = owners
    record_id, revision = binned_posting(database, a, clock)

    with database.transaction() as db:
        request_job_record_purge(db, a, JobPosting, record_id, revision, clock.now())

    with database.transaction() as db:
        with pytest.raises(NotFound):
            restore_job_record(db, a, JobPosting, record_id, revision + 1, clock.now())

        assert not collect_job_records(db, a, clock.now())

    assert purge_due(database, clock.now()).done == 1
    assert (count(database, JobPosting), count(database, JobPostingCheck), count(database, JobFeedback)) == (0, 0, 0)


def test_an_older_record_is_not_restored_over_one_in_use_for_the_same_posting(database, owners, clock):
    a, _ = owners
    old, old_revision = binned_posting(database, a, clock)
    new, new_revision = kept(database, a, lead(), now=clock.now())

    with database.transaction() as db, pytest.raises(NotAllowed, match="已经有这个岗位的一条记录"):
        restore_job_record(db, a, JobPosting, old, old_revision, clock.now())

    assert SAME_POSTING_REASON

    with database.transaction() as db:
        delete_job_record(db, a, JobPosting, new, new_revision, clock.now())

    with database.transaction() as db:
        restore_job_record(db, a, JobPosting, old, old_revision, clock.now())

    assert row_of(database, old).deleted_at is None


def test_a_kept_search_summary_follows_the_same_bin_rules(database, owners, clock):
    a, _ = owners

    def summary(search_id="search-1") -> str:
        with database.transaction() as db:
            row = JobSearch(
                id=new_id(), owner_id=a, search_id=search_id, summary={"version": 1}, revision=1, kept_at=clock.now(),
                updated_at=clock.now(), deleted_at=None, purge_after=None, deletion_id=None,
            )
            db.add(row)

            return row.id

    old = summary()

    with database.transaction() as db:
        delete_job_record(db, a, JobSearch, old, 1, clock.now())

    new = summary()

    with database.transaction() as db, pytest.raises(NotAllowed, match=SAME_SEARCH_REASON[:12]):
        restore_job_record(db, a, JobSearch, old, 2, clock.now())

    with database.transaction() as db:
        request_job_record_purge(db, a, JobSearch, old, 2, clock.now())

    assert purge_due(database, clock.now()).done == 1

    with database.transaction() as db:
        assert [row.id for row in records.searches_in_use(db, a)] == [new]
        assert [entry["id"] for entry in collect_job_records(db, a, clock.now()).searches] == [new]


# --- the export -----------------------------------------------------------------------------------------------------


def test_the_export_says_when_it_was_kept_and_when_its_source_answered_apart(database, owners, clock):
    a, _ = owners
    answered = clock.now() - timedelta(hours=3)

    with database.transaction() as db:
        records.decide_on_found(db, a, found(lead(checked_at=answered)), Seen(), "favorite", ["direction"], "合成备注", clock.now())

    with database.transaction() as db:
        content = collect_job_records(db, a, clock.now())

    [entry] = content.postings
    [check] = entry["checks"]

    assert entry["kept_as"] == "in_use" and entry["decision"] == "favorite" and entry["note"] == "合成备注"
    assert check["source_answered_at"] != entry["kept_at"] and check["recorded_at"] == entry["kept_at"]
    assert content.counts() == {
        "job_postings": 1, "job_recommendations": 0, "job_searches": 0, "job_analyses": 0, "job_recycle_bin": 0,
        "job_watches": 0, "job_watch_postings": 0, "job_posting_texts": 0,
    }

    text = jobs_markdown(content, clock.now())

    assert "保存时间" in text and "（不是来源核对的时间）" in text and "- 来源核对：核对时仍公开发布" in text
    assert "我的决定：收藏；原因：方向对" in text and "合成说明" not in text


# --- F1: what a check said, from the search's tools to the record, its pages and the export ------------------------------


def checked_through_the_tools(database, owners, clock, scenario):
    """Keep one synthetic posting after the search's own tools checked it as the scenario says; return the lead, the
    kept checks, the record's state in words and its export entry."""
    library = make_library(database, owners, clock)
    confirmation = confirmation_for(library)
    run = begin(database, clock, confirmation)
    target = ref("c-closed" if scenario == "closed_then_unanswered" else "c-bj-analyst")
    adapter = run.register.adapter(CAREERS)

    assert tools.search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE
    assert tools.get_posting(run, [target]).outcome is Outcome.DONE

    if scenario == "never_answered":
        adapter.faults[("check", target.posting_id)] = SourceTimeout("synthetic")
        assert tools.check_posting(run, [target]).outcome is Outcome.FAILED
    elif scenario != "never_checked":
        assert tools.check_posting(run, [target]).outcome is Outcome.DONE
        clock.advance(minutes=1)
        adapter.faults[("check", target.posting_id)] = SourceTimeout("synthetic")
        assert tools.check_posting(run, [target]).outcome is Outcome.FAILED

    run.end(EndReason.LIMIT)
    outcome = run.outcome()
    item = next(entry for entry in outcome.leads if entry.ref == target)
    task = SimpleNamespace(id="search-f1", confirmation=confirmation, finished=SimpleNamespace(outcome=outcome))
    found = records.found_in(task, frozenset(grant.source_id for grant in confirmation.sources), CAREERS, target.posting_id)

    with database.transaction() as db:
        row = records.keep_posting(db, library.owner, found, Seen(), clock.now())
        checks = [(check.status, check.checked_at) for check in records.checks_of(db, row)]
        state = state_text(check_state(records.checks_of(db, row)), lambda when: when.isoformat())
        [entry] = collect_job_records(db, library.owner, clock.now()).postings

    return item, checks, state, entry


@pytest.mark.parametrize(
    ("scenario", "answered", "statuses", "state_starts"),
    [
        # The case Codex found: closed at its last answer, then no answer; never kept as open.
        ("closed_then_unanswered", CheckStatus.CLOSED, ["unanswered", "closed"], "岗位状态待确认：最近一次核对"),
        ("open_then_unanswered", CheckStatus.OPEN, ["unanswered", "open"], "岗位状态待确认：最近一次核对"),
        ("never_answered", None, ["unanswered"], "岗位状态待确认：最近一次核对"),
        ("never_checked", None, [], "没有来源核对记录"),
    ],
)
def test_a_check_is_kept_as_the_search_knew_it_and_a_check_without_an_answer_apart(
    database, owners, clock, scenario, answered, statuses, state_starts
):
    item, checks, state, entry = checked_through_the_tools(database, owners, clock, scenario)

    # The search's own facts: the status at its end, what was answered at the last answer, when a check got none.
    assert item.answered_status is answered
    assert item.status is (None if scenario == "never_checked" else CheckStatus.UNKNOWN)
    assert (item.check_failed_at is None) == (scenario == "never_checked")
    assert (item.checked_at is None) == (answered is None)
    # Kept as each was: an answer at its own time with what it said, and the unanswered one at the time it was tried.
    assert [status for status, _ in checks] == statuses
    assert ("unanswered", item.check_failed_at) in checks or scenario == "never_checked"
    assert answered is None or (answered.value, item.checked_at) in checks
    # The record and the export say "to be confirmed", never "still open" for now.
    assert state.startswith(state_starts) and entry["check_state"].startswith(state_starts)
    assert not entry["check_state"].startswith("核对时仍公开发布")
    assert [check["status"] for check in entry["checks"]] == list(reversed(statuses))

    for check in entry["checks"]:
        assert (check["source_answered_at"] is None) == (check["status"] == "unanswered") == (check["unanswered_at"] is not None)

    if answered is CheckStatus.CLOSED:
        assert "最后一次有回答" in state and "核对时已关闭" in state and "仍公开发布" not in state


def test_an_answer_the_search_kept_no_status_for_is_kept_as_unknown_never_as_open(database, owners, clock):
    a, _ = owners
    item = replace_answer(lead(checked_at=clock.now() - timedelta(minutes=2)), None)
    record_id, _ = kept(database, a, item, now=clock.now())

    with database.transaction() as db:
        checks = records.checks_of(db, db.get(JobPosting, record_id))

    assert [check.status for check in checks] == ["unknown"]
    assert "仍公开发布" not in state_text(check_state(checks), lambda when: when.isoformat())


def replace_answer(item: Lead, answered_status) -> Lead:
    return replace(item, answered_status=answered_status, status=None)
