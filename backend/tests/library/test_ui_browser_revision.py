"""Browser acceptance revision (Codex, 2026-11-19): with JavaScript off, 清除 under the earliest start was shown.

Codex reproduced it in Chrome with JavaScript disabled on /job-preferences/edit: 清除 was on the page and took the
focus, yet a click or Space left 2026 / 7 as it was. Its handler only comes with preferences.js, and library.css's
`button { display: inline-flex }` beat the browser's own `[hidden] { display: none }`. The fix is one rule in
library.css, `[hidden] { display: none !important; }`: the hidden attribute always hides.

Checked here, offline:
- the stylesheet: that rule stands at the top level and is the only !important in it, so by the cascade (an author's
  important declaration outranks every normal one, whatever its selector) no other rule can show an element sent
  with hidden; and the pages load no other stylesheet and carry no style of their own;
- the page: 清除 is sent hidden and type="button" in the date row, after both lists; each list keeps its empty choice
  and is not disabled; the hooks the script needs are where js/preferences_clear.test.cjs builds them;
- without script (the form exactly as the browser sends it): emptying both lists and saving keeps no start and leaves
  其他要求 and everything else as it was; a year or a month alone is refused with the error right under the year and
  month, nothing is saved; in Chinese and in English;
- with script: js/preferences_clear.test.cjs runs the real preferences.js and leave_guard.js (6 cases).
What a real browser computes (display, boxes, Tab order, mouse and Space, scripts off and on, 1440 / 390 and a 200 %
equivalent, both languages) is in the evidence folder BROWSER1, from headless Chrome with JavaScript off through the
switch DevTools uses; the isolated run here forbids the network, so Chrome cannot start inside it.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

from app import i18n
from library_helpers import forms_in
from test_library_adoption import item
from test_results_page_script import node

BACKEND = Path(__file__).resolve().parents[2]
CSS = BACKEND / "app" / "library" / "static" / "library.css"
TEMPLATES = BACKEND / "app" / "library" / "templates"
SCRIPT_SUITE = Path(__file__).resolve().parent / "js" / "preferences_clear.test.cjs"
# The cases in the suite: all must run and pass, none skipped.
SCRIPT_CASES = 6
OTHER = "每周可到岗三天（合成）"
STARTED = dict(pref_rows="1", cities_r0="北京", cities_r0_stance="ideal", must_start_year="2026", must_start_month="7",
               must_other=OTHER)


def language(browser, lang: str) -> None:
    assert browser.post("/language", {"lang": lang, "next": "/workspace"}).status_code == 303


def words(lang: str, key: str) -> str:
    with i18n.using(lang):
        return i18n.t(key)


def top_level_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) of each rule outside @media and every other at-rule (an at-rule comes back as one
    entry whose selector starts with @)."""
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, depth, start, body, selector = [], 0, 0, 0, ""

    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                selector, body = text[start:index].strip(), index + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                rules.append((selector, text[body:index].strip()))
                start = index + 1

    return rules


def editor(browser):
    """The preferences editor page and its one form, as the browser would send it without script."""
    page = browser.get("/job-preferences/edit")
    assert page.status_code == 200
    forms = [form for form in forms_in(page.text) if form.method.lower() == "post" and form.action.startswith("/job-preferences")]
    assert len(forms) == 1, [form.action for form in forms]

    return page.text, forms[0]


def started(browser) -> str:
    """Saves preferences with the earliest start 2026 / 7 and 其他要求; returns the preferences' id."""
    response = browser.submit("/job-preferences/edit", "/job-preferences", **STARTED)
    assert response.status_code == 303, response.text
    _, form = editor(browser)
    assert (form.fields["must_start_year"], form.fields["must_start_month"]) == ("2026", "7")
    match = re.fullmatch(r"/job-preferences/([0-9a-f-]{36})/save", form.action)
    assert match, form.action

    return match.group(1)


def date_row(page: str) -> str:
    return page[page.index('id="l-start"'):page.index('id="must-other"')]


def stored(app, item_id: str) -> dict:
    return dict(item(app, item_id).content)


def test_the_hidden_attribute_always_hides_whatever_else_the_stylesheet_says():
    css = CSS.read_text(encoding="utf-8")
    rules = top_level_rules(css)

    assert [body for selector, body in rules if selector == "[hidden]"] == ["display: none !important;"]
    # The only !important anywhere in the stylesheet: nothing can outrank it, so button's inline-flex, .link-button,
    # a grid item's blockified display or any later rule leaves an element sent with hidden out of sight and out of
    # the tab order.
    assert re.sub(r"/\*.*?\*/", "", css, flags=re.S).count("!important") == 1
    # Every page is base.html: one stylesheet, and no template carries a style element or attribute of its own.
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert base.count('rel="stylesheet"') == 1 and '<link rel="stylesheet" href="/static/library.css">' in base

    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        assert "<style" not in source and not re.search(r"\sstyle=", source), template.name
        assert template.name == "base.html" or ("<html" not in source and "<link" not in source), template.name


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
def test_the_page_sends_clear_hidden_after_both_lists_and_each_list_keeps_its_empty_choice(browser_a, lang):
    language(browser_a, lang)
    page, form = editor(browser_a)
    row = date_row(page)

    assert f'<button type="button" class="link-button" data-must-clear hidden>{words(lang, "prefs.clear")}</button>' in row
    assert row.index('name="must_start_year"') < row.index('name="must_start_month"') < row.index("data-must-clear")
    assert row.count("data-must-clear") == 1 and page.count("data-must-clear") == 1

    for name, empty in (("must_start_year", "months.choose_year"), ("must_start_month", "months.choose_month")):
        opening = re.search(rf'<select name="{name}"[^>]*>', row).group(0)
        first = re.search(rf'<select name="{name}"[^>]*>\s*<option value="">([^<]*)</option>', row)
        assert first and first.group(1) == words(lang, empty), name
        assert "data-must-part" in opening and "disabled" not in opening, opening
        assert form.fields[name] == ""

    # The hooks js/preferences_clear.test.cjs builds: the editor form with its leave guard, the unsaved-changes line,
    # the leave dialog, the two scripts in this order; and the one stylesheet.
    assert re.search(r'<form method="post" action="/job-preferences" class="editor pref-editor" data-leave-guard data-pref-editor', page)
    assert '<p class="dirty-status" id="dirty-status" aria-live="polite"></p>' in page
    assert '<dialog id="leave-dialog"' in page
    assert '<script src="/static/preferences.js"></script><script src="/static/leave_guard.js"></script>' in page
    assert page.count('rel="stylesheet"') == 1 and '<link rel="stylesheet" href="/static/library.css">' in page


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
def test_without_script_emptying_both_lists_and_saving_keeps_no_start_and_the_rest_as_it_was(app, browser_a, lang):
    language(browser_a, lang)
    item_id = started(browser_a)
    before = stored(app, item_id)
    assert before["must_haves"].split("\n")[1:] == [OTHER] and "2026" in before["must_haves"].split("\n")[0]
    _, form = editor(browser_a)

    response = browser_a.post(form.action, {**form.fields, "must_start_year": "", "must_start_month": ""})

    assert response.status_code == 303, response.text
    after = stored(app, item_id)
    assert after["must_haves"] == OTHER
    assert {name: value for name, value in after.items() if name != "must_haves"} == {
        name: value for name, value in before.items() if name != "must_haves"
    }
    page, form = editor(browser_a)
    assert (form.fields["must_start_year"], form.fields["must_start_month"]) == ("", "")
    assert f">{OTHER}</textarea>" in page and "data-must-clear hidden" in date_row(page)


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
@pytest.mark.parametrize(("year", "month"), [("2026", ""), ("", "7")])
def test_a_year_or_a_month_alone_is_refused_with_the_error_right_under_the_year_and_month(app, browser_a, lang, year, month):
    language(browser_a, lang)
    item_id = started(browser_a)
    before = stored(app, item_id)
    _, form = editor(browser_a)

    response = browser_a.post(form.action, {**form.fields, "must_start_year": year, "must_start_month": month})

    assert response.status_code == 422
    row = date_row(response.text)
    error = f'<p class="field-error req-error" id="e-must">{words(lang, "prefs.start_incomplete")}</p>'
    assert error in row and response.text.count('id="e-must"') == 1
    assert row.index('name="must_start_month"') < row.index('id="e-must"') < row.index("data-must-clear hidden")
    assert row.count('aria-invalid="true"') == 2 and 'aria-describedby="e-must"' in row
    # What was chosen and typed is shown again, nothing is saved.
    chosen = re.findall(r'<option value="(\d+)" selected>', row)
    assert chosen == [value for value in (year, month) if value]
    assert f">{OTHER}</textarea>" in response.text
    assert stored(app, item_id) == before


def test_with_script_clear_empties_both_lists_touches_nothing_else_and_the_page_says_unsaved():
    binary = node()

    if binary is None:
        pytest.skip("这台电脑上没有 node：清除按钮脚本的离线测试没有执行")

    result = subprocess.run(
        [binary, "--test", "--test-reporter=tap", SCRIPT_SUITE.name],
        cwd=SCRIPT_SUITE.parent,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": os.environ.get("TMPDIR", "/tmp")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    counts = dict(re.findall(r"^# (tests|pass|fail|skipped|todo|cancelled) (\d+)$", result.stdout, re.M))

    assert result.returncode == 0, result.stdout + result.stderr
    assert counts == {"tests": str(SCRIPT_CASES), "pass": str(SCRIPT_CASES), "fail": "0", "skipped": "0", "todo": "0",
                      "cancelled": "0"}, result.stdout
