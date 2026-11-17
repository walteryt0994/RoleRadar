"""U3-D2-3, offline: the 找岗位 pages on the Lever-shaped synthetic replay (app.discovery.lever_demo).

From the pages through the agent and the tools to the result, with a synthetic demo profile of its own (data
internships in San Francisco and New York, not anyone's real preferences): a list read only in part and never shown
as all of it, candidates before analysis called 初筛候选, places and types not given or not read, a title and a type
in conflict, links shown as text, "still published when checked" with the simulated time, every page an offline
demo. Also what keeps it offline: the default is the frozen synthetic sources, a mistyped setting stops the app, the
replay holds no network client, and the searches still run the simulation only. All material is made up in code;
nothing is read from any earlier real run, and nothing leaves the process.
"""
import ast
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.discovery import lever, lever_demo, presentation
from app.discovery.agent import SimulatedDecider
from app.discovery.contracts import Access, EndReason, PartialRead
from app.discovery.sources import Permission
from app.discovery.web import DEMO_VARIABLE, LEVER_REPLAY, SYNTHETIC_SOURCES, sources_from
from app.job_analysis.simulation import SimulatedProvider
from app.library.errors import ConfigError
from app.web_main import create_app
from job_helpers import InlineExecutor, RecordingProvider, confirmed
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_pages import confirm, review, search_of, section, stored

LONG_NAME = "合成演示公司 A（Lever 格式合成回放）"
# The demo candidate: saved preferences and one confirmed project, all synthetic and entered through the pages.
DEMO_PREFERENCES = dict(
    cities_ideal="San Francisco", cities_ok="New York", job_types_ideal="实习", directions_ideal="Data Analyst",
    work_content_no="销售",
)
DEMO_PROJECT = dict(
    title="Campus transit dashboard (synthetic)",
    actions="Cleaned ridership data with Python and wrote SQL queries for weekly summaries.",
    outcome="The dashboard was used in a course showcase.",
)
NETWORK_CLIENTS = {"socket", "ssl", "http", "urllib", "requests", "httpx", "httpx2", "aiohttp", "openai", "subprocess"}


@pytest.fixture
def app(make_config, clock):
    application = create_app(make_config(), clock=clock, discovery_sources=LEVER_REPLAY)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        yield application


@pytest.fixture
def searches(app):
    searches = app.state.discovery
    searches.executor = InlineExecutor()

    return searches


@pytest.fixture
def demo(browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303

    return confirmed(browser_a, **DEMO_PROJECT)


def text_of(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def run_demo(browser, searches, demo, provider=None):
    """One search through the pages: the demo scope, its project and preferences sent, confirmed and run inline."""
    if provider is not None:
        searches.make_provider = lambda task: provider

    url = search_of(
        review(browser, sources=(demo,), use_preferences=True, targets="Data Analyst Intern", city_others="excluded",
               job_type_others="excluded", direction_others="included")
    )
    assert confirm(browser, url).status_code == 303

    return url


# --- the demo through the pages -------------------------------------------------------------------


def test_the_demo_runs_from_the_pages_to_a_result_that_says_what_it_is(app, browser_a, searches, demo):
    provider = RecordingProvider()
    url = run_demo(browser_a, searches, demo, provider)
    page = browser_a.get(url).text
    shown = text_of(page)
    outcome = stored(app, browser_a, url).finished.outcome

    # A list read in part, said at the top of the result and among what was searched: never all of it.
    (read,) = outcome.partial
    assert (read.source, read.read) == (LONG_NAME, lever_demo.LONG_COUNT)
    assert f"{LONG_NAME}的职位列表只读了前 300 个，没有读完：在它上面查的条件不算查过，这里的结果不是全量。" in shown
    assert "只读了一部分" in shown and "列表只读了前 300 个，没有读完，这个条件不算查过" in shown
    assert outcome.end is not EndReason.SCOPE_DONE and "范围内查完了" not in shown
    # Every page an offline demo; a check says still published when checked, at the simulated time.
    assert "离线演示（合成预览）" in shown and "核对时间和结果由本机模拟，不是实时信息" in shown
    assert "核对时仍公开发布 ·" in shown and "合成来源在本机回答，不是真实核验" in shown and "仍在招聘" not in shown
    # A replay says what it is: its postings synthetic samples, its requests a simulation that went nowhere.
    assert "官方原始发布的合成样例" in shown and "本机模拟服务处理的请求" in shown and "（模拟，没有联网，不产生费用）" in shown
    # Candidates before analysis are only that, and facts not known are told as they are.
    assert "初筛候选" in shown and "未分析线索" not in shown and "还没有分析，不是匹配结论" in shown
    assert "地点写了但没有认出：London, United Kingdom" in shown and "地点来源没有提供" in shown
    # A type that only says the hours is told as that (U3-D2-E3), and a full-time internship is no conflict.
    assert "类型只写了工时或合同：Full-time（没有说明是不是实习）" in shown
    assert "职位名写的是实习，来源的类型字段只写了工时或合同，两者不冲突" in shown and "来源的类型字段写的不是" not in shown
    # Links as text: nothing on the page opens Lever's hosts.
    assert "https://jobs.lever.co/rr-demo-alpha/" in shown and "（合成示例地址，不能访问）" in shown
    assert not re.search(r'href="https?://(?:jobs|api)\.lever\.co', page)


def test_the_demo_sends_only_what_the_outbound_rules_let_through(app, browser_a, searches, demo):
    provider = RecordingProvider()
    url = run_demo(browser_a, searches, demo, provider)
    outcome = stored(app, browser_a, url).finished.outcome

    # The office address went out masked; the posting with a confidential marker on its address line never went out.
    assert provider.prompts and all("500 Example Street" not in prompt for prompt in provider.prompts)
    assert any("［已遮盖地址行］" in prompt for prompt in provider.prompts)
    assert all("Restricted hiring plan" not in prompt and "200 Example Avenue" not in prompt for prompt in provider.prompts)
    blocked = dict(outcome.unlisted).get(next(reason for reason, _ in outcome.unlisted if reason.value == "blocked"))
    assert [ref.posting_id for ref in blocked] == [lever_demo.posting_id(lever_demo.LONG, 6)]
    # Said as it was (U3-D2-E3): the reason it was stopped for, never one sentence for every reason.
    # Its address line carries the confidential marker: both kinds are named, nothing more.
    assert outcome.blocked == (("address,confidential", 1),)
    shown = text_of(browser_a.get(url).text)
    line = "岗位原文里有疑似地址、标明不公开的内容，什么都没有发送：1 个"
    assert "分析被拦下或原文不能外发：1 个" in shown and line in shown and presentation.blocked_lines(outcome.blocked) == [line]
    assert "含疑似地址或标明不公开的内容" not in shown


def test_another_user_cannot_open_a_demo_search(app, browser_a, browser_b, searches, demo):
    url = run_demo(browser_a, searches, demo)

    assert browser_b.get(url).status_code == 404


def test_the_review_names_the_replay_and_runs_nothing_before_confirming(app, browser_a, searches, demo):
    url = search_of(
        review(browser_a, sources=(demo,), use_preferences=True, targets="Data Analyst Intern", city_others="excluded",
               job_type_others="excluded", direction_others="included")
    )
    shown = text_of(browser_a.get(url).text)

    assert LONG_NAME in shown and "都是本机的合成数据（离线演示），不联网，不打开任何链接" in shown
    assert stored(app, browser_a, url).finished is None


# --- what keeps it offline -------------------------------------------------------------------------


def test_the_default_is_the_frozen_synthetic_sources_and_an_unknown_setting_stops_the_app(make_config, clock):
    assert sources_from({}) is SYNTHETIC_SOURCES and sources_from({DEMO_VARIABLE: "  "}) is SYNTHETIC_SOURCES
    assert sources_from({DEMO_VARIABLE: "lever-replay"}) is LEVER_REPLAY

    for value in ("lever", "LEVER-REPLAY", "live", "https://api.lever.co"):
        config = make_config()

        with pytest.raises(ConfigError, match=DEMO_VARIABLE):
            create_app(config, clock=clock, environ={DEMO_VARIABLE: value})

        # Stopped before anything was created or opened.
        assert not config.db_path.exists()

    assert create_app(make_config(), clock=clock, environ={}).state.discovery.sources is SYNTHETIC_SOURCES


def test_the_replay_holds_no_way_to_a_network_and_admits_no_real_source():
    tree = ast.parse(Path(lever_demo.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    names = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS and "app.discovery.lever_http" not in names
    register = lever_demo.register()
    assert {entry.access for entry in register.entries} == {Access.SYNTHETIC}
    assert all(entry.source_id.startswith("lever-rr-demo-") for entry in register.entries)
    assert all(type(register.adapter(entry.source_id)._transport).__name__ == "_Replay" for entry in register.entries)
    # The product's own Lever register stays unverified and closed.
    product = lever.register((lever_demo.LONG,))
    assert product.entries[0].permission is Permission.UNVERIFIED and product.refusal(lever_demo.LONG.source_id)


def test_the_demo_still_runs_only_the_simulation(app, searches):
    assert searches.sources is LEVER_REPLAY
    assert isinstance(searches.decider_for(None), SimulatedDecider)
    assert isinstance(searches.make_provider(type("Task", (), {"cancel": None})()), SimulatedProvider)


def test_the_replay_lists_300_and_is_read_only_in_part():
    register = lever_demo.register()
    source = register.adapter(lever_demo.LONG.source_id)

    with pytest.raises(lever.ListingIncomplete) as raised:
        source.listing(20.0)

    assert (raised.value.listing.read, raised.value.listing.complete) == (300, False)
    assert register.adapter(lever_demo.SHORT.source_id).listing(20.0).complete
