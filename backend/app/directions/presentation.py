"""What the direction pages say, and the shape their results are shown in (U3-C3a).

The words of the review page are a processing notice: NOTICE, of NOTICE_VERSION (app.directions.explore), for the
local simulation, or LIVE_NOTICE, of LIVE_NOTICE_VERSION, for a real service: what goes out, to whom, for what, what
stays with the service, what it costs, and how long the result stays. A task binds the version it was reviewed
under, and its pages say what that version says, whatever the app was started with since. Changing the words means
a new version, and with it a new fingerprint for every task: a confirmation of the old words never carries over.
LIVE_NOTICE describes one service's published handling of API data, at its official address (LIVE_NOTICE_COVERS),
and the pages take a real service from no other (app.directions.web.check_live_terms).

Which service a page names comes from where its words belong: the entry page from the app's service now, a task's
pages from the recipient the task was confirmed for, a saved exploration from its own snapshot. A real service's
pages never say it was offline or free, and what they say of cost is the approval's (all its tasks), never one
exploration's.

A result is shown direction by direction, every direction with its parts in the same order so they can be compared:
its general description, the user's own words it rests on, how it relates to what the user wrote they want, what is
still to learn, what cannot be told yet, how it differs from the others, and what to try next. No score, no rank, no
"best fit". A direction the user excluded before is shown last and said to be excluded, never hidden; nothing else
changes the order the result gave.
"""
from app.analysis_contracts import SIMULATION_PROVIDER
from app.directions.explore import (
    FIELD_GROUPS,
    PREFERENCE_GROUPS,
    STANCE_LABELS,
    ExplorationStatus,
    Problem,
    conflict_label,
)
from app.directions.contracts import ExplorationResult, Relation, name_key
from app.experience_analysis import PREFERENCE_TOPICS
from app.library.direction_export import (
    BASIS_LABELS,
    BASIS_STATE_LABELS,
    DECISION_STATE_LABELS,
    RELATION_LABELS,
)
from app.library.models import DecisionState
from app.library.presentation import field_label
from app.library.references import BasisState

STEP = "探索方向"
# The processing notice of NOTICE_VERSION "direction-1": the local simulation only.
NOTICE = (
    ("接收方", "本机模拟服务：RoleRadar 内置的固定规则，不是 AI。在这台电脑上运行，不联网，不保存请求。"),
    ("用途", "只用于这一次方向探索：给出 3 到 5 个可以对比的方向，以及它们和你的经历、偏好的关系。"),
    ("步骤", "确认后发出一个请求，随时可以取消。"),
    ("结果", "只临时保留 30 分钟，服务重启后也会清除；不会自动保存，要留下请在结果页保存。"),
)
# The processing notice of a real service (U3, the direction pages on the real service; Codex §28.2.1). Its first
# line, who receives the request, is the confirmed recipient's (notice_rows): the rest is the same for any model of
# the service it describes, and for that service only.
LIVE_NOTICE_VERSION = "direction-live-2"
LIVE_NOTICE_COVERS = {"openai": "https://api.openai.com/v1"}
LIVE_NOTICE = (
    ("用途", "确认后，下面勾选的经历栏目和求职偏好栏目会发给这个外部 AI 服务，只用于生成这一次方向探索：一次探索只发一个请求，不重试。"),
    (
        "留存",
        "按 OpenAI 的 API 数据政策，API 数据默认不用于训练模型。请求设为不保存回答，但这不等于不留存："
        "滥用监控日志默认最多保留 30 天，法律要求或为防止伤害时可能更长；服务商还可能缓存由提示内容算出的加密中间数据，最长 24 小时。"
        "这些数据不是原文，但这不代表匿名化或不能恢复，仍然是留存。",
    ),
    (
        "费用",
        "每次发送前按固定的保守额度预留费用，返回后按服务商报告的用量核算。这是应用内的预算检查，不是服务商账单的硬上限："
        "进行中的请求可能超出预留。",
    ),
    ("取消", "已经发出的请求无法撤回：取消只能停止还没发出的请求；已经发出的，回答不会采用，费用照常核算。"),
    (
        "结果",
        "只是一般性介绍，不是推荐；本机核对只会把说法降级，不会升级。只临时保留 30 分钟，服务重启后也会清除；"
        "不会自动保存，要不要保存由你决定。",
    ),
)
PROVIDER_NAMES = {"openai": "OpenAI"}
DECISIONS = tuple((state.value, DECISION_STATE_LABELS[state.value]) for state in DecisionState)
RELATIONS = (Relation.FITS, Relation.MAY_CONFLICT, Relation.UNKNOWN)
GROUP_NAMES = tuple(group for group, _ in PREFERENCE_GROUPS)

REJECTED = {
    Problem.SELECTION_INVALID: "所选的经历已经不能按核对时的版本使用，所以没有发送。请重新核对。",
    Problem.SOURCES_CHANGED: "核对之后，所选的资料有了变化，所以没有发送。请重新核对。",
    Problem.PREFERENCES_CHANGED: "核对之后，求职偏好有了变化，所以没有发送。请重新核对。",
    Problem.PREFERENCE_VERSION_UNAVAILABLE: "核对时所用的求职偏好版本已经不能再用，所以没有发送。请重新核对。",
    Problem.OUTBOUND_BLOCKED: "发送前的检查发现了疑似敏感的内容，所以没有发送。请重新核对。",
    Problem.OVER_LIMIT: "这次要发送的内容超过了上限，所以没有发送。",
    Problem.TOO_MANY_SOURCES: "这次选的经历超过了上限，所以没有发送。",
    Problem.APPROVAL_MISSING: "这次确认和核对的内容对不上，所以没有发送。请重新核对。",
    Problem.APPROVAL_MISMATCH: "这次确认和核对的内容对不上，所以没有发送。请重新核对。",
    Problem.SIMULATION_ONLY: "方向探索现在只能交给本机模拟服务，所以没有发送。",
    Problem.RECIPIENT_UNVERIFIED: "无法确认接收服务，所以没有发送。",
    Problem.RECIPIENT_MISMATCH: "接收服务和核对时说明的不一致，所以没有发送。",
    Problem.LIVE_ONLY: "这次探索只能交给核对时写明的真实服务，所以没有发送。",
    # The real service's gate (app.directions.runner.GATE_PREFIX and the gate's reason): nothing of the request went out.
    "gate_stopped": "真实服务已停用（库目录里有停用文件），所以没有发送。",
    "gate_paused": "这批批准已暂停（有费用待核实或超出预留），所以没有发送；继续需要新的批准。",
    "gate_busy": "真实服务正在处理另一个任务（例如岗位分析），所以没有发送。请等它结束后重新核对。",
    "gate_purpose": "这批批准不包括方向探索，所以没有发送。",
    "gate_requests": "这批批准的请求次数已经用完，所以没有发送；继续需要新的批准。",
    "gate_cost": "这次请求的预留费用会超出这批批准剩下的应用内预算，所以没有发送；继续需要新的批准。",
    "gate_closed": "服务正在停止，所以没有发送。",
    "gate_ended": "这次探索已经结束，所以没有发送。",
    "gate_time": "发送前的准备用完了这次请求的时限，所以没有发送。",
    "gate_input": "这次请求的输入超过了批准允许的上限，所以没有发送。",
    "gate_payload": "这次请求的内容超过了预算策略允许的大小，所以没有发送。",
    "gate_count": "发送前的计数没有完成，所以没有发送探索请求。",
}
REJECTED_OTHERWISE = "这次请求不完整，所以没有发送。请重新核对。"
FAILED = {
    "timeout": "服务没有在限定时间内回答。",
    "connection": "连不上服务。",
    "rate_limit": "服务暂时拒绝了请求。",
    "provider_response": "服务返回了错误。",
    "refusal": "服务拒绝回答这次请求。",
    "empty_response": "服务没有给出内容。",
    "invalid_json": "回答的格式不对，整份回答没有采用。",
    "schema": "回答的格式不对，整份回答没有采用。",
    "citation": "回答引用了这次没有发送的内容，整份回答没有采用。",
    "recipient_mismatch": "回答声称来自另一个服务或模型，没有采用。",
    "internal_error": "RoleRadar 自己出了错，这次探索没有完成。",
    "provider_unavailable": "真实服务现在用不了（没能建立，或者不是批准里写明的接收方），所以没有发送。",
    "provider_config": "服务的设置拦下了这次请求，没有发出。",
    "ledger_failed": "真实服务的费用记录没能写入或读出，所以没有采用这次的回答，结果不显示。已经交给服务的请求照常计数，见页面下方。",
}
# Why a confirmation does not start (app.directions.web.DirectionsRuntime.refusal): the gate's word, or "ledger".
REFUSALS = {
    "stopped": "真实服务已停用（库目录里有停用文件），现在不能开始探索；这次没有开始，什么都没有发送。",
    "paused": "这批批准已暂停，不能再发送；继续需要新的批准。这次没有开始，什么都没有发送。",
    "requests": "这批批准的请求次数已经用完，不能再发送；继续需要新的批准。这次没有开始，什么都没有发送。",
    "ledger": "现在读不出真实服务的费用记录，所以没有开始，什么都没有发送。请稍后再试。",
}
# Why an approval is paused (app.live_gate), in the words of the analysis pages, and what kind of trouble it is.
PAUSE_REASONS = {
    "interrupted": "上次运行中断，有请求按预留占用记为费用待核实",
    "unknown_usage": "有一次请求的费用无法确认，按保守占用记为费用待核实",
    "over_reservation": "有一次请求按用量核算的费用超过了它的预留",
    "above_count": "有一次请求的实际输入超过了发送前的计数",
    "count_maybe": "有一次计数请求可能已发出，费用无法确认，按计数单价记账",
}
PAUSE_KINDS = {
    "interrupted": "费用待核实",
    "unknown_usage": "费用待核实",
    "count_maybe": "费用待核实",
    "over_reservation": "超出预留",
    "above_count": "超出计数",
}
STALE = "探索期间，所用的资料有了修改：下面是当时的结果，不是当前有效的结果，不能保存，也不能据此做决定。"
CHANGED_SINCE = "这次结果用到的资料之后改过：下面是当时的结果，不能保存，也不能据此做决定。"
WITHDRAWN = "这次结果用到的资料已经删除，所以结果不再显示；恢复资料也不会让它重新出现。"
UNAVAILABLE = "探索期间，所用的资料被删除或不能再使用，所以这次结果不显示。"
SAVED_GONE = "这次探索保存以后已经永久删除：这次结果不能再保存，也不能据此做决定。要重新保存，请重新核对后再探索一次。"
CANCELLED_BY = {
    "user": "你取消了这次探索。",
    "logout": "退出登录时取消了这次探索。",
    "timeout": "这次探索超过了运行时限，已停止。",
    "shutdown": "服务停止时取消了这次探索。",
}
NOTICES = {
    "already": "这次确认已经开始过，没有重复执行。",
    "scope_updated": "已按你的选择更新核对内容，请再看一遍后确认。",
    "scope_changed": "你改动了发送范围，所以还没有开始。下面已按新的范围更新，请再看一遍后确认。",
    "service_changed": "接收方或处理说明已经变化，所以没有开始。下面已按当前的服务重新生成核对内容，请再看一遍后确认。",
    "saved": "已保存这次探索，可以在探索记录里查看。",
    "decided": "已记下你的决定。",
    "undone": "已撤销这条决定，它在回收站里，7 天内可以恢复。",
    "own": "已把这条决定改为你自己的选择；原来的依据留作历史。",
    "deleted": "已把这次探索移到回收站，7 天内可以恢复。",
    "restored": "已从回收站恢复。",
    "purged": "已永久删除，不能再恢复；数据会在清理时删掉。",
}


# The two preferences of one field each: their topic says it all.
SINGLE_STANCES = ("less", "must")


# --- which service, and what it costs ---------------------------------------------------------------------------------


def is_live(recipient) -> bool:
    return recipient.provider != SIMULATION_PROVIDER


def service_name(recipient) -> str:
    return f"{PROVIDER_NAMES.get(recipient.provider, recipient.provider)} 的 {recipient.model}"


def input_mode(recipient) -> str:
    """What the entry page says of the service a new exploration would go to: the app's, as it was started."""
    if not is_live(recipient):
        return "使用本机模拟服务：由固定规则生成结果，不是 AI。"

    return f"使用真实的外部 AI 服务：{service_name(recipient)}。下一步先列出要发送的内容，你确认后才发送。"


def task_mode(recipient) -> str:
    """What a task's running page says of its service: the recipient the task was confirmed for."""
    if not is_live(recipient):
        return "本机模拟服务：由固定规则生成结果，不是 AI。"

    return f"真实的外部 AI 服务：请求发给 {service_name(recipient)}。"


def result_mode(recipient, outcome) -> str:
    """Whose result a finished task shows: a rule result is never passed off as a real service's, nor the reverse."""
    if not is_live(recipient):
        return "本机模拟服务：由固定规则生成结果，不是 AI。"

    if outcome is not None and outcome.result is not None:
        return f"真实服务的结果：由外部 AI 模型（{service_name(recipient)}）生成，引文已在本机逐条核对。"

    return f"真实服务（{service_name(recipient)}）：这次没有可以显示的结果。"


def sends_note(recipient) -> str:
    """What follows the count of a task's requests: never offline or free for a real service."""
    if not is_live(recipient):
        return "（本机模拟，没有联网，不产生费用）"

    return "（真实服务：费用按服务商报告的用量在应用内核算，见这批批准的费用状态；不是服务商账单）"


def notice_rows(request) -> tuple:
    """The processing notice of the version a task was reviewed under, its recipient first for a real service."""
    if request.notice_version != LIVE_NOTICE_VERSION:
        return NOTICE

    recipient = request.recipient

    return (("接收方", f"外部 AI 服务：{service_name(recipient)}，请求地址 {recipient.endpoint}。"),) + LIVE_NOTICE


def pause_text(reason: str) -> str:
    """An approval paused by its ledger, said as the approval's state, never as one exploration's cost."""
    kind = PAUSE_KINDS.get(reason)
    head = f"当前批准{kind}，后续调用已暂停" if kind else "当前批准已暂停，后续调用不会再发送"

    return f"{head}：{PAUSE_REASONS.get(reason, reason)}。继续需要新的批准。"


def usd(micro_usd: int) -> str:
    return f"{micro_usd / 1_000_000:.2f}"


def condition_label(field: str) -> str:
    """Which of the user's preferences a field is: its topic and stance, or its topic alone for a single one."""
    topic, stance, _, _ = PREFERENCE_TOPICS[field]

    return topic if stance in SINGLE_STANCES else f"{topic}：{STANCE_LABELS[stance]}"


def unfilled_text(fields) -> str:
    """The preferences fields not filled in, by topic: 工作方式（理想、也可以接受）, or the topic of a single one."""
    topics: dict[str, list[str]] = {}

    for field in fields:
        topic, stance, _, _ = PREFERENCE_TOPICS[field]
        stances = topics.setdefault(topic, [])

        if stance not in SINGLE_STANCES:
            stances.append(STANCE_LABELS[stance])

    return "、".join(f"{topic}（{'、'.join(stances)}）" if stances else topic for topic, stances in topics.items())


def outcome_message(outcome, withdrawn: bool) -> str | None:
    """What a finished task says above its result, or instead of one."""
    if withdrawn:
        return WITHDRAWN

    status = outcome.status

    if status is ExplorationStatus.STALE:
        return STALE

    if status is ExplorationStatus.UNAVAILABLE:
        return UNAVAILABLE

    if status is ExplorationStatus.FAILED:
        return FAILED.get(outcome.problem, FAILED["provider_response"])

    if status is ExplorationStatus.REJECTED:
        return REJECTED.get(outcome.problem, REJECTED_OTHERWISE)

    if status is ExplorationStatus.CANCELLED:
        return CANCELLED_BY["user"]

    return None


def preference_groups(preferences) -> list[tuple[str, list]]:
    """The preferences fields of a plan in their three groups, in the order they are sent."""
    return [(group, [item for item in preferences if FIELD_GROUPS[item.field] == group]) for group in GROUP_NAMES]


def cards(result: ExplorationResult, decisions: dict, places: dict) -> list[dict]:
    """The directions of a result as the pages show them.

    decisions: the decision in use for a direction's name, by its name_key. places: the title and the category of
    the experience a quote comes from, by (item id, version id); a quote of one gone is said to be so.
    """
    shown = []

    for index, direction in enumerate(result.directions):
        decision = decisions.get(name_key(direction.name))
        relations = []

        for relation in RELATIONS:
            links = [
                {
                    "text": link.condition.text,
                    "condition": condition_label(link.condition.field),
                    "conflict": conflict_label(link),
                    "basis": link.basis,
                }
                for link in direction.ideal
                if link.relation is relation
            ]

            if links:
                relations.append((RELATION_LABELS[relation.value], links))

        shown.append(
            {
                "anchor": f"direction-{index + 1}",
                "name": direction.name,
                "main_work": direction.main_work,
                "evidence": [{"text": quote.text, "place": _place(places, quote)} for quote in direction.evidence],
                "relations": relations,
                "gaps": direction.gaps,
                "unknowns": direction.unknowns,
                "difference": direction.difference,
                "next_steps": direction.next_steps,
                "decision": decision,
                "excluded": decision is not None and decision.state == DecisionState.EXCLUDE,
            }
        )

    # Excluded before: last, and said so; the rest keep the order of the result.
    return sorted(shown, key=lambda card: card["excluded"])


def _place(places: dict, quote) -> str:
    found = places.get((quote.item_id, quote.version_id))

    if found is None:
        return "已永久删除的经历"

    title, category = found

    return f"{title} · {field_label(category, quote.field)}"


def decision_text(decision) -> str:
    return DECISION_STATE_LABELS[decision.state] if decision is not None else "未决定"


def held_text(basis) -> str:
    """Why a kept direction may not be searched for now, and what would let it be (U3-C3b)."""
    if basis in (BasisState.VERSION_IN_BIN, BasisState.RECORD_IN_BIN, BasisState.EXPLORATION_IN_BIN):
        then = "恢复它依据的内容，或者在上面把它“重新定为我的选择”之后，才能用于找岗。"
    else:
        then = "在上面把它“重新定为我的选择”之后，才能用于找岗。"

    return f"{BASIS_STATE_LABELS[basis]}。{then}"


def basis_text(decision, state) -> str:
    """What a decision rests on, and how that stands now."""
    if state is None or decision.basis == "personal":
        return BASIS_LABELS[decision.basis]

    return f"{BASIS_LABELS[decision.basis]}；{BASIS_STATE_LABELS[state]}"
