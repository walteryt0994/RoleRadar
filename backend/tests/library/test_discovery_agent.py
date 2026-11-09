"""U3-A2: the search agent's loop over the A1 tools, and the fixed flow it is compared with.

Decisions come from scripts or the simulated decider (fixed local rules, not AI); sources are synthetic and analyses
the app's simulation. These tests show that the loop adjusts to what the tools give back and cannot get past their
rules or its own limits; they say nothing of what a real model would decide or of whether that is worth its cost
(U3-B). All material is synthetic.
"""
import threading

import pytest

from app.ai_provider import AIProviderTimeoutError

from app.discovery import synthetic
from app.discovery.agent import (
    DECISION_INSTRUCTIONS,
    MAX_DECISIONS,
    WITHHELD,
    ScriptedDecider,
    SimulatedDecider,
    make_action,
    run_agent,
)
from app.discovery.baseline import run_baseline
from app.discovery.contracts import RECOMMENDED, Category, EndReason, Limits, Outcome, PostingRef, Tool, Unlisted
from app.discovery.sources import SourceTimeout
from app.discovery.synthetic import CAREERS, LEADS, PAGED
from app.discovery.tools import NOTHING_STANDS
from app.outbound_review import review
from discovery_helpers import (
    PROJECT_ACTIONS,
    PROJECT_TITLE,
    SENIOR_SCOPE,
    SENTINEL,
    WITHIN,
    begin,
    confirmation_for,
    make_library,
    ref,
)
from job_helpers import RecordingProvider


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


def search(source=CAREERS, words="数据分析 实习生", city="北京", note="按范围检索", **filters):
    return make_action(Tool.SEARCH, source_id=source, keywords=words, city=city, **{**WITHIN, **filters}, note=note)


def read(*posting_ids, note="读取详情"):
    return make_action(Tool.READ, postings=[ref(posting_id) for posting_id in posting_ids], note=note)


def check(*posting_ids, note="核对状态"):
    return make_action(Tool.CHECK, postings=[ref(posting_id) for posting_id in posting_ids], note=note)


def analyze(*posting_ids, note="分析这个岗位"):
    return make_action(Tool.ANALYZE, postings=[ref(posting_id) for posting_id in posting_ids], note=note)


def finish(claim=EndReason.ENOUGH, note="结束"):
    return make_action(Tool.FINISH, claim=claim, note=note)


def searches_made(run, source=CAREERS):
    return [query for operation, query in run.register.adapter(source).calls if operation == "search"]


# --- the six kinds of case of the plan (U3 proposal, 6.4) -------------------------------------


def test_a_normal_search_reads_checks_analyses_and_finishes_with_a_grounded_result(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    decider = ScriptedDecider([search(), read("c-bj-analyst", "c-choice"), check("c-bj-analyst", "c-choice"), analyze("c-bj-analyst"), finish()])

    finished = run_agent(run, decider)
    trace, outcome = finished.trace, finished.outcome

    assert (outcome.end, trace.end, trace.decisions, len(decider.observations)) == (EndReason.ENOUGH, EndReason.ENOUGH, 5, 5)
    assert [(step.tool, step.outcome) for step in trace.steps] == [
        (Tool.SEARCH, "done"),
        (Tool.READ, "done"),
        (Tool.CHECK, "done"),
        (Tool.ANALYZE, "done"),
        (Tool.FINISH, "done"),
    ]
    assert [(result.ref, result.category) for result in outcome.results] == [(ref("c-bj-analyst"), Category.STRONG)]
    # One analysis, its two requests: decisions from a script send nothing.
    assert len(provider.prompts) == 2
    assert trace.steps[0].params == (("来源", CAREERS), ("检索词", "数据分析 实习生"), ("城市", "北京"), ("求职类型", "实习"), ("岗位方向", "数据分析"))
    assert trace.steps[0].summary == "合成招聘官网返回 10 个岗位，新的 10 个。"


def test_the_agent_adjusts_to_senior_only_results_and_the_fixed_flow_does_not(database, library, clock):
    """The frozen case of the A2 pre-check (1): the first page holds only senior full-time roles."""
    confirmation = confirmation_for(library, scope=SENIOR_SCOPE, sources=synthetic.PAGED_GRANTS)
    provider = RecordingProvider()
    agent_run = begin(database, clock, confirmation, provider, register=synthetic.paged_register())

    finished = run_agent(agent_run, SimulatedDecider())
    trace, outcome = finished.trace, finished.outcome
    steps = [(step.tool, step.outcome, dict(step.params).get("检索词")) for step in trace.steps]

    assert steps == [
        (Tool.SEARCH, "done", "数据分析"),
        (Tool.SEARCH, "done", "数据分析 实习"),
        (Tool.READ, "done", None),
        (Tool.CHECK, "done", None),
        # The three postings checked open, with one decision, each analysed on its own (U3-Q1) ...
        (Tool.ANALYZE, "done", None),
        # ... which leaves decisions for a combination not searched yet.
        (Tool.SEARCH, "done", "数据分析"),
        (Tool.FINISH, "done", None),
    ]
    # The adjustment, and why: the type confirmed, written into the words; the same combination of the scope.
    assert "资深" in trace.steps[1].note and "实习" in trace.steps[1].note
    assert dict(trace.steps[1].params)["城市"] == "北京" and dict(trace.steps[1].params)["求职类型"] == "实习"
    assert dict(trace.steps[4].params)["岗位"].count("synthetic-paged/") == 3
    assert dict(trace.steps[5].params)["城市"] == "北京" and dict(trace.steps[5].params)["求职类型"] == "校招"
    assert (outcome.end, trace.decisions, trace.decider) == (EndReason.ENOUGH, 7, "simulated")
    assert [result.ref.posting_id for result in outcome.results] == ["p-bj-intern-1", "p-bj-intern-2", "p-bj-intern-3"]
    assert all(result.category in RECOMMENDED for result in outcome.results)
    assert dict(outcome.unlisted)[Unlisted.OUT_OF_SCOPE] == tuple(ref(f"p-senior-{n}", PAGED) for n in range(1, 6))
    # It said what it left: never "the scope was covered".
    assert outcome.unsearched == ("上海 · 实习 · 数据分析", "上海 · 校招 · 数据分析")

    # The fixed flow, the same tools and limits on the same case: one search, nothing to analyse, and said as it is.
    base_run = begin(database, clock, confirmation, RecordingProvider(), register=synthetic.paged_register())
    base = run_baseline(base_run)

    assert [(step.tool, step.outcome) for step in base.trace.steps] == [(Tool.SEARCH, "done"), (Tool.FINISH, "done")]
    assert (base.outcome.end, base.outcome.results, base.trace.decisions) == (EndReason.PARTLY_NO_RESULTS, (), 0)
    assert len(searches_made(base_run, PAGED)) == 1


def test_a_source_that_fails_is_not_asked_again_and_the_search_moves_to_another(database, library, clock):
    register = synthetic.register()
    register.adapter(CAREERS).faults[("search", None)] = SourceTimeout("synthetic")
    # No experience chosen, so the plain "enough" it ends with is still taken: with experiences chosen it would need
    # a result (U3-Q2). The source not asked again is what this case is about.
    run = begin(database, clock, confirmation_for(library, selection=(), preferences=()), register=register)
    decider = ScriptedDecider([search(), search(), search(LEADS, "数据 实习生", city="上海"), finish()])

    trace = run_agent(run, decider).trace

    assert [step.outcome for step in trace.steps] == ["failed", "refused", "done", "done"]
    assert trace.steps[1].reason == "这次检索和之前没有成功的一次完全相同，不会重试"
    assert len(searches_made(run)) == 1
    assert trace.end is EndReason.ENOUGH


def test_when_every_search_failed_the_search_says_its_sources_were_unavailable(database, library, clock):
    register = synthetic.register()

    for source in (CAREERS, LEADS):
        register.adapter(source).faults[("search", None)] = SourceTimeout("synthetic")

    run = begin(database, clock, confirmation_for(library), register=register)
    finished = run_agent(run, ScriptedDecider([search(), search(LEADS, "数据 实习生"), finish(EndReason.NO_RESULTS)]))

    assert finished.outcome.end is EndReason.SOURCES_UNAVAILABLE
    assert finished.outcome.results == finished.outcome.leads == ()


READS = ("c-bj-analyst", "c-choice", "c-two-cities", "c-address", "c-early-start", "c-bj-sales", "c-closed")


def test_the_eighth_decision_is_the_last_and_the_search_ends_at_the_limit(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    # A ninth answer is there, and is never asked for.
    decider = ScriptedDecider([search(), *(read(posting_id) for posting_id in READS), finish()])

    finished = run_agent(run, decider)

    assert len(decider.observations) == finished.trace.decisions == MAX_DECISIONS == 8
    assert (finished.outcome.end, finished.trace.end, finished.trace.defect) == (EndReason.LIMIT, EndReason.LIMIT, None)
    assert [step.number for step in finished.trace.steps] == list(range(1, 9))
    assert decider.observations[-1].decisions_left == 1


def test_a_search_finished_by_its_eighth_decision_keeps_its_own_end_reason(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    analysed = [read("c-bj-analyst"), check("c-bj-analyst"), analyze("c-bj-analyst")]
    decider = ScriptedDecider([search(), *analysed, *(read(posting_id) for posting_id in READS[1:4]), finish()])

    finished = run_agent(run, decider)

    assert (len(decider.observations), finished.outcome.end) == (8, EndReason.ENOUGH)
    assert [result.ref for result in finished.outcome.results] == [ref("c-bj-analyst")]


def test_an_eighth_decision_saying_enough_with_no_result_standing_is_refused_and_the_limit_ends_it(database, library, clock):
    """U3-Q2: with experiences chosen, "enough" with no result standing is refused even as the last decision; the
    search then ends by the loop's own rule, never by the end it claimed."""
    run = begin(database, clock, confirmation_for(library))
    decider = ScriptedDecider([search(), *(read(posting_id) for posting_id in READS[:6]), finish()])

    finished = run_agent(run, decider)

    assert (len(decider.observations), finished.outcome.end, finished.outcome.results) == (8, EndReason.LIMIT, ())
    assert (finished.trace.steps[-1].outcome, finished.trace.steps[-1].reason) == ("refused", NOTHING_STANDS)
    assert finished.outcome.refusals == (NOTHING_STANDS,)


def test_the_tools_limits_hold_whatever_is_decided_and_two_refusals_end_the_search(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library, limits=Limits(searches=1, analyses=1)), provider)
    decider = ScriptedDecider(
        [
            search(),
            search(city="上海"),
            read("c-bj-analyst", "c-bj-sales"),
            check("c-bj-analyst", "c-bj-sales"),
            analyze("c-bj-analyst"),
            analyze("c-bj-sales"),
            finish(),
        ]
    )

    finished = run_agent(run, decider)

    assert finished.outcome.end is EndReason.REFUSED
    assert finished.outcome.refusals == ("检索次数已用完（1 次）", "分析次数已用完（1 个岗位）")
    assert (len(decider.observations), len(provider.prompts), len(searches_made(run))) == (6, 2, 1)


def test_a_search_out_of_its_time_acts_on_no_late_decision(database, library, clock):
    now = [0.0]

    def late(observation):
        now[0] = Limits().seconds

        return read("c-bj-analyst")

    run = begin(database, clock, confirmation_for(library), monotonic=lambda: now[0])
    decider = ScriptedDecider([search(), late, finish()])

    finished = run_agent(run, decider)

    assert (finished.outcome.end, len(decider.observations)) == (EndReason.TIMEOUT, 2)
    assert (finished.trace.steps[-1].outcome, finished.trace.steps[-1].reason) == ("stopped", "timeout")
    assert run.register.adapter(CAREERS).calls == [("search", searches_made(run)[0])]


def test_a_cancellation_during_a_decision_drops_its_answer_and_nothing_more_goes_out(database, library, clock):
    cancel = threading.Event()
    provider = RecordingProvider()

    def cancelling(observation):
        cancel.set()

        return read("c-bj-analyst")

    run = begin(database, clock, confirmation_for(library), provider, cancel=cancel)
    decider = ScriptedDecider([search(), cancelling, finish()])

    finished = run_agent(run, decider)

    assert (finished.outcome.end, len(decider.observations), provider.prompts) == (EndReason.CANCELLED, 2, [])
    assert finished.trace.steps[-1].tool is None and finished.trace.steps[-1].outcome == "stopped"
    assert len(run.register.adapter(CAREERS).calls) == 1


def test_a_cancellation_during_an_analysis_starts_no_other_analysis_or_decision(database, library, clock):
    cancel = threading.Event()

    class Cancelling(RecordingProvider):
        def generate_text(self, prompt, **options):
            answer = super().generate_text(prompt, **options)
            cancel.set()

            return answer

    provider = Cancelling()
    run = begin(database, clock, confirmation_for(library), provider, cancel=cancel)
    decider = ScriptedDecider(
        [search(), read("c-bj-analyst", "c-bj-sales"), check("c-bj-analyst", "c-bj-sales"), analyze("c-bj-analyst"), analyze("c-bj-sales")]
    )

    finished = run_agent(run, decider)

    # The posting's first request went out and counts; the second was never sent; nothing came of either.
    assert (len(decider.observations), len(provider.prompts), run.analyses) == (4, 1, 1)
    assert finished.outcome.end is EndReason.CANCELLED
    assert dict(finished.outcome.unlisted)[Unlisted.INCOMPLETE] == (ref("c-bj-analyst"),)


def test_steps_outside_the_scope_or_the_register_are_refused_before_anything_goes_out(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    unregistered = make_action(
        Tool.READ, postings=[PostingRef("web", "https://jobs.example.invalid/1")], note="读取一个网址"
    )
    decider = ScriptedDecider([search(city="深圳"), unregistered, finish()])

    finished = run_agent(run, decider)

    assert finished.outcome.end is EndReason.REFUSED
    assert finished.outcome.refusals[0] == "“深圳”是不能接受的城市"
    assert finished.outcome.refusals[1].endswith("不是这次检索得到的岗位")
    assert len(decider.observations) == 2
    assert all(adapter.calls == [] for adapter in (run.register.adapter(CAREERS), run.register.adapter(LEADS)))
    # The address was never repeated, in the trace or anywhere a decision could see.
    assert dict(finished.trace.steps[1].params)["岗位"] == "编号不合要求，不显示"
    assert "jobs.example.invalid" not in repr(finished)


def test_experiences_never_reach_a_decision_nor_go_out_as_keywords(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    decider = ScriptedDecider([search(words=PROJECT_ACTIONS), search(words=PROJECT_TITLE), finish()])

    finished = run_agent(run, decider)

    assert finished.outcome.end is EndReason.REFUSED
    assert searches_made(run) == []
    assert [dict(step.params)["检索词"] for step in finished.trace.steps] == [WITHHELD, WITHHELD]

    for observation in decider.observations:
        for words in (SENTINEL, PROJECT_ACTIONS, PROJECT_TITLE, "合成大学", "看板在课程展示中使用"):
            assert words not in observation.text


def test_an_answer_that_is_not_one_strict_action_is_refused_and_kept_out_of_the_trace(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    extra = {**search(), "url": "https://jobs.example.invalid/1"}
    decider = ScriptedDecider(["这不是 JSON，发到 hr-a2@example.invalid", extra, finish()])

    finished = run_agent(run, decider)

    assert finished.outcome.end is EndReason.REFUSED
    assert [(step.tool, step.outcome, step.params, step.note) for step in finished.trace.steps] == [
        (None, "unusable", (), ""),
        (Tool.SEARCH, "unusable", (), ""),
    ]
    assert "example.invalid" not in repr(finished)
    assert searches_made(run) == []


@pytest.mark.parametrize(
    "action, problem",
    [
        # Several postings may be named at once (U3-Q1), but not none, and not more than the structure takes.
        (make_action(Tool.ANALYZE, postings=[]), "analyze_posting 缺少 postings"),
        (
            make_action(Tool.ANALYZE, postings=[ref(f"c-{n}") for n in range(6)]),
            "动作不是规定结构的 JSON：栏目缺少或多余、类型不对，或者太长",
        ),
        (make_action(Tool.SEARCH, source_id=CAREERS), "search_postings 缺少 keywords"),
        ({**make_action(Tool.FINISH, claim=EndReason.ENOUGH), "keywords": "数据分析"}, "finish 不用 keywords，要填 null"),
        ({**make_action(Tool.FINISH), "claim": "limit"}, "动作不是规定结构的 JSON：栏目缺少或多余、类型不对，或者太长"),
        ({**make_action(Tool.READ, postings=[ref("c-bj-analyst")]), "tool": "send_resume"}, "动作不是规定结构的 JSON：栏目缺少或多余、类型不对，或者太长"),
    ],
)
def test_an_action_must_fit_its_tool(database, library, clock, action, problem):
    run = begin(database, clock, confirmation_for(library))

    finished = run_agent(run, ScriptedDecider([action, action]))

    assert finished.outcome.refusals == (problem, problem)


def test_notes_are_kept_on_one_line_masked_and_short_or_not_at_all(database, library, clock):
    # No experience chosen, so the plain "enough" is taken (U3-Q2): the notes are what this case is about.
    run = begin(database, clock, confirmation_for(library, selection=(), preferences=()))
    long_note = "查北京的岗位，\n有问题联系 hr-a2@example.invalid 或 13800001111，" + "补充说明" * 30
    decider = ScriptedDecider([search(note=long_note), finish(note="面试在合成路 18 号")])

    steps = run_agent(run, decider).trace.steps

    assert "［已隐去邮箱］" in steps[0].note and "［已隐去电话号码］" in steps[0].note
    assert "\n" not in steps[0].note and len(steps[0].note) <= 60
    assert steps[1].note == "（行动说明里有疑似地址或标明不公开的内容，没有保存）"
    assert all(review(step.note) == () for step in steps)


# --- U3-Q1 and Q2: several analyses with one decision; what each search took; "enough" held to a result -----


def test_the_history_tells_the_new_postings_each_search_took_and_a_search_that_took_none_as_what_it_was(database, library, clock):
    """U3-Q1: a search that answered is told with the new postings it took, none included; one that failed or was
    refused is told as what it was, never as one that found nothing. Steps later the next decision still sees it."""
    register = synthetic.register()
    register.adapter(LEADS).faults[("search", None)] = SourceTimeout("synthetic")
    run = begin(database, clock, confirmation_for(library), register=register)
    decider = ScriptedDecider(
        [
            search(),
            search(direction=None, note="不限方向再查一次"),
            search(LEADS, "数据 实习生", city="上海"),
            search(city="深圳"),
            read("c-bj-analyst"),
            check("c-bj-analyst"),
            analyze("c-bj-analyst"),
            finish(),
        ]
    )

    finished = run_agent(run, decider)
    first = len(decider.observations[1].new)
    told = decider.observations[4].history[:4]

    assert first > 0 and decider.observations[2].new == ()
    assert told[:3] == (
        f"- 第 1 步 search_postings：完成，新增 {first} 个岗位",
        "- 第 2 步 search_postings：完成，新增 0 个岗位",
        "- 第 3 步 search_postings：来源没有给出结果",
    )
    assert told[3].startswith("- 第 4 步 search_postings：被拒（") and "新增" not in told[3]
    # Four steps on, the last decision is told the same, and the text it is given says it word for word.
    assert decider.observations[-1].history[:4] == told
    assert all(line in decider.observations[-1].text for line in told)
    assert (finished.outcome.end, len(finished.outcome.results)) == (EndReason.ENOUGH, 1)


def test_a_search_with_experiences_is_told_how_many_postings_are_ready_and_how_many_analyses_are_left(database, library, clock):
    """U3-Q1: one line counting the states listed below it and the analyses left; nothing new about any posting."""
    script = [search(), read("c-bj-analyst", "c-choice"), check("c-bj-analyst", "c-choice"), analyze("c-bj-analyst"), finish()]
    decider = ScriptedDecider(script)
    run_agent(begin(database, clock, confirmation_for(library)), decider)
    told = [[line for line in o.text.splitlines() if line.startswith("其中已核对仍在招聘")] for o in decider.observations]

    assert told == [
        [],
        ["其中已核对仍在招聘、还没有分析的：0 个；分析次数还剩 3 个。"],
        ["其中已核对仍在招聘、还没有分析的：0 个；分析次数还剩 3 个。"],
        ["其中已核对仍在招聘、还没有分析的：2 个；分析次数还剩 3 个。"],
        ["其中已核对仍在招聘、还没有分析的：1 个；分析次数还剩 2 个。"],
    ]

    # With no experience chosen nothing is analysed, so there is nothing to count.
    alone = ScriptedDecider([search(), read("c-bj-analyst"), check("c-bj-analyst"), finish()])
    run_agent(begin(database, clock, confirmation_for(library, selection=(), preferences=())), alone)

    assert not any("其中已核对仍在招聘" in o.text for o in alone.observations)


def test_one_decision_may_analyse_several_postings_each_on_its_own_and_the_instructions_say_so(database, library, clock):
    assert "postings 可以放多个，最多到剩余分析次数，每个岗位单独分析、单独计数" in DECISION_INSTRUCTIONS
    assert "选了经历的查找，还没有完整分析结果时不能填 enough" in DECISION_INSTRUCTIONS
    assert "一次只分析一个岗位" not in DECISION_INSTRUCTIONS

    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    postings = ("c-bj-analyst", "c-choice", "c-bj-sales")
    finished = run_agent(run, ScriptedDecider([search(), read(*postings), check(*postings), analyze(*postings), finish()]))
    step = finished.trace.steps[3]

    # One decision, one step; three analyses, each counted and each sending its own two requests.
    assert (step.tool, step.outcome, finished.trace.decisions) == (Tool.ANALYZE, "done", 5)
    assert dict(step.params)["岗位"] == "、".join(f"{CAREERS}/{posting_id}" for posting_id in postings)
    assert (run.analyses, len(provider.prompts)) == (3, 6)
    assert [result.ref.posting_id for result in finished.outcome.results] == ["c-bj-analyst", "c-choice", "c-bj-sales"]


def test_the_simulated_decider_analyses_what_is_checked_open_at_once_up_to_what_is_left(database, library, clock):
    run = begin(database, clock, confirmation_for(library, limits=Limits(analyses=2)))
    finished = run_agent(run, SimulatedDecider())
    analysed = [dict(step.params)["岗位"].split("、") for step in finished.trace.steps if step.tool is Tool.ANALYZE]

    assert [len(postings) for postings in analysed] == [2]
    assert (finished.outcome.end, len(finished.outcome.results)) == (EndReason.ENOUGH, 2)


def test_the_simulated_decider_never_says_enough_with_experiences_chosen_and_no_result(database, library, clock):
    """U3-Q2: every analysis fails here, so no result ever stands. The simulated decider goes on with what is left
    and ends only as the rules take it or at the limit: never "enough", never refused for it."""

    class Failing(RecordingProvider):
        def generate_text(self, prompt, **options):
            self.prompts.append(prompt)

            raise AIProviderTimeoutError("synthetic")

    provider = Failing()
    run = begin(database, clock, confirmation_for(library), provider)
    finished = run_agent(run, SimulatedDecider())
    claims = [dict(step.params).get("结束理由") for step in finished.trace.steps if step.tool is Tool.FINISH]

    assert run.analyses > 0 and provider.prompts and finished.outcome.results == ()
    assert finished.outcome.end in (EndReason.SCOPE_DONE, EndReason.NO_RESULTS, EndReason.PARTLY_NO_RESULTS, EndReason.LIMIT)
    assert "够了" not in claims and NOTHING_STANDS not in finished.outcome.refusals



@pytest.mark.parametrize("since", ["closed", "unanswered"])
def test_a_posting_closed_or_unanswered_since_its_analysis_is_no_longer_shown_as_analysed(database, library, clock, since):
    """U3-Q2: the next decision is told what stands. Analysed, then checked again and closed or without an answer,
    the posting is shown as that, its analysis withdrawn; and "enough" is refused, the second time ending the search."""
    run = begin(database, clock, confirmation_for(library))

    def checked_again(observation):
        careers = run.register.adapter(CAREERS)

        if since == "closed":
            careers.closed.add("c-bj-analyst")
        else:
            careers.faults[("check", "c-bj-analyst")] = SourceTimeout("synthetic")

        return check("c-bj-analyst", note="再核对一次")

    decider = ScriptedDecider([search(), read("c-bj-analyst"), check("c-bj-analyst"), analyze("c-bj-analyst"), checked_again, finish(), finish()])
    finished = run_agent(run, decider)

    def shown(observation):
        return {candidate.ref: candidate.state for candidate in observation.candidates}[ref("c-bj-analyst")]

    assert (shown(decider.observations[4]), shown(decider.observations[5])) == ("analysed", "closed" if since == "closed" else "unknown")
    assert (finished.outcome.end, finished.outcome.results, finished.outcome.refusals) == (EndReason.REFUSED, (), (NOTHING_STANDS, NOTHING_STANDS))
