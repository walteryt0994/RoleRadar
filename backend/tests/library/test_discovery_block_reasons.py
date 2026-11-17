"""U3-D2-E3, offline: why an analysis was stopped, said as it was, and noted as metadata only.

Validation A's page said one sentence for every reason an analysis could be stopped for, and kept no note of which it
was. Here each reason is told apart on the result (tools.SearchRun.outcome, presentation.blocked_lines), and each
analysis tried is noted with codes and lengths only (tools.SearchRun.note_analysis): never a posting's words, its id
or a prompt. The outbound rules and the limits themselves are those of U3-D2-2 and U2, unchanged.

Two synthetic probes look into validation A's three stopped analyses without its postings, which were never kept:

- S1, length: Lever-shaped postings as large in bytes as the four read in A, each built a few ways (its words as
  HTML and plain text, or as opening and body too; some in lists; lighter or heavier markup), measured as the
  analysis measures a posting. Bytes alone do not say whether a posting was over 8,000 characters: under some ways
  the smallest is within and the others over, as A's one completed analysis would have it, under others all are
  within or all over. That H1 (too long) holds is not shown here, only when it could.
- S2, the outbound rules: one candidate wording each, through the rules an admitted official posting goes through,
  told by what they stop and what they mask. Which of them A's postings held is not known.

Nothing here is a finding about validation A's postings: the probes say what the rules would do, not what happened.
"""
import json

import pytest

from app.discovery import lever, lever_live, presentation
from app.discovery.contracts import EndReason, Limits, Unlisted
from app.discovery.tools import analyze_posting, check_posting, get_posting, search_postings
from app.job_analysis.scope import MAX_OUTBOUND_CHARS, MAX_POSTING_CHARS, clean_posting
from app.live_gate import Blocked, LiveBlocked
from app.outbound_review import masked_official_posting
from discovery_helpers import begin, confirmation_for
from job_helpers import RecordingProvider
from lever_helpers import (
    DEMO_SCOPE,
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
    verified_register,
)
from app.discovery.contracts import PostingRef

NOTE_KEYS = {"seq", "outcome", "posting_chars", "outgoing_chars", "material_chars", "masked", "reasons", "status", "problem", "requests_sent"}
SENTENCE = "Synthetic calibration text for one posting, not a real job. "
# Validation A's four reads, in bytes as its request record kept them, in the order read (the third one's analysis
# was the one that completed).
A_READS = (23_663, 28_286, 17_947, 24_508)


@pytest.fixture
def ticks():
    return Ticks()


@pytest.fixture
def transport(ticks):
    return ReplayTransport(ticks)


@pytest.fixture
def demo(database, owners, clock):
    return make_demo_library(database, owners, clock)


def ref(number: int = 1) -> PostingRef:
    return PostingRef(NORTH.source_id, pid(number, NORTH))


class Refusing(RecordingProvider):
    """Made up: the gate lets an analysis's request no further (its requests used up)."""

    def generate_text(self, *args, **kwargs):
        raise LiveBlocked(Blocked.REQUESTS)


def analysed_once(database, clock, demo, transport, ticks, item, *, provider=None, material_chars=None):
    """One posting found, read, checked and analysed, then the search ended: its outcome and its analysis notes."""
    serve_listing(transport, NORTH, [item])
    serve_details(transport, NORTH, [item])
    confirmation = confirmation_for(demo, scope=DEMO_SCOPE, sources=grants(), preferences=(SALES,), limits=Limits(refusals=5))
    run = begin(database, clock, confirmation, provider, register=verified_register(transport, guard(ticks)))
    notes = []
    run.diagnose = notes.append

    if material_chars is not None:
        run.material_chars = material_chars

    search_postings(run, NORTH.source_id, "Data Analyst", city="San Francisco")
    get_posting(run, [ref()])
    check_posting(run, [ref()])
    result = analyze_posting(run, ref())
    run.end(EndReason.ENOUGH)

    return run.outcome(), notes, result


LONG_WORDS = "Analyze transit data with Python and SQL for weekly dashboards. " * 140
PARAGRAPH = (
    "Interns join our team at 1 Main Street, New York, NY 10011, where they work with analysts on dashboards for "
    "public transit agencies across the region every week."
)


@pytest.mark.parametrize(
    "case, fields, key, line",
    [
        ("too_long", dict(descriptionPlain=LONG_WORDS), "too_long", f"岗位原文超过 {MAX_POSTING_CHARS:,} 字，什么都没有发送：1 个"),
        ("outbound_limit", {}, "outbound_limit", f"这次分析要发送的内容超过 {MAX_OUTBOUND_CHARS:,} 字，什么都没有发送：1 个"),
        ("address", dict(additionalPlain=PARAGRAPH), "address", "岗位原文里有疑似地址，什么都没有发送：1 个"),
        ("confidential", dict(additionalPlain="We build proprietary analytics tools."), "confidential", "岗位原文里有标明不公开的内容，什么都没有发送：1 个"),
        ("gate", {}, "gate_requests", "闸门没有放行这次分析的请求（批准的请求次数用完了），之前发出的请求照常计数：1 个"),
    ],
)
def test_each_reason_an_analysis_is_stopped_for_is_said_as_it_is_and_noted_as_metadata(database, demo, clock, transport, ticks, case, fields, key, line):
    item = posting(1, **fields)
    provider = Refusing() if case == "gate" else RecordingProvider()
    # All one analysis sends over the limit: the chosen material made long, as only a test makes it.
    material = MAX_OUTBOUND_CHARS - 100 if case == "outbound_limit" else None

    outcome, notes, result = analysed_once(database, clock, demo, transport, ticks, item, provider=provider, material_chars=material)

    assert outcome.blocked == ((key, 1),)
    assert presentation.blocked_lines(outcome.blocked) == [line]
    # Never the one sentence for every reason (U3-D2-E3): the reason that stopped it, and only that.
    assert Unlisted.BLOCKED in dict(outcome.unlisted) and presentation.UNLISTED_LABELS[Unlisted.BLOCKED] == "分析被拦下或原文不能外发"

    if case != "gate":
        # Nothing went out for what the rules or limits stopped.
        assert provider.prompts == []

    # One note, metadata only: what came of it as codes, and lengths.
    (note,) = notes
    assert set(note) <= NOTE_KEYS and note["seq"] == 1
    assert note["outcome"] == ("gate" if case == "gate" else "blocked") and note["reasons"] == [key]
    assert (note["posting_chars"] > MAX_POSTING_CHARS) is (case == "too_long")

    if material is not None:
        assert note["material_chars"] == material and note["outgoing_chars"] + material > MAX_OUTBOUND_CHARS

    raw = json.dumps(note, ensure_ascii=False)
    assert pid(1, NORTH) not in raw and "Data Analyst" not in raw and "Main Street" not in raw and "proprietary" not in raw


def test_an_analysis_that_went_out_is_noted_with_its_lengths_and_status_only(database, demo, clock, transport, ticks):
    provider = RecordingProvider()
    outcome, notes, result = analysed_once(database, clock, demo, transport, ticks, posting(1))
    (note,) = notes

    assert outcome.blocked == () and note["outcome"] == "analysed" and note["status"] == "complete"
    assert note["requests_sent"] == 2 and 0 < note["outgoing_chars"] <= note["posting_chars"] <= MAX_POSTING_CHARS
    assert set(note) <= NOTE_KEYS


def test_the_analysis_log_of_the_run_directory_keeps_whole_lines_and_stops_nothing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    terms = lever_live.SourceTerms("u3d2e-admit-2026-11-12-a", None, "u3d2e-src-2026-11-12-a", None, 12, 600.0, run_dir, tmp_path)
    record = lever_live.analysis_record(terms, "task-1")
    record({"seq": 1, "outcome": "blocked", "reasons": ["too_long"], "posting_chars": 9000})
    (line,) = (run_dir / lever_live.ANALYSIS_LOG).read_text(encoding="utf-8").splitlines()

    assert json.loads(line)["reasons"] == ["too_long"] and json.loads(line)["task_id"] == "task-1"

    # A line that cannot be written is left out, whole; nothing is raised to the search.
    def broken(handle, data):
        raise OSError("disk full")

    monkeypatch.setattr(lever_live, "_write_whole", broken)
    record({"seq": 2, "outcome": "blocked"})

    assert (run_dir / lever_live.ANALYSIS_LOG).read_text(encoding="utf-8").splitlines() == [line]


# --- S1: what bytes say of a posting's length ------------------------------------------------------------------


def sized(target: int, copies: int, lists_share: float, markup: int) -> tuple[int, int]:
    """A Lever-shaped posting about target bytes long as JSON, its words carried as HTML and plain text (copies 2), or
    as opening and body too (4), lists_share of them in lists, each HTML paragraph with markup bytes around it; its
    size in bytes and its length as the analysis measures a posting."""

    def item_for(sentences: int) -> dict:
        listed = int(sentences * lists_share)
        said = sentences - listed
        wrap = "<p>{}</p>" + " " * (markup - 7)
        fields = dict(
            description="".join(wrap.format(SENTENCE) for _ in range(said)), descriptionPlain=SENTENCE * said,
            opening="", openingPlain="", descriptionBody="", descriptionBodyPlain="",
        )

        if copies == 4:
            half = said // 2
            fields.update(
                opening="".join(wrap.format(SENTENCE) for _ in range(half)), openingPlain=SENTENCE * half,
                descriptionBody="".join(wrap.format(SENTENCE) for _ in range(said - half)),
                descriptionBodyPlain=SENTENCE * (said - half),
            )

        requirements = "".join(f"<li>{SENTENCE}</li>" for _ in range(listed)) or "<li>Python</li>"

        return posting(1, "Software Engineer", board=lever_live.BOARD, requirements=requirements, **fields)

    low, high = 1, 3_000

    while low < high:
        middle = (low + high) // 2

        if len(json.dumps(item_for(middle)).encode("utf-8")) < target:
            low = middle + 1
        else:
            high = middle

    item = item_for(low)
    text = clean_posting(lever.LeverSource(lever_live.BOARD)._facts(item).text)

    return len(json.dumps(item).encode("utf-8")), len(text)


@pytest.mark.parametrize(
    "copies, lists_share, markup, over",
    [
        # As validation A's one completed analysis would have it: the smallest within, the others over.
        (2, 0.0, 30, (True, True, False, True)),
        (4, 0.5, 7, (True, True, False, True)),
        # All within: H1 would not hold.
        (4, 0.0, 7, (False, False, False, False)),
        # All over: the smallest would have been stopped too, which it was not.
        (2, 0.0, 7, (True, True, True, True)),
    ],
)
def test_s1_bytes_alone_do_not_say_whether_a_posting_was_too_long(copies, lists_share, markup, over):
    measured = [sized(size, copies, lists_share, markup) for size in A_READS]

    # Each built within 2% of A's sizes (one sentence is about 1% of the smallest).
    assert all(abs(got - size) / size < 0.02 for (got, _), size in zip(measured, A_READS))
    assert tuple(chars > MAX_POSTING_CHARS for _, chars in measured) == over


# --- S2: what the outbound rules do with each wording ----------------------------------------------------------


@pytest.mark.parametrize(
    "text, stops, masks",
    [
        ("We build proprietary analytics tools for city services.", ("confidential",), ()),
        ("You will handle confidential information with care.", ("confidential",), ()),
        # Matched within words, case aside.
        ("Strict confidentiality applies to client work.", ("confidential",), ()),
        ("This document is internal only.", ("confidential",), ()),
        # NDA only as a whole word.
        ("Candidates sign an NDA before starting.", ("confidential",), ()),
        ("Our agenda is open to every intern.", (), ()),
        # An address within a long line stops it; on lines of its own, it is masked.
        (PARAGRAPH, ("address",), ()),
        ("1 Main Street, New York, NY 10011", (), ("address",)),
        ("Our office:\n1 Main Street\nSuite 400", (), ("address",)),
        ("P.O. Box 1234, New York, NY 10011", (), ("address",)),
        ("Call +1 (212) 555-0100 with questions.", (), ("phone",)),
        ("Email jobs@example.com with questions.", (), ("email",)),
    ],
)
def test_s2_each_wording_as_the_outbound_rules_treat_an_admitted_official_posting(text, stops, masks):
    _, masked, stopping = masked_official_posting(text)

    assert (tuple(stopping), tuple(masked)) == (stops, masks)
