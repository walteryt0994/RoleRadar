"""Chinese labels and time formatting shared by the pages and the exports."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.library.content import (
    ACTIVITY,
    AWARD,
    CATEGORY_FIELDS,
    EDUCATION,
    OTHER,
    PREFERENCES,
    PROFILE,
    PROJECT,
    TITLE_FIELDS,
    WORK,
)
from app.library.models import ItemState, VersionKind
from app.library.projection import NOT_ESTABLISHED

# How the fields of each category are grouped on the form. Every field is in
# exactly one group (a test checks this), so the layout cannot drop one; the
# private note is always shown on its own. A group without a name is shown
# without a heading, where one would only repeat its single field's label.
CATEGORY_GROUP_NAMES = {
    PROJECT: (
        ("基本信息", ("title", "context", "role", "period")),
        ("过程与收获", ("actions", "outcome", "reflection")),
        ("相关资料", ("links",)),
    ),
    EDUCATION: (
        ("学校与专业", ("school", "major", "stage", "graduation")),
        (None, ("notes",)),
    ),
    WORK: (
        ("基本信息", ("organization", "role", "period")),
        ("过程与收获", ("actions", "outcome", "reflection")),
    ),
    ACTIVITY: (
        ("基本信息", ("name", "context", "period")),
        ("参与与收获", ("actions", "reflection")),
    ),
    AWARD: (
        ("基本信息", ("name", "issuer", "period")),
        (None, ("notes",)),
    ),
    OTHER: ((None, ("title", "period", "notes")),),
    PROFILE: ((None, ("name", "phone", "email", "city", "links")),),
}
CATEGORY_GROUPS = {
    category: tuple(
        (label, tuple(_field for name in names for _field in CATEGORY_FIELDS[category] if _field.name == name))
        for label, names in groups
    )
    for category, groups in CATEGORY_GROUP_NAMES.items()
}
# The project groups, as U1-A named them.
FIELD_GROUP_NAMES = CATEGORY_GROUP_NAMES[PROJECT]
FIELD_GROUPS = CATEGORY_GROUPS[PROJECT]
# Form groups that stay folded while they are empty. The folded title and
# its arrow say enough; the field inside carries its own hint.
FOLDED_GROUPS = ("相关资料",)


@dataclass(frozen=True)
class Reading:
    """How the reading page of a category lays out its fields.

    The title, a line of context under it and short facts (meta) head the
    page; highlight gets the neutral outcome block; sections follow as text
    with a quiet heading; folds open on demand.
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


# The reading page reads as a record rather than a form: for a project, what
# came out of the work first, then the user's own part. Education, awards
# and others have no "contribution", so they get none.
READING_LABELS = {
    "outcome": "结果与产出",
    "actions": "我的贡献",
    "reflection": "收获与反思",
    "links": "相关资料",
}
READINGS = {
    PROJECT: Reading("title", "context", ("role", "period"), "outcome", ("actions",), ("reflection", "links"), READING_LABELS),
    EDUCATION: Reading("school", None, ("major", "stage", "graduation"), None, ("notes",), (), {"notes": "补充说明"}),
    WORK: Reading(
        "organization", None, ("role", "period"), "outcome", ("actions",), ("reflection",),
        {"outcome": "结果与产出", "actions": "做过的事", "reflection": "收获与反思"},
    ),
    ACTIVITY: Reading("name", "context", ("period",), None, ("actions",), ("reflection",), {"actions": "参与内容", "reflection": "收获"}),
    AWARD: Reading("name", None, ("issuer", "period"), None, ("notes",), (), {"notes": "相关说明"}),
    OTHER: Reading("title", None, ("period",), None, ("notes",), (), {"notes": "补充"}),
}
READING_ORDER = READINGS[PROJECT].order
# The first of these that is filled in sums up a titled record in the list.
SUMMARY_FIELDS = {
    PROJECT: ("actions", "outcome", "context"),
    EDUCATION: ("major", "stage", "graduation"),
    WORK: ("role", "actions", "outcome"),
    ACTIVITY: ("actions", "context"),
    AWARD: ("issuer", "notes"),
    OTHER: ("notes",),
}
NEW_HEADINGS = {
    PROJECT: "记录一段项目/作品",
    EDUCATION: "记录一段教育经历",
    WORK: "记录一段实习、工作或兼职",
    ACTIVITY: "记录一项课程或实践活动",
    AWARD: "记录一项证书或奖项",
    OTHER: "记录一段其他经历",
}
EDIT_HEADINGS = {
    PROJECT: "编辑项目/作品",
    EDUCATION: "编辑教育经历",
    WORK: "编辑实习、工作或兼职",
    ACTIVITY: "编辑课程或实践活动",
    AWARD: "编辑证书或奖项",
    OTHER: "编辑其他经历",
}
# "Deleting <this>" and "<this> has been cleaned up" on the confirmation and
# recycle-bin pages.
KIND_NOUNS = {
    PROJECT: "这条项目经历",
    EDUCATION: "这条教育经历",
    WORK: "这段实习、工作或兼职经历",
    ACTIVITY: "这项课程或实践活动",
    AWARD: "这项证书或奖项",
    OTHER: "这条其他经历",
    PROFILE: "你的基本资料",
    PREFERENCES: "你的求职偏好",
}
CLEANUP_NOUNS = {
    PROJECT: "一条项目经历",
    EDUCATION: "一条教育经历",
    WORK: "一段实习、工作或兼职经历",
    ACTIVITY: "一项课程或实践活动",
    AWARD: "一项证书或奖项",
    OTHER: "一条其他经历",
    PROFILE: "一份基本资料",
    PREFERENCES: "一份求职偏好",
}
# Where the profile and the preferences are read; experiences have their own page each.
# (Not "/profile": the legacy app had an API there, and this app mounts none of its paths.)
DOCUMENT_PAGES = {PROFILE: "/basic-profile", PREFERENCES: "/job-preferences"}


def record_page(item) -> str:
    """The page that shows this record now."""
    return DOCUMENT_PAGES.get(item.category, f"/experiences/{item.id}")


def format_local(value: datetime | None) -> str:
    """An exact instant in this computer's local time, with its UTC offset."""
    if value is None:
        return "—"

    local = value.astimezone()
    offset = local.strftime("%z")

    return f"{local:%Y-%m-%d %H:%M:%S}（UTC{offset[:3]}:{offset[3:]}）"


def format_short(value: datetime) -> str:
    """Local date and minute, short enough for the record list."""
    return f"{value.astimezone():%Y-%m-%d %H:%M}"


def summary_line(content: dict, category: str = PROJECT) -> str:
    """A first line of what was done, shown under a titled record in the list.

    Untitled records already show their first words as the title, so they
    get no summary.
    """
    if not content.get(TITLE_FIELDS[category]):
        return ""

    for name in SUMMARY_FIELDS[category]:
        value = content.get(name, "")

        if value:
            return value.split("\n", 1)[0]

    return ""


def field_label(category: str, name: str) -> str:
    """How the reading page names a field of this category."""
    reading = READINGS.get(category)

    if reading is not None and name in reading.labels:
        return reading.labels[name]

    return next(field.label for field in CATEGORY_FIELDS[category] if field.name == name)


def unfilled_labels(content: dict, category: str = PROJECT) -> list[str]:
    """The experience fields still empty, in reading order, named as the reading page names them."""
    return [
        field_label(category, name)
        for name in READINGS[category].order
        if not content.get(name)
    ]


def format_size(size: int) -> str:
    """A file size as people read it; the limits on the pages use the same units (1 MB = 1024 KB)."""
    if size < 1024:
        return f"{size} 字节"

    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"

    return f"{size / (1024 * 1024):.1f} MB"


def format_remaining(deadline: datetime, now: datetime) -> str:
    left = deadline - now

    if left <= timedelta(0):
        return "已到期"

    days, seconds = left.days, left.seconds
    hours, minutes = seconds // 3600, (seconds % 3600) // 60

    if days:
        return f"还剩 {days} 天 {hours} 小时"

    if hours:
        return f"还剩 {hours} 小时 {minutes} 分钟"

    return f"还剩 {max(minutes, 1)} 分钟"


def state_label(item) -> str:
    if item.state == ItemState.CONFIRMED:
        return "已确认"

    if item.last_confirmed_at is not None:
        return "已修改，待核对"

    return "待核对"


def version_label(version) -> str:
    if version.kind == VersionKind.INITIAL:
        return "初始版"

    if version.kind == VersionKind.SAVED:
        return f"第 {version.number} 次保存"

    return f"确认版 {version.number}"


EVENT_LABELS = {
    "created": "第一次保存（形成初始版）",
    "saved": "保存修改",
    "confirmed": "确认属实（形成确认版）",
    "deleted": "移到回收站",
    "restored": "从回收站恢复",
    "purge_requested": "申请永久删除",
    "purge_completed": "清理完成",
    "version_deleted": "删除一个历史版本",
    "version_restored": "恢复一个历史版本",
    "version_purge_requested": "申请永久删除一个历史版本",
    "attachment_added": "添加一个附件",
    "attachment_deleted": "删除一个附件",
    "attachment_restored": "恢复一个附件",
    "attachment_purge_requested": "申请永久删除一个附件",
}
# What finished when a clean-up of something other than the record itself completed.
PART_CLEANUP_LABELS = {
    "experience_version": "一个历史版本已清理完成",
    "attachment": "一个附件已清理完成（文件已删除）",
}
# The profile and preferences keep every save; their first save is not an "initial version".
DOCUMENT_EVENT_LABELS = {**EVENT_LABELS, "created": "第一次保存"}


# The modules a record's reference status covers, as the deletion pages name them.
MODULE_LABELS = {"analysis": "分析", "resume": "简历"}


def reference_note(references) -> str:
    """What a deletion page says about other modules: only what the library knows."""
    if references.status != NOT_ESTABLISHED:
        raise ValueError(f"no wording for the reference status {references.status!r}")

    modules = "、".join(MODULE_LABELS[name] for name in references.modules)

    return f"本经历库尚未建立与{modules}的关联，无法列出它们是否用过这些内容；这次删除只处理经历库里的内容。"


def event_label(event, category: str = PROJECT) -> str:
    if event.action == "purge_completed" and event.object_type in PART_CLEANUP_LABELS:
        return PART_CLEANUP_LABELS[event.object_type]

    labels = DOCUMENT_EVENT_LABELS if category in DOCUMENT_PAGES else EVENT_LABELS

    return labels.get(event.action, event.action)
