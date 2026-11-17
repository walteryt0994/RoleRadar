"""U3-D2, offline and ahead of its batch: the Lever Postings API as a source, over synthetic replies only.

The adapter (app.discovery.lever) is tested on its own, then through the A1 tools and the A2 agent, both
unchanged. Every reply is made up in the shape Lever documents (read 2026-11-11), nothing is sent anywhere, and
the registers here are verified only as a test verifies them: none of this admits a source of the product or
turns real access on.
"""
import ast
import socket
import urllib.parse
from pathlib import Path

import pytest

from app.discovery import lever, rules
from app.discovery.agent import ScriptedDecider, make_action, run_agent
from app.discovery.contracts import (
    Category,
    CheckStatus,
    EndReason,
    EvidenceLevel,
    Outcome,
    Posting,
    PostingRef,
    Screen,
    Tool,
)
from app.discovery.lever import (
    MAX_BYTES,
    MAX_PAGES,
    MIN_INTERVAL,
    PAGE_SIZE,
    REQUEST_SECONDS,
    BadReply,
    HostStopped,
    IdentityMismatch,
    LeverBoard,
    LeverSource,
    ListingIncomplete,
    NetworkClosed,
    NotFound,
    Reply,
    TransportTimeout,
)
from app.discovery.sources import Permission, Query, SourceError, SourceTimeout
from app.discovery.tools import PLAIN_ID, check_posting, finish, search_postings
from discovery_helpers import begin, confirmation_for
from job_helpers import RecordingProvider
from lever_helpers import (
    DEMO_SCOPE,
    DESCRIPTION,
    HARBOR,
    JSON_TYPE,
    LABEL,
    NORTH,
    SALES,
    ReplayTransport,
    Ticks,
    detail_url,
    grants,
    guard,
    list_url,
    make_demo_library,
    ok,
    pid,
    posting,
    serve_details,
    serve_listing,
    verified_register,
)

NETWORK_CLIENTS = {"socket", "ssl", "http", "urllib", "requests", "httpx", "httpx2", "aiohttp", "openai", "os", "subprocess"}


@pytest.fixture
def ticks():
    return Ticks()


@pytest.fixture
def transport(ticks):
    return ReplayTransport(ticks)


@pytest.fixture
def host(ticks):
    return guard(ticks)


@pytest.fixture
def north(transport, host):
    return LeverSource(NORTH, transport=transport, guard=host)


@pytest.fixture
def demo(database, owners, clock):
    return make_demo_library(database, owners, clock)


def ref(number: int, board: LeverBoard = NORTH) -> PostingRef:
    return PostingRef(board.source_id, pid(number, board))


def query(words: str = "Data", city: str | None = None, job_type: str | None = None) -> Query:
    return Query(words, city, job_type, None)


def ids(found) -> list[str]:
    return [item.ref.posting_id for item in found]


def demo_run(database, clock, demo, transport, host, provider=None):
    confirmation = confirmation_for(demo, scope=DEMO_SCOPE, sources=grants(), preferences=(SALES,))

    return begin(database, clock, confirmation, provider, register=verified_register(transport, host))


def demo_postings() -> list[dict]:
    """The northern board of the demo: interns and others, in scope, outside it and not plainly anywhere."""
    return [
        posting(1),
        posting(2, location="New York, NY"),
        posting(3, "Senior Data Analyst", commitment="Full-time"),
        posting(4, location="Toronto, Ontario, Canada"),
        posting(5, location="Remote - US", workplace="remote"),
        posting(6, "International Marketing Coordinator", commitment="Full-time", team="Marketing", department="Growth"),
        posting(7, "Product Designer", commitment="Full-time", team="Design", department="Product"),
    ]


# --- not admitted, no network ---------------------------------------------------------------------


def test_boards_are_registered_unverified_and_no_tool_calls_them(database, demo, clock, transport, host):
    register = lever.register((NORTH, HARBOR), transport=transport, guard=host)
    serve_listing(transport, NORTH, demo_postings())
    run = begin(
        database, clock, confirmation_for(demo, scope=DEMO_SCOPE, sources=grants(), preferences=(SALES,)), register=register
    )

    result = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    assert {entry.permission for entry in register.entries} == {Permission.UNVERIFIED}
    assert {entry.access for entry in register.entries} == {lever.Access.LOCAL_FETCH}
    assert (result.outcome, result.reason) == (Outcome.REFUSED, f"{NORTH.name}未核实，不能调用")
    assert transport.calls == [] and host.sent == 0


def test_without_a_transport_nothing_is_sent(monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(socket, "socket", unreachable)
    monkeypatch.setattr(socket, "create_connection", unreachable)
    source = LeverSource(NORTH)

    for call in (lambda: source.search(query(), 20.0), lambda: source.read(ref(1), 20.0), lambda: source.check(ref(1), 20.0)):
        with pytest.raises(NetworkClosed):
            call()

    assert (source.guard.sent, source.guard.log) == (0, [])


def test_the_module_has_no_way_to_the_network_and_admits_nothing():
    text = Path(lever.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS
    # Nothing in it admits a source, says a posting is closed or reads a setting.
    assert [word for word in ("Permission.VERIFIED", "CheckStatus.CLOSED", "environ", "getenv") if word in text] == []
    assert lever.entry(NORTH).permission is Permission.UNVERIFIED


@pytest.mark.parametrize("site", ["", "-north", "north/../other", "north?skip=0", "north board", "a" * 59, "北岸"])
def test_a_board_is_named_by_a_plain_site_only(site):
    with pytest.raises(ValueError):
        LeverBoard(site, "Northbay Analytics（合成）")


def test_a_board_s_source_id_stays_plain_and_names_its_company():
    assert PLAIN_ID.fullmatch(LeverBoard("a" * 58, "合成").source_id)
    assert (NORTH.source_id, NORTH.name) == ("lever-rr-demo-northbay", "Northbay Analytics（合成）（Lever 公开职位接口）")

    with pytest.raises(ValueError):
        LeverBoard("rr-demo", "  ")


# --- pages and limits -----------------------------------------------------------------------------


def test_a_search_reads_page_by_page_with_documented_parameters_only(north, transport):
    serve_listing(transport, NORTH, [posting(number) for number in range(1, 151)])

    found = north.search(query("Data Analyst"), 20.0)
    asked = [urllib.parse.urlsplit(url) for url in transport.urls()]

    assert len(found) == 150
    assert [(part.scheme, part.netloc, part.path) for part in asked] == [("https", "api.lever.co", "/v0/postings/rr-demo-northbay")] * 2
    # No q (there is no full-text search) and no category filter: the board's whole list, page by page.
    assert [urllib.parse.parse_qs(part.query) for part in asked] == [
        {"mode": ["json"], "skip": ["0"], "limit": ["100"]},
        {"mode": ["json"], "skip": ["100"], "limit": ["100"]},
    ]


def test_a_full_page_is_followed_by_one_more_before_the_list_counts_as_read(north, transport):
    serve_listing(transport, NORTH, [posting(number) for number in range(1, PAGE_SIZE + 1)])

    assert len(north.search(query(), 20.0)) == PAGE_SIZE
    assert transport.urls() == [list_url(NORTH, 0), list_url(NORTH, 1)]


def test_a_list_still_full_at_its_last_page_is_a_list_read_in_part(north, transport):
    serve_listing(transport, NORTH, [posting(number) for number in range(1, MAX_PAGES * PAGE_SIZE + 1)])

    with pytest.raises(ListingIncomplete) as raised:
        north.search(query(), 20.0)

    # What was read comes with it, every record checked, and never as the whole list (U3-D2-1).
    assert (raised.value.listing.read, raised.value.listing.complete) == (MAX_PAGES * PAGE_SIZE, False)
    assert transport.urls() == [list_url(NORTH, page) for page in range(MAX_PAGES)]


def test_a_search_from_a_list_read_in_part_takes_what_it_found_and_covers_nothing(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, [posting(number) for number in range(1, MAX_PAGES * PAGE_SIZE + 1)])
    run = demo_run(database, clock, demo, transport, host)

    result = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    (record,) = run.searches

    assert result.outcome is Outcome.DONE
    assert f"只读了前 {MAX_PAGES * PAGE_SIZE} 个（读到上限，列表没读完）：这次检索的条件不算查过" in result.summary
    assert len(run.found) == MAX_PAGES * PAGE_SIZE and run.searched == set()
    assert (record.listed, record.partial, record.reused) == (MAX_PAGES * PAGE_SIZE, True, False)
    assert run.partial == {NORTH.source_id: MAX_PAGES * PAGE_SIZE}
    assert len(rules.unsearched(run.scope, run.searched)) == 8
    # Never "covered", whatever it found.
    refused = finish(run, EndReason.SCOPE_DONE)
    assert refused.outcome is Outcome.REFUSED
    assert f"{NORTH.name}的职位列表只读了前 {MAX_PAGES * PAGE_SIZE} 个，没读完，不能说范围内查完了" in refused.summary


def test_a_page_longer_than_asked_for_is_not_taken(north, transport):
    transport.add(list_url(NORTH), ok([posting(number) for number in range(1, PAGE_SIZE + 2)]))

    with pytest.raises(BadReply):
        north.search(query(), 20.0)


def test_an_empty_board_is_read_to_its_end_with_nothing_found(north, transport):
    serve_listing(transport, NORTH, [])

    assert north.search(query(), 20.0) == ()
    assert len(transport.calls) == 1


# --- what a search keeps ----------------------------------------------------------------------------


def test_keywords_are_whole_words_of_the_title_or_its_categories(north, transport):
    serve_listing(
        transport,
        NORTH,
        [
            posting(1),
            posting(2, "International Data Analyst", commitment="Full-time"),
            posting(3, "Operations Coordinator", team="Data Platform"),
            # "Data analyst" only in its requirements: words of the body are never searched.
            posting(4, "Marketing Analyst", commitment="Full-time", team="Marketing", department="Growth", requirements="<li>Data analyst experience</li>"),
            posting(5, "数据分析实习生", commitment="实习"),
        ],
    )

    assert ids(north.search(query("intern"), 20.0)) == [pid(1), pid(3)]
    assert ids(north.search(query("data analyst"), 20.0)) == [pid(1), pid(2)]
    assert ids(north.search(query("data intern"), 20.0)) == [pid(1), pid(3)]
    assert ids(north.search(query("数据分析"), 20.0)) == [pid(5)]


def test_the_city_or_type_asked_for_never_leaves_out_a_posting_whose_facts_are_unknown(north, transport):
    serve_listing(
        transport,
        NORTH,
        [
            posting(1),
            posting(2, location="New York, NY"),
            posting(3, location="Remote - US", workplace="remote"),
            posting(4, commitment="Full-time"),
            posting(5, commitment="Contract", location="New York, NY"),
            posting(6, commitment="校招"),
        ],
    )

    assert ids(north.search(query(city="San Francisco"), 20.0)) == [pid(1), pid(3), pid(4), pid(6)]
    assert ids(north.search(query(job_type="实习"), 20.0)) == [pid(1), pid(2), pid(3), pid(4), pid(5)]


# --- facts and words --------------------------------------------------------------------------------


def test_documented_fields_become_the_posting_s_facts_and_nothing_is_made_up(north, transport):
    # createdAt is not in Lever's documentation: never read, so the dates stay 未提供.
    serve_listing(transport, NORTH, [posting(1, createdAt=1_762_300_000_000)])

    (found,) = north.search(query(), 20.0)

    assert found == Posting(
        ref=PostingRef("lever-rr-demo-northbay", pid(1)),
        title=f"Data Analyst Intern ({LABEL})",
        company=NORTH.company,
        cities=("San Francisco",),
        city_choice=False,
        job_type="实习",
        direction=None,
        posted_on=None,
        deadline=None,
        official=None,
        identity=None,
        # As written (U3-D2-2), beside the city and type read from them.
        places_stated=("San Francisco, CA",),
        type_stated="Intern",
    )


@pytest.mark.parametrize(
    ("location", "all_locations", "workplace", "cities"),
    [
        ("San Francisco, CA", None, "on-site", ("San Francisco",)),
        ("Toronto, Ontario, Canada", None, "hybrid", ("Toronto",)),
        ("Beijing, China", None, "on-site", ("北京",)),
        ("上海", None, "on-site", ("上海",)),
        ("San Francisco, CA", ["San Francisco, CA", "New York, NY"], "on-site", ("San Francisco", "New York")),
        # One place not plainly a city leaves them all unknown: the one left out could be where the hire works.
        ("San Francisco, CA", ["San Francisco, CA", "Remote - US"], "on-site", ()),
        ("San Francisco, CA", None, "remote", ()),
        ("SF Bay Area", None, "hybrid", ()),
        ("Ontario, Canada", None, "hybrid", ()),
        ("United States", None, "on-site", ()),
        ("New York or Boston", None, "on-site", ()),
        ("San Francisco, CA; New York, NY", None, "on-site", ()),
        (None, None, None, ()),
    ],
)
def test_places_are_cities_only_when_they_plainly_say_so(north, transport, location, all_locations, workplace, cities):
    serve_listing(transport, NORTH, [posting(1, location=location, all_locations=all_locations, workplace=workplace)])

    (found,) = north.search(query(), 20.0)

    assert found.cities == cities


@pytest.mark.parametrize(
    ("commitment", "job_type"),
    [
        ("Intern", "实习"),
        ("Internship", "实习"),
        ("Summer Intern", "实习"),
        ("实习生", "实习"),
        ("校招", "校招"),
        ("Full-time", None),
        ("Full-Time", None),
        ("International Rotation", None),
        ("Contract", None),
        (None, None),
    ],
)
def test_a_commitment_gives_a_type_only_when_it_plainly_says_one(north, transport, commitment, job_type):
    serve_listing(transport, NORTH, [posting(1, commitment=commitment)])

    (found,) = north.search(query(), 20.0)

    assert found.job_type == job_type


def test_a_read_gives_the_documented_words_and_lever_s_own_links(north, transport):
    serve_details(transport, NORTH, [posting(1)])

    detail = north.read(ref(1), 20.0)

    assert detail.posting.ref == ref(1)
    assert detail.url == f"https://jobs.lever.co/rr-demo-northbay/{pid(1)}"
    assert detail.apply_url == f"https://jobs.lever.co/rr-demo-northbay/{pid(1)}/apply"
    assert detail.text == "\n".join(
        [
            f"Data Analyst Intern ({LABEL})",
            "Northbay Analytics（合成） · 地点：San Francisco, CA · 类型：Intern",
            "团队：Data；部门：Analytics",
            "工作方式：hybrid",
            "国家或地区代码：US",
            "薪资：30–38 USD（per-hour-wage）",
            "",
            DESCRIPTION,
            "",
            "Requirements：",
            "- Use Python to clean and join data",
            "- Write SQL queries for weekly reports",
            "",
            "Nice to have：",
            "- Tableau dashboards",
            "",
            "This posting is synthetic test data.",
        ]
    )


def test_words_given_only_as_html_are_read_from_it_without_scripts(north, transport):
    item = posting(
        1,
        drop=("descriptionPlain", "openingPlain", "descriptionBodyPlain", "additionalPlain"),
        description="<div><p>Hello <b>analytics</b> team</p><script>alert('x')</script></div>",
        additional="<ul><li>Transit pass</li></ul>",
    )
    serve_details(transport, NORTH, [item])

    text = north.read(ref(1), 20.0).text

    assert "Hello analytics team" in text and "- Transit pass" in text
    assert "alert" not in text and "<" not in text


def test_fields_left_out_stay_unknown(north, transport):
    item = posting(1, drop=("categories", "country", "workplaceType", "salaryRange", "lists", "applyUrl", "additional", "additionalPlain"))
    serve_details(transport, NORTH, [item])

    detail = north.read(ref(1), 20.0)
    facts = detail.posting

    assert (facts.cities, facts.job_type, facts.direction, facts.posted_on, facts.deadline) == ((), None, None, None, None)
    assert detail.apply_url is None
    assert "地点：来源没有提供" in detail.text and "类型：来源没有提供" in detail.text and "薪资" not in detail.text


@pytest.mark.parametrize(
    "fields",
    [
        {"categories": "Data"},
        {"categories": {"location": ["San Francisco"]}},
        {"categories": {"allLocations": "San Francisco, CA"}},
        {"lists": "Requirements"},
        {"lists": [{"text": "Requirements", "content": ["Python"]}]},
        {"descriptionPlain": 7},
        {"applyUrl": 7},
        {"text": "   "},
        {"id": "x" * 65},
    ],
)
def test_a_documented_field_of_the_wrong_kind_fails_the_listing(north, transport, fields):
    serve_listing(transport, NORTH, [posting(2), posting(1, **fields)])

    with pytest.raises(BadReply):
        north.search(query(), 20.0)


def test_an_application_link_is_given_only_as_lever_s_own_form_for_this_posting(north, transport):
    serve_details(transport, NORTH, [posting(1, applyUrl="https://apply.example.invalid/form")])

    assert north.read(ref(1), 20.0).apply_url is None


# --- whose posting it is ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hosted",
    [
        f"https://jobs.lever.co/rr-demo-harbor/{pid(1)}",
        f"https://jobs.lever.co/rr-demo-northbay/{pid(2)}",
        # The EU instance is not read.
        f"https://jobs.eu.lever.co/rr-demo-northbay/{pid(1)}",
        f"http://jobs.lever.co/rr-demo-northbay/{pid(1)}",
        None,
    ],
)
def test_links_naming_another_board_or_posting_are_never_taken(north, transport, hosted):
    serve_listing(transport, NORTH, [posting(2), posting(1, hostedUrl=hosted)])
    transport.add(detail_url(NORTH, pid(1)), ok(posting(1, hostedUrl=hosted)))

    with pytest.raises(IdentityMismatch):
        north.search(query(), 20.0)

    for call in (north.read, north.check):
        with pytest.raises(IdentityMismatch):
            call(ref(1), 20.0)


def test_a_reply_naming_another_posting_is_not_this_posting(north, transport):
    transport.add(detail_url(NORTH, pid(1)), ok(posting(2)))

    for call in (north.read, north.check):
        with pytest.raises(IdentityMismatch):
            call(ref(1), 20.0)


def test_postings_carry_their_own_board_and_nothing_is_sent_for_another(transport, host):
    north, harbor = (LeverSource(board, transport=transport, guard=host) for board in (NORTH, HARBOR))
    serve_listing(transport, NORTH, [posting(1)])

    (found,) = north.search(query(), 20.0)
    sent = len(transport.calls)

    assert found.ref == PostingRef("lever-rr-demo-northbay", pid(1))

    with pytest.raises(IdentityMismatch):
        harbor.read(found.ref, 20.0)

    with pytest.raises(BadReply):
        north.check(PostingRef(NORTH.source_id, "../rr-demo-harbor/x"), 20.0)

    assert len(transport.calls) == sent


# --- the same posting more than once ------------------------------------------------------------------


def test_a_posting_listed_again_as_it_was_is_one(north, transport):
    transport.add(list_url(NORTH, 0), ok([posting(number) for number in range(1, PAGE_SIZE + 1)]))
    # The list moved on while it was read: the last posting of page one comes again first on page two.
    transport.add(list_url(NORTH, 1), ok([posting(PAGE_SIZE), posting(PAGE_SIZE + 1)]))

    found = north.search(query(), 20.0)

    assert len(found) == PAGE_SIZE + 1 and len(set(ids(found))) == PAGE_SIZE + 1


def test_one_id_listed_with_two_contents_fails_the_listing(north, transport):
    transport.add(list_url(NORTH), ok([posting(1), posting(1, "Data Analyst Intern, Summer")]))

    with pytest.raises(BadReply):
        north.search(query(), 20.0)


def test_a_posting_found_by_two_searches_is_new_only_once(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, [posting(1)])
    run = demo_run(database, clock, demo, transport, host)

    first = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    # Fewer words, never a type (U3-D2-E4 F1: a board matches the type by its filter alone).
    second = search_postings(run, NORTH.source_id, "Analyst", city="San Francisco")

    assert first.summary.startswith(f"{NORTH.name}返回 1 个岗位，新的 1 个。")
    # The second is answered from the list the first read (U3-D2-1): said so, nothing sent again.
    assert second.summary.startswith(f"{NORTH.name}用这次查找已经读过的列表筛选（没有再发请求）：符合的 1 个，新的 0 个。")
    assert [record.reused for record in run.searches] == [False, True]
    assert transport.urls() == [list_url(NORTH)]
    assert list(run.found) == [ref(1)]


# --- failures: timeouts, odd replies, 404, refusals -------------------------------------------------------


def test_a_timeout_fails_a_search_and_leaves_a_check_without_an_answer(database, demo, clock, transport, host):
    transport.add(list_url(NORTH), TransportTimeout("no reply"))
    serve_listing(transport, HARBOR, [posting(1, board=HARBOR, location="New York, NY")])
    transport.add(detail_url(HARBOR, pid(1, HARBOR)), TransportTimeout("no reply"))
    run = demo_run(database, clock, demo, transport, host)

    failed = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    search_postings(run, HARBOR.source_id, "Data Analyst", city="New York")
    checked = check_posting(run, [ref(1, HARBOR)])
    found = run.found[ref(1, HARBOR)]

    assert (failed.outcome, failed.reason) == (Outcome.FAILED, "SourceTimeout")
    assert "没有在时限内回答" in failed.summary
    assert (found.status, found.checked_at) == (CheckStatus.UNKNOWN, None)
    assert "状态待确认" in checked.summary


ODD_REPLIES = {
    "html_page": Reply(200, "text/html; charset=utf-8", b"<html><body>Something went wrong</body></html>"),
    "json_object": ok({"ok": False, "error": "Document not found"}),
    "not_postings": ok(["Data Analyst Intern"]),
    "broken_json": Reply(200, JSON_TYPE, b'[{"id": '),
    "not_utf8": Reply(200, JSON_TYPE, "[]".encode("utf-16")),
    "too_big": Reply(200, JSON_TYPE, b"[" + b" " * MAX_BYTES + b"]"),
    "no_type": Reply(200, "", b"[]"),
    "redirect": Reply(301, "text/html", b""),
    "server_error": Reply(500, JSON_TYPE, b"{}"),
}


@pytest.mark.parametrize("name", sorted(ODD_REPLIES))
def test_an_odd_reply_is_a_failure_never_an_answer(north, transport, name):
    transport.add(list_url(NORTH), ODD_REPLIES[name]).add(detail_url(NORTH, pid(1)), ODD_REPLIES[name])

    with pytest.raises(BadReply):
        north.search(query(), 20.0)

    for call in (north.read, north.check):
        with pytest.raises(SourceError) as raised:
            call(ref(1), 20.0)

        assert not isinstance(raised.value, SourceTimeout)


def test_a_404_is_never_taken_as_closed(database, demo, clock, transport, host, north):
    transport.add(detail_url(NORTH, pid(1)), Reply(404, JSON_TYPE, b'{"ok":false,"error":"Document not found"}'))
    serve_listing(transport, NORTH, [posting(1)])

    for call in (north.read, north.check):
        with pytest.raises(NotFound):
            call(ref(1), 20.0)

    run = demo_run(database, clock, demo, transport, host)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    checked = check_posting(run, [ref(1)])
    found = run.found[ref(1)]

    assert (found.status, found.checked_at) == (CheckStatus.UNKNOWN, None)
    assert "状态待确认" in checked.summary and "已关闭" not in checked.summary


def test_a_board_that_is_not_there_fails_its_search(north, transport):
    transport.add(list_url(NORTH), Reply(404, JSON_TYPE, b"{}"))

    with pytest.raises(NotFound):
        north.search(query(), 20.0)


@pytest.mark.parametrize("status", [429, 403, 401])
def test_a_refusal_stops_every_later_request_to_the_host(transport, host, status):
    north, harbor = (LeverSource(board, transport=transport, guard=host) for board in (NORTH, HARBOR))
    transport.add(list_url(NORTH), Reply(status, JSON_TYPE, b"{}"))
    serve_listing(transport, HARBOR, [posting(1, board=HARBOR)])
    serve_details(transport, NORTH, [posting(1)])

    with pytest.raises(HostStopped):
        north.search(query(), 20.0)

    for call in (lambda: harbor.search(query(), 20.0), lambda: north.read(ref(1), 20.0), lambda: north.check(ref(1), 20.0)):
        with pytest.raises(HostStopped):
            call()

    assert (host.sent, host.log) == (1, [(list_url(NORTH), str(status))])
    assert len(transport.calls) == 1


# --- time and budget ----------------------------------------------------------------------------------


def test_requests_are_spaced_and_each_is_given_no_more_than_its_call_has_left(north, transport, ticks):
    serve_listing(transport, NORTH, [posting(number) for number in range(1, 151)])

    north.search(query(), 10.0)

    assert ticks.slept == [MIN_INTERVAL]
    assert [timeout for _, timeout in transport.calls] == [10.0, 10.0 - MIN_INTERVAL]
    assert transport.times[1] - transport.times[0] >= MIN_INTERVAL


def test_a_request_is_never_given_more_than_its_own_limit(north, transport):
    serve_details(transport, NORTH, [posting(1)])

    north.read(ref(1), 60.0)

    assert [timeout for _, timeout in transport.calls] == [REQUEST_SECONDS]


def test_no_request_is_started_without_the_time_its_call_has_left(north, transport, ticks):
    def slow_page():
        ticks.now += 9.0

        return ok([posting(number) for number in range(1, PAGE_SIZE + 1)])

    transport.add(list_url(NORTH, 0), slow_page).add(list_url(NORTH, 1), ok([]))

    with pytest.raises(SourceTimeout):
        north.search(query(), 10.0)

    assert transport.urls() == [list_url(NORTH, 0)]


def test_a_wait_that_ran_long_is_checked_again_before_anything_is_sent(transport, ticks):
    def oversleep(seconds):
        ticks.slept.append(seconds)
        ticks.now += seconds + 5.0

    north = LeverSource(NORTH, transport=transport, guard=lever.HostGuard(monotonic=ticks.monotonic, sleep=oversleep))
    serve_listing(transport, NORTH, [posting(number) for number in range(1, 151)])

    with pytest.raises(SourceTimeout):
        north.search(query(), 8.0)

    assert len(transport.calls) == 1


def test_the_budget_is_one_hard_cap_for_every_board_of_the_host(transport, ticks):
    host = guard(ticks, budget=2)
    north, harbor = (LeverSource(board, transport=transport, guard=host) for board in (NORTH, HARBOR))
    serve_listing(transport, NORTH, [posting(1)])
    serve_listing(transport, HARBOR, [posting(1, board=HARBOR)])
    serve_details(transport, NORTH, [posting(1)])

    north.search(query(), 20.0)
    harbor.search(query(), 20.0)

    with pytest.raises(HostStopped):
        north.check(ref(1), 20.0)

    assert host.sent == 2 and len(transport.calls) == 2


# --- through the tools and the agent, unchanged ------------------------------------------------------------


def test_places_not_plainly_stated_stay_pending_and_plain_ones_are_judged(database, demo, clock, transport, host):
    serve_listing(
        transport,
        NORTH,
        [
            posting(1),
            posting(5, location="Remote - US", workplace="remote"),
            posting(8, location="New York, NY", all_locations=["New York, NY", "Toronto, Ontario, Canada"]),
            posting(9, drop=("categories",)),
        ],
    )
    run = demo_run(database, clock, demo, transport, host)

    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    search_postings(run, NORTH.source_id, "Data Analyst", city="New York")
    screening = {found.ref.posting_id: found.screening for found in run.found.values()}

    assert screening[pid(1)].status is Screen.IN
    assert screening[pid(5)].status is Screen.PENDING and screening[pid(9)].status is Screen.PENDING
    assert screening[pid(5)].reasons == ("工作地点写了但没有认出，待确认",)
    assert screening[pid(8)].status is Screen.PENDING
    assert screening[pid(8)].reasons[0].startswith("工作地点待确认：写了New York、Toronto，没有说明可以选择")


def test_the_agent_finds_reads_checks_and_analyses_lever_postings_offline(database, demo, clock, transport, host):
    items = demo_postings()
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items)
    provider = RecordingProvider()
    run = demo_run(database, clock, demo, transport, host, provider)
    decider = ScriptedDecider(
        [
            make_action(Tool.SEARCH, source_id=NORTH.source_id, keywords="Data Analyst", city="San Francisco", note="按范围检索"),
            make_action(Tool.READ, postings=[ref(1), ref(3)], note="读取详情"),
            make_action(Tool.CHECK, postings=[ref(1), ref(3)], note="核对状态"),
            make_action(Tool.ANALYZE, postings=[ref(1)], note="分析实习岗位"),
            make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束"),
        ]
    )

    finished = run_agent(run, decider)
    outcome = finished.outcome
    (result,) = outcome.results

    assert outcome.end is EndReason.ENOUGH
    assert [(step.tool, step.outcome) for step in finished.trace.steps] == [
        (Tool.SEARCH, "done"),
        (Tool.READ, "done"),
        (Tool.CHECK, "done"),
        (Tool.ANALYZE, "done"),
        (Tool.FINISH, "done"),
    ]
    assert finished.trace.steps[0].summary.startswith(f"{NORTH.name}返回 3 个岗位，新的 3 个。")
    assert (result.ref, result.category, result.evidence, result.company) == (ref(1), Category.STRONG, EvidenceLevel.OFFICIAL, NORTH.company)
    assert (result.url, result.apply_url) == (f"https://jobs.lever.co/rr-demo-northbay/{pid(1)}", f"https://jobs.lever.co/rr-demo-northbay/{pid(1)}/apply")
    assert result.posted_on is None and result.checked_at is not None
    # The others found stay leads, with no conclusion: one read and checked, one whose place is not plain.
    assert {lead.ref for lead in outcome.leads} == {ref(3), ref(5)}
    # One page of the list, then each detail for its read and again for its check; one analysis, two requests.
    assert transport.urls() == [
        list_url(NORTH),
        detail_url(NORTH, pid(1)),
        detail_url(NORTH, pid(3)),
        detail_url(NORTH, pid(1)),
        detail_url(NORTH, pid(3)),
    ]
    assert len(provider.prompts) == 2
