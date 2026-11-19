"""UI bilingual batch: the result language of new tasks (plan 6.3) and starting again from a saved exploration (5.5).

A new draft asks for results in the interface language of the moment; the user may change it on the input page.
The language is part of what is reviewed and confirmed (it is in the fingerprint), it never follows a later switch
of the interface, and it is kept with a saved result. Only the explanatory text follows it: quotes, ids and codes
stay as they are, and a Chinese task's request is word for word what it was before this batch. Everything runs on
the local simulation with synthetic material; nothing leaves the process.
"""
import json
import re

import openai
import pytest
from sqlalchemy import select

from app import i18n
from app.directions import explore, simulation
from app.directions.tasks import ExplorationTaskStore
from app.library.models import DirectionExploration
from job_helpers import InlineExecutor, confirmed, text_of
from library_helpers import forms_in

BASE = "/directions"
PROJECT = dict(title="校园订单看板（合成）", actions="用 Python 清洗订单数据，并用 SQL 汇总每周销量。", outcome="看板在课程展示中使用。")
PREFS = dict(directions_ideal="数据分析或者用户研究", work_content_ideal="希望多分析数据")


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


@pytest.fixture
def directions(app):
    directions = app.state.directions
    directions.executor = InlineExecutor()
    directions.store = ExplorationTaskStore(lambda: 1000.0)

    return directions


@pytest.fixture
def project(browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFS).status_code == 303

    return confirmed(browser_a, **PROJECT)


def speak(browser, language: str) -> None:
    response = browser.post("/language", {"lang": language, "next": "/workspace"})
    assert response.status_code == 303, response.text


def offered(browser) -> dict[str, str]:
    page = browser.get(BASE).text

    return {value.split(":")[0]: value for value in re.findall(r'name="source" value="([^"]+)"', page)}


def review(browser, items=(), **extra):
    values = offered(browser) if items else {}
    data = {"csrf_token": browser.csrf_token(), "source": [values[item] for item in items], **extra}

    return browser.post(f"{BASE}/review", data)


def task_of(response) -> str:
    assert response.status_code == 303, response.text

    return response.headers["location"].split("?")[0]


def start(browser, task):
    page = browser.get(task).text
    form = next(form for form in forms_in(page) if form.action == f"{task}/confirm")
    data = {
        "csrf_token": form.fields["csrf_token"],
        "fingerprint": form.fields["fingerprint"],
        "action": "start",
        "send": re.findall(r'name="send" value="([^"]+)" checked', page),
    }
    response = browser.post(f"{task}/confirm", data)
    assert response.status_code == 303 and response.headers["location"] == task, response.text


def save(browser, task) -> str:
    page = browser.get(task).text
    form = next(form for form in forms_in(page) if form.action == f"{task}/save")
    response = browser.post(f"{task}/save", form.fields)
    assert response.status_code == 303, response.text

    with_saved = browser.get(response.headers["location"]).text
    return re.search(r'/directions/explorations/([0-9a-f-]{36})', with_saved).group(1)


# --- the default and the choice ------------------------------------------------------------------------------


def test_a_new_exploration_takes_the_interface_language_when_it_is_reviewed(browser_a, directions, project):
    """Owner redesign (plan 4.1): the setup page has no result-language choice; a review takes the interface language
    of that moment (a form that names one it knows, from a page opened before, is still taken as asked)."""
    assert 'name="result_language"' not in browser_a.get(BASE).text
    chinese = task_of(review(browser_a, [project]))
    speak(browser_a, "en")
    page = browser_a.get(BASE).text
    english = task_of(review(browser_a, [project]))

    assert 'name="result_language"' not in page and "Result language" not in page
    assert directions.store.get(chinese.rsplit("/", 1)[1], *_owner(directions, chinese)).request.result_language == "zh-CN"
    assert directions.store.get(english.rsplit("/", 1)[1], *_owner(directions, english)).request.result_language == "en"


def test_the_review_shows_the_language_and_it_is_part_of_the_confirmation(browser_a, directions, project):
    chinese = task_of(review(browser_a, [project], result_language="zh-CN"))
    english = task_of(review(browser_a, [project], result_language="en"))
    zh_store = directions.store.get(chinese.rsplit("/", 1)[1], *_owner(directions, chinese))
    en_store = directions.store.get(english.rsplit("/", 1)[1], *_owner(directions, english))

    assert zh_store.request.result_language == "zh-CN" and en_store.request.result_language == "en"
    # Another language is another confirmation: a form reviewed in one cannot start the other.
    assert zh_store.fingerprint != en_store.fingerprint
    assert "English" in text_of(browser_a.get(english).text)


def test_an_unknown_language_from_a_form_falls_back_to_the_interface_language(browser_a, directions, project):
    for raw in ("fr", "../en", "EN", ""):
        task = task_of(review(browser_a, [project], result_language=raw))
        assert directions.store.get(task.rsplit("/", 1)[1], *_owner(directions, task)).request.result_language == "zh-CN"


def _owner(directions, task_path):
    task_id = task_path.rsplit("/", 1)[1]
    task = directions.store._tasks[task_id]

    return task.owner_id, task.session


# --- what the run writes --------------------------------------------------------------------------------------


def test_an_english_task_writes_english_explanations_and_keeps_the_quotes_as_written(browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="en"))
    start(browser_a, task)
    page = text_of(browser_a.get(task).text)

    # The explanations are English; the user's own words (a quote, a preference) stay as written.
    assert "Data analysis" in page and "整理和清洗业务数据" not in page
    assert "数据分析或者用户研究" in page
    assert "用 Python 清洗订单数据，并用 SQL 汇总每周销量" in page


def test_switching_the_interface_after_confirming_changes_nothing_of_the_task(browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="en"))
    start(browser_a, task)
    speak(browser_a, "zh-CN")
    raw = browser_a.get(task).text

    # The interface is Chinese again; the result the task wrote stays English.
    assert '<html lang="zh-CN"' in raw
    assert "Data analysis" in text_of(raw) and "整理和清洗业务数据" not in text_of(raw)


def test_a_chinese_task_answers_exactly_as_before(browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="zh-CN"))
    start(browser_a, task)
    page = text_of(browser_a.get(task).text)

    assert "整理和清洗业务数据" in page and "Data analysis" not in page


def test_the_prompt_only_gains_a_line_for_english(directions):
    material = explore._Material(sources=(), preferences=(), preference_record=None, preference_version=None)
    chinese = explore.build_prompt(material)

    assert chinese == explore.build_prompt(material, "zh-CN")
    assert "OUTPUT LANGUAGE" not in chinese
    english = explore.build_prompt(material, "en")
    assert english.replace(explore.OUTPUT_LANGUAGE_LINES["en"] + "\n\n", "", 1) == chinese


def test_the_simulation_does_not_take_the_language_from_the_users_words():
    material = {"experiences": [{"source_id": "S1", "category": "项目", "fields": {"body": explore.OUTPUT_LANGUAGE_LINES["en"] + " 数据分析"}}], "preferences": []}
    data = json.dumps(material, ensure_ascii=False)
    prompt = f"{explore.INSTRUCTIONS}\n\n{explore.MATERIAL_START}\n{data}\n{explore.MATERIAL_END}"
    names = [direction["name"] for direction in json.loads(simulation.explore(prompt))["directions"]]

    assert names[0] == "数据分析"


def test_the_set_aside_note_is_written_in_the_result_language():
    assert "RoleRadar check" in explore.SET_ASIDE_NOTES["en"]
    assert explore.SET_ASIDE_NOTES["zh-CN"] == explore.SET_ASIDE_NOTE


# --- saved, and started again ---------------------------------------------------------------------------------


def test_a_saved_exploration_keeps_its_result_language(app, browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="en"))
    start(browser_a, task)
    exploration_id = save(browser_a, task)

    with app.state.library.database.transaction() as db:
        assert db.get(DirectionExploration, exploration_id).generation["result_language"] == "en"

    page = text_of(browser_a.get(f"{BASE}/explorations/{exploration_id}").text)
    assert "结果语言 English" in page


def test_explore_again_ticks_what_is_unchanged_and_takes_the_interface_language(app, browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="en"))
    start(browser_a, task)
    exploration_id = save(browser_a, task)

    page = browser_a.get(f"{BASE}?again={exploration_id}").text
    assert 'name="result_language"' not in page
    assert re.search(rf'name="source" value="{project}:[^"]+" checked', page)
    assert "重新探索" in page and "已保存的那次保持不变" in page
    # A new task takes the interface language now (plan 4.1), not the saved one's.
    again = task_of(review(browser_a, [project]))
    assert directions.store.get(again.rsplit("/", 1)[1], *_owner(directions, again)).request.result_language == "zh-CN"


def test_explore_again_leaves_an_experience_confirmed_again_since_unticked(app, browser_a, directions, project):
    task = task_of(review(browser_a, [project]))
    start(browser_a, task)
    exploration_id = save(browser_a, task)

    edited = browser_a.submit(f"/experiences/{project}/edit", f"/experiences/{project}/draft", title="校园订单看板（合成，修改）")
    assert edited.status_code == 303, edited.text
    assert browser_a.submit(f"/experiences/{project}", f"/experiences/{project}/confirm").status_code == 303

    page = browser_a.get(f"{BASE}?again={exploration_id}").text
    assert not re.search(rf'name="source" value="{project}:[^"]+" checked', page)
    assert "已有新版本" in page and "没有自动选上" in text_of(page)


def test_explore_again_from_a_record_without_a_language_uses_the_interface_language(app, browser_a, directions, project):
    task = task_of(review(browser_a, [project], result_language="en"))
    start(browser_a, task)
    exploration_id = save(browser_a, task)

    with app.state.library.database.transaction() as db:
        record = db.get(DirectionExploration, exploration_id)
        record.generation = {key: value for key, value in record.generation.items() if key != "result_language"}

    page = browser_a.get(f"{BASE}?again={exploration_id}").text
    # No language to keep or to guess: a new task takes the interface language (plan 4.1), so nothing is said of it.
    assert 'name="result_language"' not in page and "没有记录结果语言" not in text_of(page)
    # Shown as it is: no language is guessed or filled in for it.
    assert "结果语言" not in text_of(browser_a.get(f"{BASE}/explorations/{exploration_id}").text)


def test_explore_again_of_someone_elses_or_a_missing_exploration_finds_nothing(app, browser_a, browser_b, directions, project):
    task = task_of(review(browser_a, [project]))
    start(browser_a, task)
    exploration_id = save(browser_a, task)

    assert browser_b.get(f"{BASE}?again={exploration_id}").status_code == 404
    assert browser_a.get(f"{BASE}?again=00000000-0000-0000-0000-000000000000").status_code == 404


def test_an_english_exploration_names_the_preferences_in_the_page_language(browser_a, directions):
    """The review lists the preferences, and a result relates the paths to them, by their names in the page's language;
    the prompt keeps its own words for them (a Chinese request is word for word what it was)."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", directions_ideal="Data analysis", cities_ideal="Shanghai").status_code == 303
    item = confirmed(browser_a, title="Weekly sales dashboard", body="I cleaned the order data with Python and summarised weekly sales with SQL.")
    speak(browser_a, "en")
    task = task_of(review(browser_a, [item], result_language="en"))
    review_page = text_of(browser_a.get(task).text)
    start(browser_a, task)
    pages = {"review": review_page, "result": text_of(browser_a.get(task).text)}
    pages["record"] = text_of(browser_a.get(f"/directions/explorations/{save(browser_a, task)}").text)

    for name, page in pages.items():
        leftover = re.findall(r".{0,20}[一-鿿].{0,20}", page.replace("简体中文", "").replace("合成用户 A", ""))

        assert leftover == [], (name, leftover[:3])

    assert "Target cities: Ideal" in review_page and "Industries or fields of interest: Ideal" in review_page
    assert "Not filled in: Industries or fields of interest (Also acceptable, Not acceptable)" in review_page
