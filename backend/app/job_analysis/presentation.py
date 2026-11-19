"""How the job analysis pages say things, in Chinese (U2-A2).

Conclusions keep their meaning: "目前未找到支持材料" is about the chosen
material, never "the user cannot"; nothing reads as a hiring probability.
Internal ids, raw JSON and English codes stay out of what is read by default.
"""
from app.analysis_contracts import (
    SIMULATION_PROVIDER,
    AnalysisFailure,
    AnalysisStatus,
    RejectionReason,
)
# The words of an analysis's judgments are said from one place (U3-D3b F1), so the pages and a kept analysis's export
# say the same; the names stay here for the pages that read them.
from app.analysis_words import (  # noqa: F401
    BASIS_LABELS,
    CONDITION_LABELS,
    EVIDENCE_LABELS,
    GROUP_HEADINGS,
    PREFERENCE_LABELS,
    REASONS,
    conclusion_label,
    preference_label,
    reason_labels,
)
from app.job_analysis.service import LIVE_NOTICE
from app.job_analysis.tasks import Step
from app.live_gate import Blocked
from app.library.projection import NOT_CONFIRMED, PREFERENCES_CHANGED, UNAVAILABLE, VERSION_CHANGED
from app.outbound_review import NAMES as FOUND_LABELS
from app.i18n import MessageMap, lazy, list_join, t

STEP_LABELS = MessageMap({
    Step.CHECKING: "job.apres.step_labels.1",
    Step.PARSING: "job.apres.step_labels.2",
    Step.ANALYSING: "job.apres.step_labels.3",
})
SELECTION_PROBLEMS = MessageMap({
    UNAVAILABLE: "job.apres.selection_problems.1",
    NOT_CONFIRMED: "job.apres.selection_problems.2",
    VERSION_CHANGED: "job.apres.selection_problems.3",
    PREFERENCES_CHANGED: "job.apres.selection_problems.4",
})
REJECTED = MessageMap({
    RejectionReason.SOURCES_CHANGED: "job.apres.rejected.1",
    RejectionReason.SELECTION_INVALID: "job.apres.rejected.2",
    RejectionReason.OUTBOUND_BLOCKED: "job.apres.rejected.3",
    RejectionReason.RECIPIENT_MISMATCH: "job.apres.rejected.4",
    RejectionReason.RECIPIENT_UNVERIFIED: "job.apres.rejected.5",
    # These two and GATE_CLOSED may also stop a generation after its count: what went out is counted below.
    RejectionReason.GATE_STOPPED: "job.apres.rejected.6",
    RejectionReason.GATE_PAUSED: "job.apres.rejected.7",
    RejectionReason.GATE_BUSY: "job.apres.rejected.8",
    RejectionReason.GATE_REQUESTS: "job.apres.rejected.9",
    RejectionReason.GATE_COST: "job.apres.rejected.10",
    RejectionReason.GATE_CLOSED: "job.apres.rejected.11",
    RejectionReason.GATE_ENDED: "job.apres.rejected.12",
    RejectionReason.GATE_PURPOSE: "job.apres.rejected.13",
    RejectionReason.GATE_COUNT: "job.apres.rejected.14",
    RejectionReason.GATE_TIME: "job.apres.rejected.15",
    RejectionReason.GATE_INPUT: "job.apres.rejected.16",
    RejectionReason.GATE_PAYLOAD: "job.apres.rejected.17",
})
FAILED = MessageMap({
    AnalysisFailure.TIMEOUT: "job.apres.failed.1",
    AnalysisFailure.CONNECTION: "job.apres.failed.2",
    AnalysisFailure.RATE_LIMIT: "job.apres.failed.3",
    AnalysisFailure.PROVIDER_RESPONSE: "job.apres.failed.4",
    AnalysisFailure.REFUSAL: "job.apres.failed.5",
    AnalysisFailure.EMPTY_RESPONSE: "job.apres.failed.6",
    AnalysisFailure.INVALID_JSON: "job.apres.failed.7",
    AnalysisFailure.SCHEMA: "job.apres.failed.8",
    AnalysisFailure.CITATION: "job.apres.failed.9",
    AnalysisFailure.COVERAGE: "job.apres.failed.10",
    AnalysisFailure.RECIPIENT_MISMATCH: "job.apres.failed.11",
    AnalysisFailure.JOB_RECORD: "job.apres.failed.12",
    AnalysisFailure.PROVIDER_UNAVAILABLE: "job.apres.failed.13",
    # Only this request: any sent before it are counted below the result.
    AnalysisFailure.PROVIDER_CONFIG: "job.apres.failed.14",
})
# Why the review offers no start while the real service's gate refuses.
REFUSALS = MessageMap({
    Blocked.STOPPED: "job.apres.refusals.1",
    Blocked.PAUSED: "job.apres.refusals.2",
    Blocked.REQUESTS: "job.apres.refusals.3",
})
PAUSE_REASONS = MessageMap({
    "interrupted": "job.apres.pause_reasons.1",
    "unknown_usage": "job.apres.pause_reasons.2",
    "over_reservation": "job.apres.pause_reasons.3",
    "above_count": "job.apres.pause_reasons.4",
    "count_maybe": "job.apres.pause_reasons.5",
})
PROVIDER_NAMES = {"openai": "OpenAI"}
DEGRADED = MessageMap({
    "timeout": "job.apres.degraded.1",
    "connection": "job.apres.degraded.2",
    "rate_limit": "job.apres.degraded.3",
    "provider_response": "job.apres.degraded.4",
    "refusal": "job.apres.degraded.5",
    "empty_response": "job.apres.degraded.6",
    "invalid_json": "job.apres.degraded.7",
    "schema": "job.apres.degraded.8",
    "evidence": "job.apres.degraded.9",
})
CANCELLED_BY = MessageMap({
    "user": "job.apres.cancelled_by.1",
    "logout": "job.apres.cancelled_by.2",
    "timeout": "job.apres.cancelled_by.3",
    "shutdown": "job.apres.cancelled_by.4",
})


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
        "usd_left": _usd(status.micro_usd_left),
        "usd_budget": _usd(jobs.live.gate.terms.max_cost_micro_usd),
        # Never one figure called charged: what usage accounted for, what is only reserved, what is still to be checked.
        "usd_settled": _usd(status.settled_micro_usd),
        "usd_reserved": _usd(status.reserved_micro_usd),
        "usd_unknown": _usd(status.unknown_micro_usd),
        "paused": PAUSE_REASONS.get(status.paused_reason, status.paused_reason) if status.paused_reason else None,
        "refusal": REFUSALS[refusal] if refusal is not None else None,
    }


def _usd(micro_usd: int) -> str:
    return f"{micro_usd / 1_000_000:.2f}"


def service_label(recipient) -> str:
    """Which service a task goes to, as the input page (the app's) and the running page (the task's) say it."""
    if recipient.provider == SIMULATION_PROVIDER:
        return t("job.apres.60")

    return t("job.apres.61", p0=PROVIDER_NAMES.get(recipient.provider, recipient.provider), p1=recipient.model)


def running_label(task) -> str:
    return service_label(task.request.recipient)


def mode_label(result) -> str:
    """Whose result this is: a rule result is never passed off as a real service's."""
    if result.mode == "simulation":
        return t("job.apres.62")

    if result.status is AnalysisStatus.DEGRADED:
        return t("job.apres.63")

    if result.status in (AnalysisStatus.COMPLETE, AnalysisStatus.STALE):
        return t("job.apres.64")

    return t("job.apres.65")


def score_text(value) -> str:
    """A coverage score as people write it: 80, 66.67."""
    return f"{value.normalize():f}"


def found_labels(kinds) -> str:
    return list_join(FOUND_LABELS[kind] for kind in kinds)


def outcome_message(result) -> str | None:
    """What went wrong with a result that is not a complete analysis, in words."""
    if result.status is AnalysisStatus.REJECTED:
        return REJECTED.get(RejectionReason(result.problem), t("job.apres.67"))

    if result.status is AnalysisStatus.FAILED:
        known = {failure.value: text for failure, text in FAILED.items()}

        return known.get(result.problem, t("job.apres.68"))

    if result.status is AnalysisStatus.DEGRADED:
        reason = DEGRADED.get(result.problem, t("job.apres.69"))

        return t("job.apres.70", p0=reason)

    if result.status is AnalysisStatus.STALE:
        return t("job.apres.71")

    return None
