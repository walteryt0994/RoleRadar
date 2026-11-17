"""U3-D6, offline: the D6 batch through the pages (app.discovery.batch_live wired into the app; D6 plan, revision 1).

Everything of a real service but the provider, as validation B's tests have it (test_discovery_live_model): a made-up
approval under the real budget policy opens the gate on the test's own library in its run directory, and a made-up
provider under the real recipient's name answers decisions from a script and analyses by the local simulation's
rules. The board is Palantir's shape answered inside this process (RoutedLever, through the real transport over
socket pairs). The admissions and approvals are a test's own, under a home of the test's own: none is the Owner's,
none admits a source or grants a permission of the product, and nothing here validates a real model or source.
"""
import io
import json
import zipfile
from contextlib import ExitStack
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.discovery import batch_live, lever_live, presentation, watches
from app.discovery.batch_live import Launch, OperationKind
from app.discovery.contracts import Access, DimensionScope, Others, SearchScope
from app.discovery.lever_http import LeverHttpTransport
from app.discovery.web import DEMO_VARIABLE
from app.library import job_choices
from app.library.errors import ConfigError
from app.library.models import JobPosting, JobPostingRecheck, JobSearch, JobWatch, LiveRequest
from app.live_gate import FIXED_RESERVE_V1, LiveSetup
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, text_of
from lever_helpers import DESCRIPTION, Ticks, pid, posting
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_demo_pages import DEMO_PREFERENCES, DEMO_PROJECT
from test_discovery_live_model import Scripted, analyze, approval_terms, b_review, check, finish, in_run_dir, read, ref, search, whole_search
from test_discovery_live_source import RoutedLever
from test_discovery_pages import confirm, stored

TODAY = date(2026, 11, 12)
ADMISSION = "u3d6-admit-2026-11-12-a"
SOURCE_APPROVAL = "u3d6-src-2026-11-12-a"
MODEL_APPROVAL = "u3d6-ai-2026-11-12-a"
DIRECTION_APPROVAL = "u3d6-dir-2026-11-12-a"
SOURCE = lever_live.BOARD.source_id


def environ(run_dir: Path, **changes) -> dict[str, str]:
    values = {
        "HOME": str(run_dir.parent / "home"),
        "ROLERADAR_BATCH_RUN_DATE": TODAY.isoformat(),
        "ROLERADAR_BATCH_RUN_DIR": str(run_dir),
        "ROLERADAR_BATCH_ADMISSION_ID": ADMISSION,
        "ROLERADAR_BATCH_ADMISSION_UNTIL": "2026-12-10",
        "ROLERADAR_BATCH_SOURCE_APPROVAL_ID": SOURCE_APPROVAL,
        "ROLERADAR_BATCH_BOARD": "palantir",
        "ROLERADAR_BATCH_MAX_REQUESTS": "50",
        "ROLERADAR_BATCH_SAVE": "facts",
    }
    values.update(changes)

    return {name: value for name, value in values.items() if value is not None}


def model(**changes):
    values = {"approval_id": MODEL_APPROVAL, "max_requests": 20, "max_cost_micro_usd": 4_500_000}
    values.update(changes)

    return approval_terms(**values)


@pytest.fixture
def run_dir(tmp_path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()

    return folder


@pytest.fixture
def routed():
    return RoutedLever()


@pytest.fixture
def ticks():
    return Ticks()


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def handed(run_dir: Path) -> int:
    return sum(1 for line in lines(run_dir / "requests.jsonl") if line["state"] == "handed")


def ops(run_dir: Path, state: str = "end") -> list[str]:
    return [line["kind"] for line in lines(run_dir / "tasks.jsonl") if line["state"] == state]


@pytest.fixture
def d6(make_config, clock, run_dir, routed, ticks):
    """build(provider, ...) -> the discovery launch's app on the test's library in its run directory, user A signed in
    and the demo material entered; close() ends the last app (its lifespan closes the batch and its lock)."""
    stacks: list[ExitStack] = []

    def build(provider, *, save="facts", fresh=True, **changes):
        approval = model(**changes)
        batch = batch_live.load(
            environ(run_dir, ROLERADAR_BATCH_SAVE=save), Launch.DISCOVERY, approval, today=lambda: TODAY,
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
        demo = None

        if fresh:
            assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
            demo = confirmed(browser, **DEMO_PROJECT)

        return application, browser, demo

    def close() -> None:
        stacks.pop().close()

    build.close = close
    yield build

    while stacks:
        stacks.pop().close()


def searched(app, browser, demo) -> str:
    url = b_review(browser, demo)
    assert confirm(browser, url).status_code == 303

    return url


def kept_result(app, browser, url) -> str:
    """The search's first result kept through the pages; its record's id."""
    task = stored(app, browser, url)
    result = task.finished.outcome.results[0]
    response = browser.post(
        f"{url}/keep",
        {"csrf_token": browser.csrf_token(), "source": result.ref.source_id, "posting": result.ref.posting_id, "action": "keep", "record": "", "revision": ""},
    )
    assert response.status_code == 303, response.text

    with app.state.library.database.transaction() as db:
        (row,) = db.scalars(select(JobPosting).where(JobPosting.posting_id == result.ref.posting_id)).all()

        return row.id


def post(browser, path: str, **fields):
    return browser.post(path, {"csrf_token": browser.csrf_token(), **fields})


def revision(app, record_id: str) -> str:
    with app.state.library.database.transaction() as db:
        return str(db.get(JobPosting, record_id).revision)


# --- the launch ---------------------------------------------------------------------------------------------------------


def test_a_launch_with_another_set_or_its_library_outside_the_run_directory_does_not_start(make_config, clock, run_dir, routed, ticks, monkeypatch, tmp_path):
    approval = model()

    def batch():
        return batch_live.load(environ(run_dir), Launch.DISCOVERY, approval, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)

    monkeypatch.setenv(DEMO_VARIABLE, "lever-replay")

    with pytest.raises(ConfigError, match="不能和"):
        create_app(in_run_dir(make_config, run_dir), clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, Scripted), batch=batch())

    monkeypatch.delenv(DEMO_VARIABLE)
    elsewhere = make_config(db_path=tmp_path / "other" / "library.db", files_dir=tmp_path / "other" / "files")

    with pytest.raises(ConfigError, match="运行目录里"):
        create_app(elsewhere, clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, Scripted), batch=batch())

    # Refused, the app gave the run directory's lock back: a launch as it must be starts.
    batch().close()


# --- one search -----------------------------------------------------------------------------------------------------------


def test_a_search_runs_under_the_batch_claims_for_it_and_keeps_its_trajectory(d6, run_dir):
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    url = b_review(browser, demo)
    review = text_of(browser.get(url).text)

    assert "读取批准" in review and "这一批最多 50 次请求" in review and "这次查找的上限" in review
    assert "由你的各项操作共用" in review and "保存许可：只存事实和链接" in review
    assert confirm(browser, url).status_code == 303

    # robots once for the launch, three list pages (the board's 300, read in part), one read and one check.
    assert handed(run_dir) == 6
    assert ops(run_dir, "begin") == ["search"] and ops(run_dir) == ["search"]
    claims = {path.name: json.loads(path.read_text()) for path in (run_dir.parent / "home" / ".roleradar" / "batch-approvals").glob("*.claim")}
    assert set(claims) == {f"{MODEL_APPROVAL}.claim", f"{SOURCE_APPROVAL}.claim"}
    assert len({claim["batch_id"] for claim in claims.values()}) == 1

    # One full analysis at most: five decisions and its two generations, through the one gate.
    with app.state.library.database.transaction() as db:
        assert len(db.scalars(select(LiveRequest)).all()) == 7

    (trajectory,) = lines(run_dir / "trajectories.jsonl")
    assert [step["tool"] for step in trajectory["steps"]] == ["search_postings", "get_posting", "check_posting", "analyze_posting", "finish"]
    assert len(trajectory["actions"]) == 5 and trajectory["adjustments"] == [] and trajectory["end"] == "enough"


# --- what may be kept, by the admission's save permission ------------------------------------------------------------------


def test_without_a_save_permission_nothing_is_kept_checked_or_read_from_a_link(d6, run_dir):
    app, browser, demo = d6(Scripted(*whole_search()), save=None)
    searches = app.state.discovery
    assert (searches.sources.keep, searches.sources.keep_analyses) == (frozenset(), frozenset())
    url = searched(app, browser, demo)
    page = text_of(browser.get(url).text)
    before = handed(run_dir)

    # The result offers no keeping, and the review said so before it started.
    assert "加入我的岗位或做决定" not in page and "保存这次查找的摘要" not in page
    task = stored(app, browser, url)
    result = task.finished.outcome.results[0]
    refused = browser.post(f"{url}/keep", {"csrf_token": browser.csrf_token(), "source": SOURCE, "posting": result.ref.posting_id, "action": "keep", "record": "", "revision": ""})

    assert refused.status_code >= 400

    with app.state.library.database.transaction() as db:
        assert db.scalars(select(JobPosting)).all() == []

    link = post(browser, "/job-records/supply/link", link=f"https://jobs.lever.co/palantir/{pid(2, lever_live.BOARD)}")
    assert link.status_code == 422 and handed(run_dir) == before


@pytest.mark.parametrize("save, analyses", [("facts", False), ("facts_quotes", True)])
def test_facts_keep_postings_and_decisions_and_only_quotes_keep_analyses(d6, run_dir, save, analyses):
    app, browser, demo = d6(Scripted(*whole_search()), save=save)
    searches = app.state.discovery
    assert searches.sources.keep == frozenset({SOURCE})
    assert searches.sources.keep_analyses == (frozenset({SOURCE}) if analyses else frozenset())
    url = searched(app, browser, demo)
    record_id = kept_result(app, browser, url)
    decided = post(browser, f"/job-records/postings/{record_id}/decide", revision=revision(app, record_id), decision="favorite", note="")

    assert decided.status_code in (303, 200)
    # The keep page offers keeping the analysis only where the admission allows its quotes too.
    result = stored(app, browser, url).finished.outcome.results[0]
    keep_page = browser.get(f"{url}/keep?source={SOURCE}&posting={result.ref.posting_id}").text
    assert ('<button type="submit" class="secondary">保存这份分析</button>' in keep_page) is analyses


# --- the day's other operations share the batch -------------------------------------------------------------------------------


def test_checks_a_watch_and_links_share_the_batch_each_within_its_own_most(d6, run_dir, clock):
    app, browser, demo = d6(Scripted(*whole_search()))
    url = searched(app, browser, demo)
    record_id = kept_result(app, browser, url)
    first = handed(run_dir)

    assert post(browser, f"/job-records/postings/{record_id}/recheck", revision=revision(app, record_id)).status_code == 303
    assert post(browser, f"/job-records/postings/{record_id}/apply-check", revision=revision(app, record_id)).status_code == 303
    clock.advance(minutes=11)
    assert post(browser, "/job-records/refresh").status_code == 303
    real = post(browser, "/job-records/supply/link", link=f"https://jobs.lever.co/palantir/{pid(2, lever_live.BOARD)}")
    gone = post(browser, "/job-records/supply/link", link="https://jobs.lever.co/palantir/b0000999-0000-4000-8000-000000000999")

    assert real.status_code == 303 and gone.status_code == 422
    assert ops(run_dir) == ["search", "check", "apply", "recheck", "supply", "supply"]
    # Each within its own most; nothing more for robots, read once for the launch.
    ended = [line for line in lines(run_dir / "tasks.jsonl") if line["state"] == "end"]
    assert all(line["sent"] <= batch_live.OPERATION_REQUESTS[batch_live.OperationKind(line["kind"])] for line in ended[1:])
    assert handed(run_dir) == app.state.batch.sent > first
    assert app.state.batch.problem() is None


def test_a_review_is_void_once_the_batch_used_something_since(d6, run_dir):
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    record_id = kept_result(app, browser, searched(app, browser, demo))
    url = b_review(browser, demo)
    calls = len(provider.calls)
    assert post(browser, f"/job-records/postings/{record_id}/recheck", revision=revision(app, record_id)).status_code == 303
    voided = confirm(browser, url)

    # A new review, made with what is left now; nothing started and nothing was sent for it.
    assert voided.status_code == 303 and voided.headers["location"].endswith("?notice=batch_changed")
    assert "这一批的剩余次数或预算在核对之后有了变化" in text_of(browser.get(voided.headers["location"]).text)
    assert len(provider.calls) == calls and ops(run_dir, "begin").count("search") == 1


# --- 开启每日查新 on the batch's board (U3-D6 close-out, F1) --------------------------------------------------------------------

# What the page must say before the start reads the real board, and what it must never say of it.
REAL_WATCH_WORDS = (
    "这是真实来源，不是合成数据：开启要联网读取它的公开列表",
    "确认开启以后马上读取列表，建立基准",
    f"都计入这一批的读取批准 {SOURCE_APPROVAL}（只在 {TODAY.isoformat()} 当天有效，这一批最多 50 次",
    "不发送你的经历和偏好，不调用模型（不用 AI）",
    "现在不会自动检查",
)
NEVER_SAID = ("本机合成数据", "不联网")
# What a start reserves, as the batch reserves it: the list's most, and robots before a launch's first read.
ROBOTS_READ = "这一项最多读取列表 4 次（这次启动已经读过 robots，不再读），共预留 4 次"
ROBOTS_FIRST = "这一项最多读取列表 4 次；这次启动还没有读过 robots，开始前另读 robots 1 次，共预留 5 次"


def watch_review(app, browser, demo, provider, run_dir):
    """A summary kept from a search, and its 开启每日查新 page read: (summary id, html, text); the page itself sends nothing."""
    url = searched(app, browser, demo)
    assert post(browser, f"{url}/summary", record="", revision="").status_code == 303

    with app.state.library.database.transaction() as db:
        summary_id = db.scalar(select(JobSearch.id))

    before = (app.state.batch.sent, len(provider.calls), ops(run_dir, "begin"))
    page = browser.get(f"/job-records/searches/{summary_id}/watch")
    assert page.status_code == 200
    assert (app.state.batch.sent, len(provider.calls), ops(run_dir, "begin")) == before

    return summary_id, page.text, text_of(page.text)


def test_the_watch_page_on_the_batchs_board_says_a_real_read_under_its_approval_and_sends_nothing(d6, run_dir):
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    _, html, text = watch_review(app, browser, demo, provider, run_dir)

    assert "Palantir" in text
    assert all(words in text for words in REAL_WATCH_WORDS), [words for words in REAL_WATCH_WORDS if words not in text]
    assert not any(words in text for words in NEVER_SAID)
    assert f'name="notice_version" value="{watches.LIVE_NOTICE_VERSION}"' in html
    # The search read robots for this launch: the start reserves the list's most alone.
    assert app.state.batch.robots_read and ROBOTS_READ in text and ROBOTS_FIRST not in text


def test_after_a_restart_the_watch_page_says_robots_comes_first_and_the_start_reserves_and_counts_it(d6, run_dir):
    """A launch restarted in the same run folder has read nothing yet: a watch started first reads robots before the
    list, and its page says so (five reserved, four for the list and robots once), within the batch's one total."""
    app, browser, demo = d6(Scripted(*whole_search()))
    url = searched(app, browser, demo)
    assert post(browser, f"{url}/summary", record="", revision="").status_code == 303
    d6.close()
    again, browser, _ = d6(Scripted(), fresh=False)

    with again.state.library.database.transaction() as db:
        summary_id = db.scalar(select(JobSearch.id))

    sent, total = again.state.batch.sent, again.state.batch.source.max_requests
    page = browser.get(f"/job-records/searches/{summary_id}/watch")
    text = text_of(page.text)

    assert not again.state.batch.robots_read and again.state.batch.sent == sent
    assert ROBOTS_FIRST in text and ROBOTS_READ not in text and f"已用 {sent} 次" in text
    assert all(words in text for words in REAL_WATCH_WORDS) and not any(words in text for words in NEVER_SAID)

    started = post(browser, f"/job-records/searches/{summary_id}/watch", expected_revision="1", notice_version=watches.LIVE_NOTICE_VERSION)
    begun = [line for line in lines(run_dir / "tasks.jsonl") if line["state"] == "begin"][-1]
    ended = [line for line in lines(run_dir / "tasks.jsonl") if line["state"] == "end"][-1]

    # Reserved as the page said (the list's most and robots once), robots read and counted with the list: nothing left
    # out of the count, and the batch's total as it was.
    assert started.status_code == 303 and (begun["kind"], begun["requests"], begun["robots"]) == (OperationKind.WATCH.value, 4, 1)
    assert again.state.batch.robots_read and 1 < ended["sent"] <= 5 and again.state.batch.sent - sent == ended["sent"] == handed(run_dir) - sent
    assert again.state.batch.source.max_requests == total


def test_a_watch_started_under_other_words_on_the_batch_starts_nothing_and_sends_nothing(d6, run_dir):
    """The words of the page as it was before (watch-1, "本机合成数据，不联网" on the real board), or none: refused with
    the page as it is now; no watch, no operation, nothing read and nothing asked of the model."""
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    summary_id, _, _ = watch_review(app, browser, demo, provider, run_dir)
    before = (app.state.batch.sent, len(provider.calls), ops(run_dir, "begin"))

    for words in (watches.NOTICE_VERSION, ""):
        refused = post(browser, f"/job-records/searches/{summary_id}/watch", expected_revision="1", notice_version=words)
        said = text_of(refused.text)

        assert refused.status_code == 409 and "说明在你打开之后有了变化" in said, words
        assert all(phrase in said for phrase in REAL_WATCH_WORDS) and not any(phrase in said for phrase in NEVER_SAID)

    with app.state.library.database.transaction() as db:
        assert db.scalar(select(JobWatch)) is None

    assert (app.state.batch.sent, len(provider.calls), ops(run_dir, "begin")) == before


def test_a_watch_started_under_the_batchs_words_reads_its_baseline_within_its_most(d6, run_dir):
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    summary_id, _, _ = watch_review(app, browser, demo, provider, run_dir)
    sent, calls = app.state.batch.sent, len(provider.calls)

    started = post(browser, f"/job-records/searches/{summary_id}/watch", expected_revision="1", notice_version=watches.LIVE_NOTICE_VERSION)

    assert started.status_code == 303 and started.headers["location"].endswith("#watch")

    with app.state.library.database.transaction() as db:
        watch = db.scalar(select(JobWatch))

    ended = [line for line in lines(run_dir / "tasks.jsonl") if line["state"] == "end"]
    # The words it was started under are kept with it; its baseline read the board within the most a start reads, and
    # asked nothing of the model.
    assert watch is not None and watch.notice_version == watches.LIVE_NOTICE_VERSION
    assert ended[-1]["kind"] == OperationKind.WATCH.value and 0 < ended[-1]["sent"] <= batch_live.OPERATION_REQUESTS[OperationKind.WATCH]
    assert app.state.batch.sent - sent == ended[-1]["sent"] and len(provider.calls) == calls


# --- restarts, users, exports ------------------------------------------------------------------------------------------------


def test_a_restart_goes_on_from_what_was_used_and_another_user_is_refused(d6, run_dir):
    app, browser, demo = d6(Scripted(*whole_search()))
    record_id = kept_result(app, browser, searched(app, browser, demo))
    used, batch_id = app.state.batch.sent, app.state.batch.batch_id
    d6.close()
    again, browser, _ = d6(Scripted(), fresh=False)

    assert (again.state.batch.sent, again.state.batch.batch_id, again.state.batch.owner_id) == (used, batch_id, app.state.batch.owner_id)
    assert lines(run_dir / "tasks.jsonl")[-1]["state"] == "end"
    other = Browser.signed_in(again, "合成用户 B")
    refused = post(other, "/job-records/supply/link", link=f"https://jobs.lever.co/palantir/{pid(2, lever_live.BOARD)}")

    assert refused.status_code == 409 and "另一个用户" in text_of(refused.text) and again.state.batch.sent == used
    # The user of the batch goes on, from what was used.
    assert post(browser, f"/job-records/postings/{record_id}/recheck", revision=revision(again, record_id)).status_code == 303
    assert again.state.batch.sent > used


def test_an_export_keeps_the_real_postings_facts_and_never_its_text(d6, run_dir):
    app, browser, demo = d6(Scripted(*whole_search()))
    kept_result(app, browser, searched(app, browser, demo))
    archive = zipfile.ZipFile(io.BytesIO(browser.get("/export/all.zip").content))
    everything = "".join(archive.read(name).decode("utf-8", errors="replace") for name in archive.namelist() if name.endswith((".json", ".md")))

    assert SOURCE in everything and DESCRIPTION not in everything


# --- a real source's 404 (D6 plan 1.4, 4.1 item 8) ------------------------------------------------------------------------


def test_a_404_from_the_real_source_leaves_the_status_to_be_confirmed_and_the_page_and_export_say_why(d6, run_dir, routed, clock):
    assert job_choices.REAL_SOURCE == Access.LOCAL_FETCH.value
    app, browser, demo = d6(Scripted(*whole_search()))
    record_id = kept_result(app, browser, searched(app, browser, demo))

    with app.state.library.database.transaction() as db:
        posting_id = db.get(JobPosting, record_id).posting_id

    # The board no longer has it: its address answers 404. The clock moves, so the recheck is the latest.
    routed.postings = [item for item in routed.postings if item["id"] != posting_id]
    clock.advance(minutes=1)
    assert post(browser, f"/job-records/postings/{record_id}/recheck", revision=revision(app, record_id)).status_code == 303

    with app.state.library.database.transaction() as db:
        (recheck,) = db.scalars(select(JobPostingRecheck).where(JobPostingRecheck.posting_record_id == record_id)).all()

    # The judgement is unchanged: a 404 alone closes nothing.
    assert recheck.status != "closed" and recheck.evidence is None
    page = text_of(browser.get(f"/job-records/postings/{record_id}").text)
    note = job_choices.closure_note(Access.LOCAL_FETCH.value, None)

    assert "状态待确认：最近一次核对" in page and note in page and "这个来源没有给截止日期" in note
    archive = zipfile.ZipFile(io.BytesIO(browser.get("/export/all.zip").content))
    readable = "".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith("jobs.md"))

    assert f"- 核对说明：{note}" in readable
    # Any other source's posting says nothing of it, and a deadline the source gives drops the last sentence.
    assert job_choices.closure_note(Access.SYNTHETIC.value, None) is None
    assert "没有给截止日期" not in job_choices.closure_note(Access.LOCAL_FETCH.value, "2026-12-01")


# --- the scope's values, each read as one (D6 plan 4.1 item 7) --------------------------------------------------------------


def test_each_scope_value_reads_as_one_value_however_it_is_written():
    assert presentation.shown_value("北京") == "北京"
    assert presentation.shown_value("数据分析/商业分析") == '"数据分析/商业分析"'
    # A value holding quotes of its own: written as the decision prompt writes it, its own quotes kept or escaped.
    assert presentation.shown_value("“数据”分析") == '"“数据”分析"'
    assert presentation.shown_value('数据"分析') == '"数据\\"分析"'
    scope = SearchScope(
        city=DimensionScope(values=("北京", "上海、杭州"), others=Others.EXCLUDED, ideal=("北京",)),
        job_type=DimensionScope(values=("实习",), others=Others.EXCLUDED),
        direction=DimensionScope(values=("“数据”分析", "产品"), others=Others.INCLUDED, excluded=("销售/客服",)),
        targets=("数据分析实习生", "BI“工程师”"),
    )
    lines = dict(presentation.scope_lines(scope))

    assert lines["要找的岗位"] == '数据分析实习生、"BI“工程师”"'
    assert lines["城市"].startswith('北京（理想）、"上海、杭州"；')
    assert lines["岗位方向"].startswith('"“数据”分析"、产品；') and '不能接受："销售/客服"（不会检索）' in lines["岗位方向"]


# --- the direction launch ------------------------------------------------------------------------------------------------------


def test_an_exploration_runs_once_under_the_direction_batch(make_config, clock, run_dir, ticks):
    from test_direction_live_pages import Answering, approval as direction_approval, confirmation, material
    from test_direction_pages import review as direction_review, task_of

    approval = direction_approval(approval_id=DIRECTION_APPROVAL, max_requests=1, max_cost_micro_usd=400_000)
    batch = batch_live.load(environ(run_dir), Launch.DIRECTION, approval, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)
    provider = Answering()

    with TestClient(
        create_app(in_run_dir(make_config, run_dir), clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, lambda: provider), batch=batch),
        base_url=LOCAL_BASE_URL,
        client=("127.0.0.1", 50000),
    ) as client:
        application = client.app
        application.state.directions.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        project = material(browser)
        task = task_of(direction_review(browser, [project]))
        assert browser.post(f"{task}/confirm", confirmation(browser, task)).status_code == 303

        assert len(provider.calls) == 1 and ops(run_dir) == ["direction"]
        claim = run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{DIRECTION_APPROVAL}.claim"
        assert json.loads(claim.read_text())["run_dir"] == str(run_dir.resolve())

        # The approval's one request used: a second exploration does not start, and sends nothing.
        second = task_of(direction_review(browser, [project]))
        refused = browser.post(f"{second}/confirm", confirmation(browser, second))

        assert refused.status_code == 409 and len(provider.calls) == 1


# --- the ledger the batch's (D6-0 R1, Codex's F1) ------------------------------------------------------------------


@pytest.mark.parametrize("which", ["copy_beside", "copy_in_place", "another_library"])
def test_a_library_put_back_or_another_ones_does_not_start_and_the_batch_stays_stopped(d6, run_dir, make_config, clock, ticks, routed, which):
    import sqlite3

    app, browser, demo = d6(Scripted(*whole_search()))
    original = app.state.library.config.db_path
    before = run_dir / "before-search.db"

    # A copy of the same library made before the approvals were used: its users, material and ledger as they were.
    with sqlite3.connect(original) as source, sqlite3.connect(before) as copy:
        source.backup(copy)

    searched(app, browser, demo)
    used = app.state.live_service.gate.status()
    assert used.requests_used == 7
    batch_id = app.state.batch.batch_id
    d6.close()
    files = run_dir / "files"

    if which == "copy_in_place":
        original.write_bytes(before.read_bytes())
        chosen = original
    elif which == "copy_beside":
        chosen = before
    else:
        chosen, files = run_dir / "another.db", run_dir / "another-files"

    def start(db_path, files_dir):
        terms = model()
        batch = batch_live.load(
            environ(run_dir), Launch.DISCOVERY, terms, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep,
            create=lambda: LeverHttpTransport(opener=routed.opener),
        )

        return create_app(make_config(db_path=db_path, files_dir=files_dir), clock=clock, live=LiveSetup(terms, FIXED_RESERVE_V1, Scripted), batch=batch)

    with pytest.raises(ConfigError, match="比主目录记下的少|编号对不上"):
        start(chosen, files)

    stop = json.loads((run_dir / "batch-stop.json").read_text(encoding="utf-8"))
    assert stop["code"] == "ledger" and (run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{batch_id}.stop.json").exists()

    if which != "copy_in_place":
        # The library as it was is the batch's, but the batch stopped for good when another was offered.
        with pytest.raises((ConfigError, batch_live.BatchTermsError), match="已经停止"):
            start(original, run_dir / "files")


# --- many postings alike (D6-0 R1, Codex's F3) ----------------------------------------------------------------------


@pytest.mark.parametrize("count", [17, 60])
def test_many_postings_alike_keep_their_notes_bounded_and_the_search_goes_on(d6, routed, count):
    from app.ai_provider import request_bytes
    from app.discovery.agent import ACTION_FORMAT, ACTION_SCHEMA_NAME

    # The same company, title and (not recognised) city, each its own posting: the case Codex reproduced, and more.
    routed.postings = [posting(number, board=lever_live.BOARD, location=None) for number in range(1, count + 1)]
    provider = Scripted(*whole_search())
    app, browser, demo = d6(provider)
    url = searched(app, browser, demo)
    task = stored(app, browser, url)

    # Every request the search was going to make was made: five decisions and the analysis's two generations.
    assert len(provider.calls) == 7 and task.finished.outcome.end.value == "enough"
    decisions = [prompt for (name, _), prompt in zip(provider.calls, provider.prompts) if name == ACTION_SCHEMA_NAME]
    second = decisions[1]
    # The risk is still said in the decision's own lines, cut short as they are, with how many look alike.
    assert f"另有 {count - 1} 个公司、标题、城市都相同的岗位：无可靠依据确认是同一个，分开列出" in second
    note = next(note for note in task.finished.outcome.results[0].notes if note.startswith("另有"))
    assert note.endswith("等，各自单独列出）") and note.count("[lever-palantir/") == 2
    # Each posting listed keeps its own id; its note names two others at most.
    for line in second.splitlines():
        if line.startswith("- [lever-palantir/") and "公司、标题、城市相同" in line:
            assert line.count("[lever-palantir/") <= 3

    # Bounded, whatever the count: at most LISTED postings in each of the two lists a decision shows, each line's
    # marks cut short, so the largest decision body stays within the 15,000 bytes allowed (about 13,500 here, for 17
    # postings alike and for 60; it was 25,489 for 17 before, and the second decision never went out).
    from app.discovery.tools import LISTED

    assert sum(1 for line in second.splitlines() if line.startswith("- [lever-palantir/")) <= 2 * LISTED
    largest = max(
        request_bytes(provider.request_body(prompt, max_output_tokens=2_000, reasoning_effort="low", json_schema_format=ACTION_FORMAT))
        for prompt in decisions
    )
    assert largest < 14_000 < batch_live.DECISION_REQUEST_BYTES


# --- a model request noted before it goes (D6-0 R2, Codex's F1 residue) ---------------------------------------------
#
# The real gate and an in-process made-up provider, as Codex's boundary cases have them
# (/private/tmp/roleradar-d6-r1-check-uZD0V4/test_d6_r1_boundaries.py, run as it is): one request through the gate's own
# hand_on and the batch's binding, then the process "ends" before the operation does, at one of four moments, and the
# library is either the one the request was recorded in or a copy made before it.


class Handing:
    """The task's sending as runner._Sending does it for the batch: the binding asked right before each request is
    handed on. at_hand_on runs first, at that very moment (a crash's snapshot, for one)."""

    def __init__(self, binding, at_hand_on=None):
        self.binding = binding
        self.at_hand_on = at_hand_on

    def hand_on(self, call):
        if self.at_hand_on is not None:
            self.at_hand_on()

        assert self.binding.refusal() is None

    def came_back(self, call, delivered):
        pass


def copy_library(source: Path, target: Path) -> None:
    import sqlite3

    with sqlite3.connect(source) as live, sqlite3.connect(target) as copy:
        live.backup(copy)


def restart_on(make_config, clock, ticks, routed, run_dir, library: Path):
    terms = model()
    batch = batch_live.load(
        environ(run_dir), Launch.DISCOVERY, terms, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep,
        create=lambda: LeverHttpTransport(opener=routed.opener),
    )

    return create_app(make_config(db_path=library, files_dir=run_dir / "files"), clock=clock, live=LiveSetup(terms, FIXED_RESERVE_V1, Scripted), batch=batch)


@pytest.mark.parametrize("moment", ["settled", "unknown", "out_at_crash", "never_sent"])
@pytest.mark.parametrize("library", ["as_recorded", "copied_before"])
def test_a_model_request_noted_before_it_goes_keeps_a_library_without_it_from_starting(d6, run_dir, make_config, clock, ticks, routed, moment, library):
    from app.ai_provider import AIProviderConfigError
    from app.discovery.agent import ACTION_FORMAT

    def no_answer():
        raise RuntimeError("synthetic: handed on, no answer")

    def never_sent():
        raise AIProviderConfigError("synthetic: stopped before it was handed to the SDK")

    original = None
    out = run_dir / "with-the-request-out.db"
    provider = Scripted({"settled": finish, "unknown": no_answer, "never_sent": never_sent, "out_at_crash": lambda: copy_library(original, out) or finish()}[moment])
    app, browser, demo = d6(provider)
    batch, gate = app.state.batch, app.state.live_service.gate
    original = app.state.library.config.db_path
    before = run_dir / "before-the-request.db"
    operation = batch.begin(OperationKind.SEARCH, "synthetic-owner", "synthetic-crash-op")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    copy_library(original, before)

    with gate.task("discovery", "synthetic-owner", provider, sending=Handing(binding)) as sender:
        try:
            sender.generate_text("synthetic decision", max_output_tokens=2_000, json_schema_format=ACTION_FORMAT)
        except (RuntimeError, AIProviderConfigError):
            assert moment in ("unknown", "never_sent")

    # The request's id and reservation were in HOME before it went: one line, whatever came of it.
    noted = [line for line in lines(run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{batch.batch_id}.progress.jsonl") if line["kind"] == "model_request"]
    assert len(noted) == 1 and noted[0]["reserved_micro_usd"] > 0
    used = gate.status()
    d6.close()  # the process ends before the operation does: no Batch.end, no Binding.release

    if moment == "out_at_crash":
        # As the process left it with the request out: the library copied at the moment the provider had it.
        original.write_bytes(out.read_bytes())

    if library == "copied_before":
        original.write_bytes(before.read_bytes())
        # A library from before the request: refused, whatever came of the request, and the batch stops for good.
        with pytest.raises(ConfigError, match="少了.*发出前记下的 1 个请求"):
            restart_on(make_config, clock, ticks, routed, run_dir, original)

        assert json.loads((run_dir / "batch-stop.json").read_text(encoding="utf-8"))["code"] == "ledger"
        return

    if moment in ("unknown", "out_at_crash"):
        # Its cost not known (no answer; or out when the process ended, which the gate finds reserved): the approval
        # is paused, and so the batch stops; nothing gives the request back.
        with pytest.raises(ConfigError, match="暂停"):
            restart_on(make_config, clock, ticks, routed, run_dir, original)

        return

    restarted = restart_on(make_config, clock, ticks, routed, run_dir, original)

    with TestClient(restarted, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        now = restarted.state.live_service.gate.status()

        if moment == "settled":
            assert (now.requests_left, now.micro_usd_left) == (used.requests_left, used.micro_usd_left) == (19, 4_500_000 - 946)
        else:
            # Released as never sent: it counts for nothing, and the restart is as it should be.
            assert now.requests_left == used.requests_left == 20 and now.paused_reason is None


def test_a_crash_before_the_request_is_handed_on_notes_nothing_and_an_earlier_library_may_go_on(d6, run_dir, make_config, clock, ticks, routed):
    from app.discovery.agent import ACTION_FORMAT

    app, browser, demo = d6(Scripted(finish))
    batch, gate = app.state.batch, app.state.live_service.gate
    original = app.state.library.config.db_path
    before = run_dir / "before-the-request.db"
    operation = batch.begin(OperationKind.SEARCH, "synthetic-owner", "synthetic-crash-op")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    copy_library(original, before)

    class Crash(BaseException):
        pass

    def crash():
        # The reservation made, nothing handed on yet, nothing noted: the process ends here.
        raise Crash()

    with pytest.raises(Crash):
        with gate.task("discovery", "synthetic-owner", Scripted(finish), sending=Handing(binding, at_hand_on=crash)) as sender:
            sender.generate_text("synthetic decision", max_output_tokens=2_000, json_schema_format=ACTION_FORMAT)

    progress = run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{batch.batch_id}.progress.jsonl"
    assert [line for line in lines(progress) if line["kind"] == "model_request"] == []
    d6.close()
    # Nothing was handed on, so nothing was noted: a library from before the reservation may go on, its allowance whole.
    original.write_bytes(before.read_bytes())
    restarted = restart_on(make_config, clock, ticks, routed, run_dir, original)

    with TestClient(restarted, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        assert restarted.state.live_service.gate.status().requests_left == 20


@pytest.mark.parametrize("moment", ["settled", "unknown"])
@pytest.mark.parametrize("library", ["as_recorded", "copied_before"])
def test_the_direction_launchs_request_out_at_a_crash_keeps_a_library_without_it_from_the_discovery_launch(make_config, clock, run_dir, routed, ticks, monkeypatch, moment, library):
    """The other model approval of the batch, through the direction pages' own path (the exploration's runner, then
    directions.web._batched: the binding asked before the gate reserves and again right before it hands the request
    on): its one request, the process ending before the exploration's operation does (the operation's end taken away,
    as a crash leaves it); then the discovery launch, on the library as left or on one from before the request."""
    from test_direction_live_pages import Answering, approval as direction_approval, confirmation, material
    from test_direction_pages import review as direction_review, task_of

    approval = direction_approval(approval_id=DIRECTION_APPROVAL, max_requests=1, max_cost_micro_usd=400_000)
    batch = batch_live.load(environ(run_dir), Launch.DIRECTION, approval, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)
    provider = Answering(*(() if moment == "settled" else (RuntimeError("synthetic: handed on, no answer"),)))
    before = run_dir / "before-the-exploration.db"
    # As a crash leaves it: the exploration's operation never ends (neither the binding's release nor the page's end).
    monkeypatch.setattr(batch_live.Batch, "end", lambda self, operation, outcome="done": None)

    with TestClient(
        create_app(in_run_dir(make_config, run_dir), clock=clock, live=LiveSetup(approval, FIXED_RESERVE_V1, lambda: provider), batch=batch),
        base_url=LOCAL_BASE_URL,
        client=("127.0.0.1", 50000),
    ) as client:
        application = client.app
        application.state.directions.executor = InlineExecutor()
        original = application.state.library.config.db_path
        browser = Browser.signed_in(application, "合成用户 A")
        project = material(browser)
        task = task_of(direction_review(browser, [project]))
        copy_library(original, before)
        assert browser.post(f"{task}/confirm", confirmation(browser, task)).status_code == 303

        # One request through the pages' path, noted in HOME before it went; the operation never ended.
        progress = run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{batch.batch_id}.progress.jsonl"
        noted = [line for line in lines(progress) if line["kind"] == "model_request"]
        assert len(provider.calls) == 1 and [line["approval_id"] for line in noted] == [DIRECTION_APPROVAL]
        assert ops(run_dir) == [] and ops(run_dir, "begin") == ["direction"]

    monkeypatch.undo()

    if library == "copied_before":
        original.write_bytes(before.read_bytes())

        # Its request (settled or of unknown cost) noted in HOME before it went, gone from a library from before it.
        with pytest.raises(ConfigError, match=f"少了批准 {DIRECTION_APPROVAL} 发出前记下的 1 个请求"):
            restart_on(make_config, clock, ticks, routed, run_dir, original)

        return

    if moment == "unknown":
        # Its cost unknown, the direction approval paused: the discovery launch stops the batch (one batch, D6 plan 3.2).
        with pytest.raises(ConfigError, match=f"{DIRECTION_APPROVAL} 已经暂停"):
            restart_on(make_config, clock, ticks, routed, run_dir, original)

        return

    # The library as the crash left it: the discovery launch goes on, its own approval whole, the exploration's kept.
    restarted = restart_on(make_config, clock, ticks, routed, run_dir, original)

    with TestClient(restarted, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        assert restarted.state.live_service.gate.status().requests_left == 20
        held = restarted.state.live_service.gate.standing_among([DIRECTION_APPROVAL])[DIRECTION_APPROVAL]
        assert (held.final_requests, held.paused_reason) == (1, None)
