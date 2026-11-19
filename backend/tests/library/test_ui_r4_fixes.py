"""UI R4 (Codex's review of the final browser acceptance; report section 16): the five English issues of section 15.4.

1. How a direction decision's basis stands, and the field names of an exported exploration, are said in the language
   of the page or package, never in words taken when a module was imported; Chinese stays word for word.
2. A search summary, its searches and its daily watch name the sources this app registers in the page's language;
   what the summary kept is unchanged, and any other name is shown as written.
3. An English sentence and the link after it have a space between them.
4. Every English note of the simulated decider fits in NOTE_CHARS, so a step shows it whole; the limit and the masking
   are unchanged.
5. An English fact list has a wider label column, never smaller type; a narrow screen still stacks label and value.
"""
import io
import re
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app import i18n
from app.discovery import agent, records_web
from app.i18n.catalog import MESSAGES
from app.library import direction_export
from app.library.job_words import source_label
from app.library.models import DirectionExploration, DirectionSource, JobSearch
from app.library.references import BasisState
from job_helpers import InlineExecutor, text_of
from test_direction_pages import decide, directions_shown, explored
from test_discovery_pages import stored
from test_job_record_pages import finished, inline, library  # noqa: F401 (fixtures)
from test_ui_english_search import english_search

APP = Path(__file__).resolve().parents[2] / "app"
TEMPLATES = APP / "library" / "templates"
CSS = APP / "library" / "static" / "library.css"
HAN = re.compile(r"[一-鿿]")


def english(browser) -> None:
    assert browser.post("/language", {"lang": "en", "next": "/workspace"}).status_code == 303


def chinese(browser) -> None:
    assert browser.post("/language", {"lang": "zh-CN", "next": "/workspace"}).status_code == 303


def words(key: str) -> tuple[str, str]:
    zh, en = MESSAGES[key]

    return zh, en if isinstance(en, str) else en[-1]


@pytest.fixture
def directions_inline(app):
    app.state.directions.executor = InlineExecutor()


def kept_direction(browser, item) -> str:
    """An exploration of item, saved, with its first direction kept on it; the exploration's page."""
    task = explored(browser, [item])
    assert browser.post(f"{task}/save", {"csrf_token": browser.csrf_token()}).status_code == 303
    name = directions_shown(browser.get(task).text)[0]
    assert decide(browser, task, name, "keep").status_code == 303

    return task


def package_file(browser, suffix: str) -> str:
    response = browser.get("/export/all.zip")
    assert response.status_code == 200, response.text[:200]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    [name] = [name for name in archive.namelist() if name.endswith(suffix)]

    return archive.read(name).decode("utf-8")


# --- 1. a decision's basis and an exported exploration's fields --------------------------------------------------------


def test_every_basis_state_is_looked_up_when_read_and_chinese_is_unchanged():
    assert isinstance(direction_export.BASIS_STATE_LABELS, i18n.MessageMap)
    assert set(direction_export.BASIS_STATE_LABELS) == set(BasisState)

    for state in BasisState:
        key = direction_export.BASIS_STATE_LABELS.key_of(state)

        with i18n.using("zh-CN"):
            assert direction_export.BASIS_STATE_LABELS[state] == words(key)[0]

        with i18n.using("en"):
            assert direction_export.BASIS_STATE_LABELS[state] == words(key)[1]


def test_every_field_name_the_package_writes_is_in_its_language_and_chinese_is_unchanged():
    from app.library.content import ALL_KINDS, CATEGORY_FIELDS

    # What the package wrote before: one name, the last kind's words (UI R4 keeps the choice, not the import-time words).
    before = {spec.name: spec.label for kind in ALL_KINDS for spec in CATEGORY_FIELDS[kind]}

    with i18n.using("zh-CN"):
        assert {name: direction_export.field_words(name) for name in before} == before

    with i18n.using("en"):
        shown = {name: direction_export.field_words(name) for name in before}

    assert all(not HAN.search(label) and label != name for name, label in shown.items()), shown
    assert direction_export.field_words("not_a_field") == "not_a_field" and direction_export.field_words(None) is None


def test_a_decisions_basis_is_said_in_the_pages_language_and_follows_a_switch(app, directions_inline, browser_a, library):
    kept_direction(browser_a, library)
    zh, en = words("dir.exp_md.1")

    # The decisions are the records page's second tab (owner redesign, plan 4.3).
    in_chinese = text_of(browser_a.get("/directions/records?tab=decisions").text)
    english(browser_a)
    in_english = text_of(browser_a.get("/directions/records?tab=decisions").text)
    chinese(browser_a)
    back_in_chinese = text_of(browser_a.get("/directions/records?tab=decisions").text)

    assert zh in in_chinese and en not in in_chinese
    assert en in in_english and zh not in in_english
    assert back_in_chinese == in_chinese


def test_an_english_package_says_the_basis_and_the_fields_in_english_and_keeps_the_users_words(app, directions_inline, browser_a, library):
    kept_direction(browser_a, library)

    with app.state.library.database.transaction() as db:
        fields = sorted({name for row in db.scalars(select(DirectionSource)) for name in (row.fields or [])})
        [exploration] = db.scalars(select(DirectionExploration)).all()
        quotes = [quote["text"] for direction in exploration.content["directions"] for quote in direction.get("evidence", [])]

    chinese_md = package_file(browser_a, "directions.md")
    english(browser_a)
    english_md = package_file(browser_a, "directions.md")

    with i18n.using("zh-CN"):
        zh_fields = [direction_export.field_words(name) for name in fields]

    with i18n.using("en"):
        en_fields = [direction_export.field_words(name) for name in fields]

    zh, en = words("dir.exp_md.1")
    assert fields and zh in chinese_md and en in english_md and zh not in english_md
    assert all(label in chinese_md for label in zh_fields) and all(label in english_md for label in en_fields)
    # What the package says of its basis is all English; a quote's own words stay, the field it came from does not.
    source_lines = [line for line in english_md.splitlines() if line.startswith("- Basis: ")]
    quote_lines = [line for line in english_md.splitlines() if line.startswith("- Basis in your experiences: ")]
    assert source_lines and not any(HAN.search(line) for line in source_lines), source_lines
    assert all(not HAN.search(line.rsplit("(", 1)[-1]) for line in quote_lines), quote_lines
    # The user's own words, quoted by the exploration, are written as they are in both packages.
    assert quotes and all(quote in chinese_md and quote in english_md for quote in quotes), quotes


# --- 2. registered source names on the summary, its searches and its watch ----------------------------------------------


def test_a_summary_its_searches_and_its_watch_name_registered_sources_in_the_pages_language(app, browser_a, library, inline):
    url = finished(browser_a, library)
    assert browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}).status_code == 303

    with app.state.library.database.transaction() as db:
        record = db.scalar(select(JobSearch))
        kept = [dict(source) for source in record.summary["sources"]]

    names = [source["name"] for source in kept]
    with i18n.using("en"):
        shown = [source_label(name) for name in names]
    assert all(HAN.search(name) for name in names) and all(not HAN.search(name) for name in shown)

    pages = (f"/job-records/searches/{record.id}", f"/job-records/searches/{record.id}/watch", "/job-records/history?tab=searches")
    in_chinese = [text_of(browser_a.get(path).text) for path in pages]
    english(browser_a)
    in_english = [text_of(browser_a.get(path).text) for path in pages]

    for path, zh_page, en_page in zip(pages, in_chinese, in_english):
        assert names[0] in zh_page and shown[0] not in zh_page, path
        assert shown[0] in en_page and not any(name in en_page for name in names), path

    # The searches as kept say where they were made in the page's language too.
    assert f"{shown[0]} · search query" in in_english[0]

    with app.state.library.database.transaction() as db:
        assert [dict(source) for source in db.get(JobSearch, record.id).summary["sources"]] == kept


def test_a_source_name_the_app_does_not_register_is_shown_as_it_was_kept():
    kept = [{"source_id": "board-x", "name": "某某公司招聘官网"}, {"source_id": "careers", "name": "合成招聘官网"}]

    with i18n.using("en"):
        names = records_web._source_names(kept)

    assert names["board-x"] == "某某公司招聘官网"
    assert names["careers"] == "Synthetic careers site"


# --- 3. a space between an English sentence and its link -----------------------------------------------------------


CALL = r'\{\{\s*t\("(?P<NAME>[^"]+)"[^}]*\}\}'
# A message, then only a link's tag (or nothing), then the next message.
PAIR = re.compile(CALL.replace("NAME", "left") + r"(?P<between>(?:</?a\b[^>]*>)*)" + CALL.replace("NAME", "right"))
# A message ending a sentence or a label, then a value written straight after it (where a quote came from, say).
VALUE_AFTER = re.compile(CALL.replace("NAME", "left") + r"(?:</?a\b[^>]*>)*\{\{\s*[a-z_][\w.]*(?:\|\w+)?\s*\}\}")


def english_of(key: str) -> str | None:
    # A key put together in the template ("lib.version_when." ~ kind) is not one message; the scan leaves it.
    return words(key)[1] if key in MESSAGES else None


def touching(left: str, right: str) -> bool:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return False

    if left[-1] in ".!?:;":
        return right[0].isalnum() or right[0] in "\"'(“"

    return left[-1].isalnum() and right[0].isalnum()


def test_no_english_sentence_runs_into_the_link_or_message_after_it():
    found = []

    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")

        for match in PAIR.finditer(text):
            left, right = english_of(match["left"]), english_of(match["right"])

            if touching(left, right):
                found.append((path.name, text[: match.start()].count("\n") + 1, left[-20:], right[:20]))

        for match in VALUE_AFTER.finditer(text):
            left = english_of(match["left"])

            if left and left[-1] in ".:;!?":
                found.append((path.name, text[: match.start()].count("\n") + 1, left[-20:], "{{ value }}"))

    assert found == []


def test_saved_and_the_link_to_the_saved_exploration_have_a_space_in_english_only(app, directions_inline, browser_a, library):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    in_chinese = browser_a.get(task).text
    english(browser_a)
    in_english = browser_a.get(task).text

    assert "已保存。<a href=" in in_chinese
    assert "Saved. <a href=" in in_english


# --- 4. the simulated decider's English notes, whole ---------------------------------------------------------------------


def test_every_english_note_fits_and_passes_the_masking_unchanged():
    assert agent.NOTE_CHARS == 60

    notes = [*agent.SIMULATED_NOTES_EN.values(), *(agent.SIMULATED_RETRY_EN.format(type=kind) for kind in ("实习", "Internship"))]

    for note in notes:
        assert len(note) <= agent.NOTE_CHARS and agent.safe_note(note) == note, note


def test_an_english_search_shows_every_note_whole(app, browser_a, library):
    url = english_search(app, browser_a, library)
    page = text_of(browser_a.get(url).text)
    notes = [step.note for step in stored(app, browser_a, url).finished.trace.steps if step.note]

    assert notes and set(notes) & set(agent.SIMULATED_NOTES_EN.values())
    assert all(not note.endswith("…") for note in notes), notes
    # The steps are no longer listed on the result page (owner redesign, plan 5.3); the running page shows each step
    # with its note as the trace keeps it, so a whole note in the trace is a whole note shown.
    assert "Step 1" not in page and all(len(note) <= agent.NOTE_CHARS for note in notes)


# --- 5. the label column of an English fact list ---------------------------------------------------------------------


def test_an_english_fact_list_has_a_wider_label_column_and_a_narrow_screen_still_stacks_it():
    css = CSS.read_text(encoding="utf-8")
    base = re.search(r":root \{ --fact-label: ([\d.]+)em; \}", css)
    wider = re.search(r":root:lang\(en\) \{ --fact-label: ([\d.]+)em; \}", css)
    narrow = css[css.index("@media (max-width: 719px)"):]

    assert base and wider and float(base[1]) == 5 and float(wider[1]) >= 8
    assert ".facts div { display: grid; grid-template-columns: var(--fact-label) minmax(0, 1fr);" in css
    assert ".facts div { grid-template-columns: minmax(0, 1fr); gap: 2px; }" in narrow
    # Wider, never smaller: no English rule changes a font size.
    assert not re.search(r":lang\(en\)[^{]*\{[^}]*font-size", css)


def test_an_english_page_declares_its_language_so_the_wider_column_applies(browser_a):
    english(browser_a)

    assert '<html lang="en"' in browser_a.get("/job-records").text
