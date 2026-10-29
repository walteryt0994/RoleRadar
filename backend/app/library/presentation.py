"""Chinese labels and time formatting shared by the pages and the exports."""
from datetime import datetime, timedelta

from app.library.models import ItemState, VersionKind


def format_local(value: datetime | None) -> str:
    """An exact instant in this computer's local time, with its UTC offset."""
    if value is None:
        return "—"

    local = value.astimezone()
    offset = local.strftime("%z")

    return f"{local:%Y-%m-%d %H:%M:%S}（UTC{offset[:3]}:{offset[3:]}）"


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
