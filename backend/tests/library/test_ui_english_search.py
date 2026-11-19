"""An English search writes its own words in English (Codex review F3): tool summaries, rule notes, why a posting has
no analysis, why a step was refused or not done, the step parameters, a source's failure, the names of the sources the
app registers itself and the job types its rules read. Only the user's and the sources' own words stay as written.
A Chinese search writes exactly what it wrote before (the discovery suites hold that); this file holds the English.
"""
import ast
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.discovery import agent, lever, lever_demo, rules, sources, synthetic
from app.discovery.contracts import NOT_PERSONALISED_WHY, NOT_PERSONALISED_WHY_KEY, Tool
from app.discovery.tools import HIDDEN, HIDDEN_KEY, NOTHING_STANDS, NOTHING_STANDS_KEY
from app.discovery.web import LEVER_REPLAY
from app.library.job_words import source_label, type_word
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_demo_pages import DEMO_PREFERENCES, DEMO_PROJECT
from test_discovery_pages import PREFERENCES, PROJECT, confirm, review, search_of, stored
from test_job_record_pages import inline  # noqa: F401 (fixture)
from test_job_watch_pages import boards  # noqa: F401 (fixture)

HAN = re.compile(r"[一-鿿]+")
APP = Path(__file__).resolve().parents[2] / "app"


def page_text(page: str) -> str:
    page = re.sub(r"<script.*?</script>", " ", page, flags=re.S)

    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def module_strings(*paths: Path) -> set[str]:
    """Every string constant of the given modules: the sources' own words (synthetic postings, replay companies)."""
    found = set()

    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)

    return found


# What may be Chinese on an English page: the user's own words (what this test typed in), the sources' own words, the
# user's display name and the language switch's own name for Chinese.
USER_WORDS = {*PREFERENCES.values(), *PROJECT.values(), *DEMO_PREFERENCES.values(), *DEMO_PROJECT.values(), "合成用户 A", "简体中文"}
SOURCE_WORDS = module_strings(APP / "discovery" / "synthetic.py", APP / "discovery" / "lever_demo.py")


def system_chinese(text: str) -> list[str]:
    """Chinese in text that is neither the user's nor a source's words: what the system wrote and left untranslated."""
    allowed = [*USER_WORDS, *SOURCE_WORDS]

    return sorted({run for run in HAN.findall(text) if not any(run in words for words in allowed)})


def trace_texts(finished) -> list[str]:
    texts = []

    for step in finished.trace.steps:
        texts += [f"{name} {value}" for name, value in step.params]
        texts += [step.summary or "", step.failure or "", step.reason or "", step.note or "", *(step.failed or ())]

    outcome = finished.outcome
    texts += [str(lead.why) for lead in outcome.leads]
    texts += [str(note) for lead in outcome.leads for note in lead.notes]
    texts += [str(note) for result in outcome.results for note in result.notes]
    texts += [str(refusal) for refusal in outcome.refusals]

    return texts


def english_search(app, browser, item, **extra):
    app.state.discovery.executor = InlineExecutor()
    assert browser.post("/language", {"lang": "en", "next": "/workspace"}).status_code == 303
    url = search_of(review(browser, sources=[item] if item else [], result_language="en", **extra))
    assert confirm(browser, url).status_code == 303

    return url


# --- the default (synthetic) sources ---------------------------------------------------------------------------


def test_an_english_search_writes_no_chinese_of_its_own(app, browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    item = confirmed(browser_a, **PROJECT)
    url = english_search(app, browser_a, item, use_preferences=True)
    page = page_text(browser_a.get(url).text)
    finished = stored(app, browser_a, url).finished

    assert system_chinese(page) == []
    assert all(system_chinese(text) == [] for text in trace_texts(finished)), [
        (text, system_chinese(text)) for text in trace_texts(finished) if system_chinese(text)
    ]
    # Codex's case: the analyses used up is said in English, and so are a source's name and a posting's type.
    assert "分析次数已用完" not in page and "Analysis limit used up" in page
    assert "Synthetic careers site" in page and "合成招聘官网" not in page
    assert "Internship" in page
    # The step parameters too, and the user's own scope values as they were written.
    search = next(step for step in finished.trace.steps if step.tool is Tool.SEARCH)
    assert [name for name, _ in search.params][:2] == ["Source", "Search terms"] and ("City", "北京") in search.params


def test_an_english_search_without_experiences_says_why_nothing_was_analysed_in_english(app, browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    url = english_search(app, browser_a, None, use_preferences=False)
    page = page_text(browser_a.get(url).text)
    finished = stored(app, browser_a, url).finished

    assert system_chinese(page) == []
    assert all(system_chinese(text) == [] for text in trace_texts(finished))
    assert {str(lead.why) for lead in finished.outcome.leads} >= {"No experiences were chosen, so there is no personalized analysis"}


def test_a_chinese_search_keeps_its_words_and_the_interface_language_does_not_change_them(app, browser_a):
    """The result language decides, not the interface: a Chinese search run from the English interface writes Chinese."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    item = confirmed(browser_a, **PROJECT)
    app.state.discovery.executor = InlineExecutor()
    assert browser_a.post("/language", {"lang": "en", "next": "/workspace"}).status_code == 303
    url = search_of(review(browser_a, sources=[item], use_preferences=True, result_language="zh-CN"))
    assert confirm(browser_a, url).status_code == 303
    finished = stored(app, browser_a, url).finished
    summaries = " ".join(step.summary for step in finished.trace.steps)

    assert "合成招聘官网返回" in summaries and "returned" not in summaries
    assert [name for name, _ in finished.trace.steps[0].params][:2] == ["来源", "检索词"]


# --- the Lever-shaped replay -------------------------------------------------------------------------------------


@pytest.fixture
def replay_browser(make_config, clock):
    application = create_app(make_config(), clock=clock, discovery_sources=LEVER_REPLAY)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        yield application, Browser.signed_in(application, "合成用户 A")


def test_an_english_replay_search_names_its_boards_and_types_in_english(replay_browser):
    app, browser = replay_browser
    assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
    item = confirmed(browser, **DEMO_PROJECT)
    url = english_search(
        app, browser, item, use_preferences=True, targets="Data Analyst Intern", city_others="excluded",
        job_type_others="excluded", direction_others="included",
    )
    page = page_text(browser.get(url).text)
    finished = stored(app, browser, url).finished

    assert system_chinese(page) == []
    assert all(system_chinese(text) == [] for text in trace_texts(finished))
    assert "合成演示公司 A (Lever-format synthetic replay)" in page and "Lever 格式合成回放" not in page
    assert "Posted not provided" in page or "not provided" in page


# --- the words themselves ------------------------------------------------------------------------------------------


def test_the_registered_names_and_types_are_said_in_the_texts_language_and_nothing_else_is():
    assert source_label("合成招聘官网") == "合成招聘官网" and type_word("实习") == "实习"

    with i18n.using("en"):
        assert source_label("合成招聘官网") == "Synthetic careers site"
        assert source_label("合成搜索线索") == "Synthetic search leads"
        assert source_label(lever_demo.entry(lever_demo.LONG).name) == "合成演示公司 A (Lever-format synthetic replay)"
        assert source_label(lever.LeverBoard("palantir", "Palantir").name) == "Palantir (Lever public postings API)"
        # A name the app did not register, or a company alone, is shown as it is.
        assert source_label("Some board") == "Some board" and source_label("（Lever 格式合成回放）") == "（Lever 格式合成回放）"
        assert (type_word("实习"), type_word("校招"), type_word("Full-time"), type_word(None)) == ("Internship", "Campus recruiting", "Full-time", "")
        assert rules.stated(None) == "not provided"

    assert rules.stated(None) == "未提供"


def test_the_constants_old_tests_compare_are_still_the_chinese_words_of_their_keys():
    assert (NOTHING_STANDS, HIDDEN, NOT_PERSONALISED_WHY, agent.WITHHELD) == tuple(
        i18n.t_in("zh-CN", key) for key in (NOTHING_STANDS_KEY, HIDDEN_KEY, NOT_PERSONALISED_WHY_KEY, agent.WITHHELD_KEY)
    )
    assert NOTHING_STANDS.startswith("还没有现在仍然成立的完整分析结果") and HIDDEN == "这条线索指向用户跳过的岗位，不再处理"


def test_a_sources_failure_is_said_in_the_texts_language():
    assert sources.failure_text(sources.HeldBack("x")) == "发送前停下，没有发出请求"

    with i18n.using("en"):
        assert sources.failure_text(sources.HeldBack("x")) == "stopped before sending; no request was sent"
        assert sources.failure_text(sources.SourceTimeout("x")) == "the source did not answer within the time limit"
        assert str(lever.NotFound(i18n.t("disc.lever.19"))).startswith("this posting: the source answered 404")


def test_every_fixed_note_of_the_simulated_decider_has_its_english():
    """A new fixed note written in Chinese only would show Chinese in an English search (the note Codex's review found)."""
    tree = ast.parse((APP / "discovery" / "agent.py").read_text(encoding="utf-8"))
    decider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SimulatedDecider")
    formatted = [node for node in ast.walk(decider) if isinstance(node, ast.JoinedStr)]
    pieces = {id(part) for node in formatted for part in node.values}
    notes = {
        node.value
        for node in ast.walk(decider)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and HAN.search(node.value)
        and id(node) not in pieces and node.value != "资深"  # a rules mark compared, never written
    }
    # A note with a value in it is told by its pattern: filled with any value, it reads as the retry note.
    filled = ["".join(part.value if isinstance(part, ast.Constant) else "X" for part in node.values) for node in formatted]

    assert notes and notes <= set(agent.SIMULATED_NOTES_EN), notes - set(agent.SIMULATED_NOTES_EN)
    assert all(agent.SIMULATED_RETRY.match(text) for text in filled if HAN.search(text)), filled


# --- the same search in either language ------------------------------------------------------------------------------
# The words a search writes change with its result language; what it does never does. Each scope is run as a Chinese
# and as an English search, and the two must take the same steps to the same postings and the same end. (A translated
# combination once kept an English search from finding any search to make: rules.unsearched is the agents' key.)


def shape(finished) -> tuple:
    outcome = finished.outcome

    return (
        [(step.tool, step.outcome) for step in finished.trace.steps],
        [result.ref for result in outcome.results],
        [lead.ref for lead in outcome.leads],
        outcome.end,
        len(outcome.searches),
        len(outcome.unsearched),
    )


def run_in(app, browser, item, language, **extra) -> tuple:
    app.state.discovery.executor = InlineExecutor()
    url = search_of(review(browser, sources=[item] if item else [], result_language=language, **extra))
    assert confirm(browser, url).status_code == 303

    return shape(stored(app, browser, url).finished)


NO_DIRECTION = {**PREFERENCES, "directions_ideal": ""}


@pytest.mark.parametrize(
    ("preferences", "extra"),
    [
        (PREFERENCES, {"use_preferences": True}),
        # Only open dimensions to search (the scope the browser check found): every combination has an unlisted part.
        (NO_DIRECTION, {"use_preferences": True, "targets": "数据分析实习生", "city_others": "excluded", "job_type_others": "excluded", "direction_others": "included"}),
        (PREFERENCES, {"use_preferences": True, "city_others": "included", "job_type_others": "included"}),
    ],
    ids=["chosen", "only-open-direction", "open-city-and-type"],
)
def test_a_search_takes_the_same_steps_in_either_language(app, browser_a, preferences, extra):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **preferences).status_code == 303
    item = confirmed(browser_a, **PROJECT)

    chinese = run_in(app, browser_a, item, "zh-CN", **extra)
    english = run_in(app, browser_a, item, "en", **extra)

    assert english == chinese
    assert chinese[0], "the search took no step"


def test_a_search_without_experiences_takes_the_same_steps_in_either_language(app, browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303

    assert run_in(app, browser_a, None, "en", use_preferences=False) == run_in(app, browser_a, None, "zh-CN", use_preferences=False)


def test_a_replay_search_takes_the_same_steps_in_either_language(replay_browser):
    app, browser = replay_browser
    assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
    item = confirmed(browser, **DEMO_PROJECT)
    extra = dict(use_preferences=True, targets="Data Analyst Intern", city_others="excluded", job_type_others="excluded", direction_others="included")

    assert run_in(app, browser, item, "en", **extra) == run_in(app, browser, item, "zh-CN", **extra)


def test_a_combination_is_kept_in_one_form_and_said_in_the_texts_language():
    shown = "北京 · 实习 · 任意岗位方向"

    with i18n.using("en"):
        assert rules.combination_words(shown) == "北京 · 实习 · Direction: any"
        assert rules.combination_words("上海 · 所列以外的求职类型") == "上海 · Job type: beyond those listed"
        # The form the agent matches by is the same in every language.
        assert rules.unsearched(rules_scope(), set()) == UNSEARCHED_ZH

    assert rules.combination_words(shown) == shown
    assert rules.unsearched(rules_scope(), set()) == UNSEARCHED_ZH


def rules_scope():
    from app.discovery.contracts import DimensionScope, Others, SearchScope

    return SearchScope(
        city=DimensionScope(("北京",), Others.EXCLUDED),
        job_type=DimensionScope(("实习",), Others.EXCLUDED),
        direction=DimensionScope((), Others.INCLUDED),
        targets=("数据分析实习生",),
    )


UNSEARCHED_ZH = ("北京 · 实习 · 任意岗位方向",)


# --- a daily watch writes in the language of the search it was kept from ----------------------------------------------


def test_a_watch_screens_in_the_result_language_of_its_summary(app, browser_a, inline, boards, clock, monkeypatch):
    from sqlalchemy.orm.attributes import flag_modified

    from app.discovery import watches
    from app.library.models import JobSearch
    from test_job_watch_pages import run_now, start, summary_of, the_watch

    english = summary_of(app, clock)
    chinese = summary_of(app, clock)

    with app.state.library.database.transaction() as db:
        record = db.get(JobSearch, english)
        record.summary = {**record.summary, "result_language": "en"}
        flag_modified(record, "summary")

    with app.state.library.database.transaction() as db:
        assert watches.conditions_of(db.get(JobSearch, english).summary).language == "en"
        # A summary older than languages: its search wrote Chinese, and so does its watch.
        assert watches.conditions_of(db.get(JobSearch, chinese).summary).language == "zh-CN"

    seen = []
    screen = rules.screen
    monkeypatch.setattr(rules, "screen", lambda *args, **kwargs: (seen.append(i18n.language()), screen(*args, **kwargs))[1])

    careers, _ = boards

    for number, summary_id in enumerate((english, chinese)):
        start(browser_a, app, summary_id)
        # A posting new since the baseline: the run screens it.
        careers.add(f"c-language-{number}", title="数据分析实习生", company=f"合成公司语言{number}")
        clock.advance(minutes=11)
        run_now(browser_a, summary_id, the_watch(app, summary_id).id)

    assert "en" in seen and "zh-CN" in seen
