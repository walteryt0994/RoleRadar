"""What one analysis task may use and send, worked out on the server (U2-A2).

The input page offers the user's confirmed experiences, each in its current
confirmed version, a field for one job posting and a switch for the saved
job preferences. Nothing the browser sends is taken on trust: the chosen
versions and the preferences are read again here.

A ScopePlan is what the review page shows, field by field: what goes out,
what the user left out and what app.outbound_review stops. build_request
turns exactly that into the analysis request: the fields shown as going
out, the posting as shown (its contact details masked) and the preferences
shown. Its fingerprint covers all of it, so any change needs a new review.
"""
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.analysis_contracts import AnalysisRequest, Recipient, SelectedPreference, SelectedSource
from app.experience_analysis import PREFERENCE_TOPICS
from app.library.content import CATEGORY_FIELDS, display_title
from app.library.presentation import field_label
from app.library.projection import ConfirmedExperience, current_preferences, read_selected_confirmations
from app.outbound_review import ADDRESS, CONFIDENTIAL, kinds, masked_posting, review
from app.i18n import list_join, t

# Limits of one task, said on the input page next to what they limit.
MAX_POSTING_CHARS = 8000
MAX_EXPERIENCES = 10
# Experience fields, preferences and the posting together.
MAX_OUTBOUND_CHARS = 24000

SEND = "send"
LEFT_OUT = "left_out"
BLOCKED = "blocked"
PREFERENCES_KEY = "preferences"


class InputProblem(Exception):
    """Input the review cannot be built from; the page shows it with the input kept."""

    def __init__(self, message: str, where: str):
        super().__init__(message)
        self.message = message
        # "sources" or "posting": the part of the input page it belongs to.
        self.where = where


@dataclass(frozen=True)
class TaskInputs:
    """What the user entered, kept with a task so the input page can show it again."""

    # (experience id, confirmed version id), as the page offered them, in the order chosen.
    chosen: tuple[tuple[str, str], ...]
    posting: str
    use_preferences: bool
    # "<experience id>:<field>" or "preferences:<field>" the user chose to leave out.
    left_out: frozenset[str] = field(default_factory=frozenset)
    # The language the results are asked for (UI bilingual batch, plan 6.3).
    result_language: str = "zh-CN"
    # Where a filled-in posting came from (plan 5.3): "record:<id>" or "supplied:<token>", a reference the server
    # verifies at the review (the user's own, still there, the very text), never trusted as said.
    origin: str = ""


@dataclass(frozen=True)
class FieldPlan:
    name: str
    label: str
    text: str
    status: str
    # What app.outbound_review found, when the field is blocked.
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourcePlan:
    entry: ConfirmedExperience
    title: str
    fields: tuple[FieldPlan, ...]

    @property
    def sent(self) -> tuple[str, ...]:
        return tuple(plan.name for plan in self.fields if plan.status == SEND)


@dataclass(frozen=True)
class PreferencePlan:
    field: str
    topic: str
    # 理想 / 也可以接受 / 不能接受, or None for the two single preferences.
    stance: str | None
    text: str
    status: str
    blocked: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopePlan:
    sources: tuple[SourcePlan, ...]
    # The posting as it goes out.
    posting: str
    # Kinds of contact details masked in it.
    posting_hidden: tuple[str, ...]
    use_preferences: bool
    preferences: tuple[PreferencePlan, ...]
    outbound_chars: int

    @property
    def sent_preferences(self) -> tuple[PreferencePlan, ...]:
        return tuple(plan for plan in self.preferences if plan.status == SEND)


def clean_posting(raw: str) -> str:
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def build_plan(db: Session, owner_id: str, inputs: TaskInputs) -> ScopePlan:
    """Read the chosen material again and decide, field by field, what may go out."""
    chosen = inputs.chosen

    if not chosen:
        raise InputProblem(t("job.scope.1"), "sources")

    if len(chosen) > MAX_EXPERIENCES:
        raise InputProblem(t("job.scope.2", p0=MAX_EXPERIENCES, p1=len(chosen)), "sources")

    if len({item_id for item_id, _ in chosen}) != len(chosen):
        raise InputProblem(t("job.scope.3"), "sources")

    posting = clean_posting(inputs.posting)

    if not posting:
        raise InputProblem(t("job.scope.4"), "posting")

    if len(posting) > MAX_POSTING_CHARS:
        raise InputProblem(
            t("job.scope.5", p0=MAX_POSTING_CHARS, p1=len(posting)),
            "posting",
        )

    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in posting):
        raise InputProblem(t("job.scope.6"), "posting")

    masked, hidden, stopping = masked_posting(posting)

    if stopping:
        reasons = {
            ADDRESS: t("job.scope.7"),
            CONFIDENTIAL: t("job.scope.8"),
        }

        raise InputProblem(
            t("job.scope.9") + list_join((reasons[kind] for kind in stopping), "clauses_also")
            + t("job.scope.11"),
            "posting",
        )

    entries, problems = read_selected_confirmations(db, owner_id, dict(chosen))

    if problems:
        raise InputProblem(
            t("job.scope.12", p0=len(problems)),
            "sources",
        )

    sources = tuple(source_plan(entry, inputs.left_out) for entry in entries)

    for source in sources:
        if not source.sent:
            raise InputProblem(
                t("job.scope.13", p0=source.title),
                "sources",
            )

    preferences = preference_plans(db, owner_id, inputs.left_out) if inputs.use_preferences else ()
    outbound = len(masked) + sum(
        len(plan.text) for source in sources for plan in source.fields if plan.status == SEND
    ) + sum(len(plan.text) for plan in preferences if plan.status == SEND)

    if outbound > MAX_OUTBOUND_CHARS:
        raise InputProblem(
            t("job.scope.14", p0=outbound, p1=MAX_OUTBOUND_CHARS),
            "sources",
        )

    return ScopePlan(
        sources=sources,
        posting=masked,
        posting_hidden=hidden,
        use_preferences=inputs.use_preferences,
        preferences=preferences,
        outbound_chars=outbound,
    )


def _status(key: str, text: str, left_out: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    found = kinds(review(text))

    if found:
        return BLOCKED, found

    return (LEFT_OUT if key in left_out else SEND), ()


def source_plan(entry: ConfirmedExperience, left_out: frozenset[str]) -> SourcePlan:
    """One confirmed experience, field by field: sent, left out by the user, or stopped by app.outbound_review.

    Also what a search for postings shows of the experiences each of its analyses sends (app.discovery.web).
    """
    fields = []

    for definition in CATEGORY_FIELDS[entry.category]:
        text = entry.content.get(definition.name)

        if text:
            status, blocked = _status(f"{entry.item_id}:{definition.name}", text, left_out)
            fields.append(FieldPlan(definition.name, field_label(entry.category, definition.name), text, status, blocked))

    return SourcePlan(entry=entry, title=display_title(entry.content, entry.category), fields=tuple(fields))


def preference_plans(db: Session, owner_id: str, left_out: frozenset[str]) -> tuple[PreferencePlan, ...]:
    """The saved preferences, each sent, left out by the user, or stopped by app.outbound_review."""
    saved = current_preferences(db, owner_id)
    plans = []

    for name, (topic, _, stance, _) in PREFERENCE_TOPICS.items():
        text = saved.get(name)

        if text:
            status, blocked = _status(f"{PREFERENCES_KEY}:{name}", text, left_out)
            plans.append(PreferencePlan(name, topic, stance, text, status, blocked))

    return tuple(plans)


def build_request(
    owner_id: str, plan: ScopePlan, recipient: Recipient, notice_version: str, result_language: str = "zh-CN"
) -> AnalysisRequest:
    """The analysis request of exactly what the review shows as going out."""
    return AnalysisRequest(
        owner_id=owner_id,
        selection=tuple(
            SelectedSource(source.entry.item_id, source.entry.version_id, source.sent) for source in plan.sources
        ),
        job_posting=plan.posting,
        recipient=recipient,
        preferences=tuple(SelectedPreference(item.field, item.text) for item in plan.sent_preferences),
        notice_version=notice_version,
        result_language=result_language if result_language in ("zh-CN", "en") else "zh-CN",
    )
