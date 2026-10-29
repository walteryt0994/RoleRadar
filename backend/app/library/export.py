"""Exports a user can take away and read without RoleRadar.

One experience exports as a Markdown file. "Export all" builds a ZIP with a
README, one Markdown file per experience, the same data as JSON and a
manifest with checksums. Exports include the private notes, so every file
reminds the reader to check them before sharing. Content past its restore
deadline, or permanently deleted, is never exported.
"""
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import (
    CATEGORY_LABELS,
    NOT_FILLED,
    PROJECT_FIELDS,
    display_title,
)
from app.library.models import ExperienceItem, ExperienceVersion, VersionKind
from app.library.presentation import format_local, state_label, version_label
from app.library.service import active_item, is_expired, item_versions, list_experiences

EXPORT_FORMAT = "roleradar-library-export"
EXPORT_FORMAT_VERSION = 1
SHARE_WARNING = (
    "注意：这份文件包含你的私有备注。导出文件离开 RoleRadar 后不再受平台的权限保护；"
    "分享或发送之前，请先检查并删掉不想让别人看到的内容。"
)
NOT_INCLUDED = (
    "已超过恢复期限或已永久删除的内容",
    "附件：经历库目前还没有附件功能",
    "分析结果、岗位简历和投递记录：这些功能还没有建立",
)


def single_export(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
) -> tuple[str, str]:
    item = active_item(db, owner_id, item_id)
    versions = item_versions(db, owner_id, item, now)

    return f"roleradar-experience-{item.id[:8]}.md", experience_markdown(item, versions, now)


def experience_markdown(
    item: ExperienceItem,
    versions: list[ExperienceVersion],
    now: datetime,
) -> str:
    last_confirmed = (
        format_local(item.last_confirmed_at) if item.last_confirmed_at else "尚未确认"
    )
    lines = [
        f"# {display_title(item.content)}",
        "",
        f"- 类别：{CATEGORY_LABELS.get(item.category, item.category)}",
        f"- 状态：{state_label(item)}",
        f"- 第一次保存：{format_local(item.created_at)}",
        f"- 最近一次保存：{format_local(item.updated_at)}",
        f"- 上次确认：{last_confirmed}",
        f"- 经历编号：{item.id}",
        f"- 导出时间：{format_local(now)}",
    ]

    if item.deleted_at is not None:
        lines.append(
            f"- 回收站：删除于 {format_local(item.deleted_at)}，"
            f"在 {format_local(item.purge_after)} 之前可以恢复，之后会被永久删除"
        )

    lines += ["", f"> {SHARE_WARNING}", "", "## 当前内容", ""]
    lines += _field_lines(item.content)
    lines += ["## 私有备注", "", item.private_note or NOT_FILLED, ""]
    lines += [f"## 历史版本（{len(versions)} 个）", ""]

    if not versions:
        lines += ["没有保留的历史版本。", ""]

    for version in versions:
        when = "第一次保存于" if version.kind == VersionKind.INITIAL else "确认于"
        lines += [f"### {version_label(version)} · {when} {format_local(version.created_at)}", ""]

        if version.deleted_at is not None:
            lines += [
                f"这个历史版本已单独删除，在 {format_local(version.purge_after)} 之前可以恢复。",
                "",
            ]

        lines += _field_lines(version.content)
        lines += ["**当时的私有备注**", "", version.private_note or NOT_FILLED, ""]

    lines += [
        "---",
        "",
        "导出范围：这条经历的当前内容和私有备注，以及仍保留或仍可恢复的历史版本；"
        "不含已超过恢复期限或已永久删除的内容。",
        "",
    ]

    return "\n".join(lines)


def _field_lines(content: dict) -> list[str]:
    lines = []

    for field in PROJECT_FIELDS:
        lines += [f"**{field.label}**", "", content.get(field.name) or NOT_FILLED, ""]

    return lines


def export_archive(
    db: Session,
    owner_id: str,
    owner_label: str,
    now: datetime,
) -> tuple[str, bytes]:
    """Everything this owner still keeps, as (file name, ZIP bytes)."""
    current = list_experiences(db, owner_id)
    binned = [
        item
        for item in db.scalars(
            select(ExperienceItem)
            .where(
                ExperienceItem.owner_id == owner_id,
                ExperienceItem.deleted_at.is_not(None),
            )
            .order_by(ExperienceItem.deleted_at.desc())
        )
        if not is_expired(item, now)
    ]
    files: dict[str, bytes] = {}
    listing = {"experiences": [], "recycle-bin": []}
    records = {"experiences": [], "recycle-bin": []}

    for section, items in (("experiences", current), ("recycle-bin", binned)):
        for index, item in enumerate(items, start=1):
            versions = item_versions(db, owner_id, item, now)
            path = f"{section}/{index:02d}-{item.id[:8]}.md"
            files[path] = experience_markdown(item, versions, now).encode("utf-8")
            listing[section].append((path, display_title(item.content), item))
            records[section].append(_item_record(item, versions))

    data = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": _iso(now),
        "owner": owner_label,
        "field_labels": {field.name: field.label for field in PROJECT_FIELDS},
        "experiences": records["experiences"],
        "recycle_bin": records["recycle-bin"],
    }
    files["data.json"] = _json_bytes(data)
    files["README.md"] = _readme(owner_label, now, listing).encode("utf-8")
    files["manifest.json"] = _json_bytes(
        {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "generated_at": _iso(now),
            "counts": {
                "experiences": len(current),
                "recycle_bin": len(binned),
                "attachments": 0,
            },
            "not_included": list(NOT_INCLUDED),
            "files": [
                {
                    "path": path,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for path, payload in sorted(files.items())
            ],
        }
    )

    folder = f"roleradar-export-{now.astimezone():%Y%m%d-%H%M%S}"
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in files.items():
            info = zipfile.ZipInfo(f"{folder}/{path}", date_time=now.astimezone().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            # Without permission bits some unzip tools create unreadable files.
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)

    return f"{folder}.zip", buffer.getvalue()


def _readme(owner_label: str, now: datetime, listing: dict) -> str:
    lines = [
        "# RoleRadar 经历库导出",
        "",
        f"- 导出时间：{format_local(now)}",
        f"- 导出账号：{owner_label}（本地开发模式的合成用户）",
        "",
        "## 包含什么",
        "",
        f"- `experiences/`：当前的经历，每条一个 Markdown 文件，共 {len(listing['experiences'])} 条。",
        f"- `recycle-bin/`：回收站里仍可恢复的经历，共 {len(listing['recycle-bin'])} 条；"
        "每个文件都写明了恢复截止时间。",
        "- `data.json`：同样的内容整理成结构化数据，方便以后导入其他工具。",
        "- `manifest.json`：文件清单和每个文件的 SHA-256 校验值。",
        "",
    ]

    for section, heading in (("experiences", "当前经历"), ("recycle-bin", "回收站")):
        if listing[section]:
            lines += [f"### {heading}", ""]
            lines += [
                f"- `{path}`：{title}（{state_label(item)}）"
                for path, title, item in listing[section]
            ]
            lines.append("")

    lines += ["## 不包含什么", ""]
    lines += [f"- {entry}" for entry in NOT_INCLUDED]
    lines += [
        "",
        "## 分享前请注意",
        "",
        f"- {SHARE_WARNING}",
        "- 这个导出包是一份副本：之后在 RoleRadar 里删除经历，不会删除已经下载到你电脑上的这份文件。",
        "",
        "## 怎么打开",
        "",
        "`.md` 文件是纯文本，可以用任何文本编辑器或 Markdown 阅读器打开；`.json` 文件也可以用文本编辑器查看。",
        "",
    ]

    return "\n".join(lines)


def _item_record(item: ExperienceItem, versions: list[ExperienceVersion]) -> dict:
    return {
        "id": item.id,
        "category": item.category,
        "category_label": CATEGORY_LABELS.get(item.category, item.category),
        "state": item.state,
        "state_label": state_label(item),
        "revision": item.revision,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "last_confirmed_at": _iso(item.last_confirmed_at),
        "deleted_at": _iso(item.deleted_at),
        "restore_deadline": _iso(item.purge_after),
        "content": item.content,
        "private_note": item.private_note,
        "versions": [
            {
                "id": version.id,
                "kind": version.kind,
                "label": version_label(version),
                "created_at": _iso(version.created_at),
                "deleted_at": _iso(version.deleted_at),
                "restore_deadline": _iso(version.purge_after),
                "content": version.content,
                "private_note": version.private_note,
            }
            for version in versions
        ],
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
