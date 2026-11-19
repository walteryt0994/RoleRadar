"""UI bilingual batch: the interface language layer (plan 6.1, 6.2 and 6.4).

Every system text is a message key with a Chinese and an English text, the same placeholders in both; nothing the
code or a template names is missing. The language is the browser's own choice, a whitelisted cookie set from this
site's pages only; it changes no record, task or result. In English the pages say nothing of their own in Chinese,
while the user's words are shown exactly as written. Synthetic data only.
"""
import re
from pathlib import Path

import pytest

from app import i18n
from app.discovery import synthetic
from app.i18n.catalog import MESSAGES
from job_helpers import confirmed, text_of

APP = Path(__file__).resolve().parents[2] / "app"
CJK = re.compile(r"[　-〿一-鿿＀-￯]")
PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# Messages that are only a separator, empty in one language by nature.
SEPARATORS = {"common.sentence_gap"}
# Dates name the month by number in Chinese and by name in English: both are given, each language uses its own.
OWN_PLACEHOLDERS = {"time.date_at", "time.full_date_at"}
# English messages that quote Chinese words the rules or the data hold as they are (a heading the simulated rules
# read, the placeholder a masked text carries): quoted, never translated.
QUOTE_CHINESE = {"imp.import_structure_confirm.13", "imp.cand.17", "imp.cand.20", "imp.cand.23"}
USED = re.compile(r"""\b(?:t|tn|lazy|t_in)\(\s*(?:[A-Za-z_.]+,\s*)?["']([a-z][a-z0-9_]*(?:\.[A-Za-z0-9_一-鿿-]+)+)["']""")


def english_forms(entry):
    english = entry[1]

    return english if isinstance(english, tuple) else (english,)


# --- the catalog ------------------------------------------------------------------------------------------------


def test_every_message_has_both_languages_and_the_same_placeholders():
    problems = []

    for key, entry in MESSAGES.items():
        chinese = entry[0]

        if key in SEPARATORS:
            continue

        if not chinese.strip():
            problems.append((key, "no Chinese"))

        for english in english_forms(entry):
            # A bare particle such as 的 may have no English word; nothing else may be empty.
            if not english.strip() and chinese.strip() not in ("的",):
                problems.append((key, "no English"))

            if set(PLACEHOLDER.findall(chinese)) != set(PLACEHOLDER.findall(english)) and key not in OWN_PLACEHOLDERS:
                problems.append((key, "placeholders", chinese, english))

    assert problems == []


def test_english_texts_hold_no_chinese():
    allowed = re.compile(r"[“”]")
    found = [
        (key, english)
        for key, entry in MESSAGES.items()
        for english in english_forms(entry)
        if CJK.search(allowed.sub("", english)) and key not in QUOTE_CHINESE
    ]

    assert found == []


def test_every_key_the_code_and_templates_name_is_in_the_catalog():
    missing = set()

    for path in [*APP.rglob("*.py"), *APP.rglob("*.html")]:
        for match in USED.finditer(path.read_text(encoding="utf-8")):
            if match.group(1) not in MESSAGES:
                missing.add((match.group(1), path.name))

    assert missing == set()


def test_a_missing_key_shows_as_the_key_and_never_breaks_a_page():
    assert i18n.t("no.such.key") == "no.such.key"


def test_english_says_one_and_many_apart_and_chinese_stays_as_it_is():
    with i18n.using("en"):
        assert i18n.t("dir.direction_input.7", p0=1) == "1 more experience is not confirmed yet and can be chosen once confirmed."
        assert i18n.t("dir.direction_input.7", p0=2) == "2 more experiences are not confirmed yet and can be chosen once confirmed."
        assert i18n.t("trash.with_attachments", n=1) == "with 1 attachment"
        assert i18n.t("job.discover_review.4", p0=1).startswith("The 1 posting you skipped will not be listed again this time, and leads pointing to it ")
        # A count that is not a whole number takes the other form; a count of 0 is plural in English.
        assert i18n.t("trash.with_attachments", n=0) == "with 0 attachments"
        assert i18n.tn("lib.delete_attachments_along", 1) == "1 attachment moves to the trash with it and is restored with it."

    with i18n.using("zh-CN"):
        assert i18n.t("dir.direction_input.7", p0=1) == "另有 1 段经历还没有确认，确认后才能选。"


def test_every_plural_marker_names_a_count_of_its_own_message():
    problems = []

    for key, entry in MESSAGES.items():
        if i18n.PLURAL.search(entry[0]):
            problems.append((key, "marker in Chinese"))

        for english in english_forms(entry):
            for name, one, other in i18n.PLURAL.findall(english):
                # The count itself is shown as well, and both forms say something.
                if "{" + name + "}" not in english or not one.strip() or not other.strip():
                    problems.append((key, name))

            # The pages' own scripts fill their messages themselves and know no markers.
            if key.startswith("js.") and i18n.PLURAL.search(english):
                problems.append((key, "marker in a script message"))

    assert problems == []


# --- choosing the language ---------------------------------------------------------------------------------------


def test_the_language_is_a_whitelisted_cookie_and_the_page_says_which(browser_a):
    assert '<html lang="zh-CN"' in browser_a.get("/workspace").text

    response = browser_a.post("/language", {"lang": "en", "next": "/experiences"})
    assert response.status_code == 303 and response.headers["location"] == "/experiences"
    assert "rr_lang=en" in response.headers["set-cookie"] and "httponly" in response.headers["set-cookie"].lower()
    assert '<html lang="en"' in browser_a.get("/workspace").text


@pytest.mark.parametrize("value", ["fr", "../en", "EN", "", "zh-CN; path=/"])
def test_an_unknown_language_is_refused_and_nothing_is_set(browser_a, value):
    response = browser_a.post("/language", {"lang": value, "next": "/workspace"})

    assert response.status_code == 400 and "set-cookie" not in response.headers


def test_the_language_switch_never_leaves_the_site_and_refuses_another_site(browser_a):
    away = browser_a.post("/language", {"lang": "en", "next": "https://example.com/"})
    assert away.status_code == 303 and away.headers["location"] == "/workspace"

    other = browser_a.client.post("/language", data={"lang": "en"}, headers={"origin": "https://example.com"})
    assert other.status_code in (400, 403)


def test_a_cookie_of_another_value_is_ignored(browser_a):
    browser_a.client.cookies.set(i18n.LANGUAGE_COOKIE, "fr")

    assert '<html lang="zh-CN"' in browser_a.get("/workspace").text


# --- English pages ----------------------------------------------------------------------------------------------


@pytest.fixture
def english_user(browser_a):
    """User A with English material only, reading the pages in English."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", directions_ideal="Data analysis", cities_ideal="Shanghai").status_code == 303
    item = confirmed(browser_a, title="Weekly sales dashboard", body="I cleaned the order data with Python and summarised weekly sales with SQL.")
    assert browser_a.post("/language", {"lang": "en", "next": "/workspace"}).status_code == 303

    return item


ENGLISH_PAGES = (
    "/workspace",
    "/experiences",
    "/experiences/new?kind=project",
    "/job-preferences",
    "/basic-profile",
    "/imports",
    "/directions",
    "/directions/records",
    "/discover",
    "/job-analysis",
    "/job-records",
    "/job-records/history",
    "/job-records/history?tab=searches",
    "/job-records/supply",
    "/data",
    "/data/trash",
    "/data/export",
)


@pytest.mark.parametrize("path", ENGLISH_PAGES)
def test_an_english_page_says_nothing_of_its_own_in_chinese(browser_a, english_user, path):
    response = browser_a.get(path)
    assert response.status_code == 200, (path, response.status_code)
    text = text_of(response.text)
    # The language menu names Chinese in Chinese, and the user's own name is theirs (a synthetic user's label here).
    for name in ("简体中文", "合成用户 A", *(grant.name for grant in synthetic.GRANTS)):
        # Synthetic sources keep their own names too: names, not the page's words.
        text = text.replace(name, "")

    leftover = CJK.findall(text)

    assert leftover == [], (path, "".join(leftover)[:80], re.findall(r".{0,30}[一-鿿].{0,30}", text)[:3])


def test_an_english_record_page_shows_the_users_words_as_written(browser_a, english_user):
    page = text_of(browser_a.get(f"/experiences/{english_user}").text)

    assert "Weekly sales dashboard" in page and "summarised weekly sales with SQL" in page
    assert not CJK.findall(page.replace("简体中文", "").replace("合成用户 A", ""))


def test_chinese_words_stay_chinese_on_an_english_page(browser_a):
    item = confirmed(browser_a, title="校园订单看板（合成）", body="用 Python 清洗订单数据。")
    browser_a.post("/language", {"lang": "en", "next": "/workspace"})
    page = text_of(browser_a.get(f"/experiences/{item}").text)

    assert "校园订单看板（合成）" in page and "用 Python 清洗订单数据。" in page


def test_switching_the_language_changes_no_record(app, browser_a):
    item = confirmed(browser_a, title="不变的标题（合成）", body="原文。")
    before = browser_a.get(f"/experiences/{item}/history").text
    browser_a.post("/language", {"lang": "en", "next": "/workspace"})
    browser_a.post("/language", {"lang": "zh-CN", "next": "/workspace"})

    assert browser_a.get(f"/experiences/{item}/history").text == before


def test_every_key_a_message_map_names_is_in_the_catalog():
    """A code's words are looked up only when a page is rendered: a key a map names but the catalog lacks would show
    as the key itself, so every map of every module is checked here instead."""
    import importlib
    import pkgutil

    import app

    missing = set()

    for info in pkgutil.walk_packages(app.__path__, "app."):
        module = importlib.import_module(info.name)

        for name, value in vars(module).items():
            if isinstance(value, i18n.MessageMap):
                missing |= {(info.name, name, value.key_of(code)) for code in value if value.key_of(code) not in MESSAGES}

    assert missing == set()
