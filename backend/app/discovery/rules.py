"""The fixed rules of a search (U3-A1): whatever the next step is, it cannot change them.

From a posting's public facts and from the analysis entry's results they decide:

- what the scope lets a search look for (allows), and where a posting
  stands against it (screen). What the user cannot accept is out and stays
  out. A posting in several cities is judged by those in scope only when it
  says the hire may choose; otherwise it is pending (工作地点待确认), as is
  one that does not say. A fact not known is told as what it is (U3-D2-2):
  the source gave none (来源没有提供), or gave one not read as a city or a
  type (写了但没有认出), never guessed. A type that says only the hours or
  the contract (Full-time, Part-time, Contract, Permanent, Temporary) is
  told as that (只写了工时或合同, U3-D2-E3): it says nothing of whether the
  job is an internship, so it is neither taken as one nor as not one, and
  the type stays pending as written; a full-time internship is one. A
  title stating an internship while the type the source wrote states
  another kind of hire (校招) is a conflict: neither is taken, the type
  pending. Ideal values only put postings first (ideal_hits).
- which postings are one (same_posting), only on a basis that can be
  checked: a lead and the official posting it points to, or two official
  postings of one company stating the same job number and nothing else
  different. Postings only alike (alike: the same title of the same company
  in the same cities) stay apart, each a candidate of its own.
- when a posting counts as checked (checked): only when its source answered.
- where an analysed posting stands (classify), and what to confirm first;
  what of it the next decision may see leaves out the user's own words.
- what the scope holds that no search has covered (unsearched): each
  combination of its values, not each value on its own.
- in what order postings are put before a decision (relevance, U3-D5): those
  plainly in the scope first, then those pending, then those whose own public
  words are at odds with what the scope asks for. An order only: nothing is
  left out by it and no scope is widened or narrowed.

RULES_VERSION changes with any of them. 6 (U3-D5): relevance; and "N+ years" (N of 3 or more), in English, is a mark
of seniority as "N 年以上" is, a range by its lower end ("3-5 years" is, "2-4 years" is not). 7 (U3-D5 R1): seniority
is only what a posting says of the role (its title) or asks of the applicant (three or more years of work), never other
people's seniority, a student's year, the company's age, or experience preferred or not needed. 8 (U3-D5 R2): years of
work are asked only as a minimum ("at least 3", "3+", "3 or more", "3-5", "3 年以上"); a ceiling ("less than 3", "up to
3", "3 years or less", "不超过 3 年"), the upper end of a range ("1 - 3", "between 1 and 3", "1-3 年以上"), a time
window, a guess ("about 4") or years said not to be required ("does not require 3 years", "不要求 3 年以上") never are.
"""
import itertools
import re
from collections.abc import Mapping
from datetime import date, datetime

from app.analysis_contracts import (
    AnalysisResult,
    AnalysisStatus,
    ConditionConclusion,
    EvidenceConclusion,
    PreferenceConclusion,
    RequirementGroup,
    SelectedPreference,
)
from app.analysis_words import requirement_words
from app.discovery.contracts import (
    DIMENSION_LABELS,
    DIMENSION_PREFERENCES,
    Category,
    CheckAnswer,
    CheckStatus,
    Dimension,
    DimensionScope,
    Others,
    Posting,
    Screen,
    Screening,
    SearchScope,
)
from app.library.content import PREFERENCE_GROUPS, PREFERENCE_SINGLES

RULES_VERSION = "8"
NOT_PROVIDED = "未提供"
# Why a fact of a posting is not known (U3-D2-2): its source gave none, or gave one not read as such.
NOT_GIVEN = "来源没有提供"
NOT_READ = "写了但没有认出"
# Why its type is not known besides (U3-D2-E3): what the source wrote only says the hours or the contract, or states
# another kind of hire than its title.
HOURS_ONLY = "只写了工时或合同"
AT_ODDS = "和职位名不一致"
# A title that states an internship, as a commitment does (app.discovery.lever.INTERNSHIP).
INTERN_TITLE = re.compile(r"(?<![a-z])(interns?|internships?)(?![a-z])|实习", re.IGNORECASE)
# What a source may write as a posting's type that only says its hours or its contract (U3-D2-E3), and what may join
# several of them ("Full-time / Permanent"). Never a sign of whether the job is an internship.
HOURS_OR_CONTRACT = re.compile(r"(?<![a-z])(full[\s-]?time|part[\s-]?time|contract|permanent|temporary)(?![a-z])", re.IGNORECASE)
LABEL_GLUE = re.compile(r"[\s/,;&|·+]+")
MUST_HAVES = PREFERENCE_SINGLES[1].name
# Preference fields the scope is made of: held against postings by these rules, never sent to an analysis.
SCOPE_FIELDS = frozenset(
    field.name for key, _, fields in PREFERENCE_GROUPS if key in DIMENSION_PREFERENCES.values() for field in fields
)
# The preference topics an analysis may compare, with their fields.
ANALYSED_TOPICS = tuple(
    (label, tuple(field.name for field in fields))
    for key, label, fields in PREFERENCE_GROUPS
    if key not in DIMENSION_PREFERENCES.values()
) + tuple((field.label, (field.name,)) for field in PREFERENCE_SINGLES)
# How a preference lists its values: "北京、上海", "北京, 上海", or one per line.
SEPARATORS = re.compile(r"[、,，;；/\n]+")
# --- seniority: the role's own level, or years of work asked of the applicant (U3-D5 R1) ---
#
# Only what a posting says of the role or asks of the applicant: never other people's seniority ("work with senior
# engineers", "由资深工程师指导"), a student's year ("junior or senior undergraduate", "完成三年本科课程"), the
# company's age ("founded 5 years ago"), nor experience said to be preferred or not needed. What is not plain is not
# claimed: "5 years in data" or "about 4 years" is left unmarked; and years are asked only as a minimum (U3-D5 R2), never
# as a ceiling, the upper end of a range, a time window or a guess (the bounds below).
#
# The role is senior when its title says so.
SENIOR_TITLE = re.compile(r"资深|高级|(?<![a-z])senior(?![a-z])", re.IGNORECASE)
# Three or more years, a range by its lower end ("3-5 years" is, "2-4 years" is not; "between 3 and 5" too); "3 or more
# years" too (U3-D5 R2).
YEARS_EN = re.compile(
    r"(?<![\d.\-–~～])(?<!to )(?P<n>[3-9]|[1-9]\d)\s*(?:(?:[-–~～]|to|and|or)\s*\d+\s*)?(?:\+|or\s+more\s)?\s*(?:years?|yrs?)'?(?![a-z])",
    re.IGNORECASE,
)
# Which way the years go (U3-D5 R2): words just before them, or after them in their clause, that make them no minimum.
# A ceiling ("less than", "up to", "no more than", "at most", "max", "or less", "and under"), a time window ("within the
# last", "in the past"), or a guess ("about", "around", "~"). "No less than", "more than", "over" stay a minimum.
BOUND_BEFORE_EN = re.compile(
    r"(?:(?<!\bno )(?<!\bnot )\b(?:less|fewer|lower)\s+than|\bunder|\bbelow|\b(?:no|not)\s+(?:more|greater|longer)\s+than"
    r"|\bnot\s+(?:exceeding|to\s+exceed|over)|\bup\s*to|\bat\s+(?:the\s+)?most|\bmax(?:imum)?\.?(?:\s+of)?"
    r"|\bwithin(?:\s+(?:the\s+)?(?:last|past|first|next))?|\b(?:in|over|during|for)\s+the\s+(?:last|past|first|next)"
    r"|\b(?:about|around|approximately|approx\.?|roughly|nearly|almost|circa)|[<≤≦~～]=?)\s*$",
    re.IGNORECASE,
)
BOUND_AFTER_EN = re.compile(
    r"\bor\s+(?:less|fewer|lower)\b|\b(?:and|or)\s+(?:under|below)\b|\bat\s+(?:the\s+)?most\b|\bmax(?:imum)?\b(?![\-/])|\btops\b",
    re.IGNORECASE,
)
# The upper end of a range, whichever way it is written ("1 - 3", "1 to 3", "between 1 and 3", "2 or 3", "1-3 年",
# "两到三年"): the range goes by its lower end, never by this one.
RANGE_END = re.compile(r"(?:\d|[一二两三四五六七八九十])\s*(?:[-–—~～至到和或及]|to|and|or)\s*$", re.IGNORECASE)
# The same in Chinese: a ceiling or a guess before ("不超过", "少于", "最多", "约", "近"), a ceiling after ("以下", "以内").
BOUND_BEFORE_CN = re.compile(r"(?:不超过|不多于|不高于|不大于|少于|低于|小于|不足|不到|最多|至多|顶多|最长|约|近)\s*$")
BOUND_AFTER_CN = re.compile(r"\s*(?:及|或)?(?:以下|以内|之内|内|左右|上下)")
# What ties those years to the applicant's work: "years of (professional ...) experience", "years working as";
# or "experience: at least" just before them. Words of study between the years and "experience" untie them.
EXPERIENCE_AFTER = re.compile(r"\s*(?:of\s+)?(?P<between>(?:[a-z][\w/\-]*\s+){0,4}?)experienced?\b", re.IGNORECASE)
WORKING_AFTER = re.compile(r"\s*working\s+(?:as|in|on|with)\b", re.IGNORECASE)
EXPERIENCE_BEFORE = re.compile(
    r"experienced?\s*(?:of|:|：|-|–)?\s*(?:at\s+least|a\s+minimum\s+of|minimum\s+of|minimum|over|more\s+than)?\s*$", re.IGNORECASE
)
STUDY = re.compile(
    r"undergraduate|graduate|college|university|study|studies|school|coursework|course|education|degree|bachelor|master|phd|academic",
    re.IGNORECASE,
)
# Clauses that do not ask it of the applicant: experience not needed, only preferred, or other people's. Not needed
# however it is said (U3-D5 R2): "does not require 3 years", "you don't need 5 years", "3 years: not required".
NOT_ASKED_EN = re.compile(
    r"\bno\s+(?:prior\s+|previous\s+|professional\s+|work\s+|relevant\s+|industry\s+)*experience"
    r"|experience\s+(?:is\s+)?not\s+(?:required|necessary|needed)|without\s+(?:any\s+)?(?:prior\s+)?experience"
    r"|\bnot\s+(?:required|necessary|needed|mandatory)\b|n[’']t\s+(?:required|necessary|needed|mandatory)\b"
    r"|\b(?:does|do|did|will|would|should)(?:\s+not|n[’']t)\s+(?:\w+\s+){0,2}?(?:require|need|expect|ask)"
    r"|\bneed(?:\s+not|n[’']t)\b|\bno\s+need\b"
    r"|\bpreferred\b|\ba\s+plus\b|\bnice\s+to\s+have\b|\bbonus\b|\bdesirable\b|\bideally\b"
    r"|\b(?:our|the)\s+(?:team|company|engineers|founders|firm|group|mentors?|staff)\s+(?:has|have|had|brings?|boasts?)\b"
    r"|\bwe\s+(?:have|bring)\b|\bwe've\b",
    re.IGNORECASE,
)
_CN_N = r"(?:[3-9]|[1-9]\d|[三四五六七八九十])"
YEARS_CN = re.compile(
    rf"(?P<a>{_CN_N})\s*(?:[-–~～至到]\s*(?:\d+|[三四五六七八九十])\s*)?年(?:及|或)?以上"
    rf"|(?:至少|最少|不少于|不低于)\s*(?P<b>{_CN_N})\s*年"
    rf"|(?P<c>{_CN_N})\s*[-–~～至到]\s*(?:\d+|[三四五六七八九十])\s*年"
)
WORK_CN = re.compile(r"经验|工作|从业")
# Not needed however it is said (U3-D5 R2): "不要求 3 年以上工作经验" as "不要求工作经验".
NOT_ASKED_CN = re.compile(
    r"不要求|无需|无须|不需要|不必|非必须|非必需|不作要求|不做要求|没有要求|经验不限|不限经验|无(?:工作)?经验要求|零经验|无经验"
    r"|优先|加分|更佳|为佳"
    r"|(?:团队|公司|我们|导师|同事)[^，,。；;]{0,6}(?:拥有|有|具备|平均|均有)"
)
# A clause: a sentence, or one item of a list, so a word that preferred or waived one item never reaches another
# ("本科及以上学历，3 年以上工作经验，有金融经验者优先").
# The full stop of an abbreviation ("Max. 3 years", "approx. 4 years") ends no clause (U3-D5 R2): its word still bounds
# the years after it.
CLAUSES = re.compile(r"[。；;！!？?\n，,、]+|(?<!\bmax)(?<!\bmin)(?<!\bapprox)(?<!\bca)\.(?=\s|$)", re.IGNORECASE)
CN_NUMBERS = {"三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# The kinds of hire a posting asking for years of experience is at odds with (U3-D5): when the scope asks for these
# only, such a posting is put after the others, never left out.
EARLY_CAREER = frozenset({"实习", "校招"})
# The order of postings before a decision (relevance): plainly in the scope, pending, at odds with what it asks for.
PLAIN, UNCLEAR, AT_ODDS_WITH_SCOPE = 0, 1, 2
RELEVANCE_LABELS = {PLAIN: "明确符合检索范围", UNCLEAR: "有一项待确认", AT_ODDS_WITH_SCOPE: "可能不符合目标"}
# Words that say a posting has requirements or duties at all.
REQUIREMENT_WORDS = ("要求", "任职资格", "岗位职责", "职位描述", "工作内容", "requirement", "qualification", "responsibilit")
# Words in a posting that speak to the system rather than to applicants: a reminder, never a protection.
INSTRUCTION_MARKERS = (
    "忽略之前",
    "忽略以上",
    "忽略上述",
    "无视之前",
    "放宽条件",
    "候选人的经历",
    "用户的经历",
    "系统提示",
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "system prompt",
)


def values_of(text: str | None) -> tuple[str, ...]:
    """The values a preference lists, as the user wrote them, each once."""
    values: list[str] = []

    for piece in SEPARATORS.split(text or ""):
        value = piece.strip()

        if value and value not in values:
            values.append(value)

    return tuple(values)


def normalized(dimension: Dimension, value: str) -> str:
    folded = " ".join(value.split()).casefold()

    # "北京市" is 北京.
    if dimension is Dimension.CITY and len(folded) > 2 and folded.endswith("市"):
        folded = folded[:-1]

    return folded


def prefill(preferences: Mapping[str, str]) -> dict[Dimension, DimensionScope]:
    """Each dimension as a review page first shows it: ideal, then acceptable values chosen.

    Whether values the preferences do not list are searched as well is left
    for the user to say (Others.UNDECIDED), however much they filled in.
    """
    draft = {}

    for dimension, key in DIMENSION_PREFERENCES.items():
        ideal = values_of(preferences.get(f"{key}_ideal"))
        excluded = values_of(preferences.get(f"{key}_no"))
        barred = {normalized(dimension, value) for value in excluded}
        values = tuple(
            value
            for value in dict.fromkeys(ideal + values_of(preferences.get(f"{key}_ok")))
            if normalized(dimension, value) not in barred
        )
        draft[dimension] = DimensionScope(
            values=values, others=Others.UNDECIDED, ideal=tuple(value for value in ideal if value in values), excluded=excluded
        )

    return draft


def scope_problems(scope: SearchScope) -> tuple[str, ...]:
    """What keeps a search of this scope from starting; nothing when it may start."""
    problems = []

    for dimension in Dimension:
        part, label = scope.of(dimension), DIMENSION_LABELS[dimension]
        barred = {normalized(dimension, value) for value in part.excluded}

        if part.others is Others.UNDECIDED:
            problems.append(f"{label}：所列以外的是否也查还没有选择（本次不限，或只看所列的）")
        elif part.others is Others.EXCLUDED and not part.values:
            problems.append(f"{label}：没有可以查的")

        problems += [f"{label}：“{value}”是不能接受的，不能选" for value in part.values if normalized(dimension, value) in barred]

    if not any(target.strip() for target in scope.targets):
        problems.append("没有要找的岗位名称")

    return tuple(problems)


def allows(scope: SearchScope, dimension: Dimension, value: str | None) -> str | None:
    """Why a search may not be narrowed to this value, or left open (None), if it may not."""
    part, label = scope.of(dimension), DIMENSION_LABELS[dimension]

    if value is None:
        return None if part.others is Others.INCLUDED else f"本次只看所列的{label}，检索时必须指定一个"

    wanted = normalized(dimension, value)

    if wanted in {normalized(dimension, barred) for barred in part.excluded}:
        return f"“{value}”是不能接受的{label}"

    if part.others is Others.INCLUDED or wanted in {normalized(dimension, listed) for listed in part.values}:
        return None

    return f"“{value}”不在本次确认的{label}范围里"


def screen(scope: SearchScope, posting: Posting) -> Screening:
    """Where a posting stands against the scope, from its public facts alone."""
    out, pending, notes = [], [], []

    if not posting.cities:
        pending.append(f"工作地点{unknown(posting.places_stated)}，待确认")
    else:
        inside = [city for city in posting.cities if allows(scope, Dimension.CITY, city) is None]
        outside = [f"{city}{_why_not(scope, Dimension.CITY, city)}" for city in posting.cities if city not in inside]

        if not inside:
            out.append(f"工作地点超出条件：{'、'.join(outside)}")
        elif outside and posting.city_choice:
            notes.append(f"可选工作地点，按{'、'.join(inside)}判断；{'、'.join(outside)}")
        elif outside:
            pending.append(f"工作地点待确认：写了{'、'.join(posting.cities)}，没有说明可以选择；{'、'.join(outside)}")

    # The type as read, or none: what was written only says the hours or the contract, or is at odds with the title
    # (type_reading); none is guessed, and none is a reason to leave a posting out.
    why_type = type_reading(posting.job_type, posting.type_stated, posting.title)
    known = {Dimension.JOB_TYPE: posting.job_type if why_type is None else None, Dimension.DIRECTION: posting.direction}
    why = {Dimension.JOB_TYPE: why_type, Dimension.DIRECTION: NOT_GIVEN}
    type_pending = False

    for dimension, value in known.items():
        label = DIMENSION_LABELS[dimension]

        if value is None:
            if scope.of(dimension).others is not Others.INCLUDED:
                pending.append(f"{label}{why[dimension]}，待确认")
                type_pending = type_pending or dimension is Dimension.JOB_TYPE
        elif allows(scope, dimension, value) is not None:
            out.append(f"{label}超出条件：{value}{_why_not(scope, dimension, value)}")

    note = type_note(posting)

    # A conflict the pending reason already says is not said twice.
    if note is not None and not (type_pending and why_type == AT_ODDS):
        notes.append(note)

    if out:
        return Screening(Screen.OUT, tuple(out), tuple(notes))

    if pending:
        return Screening(Screen.PENDING, tuple(pending), tuple(notes))

    return Screening(Screen.IN, (), tuple(notes))


def unknown(stated: tuple[str, ...]) -> str:
    """Why a fact is not known: nothing given (NOT_GIVEN), or given as stated and not read (NOT_READ)."""
    return NOT_READ if any(value.strip() for value in stated) else NOT_GIVEN


def hours_or_contract(stated: str | None) -> bool:
    """Whether what a source wrote as a posting's type only says its hours or its contract (U3-D2-E3): Full-time,
    Part-time, Contract, Permanent, Temporary, alone or together. That says nothing of whether it is an internship."""
    if not stated or not HOURS_OR_CONTRACT.search(stated):
        return False

    return not LABEL_GLUE.sub("", HOURS_OR_CONTRACT.sub("", stated))


def type_conflict(posting: Posting) -> bool:
    """A title stating an internship while the type its source wrote states another kind of hire (校招): neither is
    taken (U3-D2-E3). Hours or a contract are no other kind of hire, and a type not read is not one either: those are
    pending as they are, never called a conflict. A type no source wrote (a synthetic posting's own) is taken as it
    is."""
    return _at_odds(posting.job_type, posting.type_stated, posting.title)


def _at_odds(job_type: str | None, stated: str | None, title: str) -> bool:
    return bool(stated) and job_type is not None and job_type != "实习" and bool(INTERN_TITLE.search(title))


def type_reading(job_type: str | None, stated: str | None, title: str) -> str | None:
    """Why a posting's type is not known, or None when it is (job_type, at odds with nothing): the source gave none
    (NOT_GIVEN), gave one that only says the hours or the contract (HOURS_ONLY), gave another kind of hire than its
    title states (AT_ODDS), or gave one not read (NOT_READ). Pages and summaries say it with what was written."""
    if job_type is not None:
        return AT_ODDS if _at_odds(job_type, stated, title) else None

    if hours_or_contract(stated):
        return HOURS_ONLY

    return unknown((stated,) if stated else ())


def type_note(posting: Posting) -> str | None:
    """What a title stating an internship adds to the type its source wrote when that is not read as one. Fixed words,
    never the posting's own: what was written is shown apart, masked like any posting's words."""
    stated = posting.type_stated

    if posting.job_type == "实习" or not stated or not stated.strip() or not INTERN_TITLE.search(posting.title):
        return None

    if type_conflict(posting):
        return "求职类型待确认：职位名写的是实习，来源的类型字段写的是另一种招聘（如校招），两者不一致"

    if hours_or_contract(stated):
        return "职位名写的是实习，来源的类型字段只写了工时或合同，两者不冲突；求职类型仍待确认，不排除"

    return "职位名写的是实习，来源的类型字段没有认出，两者是否一致待确认"


def _why_not(scope: SearchScope, dimension: Dimension, value: str) -> str:
    barred = {normalized(dimension, excluded) for excluded in scope.of(dimension).excluded}

    return "（不能接受）" if normalized(dimension, value) in barred else "（不在本次范围）"


def ideal_hits(scope: SearchScope, posting: Posting) -> int:
    """In how many dimensions a posting has an ideal value within the scope: more come first, none is left out."""
    hits = 0

    for dimension, values in (
        (Dimension.CITY, posting.cities),
        (Dimension.JOB_TYPE, (posting.job_type,) if posting.job_type else ()),
        (Dimension.DIRECTION, (posting.direction,) if posting.direction else ()),
    ):
        ideal = {normalized(dimension, value) for value in scope.of(dimension).ideal}

        if any(normalized(dimension, value) in ideal and allows(scope, dimension, value) is None for value in values):
            hits += 1

    return hits


def same_posting(first: Posting, second: Posting, *, employers_own: bool = False) -> bool:
    """Whether two postings found are one job, on a basis that can be checked.

    A lead is one with the official posting it points to, and two leads
    pointing to one official posting are one. Two postings both from
    employers' own sources (employers_own) are one when they state the same
    job number of the same company, not empty once spaces are gone, and
    nothing else they state differs. A number a lead states is not checked,
    so it makes nothing one; nor does being alike: such postings stay apart.
    """
    if first.official is not None and first.official in (second.ref, second.official):
        return True

    if second.official is not None and second.official == first.ref:
        return True

    # A number with nothing in it once spaces are gone (None, "", "   ") names no job.
    number = _folded(first.identity)

    return employers_own and bool(number) and number == _folded(second.identity) and _facts(first) == _facts(second)


def alike(first: Posting, second: Posting) -> bool:
    """The same title of the same company in the same cities: never enough to make two postings one."""
    return _key(first) == _key(second)


def _key(posting: Posting) -> tuple:
    return (
        _folded(posting.company),
        _folded(posting.title),
        tuple(sorted(normalized(Dimension.CITY, city) for city in posting.cities)),
    )


def _facts(posting: Posting) -> tuple:
    return (*_key(posting), _folded(posting.job_type), _folded(posting.direction))


def _folded(text: str | None) -> str | None:
    return None if text is None else " ".join(text.split()).casefold()


def checked(previous: datetime | None, answer: CheckAnswer | None, now: datetime) -> datetime | None:
    """When a posting was last checked: refreshed only by a check its source answered."""
    if answer is None or answer.status is CheckStatus.UNKNOWN:
        return previous

    return now


def stated(day: date | None) -> str:
    """A date as a source gave it, or 未提供: never made up."""
    return NOT_PROVIDED if day is None else day.isoformat()


def relevance(scope: SearchScope, screening: Screening, posting: Posting, text: str | None) -> tuple[int, str | None]:
    """Where a posting stands before a decision (U3-D5), and why when it is at odds: plainly in the scope (screened
    in), pending (some fact not plain), or at odds with what the scope asks for by its own public words. The only odds
    for now: a scope asking for internships or campus hires only (EARLY_CAREER), and a posting whose own words make it a
    role for experienced hires (seniority). Never a reason to leave a posting out, nor a judgement of the user: an order
    only."""
    types = {normalized(Dimension.JOB_TYPE, value) for value in scope.of(Dimension.JOB_TYPE).values}
    early_only = bool(types) and types <= EARLY_CAREER and scope.of(Dimension.JOB_TYPE).others is not Others.INCLUDED
    why = seniority(posting, text) if early_only else None

    if why is not None:
        return AT_ODDS_WITH_SCOPE, f"求职类型只限实习或校招，{why}"

    return (PLAIN if screening.status is Screen.IN else UNCLEAR), None


def seniority(posting: Posting, text: str | None) -> str | None:
    """Why a posting is a role for experienced hires by its own words (U3-D5 R1), or None: its title says the role is
    senior, or its text asks the applicant for three or more years of work, as a minimum. Never other people's
    seniority, a student's year, the company's age, experience said to be preferred or not needed, nor years that are
    a ceiling, a range's upper end or a guess (the rules above)."""
    if SENIOR_TITLE.search(posting.title):
        return "岗位名称写的是资深或高级职位"

    years = required_years(text or "")

    return None if years is None else f"原文要求应聘者有 {years} 年以上工作经验"


def required_years(text: str) -> int | None:
    """The fewest years of work a text plainly asks of the applicant, three or more, or None (U3-D5 R1); asked as a
    minimum only (U3-D5 R2): years bounded the other way, the upper end of a range or a guess are passed over."""
    for clause in CLAUSES.split(text):
        if not clause.strip() or NOT_ASKED_EN.search(clause) or NOT_ASKED_CN.search(clause):
            continue

        for match in YEARS_EN.finditer(clause):
            rest, before = clause[match.end():], clause[: match.start()]
            after = EXPERIENCE_AFTER.match(rest)
            asked = (after and not STUDY.search(after.group("between"))) or WORKING_AFTER.match(rest) or EXPERIENCE_BEFORE.search(before)

            if asked and not (BOUND_BEFORE_EN.search(before) or RANGE_END.search(before) or BOUND_AFTER_EN.search(rest)):
                return int(match.group("n"))

        for match in YEARS_CN.finditer(clause):
            near = clause[max(0, match.start() - 8): match.end() + 15]
            before, rest = clause[: match.start()], clause[match.end():]

            if WORK_CN.search(near) and not (BOUND_BEFORE_CN.search(before) or RANGE_END.search(before) or BOUND_AFTER_CN.match(rest)):
                value = next(group for group in match.group("a", "b", "c") if group)

                return CN_NUMBERS.get(value) or int(value)

    return None


def instruction_flags(text: str) -> tuple[str, ...]:
    """Words in a posting that read like instructions to the system: flagged, never followed, no guarantee."""
    folded = text.casefold()

    return tuple(marker for marker in INSTRUCTION_MARKERS if marker in folded)


def marks(posting: Posting, text: str | None) -> tuple[str, ...]:
    """Public facts the next step may go by (资深, 缺要求), never a conclusion about the user."""
    found = []

    if seniority(posting, text) is not None:
        found.append("资深")

    if text is not None and not any(word in text.casefold() for word in REQUIREMENT_WORDS):
        found.append("缺要求")

    return tuple(found)


def classify(result: AnalysisResult, screening: Screening) -> Category | None:
    """Where an analysed posting stands; None unless its analysis is complete and current.

    A conflict the analysis found with a hard condition of the posting, or
    with what the user cannot accept or must have, sets it apart whatever
    else holds. With none, it rests on enough only when every required
    skill is supported and nothing needs confirming.
    """
    if result.status is not AnalysisStatus.COMPLETE:
        return None

    if result.condition_conflicts or result.preference_conflicts:
        return Category.CONFLICT

    if gaps(result) or to_confirm(result, screening):
        return Category.PARTIAL

    return Category.STRONG


def gaps(result: AnalysisResult) -> tuple[str, ...]:
    """Required skills the chosen experiences do not support, or that the posting names none."""
    required = [finding for finding in result.findings if finding.group is RequirementGroup.REQUIRED]

    if not required:
        return ("岗位没有写明必备的技能要求",)

    return tuple(
        f"经历里没有找到足够依据：{requirement_words(finding)}"
        for finding in required
        if finding.conclusion != EvidenceConclusion.SUPPORTED
    )


def to_confirm(result: AnalysisResult, screening: Screening, *, own_words: bool = True) -> tuple[str, ...]:
    """What is not known yet: a pending dimension, an unjudged condition, a firm preference not shown either way.

    With own_words=False a preference is named by its topic alone, as the
    next decision sees it: never the user's words (U3 proposal, 5.5).
    """
    # Imported here, where it is used, so that loading the rules loads no analysis workflow (U3-D4c: 每日查新 screens
    # postings by these rules and keeps to the boundary of the checks of a kept posting).
    from app.experience_analysis import PREFERENCE_TOPICS

    items = list(screening.reasons) if screening.status is Screen.PENDING else []
    items += [
        # With its condition, if its posting quote lays one down (U3-D6 R4): never an unconditional demand.
        f"招聘条件：{requirement_words(finding)}"
        for finding in result.findings
        if finding.group is RequirementGroup.HARD_CONSTRAINT
        and finding.conclusion in (ConditionConclusion.UNKNOWN, ConditionConclusion.NEEDS_CONFIRMATION)
    ]
    items += [
        f"{PREFERENCE_TOPICS[finding.field][0]}：{finding.text if own_words else '待确认'}"
        for finding in result.preference_findings
        if finding.firm
        and (
            finding.conclusion == PreferenceConclusion.NEEDS_CONFIRMATION
            # What the user must have, the posting not saying: unknown. What they cannot accept, not said: not asked for.
            or (finding.field == MUST_HAVES and finding.conclusion == PreferenceConclusion.NOT_STATED)
        )
    ]

    return tuple(items)


def first_checks(result: AnalysisResult, screening: Screening, *, own_words: bool = True) -> tuple[str, ...]:
    """The one or two things most worth confirming before applying: what is unknown, then a gap."""
    return (to_confirm(result, screening, own_words=own_words) + gaps(result))[:2]


def unsearched(scope: SearchScope, searched: set[tuple[str | None, ...]]) -> tuple[str, ...]:
    """The combinations of the scope's values that no single search covered, as "北京 · 实习 · 数据分析".

    searched holds each search as what it was narrowed to: one value per
    dimension, in the order of Dimension and normalized, or None where it
    was left open. A search covers a combination when each of its
    dimensions was narrowed to that value or left open; the values a
    dimension open in this scope does not list (本次不限) are covered only by
    leaving it open. Each value searched somewhere is not each combination
    searched: Beijing internships and Shanghai full-time roles leave
    Shanghai internships unsearched.
    """
    slots = []

    for dimension in Dimension:
        part, label = scope.of(dimension), DIMENSION_LABELS[dimension]
        listed: dict[str, str] = {}

        for value in part.values:
            listed.setdefault(normalized(dimension, value), value)

        unlisted = [(None, f"所列以外的{label}" if listed else f"任意{label}")] if part.others is Others.INCLUDED else []
        slots.append([*listed.items(), *unlisted])

    return tuple(
        " · ".join(shown for _, shown in combination)
        for combination in itertools.product(*slots)
        if not any(_covers(search, combination) for search in searched)
    )


def _covers(search: tuple[str | None, ...], combination) -> bool:
    # Left open, a dimension takes in every value, unlisted ones too; narrowed, only the value it names.
    return all(value is None or (slot is not None and value == slot) for value, (slot, _) in zip(search, combination))


def unjudged(saved: Mapping[str, str], chosen: tuple[SelectedPreference, ...]) -> tuple[str, ...]:
    """Preference topics no analysis judges, none of their fields being chosen: neither met nor in conflict."""
    fields = {preference.field for preference in chosen}

    return tuple(
        f"{label}：{'没有选择' if any(saved.get(name) for name in names) else '未填写'}，无法判断"
        for label, names in ANALYSED_TOPICS
        if not fields.intersection(names)
    )
