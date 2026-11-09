"""What the direction pages say, and the shape their results are shown in (U3-C3a).

The words of the review page are the processing notice of NOTICE_VERSION (app.directions.explore): what goes out,
to whom, for what, and how long the result stays. Changing them means a new version, and with it a new fingerprint
for every task: a confirmation of the old words never carries over.

A result is shown direction by direction, every direction with its parts in the same order so they can be compared:
its general description, the user's own words it rests on, how it relates to what the user wrote they want, what is
still to learn, what cannot be told yet, how it differs from the others, and what to try next. No score, no rank, no
"best fit". A direction the user excluded before is shown last and said to be excluded, never hidden; nothing else
changes the order the result gave.
"""
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

STEP = "探索方向"
# The processing notice of NOTICE_VERSION "direction-1": the local simulation only.
NOTICE = (
    ("接收方", "本机模拟服务：RoleRadar 内置的固定规则，不是 AI。在这台电脑上运行，不联网，不保存请求。"),
    ("用途", "只用于这一次方向探索：给出 3 到 5 个可以对比的方向，以及它们和你的经历、偏好的关系。"),
    ("步骤", "确认后发出一个请求，随时可以取消。"),
    ("结果", "只临时保留 30 分钟，服务重启后也会清除；不会自动保存，要留下请在结果页保存。"),
)
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


def basis_text(decision, state) -> str:
    """What a decision rests on, and how that stands now."""
    if state is None or decision.basis == "personal":
        return BASIS_LABELS[decision.basis]

    return f"{BASIS_LABELS[decision.basis]}；{BASIS_STATE_LABELS[state]}"
