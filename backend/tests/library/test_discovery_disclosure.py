"""U3-D2-2, offline: what a search says of postings and what of them goes out.

- A check says a posting is still published when checked, never that the employer still has places.
- A fact not known is told as what it is: the source gave none, or gave one not read; a title and a type that
  disagree are noted, and nothing is guessed.
- North American phone numbers and English street addresses are found. Of an admitted official posting, a short
  line holding an English address is masked whole before an analysis or a decision sees it; anything less plain
  stops the posting as before. The user's own material and every other posting keep the rules they had.

Synthetic replies and stand-in services only: nothing is sent anywhere, and no register here admits a source of
the product.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from app.discovery import agent, compare_tasks, lever, rules
from app.discovery.agent import observe
from app.discovery.contracts import (
    DimensionScope,
    EvidenceLevel,
    Limits,
    Others,
    Outcome,
    Posting,
    PostingRef,
    Screen,
)
from app.discovery.sources import Register
from app.discovery.tools import _outgoing, analyze_posting, check_posting, get_posting, posting_line, search_postings
from app.outbound_review import (
    ADDRESS,
    ADDRESS_LINE,
    PHONE,
    kinds,
    masked_official_posting,
    masked_posting,
    review,
)
from discovery_helpers import begin, confirmation_for
from job_helpers import RecordingProvider
from lever_helpers import (
    DEMO_SCOPE,
    HARBOR,
    NORTH,
    SALES,
    ReplayTransport,
    Ticks,
    grants,
    guard,
    make_demo_library,
    pid,
    posting,
    serve_details,
    serve_listing,
    verified_entry,
    verified_register,
)

APP = Path(__file__).resolve().parents[2] / "app"
OFFICE = "Our office:\n200 Park Avenue\nSuite 400\nNew York, NY 10166\nQuestions? Call (212) 555-0100."
ADDRESS_WORDS = ("200 Park Avenue", "Suite 400", "10166", "555-0100")


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


def ref(number: int, board=NORTH) -> PostingRef:
    return PostingRef(board.source_id, pid(number, board))


def run_of(database, clock, demo, transport, host, *, provider=None, scope=DEMO_SCOPE):
    confirmation = confirmation_for(demo, scope=scope, sources=grants(), preferences=(SALES,), limits=Limits(refusals=5))

    return begin(database, clock, confirmation, provider, register=verified_register(transport, host))


def found_kinds(text: str) -> tuple[str, ...]:
    return kinds(review(text))


# --- North American phone numbers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "phone", ["(212) 555-0100", "(212)555-0100", "212-555-0100", "212.555.0100", "212 555 0100", "+1 212 555 0100",
              "1-800-555-0100", "+1 (415) 555-0123"],
)
def test_north_american_numbers_are_found_and_masked_in_a_posting(phone):
    masked, hidden, stopping = masked_posting(f"Recruiting line: {phone}. Apply online.")

    assert found_kinds(f"我的电话 {phone}") == (PHONE,)
    assert (masked, hidden, stopping) == ("Recruiting line: ［已隐去电话号码］. Apply online.", (PHONE,), ())


@pytest.mark.parametrize(
    "text",
    ["Starts 2026-11-11, ends 11/11/2027", "Pay $120,000 - $150,000 a year", "401(k) matching", "Python 3.11 and SQL",
     "Worked there 2019-2021", "New York, NY", "Hours 09:00-17:00", "100-200 hours a month", "Ref 4155550123",
     f"Posting {pid(1)}", "Team of 12, 5 years of experience"],
)
def test_dates_amounts_versions_and_ids_are_not_phone_numbers(text):
    assert PHONE not in found_kinds(text) and masked_posting(text) == (text, (), ())


# --- English street addresses ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    ["1 Hacker Way", "200 Park Avenue", "1585 Broadway", "345 Spear St.", "500 W 2nd St", "Suite 400", "Fl. 12",
     "5th Floor", "P.O. Box 1234", "New York, NY 10001", "San Francisco, CA 94105-1234"],
)
def test_english_addresses_are_found(address):
    assert found_kinds(f"Lives at {address} now") == (ADDRESS,)


@pytest.mark.parametrize(
    "text",
    ["New York, NY", "San Francisco, CA", "London, United Kingdom", "Washington, D.C.", "Remote - US",
     "5 years of experience", "Top 3 priorities this quarter", "2026 Summer Internship", "3 days a week in office",
     "Served in Unit 8200", "Python 3 data pipelines", "Q3 2026 planning"],
)
def test_cities_and_ordinary_numbers_are_not_addresses(text):
    assert ADDRESS not in found_kinds(text)


def test_the_user_s_own_material_with_an_english_address_or_phone_still_does_not_go_out():
    # Nothing is masked for the user: the field is stopped, as with every address and phone before.
    assert found_kinds("Home: 12 Elm Street, Springfield") == (ADDRESS,)
    assert found_kinds("Reach me at 617-555-0199") == (PHONE,)


# --- an admitted official posting: English address lines masked whole, or the posting stopped ------------


def test_a_short_address_line_is_masked_whole_and_nothing_of_it_kept():
    masked, hidden, stopping = masked_official_posting(f"Data Analyst Intern\n\n{OFFICE}\nWe value curiosity.")

    assert stopping == () and hidden == (PHONE, ADDRESS)
    assert masked.count(ADDRESS_LINE) == 3 and "Our office:" in masked and "We value curiosity." in masked
    assert not any(word in masked for word in ADDRESS_WORDS)


@pytest.mark.parametrize(
    "text",
    [
        # Across a line break: only the two lines together read as an address.
        "Visit us at 200 Park\nAvenue, New York",
        # In a line too long for its bounds to be plain.
        "We are " + "a growing team building data products for city services, " * 3 + "at 200 Park Avenue, New York.",
        # A Chinese address, under the rules it always had.
        "办公地点：北京市海淀区中关村大街 27 号",
        # Marked confidential.
        "Internal only: compensation bands",
    ],
    ids=["across_lines", "long_line", "chinese_address", "confidential"],
)
def test_an_address_not_plainly_on_its_own_line_or_any_other_stop_still_stops_the_posting(text):
    masked, _, stopping = masked_official_posting(text)

    assert stopping and ADDRESS_LINE not in masked


def test_a_posting_the_user_pasted_or_any_other_keeps_the_old_rule():
    # Only a caller that knows the posting's source is admitted and official asks for lines to be masked.
    assert masked_posting(OFFICE)[2] == (ADDRESS,)


# --- facts not known, and facts in conflict ---------------------------------------------------------------------

TYPED_SCOPE = replace(DEMO_SCOPE, job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)))


def fact(**fields) -> Posting:
    base = dict(
        ref=PostingRef("lever-rr-demo-northbay", "x1"), title="Data Analyst", company="Northbay（合成）",
        cities=("San Francisco",), city_choice=False, job_type="实习", direction=None, posted_on=None, deadline=None,
    )

    return Posting(**{**base, **fields})


@pytest.mark.parametrize(
    "fields, reason",
    [
        (dict(cities=()), "工作地点来源没有提供，待确认"),
        (dict(cities=(), places_stated=("London, United Kingdom",)), "工作地点写了但没有认出，待确认"),
        (dict(job_type=None), "求职类型来源没有提供，待确认"),
        # U3-D2-E3: a type that only says the hours or the contract is told as that, never as not read.
        (dict(job_type=None, type_stated="Full-time"), "求职类型只写了工时或合同，待确认"),
        (dict(job_type=None, type_stated="Regular"), "求职类型写了但没有认出，待确认"),
    ],
)
def test_a_fact_not_given_and_one_given_but_not_read_are_told_apart(fields, reason):
    assert rules.screen(TYPED_SCOPE, fact(**fields)).reasons == (reason,)


def test_a_title_and_a_type_in_conflict_are_noted_and_neither_is_taken():
    # U3-D2-E3: only another kind of hire in the type field is a conflict with a title stating an internship.
    conflicting = fact(title="Software Engineering Internship", job_type="校招", type_stated="Campus Hire（校招）")
    screening = rules.screen(TYPED_SCOPE, conflicting)

    assert screening.status is Screen.PENDING and screening.reasons == ("求职类型和职位名不一致，待确认",)
    assert screening.notes == ()
    assert rules.type_conflict(conflicting)
    # No type field, or one read as an internship: nothing to disagree with.
    assert not rules.type_conflict(fact(title="Data Intern", job_type=None))
    assert not rules.type_conflict(fact(title="Data Intern", type_stated="Intern"))
    # A full-time internship is no conflict (U3-D2-E3): the type stays pending, the posting is not left out.
    full_time = rules.screen(TYPED_SCOPE, fact(title="Software Engineering Internship", job_type=None, type_stated="Full-time"))
    assert full_time.status is Screen.PENDING and full_time.reasons == ("求职类型只写了工时或合同，待确认",)
    assert full_time.notes == ("职位名写的是实习，来源的类型字段只写了工时或合同，两者不冲突；求职类型仍待确认，不排除",)


def test_lever_keeps_what_it_did_not_read_and_reads_no_more_places_than_before(database, demo, clock, transport, host):
    serve_listing(
        transport,
        NORTH,
        [
            posting(1, "Data Analyst Internship", commitment="Full-time"),
            posting(2, location="London, United Kingdom"),
            posting(3, location="Washington, D.C."),
            posting(4, location=None, all_locations=[], drop=()),
        ],
    )
    run = run_of(database, clock, demo, transport, host, scope=TYPED_SCOPE)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco", job_type="实习")
    found = {item.ref.posting_id: item for item in run.found.values()}
    line = {posting_id: posting_line(run, item) for posting_id, item in found.items()}

    # Places not plainly cities stay unknown, as they were: told as written and not read, never guessed.
    assert [found[pid(n)].posting.cities for n in (2, 3, 4)] == [(), (), ()]
    assert "城市写了但没有认出：London, United Kingdom" in line[pid(2)]
    assert "城市写了但没有认出：Washington, D.C." in line[pid(3)]
    assert "城市来源没有提供" in line[pid(4)]
    # A full-time internship (U3-D2-E3): the type only says the hours, so it stays pending as written, told as that,
    # and is no conflict with the title.
    first = found[pid(1)].posting
    assert (first.job_type, first.type_stated) == (None, "Full-time")
    assert "类型只写了工时或合同：Full-time" in line[pid(1)] and "职位名写的是实习" in line[pid(1)]
    assert "不一致" not in line[pid(1)]


# --- the analysis and the decision: both see only what the outbound rules let through --------------------------


def office_board(transport, *, additional: str = OFFICE):
    items = [posting(1, additionalPlain=additional)]
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items)


def test_an_analysis_of_an_admitted_official_posting_sends_its_address_lines_masked(database, demo, clock, transport, host):
    office_board(transport)
    provider = RecordingProvider()
    run = run_of(database, clock, demo, transport, host, provider=provider)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    get_posting(run, [ref(1)])
    check_posting(run, [ref(1)])

    result = analyze_posting(run, ref(1))

    assert result.outcome is Outcome.DONE and len(provider.prompts) == 2
    assert all(ADDRESS_LINE in prompt and not any(word in prompt for word in ADDRESS_WORDS) for prompt in provider.prompts)
    # Only what goes out is masked: the posting as read stays as it was.
    assert all(word in run.found[ref(1)].detail.text for word in ADDRESS_WORDS)


def test_an_address_across_lines_still_stops_the_analysis(database, demo, clock, transport, host):
    office_board(transport, additional="Visit us at 200 Park\nAvenue, New York.")
    provider = RecordingProvider()
    run = run_of(database, clock, demo, transport, host, provider=provider)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    get_posting(run, [ref(1)])
    check_posting(run, [ref(1)])

    result = analyze_posting(run, ref(1))

    assert result.outcome is Outcome.BLOCKED and provider.prompts == [] and run.analyses == 0


def test_the_decision_sees_the_same_masking_and_no_address_by_another_field(database, demo, clock, transport, host):
    items = [posting(1, additionalPlain=OFFICE), posting(2, location="200 Park Avenue, New York, NY 10166")]
    serve_listing(transport, NORTH, items)
    serve_details(transport, NORTH, items)
    run = run_of(database, clock, demo, transport, host)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    read = get_posting(run, [ref(1)])

    text = observe(run, 3, 8, last=read).text

    # The excerpt is masked as the analysis would be; a place written as an address is withheld, never shown.
    assert "原文摘录" in read.summary
    assert "城市写了但没有认出：（有疑似地址，不显示）" in posting_line(run, run.found[ref(2)])
    assert not any(word in text for word in (*ADDRESS_WORDS, "Park Avenue"))


@pytest.mark.parametrize("why", ["unverified", "not_as_confirmed", "lead_source"])
def test_only_an_admitted_official_source_has_address_lines_masked(database, demo, clock, transport, host, why):
    office_board(transport)
    run = run_of(database, clock, demo, transport, host)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    found = run.found[ref(1)]
    get_posting(run, [ref(1)])
    assert _outgoing(run, found, OFFICE)[2] == ()

    if why == "unverified":
        adapters = {board.source_id: run.register.adapter(board.source_id) for board in (NORTH, HARBOR)}
        run.register = Register((lever.entry(NORTH), verified_entry(HARBOR)), adapters)
    elif why == "not_as_confirmed":
        run.grants[NORTH.source_id] = replace(run.grants[NORTH.source_id], name="另一个接收方")
    else:
        # Registered as giving leads, whatever its postings say of themselves.
        entry = replace(verified_entry(NORTH), evidence=EvidenceLevel.LEAD)
        adapters = {board.source_id: run.register.adapter(board.source_id) for board in (NORTH, HARBOR)}
        run.register = Register((entry, verified_entry(HARBOR)), adapters)

    assert _outgoing(run, found, "This is the official posting.\n" + OFFICE)[2] == (ADDRESS,)


# --- what a check says, and the frozen digest --------------------------------------------------------------


def test_a_check_says_still_published_when_checked_and_never_still_hiring(database, demo, clock, transport, host):
    office_board(transport)
    run = run_of(database, clock, demo, transport, host)
    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")

    checked = check_posting(run, [ref(1)])

    assert "核对时仍公开发布" in checked.summary
    assert "不代表企业还有名额" in agent.DECISION_INSTRUCTIONS
    assert not [path for path in APP.rglob("*") if path.suffix in (".py", ".html") and "仍在招聘" in path.read_text(encoding="utf-8")]


def test_the_frozen_digest_is_a_new_one_no_earlier_run_was_made_under():
    assert compare_tasks.taskset_digest() == compare_tasks.FROZEN_DIGEST
    # Rules 8, judge 4 and task set 2 (U3-D5 R2): a digest no earlier run was made under, U3-D2-E4 F1's (validation B2's)
    # and U3-D5's first and R1 ones (their offline evidence) included.
    assert rules.RULES_VERSION == "8"
    assert compare_tasks.FROZEN_DIGEST[:8] not in (
        "a82ff78f", "6c1ccdba", "9aee3ffa", "a02cade6", "f9e917cd", "f52d53a5", "422c3fbf", "bd772488"
    )


# --- D2-2 R1: masking never clears another stop (F1), and what is left of an address block stops it (F2) ----------

from app.analysis_contracts import Recipient  # noqa: E402,F401  (kept beside the fixtures that follow)
from app.discovery.contracts import Access, CheckAnswer, CheckStatus, PostingDetail  # noqa: E402
from app.discovery.sources import Permission, SourceEntry  # noqa: E402
from discovery_helpers import WITHIN, make_library  # noqa: E402

F1_TEXT = "Internal only: 200 Park Avenue\nRestricted hiring plan follows."
F2_TEXT = "200 Park Avenue\nUnit 400\nWe use Python for data work."
# Each stopped posting, and words of it no outbound step may show.
STOPPED = {"confidential_same_line": (F1_TEXT, "Restricted hiring plan"), "unit_continuation": (F2_TEXT, "Unit 400")}
BOARD = "fake-official"
P1 = PostingRef(BOARD, "P0001")


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


class Board:
    """A stand-in source registered as official and verified, whose one posting reads exactly as given: the words a
    test looks for come first, where an excerpt cut short would still show them."""

    def __init__(self, text: str):
        self.posting = Posting(P1, "数据分析实习生", "合成公司", ("北京",), False, "实习", "数据分析", None, None)
        self.text = text

    def search(self, query, timeout):
        return (self.posting,)

    def read(self, ref, timeout):
        return PostingDetail(self.posting, self.text, "https://example.invalid/p1")

    def check(self, ref, timeout):
        return CheckAnswer(CheckStatus.OPEN)


def board_run(database, library, clock, text: str, provider):
    entry = SourceEntry(BOARD, "合成官方来源", Access.SYNTHETIC, Permission.VERIFIED, EvidenceLevel.OFFICIAL, "测试", "2026-11-11")
    confirmation = confirmation_for(library, sources=(entry.grant,))

    return begin(database, clock, confirmation, provider, register=Register((entry,), {BOARD: Board(text)}))


def read_and_analyse(run):
    search_postings(run, BOARD, "数据分析", city="北京", **WITHIN)
    read = get_posting(run, [P1])
    decision = observe(run, 3, 8, last=read).text
    check_posting(run, [P1])

    return read, decision, analyze_posting(run, P1)


@pytest.mark.parametrize("case", sorted(STOPPED))
def test_a_stopped_posting_reaches_neither_the_analysis_nor_the_decision(database, library, clock, case):
    text, residue = STOPPED[case]
    provider = RecordingProvider()
    run = board_run(database, library, clock, text, provider)

    read, decision, result = read_and_analyse(run)

    assert (result.outcome, len(provider.prompts), run.analyses) == (Outcome.BLOCKED, 0, 0)
    # The words stood first in the posting: an excerpt letting them through would have shown them.
    assert "摘录不交给下一步" in read.summary and residue not in decision and "200 Park" not in decision


def test_a_safe_one_line_office_address_first_is_still_masked_and_the_rest_goes(database, library, clock):
    provider = RecordingProvider()
    run = board_run(database, library, clock, "200 Park Avenue\nWe use Python for data work.", provider)

    read, decision, result = read_and_analyse(run)

    assert f"「{ADDRESS_LINE} We use Python for data work.」" in read.summary and "200 Park" not in decision
    assert (result.outcome, len(provider.prompts)) == (Outcome.DONE, 2)
    assert all(ADDRESS_LINE in prompt and "200 Park" not in prompt for prompt in provider.prompts)


@pytest.mark.parametrize(
    "text",
    [
        F1_TEXT,
        # A marker on another line than the address: masking the address leaves it, and it stops the posting.
        "200 Park Avenue\nThis plan is confidential.",
        "200 Park Avenue\n办公地点：北京市海淀区中关村大街 27 号",
    ],
    ids=["same_line", "other_line", "chinese_address"],
)
def test_masking_an_address_line_never_clears_another_stop(text):
    masked, _, stopping = masked_official_posting(text)

    assert stopping and ADDRESS_LINE not in masked


@pytest.mark.parametrize(
    "text",
    [F2_TEXT, "Unit 400\n200 Park Avenue", "200 Park Avenue\nBuilding 5", "200 Park Avenue\n#400",
     "200 Park Avenue\n10166", "200 Park Avenue\nRoom 12, east wing"],
    ids=["unit_after", "unit_before", "building", "number_sign", "zip_alone", "room"],
)
def test_what_is_left_beside_a_masked_address_line_stops_the_posting(text):
    assert masked_official_posting(text)[2] == (ADDRESS,)


@pytest.mark.parametrize(
    "text",
    ["We met in Unit 4 of the course.", "200 Park Avenue\n\nUnit 4 of the course covers SQL.", OFFICE],
    ids=["unit_in_prose", "after_a_blank_line", "phone_after_the_block"],
)
def test_unit_and_room_words_elsewhere_are_not_stopped(text):
    masked, _, stopping = masked_official_posting(text)

    assert stopping == () and ADDRESS not in found_kinds("We met in Unit 4 of the course.")
