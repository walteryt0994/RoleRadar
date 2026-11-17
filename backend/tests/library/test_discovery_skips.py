"""U3-D3c, the search tools: postings the user skipped are never taken, read, checked or analysed in a later search.

A skipped posting, by its source and id, is not taken when a search gives it, nor is a lead pointing to it; a lead that
only once read turns out to point to one (none named, or another named when searched) is hidden from then on: what it
points to never becomes readable, neither is shown or acted on again, its read stays counted, and the posting skipped is
never named to the decider. However often and however a skipped posting is come across, it counts once. A search with
no skips is as before; the confirmation's fingerprint covers the skips, in order and each once. Synthetic sources only.
"""
from dataclasses import replace

import pytest

from app.discovery import agent, synthetic
from app.discovery.contracts import EndReason, Limits, Outcome, search_fingerprint
from app.discovery.sources import Register
from app.discovery.synthetic import CAREERS, LEADS, LEAD_POSTINGS, POSTINGS, SyntheticSource
from app.discovery.tools import HIDDEN, analyze_postings, check_posting, finish, get_posting, search_postings
from discovery_helpers import WITHIN, begin, confirmation_for, make_library, ref


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


class Pointing(SyntheticSource):
    """A source of leads whose read detail points to another official posting than its search listing did."""

    def __init__(self, source_id, postings, pointing):
        super().__init__(source_id, postings)
        self.pointing = pointing

    def read(self, ref, timeout):
        detail = super().read(ref, timeout)
        target = self.pointing.get(ref.posting_id)

        return detail if target is None else replace(detail, posting=replace(detail.posting, official=target))


class Repointing(SyntheticSource):
    """A source of leads listing some of them, from its second search on, as pointing to another official posting."""

    def __init__(self, source_id, postings, later):
        super().__init__(source_id, postings)
        self.later, self.searched = later, 0

    def search(self, query, timeout):
        found = super().search(query, timeout)
        self.searched += 1

        if self.searched == 1:
            return found

        return tuple(
            replace(posting, official=self.later[posting.ref.posting_id]) if posting.ref.posting_id in self.later else posting
            for posting in found
        )


def register_with(leads=LEAD_POSTINGS, pointing=None) -> Register:
    sources = {CAREERS: SyntheticSource(CAREERS, POSTINGS)}
    sources[LEADS] = Pointing(LEADS, leads, pointing or {}) if pointing is not None else SyntheticSource(LEADS, leads)

    return Register(synthetic.ENTRIES, sources)


def run_skipping(database, clock, library, *skipped, register=None):
    # Room for the refusals a test asks for on purpose: by default two end the search.
    confirmation = replace(confirmation_for(library, limits=Limits(refusals=10)), skipped=tuple(skipped))

    return begin(database, clock, confirmation, register=register or synthetic.register())


def reads_of(register, source_id) -> list[str]:
    return [posting_id for call, posting_id in register.adapter(source_id).calls if call == "read"]


def test_a_skipped_posting_is_never_taken_read_checked_or_analysed(database, clock, library):
    skipped = ref("c-bj-analyst")
    register = synthetic.register()
    run = run_skipping(database, clock, library, skipped, register=register)

    searched = search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN)
    again = search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN)

    assert searched.outcome is Outcome.DONE and "另有 1 个是用户跳过的岗位或指向它们的线索，没有列出" in searched.summary
    # Neither result names it; the same posting found again is still one skipped.
    assert "c-bj-analyst]" not in searched.summary and "c-bj-analyst]" not in again.summary
    assert skipped not in run.found and skipped not in run.known and run.skipped_seen == {skipped}
    # The same company's repost, under an id of its own, is another posting: found as ever.
    assert ref("c-bj-analyst-repost") in run.found

    for result in (get_posting(run, [skipped]), check_posting(run, [skipped]), analyze_postings(run, [skipped])):
        assert result.outcome is Outcome.REFUSED and "不是这次检索得到的岗位" in result.reason

    assert "c-bj-analyst" not in reads_of(register, CAREERS)
    run.end(EndReason.LIMIT)
    outcome = run.outcome()

    assert outcome.skipped == 1
    assert skipped not in {item.ref for item in (*outcome.results, *outcome.leads)}
    assert skipped not in {posting for _, refs in outcome.unlisted for posting in refs}


def test_a_lead_to_a_skipped_posting_is_not_listed_and_each_posting_counts_once(database, clock, library):
    skipped = ref("c-sh-analyst")
    # Two leads pointing to the same posting the user skipped.
    second = synthetic._posting(
        LEADS, "l-sh-analyst-2", "数据分析实习生", "合成零售", ("上海",), "（搜索结果摘要）招聘数据分析实习生。",
        posted=None, official=skipped,
    )
    register = register_with(leads=(*LEAD_POSTINGS, second))
    run = run_skipping(database, clock, library, skipped, register=register)

    leads = search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN)
    careers = search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN)

    assert "另有 2 个" in leads.summary and "另有 1 个" in careers.summary
    assert ref("l-sh-analyst", LEADS) not in run.found and ref("l-sh-analyst-2", LEADS) not in run.found
    assert ref("l-sh-labelling", LEADS) in run.found and skipped not in run.known
    run.end(EndReason.LIMIT)

    # Two leads and the posting itself: one posting skipped.
    assert run.outcome().skipped == 1


@pytest.mark.parametrize("searched_as", ["pointing_nowhere", "pointing_elsewhere"])
def test_a_lead_read_and_found_to_point_to_a_skipped_posting_is_hidden_and_its_read_counted(database, clock, library, searched_as):
    skipped = ref("c-bj-analyst")
    lead_id = "l-sh-labelling" if searched_as == "pointing_nowhere" else "l-sh-analyst"
    lead = ref(lead_id, LEADS)
    register = register_with(pointing={lead_id: skipped})
    run = run_skipping(database, clock, library, skipped, register=register)

    assert search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN).outcome is Outcome.DONE
    known_before = set(run.known)
    assert lead in run.found and lead in {candidate.ref for candidate in agent._candidates(run)}

    read = get_posting(run, [lead])

    # Read and counted as read, but the posting it points to is never named, nor made readable.
    assert read.outcome is Outcome.DONE and run.reads == 1 and reads_of(register, LEADS) == [lead_id]
    assert "指向用户跳过的岗位" in read.summary and "c-bj-analyst" not in read.summary
    assert skipped not in run.known and run.known - known_before == set()
    assert lead in run.hidden and run.skipped_seen == {skipped}

    # Neither shown to the decider nor acted on again.
    assert lead not in {candidate.ref for candidate in agent._candidates(run)}
    assert all("c-bj-analyst" not in candidate.line and candidate.official != skipped for candidate in agent._candidates(run))

    for result in (get_posting(run, [lead]), check_posting(run, [lead]), analyze_postings(run, [lead]), get_posting(run, [skipped])):
        assert result.outcome is Outcome.REFUSED

    assert HIDDEN in get_posting(run, [lead]).reason
    assert reads_of(register, LEADS) == [lead_id] and "c-bj-analyst" not in reads_of(register, CAREERS)

    if searched_as == "pointing_elsewhere":
        # What it named when searched stays as readable as it was: only the skipped one is never made known.
        assert ref("c-sh-analyst") in run.known

    run.end(EndReason.LIMIT)
    outcome = run.outcome()

    assert outcome.skipped == 1 and lead not in {item.ref for item in (*outcome.results, *outcome.leads)}
    assert lead not in {posting for _, refs in outcome.unlisted for posting in refs}


@pytest.mark.parametrize("grouped", [False, True], ids=["no_cache", "grouped_before"])
@pytest.mark.parametrize("searched_as", ["pointing_nowhere", "pointing_elsewhere"])
def test_a_lead_found_again_pointing_to_a_skipped_posting_is_withdrawn(database, clock, library, searched_as, grouped):
    """U3-D3c F1: a lead an earlier search of this one took, listed by a later search as pointing to a posting the user
    skipped, is withdrawn as one read and so seen is; nothing else found is touched."""
    skipped = ref("c-bj-analyst")
    lead_id, other_id = ("l-sh-labelling", "l-sh-analyst") if searched_as == "pointing_nowhere" else ("l-sh-analyst", "l-sh-labelling")
    lead, other, independent = ref(lead_id, LEADS), ref(other_id, LEADS), ref("c-sh-analyst")
    leads = Repointing(LEADS, LEAD_POSTINGS, {lead_id: skipped})
    register = Register(synthetic.ENTRIES, {CAREERS: SyntheticSource(CAREERS, POSTINGS), LEADS: leads})
    run = run_skipping(database, clock, library, skipped, register=register)

    assert search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN).outcome is Outcome.DONE
    assert search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN).outcome is Outcome.DONE
    assert {lead, other, independent} <= {candidate.ref for candidate in agent._candidates(run)}

    if grouped:
        # Grouped before the change, as listing the candidates for a decision does.
        assert any(found.ref == lead for group in run.groups() for found in group)

    again = search_postings(run, LEADS, "数据", city="上海", **WITHIN)

    # Said by the lead's own id, never by the posting skipped; the request it took stays counted.
    assert again.outcome is Outcome.DONE and f"之前列出的 [{LEADS}/{lead_id}] 这次指向用户跳过的岗位，已撤下" in again.summary
    assert "c-bj-analyst" not in again.summary and leads.searched == 2 and len(run.searches) == 3
    assert lead in run.hidden and run.skipped_seen == {skipped} and skipped not in run.known

    # Withdrawn from the candidates and from the groups, cached or not; nothing else found is touched.
    candidates = {candidate.ref for candidate in agent._candidates(run)}
    assert lead not in candidates and {other, independent} <= candidates
    grouped_now = {found.ref for group in run.groups() for found in group}
    assert lead not in grouped_now and {other, independent} <= grouped_now

    for result in (get_posting(run, [lead]), check_posting(run, [lead]), analyze_postings(run, [lead])):
        assert result.outcome is Outcome.REFUSED and HIDDEN in result.reason

    # What it named when first searched, found on its own, stays readable.
    assert get_posting(run, [independent]).outcome is Outcome.DONE

    run.end(EndReason.LIMIT)
    outcome = run.outcome()

    said = (
        {item.ref for item in (*outcome.results, *outcome.leads)}
        | {posting for _, refs in outcome.unlisted for posting in refs}
        | set(outcome.merged)
    )

    # Said nowhere in the outcome; the other lead still is (as a lead, or merged with the posting it points to).
    assert outcome.skipped == 1 and lead not in said and other in said


def test_without_skips_a_search_is_as_before(database, clock, library):
    plain = begin(database, clock, confirmation_for(library, limits=Limits(refusals=10)))
    empty = run_skipping(database, clock, library)

    for run in (plain, empty):
        result = search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN)
        assert "跳过" not in result.summary
        run.end(EndReason.LIMIT)

    assert plain.outcome() == empty.outcome() and plain.outcome().skipped == 0
    assert set(plain.found) == set(empty.found) and plain.known == empty.known


def test_the_fingerprint_covers_the_skips_in_order_each_once(library):
    confirmation = confirmation_for(library)
    one, two = ref("c-bj-analyst"), ref("c-sh-analyst")

    assert search_fingerprint(confirmation) != search_fingerprint(replace(confirmation, skipped=(one,)))
    assert search_fingerprint(replace(confirmation, skipped=(one,))) != search_fingerprint(replace(confirmation, skipped=(two,)))
    assert search_fingerprint(replace(confirmation, skipped=(one, two))) == search_fingerprint(replace(confirmation, skipped=(two, one, two)))
    # The default and an explicit empty list read alike under the current algorithm. Not a check against the
    # algorithm before U3-D3c: that one had no "skipped" at all, so an empty list's fingerprint differs from it.
    assert search_fingerprint(confirmation) == search_fingerprint(replace(confirmation, skipped=()))


def test_a_skipped_posting_does_not_keep_a_search_from_saying_nothing_suitable(database, clock, library):
    """Skipped by the user is settled: it is no posting without a conclusion."""
    first = begin(database, clock, confirmation_for(library))
    search_postings(first, CAREERS, "数据分析 实习生", city="北京", **WITHIN)
    every = tuple(first.found)
    assert every

    run = run_skipping(database, clock, library, *every)
    search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN)

    assert not run.found and run.skipped_seen == set(every)
    ended = finish(run, EndReason.NO_RESULTS)

    assert ended.outcome is Outcome.DONE and run.ended in (EndReason.NO_RESULTS, EndReason.PARTLY_NO_RESULTS)
    assert run.outcome().skipped == len(every) and not run.outcome().results and not run.outcome().leads
