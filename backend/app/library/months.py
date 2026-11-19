"""The time of an experience as years and months (owner redesign, plan 3.1).

An experience's time (``period``) is free text, and older records hold whatever was written: "2025 年春季学期",
"2026 年暑期；还在进行中", "2026.03-2026.06". The form now offers a year-and-month picker for it, never a day, and
never fills in today's date for the person.

- Reading never rewrites anything. A time is shown in the picker only when it is written one of a few unambiguous ways
  (a year with a month, alone or as a range, possibly ending "至今" / "present"); anything else (a year alone, a
  season, a sentence, a complex range) stays as its own text, with a choice to switch to the picker.
- Saving writes a new text only when the person changed the picker: a time saved back as it was read keeps its
  original words exactly, so opening and saving a record without changes changes nothing.
- A time written from the picker uses one form that reads back the same way: "2026-03", "2026-03 – 2026-06", or
  "2026-03 – 至今" ("2026-03 – present" when written in English).
"""
import re
from dataclasses import dataclass
from datetime import date

from app.i18n import is_english, t

FIRST_YEAR = 1970
# The picker offers the years from FIRST_YEAR to a few years ahead (an expected graduation, a planned start).
LAST_YEAR = date.today().year + 6
YEARS = tuple(range(LAST_YEAR, FIRST_YEAR - 1, -1))
MONTHS = tuple(range(1, 13))
ONGOING_WORDS = {"至今": "zh", "present": "en"}
# Words that also mean "still going" in a time someone wrote themselves; written back only as they were.
_ONGOING = r"(?:至今|现在|目前|present|now|ongoing)"
_MONTH = r"(\d{4})\s*(?:年\s*(\d{1,2})\s*月|[-./]\s*(\d{1,2}))"
_SEPARATOR = r"\s*(?:-|–|—|~|～|至|到)\s*"
_SINGLE = re.compile(rf"^{_MONTH}$", re.I)
_RANGE = re.compile(rf"^{_MONTH}{_SEPARATOR}(?:{_MONTH}|({_ONGOING}))$", re.I)
# "2026年3月至今": the month then 至今 with nothing between them.
_ONGOING_TAIL = re.compile(rf"^{_MONTH}\s*(?:至今|现在|目前)$")


@dataclass(frozen=True)
class MonthRange:
    """A start month (year, month), an end month or none, and whether it is still going."""

    start: tuple[int, int] | None
    end: tuple[int, int] | None = None
    ongoing: bool = False


def _month(year: str | None, a: str | None, b: str | None) -> tuple[int, int] | None:
    month = a or b

    if year is None or month is None:
        return None

    value = (int(year), int(month))

    return value if FIRST_YEAR <= value[0] <= 2100 and 1 <= value[1] <= 12 else None


def parse_period(text: str | None) -> MonthRange | None:
    """The years and months a time is written in, when it is one of the unambiguous forms; otherwise None.

    An empty time is an empty range (nothing chosen yet)."""
    text = (text or "").strip()

    if not text:
        return MonthRange(None)

    single = _SINGLE.match(text)

    if single:
        start = _month(*single.groups())

        return MonthRange(start) if start else None

    tail = _ONGOING_TAIL.match(text)

    if tail:
        start = _month(*tail.groups())

        return MonthRange(start, None, True) if start else None

    ranged = _RANGE.match(text)

    if not ranged:
        return None

    groups = ranged.groups()
    start = _month(*groups[0:3])

    if start is None:
        return None

    if groups[6]:
        return MonthRange(start, None, True)

    end = _month(*groups[3:6])

    if end is None or end < start:
        return None

    return MonthRange(start, end)


def period_text(chosen: MonthRange, language: str) -> str:
    """The text a range chosen in the picker is saved as."""
    if chosen.start is None:
        return ""

    start = f"{chosen.start[0]:04d}-{chosen.start[1]:02d}"

    if chosen.ongoing:
        word = "present" if language == "en" else "至今"

        return f"{start} – {word}"

    if chosen.end is None or chosen.end == chosen.start:
        return start

    return f"{start} – {chosen.end[0]:04d}-{chosen.end[1]:02d}"


# The names the picker's parts are posted as, next to the time's own field.
PICKER_FIELDS = (
    "period_mode",
    "period_original",
    "period_start_year",
    "period_start_month",
    "period_end_year",
    "period_end_month",
    "period_ongoing",
)


def _picked(form, prefix: str) -> tuple[tuple[int, int] | None, str | None]:
    year, month = form.get(f"period_{prefix}_year", "").strip(), form.get(f"period_{prefix}_month", "").strip()

    if not year and not month:
        return None, None

    if not (year.isascii() and year.isdigit() and month.isascii() and month.isdigit()):
        return None, "content.month_invalid"

    value = (int(year), int(month))

    if value[0] not in YEARS or value[1] not in MONTHS:
        return None, "content.month_invalid"

    return value, None


def with_period(form: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """The form with its time as the picker says, and an error for the time if the picker cannot be read.

    Without the picker (an older page, another tool, the import's adoption) or with the person keeping their own
    words (period_mode=text), the form is returned as it came."""
    if form.get("period_mode") != "months":
        return form, None

    start, problem = _picked(form, "start")
    end, end_problem = _picked(form, "end")
    ongoing = form.get("period_ongoing") == "1"
    problem = problem or end_problem
    typed = form.get("period", "")

    if problem is None and start is None and end is None and not ongoing and typed.strip() and typed != form.get("period_original", ""):
        # Nothing chosen in the picker but words given for the time that are not the ones the page showed (a client
        # filling the field by its name, an older page): those words are the time, as they always were.
        return form, None

    if problem is None and start is None and (end is not None or ongoing):
        problem = "content.month_start_missing"

    if problem is None and end is not None and start is not None and end < start and not ongoing:
        problem = "content.month_end_before_start"

    if problem:
        return form, t(problem)

    chosen = MonthRange(start, None if ongoing else end, ongoing and start is not None)
    original = form.get("period_original", "")
    # Saved back as it was read: its own words stay.
    if parse_period(original) == chosen:
        return {**form, "period": original}, None

    return {**form, "period": period_text(chosen, "en" if is_english() else "zh")}, None


def picker_values(period: str, form: dict[str, str] | None = None) -> dict[str, str]:
    """What the picker shows: the posted parts after a failed save, otherwise the time as read."""
    if form is not None and form.get("period_mode") in ("months", "text"):
        return {name: form.get(name, "") for name in PICKER_FIELDS}

    parsed = parse_period(period)
    values = {name: "" for name in PICKER_FIELDS}
    values["period_original"] = period or ""
    values["period_mode"] = "months" if parsed is not None else "text"

    if parsed is not None and parsed.start:
        values["period_start_year"], values["period_start_month"] = str(parsed.start[0]), str(parsed.start[1])

    if parsed is not None and parsed.end:
        values["period_end_year"], values["period_end_month"] = str(parsed.end[0]), str(parsed.end[1])

    if parsed is not None and parsed.ongoing:
        values["period_ongoing"] = "1"

    return values


def period_fits(text: str | None, single: bool = False) -> bool:
    """Whether a time can be shown in the picker as it is: empty, or one of the unambiguous forms (for a single month,
    one month without an end)."""
    parsed = parse_period(text)

    if parsed is None:
        return False

    return not single or (parsed.end is None and not parsed.ongoing)
