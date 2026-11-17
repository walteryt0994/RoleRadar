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
    CATEGORY_LABELS,
    DIMENSION_LABELS,
    END_LABELS,
    EVIDENCE_LABELS,
    Category,
    EndReason,
    Dimension,
    Others,
    Outcome,
    Tool,
    Unlisted,
)
from app.discovery.runner import APPROVALS, INTERNAL_ERROR, PROVIDER_UNAVAILABLE, START_REFUSED, STOPPED, TRACING
from app.job_analysis.scope import MAX_OUTBOUND_CHARS, MAX_POSTING_CHARS
from app.library.job_recommendations import KEEP_DAYS
from app.library.presentation import format_short
from app.library.sends import sends_text
from app.live_gate import Blocked
from app.outbound_review import ADDRESS, CONFIDENTIAL

# Every page says it is an offline demo (U3-D2-3): nothing in it is real, current or checked against a real source.
PREVIEW = (
    "离线演示（合成预览）：岗位是合成样例，不是真实招聘；核对时间和结果由本机模拟，不是实时信息；"
    "每一步由本机固定规则模拟决定，不是 AI。"
)
TOOL_LABELS = {
    Tool.SEARCH: "检索岗位",
    Tool.READ: "读取详情",
    Tool.CHECK: "核对是否仍公开发布",
    Tool.ANALYZE: "分析",
    Tool.FINISH: "结束查找",
}
# A failed call is "not done", never "late" by default: why it failed is said beside it (U3-D2-E2).
STEP_OUTCOMES = {
    Outcome.DONE.value: "完成",
    Outcome.REFUSED.value: "没有执行，算一次被拒",
    Outcome.FAILED.value: "没有完成",
    Outcome.BLOCKED.value: "被拦下，没有发送",
    Outcome.STOPPED.value: "查找已经停止",
    "unusable": "动作不合格，算一次被拒",
}
SEARCH_OUTCOMES = {
    Outcome.DONE: "有回答",
    Outcome.FAILED: "没有完成",
    Outcome.STOPPED: "查找停止后才回来，没有采用",
}
UNLISTED_LABELS = {
    Unlisted.CLOSED: "核对时已关闭",
    Unlisted.OUT_OF_SCOPE: "超出条件（只在不能接受或不在范围里的城市、类型或方向）",
    # What stopped each is said beside it (blocked_lines): never one reason for all (U3-D2-E3). A text that could not
    # go out is listed here too when no analysis of it was tried (U3-D5): either way nothing was sent.
    Unlisted.BLOCKED: "分析被拦下或原文不能外发",
    Unlisted.INCOMPLETE: "分析没有完成",
}
# Why an analysis was stopped before anything of it went out (U3-D2-E3), as tools.analyze_posting found it: the
# outbound rules' kinds in the posting, the posting's length, all one analysis would send, or the analysis entry's
# own outbound check.
CONTENT_KINDS = {ADDRESS: "疑似地址", CONFIDENTIAL: "标明不公开的内容"}
BLOCK_REASONS = {
    tools.TOO_LONG: f"岗位原文超过 {MAX_POSTING_CHARS:,} 字",
    tools.OVER_LIMIT: f"这次分析要发送的内容超过 {MAX_OUTBOUND_CHARS:,} 字",
    RejectionReason.OUTBOUND_BLOCKED.value: "分析入口发送前的外发检查拦下了它",
}
# Why the gate let a request no further, in words (U3-D2-E3).
GATE_REASONS = {
    Blocked.STOPPED: "运行目录里有 LIVE_STOP",
    Blocked.PAUSED: "批准已暂停（有费用待核实或超过预留）",
    Blocked.BUSY: "另一个真实任务正在进行",
    Blocked.PURPOSE: "批准的用途不包括找岗位",
    Blocked.REQUESTS: "批准的请求次数用完了",
    Blocked.COST: "剩余预算不够这次请求的预留",
    Blocked.CLOSED: "服务正在停止",
    Blocked.ENDED: "这次查找已经结束",
    Blocked.COUNT: "计数没有完成",
    Blocked.TIME: "请求的时间用完了",
    Blocked.INPUT: "请求的输入超过了批准的范围",
    Blocked.PAYLOAD: "请求超过了批准允许的大小",
}
OTHERS_LABELS = {
    Others.INCLUDED: "所列以外的也查（仍然排除不能接受的）",
    Others.EXCLUDED: "只看所列的",
    Others.UNDECIDED: "所列以外的是否也查：还没有选择",
}
# Why a search did not run (app.discovery.runner): nothing was sent in any of these.
PROBLEMS = {
    TRACING: "环境里设置了可能打开 LangSmith 追踪的变量，这次查找没有开始，什么都没有发送。请去掉这些变量后重新启动本地网页。",
    START_REFUSED: "开始前的检查没有通过，所以没有开始，什么都没有发送：",
    STOPPED: "查找在开始前就停止了，什么都没有发送。",
    PROVIDER_UNAVAILABLE: "服务无法使用，这次查找没有开始，什么都没有发送。",
    INTERNAL_ERROR: "查找没有完成（程序出错），没有采用任何结果。",
}
CANCELLED_BY = {
    "user": "你取消了这次查找",
    "logout": "退出登录时取消了这次查找",
    "timeout": "这次查找超过了运行时限",
    "shutdown": "服务停止时取消了这次查找",
}
# The categories a result may be in, in the order the page shows them; the last is never a recommendation.
SHOWN_CATEGORIES = (Category.STRONG, Category.PARTIAL, Category.CONFLICT)


SIMULATED_REQUESTS = "（模拟，没有联网，不产生费用）"


def requests_text(task, note: str = SIMULATED_REQUESTS, *, live: bool = False) -> str:
    """The requests of a search, as the set searched has them. With the simulation (live False): handled here,
    counted, never called sent out or paid for; one handed to it and not back yet is being handled, not "maybe
    sent", and note says what they were for. With a real model (validation B, U3-D2-E3): handed to the service,
    those not known to have gone out apart, as the job analysis pages count them (sends_text), never taken back."""
    if live:
        return f"已交给模型服务的请求：{sends_text(task.handed)}{note}"

    handling = task.out.total
    text = f"本机模拟服务处理的请求：{task.handed.total} 次"

    if handling:
        text += f"，其中 {handling} 次正在处理"

    return f"{text}{note}"


def running_status(task, note: str = SIMULATED_REQUESTS, *, live: bool = False) -> str:
    done = len(task.steps)
    progress = f"已完成 {done} 步" if done else "第一步进行中"

    return f"正在查找：{progress}。{requests_text(task, note, live=live)}。"


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
        return f"本机模拟服务处理的请求 {handled} 次"

    return (
        f"交给真实模型服务的请求 {handled} 次（{PROVIDER_NAMES.get(provider, provider)} 的 {recipient.get('model')}，"
        "决策和分析都交给它；费用按这一批批准的账本核算，不是服务商账单）"
    )


def tracking_scope(sources) -> str:
    """Which postings 现在核对 and the automatic check track, by their sources, as the running set has them (its keep,
    the sources whose postings may be kept): the synthetic sets' as before; the D6 batch's admitted board by name; none
    where the set keeps nothing (D6-1 R3, L2)."""
    if sources.mode != "real":
        return "只限合成来源"

    names = [grant.name for grant in sources.grants if grant.source_id in sources.keep]

    if not names:
        return "这次运行没有可以保存岗位的来源，所以不跟踪"

    return f"只限这次运行准入、可以保存岗位的来源：{'、'.join(names)}"


def watch_source_lines(sources, names) -> list[str]:
    """What the page that starts 每日查新 says of its sources, before anything is read (U3-D6 close-out, F1), as the
    running set (app.discovery.web.SourceSet) reads them. The synthetic and replay sets: this machine's data, nothing
    goes out, in the words as before (watch-1). A real source: read for real, at once, under the batch's read approval
    and within the most a start reads, never called synthetic or offline; nothing of the user's goes, and no model is
    asked. The words of the D6 batch (watches.LIVE_NOTICE_VERSION) are the batch's terms as they stand now."""
    named = "、".join(names)

    if sources.mode != "real":
        return [
            f"来源：{named}（本机合成数据，不联网）。发给来源的只有上面的检索词和筛选值；按整张列表读取的来源（Lever 形状）只读列表，"
            "检索词不发出。发送前按找岗位时同样的规则再检查一遍，没过的不发。"
        ]

    batch = sources.batch
    lines = [f"来源：{named}。这是真实来源，不是合成数据：开启要联网读取它的公开列表，只读，不登录、不投递。"]

    if batch is not None and batch.source is not None:
        # Imported here: the batch is only of the D6 set, and the pages of every other set load nothing of it.
        from app.discovery.batch_live import OPERATION_REQUESTS, OperationKind

        source = batch.source
        reads = OPERATION_REQUESTS[OperationKind.WATCH]
        # As the batch reserves it (Batch._allowance): robots once a launch, before its first read, on top of the list.
        if batch.robots_read:
            need = f"这一项最多读取列表 {reads} 次（这次启动已经读过 robots，不再读），共预留 {reads} 次"
        else:
            need = f"这一项最多读取列表 {reads} 次；这次启动还没有读过 robots，开始前另读 robots 1 次，共预留 {reads + 1} 次"

        lines.append(
            f"确认开启以后马上读取列表，建立基准：{need}，都计入这一批的读取批准 {source.approval_id}"
            f"（只在 {source.run_date} 当天有效，这一批最多 {source.max_requests} 次，已用 {batch.sent} 次）；"
            "剩下的不够这一项，这次就不读取，什么都不发送。"
        )
    else:
        lines.append("确认开启以后马上读取一次列表，建立基准；按这次运行的读取批准受控读取。")

    lines.append(
        "只读整张列表，检索词和筛选值不发出，读到以后只在本机按这份摘要的范围初筛；不发送你的经历和偏好，不调用模型（不用 AI）。"
        "发送前按找岗位时同样的规则再检查一遍，没过的不发。"
    )

    return lines


def refresh_limits_text(limits) -> str:
    """The most one 现在核对 or automatic check takes, as it runs (DiscoveryRuntime.refresh_limits): the defaults, or
    the D6 batch's three postings under its own requests and time (D6-1 R3, L2)."""
    seconds = int(limits.seconds)
    duration = f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"

    return f"每轮最多 {limits.postings} 个岗位、{limits.calls} 次来源请求、{duration}"


def step_view(step) -> dict:
    """One step of a search as the pages show it: what it was, how it went, what came of it, and the decider's note."""
    tool = TOOL_LABELS.get(step.tool, "没有可用的动作") if step.tool is not None else "没有可用的动作"
    outcome = STEP_OUTCOMES.get(step.outcome, step.outcome)
    failed = tuple(getattr(step, "failed", ()) or ())

    # Why a source's call failed, in fixed words (U3-D2-E2). R10 (U3-D6-3 F3): a step that read or checked several
    # postings says which of them did not come to an answer, on a line of its own; its first line is one posting's,
    # and the step's failure is never put after it. Without such postings (a search is one call) it follows the line.
    why = f" 原因：{step.failure}" if getattr(step, "failure", None) and not failed else ""

    return {
        "title": f"第 {step.number} 步 · {tool} · {outcome}",
        "params": " · ".join(f"{name} {value}" for name, value in step.params),
        "summary": f"{step.summary}{why}",
        "failed": failed_text(step.outcome, failed),
        "note": f"说明：{step.note}" if step.note else "",
    }


def failed_text(outcome: str, failed: tuple[str, ...]) -> str:
    """Which postings of a step did not come to an answer (R10, U3-D6-3 F3): all it asked about when the step came to
    none; otherwise these ones, and nothing is said of the others here."""
    if not failed:
        return ""

    lead = "这一步问到的岗位都没有结果" if outcome == Outcome.FAILED.value else f"这一步其中 {len(failed)} 个岗位没有结果"

    return f"{lead}：{'；'.join(failed)}"


def problem_text(problem: str) -> str:
    if problem.startswith("gate_"):
        why = gate_text(problem.removeprefix("gate_"))

        return f"服务的闸门没有放行这次查找（{why}），什么都没有发送。"

    if problem.startswith(APPROVALS):
        return "这次查找的批准不能使用（已经用过、不是这次查找的、过了运行日，或者运行目录里有 LIVE_STOP），所以没有开始，什么都没有发送。"

    return PROBLEMS.get(problem, PROBLEMS[INTERNAL_ERROR])


def gate_text(code: str) -> str:
    """Why the gate let a request no further, from its code."""
    try:
        return GATE_REASONS[Blocked(code)]
    except (ValueError, KeyError):
        return "闸门没有放行"


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
            lines.append(f"闸门没有放行这次分析的请求（{gate_text(gate[0].removeprefix('gate_'))}），之前发出的请求照常计数：{count} 个")
            continue

        content = [CONTENT_KINDS[code] for code in codes if code in CONTENT_KINDS]
        parts = [f"岗位原文里有{'、'.join(content)}"] if content else []
        parts += [BLOCK_REASONS.get(code, "外发检查拦下了它") for code in codes if code not in CONTENT_KINDS]
        lines.append(f"{'；'.join(parts)}，什么都没有发送：{count} 个")

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
    values = "、".join(
        f"{shown_value(value)}（理想）" if value in ideal else shown_value(value) for value in part.values
    ) or "没有列出"
    text = f"{values}；{OTHERS_LABELS[part.others]}"

    if part.excluded:
        text += f"；不能接受：{'、'.join(shown_value(value) for value in part.excluded)}（不会检索）"

    return text


def kept_lines(check) -> list[str]:
    """What the input page says of kept directions chosen that it did not take as chosen (handoff.KeptCheck)."""
    lines = []

    if check.changed:
        names = "、".join(f"“{name}”" for name in check.changed)
        lines.append(f"{names}在你选择之后有变化，或者现在不能用于找岗，没有带入。可以在探索记录里查看它们现在的情况。")

    if check.missing:
        lines.append(f"另有 {check.missing} 个方向已经撤销或找不到，没有带入。")

    return lines


def kept_problems(check) -> list[str]:
    """Why a review was not made: a kept direction chosen is no longer as it was chosen."""
    problems = []

    if check.changed:
        names = "、".join(f"“{name}”" for name in check.changed)
        problems.append(f"保留的方向{names}在你选择之后有变化，或者现在不能用于找岗。")

    if check.missing:
        problems.append(f"有 {check.missing} 个保留的方向已经撤销或找不到。")

    if problems:
        problems.append("下面已按现在的情况列出保留的方向，请重新选择后再核对。")

    return problems


def scope_lines(scope) -> list[tuple[str, str]]:
    return [("要找的岗位", "、".join(shown_value(target) for target in scope.targets))] + [
        (DIMENSION_LABELS[dimension], dimension_text(scope.of(dimension), dimension)) for dimension in Dimension
    ]


def stated(day) -> str:
    return rules.stated(day)


def checked_text(value, note: str = "合成来源在本机回答，不是真实核验") -> str:
    """When the source last answered a check, with what that answer is (note): a synthetic one never passed off as
    a real check, a real one never as a promise of places."""
    if value is None:
        return f"{rules.NOT_PROVIDED}（没有得到回答的核对不算）"

    return f"{format_short(value)}（{note}）"


def deadlines_text(deadlines) -> str:
    """The deadline at each place a posting was found, side by side: none is picked."""
    if len(deadlines) == 1:
        return stated(deadlines[0][1])

    return "；".join(f"{stated(day)}（{tools.shown_ref(ref)}）" for ref, day in deadlines)


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
    filters = " · ".join(f"{DIMENSION_LABELS[dimension]} {value or '不限'}" for dimension, value in record.filters)
    line = f"{names.get(record.source_id, record.source_id)} · 检索词“{record.keywords}” · {filters} · "
    answered = f"从这次已经读过的列表筛出 {record.found} 个（没有再发请求）" if record.reused else f"返回 {record.found} 个"
    line += answered if record.outcome is Outcome.DONE else SEARCH_OUTCOMES.get(record.outcome, "没有结果")

    if record.outcome is Outcome.FAILED and getattr(record, "failure", None):
        line += f"（{record.failure}）"

    if record.partial and again:
        line += "；同样只从这份没读完的列表筛出，不算查过"
    elif record.partial:
        line += f"；列表只读了前 {record.listed} 个，没有读完，这个条件不算查过"

    if record.unverifiable:
        line += f"，其中 {record.unverifiable} 个编号无法核验，没有采用"

    if record.foreign:
        line += f"，另有 {record.foreign} 条属于别的来源（来源异常），没有采用"

    return line


def evidence_label(evidence, note: str = "的合成样例") -> str:
    """A source's evidence level as the pages name it, with what the set's postings are (note): a synthetic sample,
    or, for the real source, nothing added."""
    return f"{EVIDENCE_LABELS[evidence]}{note}"


def category_label(category: Category) -> str:
    return CATEGORY_LABELS[category]


def end_label(end) -> str:
    return END_LABELS[end]


def place_text(item) -> str:
    """Where a result or a lead is (U3-D2-3): its cities as read, or why they are not known, with what the source
    wrote; nothing guessed."""
    if item.cities:
        return "、".join(item.cities)

    if rules.unknown(item.places_stated) == rules.NOT_READ:
        return f"地点{rules.NOT_READ}：{'、'.join(item.places_stated)}"

    return f"地点{rules.NOT_GIVEN}"


def type_text(item) -> str:
    """Its job type as read, or why it is not known, with what the source wrote (U3-D2-3): not given, given and not
    read, only the hours or the contract, or at odds with the title (U3-D2-E3)."""
    why = rules.type_reading(item.job_type, item.type_stated, item.title)

    if why is None:
        return item.job_type

    if why == rules.NOT_GIVEN:
        return f"类型{rules.NOT_GIVEN}"

    if why == rules.HOURS_ONLY:
        return f"类型{rules.HOURS_ONLY}：{item.type_stated}（没有说明是不是实习）"

    return f"类型{why}：{item.type_stated}"


def partial_text(read) -> str:
    """A source whose list was read only in part (U3-D2-1), as every page of the result tells it: never as all of it."""
    return f"{read.source}的职位列表只读了前 {read.read} 个，没有读完：在它上面查的条件不算查过，这里的结果不是全量。"


# --- current recommendations held for tracking (U3-D4b-2) ---------------------------------------------------------------

# What the review says when 临时留存并跟踪这次的推荐 is ticked and offered; also why not, when ticked and not offered.
TRACKING_TERMS = (
    "查找正常结束时，系统临时留存这次的推荐（最多 3 条，只含“依据较充分”和“有基础但有差距或待确认”的完整分析结果）："
    "它们的公开信息、核对结果和之后的核对记录；不存岗位全文、分析、经历和偏好。",
    f"留 {KEEP_DAYS} 天，到期自动删除，不进回收站；被下一次勾选、更晚开始并且有推荐的查找替换时提前删除。查找出错、被拦下或被停止时不留存，原来的推荐不变。",
    "临时留存的推荐不是“我的岗位”：只有你保存它、做决定或指出不准时，才加入我的岗位（保存它的分析不会）。"
    "在岗位记录页的“当前推荐（临时留存跟踪）”一组查看，点“不再跟踪”马上删除。",
    "本机应用开着、并且打开了每 24 小时检查时才会自动检查；否则可以在岗位记录页点“现在核对”。",
)
NOT_OFFERED = "这次不能临时留存推荐：只有选了经历（会做完整分析）、并且用到的来源都允许保存和跟踪时才可以。这次的结果照常只临时显示。"
# Why a ticked search's recommendations were not held (app.discovery.records.Retention.reason).
RETENTION_REASONS = {
    "stopped": "这次查找被停止了，没有临时留存推荐；原来的推荐不变。",
    "failed": "这次查找没有正常完成，没有临时留存推荐；原来的推荐不变。",
    "ended_early": "这次查找因为出错、被拦下或来源不可用而结束，没有临时留存推荐；原来的推荐不变。需要的话，可以逐条加入我的岗位。",
    "not_offered": NOT_OFFERED,
    "superseded": "已经有更晚开始的勾选查找，这次的推荐没有临时留存；当前推荐以那次为准。",
    "none_recommended": "这次没有可以临时留存的推荐；原来的推荐不变。",
    "write_failed": "这次的推荐没有临时留存成功，什么都没有写；需要的话，可以逐条加入我的岗位。",
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
            return "正在临时留存这次的推荐，刷新页面后可以看到结果。"

        return RETENTION_REASONS["stopped"]

    if retention.reason is not None:
        return RETENTION_REASONS.get(retention.reason, RETENTION_REASONS["failed"])

    held = retention.kept + retention.renewed
    parts = [f"查找结束时，这次的推荐临时留存跟踪了 {held} 条（当时都还没加入我的岗位），留到 {when(retention.until)} 自动删除"]

    if retention.mine:
        parts.append(f"当时另有 {retention.mine} 条已在我的岗位里，没有改动")

    if retention.in_bin:
        parts.append(f"当时 {retention.in_bin} 条在回收站里，没有留存")

    if retention.removed:
        parts.append(f"原来的 {retention.removed} 条推荐不在这次里，当时已删除")

    text = "；".join(parts) + "。"

    if holding is None or not held:
        return text

    if holding >= held:
        return text + f"现在这 {held} 条仍在临时留存跟踪中。"

    return text + (
        f"现在其中 {holding} 条仍在临时留存跟踪中；其余的以下面各条和岗位记录为准："
        "加入了我的岗位的（包括做了跳过等决定的）不会随推荐删除，不再跟踪或已到期的已经删除。"
    )
