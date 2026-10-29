"""What a project/portfolio experience contains, and how form input is checked.

Every field is optional, but at least one must be filled in. An empty field
means "not written yet", never "the user lacks this". The private note is
kept apart from the experience fields and does not count as content.
"""
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.library.errors import ContentError

PROJECT = "project"
CATEGORY_LABELS = {PROJECT: "项目/作品"}
NOT_FILLED = "未填写"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    hint: str
    multiline: bool
    max_length: int


PROJECT_FIELDS = (
    Field("title", "标题", "例如：课程大作业：校园二手书交换小程序", False, 200),
    Field("context", "场景或组织", "课程、社团、公司、比赛或个人尝试", False, 200),
    Field("role", "我的角色", "例如：负责后端接口，和两位同学合作", False, 200),
    Field("period", "时间", "随意写，例如：2025 年春季学期；还在进行中", False, 100),
    Field("actions", "做了什么", "只写一句话也可以", True, 5000),
    Field("outcome", "结果或现状", "没完成、没达到预期，也可以如实写", True, 5000),
    Field("reflection", "反思", "学到了什么，下次会怎么做", True, 5000),
    Field("links", "相关链接", "每行一个，例如作品或代码地址", True, 2000),
)
FIELD_NAMES = tuple(field.name for field in PROJECT_FIELDS)
NOTE_FIELD = Field(
    "private_note",
    "私有备注",
    "只给自己看的提醒；不会进入资料快照或简历，但导出时会包含",
    True,
    5000,
)


@dataclass(frozen=True)
class ExperienceInput:
    content: dict[str, str]
    private_note: str


def parse_experience_form(form) -> ExperienceInput:
    """Validate submitted form values; raise ContentError if nothing can be saved."""
    errors = {}
    content = {
        field.name: _clean(form.get(field.name, ""), field, errors)
        for field in PROJECT_FIELDS
    }
    note = _clean(form.get(NOTE_FIELD.name, ""), NOTE_FIELD, errors)

    if errors:
        raise ContentError("有内容没能保存，请按提示修改后再保存。", errors)

    if not any(content.values()):
        message = "请至少在经历栏目里写一项内容（比如一句话说明做了什么），再保存草稿。"

        if note:
            message += "私有备注不算经历内容。"

        raise ContentError(message)

    return ExperienceInput(content=content, private_note=note)


def _clean(raw, field: Field, errors: dict[str, str]) -> str:
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()

    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in text):
        errors[field.name] = "包含无法保存的控制字符。"
    elif len(text) > field.max_length:
        errors[field.name] = f"最多 {field.max_length} 个字，现在是 {len(text)} 个字。"

    return text


def display_title(content: dict) -> str:
    """A title for lists, falling back to the first words the user wrote."""
    if content.get("title"):
        return content["title"]

    for name in FIELD_NAMES:
        value = content.get(name, "")

        if value:
            first_line = value.split("\n", 1)[0]

            if len(first_line) > 30:
                first_line = first_line[:30] + "…"

            return f"（未写标题）{first_line}"

    return "（未写标题）"


def link_lines(text: str) -> list[tuple[str, str | None]]:
    """Each non-empty line, paired with an href only if it is a plain http(s) URL."""
    lines = []

    for line in text.split("\n"):
        line = line.strip()

        if line:
            lines.append((line, line if is_safe_link(line) else None))

    return lines


def is_safe_link(value: str) -> bool:
    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        return False

    try:
        parts = urlsplit(value)
    except ValueError:
        return False

    return parts.scheme in ("http", "https") and bool(parts.netloc)
