"""The fixed rules of a search (U3-A1): whatever the next step is, it cannot change them.

From a posting's public facts and from the analysis entry's results they decide:

- what the scope lets a search look for (allows), and where a posting
  stands against it (screen). What the user cannot accept is out and stays
  out. A posting in several cities is judged by those in scope only when it
  says the hire may choose; otherwise it is pending (工作地点待确认), as is
  one that does not say. Ideal values only put postings first (ideal_hits).
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

RULES_VERSION changes with any of them.
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
from app.experience_analysis import PREFERENCE_TOPICS
from app.library.content import PREFERENCE_GROUPS, PREFERENCE_SINGLES

RULES_VERSION = "3"
NOT_PROVIDED = "未提供"
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
SENIOR = re.compile(r"(?:[3-9]|[1-9]\d|[三四五六七八九十])\s*年以上|资深|高级|senior", re.IGNORECASE)
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
        pending.append("工作地点未写明，待确认")
    else:
        inside = [city for city in posting.cities if allows(scope, Dimension.CITY, city) is None]
        outside = [f"{city}{_why_not(scope, Dimension.CITY, city)}" for city in posting.cities if city not in inside]

        if not inside:
            out.append(f"工作地点超出条件：{'、'.join(outside)}")
        elif outside and posting.city_choice:
            notes.append(f"可选工作地点，按{'、'.join(inside)}判断；{'、'.join(outside)}")
        elif outside:
            pending.append(f"工作地点待确认：写了{'、'.join(posting.cities)}，没有说明可以选择；{'、'.join(outside)}")

    for dimension, value in ((Dimension.JOB_TYPE, posting.job_type), (Dimension.DIRECTION, posting.direction)):
        label = DIMENSION_LABELS[dimension]

        if value is None:
            if scope.of(dimension).others is not Others.INCLUDED:
                pending.append(f"{label}未写明，待确认")
        elif allows(scope, dimension, value) is not None:
            out.append(f"{label}超出条件：{value}{_why_not(scope, dimension, value)}")

    if out:
        return Screening(Screen.OUT, tuple(out), tuple(notes))

    if pending:
        return Screening(Screen.PENDING, tuple(pending), tuple(notes))

    return Screening(Screen.IN, (), tuple(notes))


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


def instruction_flags(text: str) -> tuple[str, ...]:
    """Words in a posting that read like instructions to the system: flagged, never followed, no guarantee."""
    folded = text.casefold()

    return tuple(marker for marker in INSTRUCTION_MARKERS if marker in folded)


def marks(posting: Posting, text: str | None) -> tuple[str, ...]:
    """Public facts the next step may go by (资深, 缺要求), never a conclusion about the user."""
    found = []

    if SENIOR.search(f"{posting.title}\n{text or ''}"):
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
        f"经历里没有找到足够依据：{finding.text}"
        for finding in required
        if finding.conclusion != EvidenceConclusion.SUPPORTED
    )


def to_confirm(result: AnalysisResult, screening: Screening, *, own_words: bool = True) -> tuple[str, ...]:
    """What is not known yet: a pending dimension, an unjudged condition, a firm preference not shown either way.

    With own_words=False a preference is named by its topic alone, as the
    next decision sees it: never the user's words (U3 proposal, 5.5).
    """
    items = list(screening.reasons) if screening.status is Screen.PENDING else []
    items += [
        f"招聘条件：{finding.text}"
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
