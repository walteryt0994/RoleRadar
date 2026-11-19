"""U3-D6-1 R3, offline: what D6-1's real run showed of the decision protocol and the pages (Codex's acceptance of D6-1,
L1–L3), on synthetic services and temporary libraries only; nothing here reaches a real source or model.

- L1: the decision instructions say, with an example each, that read, check and analyze carry their sources inside
  postings with the top-level source_id null, and that a type word goes in job_type, never in keywords; the action
  schema and the strict check of a field its tool does not take are unchanged; a refused or unusable action put
  right is no adjustment; and the longer instructions still let a decision about many postings alike go out
  (test_discovery_batch_pages.test_many_postings_alike_keep_their_notes_bounded_and_the_search_goes_on).
- L2: a kept summary says who handled its requests as its review named them; the records page says which sources
  are tracked and the most one 现在核对 takes, as the running set has them; the simulation keeps its own words.
- L3: a review page says, without writing, claiming or sending anything, when what is left of the batch cannot cover
  one search, and shows no start button then; a confirmation is refused all the same, and the batch's begin() asks
  again under its lock.

Codex's independent file (/private/tmp/roleradar-d6-live-review-NT3JUf/test_d6_live_review.py) is run as it is beside
these, never changed; its five cases are taken in here as well (L2's three, and L3's hidden button and refused submit).
"""
import json
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.discovery import batch_live, presentation, updates
from app.discovery.agent import ACTION_SCHEMA, DECISION_INSTRUCTIONS, make_action, parse_action
from app.discovery.batch_live import Launch, OperationKind
from app.discovery.contracts import EndReason, Tool
from app.discovery.lever_http import LeverHttpTransport
from app.library.models import JobSearch, LiveRequest
from app.live_gate import FIXED_RESERVE_V1, LiveSetup
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, text_of
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_batch_pages import (  # noqa: F401  (fixtures)
    SOURCE,
    TODAY,
    d6,
    environ,
    kept_result,
    lines,
    model,
    ops,
    post,
    routed,
    run_dir,
    searched,
    ticks,
)
from test_discovery_demo_pages import DEMO_PREFERENCES, DEMO_PROJECT
from test_discovery_live_model import Scripted, analyze, b_review, check, finish, in_run_dir, read, ref, search, whole_search
from test_discovery_pages import confirm
from test_job_record_pages import finished, inline, library  # noqa: F401  (fixtures)

START = 'name="action" value="start"'


def batch_files(run_dir) -> dict[str, bytes]:
    """What the batch keeps on disk of its own, in the run directory and in the test's home: a review page writes none
    of it (its library is the app's, and a session may touch it)."""
    home = run_dir.parent / "home" / ".roleradar" / "batch-approvals"
    paths = [*(run_dir / "approvals").glob("*"), *(home.glob("*") if home.exists() else [])]
    paths += [run_dir / name for name in ("tasks.jsonl", "requests.jsonl", "batch-stop.json", "trajectories.jsonl", "analyses.jsonl")]

    return {str(path): path.read_bytes() for path in paths if path.is_file()}


def ledger_rows(app) -> int:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(func.count()).select_from(LiveRequest))


def nothing_taken(app, run_dir, provider, routed, before: dict[str, bytes]) -> None:
    """A review page claimed, recorded, reserved and sent nothing."""
    assert batch_files(run_dir) == before
    assert provider.calls == [] and routed.requests == [] and ledger_rows(app) == 0
    assert ops(run_dir, "begin") == []


@pytest.fixture
def d6_reads(make_config, clock, run_dir, routed, ticks):
    """As d6, with the read approval's most requests of the test's own (ROLERADAR_BATCH_MAX_REQUESTS)."""
    stacks: list[ExitStack] = []

    def build(provider, most: int):
        approval = model()
        batch = batch_live.load(
            environ(run_dir, ROLERADAR_BATCH_MAX_REQUESTS=str(most)), Launch.DISCOVERY, approval, today=lambda: TODAY,
            monotonic=ticks.monotonic, sleep=ticks.sleep, create=lambda: LeverHttpTransport(opener=routed.opener),
        )
        application = create_app(
            in_run_dir(make_config, run_dir), clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, lambda: provider), batch=batch
        )
        stack = ExitStack()
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))
        stacks.append(stack)
        application.state.discovery.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303

        return application, browser, confirmed(browser, **DEMO_PROJECT)

    yield build

    while stacks:
        stacks.pop().close()


# --- L1: the decision protocol, said tool by tool, checked as strictly as before ------------------------------------------


def test_the_instructions_say_which_fields_read_check_and_analyze_fill_and_keep_type_words_out_of_keywords():
    # The sources of get_posting, check_posting and analyze_posting go inside postings; the top-level source_id is null.
    assert "get_posting、check_posting、analyze_posting 只填 postings：岗位的来源写在每一项的 source_id 里" in DECISION_INSTRUCTIONS
    assert "外层 source_id 和其余栏目都填 null" in DECISION_INSTRUCTIONS
    # A type word goes in job_type, never in keywords: said with an example, beside what D2-E4 F1 already said.
    assert "例如要找 Data Analyst Intern、类型是实习时，keywords 写 Data Analyst，job_type 写实习" in DECISION_INSTRUCTIONS
    assert "城市和求职类型不能写进 keywords" in DECISION_INSTRUCTIONS
    # The structure the model answers in, and the check of it, are as before: nothing is loosened.
    assert set(ACTION_SCHEMA["properties"]) == {"tool", "source_id", "keywords", "city", "job_type", "direction", "postings", "claim", "note"}
    assert ACTION_SCHEMA["additionalProperties"] is False


def test_the_instructions_example_makes_a_legal_check():
    """The example the instructions give, filled into a check, passes the very check the tools make: it cannot teach a
    wrong shape."""
    (line,) = [line for line in DECISION_INSTRUCTIONS.splitlines() if line.startswith("get_posting、check_posting、analyze_posting")]
    example = json.loads("{" + line.split("例如 ", 1)[1].rstrip("。") + "}")
    action = {**make_action(Tool.CHECK, note="核对"), **example}
    parsed, tool, problem = parse_action(json.dumps(action, ensure_ascii=False))

    assert set(example) == {"source_id", "postings"} and example["source_id"] is None
    assert problem is None and tool is Tool.CHECK and parsed.source_id is None
    assert [(posting.source_id, posting.posting_id) for posting in parsed.postings] == [("来源编号", "岗位编号")]


@pytest.mark.parametrize("action", [search(), read(), check(), analyze(), finish()], ids=["search", "read", "check", "analyze", "finish"])
def test_each_tool_with_only_its_own_fields_is_a_legal_action(action):
    assert parse_action(json.dumps(action, ensure_ascii=False))[2] is None


@pytest.mark.parametrize(
    ("action", "problem"),
    [
        (make_action(Tool.CHECK, source_id=SOURCE, postings=[ref()]), "check_posting 不用 source_id，要填 null"),
        (make_action(Tool.READ, source_id=SOURCE, postings=[ref()]), "get_posting 不用 source_id，要填 null"),
        (make_action(Tool.ANALYZE, postings=[ref()], job_type="实习"), "analyze_posting 不用 job_type，要填 null"),
        (make_action(Tool.SEARCH, source_id=SOURCE, keywords="Data Analyst", postings=[ref()]), "search_postings 不用 postings，要填 []"),
        (make_action(Tool.FINISH, claim=EndReason.ENOUGH, source_id=SOURCE), "finish 不用 source_id，要填 null"),
        (make_action(Tool.SEARCH, source_id=SOURCE), "search_postings 缺少 keywords"),
    ],
    ids=["check-source", "read-source", "analyze-type", "search-postings", "finish-source", "search-no-keywords"],
)
def test_a_field_its_tool_does_not_take_is_refused_as_strictly_as_before(action, problem):
    parsed, _, said = parse_action(json.dumps(action, ensure_ascii=False))

    assert parsed is None and said == problem


def test_a_type_word_in_the_keywords_is_refused_and_the_search_put_right_is_no_adjustment(d6, run_dir):
    typed = make_action(Tool.SEARCH, source_id=SOURCE, keywords="Data Analyst Intern", city="San Francisco", job_type="实习", note="检索")
    app, browser, demo = d6(Scripted(typed, *whole_search()))
    searched(app, browser, demo)
    (trajectory,) = lines(run_dir / "trajectories.jsonl")
    first, second = trajectory["steps"][:2]

    # Refused before anything was read, with how to put it right; the next search, put right, ran.
    assert first["outcome"] == "refused" and "Intern" in first["reason"]
    assert second["outcome"] == "done" and second["params"]["检索词"] == "Data Analyst"
    # A refusal put right is never an adjustment (judge 4): only a change after what a tool returned counts.
    assert trajectory["end"] == "enough" and trajectory["adjustments"] == []


def test_a_check_given_its_own_source_id_is_refused_and_the_check_put_right_is_no_adjustment(d6, run_dir):
    unusable = make_action(Tool.CHECK, source_id=SOURCE, postings=[ref()], note="核对")
    app, browser, demo = d6(Scripted(search(), read(), unusable, check(), analyze(), finish()))
    searched(app, browser, demo)
    (trajectory,) = lines(run_dir / "trajectories.jsonl")
    wrong, right = trajectory["steps"][2:4]

    assert wrong["outcome"] == "unusable" and wrong["reason"] == "check_posting 不用 source_id，要填 null"
    assert right["outcome"] == "done"
    assert trajectory["end"] == "enough" and trajectory["adjustments"] == []


# --- L2: the pages say the mode and the limits that run --------------------------------------------------------------


def test_a_kept_real_summary_says_the_real_service_handled_its_requests(d6):
    app, browser, demo = d6(Scripted(*whole_search()))
    url = searched(app, browser, demo)
    assert post(browser, f"{url}/summary").status_code == 303

    with app.state.library.database.transaction() as db:
        row = db.scalars(select(JobSearch)).one()
        record_id, summary = row.id, row.summary

    assert summary["mode"] == "real"
    page = text_of(browser.get(f"/job-records/searches/{record_id}").text)

    assert "本机模拟服务处理的请求" not in page
    assert f"交给真实模型服务的请求 {summary['requests']['handled']} 次（OpenAI 的 {summary['recipient']['model']}" in page


def test_a_kept_simulated_summary_keeps_saying_the_simulation_handled_it(app, browser_a, library, inline):
    url = finished(browser_a, library)
    assert browser_a.post(f"{url}/summary", {"csrf_token": browser_a.csrf_token(), "record": "", "revision": ""}).status_code == 303

    with app.state.library.database.transaction() as db:
        row = db.scalars(select(JobSearch)).one()
        record_id, handled = row.id, row.summary["requests"]["handled"]

    page = text_of(browser_a.get(f"/job-records/searches/{record_id}").text)

    assert f"本机模拟服务处理的请求 {handled} 次" in page and "交给真实模型服务" not in page


def test_a_summary_that_names_no_service_is_the_simulations():
    assert presentation.kept_requests_text({"requests": {"handled": 3}}) == "本机模拟服务处理的请求 3 次"
    simulated = {"requests": {"handled": 2}, "recipient": {"provider": "simulation", "model": "roleradar-rules-1"}}
    assert presentation.kept_requests_text(simulated) == "本机模拟服务处理的请求 2 次"


def test_the_records_page_says_the_d6_tracking_and_its_limits_as_they_run(d6):
    app, browser, demo = d6(Scripted(*whole_search()))
    kept_result(app, browser, searched(app, browser, demo))
    searches = app.state.discovery
    page = text_of(browser.get("/job-records").text)
    (name,) = [grant.name for grant in searches.sources.grants if grant.source_id in searches.sources.keep]

    assert "只限合成来源" not in page and f"只限这次运行准入、可以保存岗位的来源：{name}" in page
    assert "每轮最多 3 个岗位、9 次来源请求、3 分钟" in page and "20 个岗位、60 次来源请求、2 分钟" not in page
    # What the page says is what a 现在核对 runs under: one function gives both.
    assert searches.refresh_limits() == updates.BatchLimits(3, 9, 180.0)


def test_the_records_page_without_a_save_permission_says_nothing_is_tracked(d6):
    app, browser, demo = d6(Scripted(), save=None)
    page = text_of(browser.get("/job-records").text)

    # Nothing can be kept, so nothing is tracked: the page is its short empty state, with no update offered (owner
    # redesign, plan 5.3: no update without a target); the scope's words say so wherever they are shown.
    assert "还没有保存的岗位。" in page and "更新跟踪中的岗位" not in page and "只限合成来源" not in page
    assert "这次运行没有可以保存岗位的来源，所以不跟踪" in presentation.tracking_scope(app.state.discovery.sources)


def test_the_simulations_records_page_keeps_its_own_words(app, browser_a):
    page = text_of(browser_a.get("/job-records").text)

    # Empty: no update and none of its details (plan 5.3); the simulation's own words stay what they were.
    assert "还没有保存的岗位。" in page and "更新跟踪中的岗位" not in page
    assert "只限合成来源" in presentation.tracking_scope(app.state.discovery.sources)
    assert app.state.discovery.refresh_limits() == updates.BatchLimits()


# --- L3: a review says when what is left cannot cover one search, and takes nothing ----------------------------------


@pytest.mark.parametrize(("left", "shown"), [(10, True), (9, False), (8, False)])
def test_the_review_shows_its_start_button_only_when_the_model_requests_left_cover_one_search(d6, run_dir, routed, left, shown):
    provider = Scripted()
    app, browser, demo = d6(provider, max_requests=left)
    url = b_review(browser, demo)
    before = batch_files(run_dir)
    page = browser.get(url).text
    need, worst = app.state.batch.model_need(OperationKind.SEARCH)

    assert need == 10 and (START in page) is shown

    if not shown:
        # Each shortfall said on its own (UI bilingual batch, plan 4.3): here the requests, not the money.
        assert f"模型批准还剩 {left} 次请求，不够这项操作最多要用的 {need} 次" in text_of(page)
        assert "应用内预算还剩" not in text_of(page)

    nothing_taken(app, run_dir, provider, routed, before)


def test_the_review_shows_no_start_button_when_the_money_left_cannot_cover_one_search(d6, run_dir, routed):
    provider = Scripted()
    app, browser, demo = d6(provider, max_cost_micro_usd=2_000_000)
    need, worst = app.state.batch.model_need(OperationKind.SEARCH)
    # Twenty requests are left, enough for ten; $2.00 is not the worst one search may reserve.
    assert worst > 2_000_000
    url = b_review(browser, demo)
    before = batch_files(run_dir)
    page = browser.get(url).text

    assert START not in page
    # The money alone, said on its own (plan 4.3): twenty requests are enough, so they are not named as short.
    assert f"应用内预算还剩 $2.00，不够这项操作最坏要预留的 ${worst / 1_000_000:.2f}" in text_of(page)
    assert "次请求，不够" not in text_of(page)
    nothing_taken(app, run_dir, provider, routed, before)


@pytest.mark.parametrize(("most", "shown"), [(12, True), (11, False)])
def test_the_review_counts_robots_once_a_launch_against_the_reads_left(d6_reads, run_dir, routed, most, shown):
    provider = Scripted()
    app, browser, demo = d6_reads(provider, most)
    url = b_review(browser, demo)
    before = batch_files(run_dir)
    page = browser.get(url).text

    # Its first read of this launch reads robots too: a search may then take 11 + 1.
    assert (START in page) is shown

    if not shown:
        assert "读取批准剩下 11 次请求，不够这项操作最多要用的 12 次" in text_of(page)

    nothing_taken(app, run_dir, provider, routed, before)


def test_a_review_made_while_enough_was_left_says_so_no_longer_once_a_search_used_it(d6, run_dir, routed):
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider, max_requests=16)
    early = b_review(browser, demo)
    assert START in browser.get(early).text
    # Another search uses 7 of the 16: 9 are left, short of one search's 10.
    searched(app, browser, demo)
    calls, reads, begun = len(provider.calls), len(routed.requests), ops(run_dir, "begin")
    page = browser.get(early).text

    assert START not in page and "模型批准还剩 9 次" in text_of(page)

    # Submitted all the same: voided (the batch changed since the review) or refused, and nothing started or went out.
    response = confirm(browser, early)
    assert response.status_code in (303, 409)

    if response.status_code == 303:
        assert START not in browser.get(response.headers["location"]).text

    assert len(provider.calls) == calls and len(routed.requests) == reads and ops(run_dir, "begin") == begun


def test_a_submit_made_without_the_button_is_refused_by_the_same_reckoning_and_sends_nothing(d6, run_dir, routed):
    provider = Scripted()
    app, browser, demo = d6(provider, max_requests=8)
    url = b_review(browser, demo)
    before = batch_files(run_dir)
    response = confirm(browser, url)

    assert response.status_code == 409
    assert "模型批准还剩 8 次" in text_of(response.text) and "所以没有开始，什么都没有发送" in text_of(response.text)
    nothing_taken(app, run_dir, provider, routed, before)
