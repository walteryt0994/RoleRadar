"""The job preferences as rows, for the overview and the three-step editor (owner redesign, plan 3.3).

The preferences are stored as they always were: for each dimension (cities, job types, fields, work content, ways of
working) one text per stance (理想 / 也可以接受 / 不能接受), plus two single texts (希望减少的内容, the must-have
requirements). Nothing here changes that storage.

- Reading: every stance text that holds something is one row, its words as stored. A text is never split into items
  by guessing (no commas, no line breaks), so a list someone wrote stays one row they can edit.
- Saving from the editor: each row has its own stance. The rows of one stance are folded back into that stance's
  text; one row is saved exactly as typed, several are put one per line (the way the search already reads a list,
  app.discovery.rules.SEPARATORS). A row left empty is dropped. Saved back unchanged, every text is what it was.
- Which lines were rows of their own is kept beside the texts, in the same stored content (ROWS_KEY, "editor_rows":
  {dimension: {stance: [row, row, ...]}}), only for a stance with two or more rows. It is optional and read back only
  while its rows, one per line, are still exactly that stance's text: then each is a row again (北京 and 上海, both
  理想, stay two rows). A stance text without it (anything saved before, or by another tool) stays one row as stored,
  never split. Nothing else reads it: the overview, history, export and analyses read the stance texts as before.
- The requirements (must_haves) stay one firm condition. Its start month is shown in a month picker only when its first
  line is written in one of two exact forms (zh "最早 2026 年 7 月入职", en "Earliest start: 2026-07"); a changed month
  is written in that form in the page's language, an unchanged one keeps its words; the rest is "其他要求", kept as
  written. No date is read out of other words.
"""
import re
from dataclasses import replace

from app.i18n import is_english, t
from app.library.content import PREFERENCE_DIMENSIONS, PREFERENCE_STANCES
from app.library.errors import ContentError

DIMENSIONS = tuple(key for key, _, _ in PREFERENCE_DIMENSIONS)
STANCES = tuple(stance for stance, _ in PREFERENCE_STANCES)
# The editor's three steps (plan 3.3): 求职目标, 工作偏好, 求职要求.
STEPS = (
    ("goals", ("directions", "job_types", "cities")),
    ("work", ("work_content", "work_style")),
    ("requirements", ()),
)
# The most rows one dimension can hold in the editor: three stored texts and the rows added since.
MAX_ROWS = 12
# The job types the editor offers as choices; they are saved as these words and shown in the page's language.
JOB_TYPES = ("全职", "实习", "兼职")
JOB_TYPE_KEYS = {"全职": "prefs.type.full_time", "实习": "prefs.type.internship", "兼职": "prefs.type.part_time"}
EDITOR_MARK = "pref_rows"
# The rows the editor saved for a stance with two or more of them (optional metadata in the stored content).
ROWS_KEY = "editor_rows"
_ROW = re.compile(r"^(" + "|".join(DIMENSIONS) + r")_r(\d+)(_stance)?$")
_START_ZH = re.compile(r"^最早\s*(\d{4})\s*年\s*(\d{1,2})\s*月入职$")
_START_EN = re.compile(r"^Earliest start:\s*(\d{4})-(\d{2})$")


def _items(content: dict, key: str, stance: str) -> list[str]:
    """The rows of one stance as stored: the editor's rows when they still make up its text exactly, otherwise its
    text as one row (or none)."""
    text = content.get(f"{key}_{stance}", "")
    kept = content.get(ROWS_KEY)
    items = kept.get(key, {}).get(stance) if isinstance(kept, dict) and isinstance(kept.get(key), dict) else None

    if (
        isinstance(items, list)
        and len(items) >= 2
        and all(isinstance(item, str) and item.strip() and item == item.strip() and "\n" not in item for item in items)
        and "\n".join(items) == text
    ):
        return list(items)

    return [text] if text.strip() else []


def rows_of(content: dict) -> dict[str, list[tuple[str, str]]]:
    """Each dimension's rows as stored: (stance, text) for every row a stance holds (app docstring: one per stance
    text, or the editor's rows when they were kept)."""
    return {key: [(stance, item) for stance in STANCES for item in _items(content, key, stance)] for key in DIMENSIONS}


def rows_kept(content: dict) -> dict:
    """The editor's rows kept for this content (ROWS_KEY), or none."""
    kept = content.get(ROWS_KEY)

    return kept if isinstance(kept, dict) else {}


def with_rows(entry, folded: dict):
    """The checked entry (parse_experience_form) with the editor's rows of fold_rows kept beside its texts: only those
    that still make up their stance's text exactly, as checked and stored."""
    kept = folded.get(ROWS_KEY)

    if not isinstance(kept, dict):
        return entry

    rows = {
        key: {stance: items for stance, items in stances.items() if "\n".join(items) == entry.content.get(f"{key}_{stance}")}
        for key, stances in kept.items()
        if key in DIMENSIONS
    }
    rows = {key: stances for key, stances in rows.items() if stances}

    if not rows:
        return entry

    return replace(entry, content={**entry.content, ROWS_KEY: rows})


def type_label(value: str) -> str:
    """A job type the editor offers, in the page's language; any other words as written."""
    return t(JOB_TYPE_KEYS[value]) if value in JOB_TYPE_KEYS else value


def start_of(must_haves: str | None) -> tuple[tuple[int, int] | None, str]:
    """The start month a requirements text begins with (in one of the two exact forms), and the rest of it."""
    text = must_haves or ""
    first, _, rest = text.partition("\n")
    match = _START_ZH.match(first.strip()) or _START_EN.match(first.strip())

    if not match:
        return None, text

    year, month = int(match.group(1)), int(match.group(2))

    if not 1 <= month <= 12:
        return None, text

    return (year, month), rest


def start_line(month: tuple[int, int]) -> str:
    if is_english():
        return f"Earliest start: {month[0]:04d}-{month[1]:02d}"

    return f"最早 {month[0]} 年 {month[1]} 月入职"


def _clean(text: str) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def fold_rows(form: dict[str, str]) -> dict[str, str]:
    """The stored fields as the editor's rows say, ready for the usual checks (parse_experience_form).

    A form without the editor's rows (an older page, another tool) is returned as it came; a stored field posted by its
    own name next to the rows says that field itself. Rows with an unknown stance or too many rows are refused, never
    guessed."""
    if form.get(EDITOR_MARK) != "1":
        return form

    rows: dict[str, dict[int, dict[str, str]]] = {key: {} for key in DIMENSIONS}

    for name, value in form.items():
        match = _ROW.match(name)

        if not match:
            continue

        key, index, is_stance = match.group(1), int(match.group(2)), bool(match.group(3))

        if index >= MAX_ROWS:
            raise ContentError(t("content.fix_and_save"), {f"{key}_ideal": t("prefs.too_many_rows", most=MAX_ROWS)})

        rows[key].setdefault(index, {})["stance" if is_stance else "text"] = value

    folded = {name: value for name, value in form.items() if not _ROW.match(name) and name != ROWS_KEY}
    errors = {}
    kept: dict[str, dict[str, list[str]]] = {}

    for key in DIMENSIONS:
        by_stance: dict[str, list[str]] = {stance: [] for stance in STANCES}

        for index in sorted(rows[key]):
            text = _clean(rows[key][index].get("text", ""))
            stance = rows[key][index].get("stance", "")

            if not text:
                continue

            if stance not in STANCES:
                errors[f"{key}_ideal"] = t("prefs.choose_stance")
                continue

            by_stance[stance].append(text)

        for stance in STANCES:
            # A stored field posted by name (another tool filling the editor's form) says that stance itself.
            if f"{key}_{stance}" not in form:
                folded[f"{key}_{stance}"] = "\n".join(by_stance[stance])

                # Two or more rows of one stance: which lines were rows is kept beside the text (ROWS_KEY).
                if len(by_stance[stance]) >= 2:
                    kept.setdefault(key, {})[stance] = list(by_stance[stance])

    if errors:
        raise ContentError(t("content.fix_and_save"), errors)

    if kept:
        folded[ROWS_KEY] = kept

    if "must_haves" not in form:
        folded["must_haves"] = _requirements(form)

    return folded


def _requirements(form: dict[str, str]) -> str:
    """The requirements text from the start month and the other requirements, keeping the original words when the
    month and the rest are unchanged."""
    original = form.get("must_original", "")
    year, month = form.get("must_start_year", "").strip(), form.get("must_start_month", "").strip()
    other = _clean(form.get("must_other", ""))

    if bool(year) != bool(month):
        raise ContentError(t("content.fix_and_save"), {"must_haves": t("prefs.start_incomplete")})

    chosen = None

    if year:
        if not (year.isascii() and year.isdigit() and month.isascii() and month.isdigit()) or not 1 <= int(month) <= 12:
            raise ContentError(t("content.fix_and_save"), {"must_haves": t("content.month_invalid")})

        chosen = (int(year), int(month))

    was_start, was_rest = start_of(original)

    if chosen == was_start and other == _clean(was_rest):
        return original

    if chosen == was_start and was_start is not None:
        # The month as it was: its words stay; only the rest changed.
        return "\n".join(part for part in (original.partition("\n")[0], other) if part)

    return "\n".join(part for part in ((start_line(chosen) if chosen else ""), other) if part)


def posted_rows(form: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    """The editor's rows as posted, in their order, to show the editor again after a failed save (nothing typed is
    lost; an empty row stays out)."""
    rows: dict[str, dict[int, dict[str, str]]] = {key: {} for key in DIMENSIONS}

    for name, value in form.items():
        match = _ROW.match(name)

        if match and int(match.group(2)) < MAX_ROWS:
            rows[match.group(1)].setdefault(int(match.group(2)), {})["stance" if match.group(3) else "text"] = value

    shown = {
        key: [
            (rows[key][index].get("stance", ""), rows[key][index].get("text", ""))
            for index in sorted(rows[key])
            if rows[key][index].get("text", "").strip()
        ]
        for key in DIMENSIONS
    }

    # A stored field posted by its own name says that stance itself (fold_rows): shown as that stance's one row.
    for key in DIMENSIONS:
        for stance in STANCES:
            if f"{key}_{stance}" in form:
                shown[key] = [row for row in shown[key] if row[0] != stance]

                if form[f"{key}_{stance}"].strip():
                    shown[key].append((stance, form[f"{key}_{stance}"]))

    return shown


def requirement_parts(must_haves: str | None, form: dict[str, str] | None = None) -> dict[str, str]:
    """What step 3 shows: the posted parts after a failed save, otherwise the stored text read as a start month (in
    one of the two exact forms) and the rest."""
    if form is not None and form.get(EDITOR_MARK) == "1" and "must_haves" in form:
        month, rest = start_of(form["must_haves"])

        return {
            "original": form.get("must_original", ""),
            "year": str(month[0]) if month else "",
            "month": str(month[1]) if month else "",
            "other": rest,
        }

    if form is not None and form.get(EDITOR_MARK) == "1":
        return {
            "original": form.get("must_original", ""),
            "year": form.get("must_start_year", ""),
            "month": form.get("must_start_month", ""),
            "other": form.get("must_other", ""),
        }

    month, rest = start_of(must_haves)

    return {
        "original": must_haves or "",
        "year": str(month[0]) if month else "",
        "month": str(month[1]) if month else "",
        "other": rest,
    }
