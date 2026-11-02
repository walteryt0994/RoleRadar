"""Writing a checked candidate into the user's own records, at their explicit choice (U2-B2b).

A candidate waiting on the review page can be written one of three ways:
as a new experience (a draft, never confirmed on its own), into chosen
fields of an experience of its kind that is there already, or into chosen
fields of the basic profile, which is made for it if there is none. The
skills a resume lists go into the profile's own 自述技能 field only: the
user's words about themselves, never an experience or evidence of practice
(the job analysis does not read the profile). The old job intention is no
candidate for this: it stays for reference.

Every write is one transaction that first checks everything again: the
candidate is the owner's, its import open and unexpired, the candidate
still waiting and as the page showed it; the record is the one the page
showed (its id, not only its revision), the owner's, of the candidate's
kind, not in the recycle bin and as the page showed it. Then
the record is saved from what is stored, with only the chosen fields
changed: its other fields, its private note, its attachments and its
history stay. An event in the record's history says it was written from a
resume import, the ImportAdoption row says where and which fields, and the
candidate is marked adopted. Any failure undoes all of it. Nothing chosen,
or a choice that would leave the record as it is, writes nothing and
leaves the candidate waiting.

A candidate is written at most once. A second request, from the same page
or another, passes the same checks and then finds it adopted: it writes
nothing and says where it went. From then on the record lives by its own
rules; ending the import removes the candidate and its ImportAdoption,
never the record.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library.candidates import CANDIDATE_FIELDS, SKILLS, current_values
from app.library.content import CATEGORY_FIELDS, EXPERIENCE_CATEGORIES, NOTE_FIELD, PROFILE, parse_experience_form
from app.library.db import Database
from app.library.errors import NotFound
from app.library.models import AdoptionAction, CandidateState, ExperienceItem, ImportAdoption, ImportCandidate
from app.library.review import (
    ADOPTED_FROM_IMPORT,
    DIFFERS,
    FILLS,
    SUPPLEMENTED_FROM_IMPORT,
    field_state,
    open_candidate,
    record_kind,
)
from app.library.service import (
    active_item,
    create_document,
    create_experience,
    current_document,
    new_id,
    record_event,
    save_draft,
)

KEEP = "keep"
FILL = "fill"
REPLACE = "replace"
APPEND = "append"
CHOICES = (KEEP, FILL, REPLACE, APPEND)
# Where each kind's candidate fields go in its record: the same names, except the skills a resume
# lists, which go into the profile's own field and nowhere else. The job intention goes nowhere.
TARGETS = {
    **{kind: {entry.name: entry.name for entry in CANDIDATE_FIELDS[kind]} for kind in EXPERIENCE_CATEGORIES},
    PROFILE: {entry.name: entry.name for entry in CANDIDATE_FIELDS[PROFILE]},
    SKILLS: {"text": "skills"},
}
PROFILE_KINDS = (PROFILE, SKILLS)
# Why a candidate cannot be written as asked.
LEFT_OUT = "left_out"
WRONG_KIND = "kind"
BAD_CHOICE = "choice"


class NothingChosen(Exception):
    """No field was chosen: nothing is written, and the candidate keeps waiting."""


class NoChange(Exception):
    """What was chosen would leave the record as it is: nothing is written."""


class NotAdoptable(Exception):
    """The candidate cannot be written this way: left out, of another kind, or a choice the page never offered."""

    def __init__(self, reason: str):
        self.reason = reason


class TargetGone(Exception):
    """The record chosen is not the owner's, not of the candidate's kind, in the recycle bin or gone."""


class AdoptionConflict(Exception):
    """The candidate or the record changed after the page was shown; nothing was written."""

    def __init__(self, what: str):
        # "candidate", "target", or "profile": the profile shown is not the one there now
        self.what = what


@dataclass(frozen=True)
class Outcome:
    # False when an earlier request had written it: nothing new was written now.
    written: bool
    item_id: str
    action: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class FieldRow:
    """One field a candidate could write: its words, the record's words, and what may be done."""

    source: str
    target: str
    label: str
    multiline: bool
    value: str
    existing: str
    state: str
    # What the page offers besides keeping the field as it is.
    actions: tuple[str, ...]


def field_rows(kind: str, values: dict[str, str], content: dict) -> list[FieldRow]:
    """The candidate's filled fields against the record's content, with what each allows."""
    entries = {entry.name: entry for entry in CATEGORY_FIELDS[record_kind(kind)]}
    rows = []

    for source, target in TARGETS[kind].items():
        value = values.get(source, "")

        if not value:
            continue

        entry = entries[target]
        existing = content.get(target, "")
        state = field_state(entry, value, existing)

        if state == FILLS:
            actions = (FILL,)
        elif state == DIFFERS:
            actions = (REPLACE, APPEND) if entry.multiline else (REPLACE,)
        else:
            # The same, or already there line by line: nothing to write.
            actions = ()

        rows.append(FieldRow(source, target, entry.label, entry.multiline, value, existing, state, actions))

    return rows


def merged_content(kind: str, rows: list[FieldRow], choices: dict[str, str], content: dict) -> tuple[dict, list[str]]:
    """The record's content with the chosen fields written, and their names; the rest as stored.

    A choice for a field the kind does not write, or one the page did not
    offer for that field, is refused (the page never sends it).
    """
    offered = {row.source: row for row in rows}

    for source, action in choices.items():
        if source not in TARGETS[kind] or action not in CHOICES:
            raise NotAdoptable(BAD_CHOICE)

        if action != KEEP and (source not in offered or action not in offered[source].actions):
            raise NotAdoptable(BAD_CHOICE)

    merged, written = dict(content), []

    for row in rows:
        action = choices.get(row.source, KEEP)

        if action == KEEP:
            continue

        merged[row.target] = f"{row.existing}\n{row.value}" if action == APPEND else row.value
        written.append(row.target)

    if not written:
        raise NothingChosen()

    return merged, written


def adopted_already(db: Session, candidate: ImportCandidate) -> Outcome:
    adoption = db.scalar(select(ImportAdoption).where(ImportAdoption.candidate_id == candidate.id))

    if adoption is None:
        raise NotFound()

    return Outcome(False, adoption.item_id, adoption.action, tuple(adoption.fields))


def _waiting(
    db: Session, owner_id: str, import_id: str, candidate_id: str, expected_revision: int, kinds, now: datetime
) -> tuple[ImportCandidate, Outcome | None]:
    """The candidate, if it may be written now, or how an earlier request wrote it.

    The owner and the import's validity are checked before anything else,
    so a repeat never skips them.
    """
    _, candidate = open_candidate(db, owner_id, import_id, candidate_id, now)

    if candidate.state == CandidateState.ADOPTED:
        return candidate, adopted_already(db, candidate)

    if candidate.state != CandidateState.PENDING:
        raise NotAdoptable(LEFT_OUT)

    if candidate.kind not in kinds:
        raise NotAdoptable(WRONG_KIND)

    if candidate.revision != expected_revision:
        raise AdoptionConflict("candidate")

    return candidate, None


def _adopted(db: Session, owner_id: str, candidate: ImportCandidate, item_id: str, action: str, fields, now: datetime) -> Outcome:
    db.add(
        ImportAdoption(
            id=new_id(),
            owner_id=owner_id,
            import_id=candidate.import_id,
            candidate_id=candidate.id,
            item_id=item_id,
            action=action,
            fields=list(fields),
            created_at=now,
        )
    )
    candidate.state = CandidateState.ADOPTED
    candidate.revision += 1
    candidate.updated_at = now

    return Outcome(True, item_id, action, tuple(fields))


def experience_target(db: Session, owner_id: str, item_id: str, kind: str) -> ExperienceItem:
    """An experience of this owner and kind that is not in the recycle bin; TargetGone otherwise."""
    try:
        return active_item(db, owner_id, item_id, kinds=(kind,))
    except NotFound:
        raise TargetGone() from None


# --- the three ways --------------------------------------------------------------------------


def adopt_as_new(
    database: Database, owner_id: str, import_id: str, candidate_id: str, expected_revision: int, now: datetime
) -> Outcome:
    """Write an experience candidate as a new draft experience, as it stands now."""
    with database.transaction() as db:
        candidate, done = _waiting(db, owner_id, import_id, candidate_id, expected_revision, EXPERIENCE_CATEGORIES, now)

        if done is not None:
            return done

        values = current_values(candidate.kind, candidate.extracted, candidate.edits)
        entry = parse_experience_form({**values, NOTE_FIELD.name: ""}, candidate.kind)
        item = create_experience(db, owner_id, entry, now)
        record_event(db, owner_id, item.id, "experience", item.id, ADOPTED_FROM_IMPORT, now)
        written = [name for name, value in entry.content.items() if value]

        return _adopted(db, owner_id, candidate, item.id, AdoptionAction.NEW, written, now)


def supplement(
    database: Database,
    owner_id: str,
    import_id: str,
    candidate_id: str,
    expected_revision: int,
    target_id: str,
    expected_target_revision: int,
    choices: dict[str, str],
    now: datetime,
) -> Outcome:
    """Write the chosen fields of an experience candidate into an experience that is there."""
    with database.transaction() as db:
        candidate, done = _waiting(db, owner_id, import_id, candidate_id, expected_revision, EXPERIENCE_CATEGORIES, now)

        if done is not None:
            return done

        target = experience_target(db, owner_id, target_id, candidate.kind)

        if target.revision != expected_target_revision:
            raise AdoptionConflict("target")

        values = current_values(candidate.kind, candidate.extracted, candidate.edits)
        rows = field_rows(candidate.kind, values, target.content)
        merged, written = merged_content(candidate.kind, rows, choices, target.content)
        # The whole record is checked as it would be saved; its private note stays as it is.
        entry = parse_experience_form({**merged, NOTE_FIELD.name: target.private_note}, candidate.kind)
        _, changed = save_draft(db, owner_id, target.id, expected_target_revision, entry, now)

        if not changed:
            raise NoChange()

        record_event(db, owner_id, target.id, "experience", target.id, SUPPLEMENTED_FROM_IMPORT, now)

        return _adopted(db, owner_id, candidate, target.id, AdoptionAction.SUPPLEMENT, written, now)


def into_profile(
    database: Database,
    owner_id: str,
    import_id: str,
    candidate_id: str,
    expected_revision: int,
    shown_profile: tuple[str, int] | None,
    choices: dict[str, str],
    now: datetime,
) -> Outcome:
    """Write the chosen fields of a profile or skills candidate into the basic profile.

    shown_profile is the id and revision of the profile the page showed, or
    None when it showed none: then one is made, unless another request made
    one meanwhile (a conflict, so nothing it saved is overwritten). A shown
    profile must still be that very profile: one deleted since, or deleted
    and made again (its revision starts at 1 again), is another record, so
    nothing is written into it and none is made in its place.
    """
    with database.transaction() as db:
        candidate, done = _waiting(db, owner_id, import_id, candidate_id, expected_revision, PROFILE_KINDS, now)

        if done is not None:
            return done

        profile = current_document(db, owner_id, PROFILE)

        if shown_profile is None:
            if profile is not None:
                raise AdoptionConflict("target")
        else:
            shown_id, shown_revision = shown_profile

            if profile is None or profile.id != shown_id:
                raise AdoptionConflict("profile")

            if profile.revision != shown_revision:
                raise AdoptionConflict("target")

        content = profile.content if profile is not None else {}
        values = current_values(candidate.kind, candidate.extracted, candidate.edits)
        merged, written = merged_content(candidate.kind, field_rows(candidate.kind, values, content), choices, content)
        entry = parse_experience_form(merged, PROFILE)

        if profile is None:
            item = create_document(db, owner_id, entry, now)
            record_event(db, owner_id, item.id, "experience", item.id, ADOPTED_FROM_IMPORT, now)
        else:
            item, changed = save_draft(db, owner_id, profile.id, shown_revision, entry, now)

            if not changed:
                raise NoChange()

            record_event(db, owner_id, item.id, "experience", item.id, SUPPLEMENTED_FROM_IMPORT, now)

        return _adopted(db, owner_id, candidate, item.id, AdoptionAction.PROFILE, written, now)
