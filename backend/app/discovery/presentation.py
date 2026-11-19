"""How the 找岗位 pages say things, in Chinese (U3-A3).

Every page says where its postings come from (app.discovery.web.SourceSet): synthetic or replayed ones are a preview
and say so (PREVIEW), the real source read under control says it is real. Each decision and analysis comes from fixed
rules on this machine, not from AI, and the pages say so, in every set but validation B's (U3-D2-E3), where a real
model gives them and the pages name it. The requests the simulation handled are counted as such, never as requests
sent out or as costs; a real model's are counted as the job analysis pages count them. A conflict found by an analysis is shown as it is, after the other results, and no page
calls a result a recommendation. Addresses of synthetic postings are shown as text only: they use the reserved .invalid
domain, nothing opens them and nothing applies anywhere. Internal ids stay out of what is read by default.
"""
import json
from app.analysis_contracts import SIMULATION_PROVIDER, RejectionReason
from app.discovery import rules, tools
from app.discovery.contracts import (
    CATEGORY_NAMES,
    DIMENSION_NAMES,
    END_NAMES,
    EVIDENCE_NAMES,
    Category,
    CheckStatus,
    EndReason,
    Dimension,
    Others,
    Outcome,
    Tool,
    Unlisted,
)
from app.discovery.runner import APPROVALS, INTERNAL_ERROR, PROVIDER_UNAVAILABLE, START_REFUSED, STOPPED, TRACING
from app.job_analysis.scope import MAX_OUTBOUND_CHARS, MAX_POSTING_CHARS
from app.library.job_choices import CHECK_WORDS
from app.library.job_recommendations import KEEP_DAYS
from app.library.job_words import source_label
from app.library.presentation import format_short
from app.library.sends import sends_text
from app.live_gate import Blocked
from app.outbound_review import ADDRESS, CONFIDENTIAL
from app.i18n import MessageMap, lazy, list_join, t

# Every page says it is an offline demo (U3-D2-3): nothing in it is real, current or checked against a real source.
PREVIEW = (
    lazy("job.dpres.45")
)
TOOL_LABELS = MessageMap({
    Tool.SEARCH: "job.dpres.tool_labels.1",
    Tool.READ: "job.dpres.tool_labels.2",
    Tool.CHECK: "job.dpres.tool_labels.3",
    Tool.ANALYZE: "job.dpres.tool_labels.4",
    Tool.FINISH: "job.dpres.tool_labels.5",
})
# A failed call is "not done", never "late" by default: why it failed is said beside it (U3-D2-E2).
STEP_OUTCOMES = MessageMap({
    Outcome.DONE.value: "job.dpres.step_outcomes.1",
    Outcome.REFUSED.value: "job.dpres.step_outcomes.2",
    Outcome.FAILED.value: "job.dpres.step_outcomes.3",
    Outcome.BLOCKED.value: "job.dpres.step_outcomes.4",
    Outcome.STOPPED.value: "job.dpres.step_outcomes.5",
    "unusable": "job.dpres.step_outcomes.6",
})
SEARCH_OUTCOMES = MessageMap({
    Outcome.DONE: "job.dpres.search_outcomes.1",
    Outcome.FAILED: "job.dpres.search_outcomes.2",
    Outcome.STOPPED: "job.dpres.search_outcomes.3",
})
UNLISTED_LABELS = MessageMap({
    Unlisted.CLOSED: "job.dpres.unlisted_labels.1",
    Unlisted.OUT_OF_SCOPE: "job.dpres.unlisted_labels.2",
    # What stopped each is said beside it (blocked_lines): never one reason for all (U3-D2-E3). A text that could not
    # go out is listed here too when no analysis of it was tried (U3-D5): either way nothing was sent.
    Unlisted.BLOCKED: "job.dpres.unlisted_labels.3",
    Unlisted.INCOMPLETE: "job.dpres.unlisted_labels.4",
})
# Why an analysis was stopped before anything of it went out (U3-D2-E3), as tools.analyze_posting found it: the
# outbound rules' kinds in the posting, the posting's length, all one analysis would send, or the analysis entry's
# own outbound check.
CONTENT_KINDS = MessageMap({ADDRESS: "job.dpres.content_kinds.1", CONFIDENTIAL: "job.dpres.content_kinds.2"})
BLOCK_REASONS = {
    tools.TOO_LONG: lazy("job.dpres.46", p0=format(MAX_POSTING_CHARS, ',')),
    tools.OVER_LIMIT: lazy("job.dpres.47", p0=format(MAX_OUTBOUND_CHARS, ',')),
    RejectionReason.OUTBOUND_BLOCKED.value: lazy("job.dpres.48"),
}
# Why the gate let a request no further, in words (U3-D2-E3).
GATE_REASONS = MessageMap({
    Blocked.STOPPED: "job.dpres.gate_reasons.1",
    Blocked.PAUSED: "job.dpres.gate_reasons.2",
    Blocked.BUSY: "job.dpres.gate_reasons.3",
    Blocked.PURPOSE: "job.dpres.gate_reasons.4",
    Blocked.REQUESTS: "job.dpres.gate_reasons.5",
    Blocked.COST: "job.dpres.gate_reasons.6",
    Blocked.CLOSED: "job.dpres.gate_reasons.7",
    Blocked.ENDED: "job.dpres.gate_reasons.8",
    Blocked.COUNT: "job.dpres.gate_reasons.9",
    Blocked.TIME: "job.dpres.gate_reasons.10",
    Blocked.INPUT: "job.dpres.gate_reasons.11",
    Blocked.PAYLOAD: "job.dpres.gate_reasons.12",
})
OTHERS_LABELS = MessageMap({
    Others.INCLUDED: "job.dpres.others_labels.1",
    Others.EXCLUDED: "job.dpres.others_labels.2",
    Others.UNDECIDED: "job.dpres.others_labels.3",
})
# Why a search did not run (app.discovery.runner): nothing was sent in any of these.
PROBLEMS = MessageMap({
    TRACING: "job.dpres.problems.1",
    START_REFUSED: "job.dpres.problems.2",
    STOPPED: "job.dpres.problems.3",
    PROVIDER_UNAVAILABLE: "job.dpres.problems.4",
    INTERNAL_ERROR: "job.dpres.problems.5",
})
CANCELLED_BY = MessageMap({
    "user": "job.dpres.cancelled_by.1",
    "logout": "job.dpres.cancelled_by.2",
    "timeout": "job.dpres.cancelled_by.3",
    "shutdown": "job.dpres.cancelled_by.4",
})
# The categories a result may be in, in the order the page shows them; the last is never a recommendation.
SHOWN_CATEGORIES = (Category.STRONG, Category.PARTIAL, Category.CONFLICT)


SIMULATED_REQUESTS = lazy("job.dpres.49")


def requests_text(task, note: str = SIMULATED_REQUESTS, *, live: bool = False) -> str:
    """The requests of a search, as the set searched has them. With the simulation (live False): handled here,
    counted, never called sent out or paid for; one handed to it and not back yet is being handled, not "maybe
    sent", and note says what they were for. With a real model (validation B, U3-D2-E3): handed to the service,
    those not known to have gone out apart, as the job analysis pages count them (sends_text), never taken back."""
    if live:
        return t("job.dpres.50", p0=sends_text(task.handed), p1=note)

    handling = task.out.total
    text = t("job.dpres.51", p0=task.handed.total)

    if handling:
        text += t("job.dpres.52", p0=handling)

    return f"{text}{note}"


def running_status(task, note: str = SIMULATED_REQUESTS, *, live: bool = False) -> str:
    done = len(task.steps)
    progress = t("job.dpres.53", p0=done) if done else t("job.dpres.54")

    return t("job.dpres.55", p0=progress, p1=requests_text(task, note, live=live))


# The names of the services a kept summary may name (as the job analysis and direction pages have them).
PROVIDER_NAMES = {"openai": "OpenAI"}


def kept_requests_text(summary: dict) -> str:
    """The requests line of a kept search summary, as the search went: handled by the local simulation, or handed to
    the real service its review named (the D6 batch), never the one said as the other (D6-1 R3, L2). A summary that
    names no service is of a search of the simulation: every search before validation B, and none could keep one."""
    handled = summary["requests"]["handled"]
    recipient = summary.get("recipient") or {}
    provider = recipient.get("provider")

    if provider in (None, SIMULATION_PROVIDER):
        return t("job.dpres.56", p0=handled)

    return (
        t("job.dpres.57", p0=handled, p1=PROVIDER_NAMES.get(provider, provider), p2=recipient.get('model'))
    )


def tracking_scope(sources) -> str:
    """Which postings 现在核对 and the automatic check track, by their sources, as the running set has them (its keep,
    the sources whose postings may be kept): the synthetic sets' as before; the D6 batch's admitted board by name; none
    where the set keeps nothing (D6-1 R3, L2)."""
    if sources.mode != "real":
        return t("job.dpres.58")

    names = [source_label(grant.name) for grant in sources.grants if grant.source_id in sources.keep]

    if not names:
        return t("job.dpres.59")

    return t("job.dpres.60", p0=list_join(names))


def watch_source_lines(sources, names) -> list[str]:
    """What the page that starts 每日查新 says of its sources, before anything is read (U3-D6 close-out, F1), as the
    running set (app.discovery.web.SourceSet) reads them. The synthetic and replay sets: this machine's data, nothing
    goes out, in the words as before (watch-1). A real source: read for real, at once, under the batch's read approval
    and within the most a start reads, never called synthetic or offline; nothing of the user's goes, and no model is
    asked. The words of the D6 batch (watches.LIVE_NOTICE_VERSION) are the batch's terms as they stand now."""
    named = list_join(names)

    if sources.mode != "real":
        return [
            t("job.dpres.62", p0=named)
        ]

    batch = sources.batch
    lines = [t("job.dpres.63", p0=named)]

    if batch is not None and batch.source is not None:
        # Imported here: the batch is only of the D6 set, and the pages of every other set load nothing of it.
        from app.discovery.batch_live import OPERATION_REQUESTS, OperationKind

        source = batch.source
        reads = OPERATION_REQUESTS[OperationKind.WATCH]
        # As the batch reserves it (Batch._allowance): robots once a launch, before its first read, on top of the list.
        if batch.robots_read:
            need = t("job.dpres.64", p0=reads, p1=reads)
        else:
            need = t("job.dpres.65", p0=reads, p1=reads + 1)

        lines.append(
            t("job.dpres.66", p0=need, p1=source.approval_id, p2=source.run_date, p3=source.max_requests, p4=batch.sent)
        )
    else:
        lines.append(t("job.dpres.67"))

    lines.append(
        t("job.dpres.68")
    )

    return lines


def refresh_limits_text(limits) -> str:
    """The most one 现在核对 or automatic check takes, as it runs (DiscoveryRuntime.refresh_limits): the defaults, or
    the D6 batch's three postings under its own requests and time (D6-1 R3, L2)."""
    seconds = int(limits.seconds)
    duration = t("job.dpres.69", p0=seconds // 60) if seconds % 60 == 0 else t("job.dpres.70", p0=seconds)

    return t("job.dpres.71", p0=limits.postings, p1=limits.calls, p2=duration)


def step_view(step) -> dict:
    """One step of a search as the pages show it: what it was, how it went, what came of it, and the decider's note."""
    tool = TOOL_LABELS.get(step.tool, t("job.dpres.72")) if step.tool is not None else t("job.dpres.72")
    outcome = STEP_OUTCOMES.get(step.outcome, step.outcome)
    failed = tuple(getattr(step, "failed", ()) or ())

    # Why a source's call failed, in fixed words (U3-D2-E2). R10 (U3-D6-3 F3): a step that read or checked several
    # postings says which of them did not come to an answer, on a line of its own; its first line is one posting's,
    # and the step's failure is never put after it. Without such postings (a search is one call) it follows the line.
    why = t("job.dpres.73", p0=step.failure) if getattr(step, "failure", None) and not failed else ""

    return {
        "title": t("job.dpres.74", p0=step.number, p1=tool, p2=outcome),
        "params": " · ".join(f"{name} {value}" for name, value in step.params),
        "summary": f"{step.summary}{why}",
        "failed": failed_text(step.outcome, failed),
        "note": t("job.dpres.75", p0=step.note) if step.note else "",
    }


def failed_text(outcome: str, failed: tuple[str, ...]) -> str:
    """Which postings of a step did not come to an answer (R10, U3-D6-3 F3): all it asked about when the step came to
    none; otherwise these ones, and nothing is said of the others here."""
    if not failed:
        return ""

    lead = t("job.dpres.76") if outcome == Outcome.FAILED.value else t("job.dpres.77", p0=len(failed))

    return t("job.dpres.78", p0=lead, p1=list_join(failed, "clauses"))


def problem_text(problem: str) -> str:
    if problem.startswith("gate_"):
        why = gate_text(problem.removeprefix("gate_"))

        return t("job.dpres.79", p0=why)

    if problem.startswith(APPROVALS):
        return t("job.dpres.80")

    return PROBLEMS.get(problem, PROBLEMS[INTERNAL_ERROR])


def gate_text(code: str) -> str:
    """Why the gate let a request no further, from its code."""
    try:
        return GATE_REASONS[Blocked(code)]
    except (ValueError, KeyError):
        return t("job.dpres.81")


def end_note(outcome) -> str | None:
    """Why a search ended, beside its end label, when there is more to say (U3-D2-E3): at the gate, the gate's or the
    approvals' words; for a source that could send nothing more (validation B), which and what stopped it."""
    if outcome.end is EndReason.SOURCES_UNAVAILABLE:
        return outcome.source_note

    return gate_line(outcome)


def gate_line(outcome) -> str | None:
    """Why a search ended at the gate, when it did (U3-D2-E3): the approvals' own words, else the gate's reason."""
    if outcome.gate_note:
        return outcome.gate_note

    return gate_text(outcome.gate_reason) if outcome.gate_reason else None


def blocked_lines(blocked) -> list[str]:
    """Each reason the analyses kept out as blocked were stopped for, with how many (U3-D2-E3): what the outbound
    rules or limits stopped sent nothing; what the gate stopped may have sent its first request, which still counts."""
    lines = []

    for key, count in blocked:
        codes = [code for code in key.split(",") if code]
        gate = [code for code in codes if code.startswith("gate_")]

        if gate:
            lines.append(t("job.dpres.82", p0=gate_text(gate[0].removeprefix('gate_')), p1=count))
            continue

        content = [CONTENT_KINDS[code] for code in codes if code in CONTENT_KINDS]
        parts = [t("job.dpres.83", p0=list_join(content))] if content else []
        parts += [BLOCK_REASONS.get(code, t("job.dpres.84")) for code in codes if code not in CONTENT_KINDS]
        lines.append(t("job.dpres.85", p0=list_join(parts, "clauses"), p1=count))

    return lines


# What makes a value need quoting as it is shown: what a list is split at, or a quote of any kind (U3-D6, the D5
# finding): a value holding “”、 must never read as two values.
QUOTE_CHARACTERS = frozenset('"“”‘’「」『』\'\\')


def shown_value(value: str) -> str:
    """One value of a scope, whole: bare when it holds neither a separator nor a quote; otherwise written as the
    decision prompt writes values (JSON, U3-D5 F3), its own quotes escaped, so "数据分析/商业分析" or a name holding
    “”、 reads as one value next to the others (U3-C3b; U3-D6)."""
    if rules.SEPARATORS.search(value) or any(char in QUOTE_CHARACTERS for char in value):
        return json.dumps(value, ensure_ascii=False)

    return value


def dimension_text(part, dimension: Dimension) -> str:
    """One dimension of a confirmed scope, as the review and the result say it."""
    ideal = set(part.ideal)
    values = list_join(
        t("job.dpres.86", p0=shown_value(value)) if value in ideal else shown_value(value) for value in part.values
    ) or t("job.dpres.87")
    text = t("job.dpres.88", p0=values, p1=OTHERS_LABELS[part.others])

    if part.excluded:
        text += t("job.dpres.89", p0=list_join(shown_value(value) for value in part.excluded))

    return text


def kept_lines(check) -> list[str]:
    """What the input page says of kept directions chosen that it did not take as chosen (handoff.KeptCheck)."""
    lines = []

    if check.changed:
        names = list_join(t("job.dpres.90", p0=name) for name in check.changed)
        lines.append(t("job.dpres.91", p0=names))

    if check.missing:
        lines.append(t("job.dpres.92", p0=check.missing))

    return lines


def kept_problems(check) -> list[str]:
    """Why a review was not made: a kept direction chosen is no longer as it was chosen."""
    problems = []

    if check.changed:
        names = list_join(t("job.dpres.90", p0=name) for name in check.changed)
        problems.append(t("job.dpres.93", p0=names))

    if check.missing:
        problems.append(t("job.dpres.94", p0=check.missing))

    if problems:
        problems.append(t("job.dpres.95"))

    return problems


def scope_lines(scope) -> list[tuple[str, str]]:
    return [(t("job.dpres.96"), list_join(shown_value(target) for target in scope.targets))] + [
        (DIMENSION_NAMES[dimension], dimension_text(scope.of(dimension), dimension)) for dimension in Dimension
    ]


def stated(day) -> str:
    return rules.stated(day)


def checked_text(value, note: str = lazy("job.dpres.97")) -> str:
    """When the source last answered a check, with what that answer is (note): a synthetic one never passed off as
    a real check, a real one never as a promise of places."""
    if value is None:
        return t("job.dpres.98", p0=t("job.rule.not_provided"))

    return t("job.dpres.99", p0=format_short(value), p1=note)


def found_check_text(item, note: str = lazy("job.dpres.97")) -> str:
    """Where a posting found by a search stands by its checks as the search ended (owner redesign, plan 5.3), in one
    line: still listed only when its latest check answered so; a check that got no answer said as one, never given an
    answer's time; one never checked said so. A public page that answers is never a promise of places."""
    if item.status is CheckStatus.OPEN and item.checked_at is not None:
        return t("job.discover_task.23", p0=checked_text(item.checked_at, note))

    if item.status is CheckStatus.UNKNOWN and item.check_failed_at is not None:
        text = t("job.choice.68", p0=format_short(item.check_failed_at))

        if item.checked_at is not None and item.answered_status is not None:
            text += t("job.choice.69", p0=format_short(item.checked_at), p1=CHECK_WORDS.get(item.answered_status, CHECK_WORDS["unknown"]))

        return text

    if item.checked_at is None:
        return t("disc.tool.20")

    return t("job.choice.70", p0=CHECK_WORDS.get(item.status or "unknown", CHECK_WORDS["unknown"]), p1=format_short(item.checked_at))


def deadlines_text(deadlines) -> str:
    """The deadline at each place a posting was found, side by side: none is picked."""
    if len(deadlines) == 1:
        return stated(deadlines[0][1])

    return list_join((t("job.dpres.99", p0=stated(day), p1=tools.shown_ref(ref)) for ref, day in deadlines), "clauses")


def search_lines(records, names) -> list[str]:
    """The searches as they were made (U3-D5): a list read only in part is said in full on the first search made from
    it, and briefly on each after, never again at length."""
    said, lines = set(), []

    for record in records:
        lines.append(search_line(record, names, again=record.partial and record.source_id in said))

        if record.partial:
            said.add(record.source_id)

    return lines


def search_line(record, names, *, again: bool = False) -> str:
    """One search as it was made: where, with which words and filters, and how many postings came back. again: the
    list it was made from read only in part was said in full on an earlier search (search_lines)."""
    filters = " · ".join(f"{DIMENSION_NAMES[dimension]} {value or t('job.unlimited')}" for dimension, value in record.filters)
    line = t("job.dpres.101", p0=names.get(record.source_id, record.source_id), p1=record.keywords, p2=filters)
    answered = t("job.dpres.102", p0=record.found) if record.reused else t("job.dpres.103", p0=record.found)
    line += answered if record.outcome is Outcome.DONE else SEARCH_OUTCOMES.get(record.outcome, t("job.dpres.104"))

    if record.outcome is Outcome.FAILED and getattr(record, "failure", None):
        line += t("job.dpres.105", p0=record.failure)

    if record.partial and again:
        line += t("job.dpres.106")
    elif record.partial:
        line += t("job.dpres.107", p0=record.listed)

    if record.unverifiable:
        line += t("job.dpres.108", p0=record.unverifiable)

    if record.foreign:
        line += t("job.dpres.109", p0=record.foreign)

    return line


def failed_search_lines(records, names) -> list[str]:
    """Each search that did not come to an answer, short, with why, each once (owner redesign, plan 5.3): a failure is
    still said on the result page, though what was searched is no longer listed there."""
    lines = []

    for record in records:
        if record.outcome is Outcome.DONE:
            continue

        reason = SEARCH_OUTCOMES.get(record.outcome, t("job.dpres.104"))

        if record.outcome is Outcome.FAILED and getattr(record, "failure", None):
            reason += t("job.dpres.105", p0=record.failure)

        line = t("jobx.search_failed", source=names.get(record.source_id, record.source_id), reason=reason)

        if line not in lines:
            lines.append(line)

    return lines


def evidence_label(evidence, note: str = lazy("job.dpres.110")) -> str:
    """A source's evidence level as the pages name it, with what the set's postings are (note): a synthetic sample,
    or, for the real source, nothing added."""
    return f"{EVIDENCE_NAMES[evidence]}{note}"


def category_label(category: Category) -> str:
    return CATEGORY_NAMES[category]


def end_label(end) -> str:
    return END_NAMES[end]


def place_text(item) -> str:
    """Where a result or a lead is (U3-D2-3): its cities as read, or why they are not known, with what the source
    wrote; nothing guessed."""
    if item.cities:
        return list_join(item.cities)

    if rules.unknown(item.places_stated) == rules.NOT_READ:
        return t("job.dpres.111", p0=t("job.rule.not_read"), p1=list_join(item.places_stated))

    return t("job.dpres.112", p0=t("job.rule.not_given"))


def type_text(item) -> str:
    """Its job type as read, or why it is not known, with what the source wrote (U3-D2-3): not given, given and not
    read, only the hours or the contract, or at odds with the title (U3-D2-E3)."""
    why = rules.type_reading(item.job_type, item.type_stated, item.title)

    if why is None:
        return rules.type_word(item.job_type)

    if why == rules.NOT_GIVEN:
        return t("job.dpres.113", p0=t("job.rule.not_given"))

    if why == rules.HOURS_ONLY:
        return t("job.dpres.114", p0=t("job.rule.hours_only"), p1=item.type_stated)

    # The rules' stable phrases, said in the interface language; anything else as it is.
    said = {rules.NOT_READ: "job.rule.not_read", rules.AT_ODDS: "job.rule.at_odds"}.get(why)

    return t("job.dpres.115", p0=t(said) if said else why, p1=item.type_stated)


def partial_text(read) -> str:
    """A source whose list was read only in part (U3-D2-1), as every page of the result tells it: never as all of it."""
    return t("job.dpres.116", p0=source_label(read.source), p1=read.read)


# --- current recommendations held for tracking (U3-D4b-2) ---------------------------------------------------------------

# What the review says when 临时留存并跟踪这次的推荐 is ticked and offered; also why not, when ticked and not offered.
TRACKING_TERMS = (
    lazy("job.dpres.117"),
    lazy("job.dpres.118", p0=KEEP_DAYS),
    lazy("job.dpres.119"),
    lazy("job.dpres.120"),
)
NOT_OFFERED = lazy("job.dpres.121")
# Why a ticked search's recommendations were not held (app.discovery.records.Retention.reason).
RETENTION_REASONS = {
    "stopped": lazy("job.dpres.122"),
    "failed": lazy("job.dpres.123"),
    "ended_early": lazy("job.dpres.124"),
    "not_offered": NOT_OFFERED,
    "superseded": lazy("job.dpres.125"),
    "none_recommended": lazy("job.dpres.126"),
    "write_failed": lazy("job.dpres.127"),
}


def retention_text(task, when, holding: int | None = None) -> str | None:
    """What the result page says came of 临时留存并跟踪这次的推荐: nothing when it was not ticked.

    R10 (U3-D6-3 F3): the counts are what the search's end held (task.retention), and are said as such; holding, read
    from the records on each view, is how many of this page's results are held as current recommendations now. A
    recommendation added to 我的岗位 since, by a decision such as 跳过 or by 加入我的岗位, is held no more and is never
    deleted with them: the page says so instead of "还没加入我的岗位" as if nothing had changed."""
    if not task.confirmation.track_recommendations:
        return None

    retention = task.retention

    if retention is None:
        if task.stage == "done":
            return t("job.dpres.128")

        return RETENTION_REASONS["stopped"]

    if retention.reason is not None:
        return RETENTION_REASONS.get(retention.reason, RETENTION_REASONS["failed"])

    held = retention.kept + retention.renewed
    parts = [t("job.dpres.129", p0=held, p1=when(retention.until))]

    if retention.mine:
        parts.append(t("job.dpres.130", p0=retention.mine))

    if retention.in_bin:
        parts.append(t("job.dpres.131", p0=retention.in_bin))

    if retention.removed:
        parts.append(t("job.dpres.132", p0=retention.removed))

    text = list_join(parts, "clauses") + t("job.dpres.133")

    if holding is None or not held:
        return text

    if holding >= held:
        return text + t("job.dpres.134", p0=held)

    return text + (
        t("job.dpres.135", p0=holding)
    )
