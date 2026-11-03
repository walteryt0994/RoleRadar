"""How the job analysis pages say things, in Chinese (U2-A2).

Conclusions keep their meaning: "目前未找到支持材料" is about the chosen
material, never "the user cannot"; nothing reads as a hiring probability.
Internal ids, raw JSON and English codes stay out of what is read by default.
"""
from app.analysis_contracts import (
    SIMULATION_PROVIDER,
    AnalysisFailure,
    AnalysisStatus,
    ConditionConclusion,
    EvidenceConclusion,
    PreferenceConclusion,
    RejectionReason,
    RequirementGroup,
)
from app.job_analysis.service import LIVE_NOTICE
from app.job_analysis.tasks import Step
from app.live_gate import Blocked
from app.library.projection import NOT_CONFIRMED, PREFERENCES_CHANGED, UNAVAILABLE, VERSION_CHANGED
from app.outbound_review import LABELS as FOUND_LABELS

STEP_LABELS = {
    Step.CHECKING: "核对资料",
    Step.PARSING: "解析岗位",
    Step.ANALYSING: "分析经历",
}
EVIDENCE_LABELS = {
    EvidenceConclusion.SUPPORTED: "有经历支持",
    EvidenceConclusion.NEEDS_CONFIRMATION: "待确认",
    EvidenceConclusion.NO_EVIDENCE: "目前未找到支持材料",
}
CONDITION_LABELS = {
    ConditionConclusion.MET: "符合",
    ConditionConclusion.CONFLICT: "冲突",
    ConditionConclusion.NEEDS_CONFIRMATION: "待确认",
    ConditionConclusion.UNKNOWN: "未知",
}
PREFERENCE_LABELS = {
    PreferenceConclusion.CONSISTENT: "一致",
    PreferenceConclusion.DIFFERENT: "不一致",
    PreferenceConclusion.CONFLICT: "冲突",
    PreferenceConclusion.NEEDS_CONFIRMATION: "待确认",
    PreferenceConclusion.NOT_STATED: "岗位未说明",
}
REASONS = {
    "practice": "写明了你自己做过",
    "claim": "只写了熟悉或掌握，没有写做过什么",
    "plan": "写的是打算学或打算做",
    "other_person": "写的是别人做的",
    "negated": "写明没有做过或没有用过",
    "fact": "写明的事实",
    "conflict": "写明的事实和要求不符",
    "contradiction": "所选经历里有相互矛盾的说法",
    "no_material": "所选经历里没有相关内容",
    "flag:plan": "引文所在的句子提到“计划”或“打算”",
    "flag:other_person": "引文所在的句子提到了别人",
    "flag:negated": "引文所在的句子有否定的说法",
    "flag:claim": "引文所在的句子只是说熟悉或掌握",
    "consistent": "岗位原文与偏好一致",
    "conflicts": "岗位原文与偏好相反",
    "unclear": "岗位原文提到了，但看不出是否符合",
    "no_quote": "没有岗位原文能说明",
    "other_aspect": "引用的岗位原文说的是别的方面，不能作为这一项的依据",
    "not_stated": "岗位未明确说明这一条件",
}
BASIS_LABELS = {
    "practice": "实践",
    "claim": "自述",
    "plan": "计划",
    "other_person": "他人",
    "negated": "否定",
    "fact": "事实",
    "conflict": "不符的事实",
}
GROUP_HEADINGS = {
    RequirementGroup.REQUIRED: "必要要求",
    RequirementGroup.PREFERRED: "加分项",
    RequirementGroup.UNCERTAIN: "写得不明确的要求",
}
SELECTION_PROBLEMS = {
    UNAVAILABLE: "已删除或找不到",
    NOT_CONFIRMED: "修改后还没有重新确认",
    VERSION_CHANGED: "已重新确认成新版本",
    PREFERENCES_CHANGED: "选用的求职偏好已修改",
}
REJECTED = {
    RejectionReason.SOURCES_CHANGED: "开始之后，所选的资料有了变化，所以没有继续发送。请重新核对后再开始。",
    RejectionReason.SELECTION_INVALID: "所选的资料已经不能按核对时的版本使用，所以没有发送。请重新核对后再开始。",
    RejectionReason.OUTBOUND_BLOCKED: "发送前的检查发现了疑似敏感的内容，所以没有发送。请重新核对后再开始。",
    RejectionReason.RECIPIENT_MISMATCH: "接收服务和核对时说明的不一致，所以没有发送。",
    RejectionReason.RECIPIENT_UNVERIFIED: "无法确认接收服务，所以没有发送。",
    RejectionReason.GATE_STOPPED: "真实服务已停用，这次请求没有发出。",
    RejectionReason.GATE_PAUSED: "这批批准已暂停，这次请求没有发出；继续需要新的批准。",
    RejectionReason.GATE_BUSY: "另一个真实分析正在进行，这次请求没有发出。请等它结束后重新分析。",
    RejectionReason.GATE_REQUESTS: "这批批准的请求次数已经用完，这次请求没有发出。",
    RejectionReason.GATE_COST: "这次请求按最坏情况预留会超出这批批准的费用上限，所以没有发出。",
    RejectionReason.GATE_CLOSED: "服务正在停止，这次请求没有发出。",
    RejectionReason.GATE_ENDED: "这次分析已经结束，之后的请求没有发出。",
    RejectionReason.GATE_PURPOSE: "这批批准不包括岗位分析，这次请求没有发出。",
}
FAILED = {
    AnalysisFailure.TIMEOUT: "分析服务没有在限定时间内回答。",
    AnalysisFailure.CONNECTION: "连不上分析服务。",
    AnalysisFailure.RATE_LIMIT: "分析服务暂时拒绝了请求（次数或额度限制）。",
    AnalysisFailure.PROVIDER_RESPONSE: "分析服务返回了错误。",
    AnalysisFailure.REFUSAL: "分析服务拒绝回答这次请求。",
    AnalysisFailure.EMPTY_RESPONSE: "分析服务没有给出内容。",
    AnalysisFailure.INVALID_JSON: "分析服务的回答格式不对，整份回答没有采用。",
    AnalysisFailure.SCHEMA: "分析服务的回答格式不对，整份回答没有采用。",
    AnalysisFailure.CITATION: "分析服务引用了所选资料或岗位描述里没有的原文，整份回答没有采用。",
    AnalysisFailure.COVERAGE: "分析服务漏掉或重复了某些要求，整份回答没有采用。",
    AnalysisFailure.RECIPIENT_MISMATCH: "回答声称来自另一个服务或模型，没有采用。",
    AnalysisFailure.JOB_RECORD: "岗位解析的结果和这份岗位描述对不上，没有继续。",
    AnalysisFailure.PROVIDER_UNAVAILABLE: "真实服务无法使用（无法建立，或和批准的接收方不符），没有发送任何请求。",
    # Only this request: any sent before it are counted below the result.
    AnalysisFailure.PROVIDER_CONFIG: "分析服务的本机设置有问题，这次请求在发出之前就停止了，没有发出。",
}
# Why the review offers no start while the real service's gate refuses.
REFUSALS = {
    Blocked.STOPPED: "真实服务已停用（库目录里有停用文件），现在不能开始分析。",
    Blocked.PAUSED: "这批批准已暂停，不能再发送；继续需要新的批准。",
    Blocked.REQUESTS: "这批批准的请求次数已经用完，不能再发送；继续需要新的批准。",
}
PAUSE_REASONS = {
    "interrupted": "上次运行中断，有请求的费用按预留记账",
    "unknown_usage": "有一次请求的费用无法确认，按预留记账",
    "over_reservation": "有一次请求的实际费用超过了预留",
}
PROVIDER_NAMES = {"openai": "OpenAI"}
DEGRADED = {
    "timeout": "解析岗位时服务超时",
    "connection": "解析岗位时连不上服务",
    "rate_limit": "解析岗位时服务暂时拒绝了请求",
    "provider_response": "解析岗位时服务返回了错误",
    "refusal": "服务拒绝解析这份岗位描述",
    "empty_response": "服务没有给出解析结果",
    "invalid_json": "解析结果格式不对",
    "schema": "解析结果格式不对",
    "evidence": "解析结果引用了岗位描述里没有的原文",
}
CANCELLED_BY = {
    "user": "你取消了这次分析。",
    "logout": "退出登录时取消了这次分析。",
    "timeout": "这次分析超过了运行时限，已停止。",
    "shutdown": "服务停止时取消了这次分析。",
}


def service_view(jobs) -> dict:
    """What the review page says about the service a task would go to."""
    if jobs.live is None:
        return {"live": False, "refusal": None}

    recipient = jobs.live.recipient
    status = jobs.live.gate.status()
    refusal = jobs.refusal()

    return {
        "live": True,
        "provider": PROVIDER_NAMES.get(recipient.provider, recipient.provider),
        "model": recipient.model,
        "endpoint": recipient.endpoint,
        "notice": LIVE_NOTICE,
        "approval": jobs.live.gate.terms.approval_id,
        "requests_left": status.requests_left,
        "usd_left": f"{status.micro_usd_left / 1_000_000:.2f}",
        "paused": PAUSE_REASONS.get(status.paused_reason, status.paused_reason) if status.paused_reason else None,
        "refusal": REFUSALS[refusal] if refusal is not None else None,
    }


def running_label(task) -> str:
    recipient = task.request.recipient

    if recipient.provider == SIMULATION_PROVIDER:
        return "本机模拟服务：由固定规则生成结果，不是 AI。"

    return f"真实服务：请求发给 {PROVIDER_NAMES.get(recipient.provider, recipient.provider)} 的 {recipient.model}。"


def mode_label(result) -> str:
    """Whose result this is: a rule result is never passed off as a real service's."""
    if result.mode == "simulation":
        return "本机模拟服务的结果：由固定规则生成，不是 AI 的判断。"

    if result.status is AnalysisStatus.DEGRADED:
        return "规则降级的结果：真实服务没能完成岗位解析，下面只是按规则找到的技能词，不是 AI 的判断。"

    if result.status in (AnalysisStatus.COMPLETE, AnalysisStatus.STALE):
        return "真实服务的结果：由外部 AI 模型生成，引文已和原文逐条核对。"

    return "真实服务：这次没有得到可用的分析结果。"


def score_text(value) -> str:
    """A coverage score as people write it: 80, 66.67."""
    return f"{value.normalize():f}"


def found_labels(kinds) -> str:
    return "、".join(FOUND_LABELS[kind] for kind in kinds)


def conclusion_label(finding) -> str:
    if finding.group is RequirementGroup.HARD_CONSTRAINT:
        return CONDITION_LABELS[ConditionConclusion(finding.conclusion)]

    return EVIDENCE_LABELS[EvidenceConclusion(finding.conclusion)]


def preference_label(finding) -> str:
    return PREFERENCE_LABELS[PreferenceConclusion(finding.conclusion)]


def reason_labels(reasons) -> list[str]:
    return [REASONS.get(reason, reason) for reason in reasons]


def outcome_message(result) -> str | None:
    """What went wrong with a result that is not a complete analysis, in words."""
    if result.status is AnalysisStatus.REJECTED:
        return REJECTED.get(RejectionReason(result.problem), "这次分析没有开始。")

    if result.status is AnalysisStatus.FAILED:
        known = {failure.value: text for failure, text in FAILED.items()}

        return known.get(result.problem, "分析没有完成（程序出错）。没有采用任何结果。")

    if result.status is AnalysisStatus.DEGRADED:
        reason = DEGRADED.get(result.problem, "岗位描述没能完整解析")

        return f"{reason}，所以没有做经历分析。下面只列出按规则在岗位描述里找到的技能词，不是分析结论，也没有分数。"

    if result.status is AnalysisStatus.STALE:
        return "分析完成之后，所选的资料有了变化。这份结果按核对时的版本得出，不是当前有效的分析。"

    return None
