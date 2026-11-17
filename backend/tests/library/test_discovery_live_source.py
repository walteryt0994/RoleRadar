"""U3-D2-E1, offline: Palantir's Lever board as a controlled real source (app.discovery.lever_live).

The real transport (app.discovery.lever_http) is used as it is, but every connection it opens is a socket pair inside
this process (RoutedLever answers as api.lever.co would, from synthetic postings), so nothing leaves the machine. The
admissions and approvals here are a test's own: none of them is the Owner's, and none admits a source of the product.
Each test has its own home beside its run directory (environ), so the ledger of used approvals is a test's own too,
never the real one. Covered: what keeps the real source closed by default and refused when its terms are not right;
nothing read at start or on a page; the approval claimed once, atomically, for one search, by its id: another run
directory, another search, a restart or a race does not use it again, and a stop or a failure never gives it back;
robots first; one budget for every request; the checks right before each request (a cancellation during a wait,
LIVE_STOP, the day, the claim), made again once the transport is made ready, whose time counts against the request
and the window; the stops; a list read in part; another user; the pages telling the real source from the simulated
decisions and analyses; and those still simulated whatever service the app has.
"""
import json
import os
import re
import socket
import threading
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.discovery import lever_live
from app.discovery.agent import SimulatedDecider
from app.discovery.contracts import PostingRef
from app.discovery.lever import HostStopped, LeverBoard, ListingIncomplete, Reply, TransportRefused
from app.discovery.lever_http import LeverHttpTransport
from app.discovery.sources import HeldBack, SourceTimeout, failure_text
from app.discovery.web import DEMO_VARIABLE, SYNTHETIC_SOURCES, live_source_set, sources_from
from app.job_analysis.simulation import SimulatedProvider
from app.library.errors import ConfigError
from app.live_gate import LiveSetup
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed
from lever_helpers import Ticks, posting
from library_helpers import LOCAL_BASE_URL, Browser
from test_discovery_demo_pages import DEMO_PREFERENCES, DEMO_PROJECT, run_demo, text_of
from test_discovery_lever_http import PairedConnection, response
from test_discovery_pages import stored, task_id
from test_live_gate import BOUND, terms as gate_terms

TODAY = date(2026, 11, 12)
BOARD = lever_live.BOARD
ROBOTS = b"User-agent: *\nAllow: /\nCrawl-delay: 1\n"
LIST = "/v0/postings/palantir?mode=json&skip={}&limit=100"
APPROVAL = "u3d2e-src-2026-11-12-a"
# Where a used approval is recorded, under the home the app is started with: the same wherever the run directory is.
LEDGER = Path(".roleradar", "source-approvals")
# What the requests line says of a search with the real source: the simulation counted, the source read for real.
REAL_REQUESTS = "（决策和分析都是本机模拟，没有发给任何模型，不产生模型费用；岗位来自真实来源的受控读取）"
# How the simulated sets name what they show; none of it may name the real source's postings.
SYNTHETIC_WORDS = re.compile(r"合成(样例|数据|来源|示例|预览|回放)")


def palantir_postings() -> list[dict]:
    """300 synthetic postings on the board: still full at the adapter's last page, so read only in part."""
    shown = [posting(number, board=BOARD) for number in (1, 2, 3)]
    rest = [
        posting(number, "Software Engineer", board=BOARD, location="London, United Kingdom", commitment="Full-time",
                team="Engineering", department="Platform")
        for number in range(4, 301)
    ]

    return shown + rest


class RoutedLever:
    """A stand-in for api.lever.co inside this process: each connection opened through opener is one end of a socket
    pair, and the other end answers its one request by its path, from synthetic data. requests keeps each path."""

    def __init__(self, postings=None, *, robots: bytes = ROBOTS, overrides=None):
        self.postings = palantir_postings() if postings is None else postings
        self.robots = robots
        self.overrides = overrides or {}
        self.requests: list[str] = []
        self._lock = threading.Lock()

    def opener(self, timeout: float) -> PairedConnection:
        near, far = socket.socketpair()
        threading.Thread(target=self._serve, args=(far,), daemon=True).start()

        return PairedConnection(near, timeout)

    def _serve(self, far: socket.socket) -> None:
        try:
            data = b""

            while b"\r\n\r\n" not in data:
                chunk = far.recv(4096)

                if not chunk:
                    return

                data += chunk

            path = data.split(b"\r\n", 1)[0].decode("latin-1").split(" ")[1]

            with self._lock:
                self.requests.append(path)

            far.sendall(self._reply(path))
        except OSError:
            return
        finally:
            far.close()

    def _reply(self, path: str) -> bytes:
        if path in self.overrides:
            return self.overrides[path]

        if path == "/robots.txt":
            return response(200, self.robots, content_type="text/plain; charset=utf-8")

        listed = re.fullmatch(r"/v0/postings/palantir\?mode=json&skip=(\d+)&limit=(\d+)", path)

        if listed:
            skip, limit = int(listed[1]), int(listed[2])

            return response(200, json.dumps(self.postings[skip : skip + limit]).encode("utf-8"))

        one = re.fullmatch(r"/v0/postings/palantir/([A-Za-z0-9-]+)", path)
        item = next((item for item in self.postings if one and item["id"] == one[1]), None)

        return response(200, json.dumps(item).encode("utf-8")) if item else response(404, b'{"ok":false}')


def environ(run_dir, day: date = TODAY, **changes) -> dict[str, str]:
    """What the app is started with for the real source: a test's own admission and approval, and a home of the
    test's own beside the run directory (runs side by side share it, as on one machine), never the real one."""
    values = {
        "HOME": str(Path(run_dir).parent / "home"),
        DEMO_VARIABLE: lever_live.KEY,
        "ROLERADAR_SOURCE_ADMISSION_ID": f"u3d2e-admit-{day}-a",
        "ROLERADAR_SOURCE_ADMISSION_UNTIL": str(day + timedelta(days=29)),
        "ROLERADAR_SOURCE_APPROVAL_ID": f"u3d2e-src-{day}-a",
        "ROLERADAR_SOURCE_RUN_DATE": str(day),
        "ROLERADAR_SOURCE_BOARD": "palantir",
        "ROLERADAR_SOURCE_MAX_REQUESTS": "12",
        "ROLERADAR_SOURCE_WINDOW_SECONDS": "600",
        "ROLERADAR_SOURCE_RUN_DIR": str(run_dir),
    }
    values.update({name: value for name, value in changes.items() if value is not None})

    return {name: value for name, value in values.items() if changes.get(name, "") is not None}


def source_terms(run_dir, **changes) -> lever_live.SourceTerms:
    return lever_live.load_terms(environ(run_dir, **changes), today=TODAY)


def ledger_of(run_dir) -> Path:
    return Path(environ(run_dir)["HOME"]) / LEDGER


def claim_record(run_dir, approval: str = APPROVAL) -> dict:
    return json.loads((ledger_of(run_dir) / f"{approval}.claim").read_text(encoding="utf-8"))


def another_run_dir(run_dir) -> Path:
    """A second run directory beside the first: the same machine, the same home."""
    folder = run_dir.parent / "another-run"
    folder.mkdir()

    return folder


class Handed:
    """The transport behind a search's guard, in place of the real one: keeps what it was handed (address and time),
    answers robots as recorded and every list with an empty page."""

    def __init__(self):
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, timeout: float) -> Reply:
        self.calls.append((url, timeout))

        if url == lever_live.ROBOTS_URL:
            return Reply(200, "text/plain", ROBOTS)

        return Reply(200, "application/json", b"[]")


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


def controlled(run_dir, routed, ticks, *, task=None, today=lambda: TODAY, sleep=None, **changes):
    task = task or SimpleNamespace(id="task-1", cancel=threading.Event())
    register = lever_live.register(
        source_terms(run_dir, **changes), task, create=lambda: LeverHttpTransport(opener=routed.opener), today=today,
        monotonic=ticks.monotonic, sleep=sleep or ticks.sleep,
    )

    return register.adapter(BOARD.source_id), task


# --- closed unless its terms are right -----------------------------------------------------------------


def test_the_default_stays_synthetic_and_the_lever_register_unverified():
    from app.discovery import lever

    assert sources_from({}) is SYNTHETIC_SOURCES
    assert lever.register((BOARD,)).refusal(BOARD.source_id)


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"ROLERADAR_SOURCE_APPROVAL_ID": None}, "缺少 ROLERADAR_SOURCE_APPROVAL_ID"),
        ({"ROLERADAR_SOURCE_ADMISSION_ID": "palantir-ok"}, "应写成 u3d2e-admit-"),
        ({"ROLERADAR_SOURCE_ADMISSION_UNTIL": "2027-01-31"}, "最长 31 天"),
        ({"ROLERADAR_SOURCE_ADMISSION_ID": "u3d2e-admit-2026-11-13-a"}, "不能晚于今天开始"),
        ({"ROLERADAR_SOURCE_APPROVAL_ID": "u3d2e-src-2026-11-11-a"}, "日期和运行日不一致"),
        ({"ROLERADAR_SOURCE_APPROVAL_ID": "u3d2e-src-2026-11-11-a", "ROLERADAR_SOURCE_RUN_DATE": "2026-11-11"}, "只在 2026-11-11"),
        ({"ROLERADAR_SOURCE_BOARD": "zilliz"}, "只能读取 palantir"),
        ({"ROLERADAR_SOURCE_MAX_REQUESTS": "13"}, "应在 1 到 12 之间"),
        ({"ROLERADAR_SOURCE_WINDOW_SECONDS": "601"}, "应在 1.0 到 600.0 之间"),
        ({"ROLERADAR_SOURCE_RUN_DIR": "run"}, "绝对路径"),
        ({"ROLERADAR_SOURCE_RUN_DIR": str(lever_live.REPO / "backend")}, "不能放在仓库里"),
        ({"HOME": None}, "缺少 HOME"),
        ({"HOME": "home"}, "HOME 要写绝对路径"),
    ],
)
def test_terms_not_as_they_must_be_stop_the_app_before_anything_is_created(make_config, clock, run_dir, changes, problem):
    with pytest.raises(lever_live.SourceTermsError, match=problem):
        source_terms(run_dir, **changes)

    config = make_config()

    with pytest.raises(ConfigError):
        create_app(config, clock=clock, environ=environ(run_dir, **changes))

    # Refused, never run as another set; nothing created.
    assert not config.db_path.exists()


@pytest.mark.parametrize("restart", ["same_run_directory", "another_run_directory"])
def test_an_approval_claimed_before_stops_the_app_from_starting_again(make_config, clock, run_dir, restart):
    # Today's own dates: the app reads the day itself at start.
    today = lever_live.today_here()
    terms = lever_live.load_terms(environ(run_dir, today), today=today)
    lever_live.claim(terms, "an-earlier-search")
    folder = run_dir if restart == "same_run_directory" else another_run_dir(run_dir)

    # The approval is used by its id: a new run directory does not make it unused.
    with pytest.raises(lever_live.SourceTermsError, match=f"读取批准 u3d2e-src-{today}-a 已经用过"):
        lever_live.load_terms(environ(folder, today), today=today)

    with pytest.raises(ConfigError, match="已经用过"):
        create_app(make_config(), clock=clock, environ=environ(folder, today))

    # Its one record stays as the earlier search made it; nothing is written for the new start.
    assert claim_record(run_dir, f"u3d2e-src-{today}-a")["task_id"] == "an-earlier-search"
    assert [path.name for path in ledger_of(run_dir).iterdir()] == [f"u3d2e-src-{today}-a.claim"]
    assert not any(folder.iterdir())


# --- nothing read at start or on a page; one claim, one search ----------------------------------------


@pytest.fixture
def app(make_config, clock, run_dir, routed, ticks):
    sources = live_source_set(
        source_terms(run_dir), create=lambda: LeverHttpTransport(opener=routed.opener), today=lambda: TODAY,
        monotonic=ticks.monotonic, sleep=ticks.sleep,
    )
    application = create_app(make_config(), clock=clock, discovery_sources=sources)

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


def test_a_search_reads_the_real_source_once_confirmed_and_says_what_it_read(app, browser_a, browser_b, searches, demo, routed, run_dir):
    assert browser_a.get("/discover").status_code == 200 and routed.requests == []
    url = run_demo(browser_a, searches, demo)
    page = browser_a.get(url).text
    shown = text_of(page)
    record = claim_record(run_dir)
    outcome = stored(app, browser_a, url).finished.outcome

    # robots first, then the list's three pages, all within one budget of 12.
    assert routed.requests[:4] == ["/robots.txt", LIST.format(0), LIST.format(100), LIST.format(200)]
    assert len(routed.requests) <= 12
    # The approval claimed by this very search, by its id in the ledger, with its terms' digest and run directory;
    # nothing is written in the run directory.
    assert (record["approval_id"], record["task_id"], record["run_dir"]) == (APPROVAL, task_id(url), str(run_dir))
    terms = lever_live.SourceTerms(
        "u3d2e-admit-2026-11-12-a", TODAY + timedelta(days=29), APPROVAL, TODAY, 12, 600.0, run_dir.resolve(),
        ledger_of(run_dir).resolve(),
    )
    # The run directory holds the request record (U3-D2-E2) and the analyses' notes (U3-D2-E3), nothing else.
    assert record["digest"] == terms.digest
    assert sorted(path.name for path in run_dir.iterdir()) == sorted([lever_live.REQUEST_LOG, lever_live.ANALYSIS_LOG])
    # Read in part and said so; the page says it read a real source, with decisions and analyses simulated.
    assert [(read.source, read.read) for read in outcome.partial] == [(lever_live.NAME, 300)]
    assert f"{lever_live.NAME}的职位列表只读了前 300 个，没有读完" in shown
    assert "真实来源受控只读" in shown and "离线演示" not in shown
    assert "（真实岗位地址，页面不提供链接）" in shown and "来源在这个时间回答：核对时仍公开发布，不代表企业还有名额" in shown
    assert not re.search(r'href="https?://(?:jobs|api)\.lever\.co', page)
    # The real source is never called a synthetic sample, and nothing says the search stayed offline; the simulated
    # requests are said to be decisions and analyses only, sent to no model, here and on the running page's line.
    assert "证据 官方原始发布" in " ".join(shown.split()) and not SYNTHETIC_WORDS.search(shown)
    assert "本机模拟服务处理的请求：" in shown and REAL_REQUESTS in shown and "没有联网" not in shown
    assert REAL_REQUESTS in browser_a.get(f"{url}/status").json()["status"]
    # Another user opens nothing of it.
    assert browser_b.get(url).status_code == 404


def test_the_review_and_a_refresh_read_nothing(app, browser_a, searches, demo, routed, run_dir):
    from test_discovery_pages import review, search_of

    url = search_of(
        review(browser_a, sources=(demo,), use_preferences=True, targets="Data Analyst Intern", city_others="excluded",
               job_type_others="excluded", direction_others="included")
    )

    for _ in range(2):
        shown = text_of(browser_a.get(url).text)

    assert "读取批准 u3d2e-src-2026-11-12-a" in shown and "确认之前什么都不读取" in shown
    assert routed.requests == [] and not any(run_dir.iterdir()) and not ledger_of(run_dir).exists()
    # The source is named as the real one it is, never as a synthetic sample.
    assert f"{lever_live.NAME}（官方原始发布）" in shown and not SYNTHETIC_WORDS.search(shown)


def test_a_second_search_with_the_same_approval_sends_nothing(app, browser_a, searches, demo, routed, run_dir):
    run_demo(browser_a, searches, demo)
    sent = list(routed.requests)
    second = run_demo(browser_a, searches, demo)
    outcome = stored(app, browser_a, second).finished.outcome

    assert routed.requests == sent and outcome.partial == () and not outcome.results
    assert claim_record(run_dir)["task_id"] != task_id(second)


def test_an_approval_one_search_used_sends_nothing_for_another_from_another_run_directory(run_dir, routed):
    # Two apps started with the same approval before either searched, from two run directories.
    first, second = source_terms(run_dir), source_terms(another_run_dir(run_dir))
    make = lambda: LeverHttpTransport(opener=routed.opener)  # noqa: E731
    assert lever_live.ControlledTransport(first, "task-1", create=make, today=lambda: TODAY).get(lever_live.ROBOTS_URL, 5.0).status == 200
    late = lever_live.ControlledTransport(second, "task-2", create=make, today=lambda: TODAY)

    with pytest.raises(TransportRefused, match="已经被占用"):
        late.get(lever_live.ROBOTS_URL, 5.0)

    assert routed.requests == ["/robots.txt"] and not late.created
    assert claim_record(run_dir)["task_id"] == "task-1"


@pytest.mark.parametrize("folders", ["one_run_directory", "two_run_directories"])
def test_two_searches_racing_for_one_approval_claim_it_once(run_dir, routed, ticks, folders):
    other = run_dir if folders == "one_run_directory" else another_run_dir(run_dir)
    terms = (source_terms(run_dir), source_terms(other))
    transports = [
        lever_live.ControlledTransport(terms[number - 1], f"task-{number}", create=lambda: LeverHttpTransport(opener=routed.opener), today=lambda: TODAY)
        for number in (1, 2)
    ]
    outcomes, start = [], threading.Barrier(2)

    def go(transport):
        start.wait()

        try:
            outcomes.append(transport.get(lever_live.ROBOTS_URL, 5.0).status)
        except TransportRefused as refused:
            outcomes.append(str(refused))

    threads = [threading.Thread(target=go, args=(transport,)) for transport in transports]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join(10)

    assert sorted(map(str, outcomes)) == ["200", "读取批准 u3d2e-src-2026-11-12-a 已经被占用，没有发出"]
    assert routed.requests == ["/robots.txt"]
    # One use, the winner's; the loser's request is on record as kept back, wherever its run directory is.
    assert [path.name for path in ledger_of(run_dir).iterdir()] == [f"{APPROVAL}.claim"]
    winner = claim_record(run_dir)["task_id"]
    lines = logged(run_dir) + (logged(other) if other != run_dir else [])
    assert [line["task_id"] for line in lines if line["state"] == "handed"] == [winner]
    assert [(line["task_id"], line["reason"]) for line in lines if line["state"] == "refused"] == [
        ({"task-1": "task-2", "task-2": "task-1"}[winner], "claimed")
    ]


# --- checks right before each request; stops; one budget ---------------------------------------------


def test_a_cancellation_during_the_wait_keeps_the_request_from_leaving(run_dir, routed, ticks):
    task = SimpleNamespace(id="task-1", cancel=threading.Event())

    def sleep(seconds):
        ticks.sleep(seconds)
        task.cancel.set()

    source, _ = controlled(run_dir, routed, ticks, task=task, sleep=sleep)

    with pytest.raises(Exception, match="查找已经停止"):
        source.listing(60.0)

    assert routed.requests == ["/robots.txt"]


@pytest.mark.parametrize("stop", ["live_stop", "next_day"])
def test_a_stop_file_or_a_new_day_keeps_the_next_request_from_leaving(run_dir, routed, ticks, stop):
    # The stop comes during the wait before the second request, however often the day is read.
    day = [TODAY]

    def sleep(seconds):
        ticks.sleep(seconds)

        if stop == "live_stop":
            (run_dir / lever_live.STOP_FILE).write_text("stop", encoding="utf-8")
        else:
            day[0] = TODAY + timedelta(days=1)

    source, _ = controlled(run_dir, routed, ticks, today=lambda: day[0], sleep=sleep)

    with pytest.raises(Exception, match="LIVE_STOP" if stop == "live_stop" else "只在 2026-11-12"):
        source.listing(60.0)

    assert routed.requests == ["/robots.txt"]


@pytest.mark.parametrize(
    "stop, refused, said",
    [
        ("cancel", HeldBack, "查找已经停止"),
        ("live_stop", HeldBack, "LIVE_STOP"),
        ("next_day", HeldBack, "只在 2026-11-12"),
        ("slow", HeldBack, "准备发出用掉了这次请求的时间"),
        ("window", HostStopped, "时间窗口到了"),
    ],
)
def test_a_stop_while_the_first_request_is_made_ready_keeps_it_back(run_dir, ticks, stop, refused, said):
    task, day, handed, made = SimpleNamespace(id="task-1", cancel=threading.Event()), [TODAY], Handed(), []

    def make():
        # The stop comes while the transport is made ready: after the check made before it.
        made.append(ticks.now)

        if stop == "cancel":
            task.cancel.set()
        elif stop == "live_stop":
            (run_dir / lever_live.STOP_FILE).write_text("stop", encoding="utf-8")
        elif stop == "next_day":
            day[0] = TODAY + timedelta(days=1)
        else:
            # Its time counts: 14 seconds leave too little of the request's 15; 601 outlast the window's 600.
            ticks.now += 14 if stop == "slow" else 601

        return handed

    source = lever_live.register(
        source_terms(run_dir), task, create=make, today=lambda: day[0], monotonic=ticks.monotonic, sleep=ticks.sleep
    ).adapter(BOARD.source_id)

    with pytest.raises(refused, match=said):
        source.listing(60.0)

    with pytest.raises(lever_live.SourceHalted):
        source.listing(60.0)

    # Made ready once, and nothing handed to it, then or after; nothing counted as sent.
    assert len(made) == 1 and handed.calls == [] and source.guard.sent == 0
    # The approval stays used: a stop never gives it back, from this run directory or another.
    assert claim_record(run_dir)["task_id"] == "task-1"

    for folder in (run_dir, another_run_dir(run_dir)):
        with pytest.raises(lever_live.SourceTermsError, match="已经用过"):
            source_terms(folder)


def test_the_time_making_the_transport_ready_takes_counts_against_its_first_request(run_dir, ticks):
    handed = Handed()

    def make():
        ticks.now += 5

        return handed

    task = SimpleNamespace(id="task-1", cancel=threading.Event())
    source = lever_live.register(
        source_terms(run_dir), task, create=make, today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep
    ).adapter(BOARD.source_id)
    window_ends = source.guard.window_ends

    assert source.listing(60.0).complete
    # robots is handed what is left of its 15 seconds once 5 went on making the transport ready; the list, sent
    # later, its whole time. The window ends where it did: making ready never restarts any timing.
    assert handed.calls == [(lever_live.ROBOTS_URL, 10.0), (f"https://api.lever.co{LIST.format(0)}", 15.0)]
    assert source.guard.window_ends == window_ends == 100.0 + 600


def test_robots_and_every_page_share_one_budget(run_dir, routed, ticks):
    source, _ = controlled(run_dir, routed, ticks, ROLERADAR_SOURCE_MAX_REQUESTS="3")

    with pytest.raises(HostStopped, match="请求次数已用完（3 次）"):
        source.listing(60.0)

    assert routed.requests == ["/robots.txt", LIST.format(0), LIST.format(100)]


@pytest.mark.parametrize(
    "overrides, robots, stop",
    [
        ({}, b"User-agent: *\nDisallow: /\n", "robots 规则和 2026-11-10 核对时不同"),
        ({"/robots.txt": response(302, b"", headers=("Location: https://example.invalid/",))}, ROBOTS, "robots 回应 302"),
        ({LIST.format(0): response(301, b"", headers=("Location: https://example.invalid/",))}, ROBOTS, "301"),
        ({LIST.format(0): response(429, b"{}")}, ROBOTS, "要求登录、付费或限流"),
    ],
    ids=["robots_changed", "robots_redirect", "list_redirect", "too_many_requests"],
)
def test_a_stop_condition_stops_the_source_and_nothing_is_sent_after(run_dir, ticks, overrides, robots, stop):
    routed = RoutedLever(robots=robots, overrides=overrides)
    source, _ = controlled(run_dir, routed, ticks)

    with pytest.raises(Exception, match=stop):
        source.listing(60.0)

    sent = list(routed.requests)

    with pytest.raises(lever_live.SourceHalted):
        source.listing(60.0)

    # Robots changed or redirected: no posting asked for. After any stop, nothing more is sent.
    assert routed.requests == sent
    assert (LIST.format(0) in sent) is (stop in ("301", "要求登录、付费或限流"))


def test_the_window_runs_from_the_confirmed_search_not_from_the_start(run_dir, routed, ticks):
    terms = source_terms(run_dir)
    sources = live_source_set(terms, create=lambda: LeverHttpTransport(opener=routed.opener), today=lambda: TODAY,
                              monotonic=ticks.monotonic, sleep=ticks.sleep)
    ticks.now += 5_000
    register = sources.make_register(SimpleNamespace(id="task-1", cancel=threading.Event()))

    assert register.adapter(BOARD.source_id).guard.window_ends == ticks.now + 600
    assert routed.requests == [] and not register.adapter(BOARD.source_id)._transport.created


# --- D2-E2: every request recorded, metadata only; failures told as they were ---------------------------


def logged(run_dir) -> list[dict]:
    return [json.loads(line) for line in (run_dir / lever_live.REQUEST_LOG).read_text(encoding="utf-8").splitlines()]


def test_every_request_handed_on_is_recorded_with_its_metadata_and_nothing_of_a_posting(run_dir, routed, ticks):
    source, _ = controlled(run_dir, routed, ticks)

    with pytest.raises(ListingIncomplete):
        source.listing(60.0)

    ref = PostingRef(BOARD.source_id, routed.postings[0]["id"])
    source.read(ref, 60.0)
    source.check(ref, 60.0)
    claimed, *rest = logged(run_dir)
    handed = [line for line in rest if line["state"] == "handed"]
    answered = [line for line in rest if line["state"] == "answered"]

    # Claimed before the first request, by this search; then each request handed on, then what came back.
    assert (claimed["state"], claimed["task_id"], claimed["approval_id"]) == ("claimed", "task-1", APPROVAL)
    assert [(line["call"], line["kind"], line["page"]) for line in handed] == [
        ("robots", "robots", None), ("listing", "list", 1), ("listing", "list", 2), ("listing", "list", 3),
        ("read", "posting", None), ("check", "posting", None),
    ]
    assert len(handed) == len(routed.requests) == 6 and {line["task_id"] for line in rest} == {"task-1"}
    assert [line["seq"] for line in answered] == [line["seq"] for line in handed] == [1, 2, 3, 4, 5, 6]
    assert all(line["outcome"] == "200" and line["bytes"] > 0 and line["seconds"] >= 0 for line in answered)
    # Spacing, each request's time and the window, from the record alone: the pre-send records when written and
    # with what time planned; the results when each went in fact and with what time: 2 s apart, 15 s each.
    assert [(line["since_confirm"], line["planned_seconds"]) for line in handed] == [(t, 15.0) for t in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0)]
    assert [(line["sent_since_confirm"], line["given_seconds"]) for line in answered] == [(t, 15.0) for t in (0.0, 2.0, 4.0, 6.0, 8.0, 10.0)]
    # Nothing of a posting and no address: kinds and pages only.
    text = (run_dir / lever_live.REQUEST_LOG).read_text(encoding="utf-8")
    assert "http" not in text and "palantir/" not in text and ref.posting_id not in text


@pytest.mark.parametrize("stop, code", [("cancel", "cancelled"), ("live_stop", "live_stop"), ("next_day", "not_today")])
def test_a_request_kept_back_is_recorded_as_kept_back_with_why(run_dir, routed, ticks, stop, code):
    task, day = SimpleNamespace(id="task-1", cancel=threading.Event()), [TODAY]

    def sleep(seconds):
        ticks.sleep(seconds)

        if stop == "cancel":
            task.cancel.set()
        elif stop == "live_stop":
            (run_dir / lever_live.STOP_FILE).write_text("stop", encoding="utf-8")
        else:
            day[0] = TODAY + timedelta(days=1)

    source, _ = controlled(run_dir, routed, ticks, task=task, today=lambda: day[0], sleep=sleep)

    with pytest.raises(Exception):
        source.listing(60.0)

    states = [(line["state"], line.get("kind"), line.get("page"), line.get("reason")) for line in logged(run_dir)]

    assert states == [
        ("claimed", None, None, None), ("handed", "robots", None, None), ("answered", None, None, None),
        ("refused", "list", 1, code),
    ]
    assert routed.requests == ["/robots.txt"]


def test_a_record_that_cannot_be_written_keeps_every_request_back(run_dir, routed, ticks):
    (run_dir / lever_live.REQUEST_LOG).mkdir()
    source, _ = controlled(run_dir, routed, ticks)

    with pytest.raises(Exception, match="请求记录写不进去"):
        source.listing(60.0)

    assert routed.requests == []


def test_once_a_result_cannot_be_recorded_nothing_more_is_sent(run_dir, ticks):
    handed = Handed()
    log, moved = run_dir / lever_live.REQUEST_LOG, run_dir / "requests.moved"

    class Swapping:
        def get(self, url, timeout):
            reply = handed.get(url, timeout)
            # The record cannot be written while this request is out: what came of it is not written.
            log.rename(moved)
            log.mkdir()

            return reply

    def sleep(seconds):
        ticks.sleep(seconds)

        # And it can be written again before the next request: that one is still not sent.
        if log.is_dir():
            log.rmdir()
            moved.rename(log)

    source = lever_live.register(
        source_terms(run_dir), SimpleNamespace(id="task-1", cancel=threading.Event()), create=Swapping,
        today=lambda: TODAY, monotonic=ticks.monotonic, sleep=sleep,
    ).adapter(BOARD.source_id)

    with pytest.raises(Exception, match="请求记录写不进去"):
        source.listing(60.0)

    assert [url for url, _ in handed.calls] == [lever_live.ROBOTS_URL]
    # On record: robots handed on with no outcome (not known, never "did not happen"), then the next one kept back.
    assert [(line["state"], line.get("kind"), line.get("reason")) for line in logged(run_dir)] == [
        ("claimed", None, None), ("handed", "robots", None), ("refused", "list", "unrecorded"),
    ]


# --- D2-E2 R1: checked again once the pre-send record is down; every line written whole -----------------


def watched(run_dir, ticks, *, today=lambda: TODAY):
    """The board through its guard on made-up ticks, its transport a Handed one: what it was handed, with what time."""
    handed, task = Handed(), SimpleNamespace(id="task-1", cancel=threading.Event())
    source = lever_live.register(source_terms(run_dir), task, create=lambda: handed, today=today,
                                 monotonic=ticks.monotonic, sleep=ticks.sleep).adapter(BOARD.source_id)

    return source, handed, task


@pytest.mark.parametrize(
    "stop, code, raised",
    [
        ("cancel", "cancelled", HeldBack),
        ("live_stop", "live_stop", HeldBack),
        ("next_day", "not_today", HeldBack),
        ("window", "late", HostStopped),
        ("too_slow", "late", HeldBack),
    ],
)
def test_a_stop_while_the_pre_send_record_is_written_keeps_the_request_back(run_dir, ticks, monkeypatch, stop, code, raised):
    day = [TODAY]
    source, handed, task = watched(run_dir, ticks, today=lambda: day[0])
    write = lever_live.ControlledTransport._write

    def writing(self, line):
        write(self, line)

        # The first request's pre-send record is down; the stop came while it was being written.
        if line["state"] == "handed" and line["seq"] == 1:
            if stop == "cancel":
                task.cancel.set()
            elif stop == "live_stop":
                (run_dir / lever_live.STOP_FILE).write_text("stop", encoding="utf-8")
            elif stop == "next_day":
                day[0] = TODAY + timedelta(days=1)
            else:
                # Its time counts: 601 s outlast the window, 14 s leave too little of the request's 15.
                ticks.now += 601 if stop == "window" else 14

    monkeypatch.setattr(lever_live.ControlledTransport, "_write", writing)

    with pytest.raises(raised):
        source.listing(60.0)

    # Nothing handed to the transport; the pre-send record is followed by one saying it was not sent, and why.
    assert handed.calls == []
    assert [(line["state"], line.get("reason")) for line in logged(run_dir)] == [
        ("claimed", None), ("handed", None), ("withdrawn", code),
    ]


def test_the_time_the_pre_send_record_takes_is_taken_from_the_request_and_both_times_are_recorded(run_dir, ticks, monkeypatch):
    source, handed, _ = watched(run_dir, ticks)
    write = lever_live.ControlledTransport._write

    def slow(self, line):
        write(self, line)

        if line["state"] == "handed":
            ticks.now += 5

    monkeypatch.setattr(lever_live.ControlledTransport, "_write", slow)

    assert source.listing(60.0).complete
    lines = logged(run_dir)

    # 5 s of writing come out of each request's 15, never restarting its time: sent 5 s later, with 10.
    assert [timeout for _, timeout in handed.calls] == [10.0, 10.0]
    # The pre-send record says when it was written and what was planned; the result, when it went and with what.
    assert [(line["since_confirm"], line["planned_seconds"]) for line in lines if line["state"] == "handed"] == [(0.0, 15.0), (7.0, 15.0)]
    assert [(line["sent_since_confirm"], line["given_seconds"]) for line in lines if line["state"] == "answered"] == [(5.0, 10.0), (12.0, 10.0)]


@pytest.mark.parametrize("state", ["handed", "answered"])
def test_a_short_write_is_completed_into_one_whole_line(run_dir, ticks, monkeypatch, state):
    source, handed, _ = watched(run_dir, ticks)
    write, short = os.write, []

    def halves(fd, payload):
        if f'"state": "{state}"'.encode() in payload:
            short.append(len(payload))

            return write(fd, payload[: len(payload) // 2])

        return write(fd, payload)

    monkeypatch.setattr(lever_live.os, "write", halves)

    assert source.listing(60.0).complete
    # Every line whole (logged reads each as JSON), and as many pre-send records as requests handed on.
    states = [line["state"] for line in logged(run_dir)]
    assert short and states.count("handed") == len(handed.calls) == 2 and states.count("answered") == 2


@pytest.mark.parametrize("state", ["handed", "answered"])
@pytest.mark.parametrize("failing", ["no_progress", "half_then_error"])
def test_a_line_that_cannot_be_written_whole_is_not_left_behind_or_taken_as_written(run_dir, ticks, monkeypatch, state, failing):
    source, handed, _ = watched(run_dir, ticks)
    write, trap = os.write, {"armed": False, "sprung": False}

    def failing_write(fd, payload):
        if trap["armed"]:
            trap["armed"] = False

            raise OSError("the disk went away")

        if f'"state": "{state}"'.encode() in payload and not trap["sprung"]:
            trap["sprung"] = True

            if failing == "no_progress":
                return 0

            trap["armed"] = True

            return write(fd, payload[: len(payload) // 2])

        return write(fd, payload)

    monkeypatch.setattr(lever_live.os, "write", failing_write)

    with pytest.raises(HeldBack, match="请求记录写不进去"):
        source.listing(60.0)

    # Nothing half-written is left (logged reads every line as JSON) and nothing goes out on a record not kept.
    states = [(line["state"], line.get("kind"), line.get("reason")) for line in logged(run_dir)]

    if state == "handed":
        assert handed.calls == [] and states == [("claimed", None, None), ("refused", "robots", "unrecorded")]
    else:
        # The request went; what came of it could not be kept: its outcome is unknown, and nothing more goes.
        assert [url for url, _ in handed.calls] == [lever_live.ROBOTS_URL]
        assert states == [("claimed", None, None), ("handed", "robots", None), ("refused", "list", "unrecorded")]


def real_app(make_config, clock, run_dir, routed, ticks):
    sources = live_source_set(source_terms(run_dir), create=lambda: LeverHttpTransport(opener=routed.opener),
                              today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)

    return create_app(make_config(), clock=clock, discovery_sources=sources)


class StopAfterRobots(RoutedLever):
    """Makes LIVE_STOP in the run directory while it answers robots: the next request is kept back."""

    def __init__(self, run_dir, **kwargs):
        super().__init__(**kwargs)
        self.run_dir = run_dir

    def _reply(self, path):
        if path == "/robots.txt":
            (self.run_dir / lever_live.STOP_FILE).write_text("stop", encoding="utf-8")

        return super()._reply(path)


FALSE_WORDS = ("来源没有按时回答", "返回了身份或格式不对的内容")


@pytest.mark.parametrize("case", ["live_stop", "approval_used", "too_many_requests", "robots_changed"])
def test_a_failure_is_told_as_it_was_on_the_page(make_config, clock, run_dir, ticks, case):
    routed = {
        "live_stop": lambda: StopAfterRobots(run_dir),
        "too_many_requests": lambda: RoutedLever(overrides={LIST.format(0): response(429, b"{}")}),
        "robots_changed": lambda: RoutedLever(robots=b"User-agent: *\nDisallow: /\n"),
    }.get(case, RoutedLever)()
    application = real_app(make_config, clock, run_dir, routed, ticks)
    said = {
        "live_stop": "没有完成（传输层没有发出这个请求：运行目录里有 LIVE_STOP，没有发出）",
        "approval_used": f"没有完成（传输层没有发出这个请求：读取批准 {APPROVAL} 已经被占用，没有发出）",
        "too_many_requests": "没有完成（来源回应 429（要求登录、付费或限流），之后不再向它发请求）",
        "robots_changed": "没有完成（robots 规则和 2026-11-10 核对时不同，不发岗位请求）",
    }[case]

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        searches = application.state.discovery
        searches.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
        demo = confirmed(browser, **DEMO_PROJECT)

        if case == "approval_used":
            run_demo(browser, searches, demo)

        sent = len(routed.requests)
        url = run_demo(browser, searches, demo)
        shown = " ".join(text_of(browser.get(url).text).split())

    # What the source did, or that nothing was sent, said as it was; never "late" or "answered wrongly" for it.
    assert said in shown, shown[-1500:]
    assert not any(word in shown for word in FALSE_WORDS)
    assert len(routed.requests) - sent == {"live_stop": 1, "approval_used": 0, "too_many_requests": 2, "robots_changed": 1}[case]

    if case in ("live_stop", "approval_used"):
        # Kept back before it left: told as not sent, never as something the source returned.
        assert "发送前停下，没有发出请求" in shown and "返回了不符合要求的内容" not in shown


def test_a_failure_is_told_in_fixed_words_never_in_a_source_s_own():
    from app.discovery import lever, presentation
    from app.discovery.agent import TraceStep
    from app.discovery.contracts import Dimension, Outcome, SearchRecord, Tool

    # Our own fixed words are shown as they are; anything else by its kind, never a source's own message.
    assert failure_text(SourceTimeout("the synthetic site never answers a check")) == "来源没有在时限内回答"
    assert failure_text(HeldBack("not ours")) == "发送前停下，没有发出请求"
    assert failure_text(lever.TimeUsedUp("等待间隔以后时间不够了，没有发出")) == "等待间隔以后时间不够了，没有发出"
    assert failure_text(lever.HostStopped("请求次数已用完（12 次），没有发出")) == "请求次数已用完（12 次），没有发出"
    halted = lever_live.SourceHalted("这个来源这次查找已经停止（运行目录里有 LIVE_STOP，没有发出），之后不再发请求")
    assert failure_text(halted) == str(halted) and isinstance(halted, HeldBack)
    # A failed search and a failed step say "not done" and why, after what the step told the next decision.
    record = SearchRecord(BOARD.source_id, "Software Engineer", ((Dimension.CITY, "New York"),), 0, Outcome.FAILED,
                          failure="来源没有在时限内回答")
    assert presentation.search_line(record, {}).endswith(" · 没有完成（来源没有在时限内回答）")
    step = TraceStep(1, Tool.CHECK, (), Outcome.FAILED.value, "来源没有回答：状态待确认", "", "SourceTimeout", "来源没有在时限内回答")
    assert presentation.step_view(step)["title"] == "第 1 步 · 核对是否仍公开发布 · 没有完成"
    assert presentation.step_view(step)["summary"] == "来源没有回答：状态待确认 原因：来源没有在时限内回答"


# --- decisions and analyses stay simulated -------------------------------------------------------------


def test_a_real_model_service_the_app_has_is_never_reached_from_a_real_source_search(make_config, clock, run_dir, routed, ticks):
    built = []

    def make():
        built.append("built")
        raise AssertionError("a search reached the real service")

    setup = LiveSetup(gate_terms(purposes=frozenset({"analysis", "discovery"})), BOUND, make)
    sources = live_source_set(source_terms(run_dir), create=lambda: LeverHttpTransport(opener=routed.opener),
                              today=lambda: TODAY, monotonic=ticks.monotonic, sleep=ticks.sleep)
    application = create_app(make_config(), clock=clock, live=setup, discovery_sources=sources)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        searches = application.state.discovery
        searches.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **DEMO_PREFERENCES).status_code == 303
        url = run_demo(browser, searches, confirmed(browser, **DEMO_PROJECT))
        outcome = stored(application, browser, url).finished.outcome

        assert isinstance(searches.decider_for(None), SimulatedDecider)
        assert isinstance(searches.make_provider(SimpleNamespace(cancel=None)), SimulatedProvider)
        assert built == [] and application.state.job_analysis.live.gate.status().requests_used == 0
        assert outcome.partial and routed.requests[0] == "/robots.txt"
