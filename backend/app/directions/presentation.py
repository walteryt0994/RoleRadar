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
    ExplorationStatus,
    Problem,
    conflict_label,
)
from app.directions.contracts import ExplorationResult, Relation, name_key
from app.library.direction_export import (
    BASIS_LABELS,
    BASIS_STATE_LABELS,
    DECISION_STATE_LABELS,
    RELATION_LABELS,
)
from app.library.models import DecisionState
from app.library.presentation import field_label, preference_parts
from app.library.references import BasisState
from app.i18n import MessageMap, lazy, t

STEP = lazy("dir.pres.1")
# The processing notice of NOTICE_VERSION "direction-1": the local simulation only.
NOTICE = (
    (lazy("dir.pres.2"), lazy("dir.pres.3")),
    (lazy("dir.pres.4"), lazy("dir.pres.5")),
    (lazy("dir.pres.6"), lazy("dir.pres.7")),
    (lazy("dir.pres.8"), lazy("dir.pres.9")),
)
# The processing notice of a real service (U3, the direction pages on the real service; Codex §28.2.1). Its first
# line, who receives the request, is the confirmed recipient's (notice_rows): the rest is the same for any model of
# the service it describes, and for that service only.
LIVE_NOTICE_VERSION = "direction-live-2"
LIVE_NOTICE_COVERS = {"openai": "https://api.openai.com/v1"}
LIVE_NOTICE = (
    (lazy("dir.pres.4"), lazy("dir.pres.10")),
    (
        lazy("dir.pres.11"),
        lazy("dir.pres.12"),
    ),
    (
        lazy("dir.pres.13"),
        lazy("dir.pres.14"),
    ),
    (lazy("dir.pres.15"), lazy("dir.pres.16")),
    (
        lazy("dir.pres.8"),
        lazy("dir.pres.17"),
    ),
)
PROVIDER_NAMES = {"openai": "OpenAI"}
# Said when a page is rendered, in its language: looked up at import, they would stay in the default language.
DECISIONS = tuple((state.value, lazy(DECISION_STATE_LABELS.key_of(state.value))) for state in DecisionState)
RELATIONS = (Relation.FITS, Relation.MAY_CONFLICT, Relation.UNKNOWN)
GROUP_NAMES = tuple(group for group, _ in PREFERENCE_GROUPS)
# The three groups as the pages name them, in the interface language; GROUP_NAMES stay the stable names the plan and
# the material use.
GROUP_LABELS = MessageMap({"行业兴趣与意愿": "dir.group.interest", "理想工作内容和方式": "dir.group.work", "现实条件": "dir.group.conditions"})


def group_text(group: str) -> str:
    return GROUP_LABELS.get(group, group)

REJECTED = MessageMap({
    Problem.SELECTION_INVALID: "dir.pres.18",
    Problem.SOURCES_CHANGED: "dir.pres.19",
    Problem.PREFERENCES_CHANGED: "dir.pres.20",
    Problem.PREFERENCE_VERSION_UNAVAILABLE: "dir.pres.21",
    Problem.OUTBOUND_BLOCKED: "dir.pres.22",
    Problem.OVER_LIMIT: "dir.pres.23",
    Problem.TOO_MANY_SOURCES: "dir.pres.24",
    Problem.APPROVAL_MISSING: "dir.pres.25",
    Problem.APPROVAL_MISMATCH: "dir.pres.25",
    Problem.SIMULATION_ONLY: "dir.pres.26",
    Problem.RECIPIENT_UNVERIFIED: "dir.pres.27",
    Problem.RECIPIENT_MISMATCH: "dir.pres.28",
    Problem.LIVE_ONLY: "dir.pres.29",
    # The real service's gate (app.directions.runner.GATE_PREFIX and the gate's reason): nothing of the request went out.
    "gate_stopped": "dir.pres.30",
    "gate_paused": "dir.pres.31",
    "gate_busy": "dir.pres.32",
    "gate_purpose": "dir.pres.33",
    "gate_requests": "dir.pres.34",
    "gate_cost": "dir.pres.35",
    "gate_closed": "dir.pres.36",
    "gate_ended": "dir.pres.37",
    "gate_time": "dir.pres.38",
    "gate_input": "dir.pres.39",
    "gate_payload": "dir.pres.40",
    "gate_count": "dir.pres.41",
    "gate_batch": "dir.pres.42",
})
REJECTED_OTHERWISE = lazy("dir.pres.43")
FAILED = MessageMap({
    "timeout": "dir.pres.44",
    "connection": "dir.pres.45",
    "rate_limit": "dir.pres.46",
    "provider_response": "dir.pres.47",
    "refusal": "dir.pres.48",
    "empty_response": "dir.pres.49",
    "invalid_json": "dir.pres.50",
    "schema": "dir.pres.50",
    "citation": "dir.pres.51",
    "recipient_mismatch": "dir.pres.52",
    "internal_error": "dir.pres.53",
    "provider_unavailable": "dir.pres.54",
    "provider_config": "dir.pres.55",
    "ledger_failed": "dir.pres.56",
})
# Why a confirmation does not start (app.directions.web.DirectionsRuntime.refusal): the gate's word, or "ledger".
REFUSALS = MessageMap({
    "stopped": "dir.pres.57",
    "paused": "dir.pres.58",
    "requests": "dir.pres.59",
    "ledger": "dir.pres.60",
    "batch": "dir.pres.61",
})
# Why an approval is paused (app.live_gate), in the words of the analysis pages, and what kind of trouble it is.
PAUSE_REASONS = MessageMap({
    "interrupted": "dir.pres.62",
    "unknown_usage": "dir.pres.63",
    "over_reservation": "dir.pres.64",
    "above_count": "dir.pres.65",
    "count_maybe": "dir.pres.66",
})
PAUSE_KINDS = MessageMap({
    "interrupted": "dir.pres.67",
    "unknown_usage": "dir.pres.67",
    "count_maybe": "dir.pres.67",
    "over_reservation": "dir.pres.68",
    "above_count": "dir.pres.69",
})
STALE = lazy("dir.pres.70")
CHANGED_SINCE = lazy("dir.pres.71")
WITHDRAWN = lazy("dir.pres.72")
UNAVAILABLE = lazy("dir.pres.73")
SAVED_GONE = lazy("dir.pres.74")
CANCELLED_BY = MessageMap({
    "user": "dir.pres.75",
    "logout": "dir.pres.76",
    "timeout": "dir.pres.77",
    "shutdown": "dir.pres.78",
})
NOTICES = MessageMap({
    "already": "dir.pres.79",
    "scope_updated": "dir.pres.80",
    "scope_changed": "dir.pres.81",
    "service_changed": "dir.pres.82",
    "saved": "dir.pres.83",
    "decided": "dir.pres.84",
    "undone": "dir.pres.85",
    "own": "dir.pres.86",
    "deleted": "dir.pres.87",
    "restored": "dir.pres.88",
    "purged": "dir.pres.89",
})


# --- which service, and what it costs ---------------------------------------------------------------------------------


def is_live(recipient) -> bool:
    return recipient.provider != SIMULATION_PROVIDER


def service_name(recipient) -> str:
    return t("dir.pres.90", p0=PROVIDER_NAMES.get(recipient.provider, recipient.provider), p1=recipient.model)


def input_mode(recipient) -> str:
    """What the entry page says of the service a new exploration would go to: the app's, as it was started."""
    if not is_live(recipient):
        return t("dir.pres.91")

    return t("dir.pres.92", p0=service_name(recipient))


def task_mode(recipient) -> str:
    """What a task's running page says of its service: the recipient the task was confirmed for."""
    if not is_live(recipient):
        return t("dir.pres.93")

    return t("dir.pres.94", p0=service_name(recipient))


def result_mode(recipient, outcome) -> str:
    """Whose result a finished task shows: a rule result is never passed off as a real service's, nor the reverse."""
    if not is_live(recipient):
        return t("dir.pres.93")

    if outcome is not None and outcome.result is not None:
        return t("dir.pres.95", p0=service_name(recipient))

    return t("dir.pres.96", p0=service_name(recipient))


def sends_note(recipient) -> str:
    """What follows the count of a task's requests: never offline or free for a real service."""
    if not is_live(recipient):
        return t("dir.pres.97")

    return t("dir.pres.98")


def notice_rows(request) -> tuple:
    """The processing notice of the version a task was reviewed under, its recipient first for a real service."""
    if request.notice_version != LIVE_NOTICE_VERSION:
        return NOTICE

    recipient = request.recipient

    return ((t("dir.pres.2"), t("dir.pres.99", p0=service_name(recipient), p1=recipient.endpoint)),) + LIVE_NOTICE


def pause_text(reason: str) -> str:
    """An approval paused by its ledger, said as the approval's state, never as one exploration's cost."""
    kind = PAUSE_KINDS.get(reason)
    head = t("dir.pres.100", p0=kind) if kind else t("dir.pres.101")

    return t("dir.pres.102", p0=head, p1=PAUSE_REASONS.get(reason, reason))


def usd(micro_usd: int) -> str:
    return f"{micro_usd / 1_000_000:.2f}"


def condition_label(field: str) -> str:
    """Which of the user's preferences a field is, in the page's language: its topic and stance, or its name alone
    for a single one. (The prompt names them in its own words, explore.STANCE_LABELS and the analysis topics, unchanged.)"""
    topic, stance = preference_parts(field)

    return t("dir.pres.103", p0=topic, p1=stance) if stance else topic


def unfilled_text(fields) -> str:
    """The preferences fields not filled in, by topic, in the page's language: 工作方式（理想、也可以接受）, or the name
    of a single one."""
    topics: dict[str, list[str]] = {}

    for field in fields:
        topic, stance = preference_parts(field)
        stances = topics.setdefault(topic, [])

        if stance:
            stances.append(stance)

    return t("dir.pres.104").join(t("dir.pres.105", p0=topic, p1=t("common.list_separator").join(stances)) if stances else topic for topic, stances in topics.items())


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

        # The experiences a direction quotes, each once, in the order quoted (the card's 相关经历).
        related = []

        for quote in direction.evidence:
            found = places.get((quote.item_id, quote.version_id))

            if found is not None and found[0] not in related:
                related.append(found[0])

        shown.append(
            {
                "anchor": f"direction-{index + 1}",
                "name": direction.name,
                "related": related,
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
        return t("dir.pres.106")

    title, category = found

    return f"{title} · {field_label(category, quote.field)}"


def decision_text(decision) -> str:
    return DECISION_STATE_LABELS[decision.state] if decision is not None else t("dir.pres.107")


def held_text(basis) -> str:
    """Why a kept direction may not be searched for now, and what would let it be (U3-C3b)."""
    if basis in (BasisState.VERSION_IN_BIN, BasisState.RECORD_IN_BIN, BasisState.EXPLORATION_IN_BIN):
        then = t("dir.pres.108")
    else:
        then = t("dir.pres.109")

    return t("dir.pres.110", p0=BASIS_STATE_LABELS[basis], p1=then)


def basis_text(decision, state) -> str:
    """What a decision rests on, and how that stands now."""
    if state is None or decision.basis == "personal":
        return BASIS_LABELS[decision.basis]

    return t("dir.pres.111", p0=BASIS_LABELS[decision.basis], p1=BASIS_STATE_LABELS[state])
