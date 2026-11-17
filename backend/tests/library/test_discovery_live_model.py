"""U3-D2-E3, offline: validation B's wiring (app.discovery.model_live). The real source is read as in D2-E1; each
decision is a model's and each analysis a real service's, both through the app's one gate, under two approvals bound
to one search.

Everything of a real service but the provider: a made-up approval under the real budget policy, with made-up prices,
opens the gate on the test's own library, and a made-up provider with the real recipient's name (Scripted) answers
decisions from a script and analyses by the local simulation's rules. The real source is the real transport over
socket pairs inside this process (test_discovery_live_source.RoutedLever). Building a real service's client fails the
test, and the launcher blocks the network. The approvals here are a test's own, under a home of the test's own, never
the Owner's. None of this validates a real model.

Covered: the simulation stays the default, and stays for validation A, whatever service the app has; validation B
does not start without everything it needs, and never falls back to the simulation; the review names the three
recipients, the notice and both approvals, and opening pages builds no client and sends nothing; decisions and
analyses go through one gate task and one ledger, decisions with their own limits; one pair of approvals for one
search, through a second confirmation, a repeated one, concurrent searches, a search after the first ended, a restart
and another run directory; nothing more is sent after a cancellation, LIVE_STOP, the search's time running out or a
paused approval; the analyses' notes are metadata only.
"""
import json
import threading
from contextlib import ExitStack
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_provider import AIProvider, ProviderIdentity, responses_request
from app.analysis_contracts import Recipient
from app.discovery import lever_live, model_live, presentation
from app.discovery.agent import ACTION_SCHEMA_NAME, ModelDecider, SimulatedDecider, make_action
from app.discovery.contracts import DimensionScope, EndReason, Others, PostingRef, SearchScope, Tool
from app.discovery.lever_http import LeverHttpTransport
from app.discovery.runner import live_provider_for, run_search
from app.job_analysis.service import local_provider
from app.discovery.tasks import DiscoveryTaskStore, Stage, Start
from app.discovery.web import (
    DEMO_VARIABLE,
    DiscoveryRuntime,
    SYNTHETIC_SOURCES,
    live_model_source_set,
    live_source_set,
)
from app.job_analysis.service import LiveService
from app.job_analysis.simulation import SimulatedProvider
from app.library.errors import ConfigError
from app.library.models import LiveRequest, LiveRequestState
from app.live_gate import FIXED_RESERVE_V1, LiveSetup, open_gate
from app.web_main import create_app
from discovery_helpers import SALES, confirmation_for, make_library
from job_helpers import InlineExecutor, confirmed
from lever_helpers import Ticks, pid
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_compare import B1_TERMS
from test_discovery_demo_pages import DEMO_PREFERENCES, DEMO_PROJECT, text_of
from test_discovery_live_source import APPROVAL as SOURCE_APPROVAL
from test_discovery_live_source import LIST, RoutedLever, claim_record, environ, ledger_of, source_terms
from test_discovery_lever_http import response
from test_discovery_pages import confirm, review, search_of, stored, task_id
from test_live_gate import USED, answer, budget_terms

TODAY = date(2026, 11, 12)
MODEL_APPROVAL = "u3d2e-ai-2026-11-12-a"
OPENAI = ("openai", "gpt-5.6-sol", "https://api.openai.com/v1")
SOURCE = lever_live.BOARD.source_id
# Usage the ledger cannot price (another tier than the approval's): it pauses the approval.
UNPRICED = replace(USED, service_tier="priority")
# The demo's scope on Palantir's synthetic board: San Francisco and internships only, any direction.
SCOPE = SearchScope(
    city=DimensionScope(("San Francisco",), Others.EXCLUDED, ("San Francisco",)),
    job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope(("数据分析",), Others.INCLUDED, ("数据分析",)),
    targets=("Data Analyst Intern",),
)


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    """Building a real service's client fails the test: nothing here may reach one."""

    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


class Scripted(AIProvider):
    """Made up, under the real recipient's name: each decision from the script in order (an action as JSON data, raw
    text, or a callable run first, which may stop the search, then answered by what it returns), each analysis by
    the local simulation's rules; usage as given, or for each request in turn from usages. calls keeps (kind, output
    limit) of every request that reached it, prompts what each carried."""

    SYNTHETIC = True

    def __init__(self, *decisions, usages=()):
        self.decisions = list(decisions)
        self.usages = list(usages)
        self.calls: list[tuple[str, int | None]] = []
        self.prompts: list[str] = []

    def identity(self):
        return ProviderIdentity(*OPENAI)

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        return responses_request(
            OPENAI[1], prompt, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort,
            service_tier="default", json_schema_format=json_schema_format,
        )

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        name = json_schema_format.name if json_schema_format is not None else ""
        self.calls.append((name, max_output_tokens))
        self.prompts.append(prompt)

        if name == ACTION_SCHEMA_NAME:
            item = self.decisions.pop(0) if self.decisions else finish()

            while callable(item):
                item = item()

            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        else:
            text = SimulatedProvider().generate_text(prompt, json_schema_format=json_schema_format).text

        usage = self.usages.pop(0) if self.usages else USED

        return replace(answer(usage), text=text, provider=OPENAI[0], requested_model=OPENAI[1], model=OPENAI[1])

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]


def ref(number: int = 1) -> PostingRef:
    return PostingRef(SOURCE, pid(number, lever_live.BOARD))


def search():
    return make_action(Tool.SEARCH, source_id=SOURCE, keywords="Data Analyst", city="San Francisco", job_type="实习", note="检索")


def read():
    return make_action(Tool.READ, postings=[ref()], note="读取")


def check():
    return make_action(Tool.CHECK, postings=[ref()], note="核对")


def analyze():
    return make_action(Tool.ANALYZE, postings=[ref()], note="分析")


def finish():
    return make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束")


def whole_search():
    return (search(), read(), check(), analyze(), finish())


def approval_terms(**changes):
    """A made-up model approval for validation B under the real budget policy, with made-up prices."""
    provider, model, endpoint = OPENAI
    values = {
        "approval_id": MODEL_APPROVAL,
        "purposes": frozenset({"discovery"}),
        "provider": provider,
        "model": model,
        "endpoint": endpoint,
        "max_requests": 14,
        "max_cost_micro_usd": 3_400_000,
        "timeout_seconds": 120.0,
    }
    values.update(changes)

    return budget_terms(**values)


def model_ledger(run_dir) -> Path:
    return ledger_of(run_dir).parent / "model-approvals"


def model_claim(run_dir, approval: str = MODEL_APPROVAL) -> dict:
    return json.loads((model_ledger(run_dir) / f"{approval}.claim").read_text(encoding="utf-8"))


def rows(database) -> list[LiveRequest]:
    with database.transaction() as db:
        return list(db.scalars(select(LiveRequest).order_by(LiveRequest.reserved_at)))


@pytest.fixture
def run_dir(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()

    return folder


@pytest.fixture
def routed():
    return RoutedLever()


@pytest.fixture
def ticks():
    return Ticks()


def inject(routed, ticks) -> dict:
    return dict(create=lambda: LeverHttpTransport(opener=routed.opener), today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)


def b_sources(run_dir, routed, ticks, approval):
    terms = source_terms(run_dir)

    return live_model_source_set(terms, model_live.load_terms(approval, terms, today=TODAY), approval, **inject(routed, ticks))


@pytest.fixture
def b_app(make_config, clock, run_dir, routed, ticks):
    """build(provider, ...) -> validation B's app on the test's library, with user A signed in and the demo material
    entered through the pages."""
    stack = ExitStack()

    def build(provider, **changes):
        approval = approval_terms(**changes)
        application = create_app(
            in_run_dir(make_config, run_dir), clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, lambda: provider),
            discovery_sources=b_sources(run_dir, routed, ticks, approval),
        )
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))
        application.state.discovery.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303

        return application, browser, confirmed(browser, **DEMO_PROJECT)

    yield build
    stack.close()


def in_run_dir(make_config, run_dir):
    """Validation B's library sits in its run directory: one LIVE_STOP there stops the gate and the source alike."""
    return make_config(db_path=run_dir / "library.db", files_dir=run_dir / "files")


def written(run_dir) -> set[str]:
    """What a search wrote in the run directory, besides the library."""
    return {path.name for path in run_dir.iterdir()} & {lever_live.REQUEST_LOG, lever_live.ANALYSIS_LOG, lever_live.STOP_FILE}


def b_review(browser, demo) -> str:
    return search_of(
        review(browser, sources=(demo,), use_preferences=True, targets="Data Analyst Intern", city_others="excluded",
               job_type_others="excluded", direction_others="included")
    )


# --- the simulation unless validation B is chosen with all it needs -------------------------------------------


@pytest.mark.parametrize("which", ["default", "validation_a"])
def test_the_default_and_validation_a_take_no_model_whatever_service_the_app_has(make_config, clock, run_dir, routed, ticks, which):
    provider = Scripted()
    sources = SYNTHETIC_SOURCES if which == "default" else live_source_set(source_terms(run_dir), **inject(routed, ticks))
    application = create_app(
        make_config(), clock=clock, live=LiveSetup(approval_terms(), FIXED_RESERVE_V1, lambda: provider), discovery_sources=sources
    )

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        searches = application.state.discovery
        # The app has a real service: the search pages do not take it; the job analysis pages keep theirs as before.
        assert (searches.model, searches.live) == (None, None)
        assert application.state.job_analysis.live is application.state.live_service
        # Even handed the service, a set other than validation B's does not keep it (DiscoveryRuntime).
        handed = DiscoveryRuntime(store=DiscoveryTaskStore(), executor=None, sources=sources, live=application.state.live_service)
        assert handed.live is None and handed.recipient.provider == "simulation"
        assert isinstance(searches.decider_for(provider), SimulatedDecider)
        assert searches.recipient.provider == "simulation" and searches.refusal() is None

        if which == "validation_a":
            searches.executor = InlineExecutor()
            browser = Browser.signed_in(application, "合成用户 A")
            assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
            url = b_review(browser, confirmed(browser, **DEMO_PROJECT))
            review_page = text_of(browser.get(url).text)
            # A's review says what it reads and that the model is not used; never that nothing goes online (U3-D2-E3).
            assert "不联网，不打开" not in review_page and "除了上面写明的来源读取，不访问别的网站" in review_page
            assert "本机模拟决策" in review_page and "处理说明" not in review_page
            assert confirm(browser, url).status_code == 303
            shown = text_of(browser.get(url).text)

            # Read for real, decided and analysed by the simulation: no model call, no ledger row.
            assert routed.requests and provider.calls == [] and rows(application.state.library.database) == []
            assert "本机模拟服务处理的请求" in shown and "已交给模型服务的请求" not in shown
            # What A's review says is not done no longer says it stays offline (U3-D2-E3).
            # Version 6 since U3-D3c (5 since U3-D3b): what a set that lets keep says; A's own words did not change.
            assert stored(application, browser, url).confirmation.notice_version == "discovery-preview-7"


def b_environ(run_dir, day: date, **changes) -> dict[str, str]:
    """What the app is started with for validation B: the real source's terms (test_discovery_live_source.environ),
    a made-up approval for discovery alone, its key made up too, and the choice."""
    values = {
        **environ(run_dir, day),
        **B1_TERMS,
        "ROLERADAR_LIVE_APPROVAL_ID": f"u3d2e-ai-{day}-a",
        "ROLERADAR_LIVE_MAX_REQUESTS": "14",
        "ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "4000",
        "ROLERADAR_LIVE_TIMEOUT_SECONDS": "120",
        "ROLERADAR_LIVE_MAX_COST_USD": "3.40",
        model_live.VARIABLE: model_live.LIVE,
    }
    values.update({name: value for name, value in changes.items() if value is not None})

    return {name: value for name, value in values.items() if changes.get(name, "") is not None}


def test_validation_b_chosen_with_all_it_needs_takes_the_service_for_the_searches_alone(make_config, clock, run_dir):
    today = lever_live.today_here()
    application = create_app(in_run_dir(make_config, run_dir), clock=clock, environ=b_environ(run_dir, today))

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        searches = application.state.discovery
        # The real service is the searches': built nowhere yet, and not the job analysis pages'.
        assert searches.model.approval_id == f"u3d2e-ai-{today}-a" and searches.live is application.state.live_service
        assert application.state.job_analysis.live is None
        assert isinstance(searches.decider_for(object()), ModelDecider)
        assert searches.notice_version == model_live.NOTICE_VERSION and searches.recipient == Recipient(*OPENAI)

    # Started and stopped: nothing claimed, nothing written in the run directory but the library.
    assert not ledger_of(run_dir).exists() and not model_ledger(run_dir).exists() and written(run_dir) == set()


def test_validation_b_with_its_library_outside_the_run_directory_does_not_start(make_config, clock, run_dir):
    config = make_config()

    with pytest.raises(ConfigError, match="要放在运行目录里"):
        create_app(config, clock=clock, environ=b_environ(run_dir, lever_live.today_here()))

    assert not config.db_path.exists() and not model_ledger(run_dir).exists()


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({DEMO_VARIABLE: None}, "只能和真实来源一起用"),
        ({DEMO_VARIABLE: "lever-replay"}, "只能和真实来源一起用"),
        ({"ROLERADAR_ANALYSIS_MODE": None}, "没有可用的模型批准"),
        ({"ROLERADAR_LIVE_PURPOSES": "discovery,analysis"}, "用途只能写 discovery"),
        ({"ROLERADAR_LIVE_PURPOSES": "analysis"}, "用途只能写 discovery"),
        ({"ROLERADAR_LIVE_APPROVAL_ID": "u3b1-synthetic-test"}, "应写成 u3d2e-ai-"),
        ({"ROLERADAR_LIVE_APPROVAL_ID": "u3d2e-ai-2020-01-01-a"}, "和读取批准的运行日不一致"),
        ({"ROLERADAR_SOURCE_APPROVAL_ID": None}, "缺少 ROLERADAR_SOURCE_APPROVAL_ID"),
        ({model_live.VARIABLE: "yes"}, "只能不设，或者设为 live"),
    ],
)
def test_validation_b_without_all_it_needs_does_not_start_and_never_simulates(make_config, clock, run_dir, changes, problem):
    config = in_run_dir(make_config, run_dir)

    with pytest.raises(ConfigError, match=problem):
        create_app(config, clock=clock, environ=b_environ(run_dir, lever_live.today_here(), **changes))

    # Refused before anything is created; nothing claimed.
    assert not config.db_path.exists() and not model_ledger(run_dir).exists() and not any(run_dir.iterdir())


def test_validation_b_holds_only_on_its_run_date():
    terms = SimpleTerms.source(TODAY)
    late = model_live.load_terms

    with pytest.raises(model_live.ModelTermsError, match="只在 2026-11-12（美东）有效，今天是 2026-11-13"):
        late(approval_terms(), terms, today=TODAY + timedelta(days=1))


class SimpleTerms:
    """Read terms of the real source as load_terms would give them, without reading the environment."""

    @staticmethod
    def source(day: date, root: Path | None = None) -> lever_live.SourceTerms:
        root = root or Path("/nonexistent-run")

        return lever_live.SourceTerms(
            f"u3d2e-admit-{day}-a", day + timedelta(days=29), f"u3d2e-src-{day}-a", day, 12, 600.0, root,
            root.parent / "home" / ".roleradar" / "source-approvals",
        )


@pytest.mark.parametrize("folder", ["same_run_directory", "another_run_directory"])
def test_a_restart_or_another_run_directory_does_not_make_used_approvals_unused(make_config, clock, run_dir, folder):
    today = lever_live.today_here()
    terms = lever_live.load_terms(environ(run_dir, today), today=today)
    model = model_live.load_terms(approval_terms(approval_id=f"u3d2e-ai-{today}-a"), terms, today=today)
    model_live.claim(model, "an-earlier-search")
    other = run_dir if folder == "same_run_directory" else (run_dir.parent / "another-run")
    other.mkdir(exist_ok=True)

    # Looked for by id under HOME: a new run directory or a restart does not make it unused.
    with pytest.raises(ConfigError, match=f"模型批准 u3d2e-ai-{today}-a 已经用过"):
        create_app(in_run_dir(make_config, other), clock=clock, environ=b_environ(other, today))

    assert model_claim(run_dir, f"u3d2e-ai-{today}-a")["task_id"] == "an-earlier-search"
    assert [path.name for path in model_ledger(run_dir).iterdir()] == [f"u3d2e-ai-{today}-a.claim"]


# --- one search through the pages ------------------------------------------------------------------------------


def test_validation_b_runs_through_one_gate_names_its_three_recipients_and_keeps_only_metadata(b_app, run_dir, routed):
    provider = Scripted(*whole_search())
    application, browser, demo = b_app(provider)
    database = application.state.library.database
    assert browser.get("/discover").status_code == 200
    url = b_review(browser, demo)
    page = text_of(browser.get(url).text)

    # Opening the pages builds no client and sends nothing: no source request, no model call, no ledger row, no claim.
    assert (routed.requests, provider.calls, rows(database)) == ([], [], [])
    assert not ledger_of(run_dir).exists() and not model_ledger(run_dir).exists() and written(run_dir) == set()
    # The review names three recipients: the source read, the model deciding, the model analysing; the notice, both
    # approvals, and that another notice or approval is another confirmation.
    assert "岗位来源" in page and f"读取批准 {SOURCE_APPROVAL}" in page
    assert f"OpenAI 的 gpt-5.6-sol（模型批准 {MODEL_APPROVAL}" in page and "由它选下一个动作" in page
    assert "（同一份模型批准）：每分析一个岗位" in page and "英文原文" in page
    assert "处理说明" in page and all(paragraph in page for paragraph in model_live.NOTICE)
    assert "处理说明的版本或任一批准变了，原来的核对作废" in page and "还剩 14 次" in page
    assert "不联网" not in page and "除了上面写明的来源读取和模型请求，不访问别的网站" in page
    assert stored(application, browser, url).confirmation.notice_version == model_live.NOTICE_VERSION

    assert confirm(browser, url).status_code == 303
    task = stored(application, browser, url)
    outcome = task.finished.outcome
    shown = text_of(browser.get(url).text)

    # A model's decisions drove the tools, and one analysis went through the same gate task: seven requests, one
    # ledger, one purpose, every row settled. Decisions ask for their own limit, analyses for the approval's.
    assert provider.kinds() == ["discovery_action"] * 4 + ["structured_job_description", "experience_evidence_analysis", "discovery_action"]
    assert {limit for kind, limit in provider.calls if kind == ACTION_SCHEMA_NAME} == {model_live.DECISION_OUTPUT_TOKENS}
    assert [(row.purpose, row.state) for row in rows(database)] == [("discovery", LiveRequestState.SETTLED)] * 7
    # Each reserved at its own output limit (made-up prices: 16,000 input at 10, output at 20, in millionths): a
    # decision at 2,000, an analysis at the approval's 4,000. So 8 decisions and 6 analysis requests reserve at most
    # 8 × 0.20 + 6 × 0.24 = $3.04 at the real prices the proposal names.
    assert [row.reserved_micro_usd for row in rows(database)] == [200_000] * 4 + [240_000] * 2 + [200_000]
    assert (task.stage, outcome.end, [result.ref for result in outcome.results]) == (Stage.DONE, EndReason.ENOUGH, [ref()])
    assert task.sends.generation_sent == 7
    # Both approvals claimed by this very search, the model's naming the read approval.
    assert model_claim(run_dir)["task_id"] == claim_record(run_dir)["task_id"] == task_id(url)
    assert model_claim(run_dir)["source_approval_id"] == SOURCE_APPROVAL
    # Robots first, then the list, a read and a check: within the read approval.
    assert routed.requests[0] == "/robots.txt" and len(routed.requests) == 6
    # The pages say what was real: requests handed to the model service, counted as the job analysis pages count them.
    assert "已交给模型服务的请求：计数 0 次、生成 7 次，合计 7 次" in shown and "本机模拟" not in shown
    # Nothing the user wrote but the chosen fields went to the decisions: no private note, no experience text.
    decisions = [prompt for (kind, _), prompt in zip(provider.calls, provider.prompts) if kind == ACTION_SCHEMA_NAME]
    assert not any(DEMO_PROJECT["actions"] in prompt for prompt in decisions)
    # The analyses' notes: metadata only, never a posting's id or words.
    notes = [json.loads(line) for line in (run_dir / lever_live.ANALYSIS_LOG).read_text(encoding="utf-8").splitlines()]
    assert [note["outcome"] for note in notes] == ["analysed"] and notes[0]["status"] == "complete"
    assert set(notes[0]) <= {
        "seq", "outcome", "posting_chars", "outgoing_chars", "material_chars", "masked", "reasons", "status", "problem",
        "requests_sent", "task_id", "at",
    }
    raw = (run_dir / lever_live.ANALYSIS_LOG).read_text(encoding="utf-8")
    assert ref().posting_id not in raw and "Data Analyst" not in raw and "Northbay" not in raw


def test_validation_b_refuses_a_city_or_type_in_the_boards_keywords_before_any_request_and_the_model_makes_it_again(
    b_app, run_dir, routed
):
    """U3-D2-E4 F1: validation B's first action, on the real wiring (the controlled board, one gate): refused before
    robots or the list is asked for, the model told which words and what is left, and the search made again goes on
    as a whole search would. Every decision is told what the board matches keywords against."""
    misuse = make_action(
        Tool.SEARCH, source_id=SOURCE, keywords="Data Analyst Intern San Francisco", city="San Francisco", job_type="实习",
        note="检索",
    )
    asked: list[list[str]] = []

    def again():
        # What the board had been asked when the second decision was: nothing.
        asked.append(list(routed.requests))

        return search()

    provider = Scripted(misuse, again, read(), check(), analyze(), finish())
    application, browser, demo = b_app(provider)
    url = b_review(browser, demo)

    assert confirm(browser, url).status_code == 303
    task = stored(application, browser, url)
    outcome, trace = task.finished.outcome, task.finished.trace
    decisions = [prompt for (kind, _), prompt in zip(provider.calls, provider.prompts) if kind == ACTION_SCHEMA_NAME]

    assert asked == [[]]
    assert [(step.tool, step.outcome) for step in trace.steps[:2]] == [(Tool.SEARCH, "refused"), (Tool.SEARCH, "done")]
    assert (task.stage, outcome.end, [result.ref for result in outcome.results]) == (Stage.DONE, EndReason.ENOUGH, [ref()])
    # One more decision than a whole search, the same requests of the board: robots, the list, a read and a check.
    assert provider.kinds() == ["discovery_action"] * 5 + ["structured_job_description", "experience_evidence_analysis", "discovery_action"]
    assert routed.requests[0] == "/robots.txt" and len(routed.requests) == 6
    assert [(row.purpose, row.state) for row in rows(application.state.library.database)] == [("discovery", LiveRequestState.SETTLED)] * 8
    # Every decision was told what the board matches; the second why the first was refused and what is left.
    assert all("检索词只在职位名、团队、部门和类型里整词匹配" in prompt for prompt in decisions)
    assert all("岗位、技能、城市这类条件词" not in prompt for prompt in decisions)
    assert "城市“San Francisco”" in decisions[1] and "求职类型“Intern”" in decisions[1]
    assert "去掉以后检索词是“Data Analyst”，其余条件不变" in decisions[1]


def test_a_repeated_confirmation_starts_nothing_more(b_app, run_dir):
    provider = Scripted(*whole_search())
    application, browser, demo = b_app(provider)
    url = b_review(browser, demo)
    page = browser.get(url).text
    assert confirm(browser, url).status_code == 303
    calls = list(provider.calls)

    # The same form posted again: already started, nothing more.
    again = browser.post(f"{url}/confirm", {"csrf_token": browser.csrf_token(), "fingerprint": stored(application, browser, url).fingerprint, "action": "start"})
    assert again.status_code == 303 and "notice=already" in again.headers["location"]
    assert provider.calls == calls and page


@pytest.mark.parametrize("when", ["second_confirmation", "after_the_first_ended"])
def test_one_pair_of_approvals_for_one_search(b_app, run_dir, routed, when):
    provider = Scripted(*whole_search())
    application, browser, demo = b_app(provider)
    database = application.state.library.database
    first = b_review(browser, demo)
    second = b_review(browser, demo) if when == "second_confirmation" else None
    assert confirm(browser, first).status_code == 303
    second = second or b_review(browser, demo)
    calls, requests, ledger = list(provider.calls), list(routed.requests), len(rows(database))

    # The review says the approvals are used and offers no start; a start posted anyway sends nothing.
    page = text_of(browser.get(second).text)
    assert f"模型批准 {MODEL_APPROVAL} 已经用过，一次查找只能用一次" in page and "确认并开始查找" not in page
    response = confirm(browser, second)
    assert response.status_code == 409 and "什么都没有发送" in text_of(response.text)
    assert (provider.calls, routed.requests, len(rows(database))) == (calls, requests, ledger)
    assert stored(application, browser, second).stage is Stage.REVIEW
    # The records stay the first search's.
    assert model_claim(run_dir)["task_id"] == claim_record(run_dir)["task_id"] == task_id(first)


# --- below the pages: one gate, the binding, the stops --------------------------------------------------------


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


@pytest.fixture
def gate(database, clock, run_dir):
    # Its lock and stop file in the run directory, as validation B's library is there (web_main).
    opened = open_gate(database, run_dir / "library.db", approval_terms(), FIXED_RESERVE_V1, clock)

    yield opened

    opened.close()


@pytest.fixture
def model(run_dir):
    """Validation B's model terms, read once as the app reads them at start."""
    return model_live.load_terms(approval_terms(), source_terms(run_dir), today=TODAY)


def b_task(store, library, model, session="session-1"):
    confirmation = replace(
        confirmation_for(
            library, scope=SCOPE, sources=(lever_live.entry(model.source).grant,), recipient=Recipient(*OPENAI),
            preferences=(SALES,), limits=lever_live.LIMITS,
        ),
        notice_version=model_live.NOTICE_VERSION,
    )
    task = store.create(library.owner, session, confirmation)
    state, task = store.begin(task.id, library.owner, session, task.fingerprint)
    assert state is Start.STARTED

    return task


def run_b(store, database, clock, task, gate, provider, model, routed, ticks):
    """One search of validation B as the pages run it, below them: its register, its binding, the gate's provider."""
    binding = model_live.Binding(model, task.id, task.cancel, today=lambda: TODAY)
    register = live_source_set(model.source, **inject(routed, ticks)).make_register(task)
    run_search(
        store, database, task.id, live_provider_for(LiveService(gate, lambda: provider)), register=register, clock=clock,
        decider_for=lambda guarded: ModelDecider(guarded, output_tokens=2_000, max_request_bytes=15_000), environ={},
        binding=binding, diagnose=lever_live.analysis_record(model.source, task.id),
    )

    with store._lock:
        return replace(store._tasks[task.id])


def test_concurrent_searches_one_holds_the_gate_and_the_approvals_the_other_sends_nothing(database, library, clock, gate, model, run_dir, routed, ticks):
    store = DiscoveryTaskStore(ticks.monotonic)
    first, second = b_task(store, library, model, "session-1"), b_task(store, library, model, "session-2")
    inside, release = threading.Event(), threading.Event()

    def hold():
        inside.set()
        assert release.wait(10)

        # Nothing found, said so: the search ends on its first decision.
        return make_action(Tool.FINISH, claim=EndReason.NO_RESULTS, note="结束")

    provider = Scripted(hold)
    runner = threading.Thread(target=run_b, args=(store, database, clock, first, gate, provider, model, routed, ticks))
    runner.start()
    assert inside.wait(10)
    other = Scripted(*whole_search())

    try:
        # The first holds the gate: the second never enters it, claims nothing and sends nothing.
        task = run_b(store, database, clock, second, gate, other, model, routed, ticks)
    finally:
        release.set()
        runner.join(10)

    assert (task.problem, other.calls) == ("gate_busy", [])
    assert model_claim(run_dir)["task_id"] == first.id and claim_record(run_dir)["task_id"] == first.id
    # After the first ended, a third search finds both approvals used: nothing sent either.
    later = b_task(store, library, model, "session-3")
    task = run_b(store, database, clock, later, gate, other, model, routed, ticks)
    assert (task.problem, other.calls) == ("approvals_model_claimed", [])
    assert [row.state for row in rows(database)] == [LiveRequestState.SETTLED]
    assert model_claim(run_dir)["task_id"] == claim_record(run_dir)["task_id"] == first.id


@pytest.mark.parametrize("how", ["cancel", "live_stop", "timeout", "pause"])
def test_after_a_stop_no_decision_analysis_or_read_is_sent(database, library, clock, gate, model, run_dir, routed, ticks, how):
    store = DiscoveryTaskStore(ticks.monotonic)
    task = b_task(store, library, model)

    def stop_then(action):
        def stop():
            if how == "cancel":
                store.cancel(task.id, task.owner_id, task.session)
            elif how == "live_stop":
                # The run directory's, which is the gate's too (the library is there).
                (run_dir / lever_live.STOP_FILE).touch()
            elif how == "timeout":
                ticks.now += 601

            return action

        return stop

    # The second decision is where it stops: it went out, its answer is not acted on (for LIVE_STOP its read is kept
    # back before it leaves). A pause comes from the first decision's cost, which the ledger cannot price.
    script = (search(), stop_then(read()), check(), analyze(), finish())
    provider = Scripted(*script, usages=(UNPRICED,) if how == "pause" else ())

    task = run_b(store, database, clock, task, gate, provider, model, routed, ticks)
    sent = 1 if how == "pause" else 2

    # Nothing more reached the model, and nothing is left reserved: what the gate reserved and kept back is released.
    assert provider.kinds() == ["discovery_action"] * sent, provider.kinds()
    states = [row.state for row in rows(database)]
    assert len([state for state in states if state is not LiveRequestState.RELEASED]) == sent, states
    assert LiveRequestState.RESERVED not in states
    # The source: the first search only (robots and the list's three pages), no read after the stop.
    assert len(routed.requests) == 4, routed.requests
    log = [json.loads(line) for line in (run_dir / lever_live.REQUEST_LOG).read_text(encoding="utf-8").splitlines()]
    assert [line["state"] for line in log if line["state"] in ("answered", "failed")] == ["answered"] * 4

    outcome = task.finished.outcome if task.finished is not None else None

    if how == "live_stop":
        # The read it asked for was kept back by the source, which stopped: the search ends there, by the rules
        # (U3-D2-E3 R1), before any other decision; the page says why.
        assert (outcome.end, outcome.source_note) == (
            EndReason.SOURCES_UNAVAILABLE, f"{lever_live.NAME}：传输层没有发出这个请求：运行目录里有 LIVE_STOP，没有发出"
        )
        assert presentation.end_note(outcome) == outcome.source_note
    elif how == "pause":
        # Ended at the gate, and said why: the gate's reason for a pause.
        assert (outcome.end, outcome.gate_reason) == (EndReason.GATE, "paused")
        assert presentation.end_note(outcome) == "批准已暂停（有费用待核实或超过预留）"


def test_the_binding_keeps_back_what_the_gate_would_let_through(database, library, clock, gate, model, run_dir, routed, ticks, monkeypatch):
    """The binding's own checks, right before a request is handed on, after the gate's: a claim no longer this
    search's ends it at the gate, with its words, and nothing more is sent."""
    store = DiscoveryTaskStore(ticks.monotonic)
    task = b_task(store, library, model)

    def taken_over():
        # Someone's record in place of this search's: no request of the search goes on after it.
        model.claim_path.write_text(json.dumps({**model.record("someone-else"), "claimed_at": "x"}), encoding="utf-8")

        return read()

    provider = Scripted(search(), taken_over, check(), analyze(), finish())
    task = run_b(store, database, clock, task, gate, provider, model, routed, ticks)
    outcome = task.finished.outcome

    # The third decision was reserved by the gate, then kept back by the binding before it was handed on: released,
    # never sent, never counted against the approval.
    assert provider.kinds() == ["discovery_action"] * 2
    assert [row.state for row in rows(database)] == [LiveRequestState.SETTLED] * 2 + [LiveRequestState.RELEASED]
    assert gate.status().requests_used == 2 and task.sends.generation_sent == 2
    assert (outcome.end, outcome.gate_note) == (EndReason.GATE, "模型批准的占用记录不是这次查找的，没有发出")
    # The read the second decision asked for still went: the source's own claim is this search's. Nothing after.
    assert len(routed.requests) == 5


# --- the source stops: validation B ends there, by the rules, and asks no model again (U3-D2-E3 R1) ---------------


def read_n(number: int):
    return make_action(Tool.READ, postings=[ref(number)], note="读取")


def posting_path(number: int) -> str:
    return f"/v0/postings/palantir/{pid(number, lever_live.BOARD)}"


def ended_by_the_source(task, provider, routed, database, *, decisions: int, requests: list[str], why: str) -> None:
    """What every source stop of validation B leaves: the decisions before it and no other, the source's requests
    before it and no other, every model request settled, the search ended for its source with what stopped it."""
    outcome = task.finished.outcome

    assert provider.kinds() == [ACTION_SCHEMA_NAME] * decisions, provider.kinds()
    assert routed.requests == requests, routed.requests
    states = [row.state for row in rows(database)]
    assert states == [LiveRequestState.SETTLED] * decisions, states
    assert (task.stage, outcome.end) == (Stage.DONE, EndReason.SOURCES_UNAVAILABLE)
    assert outcome.source_note.startswith(f"{lever_live.NAME}：") and why in outcome.source_note, outcome.source_note
    assert presentation.end_note(outcome) == outcome.source_note
    # No step after the one whose source stopped: as many steps as decisions, the last one a call to the source.
    steps = task.finished.trace.steps
    assert len(steps) == decisions and steps[-1].tool in (Tool.SEARCH, Tool.READ, Tool.CHECK)


@pytest.mark.parametrize("failure", ["robots_changed", "listing_403"])
def test_a_source_stop_ends_validation_b_through_the_pages_and_no_model_is_asked_again(b_app, run_dir, routed, failure):
    if failure == "robots_changed":
        routed.robots = b"User-agent: *\nDisallow: /\n"
    else:
        routed.overrides[LIST.format(0)] = response(403, b"forbidden")

    provider = Scripted(search(), finish())
    application, browser, demo = b_app(provider)
    url = b_review(browser, demo)
    assert confirm(browser, url).status_code == 303
    task = stored(application, browser, url)
    shown = text_of(browser.get(url).text)
    why = "robots 规则和 2026-11-10 核对时不同" if failure == "robots_changed" else "来源回应 403"
    requests = ["/robots.txt"] if failure == "robots_changed" else ["/robots.txt", LIST.format(0)]

    # One decision went out, its search stopped the source: no second decision, no other request.
    ended_by_the_source(task, provider, routed, application.state.library.database, decisions=1, requests=requests, why=why)
    assert f"来源不可用（{lever_live.NAME}：" in shown and why in shown
    # Both approvals stay used: nothing is given back.
    assert model_claim(run_dir)["task_id"] == claim_record(run_dir)["task_id"] == task_id(url)


@pytest.mark.parametrize("path", ["anomaly", "failures_in_a_row", "budget", "window", "record", "record_after"])
def test_every_way_the_source_stops_ends_validation_b_before_another_model_call(
    database, library, clock, gate, run_dir, routed, ticks, monkeypatch, path
):
    terms = source_terms(run_dir, ROLERADAR_SOURCE_MAX_REQUESTS="4") if path == "budget" else source_terms(run_dir)
    model = model_live.load_terms(approval_terms(), terms, today=TODAY)
    # The search's own time on a clock of its own: only the source's window runs out here.
    store = DiscoveryTaskStore(Ticks().monotonic)
    task = b_task(store, library, model)
    listed = ["/robots.txt", LIST.format(0), LIST.format(100), LIST.format(200)]

    def failing(handle, data):
        raise OSError("disk full")

    def then(action, *, before=None):
        def step():
            if before is not None:
                before()

            return action

        return step

    if path == "anomaly":
        # A list that is not the documented JSON: the source answered with what it must not.
        routed.overrides[LIST.format(0)] = response(200, b"not json")
        script, decisions, requests, why = (search(), finish()), 1, listed[:2], "返回的 JSON 读不出来"
    elif path == "failures_in_a_row":
        # Two reads in a row with no answer at all: the host's guard stops after the second.
        routed.overrides[posting_path(1)] = b""
        routed.overrides[posting_path(2)] = b""
        script, decisions, why = (search(), read_n(1), read_n(2), finish()), 3, "连续 2 个请求失败"
        requests = [*listed, posting_path(1), posting_path(2)]
    elif path == "budget":
        # Four requests allowed: robots and the list's three pages; the read asked for next is refused, not sent.
        script, decisions, requests, why = (search(), read_n(1), finish()), 2, listed, "请求次数已用完（4 次）"
    elif path == "window":
        # The source's network window runs out before the read the second decision asks for.
        script = (search(), then(read_n(1), before=lambda: setattr(ticks, "now", ticks.now + 700)), finish())
        decisions, requests, why = 2, listed, "网络时间窗口到了"
    elif path == "record":
        # The request record can no longer be written: the read is kept back, never sent.
        script = (search(), then(read_n(1), before=lambda: monkeypatch.setattr(lever_live, "_write_whole", failing)), finish())
        decisions, requests, why = 2, listed, "请求记录写不进去"
    else:
        # The read went out, but what came of it cannot be written: nothing raised, the transport sends no more, and
        # the search ends on its word alone (ControlledTransport.problem).
        whole = lever_live._append_line

        def result_unwritten(path, line):
            # Put in place right before the read: the first result line from then on is the read's.
            if path.name == lever_live.REQUEST_LOG and line.get("state") == "answered":
                raise OSError("disk full")

            whole(path, line)

        script = (search(), then(read_n(1), before=lambda: monkeypatch.setattr(lever_live, "_append_line", result_unwritten)), finish())
        decisions, requests, why = 2, [*listed, posting_path(1)], "请求记录写不进去，之后不再发请求"

    provider = Scripted(*script)
    task = run_b(store, database, clock, task, gate, provider, model, routed, ticks)

    ended_by_the_source(task, provider, routed, database, decisions=decisions, requests=requests, why=why)


def test_validation_a_goes_on_after_its_source_stopped_as_before(database, library, clock, run_dir, routed, ticks):
    """Only validation B ends with its source: the real source with simulated decisions (validation A) goes on with
    what it has, as it did, and nothing ends it for its source."""
    routed.robots = b"User-agent: *\nDisallow: /\n"
    terms = source_terms(run_dir)
    store = DiscoveryTaskStore(ticks.monotonic)
    task = store.create(library.owner, "session-a", replace(
        confirmation_for(library, scope=SCOPE, sources=(lever_live.entry(terms).grant,), preferences=(SALES,), limits=lever_live.LIMITS),
    ))
    state, task = store.begin(task.id, library.owner, "session-a", task.fingerprint)
    assert state is Start.STARTED
    run_search(
        store, database, task.id, local_provider(lambda task: SimulatedProvider()),
        register=live_source_set(terms, **inject(routed, ticks)).make_register(task), clock=clock,
        decider_for=lambda guarded: SimulatedDecider(), environ={},
    )

    with store._lock:
        finished = store._tasks[task.id].finished

    outcome = finished.outcome
    assert routed.requests == ["/robots.txt"]
    # Not ended by the new rule (no source note): it went on deciding after the stop, by its own rules, with nothing
    # more sent to the source.
    assert outcome.source_note is None and len(finished.trace.steps) > 1
