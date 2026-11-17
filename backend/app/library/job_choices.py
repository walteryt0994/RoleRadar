"""The fixed choices of what the user says of a kept posting (U3-D3a): decisions, their reasons, and what feedback
can point at, and the words for what a kept check says. Codes are stored; the words are what pages and exports show.
Shared by app.discovery.records (which checks a form against it), its pages and app.library.job_export, so that the
library loads nothing of a search."""
from collections.abc import Callable
from dataclasses import dataclass

from app.library.models import JobDecision

# What a kept check says (U3-D3a F1): what its source answered at that time, or that it gave no answer then. A check's
# status is only ever what the search knew; none is guessed from its time or from any text.
CHECK_OPEN = "open"
CHECK_CLOSED = "closed"
# Its source answered, but the search kept no status for that answer: never taken for open.
CHECK_UNKNOWN = "unknown"
CHECK_UNANSWERED = "unanswered"
ANSWERED = (CHECK_OPEN, CHECK_CLOSED, CHECK_UNKNOWN)
CHECK_WORDS = {
    CHECK_OPEN: "核对时仍公开发布（不代表企业还有名额）",
    CHECK_CLOSED: "核对时已关闭",
    CHECK_UNKNOWN: "来源回答了，但没有可靠的状态",
    CHECK_UNANSWERED: "核对没有得到回答，状态待确认",
}


@dataclass(frozen=True)
class CheckState:
    """A kept posting's checks in short: the latest its source answered, and the latest without an answer."""

    answered: object | None = None
    unanswered: object | None = None

    @property
    def latest(self):
        candidates = [check for check in (self.answered, self.unanswered) if check is not None]

        return max(candidates, key=lambda check: check.checked_at) if candidates else None


def check_state(checks) -> CheckState:
    """From checks with a status and a time each (rows of JobPostingCheck, newest or oldest first alike)."""
    answered = [check for check in checks if check.status in ANSWERED]
    unanswered = [check for check in checks if check.status == CHECK_UNANSWERED]

    return CheckState(
        max(answered, key=lambda check: check.checked_at) if answered else None,
        max(unanswered, key=lambda check: check.checked_at) if unanswered else None,
    )


def check_line(check, when: Callable) -> str:
    """One kept check: an answer's time and what it said, or the time a check got no answer."""
    if check.status == CHECK_UNANSWERED:
        return f"核对没有得到回答：{when(check.checked_at)}（尝试核对的时间），状态待确认"

    return f"来源回答：{when(check.checked_at)}，{CHECK_WORDS.get(check.status, CHECK_WORDS[CHECK_UNKNOWN])}"


def state_text(state: CheckState, when: Callable) -> str:
    """What the checks kept say now, in one line: never "still open" unless the latest check answered so."""
    latest = state.latest

    if latest is None:
        return "没有来源核对记录"

    if latest.status == CHECK_UNANSWERED:
        text = f"状态待确认：最近一次核对（{when(latest.checked_at)}）没有得到回答"

        if state.answered is not None:
            text += f"；最后一次有回答是 {when(state.answered.checked_at)}，{CHECK_WORDS.get(state.answered.status, CHECK_WORDS[CHECK_UNKNOWN])}"

        return text

    return f"{CHECK_WORDS.get(latest.status, CHECK_WORDS[CHECK_UNKNOWN])}（来源回答 {when(latest.checked_at)}）"


# --- a kept posting's checks together (U3-D4a) ------------------------------------------------------------------------
#
# The checks a search kept (JobPostingCheck) and those made from the record since (JobPostingRecheck), read as one:
# the latest attempt, the latest its source answered, and the latest success are three things, never one taken for
# another. A success is "still published" or "closed" with its evidence kept; a search's old "closed" has none, so it is
# only what its source said then, never a confirmed close, and an old unknown is no success either. When the latest
# attempt is not a success the posting's status is to be confirmed, with the last success's time kept as it was.

# A search's "closed" as the record shows it now: what its source answered then, with no evidence kept.
CHECK_CLOSED_THEN = "核对时已关闭（当时来源的回答，没有关闭证据，不算确认关闭）"
# A posting read from a real source the Owner admitted (U3-D6): Access.LOCAL_FETCH of app.discovery.contracts, by its
# stored value, so that the library loads nothing of a search (as job_texts.USER_PROVIDED).
REAL_SOURCE = "local_fetch"


def closure_note(source_access: str, deadline: str | None) -> str | None:
    """What the status of a real source's posting can and cannot show (U3-D6, D6 plan 1.4): closed only once the
    deadline the source itself gives has passed; a 404, no answer or a failed read leaves it to be confirmed, the
    checks' judgement unchanged. None for a posting of any other source."""
    if source_access != REAL_SOURCE:
        return None

    note = (
        "这是真实来源上的岗位：只有来源自己给出的截止日期已过，才记为已关闭；来源说找不到它（404）、没有回答或读取失败，"
        "都只记为状态待确认，不算关闭。"
    )

    return note if deadline else note + "这个来源没有给截止日期，所以多半判断不了它是否已关闭。"
# Why a check made from the record settled nothing, or got no answer.
RECHECK_REASONS = {
    "timeout": "来源没有在时限内回答",
    "not_sent": "发送前停下，没有发出请求",
    "anomaly": "来源返回了不符合要求的内容",
    "failed": "请求没有成功",
    "source_unavailable": "这个来源现在没有接入，没有发出请求",
    "not_found": "来源回应没有这个岗位（404），但没有完整列表佐证，不据此判断已关闭",
    "not_found_but_listed": "详情回应 404，但完整列表里还有它，不据此判断",
    "listing_partial": "详情回应 404，但列表没有读完，不据此判断",
    "listing_failed": "详情回应 404，但读取列表没有成功，不据此判断",
    "closed_without_evidence": "来源说已关闭，但没有可核对的证据",
    "identity_mismatch": "来源返回的是别的岗位",
    "no_status": "来源没有给出状态",
    "deadline_conflict": "同一次核对里，详情和核对给出的截止日期不一致，不据此判断",
    # A batch's budget ran out half way through this check (U3-D4b-1): it was tried, and not finished.
    "time_used_up": "这一轮的 2 分钟用完了，核对没做完",
    "calls_used_up": "这一轮的来源调用次数用完了，核对没做完",
    "stopped": "应用正在关闭，核对没做完",
}
# Which answer of a check gave the deadline a close rests on (F1).
DEADLINE_FROM = {"both": "详情和核对给出的一致", "check": "核对时来源给出，详情里没有写", "read": "详情里写的"}
# How a check made from the record was asked for.
TRIGGER_WORDS = {"recheck": "再核对一次", "apply": "申请前核对", "manual": "现在核对", "scheduled": "定时检查", "link": "补充链接时读取"}
# How a batch of checks ended (U3-D4b-1), and why it stopped.
BATCH_OUTCOMES = {"complete": "全部完成", "partial": "部分完成", "interrupted": "中断"}
BATCH_REASONS = {
    "postings_limit": "到了每轮 20 个岗位的上限，其余留到下一轮",
    "calls_limit": "到了每轮 60 次来源调用的上限",
    "time_limit": "到了每轮 2 分钟的时限",
    "source_stopped": "有来源回答异常或连续超时，这一轮不再调用它",
    "app_closing": "应用正在关闭",
    "error": "出错",
    "not_finished": "应用关闭或出错，这一轮没有收尾",
}
# The facts a check compares with the version before, in the pages' words; "text" is only that the text changed.
CHANGE_FIELDS = {
    "title": "职位",
    "company": "公司",
    "cities": "地点",
    "places_stated": "地点（来源原文）",
    "job_type": "类型",
    "type_stated": "类型（来源原文）",
    "posted_on": "发布日期",
    "deadline": "截止日期",
    "url": "原始链接",
    "apply_url": "申请入口",
    "text": "岗位正文",
}


@dataclass(frozen=True)
class CheckSummary:
    """A kept posting's checks of both kinds in short (U3-D4a)."""

    attempted: object | None = None
    answered: object | None = None
    succeeded: object | None = None


def is_success(check) -> bool:
    """Still published, or closed with its evidence kept; a search's "closed" has none."""
    return check.status == CHECK_OPEN or (check.status == CHECK_CLOSED and bool(getattr(check, "evidence", None)))


def summarize(checks) -> CheckSummary:
    """From checks of either kind, each with a status, the time of its answer or try, and when it was recorded."""
    checks = list(checks)

    def latest(rows):
        return max(rows, key=lambda check: (check.checked_at, check.recorded_at)) if rows else None

    return CheckSummary(
        latest(checks),
        latest([check for check in checks if check.status in ANSWERED]),
        latest([check for check in checks if is_success(check)]),
    )


def evidence_text(evidence: dict) -> str:
    """What a close rests on, in words: only what was kept of it."""
    kind = evidence.get("kind")

    if kind == "deadline":
        given = DEADLINE_FROM.get(evidence.get("from"))

        return f"来源写的截止日期 {evidence.get('deadline')} 已过" + (f"（{given}）" if given else "")

    if kind == "delisted":
        return f"同一次核对里，完整列表（{evidence.get('listing_count')} 个岗位）没有它，详情也回应 404"

    if kind == "synthetic":
        return "合成来源模拟的关闭（离线演示，不是真实核验）"

    return "证据不明"


def check_words(check) -> str:
    """What one check of either kind found, as the record says it now."""
    reason = getattr(check, "reason", None)
    evidence = getattr(check, "evidence", None)

    if check.status == CHECK_OPEN:
        return CHECK_WORDS[CHECK_OPEN]

    if check.status == CHECK_CLOSED:
        return f"来源已明确关闭：{evidence_text(evidence)}" if evidence else CHECK_CLOSED_THEN

    words = CHECK_WORDS[CHECK_UNANSWERED] if check.status == CHECK_UNANSWERED else CHECK_WORDS[CHECK_UNKNOWN]

    return words + (f"（{RECHECK_REASONS.get(reason, reason)}）" if reason else "")


def summary_text(summary: CheckSummary, when: Callable) -> str:
    """The posting's status now, in one line: a success as it is; anything else to be confirmed, with the last success
    and, when it says more, the last answer."""
    latest = summary.attempted

    if latest is None:
        return "没有来源核对记录"

    if is_success(latest):
        return f"{check_words(latest)}（来源回答 {when(latest.checked_at)}）"

    if latest.status == CHECK_UNANSWERED:
        text = f"状态待确认：最近一次核对（{when(latest.checked_at)}）没有得到回答"
    elif latest.status == CHECK_CLOSED:
        text = f"状态待确认：最近一次核对（{when(latest.checked_at)}）来源回答已关闭，但没有关闭证据，不算确认关闭"
    else:
        text = f"状态待确认：最近一次核对（{when(latest.checked_at)}）来源回答了，但没有可靠的状态"

    reason = getattr(latest, "reason", None)

    if reason:
        text += f"（{RECHECK_REASONS.get(reason, reason)}）"

    if summary.succeeded is not None:
        text += f"；上次成功核对是 {when(summary.succeeded.checked_at)}，{check_words(summary.succeeded)}"
    else:
        text += "；还没有成功核对过"

    answered = summary.answered

    if answered is not None and answered is not latest and answered is not summary.succeeded:
        text += f"；最后一次有回答是 {when(answered.checked_at)}，{check_words(answered)}"

    return text


def history_line(check, when: Callable) -> str:
    """One check of either kind, as the record lists it."""
    if check.status == CHECK_UNANSWERED:
        reason = getattr(check, "reason", None)
        why = f"（{RECHECK_REASONS.get(reason, reason)}）" if reason else ""

        return f"核对没有得到回答：{when(check.checked_at)}（尝试核对的时间），状态待确认{why}"

    return f"来源回答：{when(check.checked_at)}，{check_words(check)}"


def change_lines(changes) -> list[str]:
    """What a check found changed since the version before, never the text's words."""
    lines = []

    for change in changes or ():
        name = CHANGE_FIELDS.get(change.get("field"), change.get("field"))

        if change.get("field") == "text":
            lines.append(f"{name}有变化（和上一次读到的版本不同）")
            continue

        lines.append(f"{name}：{_shown(change.get('before'))} → {_shown(change.get('after'))}")

    return lines


def deadline_text(facts: dict) -> str:
    """A check's deadline as its page says it (F1): the one it settled on and which answer gave it, or both answers
    when they differed; never a date with nothing said of where it came from."""
    answers = facts.get("deadline_answers") or {}
    stated, checked = answers.get("read"), answers.get("check")

    if stated and checked and stated != checked:
        return f"两次回答不一致：详情里写的是 {stated}，核对时来源给出 {checked}；不据此判断"

    deadline = facts.get("deadline")

    if deadline is None:
        return "来源没有写"

    if checked and not stated:
        return f"{deadline}（核对时来源给出，详情里没有写）"

    return f"{deadline}（这次读到的）"


def batch_lines(run, counts, when: Callable) -> list[str]:
    """A batch of checks as the records page says it (U3-D4b-1): what it actually did, never what is due now."""
    ended = f"结束于 {when(run.finished_at)}" if run.finished_at else "没有收尾"
    lines = [f"{TRIGGER_WORDS.get(run.trigger, run.trigger)}：开始于 {when(run.started_at)}，{ended}"]

    if run.outcome is None:
        lines.append("正在进行")
    else:
        why = [BATCH_REASONS.get(code, code) for code in (run.reason or "").split(",") if code]
        lines.append(BATCH_OUTCOMES.get(run.outcome, run.outcome) + (f"：{'；'.join(why)}" if why else ""))

    lines.append(f"调用来源 {run.calls} 次")

    if counts is None:
        lines.append("没有计数：这一轮没有收尾")
    else:
        lines.append(
            f"开始时有 {counts.due} 个待检查，这一轮选了 {counts.selected} 个：成功 {counts.succeeded} 个，待确认 {counts.failed} 个，"
            f"作废 {counts.discarded} 个（核对期间记录变了），跳过 {counts.skipped} 个（不再需要核对或正在核对），没轮到 {counts.unreached} 个"
        )

    return lines


def _shown(value) -> str:
    if value is None or value == [] or value == "":
        return "未提供"

    if isinstance(value, dict):
        # A deadline two answers of one check gave differently (F1).
        return f"两次回答不一致（详情 {_shown(value.get('read'))}，核对 {_shown(value.get('check'))}）"

    if isinstance(value, list):
        return "、".join(str(item) for item in value)

    return str(value)


DECISION_LABELS = {
    JobDecision.FAVORITE: "收藏",
    JobDecision.DEFER: "暂缓",
    JobDecision.SKIP: "跳过",
    # The user's own decision (U3-D4a), never that anything was checked, sent or applied for.
    JobDecision.APPLY: "准备申请",
}
# Why, as fixed codes: the user picks any, or none.
REASONS = {
    JobDecision.FAVORITE: (
        ("direction", "方向对"),
        ("reachable", "要求能达到"),
        ("company", "想去这家公司"),
        ("other", "其他"),
    ),
    JobDecision.DEFER: (
        ("build_up", "要先补经历或技能"),
        ("more_info", "等更多信息"),
        ("timing", "时间不合适"),
        ("other", "其他"),
    ),
    JobDecision.SKIP: (
        ("type", "类型不对（比如不是实习）"),
        ("place", "地点不对"),
        ("too_demanding", "要求太高"),
        ("direction", "不想做这个方向"),
        ("company", "不想去这家公司"),
        ("handled", "已经处理过"),
        ("other", "其他"),
    ),
    # No fixed reasons were decided for it (U3-D4a): a note only.
    JobDecision.APPLY: (),
}
# Which fact is not right, and how (D3a: a posting's facts only; an analysis is D3b's).
FEEDBACK_FIELDS = (
    ("company", "公司"),
    ("title", "职位"),
    ("place", "地点"),
    ("job_type", "类型"),
    ("dates", "发布或截止日期"),
    ("check", "核对结果"),
    ("link", "链接"),
)
FEEDBACK_REASONS = (
    ("wrong", "认错了"),
    ("missing", "漏了"),
    ("wording", "说法不对"),
    ("other", "其他"),
)
EVIDENCE_WORDS = {"official": "官方原始发布", "lead": "待核验线索", "user_provided": "用户提供，系统未独立核验"}


def decision_label(decision: str | None) -> str:
    return "没有决定" if decision is None else DECISION_LABELS[JobDecision(decision)]


def reason_labels(decision: str | None, codes) -> list[str]:
    if decision is None:
        return []

    words = dict(REASONS[JobDecision(decision)])

    return [words.get(code, code) for code in codes]


def feedback_label(field: str, reason: str) -> str:
    return f"{dict(FEEDBACK_FIELDS).get(field, field)}：{dict(FEEDBACK_REASONS).get(reason, reason)}"


# --- kept analyses (U3-D3b) -----------------------------------------------------------------------------------------
# What feedback on a kept analysis can point at, and why. The part pointed at is one the analysis has: its category, a
# requirement (by its id), an experience quote (by its requirement and place), a preference compared (by its id).
ANALYSIS_TARGETS = (
    ("category", "分类"),
    ("requirement", "招聘条件的判断"),
    ("citation", "经历引文"),
    ("preference", "偏好对照"),
)
ANALYSIS_REASONS = (
    ("wrong", "判断不对"),
    ("missing", "漏了"),
    ("wording", "说法不对"),
    ("other", "其他"),
)
# How what a kept analysis rests on stands now (app.library.references.SourceState), as pages and exports say it.
BASIS_WORDS = {
    "current": "依据和保存时一致",
    "changed": "依据已修改：经历或偏好在保存之后改过，下面是按当时内容得出的分析",
    "version_in_bin": "依据的版本在回收站里",
    "record_in_bin": "依据的经历或偏好在回收站里",
    "gone": "依据已永久删除，下面只剩保存时的原话",
}


def analysis_feedback_label(target: str, reason: str) -> str:
    return f"{dict(ANALYSIS_TARGETS).get(target, target)}：{dict(ANALYSIS_REASONS).get(reason, reason)}"


def basis_text(state) -> str:
    return BASIS_WORDS.get(getattr(state, "value", state), str(state))


# A kept analysis's category, as the search's result page names it (app.discovery.contracts.CATEGORY_LABELS; a test
# keeps the two the same): the library loads nothing of a search to say it.
CATEGORY_WORDS = {
    "strong": "依据较充分",
    "partial": "有基础但有差距或待确认",
    "conflict": "有明确冲突，需谨慎考虑",
}
