"""The local simulation that turns an import's text into candidates (U2-B2a).

It is a handful of fixed rules, not a model, and it runs in this process:
nothing is sent anywhere. Its answers use the format of candidates.py and
pass the same checks any structurer's would; the pages always say that the
candidates came from it. It does not try to understand a resume, and it is
meant to stay small.

The rules:
- A short line naming a known section (教育经历, Projects, 专业技能 …)
  starts that section; the lines before the first one are the header.
- In the header, an email address, a mainland mobile number, http(s) links
  and labelled values (姓名：…, 城市：…) give the basic profile. A name is
  never guessed from where it stands.
- In an education, work, project or activity section, a line with cells
  (separated by |, a tab or two spaces) or with a date at either end starts
  an entry. A date cell goes to the time field, except that a date range
  in education goes to its notes: it is never guessed into a graduation
  date. The other cells fill the kind's first fields in order. A line that
  cannot be split goes whole into the first field, marked unsplit. The
  lines after it, bullets taken off, go to its description field.
- In an awards section every line is an entry. Skills, job intention and
  other sections become one candidate each, line by line.

Every value is a whole line, cell or labelled value of the text, so
nothing said around it is cut off. Lines the rules cannot place stay only
in the text.
"""
import json
import re
import unicodedata

from app.library.candidates import INTENTION, KIND_UNCLEAR, SKILLS, UNSPLIT, readable_text
from app.library.content import ACTIVITY, AWARD, EDUCATION, OTHER, PROFILE, PROJECT, WORK

SIMULATION_NAME = "simulation:roleradar-resume-rules-1"

HEADINGS = {
    EDUCATION: ("教育背景", "教育经历", "教育", "学历", "education", "academic background"),
    WORK: (
        "实习经历", "工作经历", "实习经验", "工作经验", "实习", "工作", "experience", "work experience",
        "professional experience", "internship", "internships", "employment",
    ),
    PROJECT: (
        "项目经历", "项目经验", "项目", "作品", "科研经历", "研究经历", "projects", "project experience",
        "portfolio", "research experience",
    ),
    ACTIVITY: (
        "校园经历", "社团经历", "活动经历", "课外活动", "实践经历", "社会实践", "志愿经历", "志愿者经历", "课程实践",
        "activities", "leadership", "extracurricular activities", "volunteer experience", "volunteering",
    ),
    AWARD: (
        "获奖情况", "获奖经历", "荣誉奖项", "荣誉", "奖项", "获奖", "证书", "资格证书", "证书与获奖",
        "awards", "honors", "honours", "certifications", "certificates", "awards and honors",
    ),
    SKILLS: ("专业技能", "技能特长", "个人技能", "技能", "skills", "technical skills"),
    INTENTION: ("求职意向", "求职目标", "意向岗位", "objective", "career objective", "job objective"),
    OTHER: ("其他", "其他经历", "others"),
}
# Sections that are not experiences of any kind; what they hold is kept as "other", said to be unclear.
UNCLEAR_HEADINGS = ("自我评价", "个人评价", "个人总结", "兴趣爱好", "summary", "additional information")
# Decoration around a heading: marks, brackets and rules.
DECORATION = " \t#*:：-—_=【】[]（）()·•●■◆▌|｜"

FIRST_FIELDS = {
    EDUCATION: ("school", "major", "stage"),
    WORK: ("organization", "role"),
    PROJECT: ("title", "role", "context"),
    ACTIVITY: ("name", "context"),
    AWARD: ("name", "issuer"),
}
TIME_FIELDS = {EDUCATION: "graduation", WORK: "period", PROJECT: "period", ACTIVITY: "period", AWARD: "period"}
DESCRIPTION_FIELDS = {EDUCATION: "notes", WORK: "actions", PROJECT: "actions", ACTIVITY: "actions", AWARD: "notes"}
SINGLE_LINE = {
    "school", "major", "stage", "graduation", "organization", "role", "period", "title", "context", "name", "issuer",
}
WHOLE_SECTIONS = {SKILLS: "text", INTENTION: "text", OTHER: "notes"}

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_POINT = rf"(?:19|20)\d{{2}}(?:\s*[./\-年]\s*\d{{1,2}}(?:\s*月)?)?|(?:{_MONTHS})[a-z]*\.?\s+(?:19|20)\d{{2}}"
_TO = r"\s*(?:-|–|—|~|～|至|到|to)\s*"
_SPAN = rf"(?:{_POINT})(?:{_TO}(?:{_POINT}|至今|现在|今|present|now|current))?"
_QUALIFIER = r"(?:[（(]\s*(?:预计|预期|expected)?\s*(?:毕业|graduation)?\s*[)）]|(?:预计|预期|expected)\s*(?:毕业|graduation)?)"
DATE_CELL = re.compile(rf"(?:{_QUALIFIER}\s*)?[（(]?{_SPAN}[)）]?(?:\s*{_QUALIFIER})?", re.IGNORECASE)
DATE_RANGE = re.compile(rf"(?:{_POINT}){_TO}(?:{_POINT}|至今|现在|今|present|now|current)", re.IGNORECASE)
EDGE_DATE = (
    re.compile(rf"(?P<date>{DATE_CELL.pattern})\s+(?P<rest>\S.*)", re.IGNORECASE),
    re.compile(rf"(?P<rest>.*\S)\s+(?P<date>{DATE_CELL.pattern})", re.IGNORECASE),
)
CELL_BREAK = re.compile(r"\s*[|｜]\s*|\t+| {2,}|　+")
BULLET = re.compile(r"(?:[-–—•·●▪◦○■□◆◇►▶*]+|\d{1,2}[.)、）]|[（(]\d{1,2}[)）])\s*")
CJK_GAP = re.compile(r"[㐀-鿿]\s+\S|\S\s+[㐀-鿿]")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d+])(?:\+?86[ -]?)?1[3-9]\d(?:[ -]?\d{4}){2}(?!\d)")
LINK = re.compile(r"https?://[^\s，。；、|｜<>\"'（）()]+")
LABELLED = re.compile(
    r"(?:^|(?<=[\s|｜]))(?P<label>姓名|名字|name|所在城市|现居城市|居住城市|现居|城市|location|city)\s*[:：]\s*"
    r"(?P<value>[^|｜\t:：]*?[^|｜\t:：\s])(?=\s{2,}|\s*[|｜\t]|$)",
    re.IGNORECASE,
)
LABEL_FIELDS = {"姓名": "name", "名字": "name", "name": "name"}


def _key(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


SECTIONS = {_key(name): kind for kind, names in HEADINGS.items() for name in names}
UNCLEAR = {_key(name) for name in UNCLEAR_HEADINGS}


class SimulatedStructurer:
    """The local rules, answering in the format of candidates.py."""

    name = SIMULATION_NAME

    def structure(self, segments: list[dict]) -> str:
        return json.dumps({"candidates": structure(segments)}, ensure_ascii=False)


class _Entry:
    def __init__(self, kind: str):
        self.kind = kind
        self.fields: dict[str, list[dict]] = {}
        self.notes: list[str] = []

    def add(self, name: str, segment: int, quote: str) -> None:
        if name in SINGLE_LINE and name in self.fields:
            # A second value for a one-line field reads as part of the description.
            name = DESCRIPTION_FIELDS[self.kind]

        self.fields.setdefault(name, []).append({"segment": segment, "quote": quote})

    def answer(self) -> dict:
        answer = {"kind": self.kind, "fields": self.fields}

        if self.notes:
            answer["notes"] = list(dict.fromkeys(self.notes))

        return answer


def structure(segments: list[dict]) -> list[dict]:
    """The candidates the rules find in these segments, in the order of the text."""
    lines = [
        (number, line.strip())
        for number, segment in enumerate(segments, start=1)
        for line in (readable_text(segment) or "").split("\n")
        if line.strip()
    ]
    header, sections, current = [], [], None

    for number, line in lines:
        heading = _heading(line)

        if heading is not None:
            current = (heading[0], heading[1], [])
            sections.append(current)
        elif current is None:
            header.append((number, line))
        else:
            current[2].append((number, line))

    found = []
    profile = _profile(header)

    if profile is not None:
        found.append(profile.answer())

    for kind, unclear, body in sections:
        if not body:
            continue

        if kind in WHOLE_SECTIONS:
            entry = _Entry(kind)

            for number, line in body:
                entry.add(WHOLE_SECTIONS[kind], number, _without_bullet(line))

            if unclear:
                entry.notes.append(KIND_UNCLEAR)

            found.append(entry.answer())
        elif kind == AWARD:
            found += [_title_entry(kind, number, _without_bullet(line)).answer() for number, line in body]
        else:
            found += [entry.answer() for entry in _entries(kind, body)]

    return found


def _heading(line: str) -> tuple[str, bool] | None:
    """The section a line names, and whether it is one of the unclear ones; None for other lines."""
    core = line.strip().strip(DECORATION)

    if not core or len(core) > 30:
        return None

    key = _key(core)

    if key in SECTIONS:
        return SECTIONS[key], False

    if key in UNCLEAR:
        return OTHER, True

    # Two names of the same section, such as 教育背景 Education.
    parts = re.split(r"\s*[/｜|]\s*|\s+", core, maxsplit=1)

    if len(parts) == 2 and SECTIONS.get(_key(parts[0])) and SECTIONS.get(_key(parts[0])) == SECTIONS.get(_key(parts[1])):
        return SECTIONS[_key(parts[0])], False

    return None


def _without_bullet(line: str) -> str:
    match = BULLET.match(line)
    rest = line[match.end():].strip() if match else ""

    return rest or line


def _starts_entry(line: str) -> bool:
    return len(_cells(line)) > 1 or DATE_CELL.fullmatch(line) is not None or any(
        pattern.fullmatch(line) for pattern in EDGE_DATE
    )


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in CELL_BREAK.split(line) if cell.strip()]


def _entries(kind: str, body: list[tuple[int, str]]) -> list[_Entry]:
    entries = []

    for number, line in body:
        text = _without_bullet(line)
        bulleted = text != line

        if (not bulleted and _starts_entry(text)) or not entries:
            entries.append(_title_entry(kind, number, text))
        else:
            entries[-1].add(DESCRIPTION_FIELDS[kind], number, text)

    return entries


def _title_entry(kind: str, number: int, line: str) -> _Entry:
    """An entry from its first line: cells to fields, a date cell to the time field."""
    entry = _Entry(kind)
    cells = _cells(line)
    separated = len(cells) > 1

    if not separated:
        edge = next((match for pattern in EDGE_DATE if (match := pattern.fullmatch(line))), None)

        if edge is not None:
            cells = [edge["rest"].strip(), edge["date"].strip()]

    firsts = list(FIRST_FIELDS[kind])
    timed = False

    for cell in cells:
        if not timed and DATE_CELL.fullmatch(cell):
            timed = True
            ranged = kind == EDUCATION and DATE_RANGE.search(cell)
            entry.add(DESCRIPTION_FIELDS[kind] if ranged else TIME_FIELDS[kind], number, cell)
        elif firsts:
            entry.add(firsts.pop(0), number, cell)

            # Without separators, words with a gap between them may be several things in one field.
            if not separated and CJK_GAP.search(cell):
                entry.notes.append(UNSPLIT)
        else:
            entry.add(DESCRIPTION_FIELDS[kind], number, cell)

    return entry


def _profile(header: list[tuple[int, str]]) -> _Entry | None:
    """Contact details and labelled values of the header, as a profile candidate."""
    entry = _Entry(PROFILE)

    for number, line in header:
        for match in LABELLED.finditer(line):
            name = LABEL_FIELDS.get(match["label"].casefold(), "city")

            if name not in entry.fields:
                entry.fields[name] = [{"segment": number, "quote": match["value"].strip()}]

        if "email" not in entry.fields and (found := EMAIL.search(line)):
            entry.fields["email"] = [{"segment": number, "quote": found.group()}]

        if "phone" not in entry.fields and (found := PHONE.search(line)):
            entry.fields["phone"] = [{"segment": number, "quote": found.group()}]

        for found in LINK.finditer(line):
            link = found.group().rstrip(".,;:!?")
            pieces = entry.fields.setdefault("links", [])

            if all(piece["quote"] != link for piece in pieces):
                pieces.append({"segment": number, "quote": link})

    return entry if entry.fields else None
