"""U3-D5, offline: the search agent's quality, as the business asks for it, over synthetic sources only.

- The same search from the same data is never made again nor counted; any other search is made as before.
- Postings are shown to a decision by how they stand against the scope: plainly in it first, pending next, at odds
  with what it asks for last. Nothing is left out, nothing is judged of the user, the scope never changes.
- A text that cannot go out for an analysis is said so when read; it is never cut to fit, sent or counted.
- What to confirm is said once and every risk kept; postings not verifiable are counted as postings.
- The fixed flow ends truthfully whatever its refusal limit (Q-L1); a scope value holding a separator is quoted
  (C3b-L1); every decision of a search begins with the same words, so what stays the same comes first.
- The comparison gives both flows the same inputs and records what each spent its steps on; an adjustment needs what
  a tool gave back, a choice that changed after it, and the action done.

Nothing is sent anywhere: the sources are synthetic, analyses the app's simulation, decisions scripts or local rules.
What these show is that the tools and the records work, never how a model would decide.
"""
import json
import os
import re
from dataclasses import replace

import pytest

from app.discovery import presentation, rules, synthetic
from app.discovery.agent import (
    OPEN,
    READ_FAILED,
    STATE_LABELS,
    UNSENDABLE,
    ScriptedDecider,
    SimulatedDecider,
    decision_prompt,
    make_action,
    observe,
    quoted,
    run_agent,
    DECISION_INSTRUCTIONS,
)
from app.discovery.baseline import _ranked, run_baseline
from app.discovery.compare import adjustments, d5_metrics, run_batch
from app.discovery.compare_tasks import TASKS
from app.discovery.contracts import (
    Dimension,
    DimensionScope,
    EndReason,
    Limits,
    Others,
    Outcome,
    PostingRef,
    Screen,
    SearchRecord,
    Tool,
    Unlisted,
)
from app.discovery.sources import Access, Permission, Register, SourceEntry, SourceTimeout
from app.discovery.tools import (
    REPEATED,
    analyze_postings,
    check_posting,
    finish,
    get_posting,
    search_postings,
)
from app.job_analysis import scope as analysis_scope
from discovery_helpers import SCOPE, WITHIN, begin, confirmation_for, make_library
from job_helpers import RecordingProvider

T5 = next(task for task in TASKS if task.id == "T5")
MIXED = synthetic.MIXED
INTERNS = ("m-intern-1", "m-intern-long", "m-intern-unreadable", "m-intern-2")
FULL_TIME = tuple(f"m-full-{number}" for number in range(1, 13))
WHOLE = dict(city="北京", job_type="实习", direction="数据分析")


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


def mixed(posting_id: str) -> PostingRef:
    return PostingRef(MIXED, posting_id)


def mixed_run(database, clock, library, *, limits=Limits(), provider=None, scope=None, register=None):
    """A search of the D5 case: T5's scope over the site listing full-time roles of unknown type before its interns."""
    confirmation = confirmation_for(library, scope=scope or T5.scope, sources=synthetic.MIXED_GRANTS, limits=limits)

    return begin(database, clock, confirmation, provider, register=register or synthetic.mixed_register())


def search_all(run, words="数据分析"):
    return search_postings(run, MIXED, words, **WHOLE)


def listed_lines(text: str) -> list[str]:
    """The postings a decision is shown, as listed, with the group headers."""
    lines = text.splitlines()
    start = next(number for number, line in enumerate(lines) if line.startswith("已找到的岗位"))

    return [line for line in lines[start + 1:] if line.startswith(("- [", "【"))]


# --- the same search again -------------------------------------------------------------------------------------------


def test_the_same_search_from_the_same_answers_is_not_made_again_nor_counted(database, clock, library):
    run = mixed_run(database, clock, library)
    adapter = run.register.adapter(MIXED)

    first = search_all(run)
    # Its spacing and case are not another search.
    again = search_postings(run, MIXED, "  数据分析 ", city=" 北京", job_type="实习", direction="数据分析")

    assert (first.outcome, first.reason) == (Outcome.DONE, None)
    assert (again.outcome, again.reason) == (Outcome.DONE, REPEATED)
    assert "和第 1 次检索完全相同" in again.summary and "没有再执行" in again.summary and "不计检索次数" in again.summary
    # Never made, never counted, never refused: the source was asked once, and the search found what it found.
    assert [call for call, _ in adapter.calls] == ["search"]
    assert len(run.searches) == 1 and not run.refusals and len(run.found) == 16


def test_other_words_another_combination_or_another_source_is_another_search(database, owners, clock):
    library = make_library(database, owners, clock)
    run = begin(database, clock, confirmation_for(library))
    made = [
        search_postings(run, synthetic.CAREERS, "数据分析", city="北京", **WITHIN),
        search_postings(run, synthetic.LEADS, "数据分析", city="北京", **WITHIN),
        search_postings(run, synthetic.CAREERS, "数据分析 实习生", city="北京", **WITHIN),
        search_postings(run, synthetic.CAREERS, "数据分析", city="上海", **WITHIN),
    ]

    assert [result.reason for result in made] == [None] * 4 and len(run.searches) == 4
    # With every search used up the same search again is still no search and no refusal; any other is refused.
    again = search_postings(run, synthetic.CAREERS, "数据分析", city="北京", **WITHIN)
    other = search_postings(run, synthetic.CAREERS, "数据", city="北京", **WITHIN)

    assert (again.outcome, again.reason) == (Outcome.DONE, REPEATED) and "和第 1 次检索完全相同" in again.summary
    assert other.outcome is Outcome.REFUSED and "检索次数已用完" in other.summary
    assert len(run.searches) == 4 and len(run.refusals) == 1


class WholeList:
    """A list read to its end (sources.Listing): every search of it gives every posting it holds."""

    def __init__(self, postings):
        self.postings = postings
        self.read = len(postings)
        self.complete = True

    def select(self, query):
        return self.postings


class ListSite:
    """A site answering searches only from its whole list (sources.ListingSource), read when asked for."""

    def __init__(self, postings):
        self.postings = postings
        self.calls = []

    def listing(self, timeout):
        self.calls.append("listing")

        return WholeList(self.postings)

    def search(self, query, timeout):
        raise AssertionError("a listing site is searched through its list only")


LIST_ID = "synthetic-list"
LIST_ENTRY = SourceEntry(
    LIST_ID, "合成招聘列表", Access.SYNTHETIC, Permission.VERIFIED, synthetic.EvidenceLevel.OFFICIAL, synthetic.BASIS, synthetic.CHECKED_ON
)


def test_a_list_read_anew_is_other_data_so_the_same_search_on_it_is_made(database, clock, library):
    postings = tuple(
        replace(item.posting, ref=PostingRef(LIST_ID, item.posting.ref.posting_id)) for item in synthetic.MIXED_POSTINGS[-2:]
    )
    site = ListSite(postings)
    confirmation = confirmation_for(library, scope=T5.scope, sources=(LIST_ENTRY.grant,))
    run = begin(database, clock, confirmation, register=Register((LIST_ENTRY,), {LIST_ID: site}))

    first = search_postings(run, LIST_ID, "数据分析", **WHOLE)
    again = search_postings(run, LIST_ID, "数据分析", **WHOLE)

    assert first.reason is None and again.reason == REPEATED and site.calls == ["listing"]
    # Its list no longer this search's (nothing reads it anew today, so the test drops it), another search reads it
    # again: the first search, made on the old list, does not stand for one on the new list, so it is made and counted.
    del run.listings[LIST_ID]
    other = search_postings(run, LIST_ID, "数据分析 实习生", **WHOLE)
    anew = search_postings(run, LIST_ID, "数据分析", **WHOLE)

    assert other.reason is None and site.calls == ["listing", "listing"]
    assert (anew.outcome, anew.reason) == (Outcome.DONE, None) and len(run.searches) == 3
    # And made on the new list once, it is the same search again.
    assert search_postings(run, LIST_ID, "数据分析", **WHOLE).reason == REPEATED and len(run.searches) == 3


def test_a_decision_is_told_the_same_search_was_not_made_and_it_is_never_a_search_that_found_nothing(database, clock, library):
    run = mixed_run(database, clock, library)
    search = make_action(Tool.SEARCH, source_id=MIXED, keywords="数据分析", note="检索", **WHOLE)
    decider = ScriptedDecider([search, search, make_action(Tool.FINISH, claim=EndReason.SCOPE_DONE, note="结束")])

    finished = run_agent(run, decider)
    third = decider.observations[2]

    assert (finished.trace.steps[1].outcome, finished.trace.steps[1].reason) == (Outcome.DONE.value, REPEATED)
    assert third.history[1] == "- 第 2 步 search_postings：和之前的一次检索完全相同，没有执行"
    assert "新增 0 个岗位" not in "\n".join(third.history)
    assert third.left["searches"] == Limits().searches - 1 and third.left["refusals"] == Limits().refusals
    assert "和第 1 次检索完全相同" in third.text
    assert finished.outcome.end is EndReason.SCOPE_DONE and finished.outcome.refusals == ()


def test_zero_results_then_the_same_search_then_a_true_end(database, clock, library):
    run = mixed_run(database, clock, library)

    none = search_all(run, "物流")
    again = search_all(run, "物流")

    assert none.outcome is Outcome.DONE and "返回 0 个岗位，新的 0 个" in none.summary
    assert again.reason == REPEATED and len(run.searches) == 1
    # Its one combination searched, nothing found and nothing left without a conclusion: nothing suitable is true.
    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.DONE
    assert run.outcome().end is EndReason.NO_RESULTS


# --- relevance: an order, never a filter -----------------------------------------------------------------------------


def test_internships_are_shown_before_full_time_roles_of_unknown_type_and_every_posting_found_still_is(database, clock, library):
    run = mixed_run(database, clock, library)
    search_all(run)
    observation = observe(run, 2, 8)
    ids = [candidate.ref.posting_id for candidate in observation.candidates]

    # Found after twelve full-time roles, the internships come first; nothing found is left out.
    assert ids == [*INTERNS, *FULL_TIME]
    assert [candidate.tier for candidate in observation.candidates] == [rules.PLAIN] * 4 + [rules.UNCLEAR] * 12
    # A type said only as "Full-time" stays pending: neither taken as in the scope nor left out.
    assert {run.found[mixed(posting_id)].screening.status for posting_id in FULL_TIME} == {Screen.PENDING}
    assert "已找到的岗位（共 16 个：明确符合检索范围 4 个，有一项待确认 12 个；按这个顺序列出）：" in observation.text
    lines = listed_lines(observation.text)

    # Each internship among the twelve lines a decision is shown (before D5 they were the four not listed).
    assert lines[0] == "【明确符合检索范围】" and lines[5] == "【有一项待确认】"
    assert all(f"[{MIXED}/{posting_id}]" in lines[number + 1] for number, posting_id in enumerate(INTERNS))
    assert sum(line.startswith("- [") for line in lines) == 12 and "另有 4 个没有列出" in observation.text
    assert "求职类型" in lines[6] and "待确认" in lines[6]


def test_a_role_asking_for_years_of_experience_comes_after_pending_ones_and_is_never_left_out(database, clock, library):
    run = mixed_run(database, clock, library)
    # The twelve full-time roles only.
    search_all(run, "数据分析师")
    get_posting(run, [mixed("m-full-1"), mixed("m-full-2")])
    observation = observe(run, 3, 8)
    by_id = {candidate.ref.posting_id: candidate for candidate in observation.candidates}
    # Said why, from its own words (U3-D5 R1): the years of work it asks of the applicant.
    odds = "求职类型只限实习或校招，原文要求应聘者有 3 年以上工作经验"

    assert (by_id["m-full-1"].tier, by_id["m-full-1"].odds) == (rules.AT_ODDS_WITH_SCOPE, odds)
    assert "资深" in by_id["m-full-1"].marks
    # Its words ask nothing of the kind: still pending, as before it was read.
    assert (by_id["m-full-2"].tier, by_id["m-full-2"].odds) == (rules.UNCLEAR, None)
    assert [candidate.ref.posting_id for candidate in observation.candidates][-1] == "m-full-1"
    # An order only: it is still pending in the scope, still shown, still one a decision may choose.
    assert run.found[mixed("m-full-1")].screening.status is Screen.PENDING
    lines = listed_lines(observation.text)
    assert lines[-2] == "【可能不符合目标】" and f"[{MIXED}/m-full-1]" in lines[-1] and f"（{odds}）" in lines[-1]
    assert "有一项待确认 11 个，可能不符合目标 1 个" in observation.text


@pytest.mark.parametrize(
    "job_type",
    [DimensionScope(("实习", "全职"), Others.EXCLUDED, ("实习",)), DimensionScope(("实习",), Others.INCLUDED, ("实习",))],
    ids=["full_time_listed", "others_included"],
)
def test_years_of_experience_are_at_odds_only_with_a_scope_asking_for_internships_or_campus_hires_only(job_type):
    item = next(item for item in synthetic.MIXED_POSTINGS if item.posting.ref.posting_id == "m-full-1")
    scope = replace(T5.scope, job_type=job_type)

    tier, odds = rules.relevance(scope, rules.screen(scope, item.posting), item.posting, item.text)
    assert odds is None and tier in (rules.PLAIN, rules.UNCLEAR)

    # Internships only (T5), or internships and campus hires: at odds, and said why.
    for asked in (T5.scope, replace(T5.scope, job_type=DimensionScope(("实习", "校招"), Others.EXCLUDED, ("实习",)))):
        assert rules.relevance(asked, rules.screen(asked, item.posting), item.posting, item.text)[0] == rules.AT_ODDS_WITH_SCOPE


@pytest.mark.parametrize(
    "words, senior",
    [
        # Three or more years of work asked of the applicant, in English or Chinese; a range by its lower end.
        ("4+ years of experience with SQL", True),
        ("5 years of experience in analytics", True),
        ("5+ years' experience in analytics", True),
        ("3-5 years of experience", True),
        ("3 to 5 years of experience", True),
        ("Experience: at least 3 years", True),
        ("3 years working as an analyst", True),
        ("3 年以上数据分析经验", True),
        ("至少三年数据分析经验", True),
        ("本科及以上学历，3 年以上工作经验，有金融经验者优先", True),
        ("2+ years of experience", False),
        ("2-4 years of experience", False),
        ("2 to 4 years of experience", False),
        ("1-3 years", False),
        ("Python 3.5 years ago", False),
        ("熟练使用 Python", False),
        # Not plain which years they are, or not asked of the applicant (U3-D5 R1): never claimed.
        ("10+ yrs in data", False),
        ("Senior engineers will mentor you", False),
        ("Currently a junior or senior undergraduate student", False),
        ("Our team has 10+ years of experience in fintech", False),
        ("Our company was founded 5 years ago", False),
        ("Completed 3 years of undergraduate study", False),
        ("3 years of undergraduate study or equivalent experience", False),
        ("4+ years of experience preferred", False),
        ("No prior work experience required", False),
        ("由资深工程师指导，不要求工作经验", False),
        ("3 年以上工作经验优先", False),
        ("我们团队拥有 10 年以上行业经验", False),
        ("公司成立 5 年以上，团队经验丰富", False),
        ("大三、大四在校生", False),
    ],
)
def test_only_years_of_work_asked_of_the_applicant_mark_a_posting_senior(words, senior):
    posting = synthetic.MIXED_POSTINGS[-1].posting

    assert ("资深" in rules.marks(posting, f"岗位要求：\n- {words}")) is senior
    assert (rules.relevance(T5.scope, rules.screen(T5.scope, posting), posting, words)[0] == rules.AT_ODDS_WITH_SCOPE) is senior


@pytest.mark.parametrize(
    "title, senior",
    [("资深数据分析师", True), ("高级数据分析师", True), ("Senior Data Analyst", True), ("数据分析实习生", False), ("Seniority Analyst Intern", False)],
)
def test_a_title_naming_a_senior_role_is_at_odds_with_an_internship_only_scope(title, senior):
    item = next(item for item in synthetic.MIXED_POSTINGS if item.posting.ref.posting_id == "m-intern-2")
    posting = replace(item.posting, title=title)
    tier, why = rules.relevance(T5.scope, rules.screen(T5.scope, posting), posting, "工作内容：和资深同事一起整理数据。")

    assert (tier == rules.AT_ODDS_WITH_SCOPE) is senior
    assert (why == "求职类型只限实习或校招，岗位名称写的是资深或高级职位") is senior


def test_the_fixed_flow_goes_by_the_order_the_agent_is_shown(database, clock, library):
    run = mixed_run(database, clock, library)
    search_all(run)

    assert [found.ref.posting_id for found in _ranked(run)] == [*INTERNS, *FULL_TIME]


def test_relevance_orders_postings_even_where_ideal_values_do_not_tell_them_apart(database, clock, library):
    # No ideal type: internships and full-time roles match the same ideal values (北京, 数据分析), so only where they
    # stand against the scope puts the internships first, for the agent and for the fixed flow alike.
    scope = replace(T5.scope, job_type=DimensionScope(("实习",), Others.EXCLUDED, ()))
    run = mixed_run(database, clock, library, scope=scope)
    search_all(run)
    shown = [candidate.ref.posting_id for candidate in observe(run, 2, 8).candidates]

    assert len({rules.ideal_hits(scope, found.posting) for found in run.found.values()}) == 1
    assert shown == [*INTERNS, *FULL_TIME]
    assert [found.ref.posting_id for found in _ranked(run)] == shown


# --- a text that cannot go out ---------------------------------------------------------------------------------------


def test_a_text_too_long_to_send_is_said_when_read_and_is_never_cut_sent_or_counted(database, clock, library):
    provider = RecordingProvider()
    # Two reads and two checks: once both are done, what is left for the local rules is to analyse.
    run = mixed_run(database, clock, library, provider=provider, limits=Limits(reads=2, checks=2))
    search_all(run)
    long_text = analysis_scope.clean_posting(next(item.text for item in synthetic.MIXED_POSTINGS if item.posting.ref.posting_id == "m-intern-long"))
    said = f"原文 {len(long_text)} 字，超过分析外发上限 8000 字：不能分析，也不会截断"

    # The limits stay as they are.
    assert (analysis_scope.MAX_POSTING_CHARS, analysis_scope.MAX_OUTBOUND_CHARS) == (8000, 24000) and len(long_text) > 8000
    read = get_posting(run, [mixed("m-intern-long"), mixed("m-intern-1")])
    check_posting(run, [mixed("m-intern-long"), mixed("m-intern-1")])
    observation = observe(run, 4, 8)
    states = {candidate.ref.posting_id: candidate.state for candidate in observation.candidates}

    assert said in read.summary
    assert (states["m-intern-long"], states["m-intern-1"]) == (UNSENDABLE, OPEN)
    (line,) = [line for line in listed_lines(observation.text) if f"[{MIXED}/m-intern-long]" in line]
    assert said in line and line.endswith(f" —— {STATE_LABELS[UNSENDABLE]}"), line
    # Not counted among those ready to analyse, and the local rules never choose it.
    assert "其中核对时仍公开发布、还没有分析的：1 个" in observation.text
    action = SimulatedDecider()._choose(observation)
    assert action["tool"] == Tool.ANALYZE.value and [posting["posting_id"] for posting in action["postings"]] == ["m-intern-1"]

    # Asked for anyway: stopped before anything is sent, and no analysis is counted.
    stopped = analyze_postings(run, [mixed("m-intern-long")])
    assert stopped.outcome is Outcome.BLOCKED and run.analyses == 0 and provider.prompts == []

    analyze_postings(run, [mixed("m-intern-1")])
    assert run.analyses == 1 and not any("合成传媒" in prompt for prompt in provider.prompts)


def test_a_text_that_could_not_go_out_is_listed_as_stopped_even_when_never_tried(database, clock, library):
    run = mixed_run(database, clock, library)
    search_all(run)
    get_posting(run, [mixed("m-intern-long"), mixed("m-intern-1")])
    check_posting(run, [mixed("m-intern-long"), mixed("m-intern-1")])
    analyze_postings(run, [mixed("m-intern-1")])
    finish(run, EndReason.ENOUGH)
    outcome = run.outcome()

    # The page says the same either way: nothing was sent, and why.
    assert mixed("m-intern-long") in dict(outcome.unlisted)[Unlisted.BLOCKED]
    assert dict(outcome.blocked) == {"too_long": 1}
    assert presentation.UNLISTED_LABELS[Unlisted.BLOCKED] == "分析被拦下或原文不能外发"
    assert presentation.blocked_lines(outcome.blocked) == ["岗位原文超过 8,000 字，什么都没有发送：1 个"]
    assert [result.ref for result in outcome.results] == [mixed("m-intern-1")]


# --- what a result says, once ----------------------------------------------------------------------------------------


def test_a_result_says_what_to_confirm_once_and_keeps_every_risk(database, clock, library):
    run = mixed_run(database, clock, library)
    search_all(run, "数据分析师")
    get_posting(run, [mixed("m-full-2")])
    check_posting(run, [mixed("m-full-2")])
    analyze_postings(run, [mixed("m-full-2")])
    finish(run, EndReason.ENOUGH)
    (result,) = run.outcome().results
    screening = run.found[mixed("m-full-2")].screening

    # The pending type, the reason a decision put it second, is said among what to confirm, and not again.
    assert screening.reasons and set(screening.reasons) <= set(result.first_checks)
    assert not set(result.notes) & set(result.first_checks)
    said = [*result.first_checks, *result.notes]
    assert len(said) == len(set(said))
    assert sum("求职类型" in item for item in said) == 1


def test_postings_not_verifiable_are_counted_as_postings_once_each(database, owners, clock):
    library = make_library(database, owners, clock)
    first = synthetic.POSTINGS[0]
    odd = replace(first, posting=replace(first.posting, ref=PostingRef(synthetic.CAREERS, "hr@example.invalid"), identity=None), listed=True)
    register = Register(
        synthetic.ENTRIES,
        {
            synthetic.CAREERS: synthetic.SyntheticSource(synthetic.CAREERS, (*synthetic.POSTINGS, odd)),
            synthetic.LEADS: synthetic.SyntheticSource(synthetic.LEADS, synthetic.LEAD_POSTINGS),
        },
    )
    run = begin(database, clock, confirmation_for(library), register=register)

    search_postings(run, synthetic.CAREERS, "数据分析", city="北京", **WITHIN)
    search_postings(run, synthetic.CAREERS, "数据分析", city="上海", **WITHIN)
    # While any came back, nothing suitable may not be said.
    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.REFUSED
    run.end(EndReason.LIMIT)
    outcome = run.outcome()

    # Given back by both searches: one posting, two times; its id is never kept.
    assert (outcome.unverifiable, outcome.unverifiable_distinct) == (2, 1)
    assert all("hr@example.invalid" not in str(item) for item in (run.unplain_seen, outcome))


def test_a_list_read_in_part_is_said_in_full_once_and_briefly_after():
    records = [
        SearchRecord("s", "数据分析", ((Dimension.CITY, "北京"),), 3, Outcome.DONE, listed=300, partial=True),
        SearchRecord("s", "数据分析", ((Dimension.CITY, "上海"),), 1, Outcome.DONE, listed=300, partial=True, reused=True),
        SearchRecord("t", "数据分析", ((Dimension.CITY, "北京"),), 2, Outcome.DONE),
    ]
    lines = presentation.search_lines(records, {"s": "合成列表", "t": "合成官网"})

    assert "列表只读了前 300 个，没有读完，这个条件不算查过" in lines[0]
    assert "同样只从这份没读完的列表筛出，不算查过" in lines[1] and "前 300 个" not in lines[1]
    assert "没读完" not in lines[2] and "列表" not in lines[2].split("·", 1)[1]


# --- the fixed flow ends truthfully (Q-L1) ---------------------------------------------------------------------------


@pytest.mark.parametrize("refusals", [2, 3])
def test_the_fixed_flow_ends_at_its_limit_when_no_end_it_may_claim_is_true(database, clock, library, refusals):
    run = mixed_run(database, clock, library, limits=Limits(refusals=refusals))
    adapter = run.register.adapter(MIXED)

    # Its three picks are never read, so nothing is analysed and thirteen postings found have no conclusion: neither
    # "enough" nor "nothing suitable" is true.
    for posting_id in ("m-intern-1", "m-intern-long"):
        adapter.faults[("read", posting_id)] = SourceTimeout("the synthetic site does not answer")

    finished = run_baseline(run)
    claims = [dict(step.params)["结束理由"] for step in finished.trace.steps if step.tool is Tool.FINISH]

    assert finished.outcome.end is EndReason.LIMIT
    # One claim, refused; never "enough" without a result, and no exception whatever the refusal limit.
    assert claims == ["没有合适结果"] and len(finished.outcome.refusals) == 1


# --- what a decision is told -----------------------------------------------------------------------------------------


def test_scope_values_are_written_as_json_so_one_value_never_reads_as_two(database, clock, library):
    # One value holding "、" and quote marks, and two values: told apart, each read back exactly as confirmed.
    single, pair = ("数据、分析」、「商业、分析",), ("数据、分析", "商业、分析")
    assert quoted(single) == '["数据、分析」、「商业、分析"]' and quoted(pair) == '["数据、分析", "商业、分析"]'
    tricky = ('他说"好"', "第一行\n第二行", "a\\b", "北京、上海", "「引号」", "")
    assert all(json.loads(quoted(values)) == list(values) for values in (single, pair, tricky))
    assert "\n" not in quoted(tricky)

    texts = [observe(mixed_run(database, clock, library, scope=replace(T5.scope, targets=targets)), 1, 8).text for targets in (single, pair)]
    lines = [next(line for line in text.splitlines() if line.startswith("- 要找的岗位：")) for text in texts]
    assert lines == ['- 要找的岗位：["数据、分析」、「商业、分析"]', '- 要找的岗位：["数据、分析", "商业、分析"]']

    direction = DimensionScope(("数据分析", "数据/商业分析"), Others.EXCLUDED, ("数据分析",))
    text = observe(mixed_run(database, clock, library, scope=replace(T5.scope, direction=direction)), 1, 8).text
    assert '：["数据分析", "数据/商业分析"]；只看所列的' in text
    # The combinations not searched, with the search's own fields: each value whole.
    line = next(line for line in text.splitlines() if line.startswith("还没有查的组合"))
    assert json.loads(line.split("：", 1)[1]) == [
        {"city": "北京", "job_type": "实习", "direction": "数据分析"},
        {"city": "北京", "job_type": "实习", "direction": "数据/商业分析"},
    ]


def test_a_dimension_left_open_is_null_in_the_combinations_not_searched(database, owners, clock):
    library = make_library(database, owners, clock)
    text = observe(begin(database, clock, confirmation_for(library)), 1, 8).text
    line = next(line for line in text.splitlines() if line.startswith("还没有查的组合"))
    combinations = json.loads(line.split("：", 1)[1])

    # The directions of the helpers' scope include others: searched with direction null.
    assert {"city": "北京", "job_type": "实习", "direction": None} in combinations
    assert "组合里的 null 表示这一项不限" in DECISION_INSTRUCTIONS


class Prompts(SimulatedDecider):
    """The local rules, keeping the prompt a model would be sent for each decision."""

    def __init__(self):
        self.prompts = []

    def decide(self, observation):
        self.prompts.append(decision_prompt(observation))

        return super().decide(observation)


def test_every_decision_of_a_search_begins_with_the_same_words_and_what_changes_comes_after(database, clock, library):
    run = mixed_run(database, clock, library)
    decider = Prompts()
    run_agent(run, decider)
    prompts = decider.prompts
    stable = [prompt[: prompt.index("\n\n第 ")] for prompt in prompts]

    assert len(prompts) >= 5 and len(set(stable)) == 1
    # The instructions, the scope and the sources come first; the step and what is left after them.
    assert stable[0].startswith(DECISION_INSTRUCTIONS) and "检索范围（用户确认的条件，值写成 JSON 数组）：" in stable[0] and "可以调用的来源：" in stable[0]
    assert all(prompt.index("第 ") > prompt.index("可以调用的来源：") for prompt in prompts)

    for earlier, later in zip(prompts, prompts[1:]):
        assert len(os.path.commonprefix([earlier, later])) >= len(stable[0])


# --- read failures and limits ----------------------------------------------------------------------------------------


def test_a_read_that_failed_is_said_so_and_never_tried_again(database, clock, library):
    run = mixed_run(database, clock, library)
    adapter = run.register.adapter(MIXED)
    search_all(run)

    first = get_posting(run, [mixed("m-intern-unreadable")])
    again = get_posting(run, [mixed("m-intern-unreadable")])
    states = {candidate.ref.posting_id: candidate.state for candidate in observe(run, 4, 8).candidates}

    assert first.outcome is Outcome.FAILED and again.outcome is not Outcome.DONE
    assert adapter.calls.count(("read", "m-intern-unreadable")) == 1
    assert states["m-intern-unreadable"] == READ_FAILED


def test_with_one_analysis_left_the_agent_analyses_the_most_relevant_and_ends_within_its_limits(database, clock, library):
    provider = RecordingProvider()
    run = mixed_run(database, clock, library, limits=Limits(analyses=1), provider=provider)

    finished = run_agent(run, SimulatedDecider())

    assert [result.ref for result in finished.outcome.results] == [mixed("m-intern-1")]
    assert run.analyses == 1 and len(provider.prompts) == 2
    assert finished.outcome.end in (EndReason.ENOUGH, EndReason.LIMIT)


def test_out_of_decisions_a_search_ends_at_its_limit_and_claims_nothing(database, clock, library):
    run = mixed_run(database, clock, library)
    search = make_action(Tool.SEARCH, source_id=MIXED, keywords="数据分析", note="检索", **WHOLE)
    read = make_action(Tool.READ, postings=[mixed("m-intern-1")], note="读取")

    finished = run_agent(run, ScriptedDecider([search, read]), limit=2)

    assert (finished.trace.decisions, finished.outcome.end) == (2, EndReason.LIMIT)
    assert finished.outcome.results == () and run.analyses == 0


# --- the comparison --------------------------------------------------------------------------------------------------


def test_the_d5_case_gives_both_flows_the_same_inputs_and_records_what_each_spent_its_steps_on(tmp_path):
    summary = run_batch(tmp_path / "t5", tasks=(T5,))
    (comparison,) = summary["comparisons"]
    base, agent = comparison["baseline"], comparison["agent"]
    records = {
        flow: __import__("json").loads((tmp_path / "t5" / "runs" / f"T5-{flow}.json").read_text(encoding="utf-8"))
        for flow in ("baseline", "agent")
    }

    assert (summary["mode"], summary["failed"], summary["not_run"]) == ("offline", [], [])
    assert records["baseline"]["confirmation_fingerprint"] == records["agent"]["confirmation_fingerprint"]

    for record in records.values():
        # Each a valid run of its own: finished, a true end, nothing a run must never do.
        assert record["status"] == "finished" and record["hard_problems"] == [] and record["ledger"] == []

    # The fixed flow picks before it reads: the text too long is tried and stopped (nothing sent), the one never
    # answered is lost. The local rules read internships first and never try the text they were told cannot go out.
    assert base["analyses_tried_on_unsendable"] == 1 and agent["analyses_tried_on_unsendable"] == 0
    assert agent["analyses_by_relevance"]["plain"] == 2 and agent["analyses_by_relevance"]["at_odds"] == 0
    assert agent["repeated_searches"] == 0 and base["source_calls"] > 0 and agent["source_calls"] > 0
    assert set(d5_metrics(records["agent"])) <= set(agent)


def step(number, tool, outcome="done", reason=None, summary=""):
    return {"number": number, "tool": tool, "outcome": outcome, "reason": reason, "summary": summary, "params": {}, "note": ""}


# A posting a search step found, as an agent's record keeps it (compare._found): within the scope, not senior.
FOUND_IN_SCOPE = {"posting_id": "m-intern-1", "source_id": MIXED, "screen": "in", "senior": False, "official": None, "evidence": "official"}


def act_on(number, tool, *posting_ids, **fields):
    return {"number": number, "action": {"tool": tool, "postings": [{"source_id": MIXED, "posting_id": item} for item in posting_ids], **fields}}


def test_an_adjustment_needs_what_a_tool_gave_back_a_changed_choice_and_the_action_done():
    record = {
        "steps": [
            step(1, "search_postings"),
            step(2, "search_postings", reason=REPEATED),
            step(3, "get_posting"),
            step(4, "analyze_posting", outcome="blocked"),
            step(5, "analyze_posting"),
        ],
        "actions": [
            act_on(1, "search_postings", source_id=MIXED, keywords="数据分析"),
            act_on(2, "search_postings", source_id=MIXED, keywords="数据分析"),
            act_on(3, "get_posting", "m-intern-1", "m-intern-long"),
            act_on(4, "analyze_posting", "m-intern-long"),
            act_on(5, "analyze_posting", "m-intern-1"),
        ],
        "postings": [
            {"posting_id": "m-intern-long", "blocked": True, "unsendable": True, "read": {}},
            {"posting_id": "m-intern-1", "blocked": False, "unsendable": False, "read": {}},
        ],
    }
    record["steps"][0]["new"] = [FOUND_IN_SCOPE]
    kinds = [item["kind"] for item in adjustments(record)]

    assert "重复后改做有进展的动作" in kinds and "被拦后改分析别的候选" in kinds
    # A record from before U3-D5 R1 (no progress): a step stopped as a whole sent nothing, and says so of its posting.
    stop = next(item for item in adjustments(record) if item["kind"] == "被拦后改分析别的候选")
    assert stop["feedback"] == f"第 4 步要分析的 [{MIXED}/m-intern-long] 在发送前被拦下，这一步什么都没有发送"

    # Told when reading that a text cannot go out, and never trying it, is no adjustment one can see: counted apart.
    told = {
        **record,
        "steps": [step(1, "search_postings"), step(2, "get_posting", summary="原文 9000 字，超过分析外发上限 8000 字：不能分析，也不会截断"), step(3, "analyze_posting")],
        "actions": [act_on(1, "search_postings"), act_on(2, "get_posting", "m-intern-long", "m-intern-1"), act_on(3, "analyze_posting", "m-intern-1")],
        "postings": [{"posting_id": "m-intern-long", "blocked": False, "unsendable": True, "read": {}}],
    }
    told["steps"][0]["new"] = [FOUND_IN_SCOPE]

    assert adjustments(told) == []
    # Analysing the same text again after it was stopped is no change of choice.
    again = {**record, "steps": record["steps"][:4] + [step(5, "analyze_posting", outcome="blocked")], "actions": record["actions"][:4] + [act_on(5, "analyze_posting", "m-intern-long")]}
    assert "被拦后改分析别的候选" not in [item["kind"] for item in adjustments(again)]


# --- adjustments read from the steps as they were (U3-D5 R1) --------------------------------------------------------

T4 = next(task for task in TASKS if task.id == "T4")


def agent_record(folder, task, script) -> dict:
    """The agent's record of one offline run of a frozen task, its decisions from a script (the fixed flow runs too)."""
    summary = run_batch(folder, tasks=(task,), decider_for=lambda: ScriptedDecider(script))
    assert summary["failed"] == [] and summary["stopped"] is None, summary

    return json.loads((folder / "runs" / f"{task.id}-agent.json").read_text(encoding="utf-8"))


def act(tool, *posting_ids, source=MIXED, **fields):
    return make_action(tool, postings=[PostingRef(source, item) for item in posting_ids], note="下一步", **fields)


SEARCH_ALL = make_action(Tool.SEARCH, source_id=MIXED, keywords="数据分析", note="检索", **WHOLE)


def test_each_step_records_what_it_did_and_the_analyses_it_saw_stopped(tmp_path):
    record = agent_record(tmp_path / "steps", T5, [
        SEARCH_ALL,
        act(Tool.READ, "m-intern-long", "m-intern-1"),
        act(Tool.CHECK, "m-intern-long", "m-intern-1"),
        act(Tool.ANALYZE, "m-intern-long"),
        act(Tool.ANALYZE, "m-intern-1"),
        make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束"),
    ])
    steps = record["steps"]

    assert [step["progress"] for step in steps[:5]] == [
        {"searches": 1, "reads": 0, "checks": 0, "analyses": 0},
        {"searches": 0, "reads": 2, "checks": 0, "analyses": 0},
        {"searches": 0, "reads": 0, "checks": 2, "analyses": 0},
        # Stopped before anything went out: no analysis counted.
        {"searches": 0, "reads": 0, "checks": 0, "analyses": 0},
        {"searches": 0, "reads": 0, "checks": 0, "analyses": 1},
    ]
    assert [step["blocked"] for step in steps[:5]] == [[], [], [], [{"source_id": MIXED, "posting_id": "m-intern-long", "kinds": ["too_long"]}], []]
    # And (U3-D5 R2) the postings each step analysed, and those whose analysis did not finish.
    assert [step["analysed"] for step in steps[:5]] == [[], [], [], [], [{"source_id": MIXED, "posting_id": "m-intern-1"}]]
    assert all(step["unfinished"] == [] for step in steps)
    # Stopped at step 4, another posting analysed at step 5: an adjustment with its evidence, posting by posting.
    assert [item["kind"] for item in record["adjustments"]] == ["被拦后改分析别的候选"]
    assert record["adjustments"][0]["feedback"] == f"第 4 步要分析的 [{MIXED}/m-intern-long] 在发送前被拦下，这一步什么都没有发送"


def test_an_analysis_before_the_stop_is_no_adjustment_to_it(tmp_path):
    record = agent_record(tmp_path / "order", T5, [
        SEARCH_ALL,
        act(Tool.READ, "m-intern-1", "m-intern-long"),
        act(Tool.CHECK, "m-intern-1", "m-intern-long"),
        act(Tool.ANALYZE, "m-intern-1"),
        act(Tool.ANALYZE, "m-intern-long"),
        make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束"),
    ])

    # The posting was stopped last: whatever came before it was chosen without that feedback.
    assert [item for item in record["postings"] if item["blocked"]] and record["adjustments"] == []


def test_after_the_same_search_only_an_action_that_did_something_is_an_adjustment(tmp_path):
    read_after = agent_record(tmp_path / "read", T5, [SEARCH_ALL, SEARCH_ALL, act(Tool.READ, "m-intern-1"), make_action(Tool.FINISH, claim=EndReason.SCOPE_DONE, note="结束")])
    finish_after = agent_record(tmp_path / "finish", T5, [SEARCH_ALL, SEARCH_ALL, make_action(Tool.FINISH, claim=EndReason.SCOPE_DONE, note="结束")])
    read_again = agent_record(tmp_path / "again", T5, [
        SEARCH_ALL, act(Tool.READ, "m-intern-1"), SEARCH_ALL, act(Tool.READ, "m-intern-1"), make_action(Tool.FINISH, claim=EndReason.SCOPE_DONE, note="结束"),
    ])

    assert [(item["kind"], item["done"]) for item in read_after["adjustments"]] == [("重复后改做有进展的动作", "第 3 步已执行：读取 1 个")]
    # A finish, or a read of what was read before (nothing done), is no change acted on.
    assert finish_after["adjustments"] == [] and read_again["adjustments"] == []
    assert read_again["steps"][3]["progress"]["reads"] == 0


def test_the_same_search_again_is_never_a_search_done_nor_one_adjusted_from(tmp_path):
    first = make_action(Tool.SEARCH, source_id=synthetic.CAREERS, keywords="产品运营", note="检索", city="广州", job_type="实习", direction="产品运营")
    second = make_action(Tool.SEARCH, source_id=synthetic.CAREERS, keywords="产品运营实习生", note="检索", city="广州", job_type="实习", direction="产品运营")
    record = agent_record(tmp_path / "t4", T4, [first, second, first, second, make_action(Tool.FINISH, claim=EndReason.NO_RESULTS, note="结束")])

    assert [step["reason"] for step in record["steps"]][2:4] == [REPEATED, REPEATED]
    # The second search, made after the first found nothing usable, is the one adjustment; neither repeat is.
    assert [(item["kind"], item["done"]) for item in record["adjustments"]] == [("检索调整", "第 2 步检索已执行")]


def test_a_lead_and_its_official_posting_are_known_by_source_and_id():
    lead = {"posting_id": "l-1", "source_id": "leads", "screen": "in", "senior": False, "official": "x", "official_source_id": "careers", "evidence": "lead"}
    record = {
        "steps": [{**step(1, "search_postings"), "new": [lead]}, step(2, "get_posting")],
        "actions": [act_on(1, "search_postings"), {"number": 2, "action": {"tool": "get_posting", "postings": [{"source_id": "other", "posting_id": "x"}]}}],
        "postings": [],
    }

    # Another source's posting with the same id is not the one the lead points to.
    assert adjustments(record) == []
    record["actions"][1]["action"]["postings"] = [{"source_id": "careers", "posting_id": "x"}]
    assert [item["kind"] for item in adjustments(record)] == ["线索到官方岗位"]


def test_a_stop_is_known_only_from_its_own_step_never_from_how_the_run_ended():
    # Two sources with a posting "x" each: careers/x analysed at step 1, careers/y at step 2, leads/x stopped at step 3.
    record = {
        "steps": [step(1, "analyze_posting"), step(2, "analyze_posting"), step(3, "analyze_posting", outcome="blocked")],
        "actions": [
            {"number": 1, "action": {"tool": "analyze_posting", "postings": [{"source_id": "careers", "posting_id": "x"}]}},
            {"number": 2, "action": {"tool": "analyze_posting", "postings": [{"source_id": "careers", "posting_id": "y"}]}},
            {"number": 3, "action": {"tool": "analyze_posting", "postings": [{"source_id": "leads", "posting_id": "x"}]}},
        ],
        "postings": [
            {"source_id": "careers", "posting_id": "x", "blocked": False},
            {"source_id": "careers", "posting_id": "y", "blocked": False},
            {"source_id": "leads", "posting_id": "x", "blocked": True},
        ],
    }

    assert adjustments(record) == []


def test_a_stop_by_the_gate_is_no_outbound_stop_to_adjust_from():
    record = {
        "steps": [
            {**step(1, "analyze_posting", outcome="blocked"), "progress": {"analyses": 0}, "blocked": [{"source_id": MIXED, "posting_id": "m-intern-long", "kinds": ["gate_requests"]}]},
            {**step(2, "analyze_posting"), "progress": {"analyses": 1}, "blocked": []},
        ],
        "actions": [act_on(1, "analyze_posting", "m-intern-long"), act_on(2, "analyze_posting", "m-intern-1")],
        "postings": [],
    }

    assert adjustments(record) == []


def test_analyses_tried_on_texts_that_could_not_go_out_count_only_those():
    read = {"url": "https://mixed.synthetic.invalid/postings/x"}
    postings = [
        {"posting_id": "a", "read": read, "blocked": True, "unsendable": True, "relevance": "plain"},
        # Stopped by the gate, its text one that could go out.
        {"posting_id": "b", "read": read, "blocked": True, "unsendable": False, "relevance": "plain"},
        {"posting_id": "c", "read": read, "blocked": False, "unsendable": True, "relevance": "plain"},
    ]
    metrics = d5_metrics({"postings": postings, "steps": []})

    assert (metrics["analyses_tried_on_unsendable"], metrics["unsendable_read"]) == (1, 2)


# --- what each step did, posting by posting; years asked only as a minimum (U3-D5 R2) --------------------------------


def test_a_step_that_analysed_one_posting_and_stopped_another_never_says_it_sent_nothing(tmp_path):
    record = agent_record(tmp_path / "partly", T5, [
        SEARCH_ALL,
        act(Tool.READ, "m-intern-long", "m-intern-1", "m-intern-2"),
        act(Tool.CHECK, "m-intern-long", "m-intern-1", "m-intern-2"),
        act(Tool.ANALYZE, "m-intern-long", "m-intern-1"),
        act(Tool.ANALYZE, "m-intern-2"),
        make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束"),
    ])
    fourth = record["steps"][3]

    # Step 4 stopped one posting before anything went out and analysed the other: one analysis went out.
    assert fourth["progress"]["analyses"] == 1 and [item["posting_id"] for item in fourth["blocked"]] == ["m-intern-long"]
    assert fourth["analysed"] == [{"source_id": MIXED, "posting_id": "m-intern-1"}] and fourth["unfinished"] == []
    # Each step its own: step 5 the posting it analysed, never again those of step 4.
    assert record["steps"][4]["analysed"] == [{"source_id": MIXED, "posting_id": "m-intern-2"}]
    # Still an adjustment (step 5 analysed a posting not stopped), its evidence as each step did it.
    assert record["adjustments"] == [
        {
            "kind": "被拦后改分析别的候选",
            "feedback": f"第 4 步要分析 2 个岗位：[{MIXED}/m-intern-long] 在发送前被拦下，没有发送；[{MIXED}/m-intern-1] 已分析",
            "choice": f"第 5 步改为分析别的岗位：[{MIXED}/m-intern-2]",
            "done": f"第 5 步已执行：[{MIXED}/m-intern-2] 已分析",
        }
    ]
    assert not any("什么都没有发送" in item["feedback"] for item in record["adjustments"])


def analysis_step(number, *, outcome="done", sent=0, stopped=(), analysed=(), unfinished=(), gate=()):
    """An analysis step as an agent's record keeps it since U3-D5 R2 (compare.StepRecorder)."""
    return {
        **step(number, "analyze_posting", outcome=outcome),
        "progress": {"searches": 0, "reads": 0, "checks": 0, "analyses": sent},
        "blocked": [{"source_id": MIXED, "posting_id": item, "kinds": ["too_long"]} for item in stopped]
        + [{"source_id": MIXED, "posting_id": item, "kinds": ["gate_requests"]} for item in gate],
        "analysed": [{"source_id": MIXED, "posting_id": item} for item in analysed],
        "unfinished": [{"source_id": MIXED, "posting_id": item, "why": "provider"} for item in unfinished],
    }


def stop_feedback(first: dict, named: tuple, *, also=None) -> str:
    """The evidence an analysis of "z" at the next step gets after first, a step that named these postings."""
    steps = [first, *([] if also is None else [also[0]])]
    actions = [act_on(1, "analyze_posting", *named), *([] if also is None else [act_on(2, "analyze_posting", *also[1])])]
    later = len(steps) + 1
    record = {
        "steps": [*steps, analysis_step(later, sent=1, analysed=("z",))],
        "actions": [*actions, act_on(later, "analyze_posting", "z")],
        "postings": [],
    }
    (evidence,) = adjustments(record)
    assert (evidence["choice"], evidence["done"]) == (f"第 {later} 步改为分析别的岗位：[{MIXED}/z]", f"第 {later} 步已执行：[{MIXED}/z] 已分析")

    return evidence["feedback"]


def test_a_step_said_to_send_nothing_is_one_that_stopped_every_posting_it_named():
    every = analysis_step(1, outcome="blocked", stopped=("a", "b"))
    assert stop_feedback(every, ("a", "b")) == f"第 1 步要分析的 2 个岗位都在发送前被拦下（[{MIXED}/a]、[{MIXED}/b]），这一步什么都没有发送"

    # Else each posting it named as it came out, in its order: stopped, analysed, not finished, not let through, not run.
    some = analysis_step(1, sent=2, stopped=("a",), analysed=("b",), unfinished=("c",))
    assert stop_feedback(some, ("b", "a", "c", "d")) == (
        f"第 1 步要分析 4 个岗位：[{MIXED}/a] 在发送前被拦下，没有发送；[{MIXED}/b] 已分析；[{MIXED}/c] 分析没有完成；[{MIXED}/d] 没有执行"
    )
    gate = analysis_step(1, outcome="stopped", sent=1, stopped=("a",), gate=("b",))
    assert stop_feedback(gate, ("a", "b")) == f"第 1 步要分析 2 个岗位：[{MIXED}/a] 在发送前被拦下，没有发送；[{MIXED}/b] 闸门没有放行"
    # Nothing went out, yet not every posting was stopped (the other was never run): said as it was, not as all stopped.
    undone = analysis_step(1, outcome="stopped", stopped=("a",))
    assert stop_feedback(undone, ("a", "d")) == f"第 1 步要分析 2 个岗位：[{MIXED}/a] 在发送前被拦下，没有发送；[{MIXED}/d] 没有执行"

    # Two steps that saw analyses stopped before the one that adjusted (the second analysed nothing to the end, so it is
    # no adjustment itself): each said as it was.
    failed = analysis_step(2, outcome="failed", sent=1, stopped=("c",), unfinished=("d",))
    assert stop_feedback(every, ("a", "b"), also=(failed, ("c", "d"))) == (
        f"第 1 步要分析的 2 个岗位都在发送前被拦下（[{MIXED}/a]、[{MIXED}/b]），这一步什么都没有发送。"
        f"第 2 步要分析 2 个岗位：[{MIXED}/c] 在发送前被拦下，没有发送；[{MIXED}/d] 分析没有完成"
    )


def test_a_step_recorded_before_r2_is_said_by_what_its_record_holds():
    # U3-D5 R1 records hold each step's progress and its stops, not the postings it analysed: the one stopped, by
    # name, and how many analyses the step sent; never that it sent nothing.
    r1 = {key: value for key, value in analysis_step(1, sent=1, stopped=("a",)).items() if key not in ("analysed", "unfinished")}
    later = {key: value for key, value in analysis_step(2, sent=1).items() if key not in ("analysed", "unfinished")}
    record = {"steps": [r1, later], "actions": [act_on(1, "analyze_posting", "a", "b"), act_on(2, "analyze_posting", "c")], "postings": []}

    (evidence,) = adjustments(record)
    assert evidence["feedback"] == f"第 1 步要分析 2 个岗位：[{MIXED}/a] 在发送前被拦下，没有发送；这一步发出了 1 个分析请求"
    assert evidence["done"] == "第 2 步已执行：这一步发出了 1 个分析请求"


R2_YEARS = [
    # A minimum asked of the applicant: claimed, the range by its lower end.
    ("At least 3 years of experience", 3),
    ("No less than 3 years of experience", 3),
    ("Not fewer than 4 years of experience", 4),
    ("More than 3 years of experience", 3),
    ("3 or more years of experience", 3),
    ("3 years of experience or more", 3),
    ("Between 3 and 5 years of experience", 3),
    ("3 - 5 years of experience", 3),
    ("min. 3 years of experience", 3),
    ("不少于 3 年工作经验", 3),
    ("3年以上5年以下工作经验", 3),
    # A ceiling, before the years or after them: never a minimum.
    ("Requirements: less than 3 years of work experience.", None),
    ("Requirements: up to 3 years of professional experience; recent graduates welcome.", None),
    ("Fewer than 5 years of experience", None),
    ("No more than 3 years of professional experience", None),
    ("Under 3 years of experience", None),
    ("A maximum of 4 years of experience", None),
    ("Max. 3 years of experience", None),
    ("At most 3 years of experience", None),
    ("3 years of experience or less", None),
    ("5 years or fewer of work experience", None),
    ("Experience: 3 years or less", None),
    ("不超过 3 年工作经验", None),
    ("3 年以下工作经验", None),
    ("3-5 年以内工作经验", None),
    # The upper end of a range, however it is written: the range goes by its lower end.
    ("1 - 3 years of experience", None),
    ("Between 1 and 3 years of experience", None),
    ("2 or 3 years of experience", None),
    ("1-3 年以上工作经验", None),
    ("两到三年以上工作经验", None),
    # A time window or a guess: not plain, never claimed.
    ("Graduated within the last 3 years with some experience in SQL", None),
    ("About 4 years of experience", None),
    ("approx. 4 years of experience", None),
    ("~ 3 years of experience", None),
    ("约 3 年以上工作经验", None),
    # Said not to be required, however it is said.
    ("This role does not require 3+ years of experience", None),
    ("You don't need 5 years of experience to apply", None),
    ("3+ years of experience is not required", None),
    ("不要求 3 年以上工作经验", None),
    ("无需 5 年以上相关经验", None),
]


@pytest.mark.parametrize("words, years", R2_YEARS)
def test_years_of_work_are_asked_only_as_a_minimum(words, years):
    posting = next(item.posting for item in synthetic.MIXED_POSTINGS if item.posting.ref.posting_id == "m-intern-1")
    tier, why = rules.relevance(T5.scope, rules.screen(T5.scope, posting), posting, words)

    assert rules.required_years(words) == years
    assert (tier == rules.AT_ODDS_WITH_SCOPE) is (years is not None)
    assert why == (None if years is None else f"求职类型只限实习或校招，原文要求应聘者有 {years} 年以上工作经验")
