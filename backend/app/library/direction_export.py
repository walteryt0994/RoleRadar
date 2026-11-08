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
from app.library.presentation import format_local
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

SOURCE_STATE_LABELS = {
    SourceState.CURRENT: "依据的资料和当时一致",
    SourceState.CHANGED: "依据的资料之后改过",
    SourceState.VERSION_IN_BIN: "依据的版本已删除（还能恢复）",
    SourceState.RECORD_IN_BIN: "依据的资料已删除（还能恢复）",
    SourceState.GONE: "依据的资料已永久删除",
}
BASIS_STATE_LABELS = {
    **{BasisState(state.value): label for state, label in SOURCE_STATE_LABELS.items()},
    BasisState.PERSONAL: "你自己的选择",
    BasisState.EXPLORATION_IN_BIN: "依据的方向探索已删除（还能恢复）",
    BasisState.EXPLORATION_GONE: "依据的方向探索已永久删除",
}
BASIS_LABELS = {
    DecisionBasis.SAVED_EXPLORATION: "根据已保存的方向探索",
    DecisionBasis.UNSAVED_EXPLORATION: "根据没有保存的方向探索（没有保留完整分析）",
    DecisionBasis.PERSONAL: "你自己的选择",
}
DECISION_STATE_LABELS = {"keep": "保留", "defer": "暂缓", "exclude": "排除"}
RELATION_LABELS = {"fits": "符合", "may_conflict": "可能冲突", "unknown": "无法判断"}
SOURCE_KIND_LABELS = {"experience": "经历", "preferences": "求职偏好"}
IN_USE, IN_BIN = "in_use", "recycle_bin"
# Field names are the same words wherever they appear.
FIELD_LABELS = {spec.name: spec.label for kind in ALL_KINDS for spec in CATEGORY_FIELDS[kind]}


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
        "# 方向探索与方向决定",
        "",
        f"- 导出时间：{format_local(now)}",
        "- 方向探索是当时生成、由你保存的结果，保存后不会改写；“依据状态”说明它依据的资料现在怎样。",
        "- 主要工作、差距等是一般性判断，没有经过岗位来源核验；没有经历依据不代表不适合。",
        "",
    ]

    for kept_as, heading in ((IN_USE, "方向决定"), (IN_BIN, "回收站里的方向决定")):
        entries = [entry for entry in content.decisions if entry["kept_as"] == kept_as]

        if entries:
            lines += [f"## {heading}", ""]
            lines += [_decision_line(entry) for entry in entries]
            lines.append("")

    for kept_as, heading in ((IN_USE, "已保存的方向探索"), (IN_BIN, "回收站里的方向探索")):
        entries = [entry for entry in content.explorations if entry["kept_as"] == kept_as]

        if entries:
            lines += [f"## {heading}", ""]

            for number, entry in enumerate(entries, start=1):
                lines += _exploration_lines(number, entry)

    return "\n".join(lines)


def _decision_line(entry: dict) -> str:
    line = f"- {entry['name']}：{entry['state_label']}（{entry['basis_label']}；{entry['basis_state_label']}）"

    if entry["kept_as"] == IN_BIN:
        line += f"，删除于 {_local(entry['deleted_at'])}，{_local(entry['restore_deadline'])} 前可以恢复"

    return line + "。"


def _exploration_lines(number: int, entry: dict) -> list[str]:
    generation = entry["generation"]
    lines = [
        f"### {number}. 保存于 {_local(entry['saved_at'])}（{entry['basis_state_label']}）",
        "",
        f"- 生成：{generation.get('provider', '')} {generation.get('model', '')}，"
        f"提示词版本 {generation.get('prompt_version', '')}，处理说明 {generation.get('notice_version', '')}，"
        f"生成于 {_local(generation.get('generated_at'))}",
    ]

    if entry["kept_as"] == IN_BIN:
        lines.append(f"- 删除于 {_local(entry['deleted_at'])}，{_local(entry['restore_deadline'])} 前可以恢复")

    for source in entry["sources"]:
        fields = "、".join(FIELD_LABELS.get(name, name) for name in source["fields"])
        lines.append(f"- 依据：{SOURCE_KIND_LABELS.get(source['kind'], source['kind'])}（{fields}）——{source['state_label']}")

    lines.append("")

    for index, direction in enumerate(entry["directions"], start=1):
        lines += [f"#### 方向 {index}：{direction.get('name', '')}", "", f"- 主要工作（一般性介绍）：{direction.get('main_work', '')}"]
        lines += [f"- 经历依据：「{quote.get('text', '')}」（{FIELD_LABELS.get(quote.get('field'), quote.get('field'))}）" for quote in direction.get("evidence", [])]

        if not direction.get("evidence"):
            lines.append("- 经历依据：暂无（不代表不适合）")

        for link in direction.get("ideal", []):
            condition = link.get("condition", {})
            basis = f"；方向一侧的依据：{link['basis']}" if link.get("basis") else ""
            lines.append(
                f"- 和你写的条件「{condition.get('text', '')}」：{RELATION_LABELS.get(link.get('relation'), link.get('relation'))}{basis}"
            )

        lines += [f"- 差距（一般性判断）：{gap}" for gap in direction.get("gaps", [])]
        lines += [f"- 还不能判断：{unknown}" for unknown in direction.get("unknowns", [])]
        lines.append(f"- 和其他方向的区别：{direction.get('difference', '')}")
        lines += [f"- 下一步可以：{step}" for step in direction.get("next_steps", [])]
        lines.append("")

    return lines


def _iso(value: datetime | None) -> str | None:
    """As the rest of the export writes times: UTC, to the second."""
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value: str | None) -> str:
    return "" if not value else format_local(datetime.fromisoformat(value))
