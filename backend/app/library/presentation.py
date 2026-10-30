"""Chinese labels and time formatting shared by the pages and the exports."""
from datetime import datetime, timedelta

from app.library.content import PROJECT_FIELDS
from app.library.models import ItemState, VersionKind

# How the experience fields are grouped on the pages. Every field is in
# exactly one group (a test checks this), so the layout cannot drop one;
# the private note is always shown on its own.
FIELD_GROUP_NAMES = (
    ("基本信息", ("title", "context", "role", "period")),
    ("过程与收获", ("actions", "outcome", "reflection")),
    ("相关资料", ("links",)),
)
_FIELDS_BY_NAME = {field.name: field for field in PROJECT_FIELDS}
FIELD_GROUPS = tuple(
    (label, tuple(_FIELDS_BY_NAME[name] for name in names))
    for label, names in FIELD_GROUP_NAMES
)
# Form groups that stay folded while they are empty. The folded title and
# its arrow say enough; the field inside carries its own hint.
FOLDED_GROUPS = ("相关资料",)
# The reading page reads as a project record rather than a form: what came
# out of the work first, then the user's own part. These are its headings;
# the form keeps the field labels.
READING_LABELS = {
    "outcome": "结果与产出",
    "actions": "我的贡献",
    "reflection": "收获与反思",
    "links": "相关资料",
}
READING_ORDER = ("title", "context", "role", "period", "outcome", "actions", "reflection", "links")


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


def summary_line(content: dict) -> str:
    """A first line of what was done, shown under a titled record in the list.

    Untitled records already show their first words as the title, so they
    get no summary.
    """
    if not content.get("title"):
        return ""

    for name in ("actions", "outcome", "context"):
        value = content.get(name, "")

        if value:
            return value.split("\n", 1)[0]

    return ""


def unfilled_labels(content: dict) -> list[str]:
    """The experience fields still empty, in reading order, named as the reading page names them."""
    return [
        READING_LABELS.get(name, _FIELDS_BY_NAME[name].label)
        for name in READING_ORDER
        if not content.get(name)
    ]


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
}


def event_label(event) -> str:
    if event.action == "purge_completed" and event.object_type == "experience_version":
        return "一个历史版本已清理完成"

    return EVENT_LABELS.get(event.action, event.action)
