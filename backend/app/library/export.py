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
file that cannot be copied is listed like an attachment that cannot. Since
U2-B2a an import that was structured also has candidates.json: the
candidates found in it, where each value stands in the text, the user's
edits and choices, and the candidates dropped, by place and reason. Since
U2-B2b a record written from an import says so: when, and whether it was
added or supplemented. That note comes from the record's own history,
holds no words and is exported only with the record itself. Since U2-B3a
the imports include those saved as drafts and drafts in the recycle bin
(until their restore deadline), each saying how it is kept; exporting them
restores, saves or adopts nothing. Since U3-C1 the package also holds the
saved direction explorations and direction decisions, in use and still
restorable, each saying how what it rests on stands now: in data.json and,
readable, in directions.md (see direction_export.py). Since U3-D3a it also
holds the postings and search summaries the user kept from a search, in use
and still restorable: their whitelisted facts, checks, decisions and
feedback, in data.json and, readable, in jobs.md (see job_export.py). Since
U3-D3b also the analyses the user kept from a search, as they were kept
(quotes whole, with what each was read with then), each saying how what it
rests on stands now.
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
from app.library.direction_export import DirectionExport, collect_directions, directions_markdown
from app.library.job_export import JobExport, collect_job_records, jobs_markdown
from app.library.errors import ChangedFile, FileStoreError, MissingFile
from app.library.files import CHUNK_BYTES, IMPORTS, FileStore
from app.library.imports import draft_of, import_text, kept_imports
from app.library.models import (
    Attachment,
    AttachmentState,
    AuditEvent,
    ExperienceItem,
    ExperienceVersion,
    ImportDraft,
    ImportSource,
    ResumeImport,
    VersionKind,
)
from app.library.presentation import (
    CATEGORY_NAMES,
    EVENT_LABELS,
    label_of,
    format_local,
    format_size,
    group_label,
    preference_label,
    shown_fields,
    state_label,
    version_label,
)
from app.library.review import IMPORT_EVENTS, adoption_views, candidates_of, kept_record, review_record, structuring_of
from app.library.service import (
    active_item,
    current_document,
    is_expired,
    item_versions,
    list_experiences,
)
from app.i18n import MessageMap, lazy, list_join, t

EXPORT_FORMAT = "roleradar-library-export"
# 2: six categories, the profile and the preferences; field labels per kind.
# 3: attachment files, their list in data.json and the manifest, and "complete".
# 4: resume imports still kept (U2-B1), with their text and original files.
# 5: the candidates of those imports and their review (U2-B2a), in candidates.json.
# 6: where adopted candidates went, and on each record the imports that wrote it (U2-B2b).
# 7: imports saved as drafts and drafts in the recycle bin, each with how it is kept (U2-B3a).
# 8: saved direction explorations and direction decisions, each with how what it rests on stands (U3-C1).
# 9: postings and search summaries kept from a search, with their checks, decisions and feedback (U3-D3a).
# 10: analyses kept from a search, with what each rests on and the feedback on them (U3-D3b).
# 11: on each kept posting, the checks made from its record (U3-D4a): never the posting's text.
# 12: current recommendations held for tracking, apart from 我的岗位, with when each was held and until when (U3-D4b-2).
# 13: 每日查新 on a kept summary: its state and last run, the postings it has seen and its new ones (U3-D4c).
# 14: postings the user added themselves (U3-D4d): how each came (supplied), and the text of one they pasted (user_text).
EXPORT_FORMAT_VERSION = 14
SHARE_WARNING = (
    lazy("exp.11")
)
NOT_INCLUDED = (
    lazy("exp.12"),
    lazy("exp.13"),
    lazy("exp.14"),
    lazy("exp.15"),
)
IMPORT_SOURCES = MessageMap({ImportSource.PDF: "exp.import_sources.1", ImportSource.DOCX: "exp.import_sources.2", ImportSource.TEXT: "exp.import_sources.3"})
IMPORT_STATES = MessageMap({"reading": "exp.import_states.1", "read": "exp.import_states.2", "partial": "exp.import_states.3", "failed": "exp.import_states.4"})
FILE_PREFIXES = {PROFILE: "roleradar-profile", PREFERENCES: "roleradar-preferences"}
# What happened to an attachment file in "export all".
INCLUDED, MISSING, UNREADABLE, CHANGED = "included", "missing", "unreadable", "changed"
STATUS_TEXT = MessageMap({
    MISSING: "exp.status_text.1",
    UNREADABLE: "exp.status_text.2",
    CHANGED: "exp.status_text.3",
})
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
    # When a resume import added or supplemented it: (event, time), no words (U2-B2b).
    provenance: list[tuple[str, datetime]] = field(default_factory=list)


@dataclass
class ExportContent:
    current: list[ExportRecord]
    documents: dict[str, ExportRecord | None]
    binned: list[ExportRecord]
    # Resume imports still kept (U2-B1), saved drafts and drafts in the recycle bin (U2-B3a); not part of the library.
    imports: list[ResumeImport] = field(default_factory=list)
    # The draft row of each import that was saved, by import id; None for a temporary one.
    drafts: dict[str, ImportDraft | None] = field(default_factory=dict)
    # The candidates and review of each structured import, by import id (U2-B2a).
    reviews: dict[str, dict] = field(default_factory=dict)
    # Saved direction explorations and direction decisions, in use and restorable (U3-C1).
    directions: DirectionExport = field(default_factory=DirectionExport)
    # Postings and search summaries kept from a search, in use and restorable (U3-D3a).
    jobs: JobExport = field(default_factory=JobExport)


@dataclass
class ExportPackage:
    filename: str
    # Open at its start; its file no longer has a name, so closing it frees it.
    handle: BinaryIO
    size: int
    complete: bool
    # What this very build could not put in (UI bilingual batch, plan 5.6): each attachment or import file left out,
    # with its status and where to find it; the same build's manifest and README list the same.
    left_out: list[dict] = field(default_factory=list)


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
    markdown = item_markdown(item, versions, now, attachments, import_provenance(db, owner_id, item.id))

    return f"{prefix}-{item.id[:8]}.md", markdown


def import_provenance(db: Session, owner_id: str, item_id: str) -> list[tuple[str, datetime]]:
    """When resume imports added or supplemented this record, from its own history; never any words."""
    return list(
        db.execute(
            select(AuditEvent.action, AuditEvent.at)
            .where(
                AuditEvent.owner_id == owner_id,
                AuditEvent.item_id == item_id,
                AuditEvent.action.in_(IMPORT_EVENTS),
            )
            .order_by(AuditEvent.at, AuditEvent.id)
        ).tuples()
    )


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
    provenance: list[tuple[str, datetime]] = (),
) -> str:
    """The record as Markdown. attachments (experiences only) are listed;
    those "export all" handled also say where their file is, or why it is not there."""
    document = item.category in DOCUMENT_KINDS
    lines = [
        f"# {display_title(item.content, item.category)}",
        "",
        t("exp.16", p0=CATEGORY_NAMES.get(item.category, item.category)),
    ]

    if not document:
        last_confirmed = (
            format_local(item.last_confirmed_at) if item.last_confirmed_at else t("exp.17")
        )
        lines.append(t("exp.18", p0=state_label(item)))

    lines += [
        t("exp.19", p0=format_local(item.created_at)),
        t("exp.20", p0=format_local(item.updated_at)),
    ]

    if not document:
        lines.append(t("exp.21", p0=last_confirmed))

    lines += [
        t("exp.22", p0=t("exp.id_document") if document else t("exp.id_item"), p1=item.id),
        t("exp.23", p0=format_local(now)),
    ]

    lines += [t("exp.24", p0=format_local(at), p1=EVENT_LABELS.get(action, action)) for action, at in provenance]

    if item.deleted_at is not None:
        lines.append(
            t("exp.25", p0=format_local(item.deleted_at), p1=format_local(item.purge_after))
        )

    lines += ["", f"> {SHARE_WARNING}", "", t("exp.26"), ""]
    lines += _field_lines(item.category, item.content)

    if not document:
        lines += [t("exp.27"), "", item.private_note or NOT_FILLED, ""]

    if attachments is not None:
        lines += _attachment_lines(item, attachments)

    lines += [t("exp.28", p0=len(versions)), ""]

    if not versions:
        lines += [t("exp.29"), ""]

    for version in versions:
        when = {
            VersionKind.INITIAL: t("exp.30"),
            VersionKind.SAVED: t("exp.31"),
        }.get(version.kind, t("exp.32"))
        lines += [f"### {version_label(version)} · {when} {format_local(version.created_at)}", ""]

        if version.deleted_at is not None:
            lines += [
                t("exp.33", p0=format_local(version.purge_after)),
                "",
            ]

        lines += _field_lines(item.category, version.content)

        if not document:
            lines += [t("exp.34"), "", version.private_note or NOT_FILLED, ""]

    scope = (
        t("exp.35")
        if document
        else t("exp.36")
    )
    lines += ["---", "", scope + t("exp.37"), ""]

    return "\n".join(lines)


# U1-A's name for it.
experience_markdown = item_markdown


def _attachment_lines(item: ExperienceItem, entries: list[AttachmentEntry]) -> list[str]:
    lines = [t("exp.38", p0=len(entries)), ""]

    if not entries:
        return lines + [t("exp.39"), ""]

    for entry in entries:
        attachment = entry.attachment
        notes = []

        if attachment.deleted_at is not None and attachment.deletion_id != item.deletion_id:
            notes.append(t("exp.40", p0=format_local(attachment.purge_after)))

        if entry.path is not None:
            notes.append(t("exp.41", p0=entry.path))
        elif entry.status is not None:
            notes.append(STATUS_TEXT[entry.status])

        line = (
            t("exp.42", p0=attachment.filename, p1=TYPE_LABELS.get(attachment.media_type, attachment.media_type), p2=format_size(attachment.size), p3=format_local(attachment.created_at))
        )
        lines.append(line + (t("exp.43") + list_join(notes, "clauses") if notes else ""))

    lines.append("")

    if all(entry.status is None for entry in entries):
        lines += [t("exp.45"), ""]

    return lines


def _field_lines(category: str, content: dict) -> list[str]:
    if category == PREFERENCES:
        return _preference_lines(content)

    lines = []

    # The fields as the reading page shows them (plan 5.2), named in the language the export is built in (6.4); the
    # user's words as they are.
    for name in shown_fields(content, category):
        lines += [f"**{label_of(category, name)}**", "", content.get(name) or t("content.not_filled"), ""]

    return lines


def _preference_lines(content: dict) -> list[str]:
    """Each preference with only the answers given; nothing given reads as undecided."""
    lines = []

    for key, _, fields in PREFERENCE_GROUPS:
        answers = [
            t("exp.46", p0=preference_label(field.name), p1=content[field.name]) for field in fields if content.get(field.name)
        ]
        lines += [f"**{group_label(f'pref.group.{key}')}**", "", "\n\n".join(answers) if answers else t("content.not_decided"), ""]

    for field in PREFERENCE_SINGLES:
        lines += [f"**{preference_label(field.name)}**", "", content.get(field.name) or t("content.not_decided"), ""]

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
            provenance=import_provenance(db, owner_id, item.id),
        )

    documents = {kind: current_document(db, owner_id, kind) for kind in DOCUMENT_KINDS}
    imports = kept_imports(db, owner_id, now)
    drafts = {row.id: draft_of(db, row.id) for row in imports}
    reviews = {}

    for row in imports:
        structuring = structuring_of(db, row.id)

        if structuring is not None:
            candidates = candidates_of(db, owner_id, row.id)
            reviews[row.id] = review_record(
                row, structuring, candidates, adoption_views(db, owner_id, candidates), drafts[row.id]
            )

    return ExportContent(
        current=[record(item) for item in current],
        documents={kind: record(item) if item else None for kind, item in documents.items()},
        binned=[record(item) for item in binned],
        imports=imports,
        drafts=drafts,
        reviews=reviews,
        directions=collect_directions(db, owner_id, now),
        jobs=collect_job_records(db, owner_id, now),
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

                texts[record_path] = item_markdown(
                    record.item, record.versions, now, record.attachments, record.provenance
                ).encode("utf-8")
                listing[section].append((record_path, record.item))

        for kind, record in content.documents.items():
            if record is not None:
                texts[f"{kind}.md"] = item_markdown(record.item, record.versions, now, None, record.provenance).encode("utf-8")
                listing["documents"].append((f"{kind}.md", record.item))

        manifest_imports = []

        for index, row in enumerate(content.imports, start=1):
            directory = f"imports/{index:02d}-{row.id[:8]}"
            text_path = None
            original_path = original_status = None

            if row.segments:
                text_path = f"{directory}/text.txt"
                texts[text_path] = import_text(row).encode("utf-8")

            review = content.reviews.get(row.id)
            candidates_path = f"{directory}/candidates.json" if review is not None else None

            if review is not None:
                texts[candidates_path] = _json_bytes(review)

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
                    **kept_record(row, content.drafts.get(row.id)),
                    "text_path": text_path,
                    "candidates_path": candidates_path,
                    "candidates": len(review["candidates"]) if review is not None else 0,
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
            "imports": [
                _import_record(row, entry, content.reviews.get(row.id), content.drafts.get(row.id))
                for row, entry in zip(content.imports, manifest_imports)
            ],
            "directions": {
                "explorations": content.directions.explorations,
                "decisions": content.directions.decisions,
            },
            "job_records": {
                "postings": content.jobs.postings,
                "searches": content.jobs.searches,
                "analyses": content.jobs.analyses,
                "recommendations": content.jobs.recommendations,
                "watches": content.jobs.watches,
            },
        }
        texts["data.json"] = _json_bytes(data)

        if content.directions.explorations or content.directions.decisions:
            texts["directions.md"] = directions_markdown(content.directions, now).encode("utf-8")

        if content.jobs:
            texts["jobs.md"] = jobs_markdown(content.jobs, now).encode("utf-8")

        texts["README.md"] = _readme(
            owner_label,
            now,
            listing,
            manifest_attachments,
            [(row, entry, content.drafts.get(row.id)) for row, entry in zip(content.imports, manifest_imports)],
            content.directions,
            content.jobs,
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
                    **content.directions.counts(),
                    **content.jobs.counts(),
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

    missing = [
        {"kind": "attachment", "name": entry["filename"], "status": entry["status"], "href": f"/experiences/{entry['experience_id']}"}
        for entry in left_out
    ] + [
        {"kind": "import", "name": entry["filename"] or entry["folder"], "status": entry["original_status"], "href": f"/imports/{entry['import_id']}"}
        for entry in imports_left_out
    ]

    return ExportPackage(filename=f"{folder}{suffix}.zip", handle=handle, size=size, complete=complete, left_out=missing)


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


def _kept_text(row: ResumeImport, draft: ImportDraft | None) -> str:
    """How an import is kept, as the README says it (U2-B3a)."""
    if draft is None:
        return t("exp.47", p0=format_local(row.expires_at))

    if draft.deleted_at is None:
        return t("exp.48", p0=format_local(draft.saved_at))

    return t("exp.49", p0=format_local(draft.purge_after))


def _import_record(row: ResumeImport, entry: dict, review: dict | None, draft: ImportDraft | None) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "source_label": IMPORT_SOURCES.get(row.source, row.source),
        "state": row.state,
        "state_label": IMPORT_STATES.get(row.state, row.state),
        "filename": row.filename,
        "received_at": _iso(row.received_at),
        "expires_at": _iso(row.expires_at),
        **kept_record(row, draft),
        "details": row.details,
        "segments": row.segments,
        "text_path": entry["text_path"],
        "original_path": entry["original_path"],
        "original_status": entry["original_status"],
        "candidates_path": entry["candidates_path"],
        "review": review,
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
    imports: list[tuple[ResumeImport, dict, ImportDraft | None]],
    directions: DirectionExport | None = None,
    jobs: JobExport | None = None,
) -> str:
    directions = directions or DirectionExport()
    jobs = jobs or JobExport()
    included = [entry for entry in attachments if entry["status"] == INCLUDED]
    left_out = [entry for entry in attachments if entry["status"] != INCLUDED]
    imports_left_out = [entry for _, entry, _ in imports if entry["original_status"] not in (None, INCLUDED)]
    current_files = sum(1 for entry in included if entry["section"] == "current")
    lines = [
        t("exp.50"),
        "",
        t("exp.23", p0=format_local(now)),
        t("exp.51", p0=owner_label),
        "",
    ]

    if left_out:
        lines += [
            t("exp.52", p0=len(left_out)),
            "",
        ]

    if imports_left_out:
        lines += [
            t("exp.53", p0=len(imports_left_out)),
            "",
        ]

    lines += [
        t("exp.54"),
        "",
        t("exp.55", p0=len(listing['experiences']), p1=current_files),
        t("exp.56"),
        t("exp.57", p0=len(listing['recycle-bin']), p1=len(included) - current_files),
        t("exp.58", p0=len(imports)),
    ]

    if directions.explorations or directions.decisions:
        counts = directions.counts()
        lines.append(
            t("exp.59", p0=counts['direction_explorations'], p1=counts['direction_decisions'], p2=counts['direction_recycle_bin'])
        )

    if jobs:
        counts = jobs.counts()
        lines.append(
            t("exp.60", p0=counts['job_postings'], p1=counts['job_analyses'], p2=counts['job_searches'], p3=counts['job_recycle_bin'], p4=counts['job_recommendations'], p5=counts['job_watches'], p6=counts['job_watch_postings'], p7=counts['job_posting_texts'])
        )

    lines += [
        t("exp.61"),
        t("exp.62"),
        "",
    ]

    if left_out:
        lines += [t("exp.63"), ""]
        lines += [
            t("exp.64", p0=entry['filename'], p1=entry['experience_file'], p2=STATUS_TEXT[entry['status']])
            for entry in left_out
        ]
        lines.append("")

    for section, heading in (
        ("experiences", t("exp.65")),
        ("documents", t("exp.66")),
        ("recycle-bin", t("exp.67")),
    ):
        if listing[section]:
            lines += [f"### {heading}", ""]
            lines += [t("exp.68", p0=path, p1=_listing_label(item)) for path, item in listing[section]]
            lines.append("")

    if imports:
        lines += [
            t("exp.69"),
            "",
            t("exp.70"),
            "",
        ]

        for row, entry, draft in imports:
            name = row.filename or t("exp.import_sources.3")
            line = t("exp.71", p0=entry['folder'], p1=name, p2=IMPORT_SOURCES.get(row.source, row.source))
            line += t("exp.72", p0=IMPORT_STATES.get(row.state, row.state), p1=_kept_text(row, draft))

            if entry["original_status"] not in (None, INCLUDED):
                line += t("exp.73", p0=STATUS_TEXT[entry['original_status']])

            if entry["candidates_path"]:
                line += t("exp.74", p0=entry['candidates'])

            lines.append(line + t("exp.75"))

        lines.append("")

    lines += [t("exp.76"), ""]
    lines += [f"- {entry}" for entry in NOT_INCLUDED]
    lines += [
        "",
        t("exp.77"),
        "",
        f"- {SHARE_WARNING}",
        t("exp.78"),
        "",
        t("exp.79"),
        "",
        t("exp.80"),
        "",
    ]

    return "\n".join(lines)


def _listing_label(item: ExperienceItem) -> str:
    title = display_title(item.content, item.category)

    if item.category in DOCUMENT_KINDS:
        return title

    return t("exp.81", p0=CATEGORY_NAMES.get(item.category, item.category), p1=title, p2=state_label(item))


def _document_record(record: ExportRecord | None) -> dict | None:
    return None if record is None else _record_data(record.item, record.versions, record.provenance)


def _item_record(record: ExportRecord) -> dict:
    data = _record_data(record.item, record.versions, record.provenance)
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


def _record_data(item: ExperienceItem, versions: list[ExperienceVersion], provenance=()) -> dict:
    record = {
        "id": item.id,
        "category": item.category,
        "category_label": CATEGORY_LABELS.get(item.category, item.category),
        # Resume imports that added or supplemented it (U2-B2b): events, never the words.
        "import_provenance": [
            {"event": action, "label": EVENT_LABELS.get(action, action), "at": _iso(at)} for action, at in provenance
        ],
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
    # Interface words (lazy messages) are written in the language the export is built in.
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")
