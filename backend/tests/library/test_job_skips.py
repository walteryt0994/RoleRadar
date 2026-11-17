"""U3-D3c, through the pages: a posting the user skipped is not listed again by a later search.

The server reads the skips of the signed-in user on the sources a search is reviewed for, by source and id, into its
confirmation; the review says how many; the search lists none of them. Undone, changed to another decision, deleted
or deleted for good, a skip stops; restored, it is back. A review whose skips changed before it was confirmed is never
confirmed, one reviewed with none and no kept direction too. Favourites and deferrals only mark postings; reasons and
notes never reach a decision or an analysis. Another user's skips never reach a search. A search summary kept before
D3c reads and exports as it was. Synthetic postings, simulated decisions and analyses; nothing leaves the process.
"""
from concurrent.futures import Future
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.discovery import synthetic
from app.discovery.agent import SimulatedDecider
from app.discovery.web import SYNTHETIC_SOURCES
from app.job_analysis.simulation import SimulatedProvider
from app.library.job_choices import REASONS
from app.library.models import JobDecision, JobPosting, JobSearch
from job_helpers import InlineExecutor, confirmed, text_of
from test_discovery_pages import PREFERENCES, PROJECT, confirm, review, search_of, stored, task_id
from test_job_record_pages import exported, finished, inline, keep, library, rows  # noqa: F401 (fixtures)

SENTINEL = "只在本机的备注-d3c-41a7"


def post(browser, path, **fields):
    return browser.post(path, {"csrf_token": browser.csrf_token(), **fields})


def row(app, model, record_id):
    with app.state.library.database.transaction() as db:
        return db.scalar(select(model).where(model.id == record_id))


def skipped_first(app, browser, item):
    """A finished search on this experience, and its first result skipped on its keep page: (page, result, record)."""
    url = finished(browser, item)
    result = stored(app, browser, url).finished.outcome.results[0]
    assert keep(browser, url, result, action="decide", decision="skip").status_code == 303
    [record] = [record for record in rows(app, JobPosting) if record.posting_id == result.ref.posting_id]

    return url, result, record


def next_search(app, browser, item):
    """Another search on this experience, run to its end: its review page's words and the search."""
    url = search_of(review(browser, sources=[item], use_preferences=True))
    page = text_of(browser.get(url).text)
    assert confirm(browser, url).status_code == 303

    return page, stored(app, browser, url)


def listed(task) -> set:
    """Every posting a search's result says anything of: results, leads, those left out with why, those merged."""
    outcome = task.finished.outcome

    return (
        {item.ref for item in (*outcome.results, *outcome.leads)}
        | {ref for _, refs in outcome.unlisted for ref in refs}
        | set(outcome.merged)
    )


def test_a_skipped_posting_is_not_listed_again_and_the_pages_say_so(app, browser_a, library, inline):
    url, result, _ = skipped_first(app, browser_a, library)
    registers = []
    app.state.discovery.sources = replace(
        SYNTHETIC_SOURCES, make_register=lambda task: registers.append(synthetic.register()) or registers[-1]
    )
    page, task = next_search(app, browser_a, library)

    # Read by the server for this user and these sources; the review says how many.
    assert task.confirmation.skipped == (result.ref,)
    assert "你跳过的 1 个岗位这次不会再列出" in page and "只按同一来源、同一岗位编号" in page
    # Not listed, read, checked or analysed; counted once on the result page.
    assert result.ref not in listed(task) and task.finished.outcome.skipped == 1
    calls = registers[-1].adapter(result.ref.source_id).calls
    assert not [call for call in calls if call[0] in ("read", "check") and call[1] == result.ref.posting_id]
    assert "这次又遇到 1 个" in text_of(browser_a.get(f"/discover/tasks/{task.id}").text)
    # The search it was skipped from still shows it, marked: a decision after a search starts counts from the next.
    assert "已加入我的岗位：跳过" in text_of(browser_a.get(url).text)


def test_undoing_changing_deleting_restoring_and_purging_a_skip_each_change_the_next_search(app, browser_a, library, inline):
    _, result, record = skipped_first(app, browser_a, library)
    posting = f"/job-records/postings/{record.id}"

    def revision():
        return row(app, JobPosting, record.id).revision

    def hidden() -> bool:
        return result.ref not in listed(next_search(app, browser_a, library)[1])

    assert hidden()
    assert post(browser_a, f"{posting}/decide", revision=revision(), decision="").status_code == 303
    assert not hidden(), "undone"

    assert post(browser_a, f"{posting}/decide", revision=revision(), decision="favorite").status_code == 303
    _, task = next_search(app, browser_a, library)
    assert result.ref in listed(task) and "已加入我的岗位：收藏" in text_of(browser_a.get(f"/discover/tasks/{task.id}").text)

    assert post(browser_a, f"{posting}/decide", revision=revision(), decision="skip").status_code == 303
    assert hidden(), "skipped again"

    assert post(browser_a, f"{posting}/delete", expected_revision=revision()).status_code == 303
    assert not hidden(), "deleted"
    assert post(browser_a, f"{posting}/restore", expected_revision=revision()).status_code == 303
    assert hidden(), "restored as skipped"

    assert post(browser_a, f"{posting}/delete", expected_revision=revision()).status_code == 303
    assert post(browser_a, f"{posting}/purge", expected_revision=revision()).status_code == 303
    assert not hidden(), "deleted for good"


def test_a_review_whose_skips_changed_is_never_confirmed(app, browser_a, library, inline):
    _, _, record = skipped_first(app, browser_a, library)
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    assert len(stored(app, browser_a, url).confirmation.skipped) == 1
    # Undone after the review was shown.
    assert post(browser_a, f"/job-records/postings/{record.id}/decide", revision=row(app, JobPosting, record.id).revision, decision="").status_code == 303

    response = confirm(browser_a, url)

    assert response.status_code == 303 and response.headers["location"].endswith("?notice=skips_changed")
    again = response.headers["location"].split("?")[0]
    assert task_id(url) not in app.state.discovery.store._tasks
    fresh = stored(app, browser_a, again)
    assert fresh.stage == "review" and fresh.finished is None and fresh.confirmation.skipped == ()
    assert "你跳过的岗位在核对之后有变化" in text_of(browser_a.get(response.headers["location"]).text)


def test_a_review_with_no_skips_and_no_kept_direction_is_checked_too(app, browser_a, library, inline):
    url = finished(browser_a, library)
    result = stored(app, browser_a, url).finished.outcome.results[0]
    reviewing = search_of(review(browser_a, sources=[library], use_preferences=True))
    assert stored(app, browser_a, reviewing).confirmation.skipped == ()
    # Skipped after the review was shown, on the page of the search before.
    assert keep(browser_a, url, result, action="decide", decision="skip").status_code == 303

    response = confirm(browser_a, reviewing)

    assert response.status_code == 303 and response.headers["location"].endswith("?notice=skips_changed")
    assert task_id(reviewing) not in app.state.discovery.store._tasks
    fresh = stored(app, browser_a, response.headers["location"].split("?")[0])
    assert fresh.finished is None and fresh.confirmation.skipped == (result.ref,)


class Queued:
    """An executor that holds each search confirmed until the test runs it: what happens between the start and the
    run can be seen."""

    def __init__(self):
        self.pending = []

    def submit(self, function, *args, **kwargs):
        future = Future()
        self.pending.append((function, args, kwargs, future))

        return future

    def run(self):
        function, args, kwargs, future = self.pending.pop(0)
        future.set_result(function(*args, **kwargs))

    def shutdown(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("skipped_at_start", [True, False], ids=["undone_after_start", "skipped_after_start"])
def test_a_decision_after_a_search_starts_counts_from_the_next_search(app, browser_a, library, inline, skipped_at_start):
    """Confirmed and begun, the search keeps the skips it was confirmed with, whatever the user decides before it runs."""
    _, result, record = skipped_first(app, browser_a, library)
    path = f"/job-records/postings/{record.id}/decide"

    def decide(value):
        assert post(browser_a, path, revision=row(app, JobPosting, record.id).revision, decision=value).status_code == 303

    if not skipped_at_start:
        decide("")

    queued = Queued()
    app.state.discovery.executor = queued
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    assert confirm(browser_a, url).status_code == 303
    assert queued.pending and stored(app, browser_a, url).finished is None

    # Changed after the start, before the search runs.
    decide("" if skipped_at_start else "skip")
    queued.run()

    assert (result.ref not in listed(stored(app, browser_a, url))) == skipped_at_start

    app.state.discovery.executor = InlineExecutor()
    _, following = next_search(app, browser_a, library)
    assert (result.ref not in listed(following)) != skipped_at_start


def test_one_users_skips_never_reach_anothers_search(app, browser_a, browser_b, library, inline):
    _, result, _ = skipped_first(app, browser_a, library)
    assert browser_b.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    other = confirmed(browser_b, **PROJECT)

    page, task = next_search(app, browser_b, other)
    assert task.confirmation.skipped == () and "你跳过的" not in page and result.ref in listed(task)

    _, mine = next_search(app, browser_a, library)
    assert result.ref not in listed(mine)


def test_favourites_and_deferrals_only_mark_postings(app, browser_a, library, inline):
    url = finished(browser_a, library)
    before = stored(app, browser_a, url).finished.outcome
    first, second = before.results[0], before.results[1]
    assert keep(browser_a, url, first, action="decide", decision="favorite").status_code == 303
    assert keep(browser_a, url, second, action="decide", decision="defer").status_code == 303

    _, task = next_search(app, browser_a, library)
    after = task.finished.outcome

    # The same postings, in the same order and categories: only marked.
    assert [(item.ref, item.category) for item in after.results] == [(item.ref, item.category) for item in before.results]
    assert [item.ref for item in after.leads] == [item.ref for item in before.leads]
    assert task.confirmation.skipped == () and after.skipped == 0
    page = text_of(browser_a.get(f"/discover/tasks/{task.id}").text)
    assert "已加入我的岗位：收藏" in page and "已加入我的岗位：暂缓" in page


class Recording:
    """A decider passing every observation it is shown, as text, to seen."""

    def __init__(self, seen: list[str]):
        self.seen, self.inner = seen, SimulatedDecider()
        self.kind = self.inner.kind

    def decide(self, observation):
        self.seen.append(observation.text)

        return self.inner.decide(observation)


class RecordingSimulation(SimulatedProvider):
    """The simulated analysis service, passing every request it is given to seen."""

    def __init__(self, seen: list[str], **kwargs):
        super().__init__(**kwargs)
        self.seen = seen

    def generate_text(self, prompt, *args, **kwargs):
        self.seen.append(prompt)

        return super().generate_text(prompt, *args, **kwargs)


def test_reasons_and_notes_never_reach_a_decision_or_an_analysis(app, browser_a, library, inline):
    url = finished(browser_a, library)
    outcome = stored(app, browser_a, url).finished.outcome
    first, second = outcome.results[0], outcome.results[1]
    code, words = REASONS[JobDecision.SKIP][0]
    assert keep(browser_a, url, first, action="decide", decision="skip", reason=code, note=f"跳过{SENTINEL}").status_code == 303
    assert keep(browser_a, url, second, action="decide", decision="favorite", note=f"收藏{SENTINEL}").status_code == 303
    decisions, analyses = [], []
    app.state.discovery.decider_for = lambda provider: Recording(decisions)
    app.state.discovery.make_provider = lambda task: RecordingSimulation(analyses, stop=task.cancel)

    _, task = next_search(app, browser_a, library)
    shown = "\n".join(decisions + analyses)

    assert decisions and analyses and task.finished.outcome.skipped == 1
    assert SENTINEL not in shown and words not in shown
    # The decider is told how many were not listed, never which or why.
    assert "另有 1 个是用户跳过的岗位或指向它们的线索，没有列出" in "\n".join(decisions)
    assert f"{first.ref.posting_id}]" not in "\n".join(decisions)


def test_a_summary_kept_before_skips_counted_reads_and_exports_as_it_was(app, browser_a, library, inline):
    skipped_first(app, browser_a, library)
    _, task = next_search(app, browser_a, library)
    assert post(browser_a, f"/discover/tasks/{task.id}/summary", record="", revision="").status_code == 303
    [kept] = rows(app, JobSearch)
    page = f"/job-records/searches/{kept.id}"

    assert kept.summary["version"] == 3 and kept.summary["counts"]["skipped"] == 1
    assert "你跳过的岗位又遇到 1 个" in text_of(browser_a.get(page).text)
    assert "你跳过的岗位又遇到 1 个" in exported(browser_a)[1]

    # As a summary of version 2 was kept: no count of skips, and none made up for it.
    with app.state.library.database.transaction() as db:
        old = db.scalar(select(JobSearch).where(JobSearch.id == kept.id))
        counts = {key: value for key, value in old.summary["counts"].items() if key != "skipped"}
        old.summary = {**old.summary, "version": 2, "counts": counts}

    response = browser_a.get(page)
    assert response.status_code == 200 and "你跳过的岗位又遇到" not in text_of(response.text)
    data, jobs = exported(browser_a)
    [entry] = data["job_records"]["searches"]
    assert entry["summary"]["version"] == 2 and "skipped" not in entry["summary"]["counts"] and "你跳过的岗位又遇到" not in jobs


def test_the_pages_say_what_a_skip_does(app, browser_a, library, inline):
    url, result, record = skipped_first(app, browser_a, library)
    keep_page = text_of(browser_a.get(f"{url}/keep?source={result.ref.source_id}&posting={result.ref.posting_id}").text)
    records_page = text_of(browser_a.get("/job-records").text)
    posting_page = text_of(browser_a.get(f"/job-records/postings/{record.id}").text)

    assert "跳过不会影响以后的查找" not in keep_page and "跳过以后，下次查找不再列出这个岗位" in keep_page
    assert "收藏、暂缓只是标记，不影响以后的查找" in keep_page and "原因和备注只存在本机" in keep_page
    assert "不影响以后的查找" not in records_page and "跳过的岗位，下次查找不再列出" in records_page
    assert "下次查找不再列出这个岗位" in posting_page
