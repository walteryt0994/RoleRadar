"""What each kind of record contains, and how form input is checked.

There are six categories of experience, plus two records a user keeps at
most one of: the basic profile and the job preferences. Every field is
optional, but at least one must be filled in. An empty field means "not
written yet", never "the user lacks this" or "anything is fine". The private
note of an experience is kept apart from its fields and does not count as
content; the profile and the preferences have no private note.
"""
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.library.errors import ContentError

PROJECT = "project"
EDUCATION = "education"
WORK = "work"
ACTIVITY = "activity"
AWARD = "award"
OTHER = "other"
PROFILE = "profile"
PREFERENCES = "preferences"

# The six categories of experience, in the order the pages offer them.
EXPERIENCE_CATEGORIES = (PROJECT, EDUCATION, WORK, ACTIVITY, AWARD, OTHER)
# Records of which a user has at most one outside the recycle bin.
DOCUMENT_KINDS = (PROFILE, PREFERENCES)
ALL_KINDS = EXPERIENCE_CATEGORIES + DOCUMENT_KINDS

CATEGORY_LABELS = {
    PROJECT: "项目/作品",
    EDUCATION: "教育",
    WORK: "实习/工作/兼职",
    ACTIVITY: "课程与实践活动",
    AWARD: "证书与获奖",
    OTHER: "其他",
    PROFILE: "基本资料",
    PREFERENCES: "求职偏好",
}
NOT_FILLED = "未填写"
# How an empty preference reads: nothing was decided, not "anything goes".
NOT_DECIDED = "尚未想好"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    hint: str
    multiline: bool
    max_length: int
    check: str = "text"


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
EDUCATION_FIELDS = (
    Field("school", "学校", "交换或辅修也可以单独记一条", False, 200),
    Field("major", "专业", "例如：计算机科学与技术", False, 200),
    Field("stage", "学习阶段", "本科、硕士、交换、辅修等，随意写", False, 100),
    Field("graduation", "毕业时间", "预计或实际，例如：2027 年 6 月（预计）", False, 100),
    Field("notes", "补充说明", "课程、成绩、论文或其他想记下的", True, 5000),
)
WORK_FIELDS = (
    Field("organization", "组织", "公司、实验室、团队或个人雇主", False, 200),
    Field("role", "岗位或角色", "例如：数据分析实习生", False, 200),
    Field("period", "时间", "随意写，例如：2026 年暑期；还在进行中", False, 100),
    Field("actions", "做了什么", "只写一句话也可以", True, 5000),
    Field("outcome", "结果或现状", "没完成、没达到预期，也可以如实写", True, 5000),
    Field("reflection", "反思", "学到了什么，下次会怎么做", True, 5000),
)
ACTIVITY_FIELDS = (
    Field("name", "课程或活动名称", "例如：数据结构课程设计、社区义教", False, 200),
    Field("context", "背景", "课程、竞赛、社团、志愿或组织活动", False, 200),
    Field("period", "时间", "随意写，例如：2025 年秋季学期", False, 100),
    Field("actions", "参与内容", "只写一句话也可以", True, 5000),
    Field("reflection", "收获", "学到了什么，或者有什么体会", True, 5000),
)
AWARD_FIELDS = (
    Field("name", "名称", "证书或奖项的名称", False, 200),
    Field("issuer", "颁发方", "学校、机构或比赛主办方", False, 200),
    Field("period", "时间", "获得时间，随意写", False, 100),
    Field("notes", "相关说明", "级别、名次、有效期或其他说明", True, 5000),
)
OTHER_FIELDS = (
    Field("title", "名称或一句话描述", "先记下来，不用想好怎么分类", False, 200),
    Field("period", "时间", "随意写", False, 100),
    Field("notes", "补充", "想到什么写什么", True, 5000),
)
PROFILE_FIELDS = (
    Field("name", "简历上使用的姓名", "可以和登录名不同", False, 100),
    Field("phone", "手机", "以后每份简历可以决定是否写上", False, 30, "phone"),
    Field("email", "邮箱", "以后每份简历可以决定是否写上", False, 200, "email"),
    Field("city", "当前所在城市", "只写城市；求职目标城市写在求职偏好里", False, 100),
    Field("links", "个人链接", "个人主页、作品集等，每行一个", True, 2000),
)

# Each of these preferences can be answered three ways, all optional: what
# would be ideal, what would also be acceptable, and what is not acceptable.
PREFERENCE_STANCES = (("ideal", "理想"), ("ok", "也可以接受"), ("no", "不能接受"))
PREFERENCE_DIMENSIONS = (
    ("cities", "目标城市", "例如：上海、杭州"),
    ("job_types", "求职类型", "全职、实习或兼职，以及更倾向哪种"),
    ("directions", "感兴趣的行业或方向", "例如：教育科技、数据分析"),
    ("work_content", "理想的工作内容", "希望日常多做什么"),
    ("work_style", "工作方式", "协作、节奏、现场或远程、是否出差"),
)
# These two are answered once: one is a soft wish, the other a firm condition.
PREFERENCE_SINGLES = (
    Field("less_content", "希望减少的内容", "不想让日常主要是哪些事", True, 2000),
    Field("must_haves", "现实必要条件", "你明确提出、必须满足的条件，例如入职时间", True, 2000),
)
PREFERENCE_GROUPS = tuple(
    (
        key,
        label,
        tuple(
            Field(f"{key}_{stance}", stance_label, hint if stance == "ideal" else "", True, 1000)
            for stance, stance_label in PREFERENCE_STANCES
        ),
    )
    for key, label, hint in PREFERENCE_DIMENSIONS
)
PREFERENCE_FIELDS = tuple(
    field for _, _, fields in PREFERENCE_GROUPS for field in fields
) + PREFERENCE_SINGLES

CATEGORY_FIELDS = {
    PROJECT: PROJECT_FIELDS,
    EDUCATION: EDUCATION_FIELDS,
    WORK: WORK_FIELDS,
    ACTIVITY: ACTIVITY_FIELDS,
    AWARD: AWARD_FIELDS,
    OTHER: OTHER_FIELDS,
    PROFILE: PROFILE_FIELDS,
    PREFERENCES: PREFERENCE_FIELDS,
}
# The project fields, as U1-A named them.
FIELD_NAMES = tuple(field.name for field in PROJECT_FIELDS)
NOTE_FIELD = Field(
    "private_note",
    "私有备注",
    "只给自己看的提醒；不会进入资料快照或简历，但导出时会包含",
    True,
    5000,
)

# The field that names an experience in lists, and what stands in for it
# while it is empty.
TITLE_FIELDS = {
    PROJECT: "title",
    EDUCATION: "school",
    WORK: "organization",
    ACTIVITY: "name",
    AWARD: "name",
    OTHER: "title",
}
MISSING_TITLES = {
    PROJECT: "（未写标题）",
    EDUCATION: "（未写学校）",
    WORK: "（未写组织）",
    ACTIVITY: "（未写名称）",
    AWARD: "（未写名称）",
    OTHER: "（未写名称）",
}
EMPTY_MESSAGES = {
    PROJECT: "请至少在经历栏目里写一项内容（比如一句话说明做了什么），再保存草稿。",
    EDUCATION: "请至少在经历栏目里写一项内容（比如学校或专业），再保存草稿。",
    WORK: "请至少在经历栏目里写一项内容（比如一句话说明做了什么），再保存草稿。",
    ACTIVITY: "请至少在经历栏目里写一项内容（比如课程或活动名称），再保存草稿。",
    AWARD: "请至少在经历栏目里写一项内容（比如证书或奖项的名称），再保存草稿。",
    OTHER: "请至少在经历栏目里写一项内容（比如一句话描述），再保存草稿。",
    PROFILE: "请至少填写一项再保存。想清空全部基本资料，可以删除它。",
    PREFERENCES: "请至少写一项再保存。想清空全部求职偏好，可以删除它。",
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE = re.compile(r"[\d+()\-　 ]+")


@dataclass(frozen=True)
class ExperienceInput:
    content: dict[str, str]
    private_note: str
    category: str = PROJECT


def parse_experience_form(form, category: str = PROJECT) -> ExperienceInput:
    """Validate submitted form values; raise ContentError if nothing can be saved."""
    if category not in CATEGORY_FIELDS:
        raise ValueError(f"unknown category: {category!r}")

    errors = {}
    content = {
        field.name: _clean(form.get(field.name, ""), field, errors)
        for field in CATEGORY_FIELDS[category]
    }
    has_note = category in EXPERIENCE_CATEGORIES
    note = _clean(form.get(NOTE_FIELD.name, ""), NOTE_FIELD, errors) if has_note else ""

    if errors:
        raise ContentError("有内容没能保存，请按提示修改后再保存。", errors)

    if not any(content.values()):
        message = EMPTY_MESSAGES[category]

        if note:
            message += "私有备注不算经历内容。"

        raise ContentError(message)

    return ExperienceInput(content=content, private_note=note, category=category)


def _clean(raw, field: Field, errors: dict[str, str]) -> str:
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n").strip()

    if any(unicodedata.category(ch) == "Cc" and ch not in "\n\t" for ch in text):
        errors[field.name] = "包含无法保存的控制字符。"
    elif len(text) > field.max_length:
        errors[field.name] = f"最多 {field.max_length} 个字，现在是 {len(text)} 个字。"
    elif text and field.check == "email" and not _EMAIL.fullmatch(text):
        errors[field.name] = "邮箱格式看起来不对，例如 name@example.com。"
    elif text and field.check == "phone" and (
        not _PHONE.fullmatch(text) or sum(ch.isdigit() for ch in text) < 5
    ):
        errors[field.name] = "手机号请只写数字，可以带空格、+、- 和括号。"

    return text


def display_title(content: dict, category: str = PROJECT) -> str:
    """A title for lists, falling back to the first words the user wrote."""
    if category in DOCUMENT_KINDS:
        return CATEGORY_LABELS[category]

    if content.get(TITLE_FIELDS[category]):
        return content[TITLE_FIELDS[category]]

    for field in CATEGORY_FIELDS[category]:
        value = content.get(field.name, "")

        if value:
            first_line = value.split("\n", 1)[0]

            if len(first_line) > 30:
                first_line = first_line[:30] + "…"

            return f"{MISSING_TITLES[category]}{first_line}"

    return MISSING_TITLES[category]


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
