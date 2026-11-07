"""U3-A2: the search agent's loop over the A1 tools, and the fixed flow it is compared with.

Decisions come from scripts or the simulated decider (fixed local rules, not AI); sources are synthetic and analyses
the app's simulation. These tests show that the loop adjusts to what the tools give back and cannot get past their
rules or its own limits; they say nothing of what a real model would decide or of whether that is worth its cost
(U3-B). All material is synthetic.
"""
import threading

import pytest

from app.discovery import synthetic
from app.discovery.agent import (
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


def analyze(posting_id, note="分析这个岗位"):
    return make_action(Tool.ANALYZE, postings=[ref(posting_id)], note=note)


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
        (Tool.ANALYZE, "done", None),
        (Tool.ANALYZE, "done", None),
        (Tool.ANALYZE, "done", None),
        (Tool.FINISH, "done", None),
    ]
    # The adjustment, and why: the type confirmed, written into the words; the same combination of the scope.
    assert "资深" in trace.steps[1].note and "实习" in trace.steps[1].note
    assert dict(trace.steps[1].params)["城市"] == "北京" and dict(trace.steps[1].params)["求职类型"] == "实习"
    assert (outcome.end, trace.decisions, trace.decider) == (EndReason.ENOUGH, MAX_DECISIONS, "simulated")
    assert [result.ref.posting_id for result in outcome.results] == ["p-bj-intern-1", "p-bj-intern-2", "p-bj-intern-3"]
    assert all(result.category in RECOMMENDED for result in outcome.results)
    assert dict(outcome.unlisted)[Unlisted.OUT_OF_SCOPE] == tuple(ref(f"p-senior-{n}", PAGED) for n in range(1, 6))
    # It said what it left: never "the scope was covered".
    assert outcome.unsearched == ("北京 · 校招 · 数据分析", "上海 · 实习 · 数据分析", "上海 · 校招 · 数据分析")

    # The fixed flow, the same tools and limits on the same case: one search, nothing to analyse, and said as it is.
    base_run = begin(database, clock, confirmation, RecordingProvider(), register=synthetic.paged_register())
    base = run_baseline(base_run)

    assert [(step.tool, step.outcome) for step in base.trace.steps] == [(Tool.SEARCH, "done"), (Tool.FINISH, "done")]
    assert (base.outcome.end, base.outcome.results, base.trace.decisions) == (EndReason.PARTLY_NO_RESULTS, (), 0)
    assert len(searches_made(base_run, PAGED)) == 1


def test_a_source_that_fails_is_not_asked_again_and_the_search_moves_to_another(database, library, clock):
    register = synthetic.register()
    register.adapter(CAREERS).faults[("search", None)] = SourceTimeout("synthetic")
    run = begin(database, clock, confirmation_for(library), register=register)
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
    decider = ScriptedDecider([search(), *(read(posting_id) for posting_id in READS[:6]), finish()])

    finished = run_agent(run, decider)

    assert (len(decider.observations), finished.outcome.end) == (8, EndReason.ENOUGH)


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
        (make_action(Tool.ANALYZE, postings=[ref("c-bj-analyst"), ref("c-bj-sales")]), "analyze_posting 一次只分析一个岗位"),
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
    run = begin(database, clock, confirmation_for(library))
    long_note = "查北京的岗位，\n有问题联系 hr-a2@example.invalid 或 13800001111，" + "补充说明" * 30
    decider = ScriptedDecider([search(note=long_note), finish(note="面试在合成路 18 号")])

    steps = run_agent(run, decider).trace.steps

    assert "［已隐去邮箱］" in steps[0].note and "［已隐去电话号码］" in steps[0].note
    assert "\n" not in steps[0].note and len(steps[0].note) <= 60
    assert steps[1].note == "（行动说明里有疑似地址或标明不公开的内容，没有保存）"
    assert all(review(step.note) == () for step in steps)
