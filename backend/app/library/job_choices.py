"""The fixed choices of what the user says of a kept posting (U3-D3a): decisions, their reasons, and what feedback
can point at, and the words for what a kept check says. Codes are stored; the words are what pages and exports show.
Shared by app.discovery.records (which checks a form against it), its pages and app.library.job_export, so that the
library loads nothing of a search."""
from collections.abc import Callable
from dataclasses import dataclass

from app.library.models import JobDecision
from app.i18n import MessageMap, lazy, list_join, t

# What a kept check says (U3-D3a F1): what its source answered at that time, or that it gave no answer then. A check's
# status is only ever what the search knew; none is guessed from its time or from any text.
CHECK_OPEN = "open"
CHECK_CLOSED = "closed"
# Its source answered, but the search kept no status for that answer: never taken for open.
CHECK_UNKNOWN = "unknown"
CHECK_UNANSWERED = "unanswered"
ANSWERED = (CHECK_OPEN, CHECK_CLOSED, CHECK_UNKNOWN)
CHECK_WORDS = MessageMap({
    CHECK_OPEN: "job.choice.check_words.1",
    CHECK_CLOSED: "job.choice.check_words.2",
    CHECK_UNKNOWN: "job.choice.check_words.3",
    CHECK_UNANSWERED: "job.choice.check_words.4",
})


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
        return t("job.choice.65", p0=when(check.checked_at))

    return t("job.choice.66", p0=when(check.checked_at), p1=CHECK_WORDS.get(check.status, CHECK_WORDS[CHECK_UNKNOWN]))


def state_text(state: CheckState, when: Callable) -> str:
    """What the checks kept say now, in one line: never "still open" unless the latest check answered so."""
    latest = state.latest

    if latest is None:
        return t("job.choice.67")

    if latest.status == CHECK_UNANSWERED:
        text = t("job.choice.68", p0=when(latest.checked_at))

        if state.answered is not None:
            text += t("job.choice.69", p0=when(state.answered.checked_at), p1=CHECK_WORDS.get(state.answered.status, CHECK_WORDS[CHECK_UNKNOWN]))

        return text

    return t("job.choice.70", p0=CHECK_WORDS.get(latest.status, CHECK_WORDS[CHECK_UNKNOWN]), p1=when(latest.checked_at))


# --- a kept posting's checks together (U3-D4a) ------------------------------------------------------------------------
#
# The checks a search kept (JobPostingCheck) and those made from the record since (JobPostingRecheck), read as one:
# the latest attempt, the latest its source answered, and the latest success are three things, never one taken for
# another. A success is "still published" or "closed" with its evidence kept; a search's old "closed" has none, so it is
# only what its source said then, never a confirmed close, and an old unknown is no success either. When the latest
# attempt is not a success the posting's status is to be confirmed, with the last success's time kept as it was.

# A search's "closed" as the record shows it now: what its source answered then, with no evidence kept.
CHECK_CLOSED_THEN = lazy("job.choice.71")
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
        t("job.choice.72")
    )

    return note if deadline else note + t("job.choice.73")
# Why a check made from the record settled nothing, or got no answer.
RECHECK_REASONS = MessageMap({
    "timeout": "job.choice.recheck_reasons.1",
    "not_sent": "job.choice.recheck_reasons.2",
    "anomaly": "job.choice.recheck_reasons.3",
    "failed": "job.choice.recheck_reasons.4",
    "source_unavailable": "job.choice.recheck_reasons.5",
    "not_found": "job.choice.recheck_reasons.6",
    "not_found_but_listed": "job.choice.recheck_reasons.7",
    "listing_partial": "job.choice.recheck_reasons.8",
    "listing_failed": "job.choice.recheck_reasons.9",
    "closed_without_evidence": "job.choice.recheck_reasons.10",
    "identity_mismatch": "job.choice.recheck_reasons.11",
    "no_status": "job.choice.recheck_reasons.12",
    "deadline_conflict": "job.choice.recheck_reasons.13",
    # A batch's budget ran out half way through this check (U3-D4b-1): it was tried, and not finished.
    "time_used_up": "job.choice.recheck_reasons.14",
    "calls_used_up": "job.choice.recheck_reasons.15",
    "stopped": "job.choice.recheck_reasons.16",
})
# Which answer of a check gave the deadline a close rests on (F1).
DEADLINE_FROM = MessageMap({"both": "job.choice.deadline_from.1", "check": "job.choice.deadline_from.2", "read": "job.choice.deadline_from.3"})
# How a check made from the record was asked for.
TRIGGER_WORDS = MessageMap({"recheck": "job.choice.trigger_words.1", "apply": "job.choice.trigger_words.2", "manual": "job.choice.trigger_words.3", "scheduled": "job.choice.trigger_words.4", "link": "job.choice.trigger_words.5", "refresh": "job.refresh.trigger"})
# How a batch of checks ended (U3-D4b-1), and why it stopped.
BATCH_OUTCOMES = MessageMap({"complete": "job.choice.batch_outcomes.1", "partial": "job.choice.batch_outcomes.2", "interrupted": "job.choice.batch_outcomes.3"})
BATCH_REASONS = MessageMap({
    "postings_limit": "job.choice.batch_reasons.1",
    "calls_limit": "job.choice.batch_reasons.2",
    "time_limit": "job.choice.batch_reasons.3",
    "source_stopped": "job.choice.batch_reasons.4",
    "app_closing": "job.choice.batch_reasons.5",
    "error": "job.choice.batch_reasons.6",
    "not_finished": "job.choice.batch_reasons.7",
})
# The facts a check compares with the version before, in the pages' words; "text" is only that the text changed.
CHANGE_FIELDS = MessageMap({
    "title": "job.choice.change_fields.1",
    "company": "job.choice.change_fields.2",
    "cities": "job.choice.change_fields.3",
    "places_stated": "job.choice.change_fields.4",
    "job_type": "job.choice.change_fields.5",
    "type_stated": "job.choice.change_fields.6",
    "posted_on": "job.choice.change_fields.7",
    "deadline": "job.choice.change_fields.8",
    "url": "job.choice.change_fields.9",
    "apply_url": "job.choice.change_fields.10",
    "text": "job.choice.change_fields.11",
})


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

        return t("job.choice.74", p0=evidence.get('deadline')) + (t("job.choice.75", p0=given) if given else "")

    if kind == "delisted":
        return t("job.choice.76", p0=evidence.get('listing_count'))

    if kind == "synthetic":
        return t("job.choice.77")

    return t("job.choice.78")


def check_words(check) -> str:
    """What one check of either kind found, as the record says it now."""
    reason = getattr(check, "reason", None)
    evidence = getattr(check, "evidence", None)

    if check.status == CHECK_OPEN:
        return CHECK_WORDS[CHECK_OPEN]

    if check.status == CHECK_CLOSED:
        return t("job.choice.79", p0=evidence_text(evidence)) if evidence else CHECK_CLOSED_THEN

    words = CHECK_WORDS[CHECK_UNANSWERED] if check.status == CHECK_UNANSWERED else CHECK_WORDS[CHECK_UNKNOWN]

    return words + (t("job.choice.75", p0=RECHECK_REASONS.get(reason, reason)) if reason else "")


# The status a list filters by (owner review, plan 5.3): where a job's source left it, short, apart from the user's own
# decision and from its analysis. A job the user pasted is checked by nothing.
STATUS_WORDS = MessageMap({
    "open": "jobx.status.open",
    "closed": "jobx.status.closed",
    "unconfirmed": "jobx.status.unconfirmed",
    "unchecked": "jobx.status.unchecked",
})


def status_key(summary: CheckSummary, pasted: bool = False) -> str:
    """One of STATUS_WORDS for a job: its latest check a success (still published, or closed with its evidence kept),
    anything else to be confirmed; a pasted job is unchecked."""
    if pasted:
        return "unchecked"

    latest = summary.attempted

    if latest is None or not is_success(latest):
        return "unconfirmed"

    return "open" if latest.status == CHECK_OPEN else "closed"


def summary_text(summary: CheckSummary, when: Callable) -> str:
    """The posting's status now, in one line: a success as it is; anything else to be confirmed, with the last success
    and, when it says more, the last answer."""
    latest = summary.attempted

    if latest is None:
        return t("job.choice.67")

    if is_success(latest):
        return t("job.choice.70", p0=check_words(latest), p1=when(latest.checked_at))

    if latest.status == CHECK_UNANSWERED:
        text = t("job.choice.68", p0=when(latest.checked_at))
    elif latest.status == CHECK_CLOSED:
        text = t("job.choice.80", p0=when(latest.checked_at))
    else:
        text = t("job.choice.81", p0=when(latest.checked_at))

    reason = getattr(latest, "reason", None)

    if reason:
        text += t("job.choice.75", p0=RECHECK_REASONS.get(reason, reason))

    if summary.succeeded is not None:
        text += t("job.choice.82", p0=when(summary.succeeded.checked_at), p1=check_words(summary.succeeded))
    else:
        text += t("job.choice.83")

    answered = summary.answered

    if answered is not None and answered is not latest and answered is not summary.succeeded:
        text += t("job.choice.69", p0=when(answered.checked_at), p1=check_words(answered))

    return text


def history_line(check, when: Callable) -> str:
    """One check of either kind, as the record lists it."""
    if check.status == CHECK_UNANSWERED:
        reason = getattr(check, "reason", None)
        why = t("job.choice.75", p0=RECHECK_REASONS.get(reason, reason)) if reason else ""

        return t("job.choice.84", p0=when(check.checked_at), p1=why)

    return t("job.choice.66", p0=when(check.checked_at), p1=check_words(check))


def change_lines(changes) -> list[str]:
    """What a check found changed since the version before, never the text's words."""
    lines = []

    for change in changes or ():
        name = CHANGE_FIELDS.get(change.get("field"), change.get("field"))

        if change.get("field") == "text":
            lines.append(t("job.choice.85", p0=name))
            continue

        lines.append(t("job.choice.86", p0=name, p1=_shown(change.get('before')), p2=_shown(change.get('after'))))

    return lines


def change_rows(changes) -> list[tuple[str, str, str]]:
    """What a check found changed, as rows of a table (owner review, image 14): the fact, its value before and now;
    of the text only that it changed, never its words."""
    rows = []

    for change in changes or ():
        name = CHANGE_FIELDS.get(change.get("field"), change.get("field"))

        if change.get("field") == "text":
            rows.append((name, t("jobx.text_before"), t("jobx.text_changed")))
            continue

        rows.append((name, _shown(change.get("before")), _shown(change.get("after"))))

    return rows


def deadline_text(facts: dict) -> str:
    """A check's deadline as its page says it (F1): the one it settled on and which answer gave it, or both answers
    when they differed; never a date with nothing said of where it came from."""
    answers = facts.get("deadline_answers") or {}
    stated, checked = answers.get("read"), answers.get("check")

    if stated and checked and stated != checked:
        return t("job.choice.87", p0=stated, p1=checked)

    deadline = facts.get("deadline")

    if deadline is None:
        return t("job.choice.88")

    if checked and not stated:
        return t("job.choice.89", p0=deadline)

    return t("job.choice.90", p0=deadline)


def batch_lines(run, counts, when: Callable) -> list[str]:
    """A batch of checks as the records page says it (U3-D4b-1): what it actually did, never what is due now."""
    ended = t("job.choice.91", p0=when(run.finished_at)) if run.finished_at else t("job.choice.92")
    lines = [t("job.choice.93", p0=TRIGGER_WORDS.get(run.trigger, run.trigger), p1=when(run.started_at), p2=ended)]

    if run.outcome is None:
        lines.append(t("job.choice.94"))
    else:
        why = [BATCH_REASONS.get(code, code) for code in (run.reason or "").split(",") if code]
        lines.append(BATCH_OUTCOMES.get(run.outcome, run.outcome) + (t("job.choice.95", p0=list_join(why, "clauses")) if why else ""))

    lines.append(t("job.choice.96", p0=run.calls))

    if counts is None:
        lines.append(t("job.choice.97"))
    else:
        lines.append(
            t("job.choice.98", p0=counts.due, p1=counts.selected, p2=counts.succeeded, p3=counts.failed, p4=counts.discarded, p5=counts.skipped, p6=counts.unreached)
        )

    return lines


def _shown(value) -> str:
    if value is None or value == [] or value == "":
        return t("job.choice.99")

    if isinstance(value, dict):
        # A deadline two answers of one check gave differently (F1).
        return t("job.choice.100", p0=_shown(value.get('read')), p1=_shown(value.get('check')))

    if isinstance(value, list):
        return list_join(str(item) for item in value)

    return str(value)


DECISION_LABELS = MessageMap({
    JobDecision.FAVORITE: "job.choice.decision_labels.1",
    JobDecision.DEFER: "job.choice.decision_labels.2",
    JobDecision.SKIP: "job.choice.decision_labels.3",
    # The user's own decision (U3-D4a), never that anything was checked, sent or applied for.
    JobDecision.APPLY: "job.choice.decision_labels.4",
})
# Why, as fixed codes: the user picks any, or none.
REASONS = {
    JobDecision.FAVORITE: (
        ("direction", lazy("job.choice.102")),
        ("reachable", lazy("job.choice.103")),
        ("company", lazy("job.choice.104")),
        ("other", lazy("job.choice.105")),
    ),
    JobDecision.DEFER: (
        ("build_up", lazy("job.choice.106")),
        ("more_info", lazy("job.choice.107")),
        ("timing", lazy("job.choice.108")),
        ("other", lazy("job.choice.105")),
    ),
    JobDecision.SKIP: (
        ("type", lazy("job.choice.109")),
        ("place", lazy("job.choice.110")),
        ("too_demanding", lazy("job.choice.111")),
        ("direction", lazy("job.choice.112")),
        ("company", lazy("job.choice.113")),
        ("handled", lazy("job.choice.114")),
        ("other", lazy("job.choice.105")),
    ),
    # No fixed reasons were decided for it (U3-D4a): a note only.
    JobDecision.APPLY: (),
}
# Which fact is not right, and how (D3a: a posting's facts only; an analysis is D3b's).
FEEDBACK_FIELDS = (
    ("company", lazy("job.choice.change_fields.2")),
    ("title", lazy("job.choice.change_fields.1")),
    ("place", lazy("job.choice.change_fields.3")),
    ("job_type", lazy("job.choice.change_fields.5")),
    ("dates", lazy("job.choice.115")),
    ("check", lazy("job.choice.116")),
    ("link", lazy("job.choice.117")),
)
FEEDBACK_REASONS = (
    ("wrong", lazy("job.choice.118")),
    ("missing", lazy("job.choice.119")),
    ("wording", lazy("job.choice.120")),
    ("other", lazy("job.choice.105")),
)
EVIDENCE_WORDS = MessageMap({"official": "job.choice.evidence_words.1", "lead": "job.choice.evidence_words.2", "user_provided": "job.choice.evidence_words.3"})


def decision_label(decision: str | None) -> str:
    return t("job.choice.121") if decision is None else DECISION_LABELS[JobDecision(decision)]


def reason_labels(decision: str | None, codes) -> list[str]:
    if decision is None:
        return []

    words = dict(REASONS[JobDecision(decision)])

    return [str(words.get(code, code)) for code in codes]


def feedback_label(field: str, reason: str) -> str:
    return t("job.choice.122", p0=dict(FEEDBACK_FIELDS).get(field, field), p1=dict(FEEDBACK_REASONS).get(reason, reason))


# --- kept analyses (U3-D3b) -----------------------------------------------------------------------------------------
# What feedback on a kept analysis can point at, and why. The part pointed at is one the analysis has: its category, a
# requirement (by its id), an experience quote (by its requirement and place), a preference compared (by its id).
ANALYSIS_TARGETS = (
    ("category", lazy("job.choice.123")),
    ("requirement", lazy("job.choice.124")),
    ("citation", lazy("job.choice.125")),
    ("preference", lazy("job.choice.126")),
)
ANALYSIS_REASONS = (
    ("wrong", lazy("job.choice.127")),
    ("missing", lazy("job.choice.119")),
    ("wording", lazy("job.choice.120")),
    ("other", lazy("job.choice.105")),
)
# How what a kept analysis rests on stands now (app.library.references.SourceState), as pages and exports say it.
BASIS_WORDS = MessageMap({
    "current": "job.choice.basis_words.1",
    "changed": "job.choice.basis_words.2",
    "version_in_bin": "job.choice.basis_words.3",
    "record_in_bin": "job.choice.basis_words.4",
    "gone": "job.choice.basis_words.5",
})


def analysis_feedback_label(target: str, reason: str) -> str:
    return t("job.choice.122", p0=dict(ANALYSIS_TARGETS).get(target, target), p1=dict(ANALYSIS_REASONS).get(reason, reason))


def basis_text(state) -> str:
    return BASIS_WORDS.get(getattr(state, "value", state), str(state))


# A kept analysis's category, as the search's result page names it (app.discovery.contracts.CATEGORY_LABELS; a test
# keeps the two the same): the library loads nothing of a search to say it.
CATEGORY_WORDS = MessageMap({
    "strong": "job.choice.category_words.1",
    "partial": "job.choice.category_words.2",
    "conflict": "job.choice.category_words.3",
})
