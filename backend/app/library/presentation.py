"""Labels and time formatting shared by the pages and the exports, in the page's language (app.i18n).

Stored codes (categories, field names, version kinds, event actions) never change with the language; only the words
shown for them do. The user's own words are never passed through here.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import i18n
from app.i18n import MessageMap, t
from app.library.content import (
    ACTIVITY,
    AWARD,
    BODY,
    BODY_SOURCES,
    CATEGORY_FIELDS,
    EDUCATION,
    MAIN_FIELDS,
    MORE_FIELDS,
    NOTE_FIELD,
    OTHER,
    PREFERENCES,
    PROFILE,
    PROJECT,
    TITLE_FIELDS,
    WORK,
)
from app.library.models import ItemState, VersionKind
from app.library.projection import NOT_ESTABLISHED

# How the fields of each category are grouped where a whole record is listed (a history version, a comparison).
# Every field is in exactly one group (a test checks this), so no page can drop one; the private note is always
# shown on its own. A group without a name is shown without a heading. The names are message keys.
CATEGORY_GROUP_NAMES = {
    PROJECT: (
        ("group.basic", ("title", "context", "role", "period")),
        ("group.body", ("body",)),
        ("group.process", ("actions", "outcome", "reflection")),
        ("group.related", ("links",)),
    ),
    EDUCATION: (
        ("group.school", ("school", "major", "stage", "graduation")),
        ("group.body", ("body",)),
        (None, ("notes",)),
    ),
    WORK: (
        ("group.basic", ("organization", "role", "period")),
        ("group.body", ("body",)),
        ("group.process", ("actions", "outcome", "reflection")),
    ),
    ACTIVITY: (
        ("group.basic", ("name", "context", "period")),
        ("group.body", ("body",)),
        ("group.participation", ("actions", "reflection")),
    ),
    AWARD: (
        ("group.basic", ("name", "issuer", "period")),
        ("group.body", ("body",)),
        (None, ("notes",)),
    ),
    OTHER: (
        (None, ("title", "period")),
        ("group.body", ("body",)),
        (None, ("notes",)),
    ),
    PROFILE: ((None, ("name", "phone", "email", "city", "links", "skills")),),
}
CATEGORY_GROUPS = {
    category: tuple(
        (label, tuple(_field for name in names for _field in CATEGORY_FIELDS[category] if _field.name == name))
        for label, names in groups
    )
    for category, groups in CATEGORY_GROUP_NAMES.items()
}
# The project groups.
FIELD_GROUP_NAMES = CATEGORY_GROUP_NAMES[PROJECT]
FIELD_GROUPS = CATEGORY_GROUPS[PROJECT]
# Groups that stay folded while they are empty.
FOLDED_GROUPS = ("group.related",)


@dataclass(frozen=True)
class Reading:
    """How the reading page of a category lays out its fields.

    The title, a line of context under it and short facts (meta) head the page; the experience details follow as
    the user wrote them; the older split fields a record may still hold are shown under their own names (plan 5.2),
    and the rest folds into 补充信息. labels are message keys for the names the reading page gives fields.
    """

    title: str
    subtitle: str | None
    meta: tuple[str, ...]
    highlight: str | None
    sections: tuple[str, ...]
    folds: tuple[str, ...]
    labels: dict

    @property
    def order(self) -> tuple[str, ...]:
        names = (self.title, self.subtitle, *self.meta, self.highlight, *self.sections, *self.folds)

        return tuple(name for name in names if name)


# The names the reading page gives the older split fields of a project: what came out first, then the user's part.
READING_LABELS = {
    "outcome": "reading.outcome",
    "actions": "reading.actions",
    "reflection": "reading.reflection",
    "links": "reading.links",
}
READINGS = {
    PROJECT: Reading("title", "context", ("role", "period"), BODY, ("outcome", "actions"), ("reflection", "links"), READING_LABELS),
    EDUCATION: Reading("school", None, ("major", "stage", "graduation"), BODY, ("notes",), (), {}),
    WORK: Reading(
        "organization", None, ("role", "period"), BODY, ("outcome", "actions"), ("reflection",),
        {"outcome": "reading.outcome", "actions": "reading.work_actions", "reflection": "reading.reflection"},
    ),
    ACTIVITY: Reading("name", "context", ("period",), BODY, ("actions",), ("reflection",), {}),
    AWARD: Reading("name", None, ("issuer", "period"), BODY, ("notes",), (), {}),
    OTHER: Reading("title", None, ("period",), BODY, ("notes",), (), {}),
}
READING_ORDER = READINGS[PROJECT].order
# The first of these that is filled in sums up a titled record in the list.
SUMMARY_FIELDS = {
    PROJECT: (BODY, "actions", "outcome", "context"),
    EDUCATION: ("major", "stage", "graduation", BODY),
    WORK: ("role", BODY, "actions", "outcome"),
    ACTIVITY: (BODY, "actions", "context"),
    AWARD: ("issuer", BODY, "notes"),
    OTHER: (BODY, "notes"),
}
NEW_HEADINGS = MessageMap({category: f"heading.new.{category}" for category in CATEGORY_GROUP_NAMES if category != PROFILE})
EDIT_HEADINGS = MessageMap({category: f"heading.edit.{category}" for category in CATEGORY_GROUP_NAMES if category != PROFILE})
# "Deleting <this>" and "<this> has been cleaned up" on the confirmation and recycle-bin pages.
KIND_NOUNS = MessageMap({kind: f"noun.this.{kind}" for kind in (PROJECT, EDUCATION, WORK, ACTIVITY, AWARD, OTHER, PROFILE, PREFERENCES)})
CLEANUP_NOUNS = MessageMap({kind: f"noun.one.{kind}" for kind in (PROJECT, EDUCATION, WORK, ACTIVITY, AWARD, OTHER, PROFILE, PREFERENCES)})
# Where the profile and the preferences are read; experiences have their own page each.
# (Not "/profile": the legacy app had an API there, and this app mounts none of its paths.)
DOCUMENT_PAGES = {PROFILE: "/basic-profile", PREFERENCES: "/job-preferences"}
# The categories' names on pages, in the page's language (content.CATEGORY_LABELS keeps the stored Chinese names
# that exports' data and model requests use).
CATEGORY_NAMES = MessageMap({kind: f"category.{kind}" for kind in (PROJECT, EDUCATION, WORK, ACTIVITY, AWARD, OTHER, PROFILE, PREFERENCES)})


def record_page(item) -> str:
    """The page that shows this record now."""
    return DOCUMENT_PAGES.get(item.category, f"/experiences/{item.id}")


def section_of(item) -> str:
    """The part of 我的资料 that a record's pages belong to in the navigation."""
    return item.category if item.category in DOCUMENT_PAGES else "library"


def format_local(value: datetime | None) -> str:
    """An exact instant in this computer's local time, with its UTC offset (written in the page's language)."""
    return i18n.format_local(value)


def format_short(value: datetime) -> str:
    """Local date and minute, short enough for the record list."""
    return i18n.format_short(value)


def summary_line(content: dict, category: str = PROJECT) -> str:
    """A first line of what was done, shown under a titled record in the list.

    Untitled records already show their first words as the title, so they get no summary.
    """
    if not content.get(TITLE_FIELDS[category]):
        return ""

    for name in SUMMARY_FIELDS[category]:
        value = content.get(name, "")

        if value:
            return value.split("\n", 1)[0]

    return ""


def label_of(category: str, name: str) -> str:
    """A field's name on forms, in the page's language: the experience details, the private note, or the field."""
    if name == BODY:
        return t("field.body")

    if name == NOTE_FIELD.name:
        return t("field.note")

    if category == PREFERENCES:
        return preference_label(name)

    key = f"field.{category}.{name}"

    return t(key) if i18n.has(key) else name


def hint_of(category: str, name: str) -> str:
    """A field's hint on forms, in the page's language ("" when it has none)."""
    if name == BODY:
        return t("hint.body")

    if name == NOTE_FIELD.name:
        return t("hint.note")

    if category == PREFERENCES:
        key = f"hint.preferences.{name}"

        return t(key) if i18n.has(key) else ""

    key = f"hint.{category}.{name}"

    return t(key) if i18n.has(key) else ""


def preference_label(name: str) -> str:
    """A preference field's name: its stance (理想 / 也可以接受 / 不能接受) or one of the two single ones."""
    for stance in ("ideal", "ok", "no"):
        if name.endswith(f"_{stance}"):
            return t(f"pref.stance.{stance}")

    key = f"field.preferences.{name}"

    return t(key) if i18n.has(key) else name


def preference_topic(name: str) -> str:
    """A preference field as an analysis names it, in the page's language: its topic, and for a dimension its stance
    in brackets ("目标城市（理想）" / "Target cities (Ideal)"); the two single fields by their own names."""
    topic, stance = preference_parts(name)

    return t("pref.topic_stance", topic=topic, stance=stance) if stance else topic


def preference_parts(name: str) -> tuple[str, str | None]:
    """A preference field's topic and, for a dimension, its stance, both in the page's language; a single field is
    its own name with no stance. (The words an analysis prompt uses are its own and stay as they are.)"""
    for stance in ("ideal", "ok", "no"):
        suffix = f"_{stance}"

        if name.endswith(suffix):
            key = f"pref.group.{name[: -len(suffix)]}"

            return (t(key) if i18n.has(key) else name), t(f"pref.stance.{stance}")

    return preference_label(name), None


def group_label(key: str | None) -> str:
    return t(key) if key else ""


def field_label(category: str, name: str) -> str:
    """How the reading page names a field of this category (in the page's language)."""
    reading = READINGS.get(category)

    if reading is not None and name in reading.labels:
        return t(reading.labels[name])

    return label_of(category, name)


def shown_fields(content: dict, category: str) -> list[str]:
    """The fields a whole record or version is read with (plan 5.2): every one but the experience details and the
    older split fields, which show when they hold text (and the details also when nothing else holds any)."""
    sources = BODY_SOURCES.get(category, ())
    split_text = any(content.get(name) for name in sources)
    names = []

    for field in CATEGORY_FIELDS[category]:
        if field.name in sources and not content.get(field.name):
            continue

        if field.name == BODY and not content.get(BODY) and split_text:
            continue

        names.append(field.name)

    return names


def unfilled_labels(content: dict, category: str = PROJECT) -> list[str]:
    """The fields still empty that the reading page names, in reading order: the metadata, and the experience
    details when no text of the record is there yet. The older split fields are never asked for (plan 5.2)."""
    sources = BODY_SOURCES.get(category, ())
    has_text = bool(content.get(BODY)) or any(content.get(name) for name in sources)
    wanted = [*MAIN_FIELDS.get(category, ()), *MORE_FIELDS.get(category, ())]
    labels = []

    for name in READINGS[category].order:
        if name in sources or content.get(name):
            continue

        if name == BODY:
            if not has_text:
                labels.append(field_label(category, name))
        elif name in wanted:
            labels.append(field_label(category, name))

    return labels


def format_size(size: int) -> str:
    """A file size as people read it; the limits on the pages use the same units (1 MB = 1024 KB)."""
    if size < 1024:
        return t("size.bytes", n=size)

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    return f"{size / (1024 * 1024):.1f} MB"


def format_remaining(deadline: datetime, now: datetime) -> str:
    left = deadline - now

    if left <= timedelta(0):
        return t("time.expired")

    days, seconds = left.days, left.seconds
    hours, minutes = seconds // 3600, (seconds % 3600) // 60

    if days:
        return t("time.days_hours_left", days=days, hours=hours)

    if hours:
        return t("time.hours_minutes_left", hours=hours, minutes=minutes)

    return t("time.minutes_left", minutes=max(minutes, 1))


def state_label(item) -> str:
    if item.state == ItemState.CONFIRMED:
        return t("state.confirmed")

    if item.last_confirmed_at is not None:
        return t("state.review_changes")

    return t("state.needs_review")


def version_label(version) -> str:
    if version.kind == VersionKind.INITIAL:
        return t("version.initial")

    if version.kind == VersionKind.SAVED:
        return t("version.saved", n=version.number)

    return t("version.confirmed", n=version.number)


EVENT_LABELS = MessageMap(
    {
        action: f"event.{action}"
        for action in (
            "created",
            "saved",
            "confirmed",
            "deleted",
            "restored",
            "purge_requested",
            "purge_completed",
            "version_deleted",
            "version_restored",
            "version_purge_requested",
            "attachment_added",
            "attachment_deleted",
            "attachment_restored",
            "attachment_purge_requested",
            # Written at the user's choice from a resume import (U2-B2b): an event, never the words.
            "adopted_from_import",
            "supplemented_from_import",
        )
    }
)
# What finished when a clean-up of something other than the record itself completed.
PART_CLEANUP_LABELS = MessageMap({"experience_version": "event.part.experience_version", "attachment": "event.part.attachment"})
# The profile and preferences keep every save; their first save is not an "initial version".
DOCUMENT_EVENT_LABELS = EVENT_LABELS.merged({"created": "event.document_created"})


# The modules a record's reference status covers, as the deletion pages name them.
MODULE_LABELS = MessageMap({"analysis": "module.analysis", "resume": "module.resume"})


def reference_note(references, *, purge: bool = False) -> str:
    """What a deletion page says about other modules: only what the library knows.

    Saved direction explorations and direction decisions keep references (U3-C1): those resting on the
    record are counted (nothing is said of them when there are none; the deletion pages list the
    explorations and offer to delete them along, U3-C3b); the modules that keep no references are said to
    be unknown, never "not used".
    """
    if references.status != NOT_ESTABLISHED:
        raise ValueError(f"no wording for the reference status {references.status!r}")

    modules = i18n.list_join(MODULE_LABELS[name] for name in references.modules)

    return (
        f"{direction_note(references, purge=purge)}"
        f"{analysis_note(references, purge=purge)}"
        + t("refs.unsaved_and_unknown", modules=modules)
    )


def analysis_note(references, *, purge: bool = False, version: bool = False) -> str:
    """What deleting a record, or one version of it, does to the analyses kept from a search resting on it (U3-D3b).

    As for explorations: nothing of them is deleted along by default; they stay to be looked at, their basis marked,
    and are never used for anything new. Purging the record does not remove the quotes and the context they hold,
    and the page says so. With none resting on it, nothing is said.
    """
    count = len(references.analyses)

    if not count:
        return ""

    note = t("refs.analyses_version" if version else "refs.analyses", n=count)

    if purge:
        note += t("refs.analyses_purge")

    return note


def direction_note(references, *, purge: bool = False, version: bool = False) -> str:
    """What deleting a record, or one version of it, does to the direction records resting on it (U3-C1).

    Nothing of them is deleted along by default: they stay to be looked at, marked, and out of new use.
    Purging the record does not remove the quotes a kept exploration holds, and the page says so. With
    none resting on it, nothing is said.
    """
    explorations, decisions = len(references.explorations), len(references.decisions)

    if not explorations and not decisions:
        return ""

    counted = [t("refs.count_explorations", n=explorations)] * bool(explorations) + [
        t("refs.count_decisions", n=decisions)
    ] * bool(decisions)
    note = t("refs.directions_version" if version else "refs.directions", counted=i18n.list_join(counted))

    if purge and explorations:
        note += t("refs.directions_purge")

    return note


def event_label(event, category: str = PROJECT) -> str:
    if event.action == "purge_completed" and event.object_type in PART_CLEANUP_LABELS:
        return PART_CLEANUP_LABELS[event.object_type]

    labels = DOCUMENT_EVENT_LABELS if category in DOCUMENT_PAGES else EVENT_LABELS

    return labels.get(event.action, event.action)
