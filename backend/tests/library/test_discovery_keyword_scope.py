"""U3-D2-E4 F1, offline: a source's keyword scope, held before the source is asked, told to every decision.

Validation B (2026-11-12) searched a Lever board with "Software Engineer Intern New York", New York and internships
as filters, and found nothing: a Lever board matches keywords only as whole words of a posting's title, team,
department and type, so no title held "New York", and "Intern" is no whole word of "Internship". The decision
instructions had let keywords hold a city.

Here, over synthetic replies in Lever's documented shape only (nothing is sent anywhere, no register admits a source
of the product): the failure as the adapter has it; the board's declaration; the tools refusing such keywords before
the list is read, saying which words and what is left, never taking anything out of them nor relaxing a filter;
what a decision is told; the simulated decider making the search again as told and going on to read, check and a
simulated analysis; a list kept being used again with no request; a search finding nothing saying it is no sign the
board has none; and sources that say nothing held to nothing new.
"""
import json
from dataclasses import replace

import pytest

from app.discovery import lever
from app.discovery.agent import DECISION_INSTRUCTIONS, Combination, SimulatedDecider, make_action, observe, run_agent
from app.discovery.contracts import (
    Dimension,
    DimensionScope,
    EndReason,
    Others,
    Outcome,
    PostingRef,
    Screen,
    SearchFix,
    SearchScope,
    Tool,
)
from app.discovery.lever import MAX_PAGES, PAGE_SIZE, LeverListing
from app.discovery.sources import Query, keyword_scope
from app.discovery.synthetic import CAREERS, PAGED, PAGED_GRANTS, paged_register
from app.discovery.tools import named_words, search_postings
from app.outbound_review import review
from discovery_helpers import SENIOR_SCOPE, begin, confirmation_for, make_library
from job_helpers import RecordingProvider
from lever_helpers import (
    NORTH,
    SALES,
    ReplayTransport,
    Ticks,
    detail_url,
    grants,
    guard,
    list_url,
    make_demo_library,
    pid,
    posting,
    serve_details,
    serve_listing,
    verified_register,
)

# Validation B's frozen search, on a synthetic board: New York and internships only, Software Engineer listed.
B_SCOPE = SearchScope(
    city=DimensionScope(("New York",), Others.EXCLUDED, ("New York",)),
    job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope(("Software Engineer",), Others.INCLUDED, ("Software Engineer",)),
    targets=("Software Engineer Intern",),
)
# The model's first action in validation B, as the run's trace kept it.
B_WORDS = "Software Engineer Intern New York"
B_FILTERS = dict(city="New York", job_type="实习", direction="Software Engineer")
NY = "New York, NY"
# The board: postings numbered by what they stand for.
INTERN_TITLE, INTERNSHIP_TYPE, INTERNSHIP_TITLE, OTHER_CITY, OTHER_TYPE, NO_PLACE, NO_TYPE, OTHER_TITLE = range(1, 9)


def board() -> list[dict]:
    return [
        posting(INTERN_TITLE, "Software Engineer Intern", location=NY, commitment="Intern", team="Engineering"),
        # An internship by its type alone: "Intern" is in neither its title nor its type.
        posting(INTERNSHIP_TYPE, "Software Engineer", location=NY, commitment="Internship", team="Engineering"),
        posting(INTERNSHIP_TITLE, "Software Engineer Internship", location=NY, commitment="Internship", team="Engineering"),
        posting(OTHER_CITY, "Software Engineer Intern", location="San Francisco, CA", commitment="Intern", team="Engineering"),
        posting(OTHER_TYPE, "Software Engineer", location=NY, commitment="校招", team="Engineering"),
        # Facts not given: no place at all, no type at all.
        posting(NO_PLACE, "Software Engineer Intern", location=None, commitment="Intern", team="Engineering"),
        posting(NO_TYPE, "Software Engineer", location=NY, commitment=None, team="Engineering"),
        posting(OTHER_TITLE, "Product Designer Intern", location=NY, commitment="Intern", team="Design", department="Product"),
    ]


IN_SCOPE = {INTERN_TITLE, INTERNSHIP_TYPE, INTERNSHIP_TITLE, NO_PLACE, NO_TYPE}


@pytest.fixture
def ticks():
    return Ticks()


@pytest.fixture
def transport(ticks):
    return ReplayTransport(ticks)


@pytest.fixture
def host(ticks):
    return guard(ticks)


@pytest.fixture
def demo(database, owners, clock):
    return make_demo_library(database, owners, clock)


def b_run(database, clock, demo, transport, host, *, scope=B_SCOPE, provider=None):
    confirmation = confirmation_for(demo, scope=scope, sources=grants((NORTH,)), preferences=(SALES,))

    return begin(database, clock, confirmation, provider, register=verified_register(transport, host, (NORTH,)))


def ref(number: int) -> PostingRef:
    return PostingRef(NORTH.source_id, pid(number))


def numbers(refs) -> set[int]:
    taken = set(refs)

    return {number for number in range(1, 9) if ref(number) in taken}


def listing() -> LeverListing:
    source = lever.LeverSource(NORTH)

    return LeverListing(tuple(source._facts(item) for item in board()), complete=True)


# --- the failure, as the adapter has it --------------------------------------------------------------------


def test_the_board_finds_nothing_for_validation_b_keywords_and_everything_in_scope_without_the_city_and_type():
    found = listing()

    # Problem 2: New York as a filter takes the postings, "New York" among the keywords leaves every one out.
    assert found.select(Query(B_WORDS, **B_FILTERS)) == ()
    # Problem 3: with internships as a filter, "Intern" among the keywords still leaves out the internships whose
    # title and type say "Internship", or whose type alone says so.
    assert numbers(p.ref for p in found.select(Query("Software Engineer Intern", "New York", "实习", None))) == {INTERN_TITLE, NO_PLACE}
    # The words a decision may take from the refusal: every posting the filters take, unknown facts never left out.
    assert numbers(p.ref for p in found.select(Query("Software Engineer", "New York", "实习", None))) == IN_SCOPE


def test_another_city_or_type_is_still_left_out_and_facts_not_given_are_held_by_the_rules(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, board())
    run = b_run(database, clock, demo, transport, host)

    result = search_postings(run, NORTH.source_id, "Software Engineer", **B_FILTERS)

    assert result.outcome is Outcome.DONE and numbers(run.found) == IN_SCOPE
    # San Francisco and a campus hire never come back; a posting with no place or no type is kept, pending.
    assert not numbers(run.found) & {OTHER_CITY, OTHER_TYPE, OTHER_TITLE}
    screens = {number: run.found[ref(number)].screening for number in IN_SCOPE}
    assert screens[INTERN_TITLE].status is Screen.IN and screens[INTERNSHIP_TYPE].status is Screen.IN
    assert screens[NO_PLACE].status is Screen.PENDING and any("工作地点" in why for why in screens[NO_PLACE].reasons)
    assert screens[NO_TYPE].status is Screen.PENDING and any("求职类型" in why for why in screens[NO_TYPE].reasons)


# --- the board's declaration -------------------------------------------------------------------------------


def test_a_lever_board_declares_what_it_matches_keywords_against_and_a_synthetic_source_declares_nothing():
    declared = keyword_scope(lever.LeverSource(NORTH))

    assert declared is lever.KEYWORD_SCOPE and declared.fields == "职位名、团队、部门和类型"
    assert declared.filtered == {Dimension.CITY, Dimension.JOB_TYPE}
    assert {declared.type_of(word) for word in ("Intern", "interns", "Internship", "实习生")} == {"实习"}
    assert declared.type_of("Internal") is None and declared.type_of("Engineer") is None
    # The synthetic sources keep their own ways: a type among the words is how the paged one finds interns (A2).
    assert keyword_scope(paged_register().adapter(PAGED)) is None


@pytest.mark.parametrize(
    "words, named, left",
    [
        (B_WORDS, [("Intern", Dimension.JOB_TYPE), ("New York", Dimension.CITY)], "Software Engineer"),
        ("software engineer internship", [("internship", Dimension.JOB_TYPE)], "software engineer"),
        ("Interns Software Engineer", [("Interns", Dimension.JOB_TYPE)], "Software Engineer"),
        # A city the board knows by name, not in the scope.
        ("Beijing Data Analyst", [("Beijing", Dimension.CITY)], "Data Analyst"),
        # Written without spaces: the whole word is named.
        ("数据分析实习生", [("数据分析实习生", Dimension.JOB_TYPE)], ""),
        # Nothing else: no guess.
        ("Software Engineer", [], "Software Engineer"),
        ("Internal Tools Engineer", [], "Internal Tools Engineer"),
        ("New Yorker Engineer", [], "New Yorker Engineer"),
        ("York Engineer", [], "York Engineer"),
    ],
)
def test_named_words_are_bounded_to_cities_and_types_and_never_a_guess(words, named, left):
    assert named_words(lever.KEYWORD_SCOPE, B_SCOPE, words) == (tuple(named), left)


# --- held before the board is asked --------------------------------------------------------------------------


def test_validation_b_keywords_are_refused_before_anything_is_sent_saying_which_words_and_what_is_left(
    database, demo, clock, transport, host
):
    serve_listing(transport, NORTH, board())
    run = b_run(database, clock, demo, transport, host)

    refused = search_postings(run, NORTH.source_id, B_WORDS, **B_FILTERS)

    assert refused.outcome is Outcome.REFUSED
    # Nothing sent, no search counted, one refusal; the search goes on.
    assert transport.urls() == [] and run.searches == [] and run.found == {} and len(run.refusals) == 1
    assert run.ended is None and run.listings == {}
    # It says which words, what the board matches, where they go instead, and what is left.
    for said in ("城市“New York”", "求职类型“Intern”", "职位名、团队、部门和类型", "不是全文检索", "city、job_type", "“Software Engineer”"):
        assert said in refused.summary
    # And the search as it may be made again: the same filters, the keywords less those words. Never wider.
    assert refused.fix == SearchFix(NORTH.source_id, "Software Engineer", "New York", "实习", "Software Engineer")
    # Its words are conditions only: nothing the outbound rules stop.
    assert review(refused.summary) == ()

    again = search_postings(run, refused.fix.source_id, refused.fix.keywords, city=refused.fix.city,
                            job_type=refused.fix.job_type, direction=refused.fix.direction)

    assert again.outcome is Outcome.DONE and numbers(run.found) == IN_SCOPE
    assert transport.urls() == [list_url(NORTH)] and len(run.searches) == 1


@pytest.mark.parametrize("words", ["Software Engineer Intern", "Software Engineer Internship", "Software Engineer 实习"])
def test_a_type_among_the_keywords_is_refused_whatever_its_spelling(database, demo, clock, transport, host, words):
    run = b_run(database, clock, demo, transport, host)

    refused = search_postings(run, NORTH.source_id, words, **B_FILTERS)

    assert refused.outcome is Outcome.REFUSED and "求职类型“" in refused.summary and "城市“" not in refused.summary
    assert refused.fix is not None and refused.fix.keywords == "Software Engineer" and transport.urls() == []


def test_with_the_filter_open_the_refusal_only_tells_and_offers_no_wider_search(database, demo, clock, transport, host):
    open_city = replace(B_SCOPE, city=DimensionScope(("New York",), Others.INCLUDED, ("New York",)))
    run = b_run(database, clock, demo, transport, host, scope=open_city)

    refused = search_postings(run, NORTH.source_id, "Software Engineer New York", city=None, job_type="实习")

    # Taken out of the keywords with the city left open, the search would cover every city: not offered.
    assert refused.outcome is Outcome.REFUSED and refused.fix is None and transport.urls() == []
    assert "“Software Engineer”" in refused.summary and "要限定城市就在 city 里填" in refused.summary


def test_with_nothing_left_the_refusal_asks_for_a_title_or_skill(database, demo, clock, transport, host):
    run = b_run(database, clock, demo, transport, host)

    refused = search_postings(run, NORTH.source_id, "Intern New York", **B_FILTERS)

    assert refused.outcome is Outcome.REFUSED and refused.fix is None and "去掉以后没有可用的检索词" in refused.summary


def test_the_checks_before_still_come_first_and_a_refusal_counts_as_any_other(database, demo, clock, transport, host):
    run = b_run(database, clock, demo, transport, host)

    # Words the outbound rules stop are refused as before, never repeated, never offered again.
    stopped = search_postings(run, NORTH.source_id, "Software Engineer 200 Park Avenue New York", **B_FILTERS)
    assert stopped.outcome is Outcome.REFUSED and stopped.fix is None and "Park" not in stopped.summary

    # Two refusals end the search: a misuse made twice is bounded like any other.
    second = search_postings(run, NORTH.source_id, B_WORDS, **B_FILTERS)
    assert second.outcome is Outcome.REFUSED and run.ended is EndReason.REFUSED and transport.urls() == []


def test_a_list_kept_is_used_again_with_no_request_and_a_misuse_after_it_sends_nothing(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, board())
    run = b_run(database, clock, demo, transport, host)

    search_postings(run, NORTH.source_id, "Software Engineer", **B_FILTERS)
    reused = search_postings(run, NORTH.source_id, "Engineer", **B_FILTERS)
    refused = search_postings(run, NORTH.source_id, "Engineer New York", **B_FILTERS)

    assert reused.outcome is Outcome.DONE and [record.reused for record in run.searches] == [False, True]
    assert refused.outcome is Outcome.REFUSED and transport.urls() == [list_url(NORTH)]


# --- none found is no sign of none ------------------------------------------------------------------------


def test_a_board_search_finding_nothing_says_so_and_keeps_the_partial_note(database, demo, clock, transport, host):
    full = MAX_PAGES * PAGE_SIZE
    serve_listing(transport, NORTH, [posting(number, "Software Engineer", location="London, UK", commitment="Intern") for number in range(1, full + 1)])
    run = b_run(database, clock, demo, transport, host)

    result = search_postings(run, NORTH.source_id, "Quantum Physicist", **B_FILTERS)

    assert result.outcome is Outcome.DONE and run.found == {}
    assert "0 个只说明没有岗位同时含这些词，不等于这个来源没有符合条件的岗位" in result.summary
    assert f"只读了前 {full} 个" in result.summary and "也不能说没有更多" in result.summary


def test_a_synthetic_search_finding_nothing_says_nothing_new(database, owners, clock):
    library = make_library(database, owners, clock)
    run = begin(database, clock, confirmation_for(library))

    result = search_postings(run, CAREERS, "量子物理", city="北京", job_type="实习", direction="数据分析")

    assert result.outcome is Outcome.DONE and "不等于这个来源没有" not in result.summary


def test_a_type_among_a_synthetic_sources_keywords_is_still_taken(database, owners, clock):
    # The paged synthetic site's type filter does not work: the type among the words is how it finds interns (A2).
    library = make_library(database, owners, clock)
    run = begin(database, clock, confirmation_for(library, scope=SENIOR_SCOPE, sources=PAGED_GRANTS), register=paged_register())

    result = search_postings(run, PAGED, "数据分析 实习", city="北京", job_type="实习", direction="数据分析")

    assert result.outcome is Outcome.DONE and len(run.found) == 3


# --- what a decision is told, and the simulated decider ----------------------------------------------------


def test_every_decision_is_told_the_boards_keyword_scope_and_the_instructions_no_longer_put_a_city_in_keywords(
    database, demo, clock, transport, host
):
    run = b_run(database, clock, demo, transport, host)

    text = observe(run, 1, 8).text

    assert f"- {NORTH.source_id}：{NORTH.name}（官方原始发布）。检索词只在职位名、团队、部门和类型里整词匹配，每个词都要出现，不是全文检索；城市和求职类型只按 city、job_type 筛选，不写进检索词。" in text
    assert "岗位、技能、城市这类条件词" not in DECISION_INSTRUCTIONS
    assert "城市和求职类型不能写进 keywords" in DECISION_INSTRUCTIONS and "城市、求职类型先用 city、job_type 筛选" in DECISION_INSTRUCTIONS


def test_a_synthetic_source_is_named_as_before(database, owners, clock):
    library = make_library(database, owners, clock)
    run = begin(database, clock, confirmation_for(library))

    text = observe(run, 1, 8).text

    assert f"- {CAREERS}：合成招聘官网（官方原始发布）\n" in text and "整词匹配" not in text


class FirstThen:
    """Validation B's model's first action, then the simulated decider: as the run went, until the refusal."""

    kind = "simulated"

    def __init__(self, *first):
        self.first, self.inner, self.seen = list(first), SimulatedDecider(), []

    def decide(self, observation):
        self.seen.append(observation)

        if self.first:
            return json.dumps(self.first.pop(0), ensure_ascii=False)

        return self.inner.decide(observation)


def test_the_replay_corrects_the_misuse_and_goes_on_to_read_check_and_a_simulated_analysis(database, demo, clock, transport, host):
    items = board()
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items)
    provider = RecordingProvider()
    run = b_run(database, clock, demo, transport, host, provider=provider)
    misuse = make_action(Tool.SEARCH, source_id=NORTH.source_id, keywords=B_WORDS, note="检索", **B_FILTERS)
    decider = FirstThen(misuse)

    finished = run_agent(run, decider)
    steps = [(step.tool.value, step.outcome) for step in finished.trace.steps]

    # Refused, then made again as told, then read, checked, analysed by the simulation, and ended with results.
    assert steps[:2] == [("search_postings", "refused"), ("search_postings", "done")]
    assert [tool for tool, _ in steps[2:5]] == ["get_posting", "check_posting", "analyze_posting"]
    assert steps[-1] == ("finish", "done") and finished.outcome.end is EndReason.ENOUGH
    assert finished.outcome.results and numbers(result.ref for result in finished.outcome.results) <= IN_SCOPE
    # The second decision was told why and what to search, and searched just that: the filters unchanged.
    told = decider.seen[1].text
    assert "去掉以后检索词是“Software Engineer”，其余条件不变" in told
    assert dict(finished.trace.steps[1].params)["检索词"] == "Software Engineer"
    assert dict(finished.trace.steps[1].params)["城市"] == "New York" and dict(finished.trace.steps[1].params)["求职类型"] == "实习"
    # One refusal; the list read once; every other request a read or a check of a posting found in scope.
    assert len(run.refusals) == 1 and transport.urls().count(list_url(NORTH)) == 1
    asked = {url for url in transport.urls() if url != list_url(NORTH)}
    assert asked <= {detail_url(NORTH, pid(number)) for number in IN_SCOPE}
    # A simulated analysis ran: nothing sent anywhere.
    assert provider.prompts


def test_the_simulated_decider_never_names_a_city_or_type_in_a_boards_keywords_and_never_adds_the_type(
    database, demo, clock, transport, host
):
    # With no direction listed, its first words are the title looked for: "Software Engineer Intern".
    scope = replace(B_SCOPE, direction=DimensionScope((), Others.INCLUDED, ()))
    serve_listing(transport, NORTH, board())
    run = b_run(database, clock, demo, transport, host, scope=scope)

    action = json.loads(SimulatedDecider().decide(observe(run, 1, 8)))

    assert action["tool"] == "search_postings" and action["keywords"] == "Software Engineer"
    assert (action["city"], action["job_type"]) == ("New York", "实习")


def test_the_simulated_decider_makes_a_refused_search_again_only_as_told(database, demo, clock, transport, host):
    run = b_run(database, clock, demo, transport, host)
    refused = search_postings(run, NORTH.source_id, B_WORDS, **B_FILTERS)

    action = json.loads(SimulatedDecider().decide(observe(run, 2, 8, last=refused)))

    assert {key: action[key] for key in ("tool", "source_id", "keywords", "city", "job_type", "direction")} == {
        "tool": "search_postings", "source_id": NORTH.source_id, "keywords": "Software Engineer",
        "city": "New York", "job_type": "实习", "direction": "Software Engineer",
    }


def test_the_simulated_decider_never_adds_the_type_to_the_words_of_a_source_matching_it_by_its_filter(database, owners, clock):
    # The paged synthetic site of A2, senior roles first: saying nothing of its keywords, it gets the type added to
    # the words (A2's adjustment); were it to say it matches the type by its filter alone, it would not.
    library = make_library(database, owners, clock)
    run = begin(database, clock, confirmation_for(library, scope=SENIOR_SCOPE, sources=PAGED_GRANTS), register=paged_register())
    search_postings(run, PAGED, "数据分析", city="北京", job_type="实习", direction="数据分析")
    last = (PAGED, "数据分析", Combination("北京", "实习", "数据分析", ""))
    observation = observe(run, 2, 8, last_search=last, new=tuple(run.found))

    plain = json.loads(SimulatedDecider().decide(observation))
    declared = json.loads(SimulatedDecider().decide(replace(observation, matching=((PAGED, lever.KEYWORD_SCOPE),))))

    assert plain["tool"] == "search_postings" and plain["keywords"] == "数据分析 实习"
    # Neither the type among the words nor the same search again: the next combination, as with nothing to adjust.
    assert (declared["tool"], declared["keywords"], declared["city"], declared["job_type"]) == ("search_postings", "数据分析", "北京", "校招")
