"""Exports a user can take away and read without RoleRadar.

One record exports as a Markdown file; it lists the record's attachments but
does not contain them. "Export all" builds a ZIP with a README, one Markdown
file per experience, the basic profile and the job preferences, the
attachment files, the same data as JSON and a manifest with checksums.
Exports include private notes and contact details, so every file reminds the
reader to check them before sharing. Content past its restore deadline, or
permanently deleted, is never exported.

The ZIP is built in two steps. collect_export reads everything it needs in
one transaction; write_export then writes the package to a temporary file in
the attachment store's export folder, copying each attachment in chunks
after checking it against its recorded size and checksum, so the database is
not locked and no file is held in memory as a whole. An attachment whose
file is missing, unreadable or changed is listed with that status and left
out, and the package says it is incomplete. The temporary file loses its
name before it is sent: a clean-up running meanwhile cannot remove it, and
nothing of it is left once the download ends.

Since U2-B1 the package also holds the resume imports still kept (not
ended, not expired): each in imports/ with the text that was read and the
original file, said to be temporary and not part of the library. An import
file that cannot be copied is listed like an attachment that cannot.
"""
import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO

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
from app.library.attachments import TYPE_LABELS
from app.library.errors import ChangedFile, FileStoreError, MissingFile
from app.library.files import CHUNK_BYTES, IMPORTS, FileStore
from app.library.imports import import_text, open_imports
from app.library.models import (
    Attachment,
    AttachmentState,
    ExperienceItem,
    ExperienceVersion,
    ImportSource,
    ResumeImport,
    VersionKind,
)
from app.library.presentation import format_local, format_size, state_label, version_label
from app.library.service import (
    active_item,
    current_document,
    is_expired,
    item_versions,
    list_experiences,
)

EXPORT_FORMAT = "roleradar-library-export"
# 2: six categories, the profile and the preferences; field labels per kind.
# 3: attachment files, their list in data.json and the manifest, and "complete".
# 4: resume imports still kept (U2-B1), with their text and original files.
EXPORT_FORMAT_VERSION = 4
SHARE_WARNING = (
    "注意：这份文件包含你的私有备注或联系方式。导出文件离开 RoleRadar 后不再受平台的权限保护；"
    "分享或发送之前，请先检查并删掉不想让别人看到的内容。"
)
NOT_INCLUDED = (
    "已超过恢复期限或已永久删除的内容，包括附件文件",
    "没有上传完成的附件",
    "已放弃或已到期的简历导入",
    "分析结果、岗位简历和投递记录：这些功能还没有建立",
)
IMPORT_SOURCES = {ImportSource.PDF: "PDF", ImportSource.DOCX: "Word（.docx）", ImportSource.TEXT: "粘贴的文字"}
IMPORT_STATES = {"reading": "正在读取", "read": "文字已读取", "partial": "部分读取", "failed": "读取失败"}
FILE_PREFIXES = {PROFILE: "roleradar-profile", PREFERENCES: "roleradar-preferences"}
# What happened to an attachment file in "export all".
INCLUDED, MISSING, UNREADABLE, CHANGED = "included", "missing", "unreadable", "changed"
STATUS_TEXT = {
    MISSING: "文件缺失，没有包含在导出包里",
    UNREADABLE: "文件无法读取，没有包含在导出包里",
    CHANGED: "文件和上传时记录的大小或校验值不一致（可能已损坏），没有包含在导出包里",
}
# Characters some systems do not allow in file names.
_NOT_IN_NAMES = re.compile(r'[\\/:*?"<>|]')


@dataclass
class AttachmentEntry:
    """An attachment as an export lists it; path and status once "export all" handled its file."""

    attachment: Attachment
    path: str | None = None
    status: str | None = None


@dataclass
class ExportRecord:
    item: ExperienceItem
    versions: list[ExperienceVersion]
    attachments: list[AttachmentEntry] = field(default_factory=list)


@dataclass
class ExportContent:
    current: list[ExportRecord]
    documents: dict[str, ExportRecord | None]
    binned: list[ExportRecord]
    # Resume imports still kept (U2-B1); not part of the library.
    imports: list[ResumeImport] = field(default_factory=list)


@dataclass
class ExportPackage:
    filename: str
    # Open at its start; its file no longer has a name, so closing it frees it.
    handle: BinaryIO
    size: int
    complete: bool


def single_export(
    db: Session,
    owner_id: str,
    item_id: str,
    now: datetime,
) -> tuple[str, str]:
    item = active_item(db, owner_id, item_id, ALL_KINDS)
    versions = item_versions(db, owner_id, item, now)
    prefix = FILE_PREFIXES.get(item.category, "roleradar-experience")
    attachments = [AttachmentEntry(row) for row in exportable_attachments(db, owner_id, item, now)]

    return f"{prefix}-{item.id[:8]}.md", item_markdown(item, versions, now, attachments)


def exportable_attachments(db: Session, owner_id: str, item: ExperienceItem, now: datetime) -> list[Attachment]:
    """Ready attachments of a record that are kept or still restorable, oldest first."""
    rows = db.scalars(
        select(Attachment)
        .where(
            Attachment.item_id == item.id,
            Attachment.owner_id == owner_id,
            Attachment.state == AttachmentState.READY,
        )
        .order_by(Attachment.created_at, Attachment.id)
    )

    return [row for row in rows if not is_expired(row, now)]


def item_markdown(
    item: ExperienceItem,
    versions: list[ExperienceVersion],
    now: datetime,
    attachments: list[AttachmentEntry] | None = None,
) -> str:
    """The record as Markdown. attachments (experiences only) are listed;
    those "export all" handled also say where their file is, or why it is not there."""
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

    if attachments is not None:
        lines += _attachment_lines(item, attachments)

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


def _attachment_lines(item: ExperienceItem, entries: list[AttachmentEntry]) -> list[str]:
    lines = [f"## 附件（{len(entries)} 个）", ""]

    if not entries:
        return lines + ["没有附件。", ""]

    for entry in entries:
        attachment = entry.attachment
        notes = []

        if attachment.deleted_at is not None and attachment.deletion_id != item.deletion_id:
            notes.append(f"已单独删除，在 {format_local(attachment.purge_after)} 之前可以恢复")

        if entry.path is not None:
            notes.append(f"文件在导出包的 `{entry.path}`")
        elif entry.status is not None:
            notes.append(STATUS_TEXT[entry.status])

        line = (
            f"- {attachment.filename}（{TYPE_LABELS.get(attachment.media_type, attachment.media_type)}，"
            f"{format_size(attachment.size)}，上传于 {format_local(attachment.created_at)}）"
        )
        lines.append(line + ("：" + "；".join(notes) if notes else ""))

    lines.append("")

    if all(entry.status is None for entry in entries):
        lines += ["附件文件本身不在这个 Markdown 文件里；需要文件时，请在经历页面单独下载，或使用“导出全部”。", ""]

    return lines


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


def collect_export(db: Session, owner_id: str, now: datetime) -> ExportContent:
    """Everything this owner still keeps, read in one transaction."""
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

    def record(item: ExperienceItem) -> ExportRecord:
        attachments = [] if item.category in DOCUMENT_KINDS else exportable_attachments(db, owner_id, item, now)

        return ExportRecord(
            item=item,
            versions=item_versions(db, owner_id, item, now),
            attachments=[AttachmentEntry(row) for row in attachments],
        )

    documents = {kind: current_document(db, owner_id, kind) for kind in DOCUMENT_KINDS}

    return ExportContent(
        current=[record(item) for item in current],
        documents={kind: record(item) if item else None for kind, item in documents.items()},
        binned=[record(item) for item in binned],
        imports=open_imports(db, owner_id, now),
    )


def write_export(
    content: ExportContent,
    files: FileStore,
    owner_label: str,
    now: datetime,
) -> ExportPackage:
    """Write the ZIP to a temporary file of the store and hand it over, open.

    The file loses its name before anything is written to it, so the
    package never lies in the folder under a name: if the process stops
    while it is written, nothing of it stays behind, and a permanent
    deletion that finishes meanwhile has no copy there to miss.
    """
    path, handle = files.new_export()

    try:
        files.detach_export(path)

        return _write_zip(content, files, owner_label, now, handle)
    except BaseException:
        handle.close()
        raise


def _write_zip(content: ExportContent, files: FileStore, owner_label: str, now: datetime, handle) -> ExportPackage:
    folder = f"roleradar-export-{now.astimezone():%Y%m%d-%H%M%S}"
    stamp = now.astimezone().timetuple()[:6]
    texts: dict[str, bytes] = {}
    copied: list[dict] = []
    listing = {"experiences": [], "documents": [], "recycle-bin": []}
    manifest_attachments = []

    with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:

        def add_file(entry: AttachmentEntry, directory: str, number: int, section: str, record_path: str):
            attachment = entry.attachment
            target = f"{directory}/{_package_name(number, attachment.filename)}"
            entry.status = _copy_attachment(archive, files, attachment, f"{folder}/{target}", stamp)

            if entry.status == INCLUDED:
                entry.path = target
                copied.append({"path": target, "bytes": attachment.size, "sha256": attachment.sha256})

            manifest_attachments.append(
                {
                    "attachment_id": attachment.id,
                    "experience_id": attachment.item_id,
                    "experience_file": record_path,
                    "section": section,
                    "path": entry.path,
                    "filename": attachment.filename,
                    "media_type": attachment.media_type,
                    "bytes": attachment.size,
                    "sha256": attachment.sha256,
                    "status": entry.status,
                    "deleted_at": _iso(attachment.deleted_at),
                    "restore_deadline": _iso(attachment.purge_after),
                }
            )

        for section, records in (("experiences", content.current), ("recycle-bin", content.binned)):
            for index, record in enumerate(records, start=1):
                name = f"{index:02d}-{record.item.id[:8]}"
                record_path = f"{section}/{name}.md"
                kept = [entry for entry in record.attachments if entry.attachment.deleted_at is None]
                in_bin = [entry for entry in record.attachments if entry.attachment.deleted_at is not None]

                for number, entry in enumerate(kept, start=1):
                    add_file(entry, f"{section}/{name}-attachments", number, "current", record_path)

                # A current experience's attachments deleted on their own are
                # recycle-bin content: they go there, under the experience's name.
                directory = (
                    f"recycle-bin/deleted-attachments/{name}"
                    if section == "experiences"
                    else f"recycle-bin/{name}-attachments"
                )

                for number, entry in enumerate(in_bin, start=1):
                    add_file(entry, directory, number, "recycle-bin", record_path)

                texts[record_path] = item_markdown(record.item, record.versions, now, record.attachments).encode("utf-8")
                listing[section].append((record_path, record.item))

        for kind, record in content.documents.items():
            if record is not None:
                texts[f"{kind}.md"] = item_markdown(record.item, record.versions, now).encode("utf-8")
                listing["documents"].append((f"{kind}.md", record.item))

        manifest_imports = []

        for index, row in enumerate(content.imports, start=1):
            directory = f"imports/{index:02d}-{row.id[:8]}"
            text_path = None
            original_path = original_status = None

            if row.segments:
                text_path = f"{directory}/text.txt"
                texts[text_path] = import_text(row).encode("utf-8")

            if row.storage_key is not None:
                target = f"{directory}/{_package_name(1, row.filename)}"
                original_status = _copy_import(archive, files, row, f"{folder}/{target}", stamp)

                if original_status == INCLUDED:
                    original_path = target
                    copied.append({"path": target, "bytes": row.size, "sha256": row.sha256})

            manifest_imports.append(
                {
                    "import_id": row.id,
                    "folder": directory,
                    "source": row.source,
                    "state": row.state,
                    "filename": row.filename,
                    "received_at": _iso(row.received_at),
                    "expires_at": _iso(row.expires_at),
                    "text_path": text_path,
                    "original_path": original_path,
                    "original_status": original_status,
                    "bytes": row.size,
                    "sha256": row.sha256,
                }
            )

        left_out = [entry for entry in manifest_attachments if entry["status"] != INCLUDED]
        imports_left_out = [
            entry for entry in manifest_imports if entry["original_status"] not in (None, INCLUDED)
        ]
        complete = not left_out and not imports_left_out
        data = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "generated_at": _iso(now),
            "owner": owner_label,
            "complete": complete,
            "field_labels": {
                kind: {field.name: field.label for field in CATEGORY_FIELDS[kind]}
                for kind in ALL_KINDS
            },
            "experiences": [_item_record(record) for record in content.current],
            "profile": _document_record(content.documents[PROFILE]),
            "preferences": _document_record(content.documents[PREFERENCES]),
            "recycle_bin": [_item_record(record) for record in content.binned],
            "imports": [_import_record(row, entry) for row, entry in zip(content.imports, manifest_imports)],
        }
        texts["data.json"] = _json_bytes(data)
        texts["README.md"] = _readme(
            owner_label, now, listing, manifest_attachments, list(zip(content.imports, manifest_imports))
        ).encode("utf-8")
        texts["manifest.json"] = _json_bytes(
            {
                "format": EXPORT_FORMAT,
                "format_version": EXPORT_FORMAT_VERSION,
                "generated_at": _iso(now),
                "complete": complete,
                "counts": {
                    "experiences": len(content.current),
                    "profile": int(content.documents[PROFILE] is not None),
                    "preferences": int(content.documents[PREFERENCES] is not None),
                    "recycle_bin": len(content.binned),
                    "attachments": sum(1 for entry in manifest_attachments if entry["status"] == INCLUDED),
                    "imports": len(content.imports),
                },
                "not_included": list(NOT_INCLUDED),
                "attachments": manifest_attachments,
                "imports": manifest_imports,
                "files": sorted(
                    [
                        {
                            "path": path,
                            "bytes": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                        for path, payload in texts.items()
                        if path != "manifest.json"
                    ]
                    + copied,
                    key=lambda entry: entry["path"],
                ),
            }
        )

        for path, payload in texts.items():
            info = zipfile.ZipInfo(f"{folder}/{path}", date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Without permission bits some unzip tools create unreadable files.
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload)

    size = handle.seek(0, os.SEEK_END)
    handle.seek(0)
    suffix = "" if complete else "-incomplete"

    return ExportPackage(filename=f"{folder}{suffix}.zip", handle=handle, size=size, complete=complete)


def _copy_attachment(archive: zipfile.ZipFile, files: FileStore, attachment: Attachment, name: str, stamp) -> str:
    """Copy one attachment file into the package in chunks; return its status."""
    try:
        source = files.open_checked(attachment.owner_id, attachment.storage_key, attachment.size, attachment.sha256)
    except MissingFile:
        return MISSING
    except ChangedFile:
        return CHANGED
    except (FileStoreError, OSError, ValueError):
        return UNREADABLE

    with source:
        info = zipfile.ZipInfo(name, date_time=stamp)
        # PDF, PNG and JPEG are compressed already.
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o644 << 16

        with archive.open(info, "w") as target:
            while chunk := source.read(CHUNK_BYTES):
                target.write(chunk)

    return INCLUDED


def _copy_import(archive: zipfile.ZipFile, files: FileStore, row: ResumeImport, name: str, stamp) -> str:
    """Copy one import's original file into the package in chunks; return its status."""
    try:
        source = files.open_checked(row.owner_id, row.storage_key, row.size, row.sha256, area=IMPORTS)
    except MissingFile:
        return MISSING
    except ChangedFile:
        return CHANGED
    except (FileStoreError, OSError, ValueError):
        return UNREADABLE

    with source:
        info = zipfile.ZipInfo(name, date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16

        with archive.open(info, "w") as target:
            while chunk := source.read(CHUNK_BYTES):
                target.write(chunk)

    return INCLUDED


def _import_record(row: ResumeImport, entry: dict) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "source_label": IMPORT_SOURCES.get(row.source, row.source),
        "state": row.state,
        "state_label": IMPORT_STATES.get(row.state, row.state),
        "filename": row.filename,
        "received_at": _iso(row.received_at),
        "expires_at": _iso(row.expires_at),
        "details": row.details,
        "segments": row.segments,
        "text_path": entry["text_path"],
        "original_path": entry["original_path"],
        "original_status": entry["original_status"],
    }


def _package_name(number: int, filename: str) -> str:
    """A numbered name that every system can create; the number keeps equal names apart."""
    name = _NOT_IN_NAMES.sub("_", filename).strip(" .") or "attachment"

    if len(name) > 100:
        stem, extension = os.path.splitext(name)
        name = stem[: 100 - len(extension)] + extension

    return f"{number:02d}-{name}"


def _readme(
    owner_label: str,
    now: datetime,
    listing: dict,
    attachments: list[dict],
    imports: list[tuple[ResumeImport, dict]],
) -> str:
    included = [entry for entry in attachments if entry["status"] == INCLUDED]
    left_out = [entry for entry in attachments if entry["status"] != INCLUDED]
    imports_left_out = [entry for _, entry in imports if entry["original_status"] not in (None, INCLUDED)]
    current_files = sum(1 for entry in included if entry["section"] == "current")
    lines = [
        "# RoleRadar 经历库导出",
        "",
        f"- 导出时间：{format_local(now)}",
        f"- 导出账号：{owner_label}（本地开发模式的合成用户）",
        "",
    ]

    if left_out:
        lines += [
            f"**这个导出包不完整：有 {len(left_out)} 个附件的文件没能放进来，原因见下面的“没能包含的附件”和 manifest.json。**",
            "",
        ]

    if imports_left_out:
        lines += [
            f"**这个导出包不完整：有 {len(imports_left_out)} 个导入的原文件没能放进来，原因见下面的“导入中的临时资料”和 manifest.json。**",
            "",
        ]

    lines += [
        "## 包含什么",
        "",
        f"- `experiences/`：当前的经历（六个类别），每条一个 Markdown 文件，共 {len(listing['experiences'])} 条；"
        f"每条经历的附件原文件在它旁边的 `…-attachments/` 文件夹里，共 {current_files} 个。",
        "- `profile.md`、`preferences.md`：当前的基本资料和求职偏好（只在已经填写时才有）。",
        f"- `recycle-bin/`：回收站里仍可恢复的内容，共 {len(listing['recycle-bin'])} 份，"
        f"以及仍可恢复的附件文件 {len(included) - current_files} 个；每份都写明了恢复截止时间。"
        "当前经历中单独删除的附件在 `recycle-bin/deleted-attachments/` 里，文件夹名对应 `experiences/` 里的经历。",
        f"- `imports/`：还在暂存期内的简历导入，共 {len(imports)} 份，每份有读到的文字（`text.txt`）和原文件。"
        "它们不属于经历库，见下面的“导入中的临时资料”。",
        "- `data.json`：同样的内容整理成结构化数据，方便以后导入其他工具。",
        "- `manifest.json`：文件清单、每个文件的 SHA-256 校验值，以及每个附件的来源和状态。",
        "",
    ]

    if left_out:
        lines += ["## 没能包含的附件", ""]
        lines += [
            f"- {entry['filename']}（属于 `{entry['experience_file']}`）：{STATUS_TEXT[entry['status']]}。"
            for entry in left_out
        ]
        lines.append("")

    for section, heading in (
        ("experiences", "当前经历"),
        ("documents", "基本资料与求职偏好"),
        ("recycle-bin", "回收站"),
    ):
        if listing[section]:
            lines += [f"### {heading}", ""]
            lines += [f"- `{path}`：{_listing_label(item)}" for path, item in listing[section]]
            lines.append("")

    if imports:
        lines += [
            "## 导入中的临时资料",
            "",
            "下面这些是导入时读到的原文和原文件，还没有加入经历库。RoleRadar 只暂存到各自的到期时间，"
            "到期后会删除；导出包里的这份副本不受影响。",
            "",
        ]

        for row, entry in imports:
            name = row.filename or "粘贴的文字"
            line = f"- `{entry['folder']}/`：{name}（{IMPORT_SOURCES.get(row.source, row.source)}，"
            line += f"{IMPORT_STATES.get(row.state, row.state)}），暂存到 {format_local(row.expires_at)}"

            if entry["original_status"] not in (None, INCLUDED):
                line += f"；原文件{STATUS_TEXT[entry['original_status']]}"

            lines.append(line + "。")

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


def _document_record(record: ExportRecord | None) -> dict | None:
    return None if record is None else _record_data(record.item, record.versions)


def _item_record(record: ExportRecord) -> dict:
    data = _record_data(record.item, record.versions)
    item = record.item
    data["attachments"] = [
        {
            "id": entry.attachment.id,
            "filename": entry.attachment.filename,
            "media_type": entry.attachment.media_type,
            "bytes": entry.attachment.size,
            "sha256": entry.attachment.sha256,
            "uploaded_at": _iso(entry.attachment.created_at),
            "deleted_at": _iso(entry.attachment.deleted_at),
            "restore_deadline": _iso(entry.attachment.purge_after),
            "deleted_with_experience": (
                entry.attachment.deleted_at is not None and entry.attachment.deletion_id == item.deletion_id
            ),
            "status": entry.status,
            "path": entry.path,
        }
        for entry in record.attachments
    ]

    return data


def _record_data(item: ExperienceItem, versions: list[ExperienceVersion]) -> dict:
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
