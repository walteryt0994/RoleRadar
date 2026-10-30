"""Exports a user can take away and read without RoleRadar.

One record exports as a Markdown file. "Export all" builds a ZIP with a
README, one Markdown file per experience, the basic profile and the job
preferences, the same data as JSON and a manifest with checksums. Exports
include private notes and contact details, so every file reminds the reader
to check them before sharing. Content past its restore deadline, or
permanently deleted, is never exported.
"""
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.content import (
    ALL_KINDS,
    CATEGORY_FIELDS,
    CATEGORY_LABELS,
    DOCUMENT_KINDS,
    NOT_DECIDED,
    NOT_FILLED,
    PREFERENCE_GROUPS,
    PREFERENCE_SINGLES,
    PREFERENCES,
    PROFILE,
    display_title,
)
from app.library.models import ExperienceItem, ExperienceVersion, VersionKind
from app.library.presentation import format_local, state_label, version_label
from app.library.service import (
    active_item,
    current_document,
    is_expired,
    item_versions,
    list_experiences,
)

EXPORT_FORMAT = "roleradar-library-export"
# 2: six categories, the profile and the preferences; field labels per kind.
EXPORT_FORMAT_VERSION = 2
SHARE_WARNING = (
    "注意：这份文件包含你的私有备注或联系方式。导出文件离开 RoleRadar 后不再受平台的权限保护；"
    "分享或发送之前，请先检查并删掉不想让别人看到的内容。"
)
NOT_INCLUDED = (
    "已超过恢复期限或已永久删除的内容",
    "附件：经历库目前还没有附件功能",
    "分析结果、岗位简历和投递记录：这些功能还没有建立",
)
FILE_PREFIXES = {PROFILE: "roleradar-profile", PREFERENCES: "roleradar-preferences"}


def single_export(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
) -> tuple[str, str]:
    item = active_item(db, owner_id, item_id, ALL_KINDS)
    versions = item_versions(db, owner_id, item, now)
    prefix = FILE_PREFIXES.get(item.category, "roleradar-experience")

    return f"{prefix}-{item.id[:8]}.md", item_markdown(item, versions, now)


def item_markdown(
    item: ExperienceItem,
    versions: list[ExperienceVersion],
    now: datetime,
) -> str:
    document = item.category in DOCUMENT_KINDS
    lines = [
        f"# {display_title(item.content, item.category)}",
        "",
        f"- 类别：{CATEGORY_LABELS.get(item.category, item.category)}",
    ]

    if not document:
        last_confirmed = (
            format_local(item.last_confirmed_at) if item.last_confirmed_at else "尚未确认"
        )
        lines.append(f"- 状态：{state_label(item)}")

    lines += [
        f"- 第一次保存：{format_local(item.created_at)}",
        f"- 最近一次保存：{format_local(item.updated_at)}",
    ]

    if not document:
        lines.append(f"- 上次确认：{last_confirmed}")

    lines += [
        f"- {'编号' if document else '经历编号'}：{item.id}",
        f"- 导出时间：{format_local(now)}",
    ]

    if item.deleted_at is not None:
        lines.append(
            f"- 回收站：删除于 {format_local(item.deleted_at)}，"
            f"在 {format_local(item.purge_after)} 之前可以恢复，之后会被永久删除"
        )

    lines += ["", f"> {SHARE_WARNING}", "", "## 当前内容", ""]
    lines += _field_lines(item.category, item.content)

    if not document:
        lines += ["## 私有备注", "", item.private_note or NOT_FILLED, ""]

    lines += [f"## 历史版本（{len(versions)} 个）", ""]

    if not versions:
        lines += ["没有保留的历史版本。", ""]

    for version in versions:
        when = {
            VersionKind.INITIAL: "第一次保存于",
            VersionKind.SAVED: "保存于",
        }.get(version.kind, "确认于")
        lines += [f"### {version_label(version)} · {when} {format_local(version.created_at)}", ""]

        if version.deleted_at is not None:
            lines += [
                f"这个历史版本已单独删除，在 {format_local(version.purge_after)} 之前可以恢复。",
                "",
            ]

        lines += _field_lines(item.category, version.content)

        if not document:
            lines += ["**当时的私有备注**", "", version.private_note or NOT_FILLED, ""]

    scope = (
        "导出范围：当前内容，以及仍保留或仍可恢复的历史版本；"
        if document
        else "导出范围：这条经历的当前内容和私有备注，以及仍保留或仍可恢复的历史版本；"
    )
    lines += ["---", "", scope + "不含已超过恢复期限或已永久删除的内容。", ""]

    return "\n".join(lines)


# U1-A's name for it.
experience_markdown = item_markdown


def _field_lines(category: str, content: dict) -> list[str]:
    if category == PREFERENCES:
        return _preference_lines(content)

    lines = []

    for field in CATEGORY_FIELDS[category]:
        lines += [f"**{field.label}**", "", content.get(field.name) or NOT_FILLED, ""]

    return lines


def _preference_lines(content: dict) -> list[str]:
    """Each preference with only the answers given; nothing given reads as undecided."""
    lines = []

    for _, label, fields in PREFERENCE_GROUPS:
        answers = [
            f"{field.label}：{content[field.name]}" for field in fields if content.get(field.name)
        ]
        lines += [f"**{label}**", "", "\n\n".join(answers) if answers else NOT_DECIDED, ""]

    for field in PREFERENCE_SINGLES:
        lines += [f"**{field.label}**", "", content.get(field.name) or NOT_DECIDED, ""]

    return lines


def export_archive(
    db: Session,
    owner_id: str,
    owner_label: str,
    now: datetime,
) -> tuple[str, bytes]:
    """Everything this owner still keeps, as (file name, ZIP bytes)."""
    current = list_experiences(db, owner_id)
    documents = {kind: current_document(db, owner_id, kind) for kind in DOCUMENT_KINDS}
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
    listing = {"experiences": [], "documents": [], "recycle-bin": []}
    records = {"experiences": [], "recycle-bin": []}

    for section, items in (("experiences", current), ("recycle-bin", binned)):
        for index, item in enumerate(items, start=1):
            versions = item_versions(db, owner_id, item, now)
            path = f"{section}/{index:02d}-{item.id[:8]}.md"
            files[path] = item_markdown(item, versions, now).encode("utf-8")
            listing[section].append((path, item))
            records[section].append(_item_record(item, versions))

    document_records = {}

    for kind, item in documents.items():
        document_records[kind] = None

        if item is not None:
            versions = item_versions(db, owner_id, item, now)
            path = f"{kind}.md"
            files[path] = item_markdown(item, versions, now).encode("utf-8")
            listing["documents"].append((path, item))
            document_records[kind] = _item_record(item, versions)

    data = {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": _iso(now),
        "owner": owner_label,
        "field_labels": {
            kind: {field.name: field.label for field in CATEGORY_FIELDS[kind]}
            for kind in ALL_KINDS
        },
        "experiences": records["experiences"],
        "profile": document_records[PROFILE],
        "preferences": document_records[PREFERENCES],
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
                "profile": int(documents[PROFILE] is not None),
                "preferences": int(documents[PREFERENCES] is not None),
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
        f"- `experiences/`：当前的经历（六个类别），每条一个 Markdown 文件，共 {len(listing['experiences'])} 条。",
        "- `profile.md`、`preferences.md`：当前的基本资料和求职偏好（只在已经填写时才有）。",
        f"- `recycle-bin/`：回收站里仍可恢复的内容，共 {len(listing['recycle-bin'])} 份；"
        "每个文件都写明了恢复截止时间。",
        "- `data.json`：同样的内容整理成结构化数据，方便以后导入其他工具。",
        "- `manifest.json`：文件清单和每个文件的 SHA-256 校验值。",
        "",
    ]

    for section, heading in (
        ("experiences", "当前经历"),
        ("documents", "基本资料与求职偏好"),
        ("recycle-bin", "回收站"),
    ):
        if listing[section]:
            lines += [f"### {heading}", ""]
            lines += [f"- `{path}`：{_listing_label(item)}" for path, item in listing[section]]
            lines.append("")

    lines += ["## 不包含什么", ""]
    lines += [f"- {entry}" for entry in NOT_INCLUDED]
    lines += [
        "",
        "## 分享前请注意",
        "",
        f"- {SHARE_WARNING}",
        "- 这个导出包是一份副本：之后在 RoleRadar 里删除内容，不会删除已经下载到你电脑上的这份文件。",
        "",
        "## 怎么打开",
        "",
        "`.md` 文件是纯文本，可以用任何文本编辑器或 Markdown 阅读器打开；`.json` 文件也可以用文本编辑器查看。",
        "",
    ]

    return "\n".join(lines)


def _listing_label(item: ExperienceItem) -> str:
    title = display_title(item.content, item.category)

    if item.category in DOCUMENT_KINDS:
        return title

    return f"{CATEGORY_LABELS[item.category]}：{title}（{state_label(item)}）"


def _item_record(item: ExperienceItem, versions: list[ExperienceVersion]) -> dict:
    record = {
        "id": item.id,
        "category": item.category,
        "category_label": CATEGORY_LABELS.get(item.category, item.category),
    }

    if item.category not in DOCUMENT_KINDS:
        record.update(state=item.state, state_label=state_label(item))

    record.update(
        revision=item.revision,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
    )

    if item.category not in DOCUMENT_KINDS:
        record["last_confirmed_at"] = _iso(item.last_confirmed_at)

    record.update(
        deleted_at=_iso(item.deleted_at),
        restore_deadline=_iso(item.purge_after),
        content=item.content,
    )

    if item.category not in DOCUMENT_KINDS:
        record["private_note"] = item.private_note

    record["versions"] = [
        {
            "id": version.id,
            "kind": version.kind,
            "label": version_label(version),
            "created_at": _iso(version.created_at),
            "deleted_at": _iso(version.deleted_at),
            "restore_deadline": _iso(version.purge_after),
            "content": version.content,
            **({} if item.category in DOCUMENT_KINDS else {"private_note": version.private_note}),
        }
        for version in versions
    ]

    return record


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None

    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
