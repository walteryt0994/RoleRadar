"""What a structuring answer may say about an import, and how it is checked (U2-B2a).

A structurer reads the segments of one import (what resume_reader found:
pages, paragraphs, tables or pasted text) and answers with JSON:

    {"candidates": [
        {"kind": "work",
         "fields": {"organization": [{"segment": 2, "quote": "合成数据科技有限公司"}],
                    "actions": [{"segment": 2, "quote": "参与搭建销售看板"},
                                {"segment": 2, "quote": "负责每周的数据清洗"}]},
         "notes": ["unsplit"]}]}

A kind is one of the six experience categories, the basic profile, or one
of two kinds kept for reference only: the skills a resume lists, which stay
the user's own description and never become evidence, and the job
intention it stated, which never replaces the current preferences. The
fields are the library's own fields of each kind.

Every value is made of pieces, each the exact words of one segment, so a
value that was found automatically can always be checked against the text.
check_answer keeps a piece only where its words stand in its segment, and
only if nothing that changes their meaning was cut off around them: a
negation, team work or a plan said in the same sentence or cell but outside
the piece, or an uncertain date next to a piece that holds a date, drops
the candidate. The same words inside a piece are kept and become cautions.
These are short word lists, not a reading of the text: a candidate that
passes is not shown to be right, and no page says it was verified.

A candidate with any piece that fails is dropped whole, with its position,
the segments it pointed at and the reason; the others are kept. An answer
that is not this JSON at all, or holds more than MAX_CANDIDATES, is
refused whole. What the user writes over a value is kept apart from it
(parse_edits): it need not stand in the text, and it is never a quote.
"""
import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Protocol

from app.library.content import (
    CATEGORY_FIELDS,
    CATEGORY_LABELS,
    EXPERIENCE_CATEGORIES,
    PROFILE,
    Field,
    _clean,
)
from app.library.errors import ContentError
from app.library.imports import locator, segment_text
from app.outbound_review import MASK_START

CONTRACT_VERSION = "1"
# A resume holds a few dozen entries at most; the bound keeps a page and its rows small.
MAX_CANDIDATES = 60

SKILLS = "skills"
INTENTION = "intention"
# Neither experiences nor profile fields of their own. Skills go into the profile's 自述技能 field only
# when the user chooses so (U2-B2b), never as evidence; the job intention is never written anywhere.
REFERENCE_KINDS = (SKILLS, INTENTION)
# In the order the review page groups them.
CANDIDATE_KINDS = (PROFILE, *EXPERIENCE_CATEGORIES, *REFERENCE_KINDS)
# The profile fields a structurer may fill, fixed as U2-B2a agreed them: the profile has grown a
# field of its own since (自述技能), which only the skills kind feeds, by the user's choice.
PROFILE_CANDIDATE_FIELDS = ("name", "phone", "email", "city", "links")
CANDIDATE_FIELDS = {
    PROFILE: tuple(entry for entry in CATEGORY_FIELDS[PROFILE] if entry.name in PROFILE_CANDIDATE_FIELDS),
    **{kind: CATEGORY_FIELDS[kind] for kind in EXPERIENCE_CATEGORIES},
    SKILLS: (Field("text", "技能（自述）", "简历里列出的技能，只是你的自述", True, 5000),),
    INTENTION: (Field("text", "当时的求职意向", "简历里当时写的求职意向，不代表现在的偏好", True, 2000),),
}
KIND_LABELS = {
    **{kind: CATEGORY_LABELS[kind] for kind in (PROFILE, *EXPERIENCE_CATEGORIES)},
    SKILLS: "技能（自述）",
    INTENTION: "当时的求职意向",
}

# Why one candidate of an answer was dropped.
FORMAT = "format"
UNKNOWN_KIND = "unknown_kind"
UNKNOWN_FIELD = "unknown_field"
BAD_SEGMENT = "bad_segment"
NOT_IN_SOURCE = "not_in_source"
NOT_ONE_LINE = "not_one_line"
TOO_LONG = "too_long"
CUT_CONTEXT = "cut_context"
# Only for an outside service's answer (U2-C3): it quoted a placeholder, or gave a basic profile.
MASKED = "masked"
LOCAL_ONLY = "local_only"
PROBLEM_TEXTS = {
    FORMAT: "格式不符合约定",
    UNKNOWN_KIND: "类别不是可以整理的类别",
    UNKNOWN_FIELD: "有一栏不属于这个类别",
    BAD_SEGMENT: "指向的原文位置不存在，或那里没有读到文字",
    NOT_IN_SOURCE: "引用的文字在原文里找不到",
    NOT_ONE_LINE: "只能写一行的栏目里有多行",
    TOO_LONG: "超过了这一栏的字数上限",
    CUT_CONTEXT: "引用截掉了同一句里的否定、团队、计划或不确定时间的说法",
    MASKED: "引用了发送前已经隐去的内容",
    LOCAL_ONLY: "基本资料只由本机规则整理，外部服务给出的这一条不采用",
}

# Why an answer gave nothing to keep. SERVICE_FAILED is said by review.py.
INVALID_ANSWER = "invalid_answer"
TOO_MANY = "too_many"
NOTHING_FOUND = "nothing_found"
NONE_VALID = "none_valid"
SERVICE_FAILED = "service_failed"
FAILURE_TEXTS = {
    INVALID_ANSWER: "整理结果不是约定的格式，什么都没有保存。",
    TOO_MANY: f"整理结果超过了 {MAX_CANDIDATES} 条的上限，什么都没有保存。可以只粘贴简历里需要的部分，重新导入后再整理。",
    NOTHING_FOUND: "没有整理出任何条目。模拟规则只认识带“教育经历”“项目经历”“实习经历”等标题的简历。",
    NONE_VALID: "整理出的条目都没有通过原文核对，什么都没有保存。",
    SERVICE_FAILED: "整理时出了错，什么都没有保存。",
}

# Cautions: what the text around a value calls for care about.
NEGATED = "negated"
TEAM = "team"
PLAN = "plan"
UNCERTAIN_DATE = "uncertain_date"
# Said by the structurer about a whole candidate.
UNSPLIT = "unsplit"
KIND_UNCLEAR = "kind_unclear"
STRUCTURER_NOTES = (UNSPLIT, KIND_UNCLEAR)
CAUTION_TEXTS = {
    NEGATED: "原文这里是否定的说法（例如“没有”“未”），不能当成做过的事。",
    TEAM: "原文写的是团队工作，或者参与、协助，你个人负责的部分请核对。",
    PLAN: "原文写的是计划或打算，不是已经做完的事。",
    UNCERTAIN_DATE: "原文的时间不确定（例如“预计”“左右”），请核对。",
    UNSPLIT: "有一行没能拆成几栏，整行放在了同一栏里，请核对。",
    KIND_UNCLEAR: "不确定属于哪一类，先放在“其他”里。",
}

_MONTHS = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
MARKERS = (
    (
        NEGATED,
        re.compile(
            r"没有|没用|没做|没能|未能|未曾|未完成|未使用|未参与|并未|从未|尚未|不会|不熟悉|不是|无法"
            r"|\bnever\b|\bnot\b|n't\b|\bwithout\b|\bno longer\b",
            re.IGNORECASE,
        ),
    ),
    (
        TEAM,
        re.compile(
            r"参与|协助|配合|辅助|协同|合作|团队|小组|我们|共同|一起|队友|同事|他人|同学负责|他负责|她负责|其他人负责"
            r"|\bteam\b|\bwe\b|\bour\b|\bassist(?:ed|ing)?\b|\bhelp(?:ed|ing)?\b|\bcollaborat\w*|\bcontribut\w*"
            r"|\bparticipat\w*|\bsupport(?:ed|ing)\b|\bjointly\b|\bco-\w+",
            re.IGNORECASE,
        ),
    ),
    (
        PLAN,
        re.compile(
            r"计划|打算|将要|将会|希望|想要|正在学习"
            r"|\bplan(?:s|ned|ning)? to\b|\bwill\b|\bgoing to\b|\bintend(?:s|ed|ing)? to\b|\bhope to\b|\baim(?:s|ing)? to\b",
            re.IGNORECASE,
        ),
    ),
    (
        UNCERTAIN_DATE,
        re.compile(
            r"预计|预期|大约|约(?=\s*\d)|左右|待定|暂定"
            r"|\bexpected\b|\banticipated\b|\bapprox\w*|\btbd\b|\btentative\w*|\bestimated\b",
            re.IGNORECASE,
        ),
    ),
)
# A date, for deciding whether an uncertain date around a piece concerns it.
DATE = re.compile(rf"\d{{4}}|\d{{1,2}}\s*月|\b(?:{_MONTHS})[a-z]*\.?\s*\d{{2,4}}|至今|\bpresent\b", re.IGNORECASE)
# What is left of a piece that is only a date once its dates and their usual words are taken out: nothing.
DATE_WORDS = re.compile(
    rf"\b(?:{_MONTHS})[a-z]*\.?|\b(?:to|present|now|current|expected|graduation)\b|[\d\s.,/\-–—~～()（）]|年|月|日|至今|至|到|今|预计|毕业",
    re.IGNORECASE,
)
# Where a sentence or a cell ends: the stretch a piece is read with.
BREAK = re.compile(r"[\n。！？；;!?|｜\t]|\.(?=\s)| {2,}|　")


class Structurer(Protocol):
    """Who turns an import's segments into an answer of this contract.

    The local simulation (resume_rules.SimulatedStructurer) answers at once,
    in this process. A real service (app.live_structuring, U2-C3) is not one of
    these: it is sent only the masked text the user confirmed, in the
    background and behind its gate, and its answer is checked by
    check_sent_answer.
    """

    # Kept with the result: who answered, by which rules or model.
    name: str

    def structure(self, segments: list[dict]) -> str: ...


@dataclass
class Found:
    """A candidate that passed: its kind, its pieces per field, and its cautions."""

    kind: str
    extracted: dict[str, list[dict]]
    cautions: list[dict] = field(default_factory=list)


@dataclass
class Checked:
    candidates: list[Found]
    # The dropped candidates, without content: position, kind, segments, reason.
    problems: list[dict]
    # Why nothing could be kept, or None.
    failure: str | None


class _Dropped(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def check_answer(answer: str, segments: list[dict]) -> Checked:
    """Check a structurer's answer against the import's segments."""
    return _checked(answer, segments)


def check_sent_answer(answer: str, sent: list[dict], segments: list[dict]) -> Checked:
    """Check an outside service's answer against the text it was sent, and again against the import's (U2-C3).

    sent holds what went out, segment by segment (outbound_resume). A
    candidate is kept only if it passes both checks: its words stand in what
    was sent and in the import's own text, and nothing around them in either
    changes what they say; its cautions are read from the import's text. A
    piece that quotes a placeholder drops its candidate, and so does a basic
    profile, which only the local rules give. So nothing masked or kept back
    comes into a candidate through a quote.
    """
    return _checked(answer, segments, sent)


def _checked(answer: str, segments: list[dict], sent: list[dict] | None = None) -> Checked:
    try:
        payload = json.loads(answer)
    except (TypeError, ValueError):
        return Checked([], [], INVALID_ANSWER)

    if not isinstance(payload, dict) or set(payload) != {"candidates"} or not isinstance(payload["candidates"], list):
        return Checked([], [], INVALID_ANSWER)

    if len(payload["candidates"]) > MAX_CANDIDATES:
        return Checked([], [], TOO_MANY)

    texts = _texts(segments)
    sent_texts = _texts(sent) if sent is not None else None
    found, problems = [], []

    for position, item in enumerate(payload["candidates"], start=1):
        try:
            if sent_texts is not None:
                _check_sent(item, sent_texts)

            found.append(_check_candidate(item, texts))
        except _Dropped as dropped:
            kind = item.get("kind") if isinstance(item, dict) else None
            problems.append(
                {
                    "position": position,
                    "kind": kind if kind in CANDIDATE_KINDS else None,
                    "segments": _cited(item, len(texts)),
                    "reason": dropped.reason,
                }
            )

    if found:
        return Checked(found, problems, None)

    return Checked([], problems, NONE_VALID if problems else NOTHING_FOUND)


def _texts(segments: list[dict]) -> list["_Text | None"]:
    return [_Text(text) if text is not None else None for text in map(readable_text, segments)]


def _check_sent(item, texts: list["_Text | None"]) -> None:
    """Drop a candidate of an outside service unless it may be kept by what it was sent."""
    if isinstance(item, dict) and item.get("kind") == PROFILE:
        raise _Dropped(LOCAL_ONLY)

    _check_candidate(item, texts)

    if any(MASK_START in piece["quote"] for pieces in item["fields"].values() for piece in pieces):
        raise _Dropped(MASKED)


def readable_text(segment: dict) -> str | None:
    """A segment's text as structurers see it, or None where nothing was read."""
    if segment.get("unreadable"):
        return None

    text = segment_text(segment)

    return text if text.strip() else None


def _check_candidate(item, texts: list["_Text | None"]) -> Found:
    if not isinstance(item, dict) or not {"kind", "fields"} <= set(item) or set(item) - {"kind", "fields", "notes"}:
        raise _Dropped(FORMAT)

    kind = item["kind"]

    if kind not in CANDIDATE_KINDS:
        raise _Dropped(UNKNOWN_KIND)

    fields, notes = item["fields"], item.get("notes", [])

    if not isinstance(fields, dict) or not fields or not isinstance(notes, list):
        raise _Dropped(FORMAT)

    if any(note not in STRUCTURER_NOTES for note in notes):
        raise _Dropped(FORMAT)

    known = {entry.name: entry for entry in CANDIDATE_FIELDS[kind]}
    extracted, cautions = {}, []

    for name, pieces in fields.items():
        entry = known.get(name)

        if entry is None:
            raise _Dropped(UNKNOWN_FIELD)

        if not isinstance(pieces, list) or not pieces:
            raise _Dropped(FORMAT)

        # The cheap checks first: a value that cannot be kept is never searched for.
        kept = [{"segment": number, "quote": quote} for number, quote in map(_piece, pieces)]

        if not entry.multiline and (len(kept) > 1 or "\n" in kept[0]["quote"]):
            raise _Dropped(NOT_ONE_LINE)

        if len(join_pieces(kept)) > entry.max_length:
            raise _Dropped(TOO_LONG)

        for piece in kept:
            number = piece["segment"]

            if not 1 <= number <= len(texts) or texts[number - 1] is None:
                raise _Dropped(BAD_SEGMENT)

            for code in texts[number - 1].codes(piece["quote"]):
                if {"field": name, "code": code} not in cautions:
                    cautions.append({"field": name, "code": code})

        extracted[name] = kept

    cautions += [{"field": None, "code": note} for note in dict.fromkeys(notes)]

    return Found(kind=kind, extracted=extracted, cautions=cautions)


def _piece(piece) -> tuple[int, str]:
    if not isinstance(piece, dict) or set(piece) != {"segment", "quote"}:
        raise _Dropped(FORMAT)

    number, quote = piece["segment"], piece["quote"]

    # A JSON true is not a segment number.
    if type(number) is not int or not isinstance(quote, str) or not quote.strip():
        raise _Dropped(FORMAT)

    return number, quote.strip()


def _cited(item, count: int) -> list[int]:
    """The segments a dropped candidate pointed at, where they exist; never its words."""
    numbers = set()
    fields = item.get("fields") if isinstance(item, dict) else None

    for pieces in fields.values() if isinstance(fields, dict) else ():
        for piece in pieces if isinstance(pieces, list) else ():
            number = piece.get("segment") if isinstance(piece, dict) else None

            if type(number) is int and 1 <= number <= count:
                numbers.add(number)

    return sorted(numbers)


class _Text:
    """One segment's text, read once for all the pieces that point at it.

    Where its sentences and cells end is found once, so the stretch around
    any place is two binary searches; the markers of each stretch are found
    once; and each distinct piece of words is checked once.
    """

    def __init__(self, text: str):
        self.text = text
        spans = [(match.start(), match.end()) for match in BREAK.finditer(text)]
        self.break_starts = [start for start, _ in spans]
        self.break_ends = [end for _, end in spans]
        self.units: dict[tuple[int, int], list] = {}
        self.checked: dict[str, list[str] | str] = {}

    def unit(self, start: int, end: int) -> tuple[int, int]:
        """The sentence or cell around the words at start..end."""
        before = bisect_right(self.break_ends, start) - 1
        after = bisect_left(self.break_starts, end)
        left = self.break_ends[before] if before >= 0 else 0
        right = self.break_starts[after] if after < len(self.break_starts) else len(self.text)

        return left, right

    def markers(self, left: int, right: int) -> list:
        """Each marker kind's places in a stretch, in order: (code, starts, spans)."""
        if (left, right) not in self.units:
            found = []

            for code, pattern in MARKERS:
                # Positions within the whole text: pos and endpos do not shift them.
                spans = [(match.start(), match.end()) for match in pattern.finditer(self.text, left, right)]

                if spans:
                    found.append((code, [start for start, _ in spans], spans))

            self.units[(left, right)] = found

        return self.units[(left, right)]

    def codes(self, quote: str) -> list[str]:
        """The cautions of these words; _Dropped if they are not in the text, or were cut out of what it said.

        The words are read wherever they stand, each time within its
        sentence or cell. A marker wholly inside them is a caution; one
        outside means they were cut out of what they said. Two markers only
        concern some words: an uncertain date only words with a date, and
        team work only words that say more than a date (a time is no
        achievement, whoever it was shared with).
        """
        if quote not in self.checked:
            try:
                self.checked[quote] = self._codes(quote)
            except _Dropped as dropped:
                self.checked[quote] = dropped.reason

        result = self.checked[quote]

        if isinstance(result, str):
            raise _Dropped(result)

        return result

    def _codes(self, quote: str) -> list[str]:
        at = self.text.find(quote)

        if at == -1:
            raise _Dropped(NOT_IN_SOURCE)

        has_date = bool(DATE.search(quote))
        only_date = has_date and not DATE_WORDS.sub("", quote)
        codes = []

        while at != -1:
            end = at + len(quote)

            for code, starts, spans in self.markers(*self.unit(at, end)):
                # Markers never overlap, so the first one from here is the only one that may lie inside.
                first = bisect_left(starts, at)

                if first < len(spans) and spans[first][1] <= end and code not in codes:
                    codes.append(code)

                outside = spans[0][0] < at or spans[-1][1] > end
                concerns = (code != UNCERTAIN_DATE or has_date) and (code != TEAM or not only_date)

                if outside and concerns:
                    raise _Dropped(CUT_CONTEXT)

            at = self.text.find(quote, at + 1)

        return codes


def join_pieces(pieces: list[dict]) -> str:
    return "\n".join(piece["quote"] for piece in pieces)


def extracted_value(extracted: dict, name: str) -> str:
    return join_pieces(extracted.get(name, []))


def current_values(kind: str, extracted: dict, edits: dict | None) -> dict[str, str]:
    """Each field of a candidate as it stands now: the user's words where they changed it."""
    edits = edits or {}

    return {
        entry.name: edits[entry.name] if entry.name in edits else extracted_value(extracted, entry.name)
        for entry in CANDIDATE_FIELDS[kind]
    }


def parse_edits(form, kind: str) -> dict[str, str]:
    """The values a user submitted for a candidate; raise ContentError if they cannot be kept.

    They are checked as the library checks its own fields, so they could be
    kept there later; they need not stand in the import's text.
    """
    errors = {}
    values = {entry.name: _clean(form.get(entry.name, ""), entry, errors) for entry in CANDIDATE_FIELDS[kind]}

    if errors:
        raise ContentError("有内容没能保存，请按提示修改后再保存。", errors)

    if not any(values.values()):
        raise ContentError("请至少保留一栏内容。不需要这一条的话，可以回到核对页选择“不加入”。")

    return values


def changed_fields(kind: str, extracted: dict, values: dict[str, str]) -> dict[str, str]:
    """The submitted values that differ from what was found: these are the user's edits."""
    return {
        entry.name: values[entry.name]
        for entry in CANDIDATE_FIELDS[kind]
        if values[entry.name] != extracted_value(extracted, entry.name)
    }


def where(segments: list[dict], piece: dict) -> str:
    """Where a piece stands, as people say it: the page, paragraph or table, and the line."""
    number = piece["segment"]

    if not 1 <= number <= len(segments):
        return "原文中"

    segment = segments[number - 1]
    place = locator(segment)
    text = segment_text(segment)
    at = text.find(piece["quote"])

    if segment["kind"] == "paragraph" or at == -1:
        return place

    line = text.count("\n", 0, at) + 1
    # "表格 1 第 2 行", but "第 2 页第 3 行" and "粘贴的文字第 3 行".
    gap = " " if place[-1].isdigit() else ""

    return f"{place}{gap}第 {line} 行"
