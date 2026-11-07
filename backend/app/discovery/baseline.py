"""The fixed flow a search agent is compared with (U3 proposal, 4.3 and 6.4): the same tools and limits, no decisions.

It searches the confirmed scope once: at the first source it may ask that gives official postings (else the first
it may ask), for the scope's first combination, any dimension open to other values left open, with the first
direction listed (else the first title) as its words; the agent's simulated decider starts the same way. Of what it
found within the scope it reads and checks the first ones, ideal values first, as many as it may analyse; analyses
those checked open; and finishes: "enough" when it analysed any, "nothing suitable" otherwise, which the rules turn
into what it really was (A1 finish), and "enough" when they refuse that. It adjusts nothing: no second search, no
other words, combination or source, and no reading of the postings leads point to. It makes no decision and so
sends none; its steps go through the very tools and checks the agent's do (agent.execute).
"""
import json

from app.discovery import rules
from app.discovery.agent import (
    Finished,
    Trace,
    TraceStep,
    combinations,
    execute,
    first_words,
    make_action,
    params,
    parse_action,
    safe_note,
    summary_line,
)
from app.discovery.contracts import CheckStatus, Dimension, EndReason, EvidenceLevel, Others, Outcome, Screen, Tool, ToolResult
from app.discovery.tools import Found, SearchRun


def run_baseline(run: SearchRun) -> Finished:
    """Run the fixed flow until the search ends, then say what it found."""
    steps: list[TraceStep] = []

    def do(data: dict) -> ToolResult:
        action, _, problem = parse_action(json.dumps(data, ensure_ascii=False))

        if action is None:
            raise RuntimeError(f"the fixed flow built an action that is not one: {problem}")

        result = execute(run, action)
        steps.append(
            TraceStep(len(steps) + 1, action.tool, params(action), result.outcome.value, summary_line(result.summary), safe_note(action.note), result.reason)
        )

        return result

    do(_first_search(run))

    picked = [found.ref for found in _ranked(run)][: min(run.limits.analyses, run.limits.per_action)]

    if picked and not run.stopping():
        do(make_action(Tool.READ, postings=picked, note="固定流程：读取排在前面的岗位"))

    if picked and not run.stopping():
        do(make_action(Tool.CHECK, postings=picked, note="固定流程：核对这些岗位"))

    for ref in picked:
        if run.stopping():
            break

        found = run.found[ref]

        if run.personalised and found.detail is not None and found.status is CheckStatus.OPEN:
            do(make_action(Tool.ANALYZE, postings=[ref], note="固定流程：分析核对过仍在招聘的岗位"))

    if not run.stopping():
        analysed = any(found.analysis is not None for found in run.found.values())
        claim = EndReason.ENOUGH if analysed else EndReason.NO_RESULTS
        result = do(make_action(Tool.FINISH, claim=claim, note="固定流程：结束"))

        if result.outcome is Outcome.REFUSED and not run.stopping():
            do(make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="固定流程：结束理由没有被接受，以够了结束"))

    if not run.stopping():
        raise RuntimeError("the fixed flow stopped before the search ended")

    return Finished(run.outcome(), Trace("baseline", None, 0, tuple(steps), run.ended))


def _first_search(run: SearchRun) -> dict:
    usable = [grant for grant in run.confirmation.sources if run.source_refusal(grant.source_id) is None]
    usable.sort(key=lambda grant: grant.evidence is not EvidenceLevel.OFFICIAL)
    source_id = (usable or list(run.confirmation.sources))[0].source_id
    first = combinations(run.scope)[0]
    values = (first.city, first.job_type, first.direction)
    filters = {
        dimension.value: None if run.scope.of(dimension).others is Others.INCLUDED else value
        for dimension, value in zip(Dimension, values)
    }

    return make_action(
        Tool.SEARCH, source_id=source_id, keywords=first_words(run.scope), note="固定流程：按确认的范围检索一次", **filters
    )


def _ranked(run: SearchRun) -> list[Found]:
    """The official postings found within the scope, one of each posting found twice: ideal values first, then as found."""
    kept = [run.kept(group) for group in run.groups()]
    usable = [
        found for found in kept if found.screening.status is not Screen.OUT and run.evidence(found) is EvidenceLevel.OFFICIAL
    ]

    return sorted(usable, key=lambda found: (-rules.ideal_hits(run.scope, found.posting), found.order))
