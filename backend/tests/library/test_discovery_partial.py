"""U3-D2-1, offline: bounded partial retrieval, the list kept by one search, sources stopped after an anomaly, and the
postings grouped once after each change. Over synthetic replies and stand-in sources only: nothing is sent anywhere,
and no register here admits a source of the product.

A list read in part is a Lever board still full at its last allowed page (three pages of a hundred). What it holds is
taken and named so in every summary, the outcome and every later decision; it covers nothing, whatever other sources
cover. Every other failure stays a failure.
"""
import json
import threading
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from app.discovery import lever, rules
from app.discovery.agent import Combination, SimulatedDecider, observe, run_agent
from app.discovery.contracts import (
    Access,
    CheckStatus,
    Dimension,
    DimensionScope,
    EndReason,
    EvidenceLevel,
    Limits,
    Others,
    Outcome,
    PartialRead,
    Posting,
    PostingDetail,
    PostingRef,
    SearchScope,
)
from app.discovery.lever import MAX_PAGES, PAGE_SIZE, Reply
from app.discovery.sources import PartialListing, Permission, Register, SourceEntry
from app.discovery.tools import (
    REPEATED,
    analyze_posting,
    analyze_postings,
    check_posting,
    finish,
    get_posting,
    group_postings,
    search_postings,
)
from discovery_helpers import WITHIN, begin, confirmation_for, make_library
from job_helpers import RecordingProvider
from lever_helpers import (
    DEMO_SCOPE,
    HARBOR,
    JSON_TYPE,
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
    verified_entry,
    verified_register,
)

FULL = MAX_PAGES * PAGE_SIZE
# The demo candidate's search, with a direction a Lever title can hold: the simulated decider searches its words.
AGENT_SCOPE = SearchScope(
    city=DimensionScope(("San Francisco", "New York"), Others.EXCLUDED, ("San Francisco",)),
    job_type=DimensionScope(("实习",), Others.INCLUDED, ("实习",)),
    direction=DimensionScope(("Data Analyst",), Others.INCLUDED, ("Data Analyst",)),
    targets=("Data Analyst Intern",),
)


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
def demo(database, owners, clock):
    return make_demo_library(database, owners, clock)


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


def ref(number: int, board=NORTH) -> PostingRef:
    return PostingRef(board.source_id, pid(number, board))


def demo_run(
    database, clock, demo, transport, host, *, provider=None, scope=DEMO_SCOPE, boards=(NORTH, HARBOR), limits=Limits(),
    cancel=None, monotonic=lambda: 0.0,
):
    confirmation = confirmation_for(demo, scope=scope, sources=grants(boards), preferences=(SALES,), limits=limits)

    return begin(
        database, clock, confirmation, provider, register=verified_register(transport, host, boards), cancel=cancel,
        monotonic=monotonic,
    )


def big_board(matching=()) -> list[dict]:
    """The northern board still full at its last page: those numbered in matching are San Francisco data analyst
    interns, the others London engineering roles that no search here matches."""
    return [
        posting(number)
        if number in matching
        else posting(
            number, "Software Engineer", commitment="Full-time", team="Engineering", department="Platform",
            location="London, United Kingdom",
        )
        for number in range(1, FULL + 1)
    ]


def cities(run) -> list[tuple[str, str | None]]:
    return [(record.source_id, dict(record.filters)[Dimension.CITY]) for record in run.searches]


# --- a list read in part: taken, never covering ----------------------------------------------------


@pytest.mark.parametrize("order", ["partial_first", "complete_first"])
def test_a_complete_list_elsewhere_never_takes_away_a_list_read_in_part(database, demo, clock, transport, host, order):
    serve_listing(transport, NORTH, big_board())
    serve_listing(transport, HARBOR, [posting(1, board=HARBOR, location="New York, NY")])
    run = demo_run(database, clock, demo, transport, host)
    boards = (NORTH, HARBOR) if order == "partial_first" else (HARBOR, NORTH)

    for board in boards:
        for city in ("San Francisco", "New York"):
            assert search_postings(run, board.source_id, "Biologist", city=city).outcome is Outcome.DONE

    everything = rules.unsearched(run.scope, set())
    expected = (PartialRead(NORTH.source_id, NORTH.name, FULL, everything),)

    # Harbor covered every combination; the northern board's list is still told as read in part, its marker never gone.
    assert rules.unsearched(run.scope, run.searched) == ()
    assert run.partial_reads() == expected
    assert [(record.source_id, record.partial) for record in run.searches] == [
        (board.source_id, board == NORTH) for board in boards for _ in range(2)
    ]
    assert f"{NORTH.source_id}（{NORTH.name}）的职位列表只读了前 {FULL} 个" in observe(run, 5, 8).text
    scope_done = finish(run, EndReason.SCOPE_DONE)
    assert scope_done.outcome is Outcome.REFUSED
    assert f"{NORTH.name}的职位列表只读了前 {FULL} 个，没读完，不能说范围内查完了" in scope_done.summary
    # Nothing suitable is said only of what was searched.
    assert finish(run, EndReason.NO_RESULTS).summary == "查找结束：已查查询无结果，仍有范围未核验。"
    outcome = run.outcome()
    assert (outcome.end, outcome.unsearched, outcome.partial) == (EndReason.PARTLY_NO_RESULTS, (), expected)
    # Each list read once.
    assert transport.urls().count(list_url(NORTH, 0)) == 1 and transport.urls().count(list_url(HARBOR, 0)) == 1


def test_searching_a_list_read_in_part_again_covers_nothing_and_finds_nothing_new(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, big_board(matching=(1, 2)))
    run = demo_run(database, clock, demo, transport, host)

    first = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    again = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    elsewhere = search_postings(run, NORTH.source_id, "Data Analyst", city="New York")

    assert first.summary.startswith(f"{NORTH.name}返回 2 个岗位，新的 2 个。")
    # The same search from the same list (U3-D5): it could bring nothing new, so it is not made, it is said so, and it
    # counts as no search. Another city on that list is another search, answered from the list as before.
    assert again.reason == REPEATED and "和第 1 次检索完全相同" in again.summary and "没有再执行" in again.summary
    assert elsewhere.summary.startswith(f"{NORTH.name}用这次查找已经读过的列表筛选（没有再发请求）：符合的 0 个，新的 0 个。")
    assert all("这次检索的条件不算查过" in result.summary for result in (first, elsewhere))
    assert [(record.found, record.partial, record.reused) for record in run.searches] == [
        (2, True, False),
        (0, True, True),
    ]
    # Tried for two cities, covered for none; two postings found once each; the list asked for once.
    assert run.searched == set() and len(run.partly) == 2 and list(run.found) == [ref(1), ref(2)]
    assert transport.urls() == [list_url(NORTH, page) for page in range(MAX_PAGES)]


def test_with_a_list_read_in_part_every_end_guard_holds(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, big_board(matching=(1,)))
    run = demo_run(database, clock, demo, transport, host, limits=Limits(refusals=5))
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    refused = {claim: finish(run, claim) for claim in (EndReason.SCOPE_DONE, EndReason.NO_RESULTS, EndReason.ENOUGH)}

    assert all(result.outcome is Outcome.REFUSED for result in refused.values()) and run.ended is None
    assert "范围里还有 8 个组合没有完整查过" in refused[EndReason.SCOPE_DONE].summary
    assert f"{NORTH.name}的职位列表只读了前 {FULL} 个，没读完" in refused[EndReason.SCOPE_DONE].summary
    # A posting found and not yet concluded on: never "nothing suitable".
    assert "还有 1 个岗位没有结论" in refused[EndReason.NO_RESULTS].summary
    # Experiences chosen and no result standing: never "enough" (U3-Q2).
    assert "还没有现在仍然成立的完整分析结果" in refused[EndReason.ENOUGH].summary


TROUBLE = {
    # A record of the last page names another board: an identity anomaly, nothing of the list taken.
    "another_board": ("IdentityMismatch", True),
    "redirect": ("BadReply", True),
    "server_error": ("BadReply", True),
    "not_found": ("NotFound", False),
    "timeout": ("SourceTimeout", False),
    "too_many_requests": ("HostStopped", False),
}


@pytest.mark.parametrize("trouble", sorted(TROUBLE))
def test_a_list_failing_on_any_page_is_never_taken_in_part(database, demo, clock, transport, host, trouble):
    reason, anomaly = TROUBLE[trouble]
    items = big_board(matching=(1,))
    last = items[2 * PAGE_SIZE :]
    last[5] = {**last[5], "hostedUrl": f"https://jobs.lever.co/other-board/{last[5]['id']}"}

    for page in range(2):
        transport.add(list_url(NORTH, page), ok(items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]))

    transport.add(
        list_url(NORTH, 2),
        {
            "another_board": ok(last),
            "redirect": Reply(302, "text/html", b""),
            "server_error": Reply(500, JSON_TYPE, b"{}"),
            "not_found": Reply(404, JSON_TYPE, b"{}"),
            "timeout": lever.TransportTimeout("no reply"),
            "too_many_requests": Reply(429, JSON_TYPE, b"{}"),
        }[trouble],
    )
    run = demo_run(database, clock, demo, transport, host)

    result = search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    assert (result.outcome, result.reason) == (Outcome.FAILED, reason)
    assert (run.found, run.partial, run.partly, run.searched, run.listings) == ({}, {}, set(), set(), {})
    assert (NORTH.source_id in run.disabled) is anomaly
    # The same list is not asked for again, whatever the next search's words: refused before anything is sent.
    requests = len(transport.calls)
    again = search_postings(run, NORTH.source_id, "Analyst", city="New York")
    assert again.outcome is Outcome.REFUSED and len(transport.calls) == requests
    assert ("这次查找不再调用它" if anomaly else f"{NORTH.name}的职位列表这次没有读到，同一个请求不会重试") in again.summary


# --- what may be taken as a list read in part, and what never is -----------------------------------------

FAKE = "fake-board"


@dataclass(frozen=True)
class FakeListing:
    postings: tuple[Posting, ...]
    complete: bool

    @property
    def read(self) -> int:
        return len(self.postings)

    def select(self, query) -> tuple[Posting, ...]:
        return self.postings


class ListingOf:
    """A stand-in source answering from its whole list: the list it gives, or one it says was cut short."""

    def __init__(self, given: FakeListing | None = None, *, cut: FakeListing | None = None, details=None):
        self.given, self.cut, self.details, self.calls = given, cut, details or {}, 0

    def listing(self, timeout):
        self.calls += 1

        if self.cut is not None:
            raise PartialListing("cut short", self.cut)

        return self.given

    def search(self, query, timeout):
        raise AssertionError("a source answering from its list is searched through it")

    def read(self, ref, timeout):
        return self.details[ref]

    def check(self, ref, timeout):
        raise AssertionError("not checked here")


class PlainSaysCut:
    """A source searched query by query that says its list was cut short: not one answering from its list."""

    def search(self, query, timeout):
        raise PartialListing("cut short", FakeListing((fake_posting(1, FAKE),), complete=False))

    def read(self, ref, timeout):
        raise AssertionError("not read here")

    check = read


def fake_posting(number: int, source: str = "fake-official", *, official=None, identity=None) -> Posting:
    # Titles and companies repeat every 150 postings: alike, and one job when they also share a job number.
    return Posting(
        ref=PostingRef(source, f"P{number:04d}"),
        title=f"数据分析实习生 {number % 150}",
        company=f"合成公司 {number % 150 % 23}",
        cities=("北京",),
        city_choice=False,
        job_type="实习",
        direction="数据分析",
        posted_on=None,
        deadline=None,
        official=official,
        identity=identity,
    )


def fake_entry(source_id: str, evidence: EvidenceLevel = EvidenceLevel.OFFICIAL) -> SourceEntry:
    return SourceEntry(source_id, f"合成来源 {source_id}", Access.SYNTHETIC, Permission.VERIFIED, evidence, "测试", "2026-11-11")


def fake_run(database, library, clock, adapters: dict, evidence=None):
    entries = tuple(fake_entry(source_id, (evidence or {}).get(source_id, EvidenceLevel.OFFICIAL)) for source_id in adapters)
    confirmation = confirmation_for(library, sources=tuple(entry.grant for entry in entries))

    return begin(database, clock, confirmation, register=Register(entries, adapters))


@pytest.mark.parametrize("source", ["plain_search_says_cut", "incomplete_without_saying", "cut_but_complete", "cut_with_nothing"])
def test_a_list_is_taken_in_part_only_as_its_source_says_and_never_otherwise(database, library, clock, source):
    one = (fake_posting(1, FAKE),)
    adapter = {
        "plain_search_says_cut": PlainSaysCut(),
        "incomplete_without_saying": ListingOf(FakeListing(one, complete=False)),
        "cut_but_complete": ListingOf(cut=FakeListing(one, complete=True)),
        "cut_with_nothing": ListingOf(cut=FakeListing((), complete=False)),
    }[source]
    run = fake_run(database, library, clock, {FAKE: adapter})

    result = search_postings(run, FAKE, "数据分析", city="北京", **WITHIN)

    assert result.outcome is Outcome.FAILED
    assert (run.found, run.partial, run.searched) == ({}, {}, set())
    # A list breaking what a source answering from its list promises is an anomaly; a plain search's is a failure.
    assert (FAKE in run.disabled) is (source != "plain_search_says_cut")


# --- the list kept: one search's own, never past a check ---------------------------------------------


def test_a_list_is_kept_by_the_search_that_read_it_and_by_no_other(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, [posting(1)])
    register = verified_register(transport, host)
    mine = confirmation_for(demo, scope=DEMO_SCOPE, sources=grants(), preferences=(SALES,))
    theirs = confirmation_for(SimpleNamespace(owner=demo.other), scope=DEMO_SCOPE, sources=grants(), selection=(), preferences=())
    runs = [begin(database, clock, confirmation, register=register) for confirmation in (mine, theirs, mine)]

    for run in runs:
        search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
        search_postings(run, NORTH.source_id, "Data Analyst", city="New York")

    # Each search read the list for itself: never from another user's search, nor from another search of the same user.
    assert transport.urls() == [list_url(NORTH)] * 3
    assert [[record.reused for record in run.searches] for run in runs] == [[False, True]] * 3
    # Once a search ends, the list it read goes with it.
    assert finish(runs[1], EndReason.ENOUGH).outcome is Outcome.DONE and runs[1].listings == {}


@pytest.mark.parametrize("stop", ["cancel", "timeout", "unverified", "not_as_confirmed"])
def test_a_list_kept_lets_no_search_past_its_checks(database, demo, clock, transport, host, stop):
    serve_listing(transport, NORTH, [posting(1)])
    cancel, now = threading.Event(), [0.0]
    run = demo_run(database, clock, demo, transport, host, cancel=cancel, monotonic=lambda: now[0])
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    assert NORTH.source_id in run.listings

    if stop == "cancel":
        cancel.set()
    elif stop == "timeout":
        now[0] = run.limits.seconds + 1
    elif stop == "unverified":
        adapters = {board.source_id: run.register.adapter(board.source_id) for board in (NORTH, HARBOR)}
        run.register = Register((lever.entry(NORTH), verified_entry(HARBOR)), adapters)
    else:
        run.grants[NORTH.source_id] = replace(run.grants[NORTH.source_id], name="另一个接收方")

    requests = len(transport.calls)
    result = search_postings(run, NORTH.source_id, "Data Analyst", city="New York")

    # Nothing answered from the list and nothing sent.
    assert len(transport.calls) == requests and len(run.searches) == 1

    if stop in ("cancel", "timeout"):
        assert result.outcome is Outcome.STOPPED and run.listings == {}
    else:
        assert result.outcome is Outcome.REFUSED
        assert ("未核实，不能调用" if stop == "unverified" else "和确认时不一致") in result.summary


def test_an_anomaly_while_reading_stops_every_later_call_to_its_source(database, demo, clock, transport, host):
    items = [posting(1), posting(2), posting(3)]
    serve_listing(transport, NORTH, items)
    # The first posting's own reply names another posting.
    transport.add(detail_url(NORTH, pid(1)), ok({**items[0], "id": pid(9)}))
    serve_details(transport, NORTH, items[1:])
    harbor = [posting(1, board=HARBOR, location="New York, NY")]
    serve_listing(transport, HARBOR, harbor)
    run = demo_run(database, clock, demo, transport, host, limits=Limits(refusals=5))
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    read = get_posting(run, [ref(1), ref(2)])

    assert f"[{NORTH.source_id}/{pid(1)}] 来源返回了不符合要求的内容，没有采用；这次查找不再调用这个来源" in read.summary
    assert f"[{NORTH.source_id}/{pid(2)}] 来源已经停用（返回了不符合要求的内容），没有发出请求" in read.summary
    assert run.reads == 1 and detail_url(NORTH, pid(2)) not in transport.urls()
    requests = len(transport.calls)
    later = [
        get_posting(run, [ref(3)]),
        check_posting(run, [ref(2)]),
        search_postings(run, NORTH.source_id, "Data Analyst", city="New York"),
    ]
    assert all(result.outcome is Outcome.REFUSED and "这次查找不再调用它" in result.summary for result in later)
    assert len(transport.calls) == requests
    # Its postings are not analysed either; another board of the same host goes on.
    assert NORTH.source_id not in {source_id for source_id, _, _ in observe(run, 5, 8).sources}
    assert search_postings(run, HARBOR.source_id, "Data Analyst", city="New York").outcome is Outcome.DONE


def test_an_anomaly_while_checking_stops_the_rest_of_the_step(database, demo, clock, transport, host):
    items = [posting(1), posting(2)]
    serve_listing(transport, NORTH, items)
    transport.add(detail_url(NORTH, pid(1)), Reply(302, "text/html", b""))
    serve_details(transport, NORTH, items[1:])
    run = demo_run(database, clock, demo, transport, host)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    checked = check_posting(run, [ref(1), ref(2)])

    assert "状态待确认，核对时间不更新；这次查找不再调用这个来源" in checked.summary and "没有发出请求" in checked.summary
    assert (run.found[ref(1)].status, run.found[ref(2)].status, run.checks) == (CheckStatus.UNKNOWN, None, 1)
    assert detail_url(NORTH, pid(2)) not in transport.urls()


# --- the postings grouped once after each change ------------------------------------------------------


def refs_of(groups) -> list[list[PostingRef]]:
    return [[found.ref for found in group] for group in groups]


def test_postings_are_grouped_once_after_each_change_as_a_fresh_grouping_would(database, library, clock):
    # 300 official postings: postings n and n + 150 alike, one job when they share a job number (every tenth);
    # and 10 leads, each pointing to an official posting.
    officials = tuple(
        fake_posting(number, identity=f"JOB-{number % 150}" if number % 10 == 0 else None) for number in range(1, FULL + 1)
    )
    leads = tuple(fake_posting(number, "fake-leads", official=officials[number - 1].ref) for number in range(1, 11))
    changed = replace(officials[9], identity=None)
    details = {changed.ref: PostingDetail(changed, "数据分析实习生，负责整理数据。", "https://example.invalid/p10")}
    listing = ListingOf(FakeListing(officials, complete=True), details=details)

    class Leads:
        def search(self, query, timeout):
            return leads

    run = fake_run(
        database, library, clock, {"fake-official": listing, "fake-leads": Leads()}, evidence={"fake-leads": EvidenceLevel.LEAD}
    )

    search_postings(run, "fake-official", "数据分析", city="北京", **WITHIN)
    # One search of 300: grouped once for every line it listed, not once per line.
    assert run.groupings == 1 and len(run.groups()) == FULL - 15
    assert refs_of(run.groups()) == refs_of(group_postings(run.found.values(), run.same))

    search_postings(run, "fake-leads", "数据分析", city="北京", **WITHIN)
    observe(run, 3, 8)
    finish(run, EndReason.NO_RESULTS)
    # New postings group them again once; listing every candidate, or a finish, does not.
    assert run.groupings == 2 and len(run.groups()) == FULL - 15
    assert [found.ref for found in run.group_of(leads[0].ref)] == [officials[0].ref, leads[0].ref]

    # A read changes the tenth posting's facts: no longer one job with posting 160, grouped again.
    get_posting(run, [changed.ref])
    assert refs_of(run.groups()) == refs_of(group_postings(run.found.values(), run.same))
    assert run.groupings == 3 and len(run.groups()) == FULL - 14


# --- the agent: a list read in part stays told, and no search of it is made twice ----------------------------


class Watching:
    """The simulated decider, keeping what each decision was shown."""

    kind = "simulated"

    def __init__(self):
        self.inner, self.seen = SimulatedDecider(), []

    def decide(self, observation):
        self.seen.append(observation)

        return self.inner.decide(observation)


def test_the_simulated_agent_works_from_a_list_read_in_part_and_keeps_saying_so(database, demo, clock, transport, host):
    items = big_board(matching=(1, 2, 3))
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items[:3])
    serve_listing(
        transport, HARBOR, [posting(1, "Product Designer", board=HARBOR, commitment="Full-time", team="Design", department="Product")]
    )
    run = demo_run(database, clock, demo, transport, host, provider=RecordingProvider(), scope=AGENT_SCOPE)
    decider = Watching()

    outcome = run_agent(run, decider).outcome

    assert outcome.end is EndReason.ENOUGH and {result.ref for result in outcome.results} == {ref(1), ref(2), ref(3)}
    assert outcome.partial == (PartialRead(NORTH.source_id, NORTH.name, FULL, rules.unsearched(run.scope, set())),)
    # Every decision after the first was told the list was read only in part.
    assert len(decider.seen) > 1 and all(f"只读了前 {FULL} 个" in seen.text for seen in decider.seen[1:])
    # No search of a list read in part made twice: the next city there, the same city at the other board.
    assert cities(run) == [(NORTH.source_id, "San Francisco"), (HARBOR.source_id, "San Francisco"), (NORTH.source_id, "New York")]
    assert transport.urls().count(list_url(NORTH, 0)) == 1


def test_the_simulated_decider_never_searches_a_list_it_has_read_again_with_more_words(database, demo, clock, transport, host):
    # Only senior roles came back: a search service may find others with the job type among the words (A2), while a
    # list read here can only give fewer of the same, so the decider goes on with what it found.
    serve_listing(transport, NORTH, [posting(number, "Senior Data Analyst", commitment="Full-time") for number in (1, 2, 3)])
    scope = replace(AGENT_SCOPE, job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)))
    run = demo_run(database, clock, demo, transport, host, scope=scope)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco", job_type="实习")
    last = (NORTH.source_id, "Data Analyst", Combination("San Francisco", "实习", None, ""))

    observation = observe(run, 2, 8, last_search=last, new=tuple(run.found))
    action = json.loads(SimulatedDecider().decide(observation))

    assert all("资深" in candidate.marks for candidate in observation.candidates)
    assert action["tool"] == "get_posting" and NORTH.source_id in observation.listed


def test_the_simulated_agent_ends_rather_than_search_a_list_read_in_part_again(database, demo, clock, transport, host):
    serve_listing(transport, NORTH, big_board())
    run = demo_run(database, clock, demo, transport, host, scope=AGENT_SCOPE, boards=(NORTH,))

    finished = run_agent(run, SimulatedDecider())

    assert cities(run) == [(NORTH.source_id, "San Francisco"), (NORTH.source_id, "New York")]
    assert [step.tool.value for step in finished.trace.steps] == ["search_postings", "search_postings", "finish"]
    assert finished.outcome.end is EndReason.PARTLY_NO_RESULTS
    assert transport.urls() == [list_url(NORTH, page) for page in range(MAX_PAGES)]


# --- D2-1 R1 (F1): a source the search may no longer ask is never analysed through, by either entry ------------

CAUSES = {
    # Reading another of its postings gave a third one's identity: an anomaly, and the source is stopped.
    "anomaly": "这次查找不再调用它",
    "unverified": "未核实，不能调用",
    "not_as_confirmed": "和确认时不一致",
}


@pytest.mark.parametrize("entry", ["single", "batch"])
@pytest.mark.parametrize("cause", [*CAUSES, "none"])
def test_no_analysis_goes_through_a_source_the_search_may_no_longer_ask(database, demo, clock, transport, host, entry, cause):
    items = [posting(1), posting(2)]
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items[:1])
    transport.add(detail_url(NORTH, pid(2)), ok({**items[1], "id": pid(9)}))
    provider = RecordingProvider()
    run = demo_run(database, clock, demo, transport, host, provider=provider, limits=Limits(refusals=5))
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    get_posting(run, [ref(1)])
    check_posting(run, [ref(1)])

    if cause == "anomaly":
        get_posting(run, [ref(2)])
        assert NORTH.source_id in run.disabled
    elif cause == "unverified":
        adapters = {board.source_id: run.register.adapter(board.source_id) for board in (NORTH, HARBOR)}
        run.register = Register((lever.entry(NORTH), verified_entry(HARBOR)), adapters)
    elif cause == "not_as_confirmed":
        run.grants[NORTH.source_id] = replace(run.grants[NORTH.source_id], name="另一个接收方")

    result = analyze_posting(run, ref(1)) if entry == "single" else analyze_postings(run, [ref(1)])

    if cause == "none":
        # A source still as confirmed: the analysis goes as before, one posting and its two generations.
        assert (result.outcome, run.analyses, len(provider.prompts)) == (Outcome.DONE, 1, 2)
        assert run.found[ref(1)].analysis is not None
        return

    assert result.outcome is Outcome.REFUSED and CAUSES[cause] in result.summary
    # Nothing sent, nothing counted, nothing kept: the posting stays a lead with no conclusion.
    assert (provider.prompts, run.analyses, run.found[ref(1)].analysis) == ([], 0, None)
    run.end(EndReason.LIMIT)
    outcome = run.outcome()
    assert outcome.results == () and ref(1) in {lead.ref for lead in outcome.leads}
