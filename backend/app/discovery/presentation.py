"""How the 找岗位 pages say things, in Chinese (U3-A3).

Every page is a preview and says so (PREVIEW): the postings are synthetic, not real jobs, and each decision comes from
fixed rules on this machine, not from AI. The requests the simulation handled are counted as such, never as requests
sent out or as costs. A conflict found by an analysis is shown as it is, after the other results, and no page calls a
result a recommendation. Addresses of synthetic postings are shown as text only: they use the reserved .invalid
domain, nothing opens them and nothing applies anywhere. Internal ids stay out of what is read by default.
"""
from app.discovery import rules, tools
from app.discovery.contracts import (
    CATEGORY_LABELS,
    DIMENSION_LABELS,
    END_LABELS,
    EVIDENCE_LABELS,
    Category,
    Dimension,
    Others,
    Outcome,
    Tool,
    Unlisted,
)
from app.discovery.runner import INTERNAL_ERROR, PROVIDER_UNAVAILABLE, START_REFUSED, STOPPED, TRACING
from app.library.presentation import format_short

PREVIEW = "合成预览：岗位是合成样例，不是真实招聘；每一步由本机固定规则模拟决定，不是 AI。"
TOOL_LABELS = {
    Tool.SEARCH: "检索岗位",
    Tool.READ: "读取详情",
    Tool.CHECK: "核对是否仍在招聘",
    Tool.ANALYZE: "分析",
    Tool.FINISH: "结束查找",
}
STEP_OUTCOMES = {
    Outcome.DONE.value: "完成",
    Outcome.REFUSED.value: "没有执行，算一次被拒",
    Outcome.FAILED.value: "来源没有按时回答",
    Outcome.BLOCKED.value: "被拦下，没有发送",
    Outcome.STOPPED.value: "查找已经停止",
    "unusable": "动作不合格，算一次被拒",
}
SEARCH_OUTCOMES = {
    Outcome.DONE: "有回答",
    Outcome.FAILED: "来源没有按时回答",
    Outcome.STOPPED: "查找停止后才回来，没有采用",
}
UNLISTED_LABELS = {
    Unlisted.CLOSED: "核对时已关闭",
    Unlisted.OUT_OF_SCOPE: "超出条件（只在不能接受或不在范围里的城市、类型或方向）",
    Unlisted.BLOCKED: "含疑似地址或标明不公开的内容，被拦下，没有发送",
    Unlisted.INCOMPLETE: "分析没有完成",
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
    PROVIDER_UNAVAILABLE: "本机模拟服务无法使用，这次查找没有开始。",
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


def requests_text(task) -> str:
    """The requests the simulation handled for a search: counted, never called sent out or paid for. One handed to
    it and not back yet is being handled, not "maybe sent": nothing here goes anywhere."""
    handling = task.out.total
    text = f"本机模拟服务处理的请求：{task.handed.total} 次"

    if handling:
        text += f"，其中 {handling} 次正在处理"

    return f"{text}（模拟，没有联网，不产生费用）"


def running_status(task) -> str:
    done = len(task.steps)
    progress = f"已完成 {done} 步" if done else "第一步进行中"

    return f"正在查找：{progress}。{requests_text(task)}。"


def step_view(step) -> dict:
    """One step of a search as the pages show it: what it was, how it went, what came of it, and the decider's note."""
    tool = TOOL_LABELS.get(step.tool, "没有可用的动作") if step.tool is not None else "没有可用的动作"
    outcome = STEP_OUTCOMES.get(step.outcome, step.outcome)

    return {
        "title": f"第 {step.number} 步 · {tool} · {outcome}",
        "params": " · ".join(f"{name} {value}" for name, value in step.params),
        "summary": step.summary,
        "note": f"说明：{step.note}" if step.note else "",
    }


def problem_text(problem: str) -> str:
    if problem.startswith("gate_"):
        return "服务的闸门没有放行这次查找，什么都没有发送。"

    return PROBLEMS.get(problem, PROBLEMS[INTERNAL_ERROR])


def dimension_text(part, dimension: Dimension) -> str:
    """One dimension of a confirmed scope, as the review and the result say it."""
    ideal = set(part.ideal)
    values = "、".join(f"{value}（理想）" if value in ideal else value for value in part.values) or "没有列出"
    text = f"{values}；{OTHERS_LABELS[part.others]}"

    if part.excluded:
        text += f"；不能接受：{'、'.join(part.excluded)}（不会检索）"

    return text


def scope_lines(scope) -> list[tuple[str, str]]:
    return [("要找的岗位", "、".join(scope.targets))] + [
        (DIMENSION_LABELS[dimension], dimension_text(scope.of(dimension), dimension)) for dimension in Dimension
    ]


def stated(day) -> str:
    return rules.stated(day)


def checked_text(value) -> str:
    """When the synthetic source last answered a check, never passed off as a real check."""
    if value is None:
        return f"{rules.NOT_PROVIDED}（没有得到回答的核对不算）"

    return f"{format_short(value)}（合成来源在本机回答，不是真实核验）"


def deadlines_text(deadlines) -> str:
    """The deadline at each place a posting was found, side by side: none is picked."""
    if len(deadlines) == 1:
        return stated(deadlines[0][1])

    return "；".join(f"{stated(day)}（{tools.shown_ref(ref)}）" for ref, day in deadlines)


def search_line(record, names) -> str:
    """One search as it was made: where, with which words and filters, and how many postings came back."""
    filters = " · ".join(f"{DIMENSION_LABELS[dimension]} {value or '不限'}" for dimension, value in record.filters)
    line = f"{names.get(record.source_id, record.source_id)} · 检索词“{record.keywords}” · {filters} · "
    line += f"返回 {record.found} 个" if record.outcome is Outcome.DONE else SEARCH_OUTCOMES.get(record.outcome, "没有结果")

    if record.unverifiable:
        line += f"，其中 {record.unverifiable} 个编号无法核验，没有采用"

    if record.foreign:
        line += f"，另有 {record.foreign} 条属于别的来源（来源异常），没有采用"

    return line


def evidence_label(evidence) -> str:
    return f"{EVIDENCE_LABELS[evidence]}的合成样例"


def category_label(category: Category) -> str:
    return CATEGORY_LABELS[category]


def end_label(end) -> str:
    return END_LABELS[end]
