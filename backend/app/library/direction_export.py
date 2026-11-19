"""The direction records in an "export all" package (U3-C1).

Saved direction explorations and direction decisions in use, and those in the recycle bin still
restorable, each with how what it rests on stands now (app.library.references). Explorations are exported
as they were saved: the quotes in them are what the user chose to keep, and the package says when what
they rest on has changed or been deleted since. Content past its restore deadline, or purged, is never
exported. collect_directions reads everything in the export's own transaction; the rest only formats.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import ALL_KINDS, CATEGORY_FIELDS
from app.library.models import DecisionBasis, DirectionDecision, DirectionExploration, PurgeJob, SourceHolder
from app.library.presentation import format_local, label_of
from app.library.references import (
    BasisState,
    SourceState,
    decision_state,
    exploration_state,
    source_state,
    sources_of,
    usable_for_search,
)
from app.library.service import is_expired
from app.i18n import MessageMap, t

SOURCE_STATE_LABELS = MessageMap({
    SourceState.CURRENT: "dir.exp_md.1",
    SourceState.CHANGED: "dir.exp_md.2",
    SourceState.VERSION_IN_BIN: "dir.exp_md.3",
    SourceState.RECORD_IN_BIN: "dir.exp_md.4",
    SourceState.GONE: "dir.exp_md.5",
})
# Looked up when read, like the rest: built from the message keys, never from words taken at import (UI R4), so an
# English page or package says how a decision's basis stands in English.
BASIS_STATE_LABELS = MessageMap({
    **{BasisState(state.value): key for state, key in SOURCE_STATE_LABELS.keys_map().items()},
    BasisState.PERSONAL: "dir.exp_md.6",
    BasisState.EXPLORATION_IN_BIN: "dir.exp_md.7",
    BasisState.EXPLORATION_GONE: "dir.exp_md.8",
})
BASIS_LABELS = MessageMap({
    DecisionBasis.SAVED_EXPLORATION: "dir.exp_md.9",
    DecisionBasis.UNSAVED_EXPLORATION: "dir.exp_md.10",
    DecisionBasis.PERSONAL: "dir.exp_md.6",
})
DECISION_STATE_LABELS = MessageMap({"keep": "dir.exp_md.11", "defer": "dir.exp_md.12", "exclude": "dir.exp_md.13"})
RELATION_LABELS = MessageMap({"fits": "dir.exp_md.14", "may_conflict": "dir.exp_md.15", "unknown": "dir.exp_md.16"})
SOURCE_KIND_LABELS = MessageMap({"experience": "dir.exp_md.17", "preferences": "dir.exp_md.18"})
IN_USE, IN_BIN = "in_use", "recycle_bin"
# A field is named by one kind's words, the last kind that has it, as the package always named it; the words are
# looked up when the package is written (UI R4), in its language, never taken at import.
FIELD_KINDS = {spec.name: kind for kind in ALL_KINDS for spec in CATEGORY_FIELDS[kind]}


def field_words(name: str | None) -> str | None:
    """A source field's name in the package's language; a name that is not one of the library's stays as it is."""
    kind = FIELD_KINDS.get(name)

    return label_of(kind, name) if kind else name


@dataclass
class DirectionExport:
    explorations: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "direction_explorations": sum(entry["kept_as"] == IN_USE for entry in self.explorations),
            "direction_decisions": sum(entry["kept_as"] == IN_USE for entry in self.decisions),
            "direction_recycle_bin": sum(entry["kept_as"] == IN_BIN for entry in self.explorations + self.decisions),
        }


def collect_directions(db: Session, owner_id: str, now: datetime) -> DirectionExport:
    explorations = [
        row
        for row in db.scalars(
            select(DirectionExploration)
            .where(DirectionExploration.owner_id == owner_id)
            .order_by(DirectionExploration.saved_at, DirectionExploration.id)
        )
        if not is_expired(row, now)
    ]
    decisions = [
        row
        for row in db.scalars(
            select(DirectionDecision)
            .where(DirectionDecision.owner_id == owner_id)
            .order_by(DirectionDecision.created_at, DirectionDecision.id)
        )
        if not is_expired(row, now)
    ]

    return DirectionExport(
        explorations=[_exploration_data(db, owner_id, row, now) for row in explorations],
        decisions=[_decision_data(db, owner_id, row, now) for row in decisions],
    )


def _kept(db: Session, row) -> dict:
    data = {
        "kept_as": IN_USE if row.deleted_at is None else IN_BIN,
        "deleted_at": _iso(row.deleted_at),
        "restore_deadline": _iso(row.purge_after),
    }

    if row.deleted_at is not None:
        job = db.scalar(select(PurgeJob).where(PurgeJob.deletion_id == row.deletion_id))
        # Deleted along with a record or a version: it comes back only with that one.
        data["deleted_along"] = job is None or job.target_id != row.id

    return data


def _sources_data(db: Session, owner_id: str, holder_type: str, holder_id: str, now: datetime) -> list[dict]:
    entries = []

    for row in sources_of(db, owner_id, holder_type, holder_id):
        state = source_state(db, owner_id, row, now)
        entries.append(
            {
                "kind": row.kind,
                "item_id": row.item_id,
                "version_id": row.version_id,
                "fields": list(row.fields or []),
                "state": state.value,
                "state_label": SOURCE_STATE_LABELS[state],
            }
        )

    return entries


def _exploration_data(db: Session, owner_id: str, row: DirectionExploration, now: datetime) -> dict:
    state = exploration_state(db, owner_id, row, now)

    return {
        "id": row.id,
        "saved_at": _iso(row.saved_at),
        **_kept(db, row),
        "basis_state": state.value,
        "basis_state_label": SOURCE_STATE_LABELS[state],
        "generation": dict(row.generation or {}),
        "sources": _sources_data(db, owner_id, SourceHolder.EXPLORATION, row.id, now),
        "directions": list((row.content or {}).get("directions", [])),
    }


def _decision_data(db: Session, owner_id: str, row: DirectionDecision, now: datetime) -> dict:
    state = decision_state(db, owner_id, row, now)

    return {
        "id": row.id,
        "name": row.name,
        "state": row.state,
        "state_label": DECISION_STATE_LABELS.get(row.state, row.state),
        "basis": row.basis,
        "basis_label": BASIS_LABELS.get(row.basis, row.basis),
        "exploration_id": row.exploration_id,
        "basis_state": state.value,
        "basis_state_label": BASIS_STATE_LABELS[state],
        "usable_for_search": usable_for_search(db, owner_id, row, now),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        **_kept(db, row),
        "sources": _sources_data(db, owner_id, SourceHolder.DECISION, row.id, now),
    }


def directions_markdown(content: DirectionExport, now: datetime) -> str:
    """The readable copy: decisions first, then each saved exploration as it was saved."""
    lines = [
        t("dir.exp_md.19"),
        "",
        t("dir.exp_md.20", p0=format_local(now)),
        t("dir.exp_md.21"),
        t("dir.exp_md.22"),
        "",
    ]

    for kept_as, heading in ((IN_USE, t("dir.exp_md.23")), (IN_BIN, t("dir.exp_md.24"))):
        entries = [entry for entry in content.decisions if entry["kept_as"] == kept_as]

        if entries:
            lines += [f"## {heading}", ""]
            lines += [_decision_line(entry) for entry in entries]
            lines.append("")

    for kept_as, heading in ((IN_USE, t("dir.exp_md.25")), (IN_BIN, t("dir.exp_md.26"))):
        entries = [entry for entry in content.explorations if entry["kept_as"] == kept_as]

        if entries:
            lines += [f"## {heading}", ""]

            for number, entry in enumerate(entries, start=1):
                lines += _exploration_lines(number, entry)

    return "\n".join(lines)


def _decision_line(entry: dict) -> str:
    line = t("dir.exp_md.27", p0=entry['name'], p1=entry['state_label'], p2=entry['basis_label'], p3=entry['basis_state_label'])

    if entry["kept_as"] == IN_BIN:
        line += t("dir.exp_md.28", p0=_local(entry['deleted_at']), p1=_local(entry['restore_deadline']))

    return line + t("dir.exp_md.29")


def _exploration_lines(number: int, entry: dict) -> list[str]:
    generation = entry["generation"]
    lines = [
        t("dir.exp_md.30", p0=number, p1=_local(entry['saved_at']), p2=entry['basis_state_label']),
        "",
        t("dir.exp_md.31", p0=generation.get('provider', ''), p1=generation.get('model', ''), p2=generation.get('prompt_version', ''), p3=generation.get('notice_version', ''), p4=_local(generation.get('generated_at'))),
    ]

    if entry["kept_as"] == IN_BIN:
        lines.append(t("dir.exp_md.32", p0=_local(entry['deleted_at']), p1=_local(entry['restore_deadline'])))

    for source in entry["sources"]:
        fields = t("dir.exp_md.33").join(field_words(name) for name in source["fields"])
        lines.append(t("dir.exp_md.34", p0=SOURCE_KIND_LABELS.get(source['kind'], source['kind']), p1=fields, p2=source['state_label']))

    lines.append("")

    for index, direction in enumerate(entry["directions"], start=1):
        lines += [t("dir.exp_md.35", p0=index, p1=direction.get('name', '')), "", t("dir.exp_md.36", p0=direction.get('main_work', ''))]
        lines += [t("dir.exp_md.37", p0=quote.get('text', ''), p1=field_words(quote.get("field"))) for quote in direction.get("evidence", [])]

        if not direction.get("evidence"):
            lines.append(t("dir.exp_md.38"))

        for link in direction.get("ideal", []):
            condition = link.get("condition", {})
            basis = t("dir.exp_md.39", p0=link['basis']) if link.get("basis") else ""
            lines.append(
                t("dir.exp_md.40", p0=condition.get('text', ''), p1=RELATION_LABELS.get(link.get('relation'), link.get('relation')), p2=basis)
            )

        lines += [t("dir.exp_md.41", p0=gap) for gap in direction.get("gaps", [])]
        lines += [t("dir.exp_md.42", p0=unknown) for unknown in direction.get("unknowns", [])]
        lines.append(t("dir.exp_md.43", p0=direction.get('difference', '')))
        lines += [t("dir.exp_md.44", p0=step) for step in direction.get("next_steps", [])]
        lines.append("")

    return lines


def _iso(value: datetime | None) -> str | None:
    """As the rest of the export writes times: UTC, to the second."""
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value: str | None) -> str:
    return "" if not value else format_local(datetime.fromisoformat(value))
