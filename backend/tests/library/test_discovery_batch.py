"""U3-D6, offline: one batch of real reads and model requests (app.discovery.batch_live; D6 plan, revision 1, 3.4).

Nothing here reaches the network or a model: the board answers from synthetic postings inside this process
(PalantirReplay), the clocks are made up (Ticks, Wall), the model's approval stands as a test says (Gate). The
admissions and approvals are a test's own, under a home of its own beside its run directory: none is the Owner's,
and none admits a source or grants a permission of the product.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.discovery import batch_live, lever_live
from app.discovery.batch_live import (
    Batch,
    BatchRefused,
    BatchTermsError,
    Launch,
    OperationKind,
    OperationSpent,
    SavePermission,
)
from app.discovery.contracts import PostingRef
from app.discovery.lever import API_HOST, PAGE_SIZE, Reply, TransportError, TransportTimeout
from app.discovery.runner import ApprovalsHeld
from app.discovery.sources import HeldBack, SourceAnomaly, SourceError
from lever_helpers import JSON_TYPE, Ticks, posting
from test_live_gate import synthetic_prices, terms as gate_terms

TODAY = date(2026, 11, 20)
BOARD = lever_live.BOARD
ROBOTS = b"User-agent: *\nAllow: /\nCrawl-delay: 1\n"
ADMISSION = "u3d6-admit-2026-11-20-a"
SOURCE_APPROVAL = "u3d6-src-2026-11-20-a"
MODEL_APPROVAL = "u3d6-ai-2026-11-20-a"
DIRECTION_APPROVAL = "u3d6-dir-2026-11-20-a"
# The D6 proposal's prices (D6 plan, 9.3): reserved at $10 a million input tokens, $20 output.
PRICES = synthetic_prices(Decimal("4"), Decimal("0.40"), Decimal("5"), Decimal("20"), reserve=Decimal("10"))
SEARCH_WORST = 8 * 200_000 + 2 * 240_000


def postings() -> list[dict]:
    return [posting(number, board=BOARD) for number in (1, 2, 3)]


class PalantirReplay:
    """The board inside this process: robots, the list's pages and each posting, from synthetic postings. answers
    overrides an address's reply (a Reply, or an exception to raise); urls keeps every request it was handed."""

    def __init__(self, items=None, *, robots: bytes = ROBOTS):
        self.items = postings() if items is None else items
        self.robots = robots
        self.answers: dict[str, list] = {}
        self.urls: list[str] = []

    def get(self, url: str, timeout: float) -> Reply:
        self.urls.append(url)
        queue = self.answers.get(url)

        if queue:
            answer = queue.pop(0)

            # An event at the very moment the request arrives (D6-0 R1): it runs, then the reply is its own or, for
            # None, the board's as usual.
            if callable(answer):
                answer = answer()

            if isinstance(answer, BaseException):
                raise answer

            if answer is not None:
                return answer

        if url == f"https://{API_HOST}/robots.txt":
            return Reply(200, "text/plain; charset=utf-8", self.robots)

        prefix = f"https://{API_HOST}/v0/postings/{BOARD.site}"

        if url.startswith(prefix + "?"):
            skip = int(url.split("skip=")[1].split("&")[0])

            return Reply(200, JSON_TYPE, json.dumps(self.items[skip : skip + PAGE_SIZE]).encode("utf-8"))

        item = next((item for item in self.items if url == f"{prefix}/{item['id']}"), None)

        return Reply(200, JSON_TYPE, json.dumps(item).encode("utf-8")) if item else Reply(404, JSON_TYPE, b'{"ok":false}')

    def answer(self, url: str, *answers) -> None:
        self.answers.setdefault(url, []).extend(answers)


def detail(number: int) -> str:
    return f"https://{API_HOST}/v0/postings/{BOARD.site}/{postings()[number - 1]['id']}"


def ref(number: int) -> PostingRef:
    return PostingRef(BOARD.source_id, postings()[number - 1]["id"])


class Wall:
    """A made-up wall clock, moved by the tests (and with Ticks, so both run together)."""

    def __init__(self, ticks: Ticks):
        self.ticks = ticks
        self.start = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.start + timedelta(seconds=self.ticks.now - 100.0)


class Gate:
    """The model's approval as a gate holds it: its terms, the reserve policy and how it stands now."""

    def __init__(self, approval, *, requests_left: int = 20, micro_usd_left: int = 4_500_000):
        self.terms = approval
        self.policy = SimpleNamespace(reserve_input_tokens=16_000)
        self.state = SimpleNamespace(paused_reason=None, requests_left=requests_left, micro_usd_left=micro_usd_left)
        # Other approvals of the ledger paused, as live_gate.LiveGate.paused_among reads them.
        self.paused_others: dict[str, str] = {}
        # What the ledger holds settled for each approval (requests, micro-USD), as LiveGate.standing_among reads it.
        self.settled: dict[str, tuple[int, int]] = {}
        # Every request the ledger holds of each approval: request id → (state, reservation).
        self.requests: dict[str, dict[str, tuple[str, int]]] = {}

    def status(self):
        return self.state

    def paused_among(self, approval_ids) -> dict[str, str]:
        return {approval_id: why for approval_id, why in self.paused_others.items() if approval_id in set(approval_ids)}

    def standing_among(self, approval_ids):
        own = self.terms.approval_id

        return {
            approval_id: SimpleNamespace(
                paused_reason=self.state.paused_reason if approval_id == own else self.paused_others.get(approval_id),
                final_requests=self.settled.get(approval_id, (0, 0))[0],
                final_micro_usd=self.settled.get(approval_id, (0, 0))[1],
                requests=dict(self.requests.get(approval_id, {})),
            )
            for approval_id in approval_ids
        }


# The library of a test's launch, as web_main names it to the batch (read_library_id); made up.
LIBRARY = "synthetic-library-1"


class Flag:
    def __init__(self):
        self.set_ = False

    def is_set(self) -> bool:
        return self.set_


def approval(purpose: str = "discovery", approval_id: str = MODEL_APPROVAL, **changes):
    values = {
        "approval_id": approval_id,
        "purposes": frozenset({purpose}),
        "provider": "openai",
        "model": "gpt-5.6-sol",
        "endpoint": "https://api.openai.com/v1",
        "max_output_tokens": 8_000 if purpose == "direction" else 4_000,
        "max_requests": 1 if purpose == "direction" else 20,
        "max_cost_micro_usd": 400_000 if purpose == "direction" else 4_500_000,
        "prices": PRICES,
    }
    values.update(changes)

    return gate_terms(**values)


def environ(run_dir: Path, **changes) -> dict[str, str]:
    values = {
        "HOME": str(run_dir.parent / "home"),
        "ROLERADAR_BATCH_RUN_DATE": TODAY.isoformat(),
        "ROLERADAR_BATCH_RUN_DIR": str(run_dir),
        "ROLERADAR_BATCH_ADMISSION_ID": ADMISSION,
        "ROLERADAR_BATCH_ADMISSION_UNTIL": "2026-12-10",
        "ROLERADAR_BATCH_SOURCE_APPROVAL_ID": SOURCE_APPROVAL,
        "ROLERADAR_BATCH_BOARD": BOARD.site,
        "ROLERADAR_BATCH_MAX_REQUESTS": "50",
        "ROLERADAR_BATCH_SAVE": "facts",
    }
    values.update(changes)

    return {name: value for name, value in values.items() if value is not None}


@pytest.fixture
def run_dir(tmp_path) -> Path:
    folder = tmp_path / "run"
    folder.mkdir()

    return folder


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def board() -> PalantirReplay:
    return PalantirReplay()


class Setup:
    """A test's launch: load, gate, transport, clocks; again() is a restart in the same run directory."""

    def __init__(self, run_dir: Path, ticks: Ticks, board: PalantirReplay):
        self.run_dir, self.ticks, self.board = run_dir, ticks, board
        self.day = TODAY
        self.gates: list[Gate] = []

    def load(self, launch: Launch = Launch.DISCOVERY, *, model=None, gate=None, library: str = LIBRARY, **changes) -> Batch:
        model = model or (approval("direction", DIRECTION_APPROVAL) if launch is Launch.DIRECTION else approval())
        batch = batch_live.load(
            environ(self.run_dir, **changes),
            launch,
            model,
            today=lambda: self.day,
            monotonic=self.ticks.monotonic,
            sleep=self.ticks.sleep,
            wall=Wall(self.ticks),
            create=lambda: self.board,
        )
        gate = gate or Gate(model)
        self.gates.append(gate)

        try:
            batch.attach_gate(gate, library)
        except BaseException:
            # As the app does (web_main.create_app): a launch refused gives the run directory's lock back.
            batch.close()
            raise

        return batch


@pytest.fixture
def setup(run_dir, ticks, board) -> Setup:
    return Setup(run_dir, ticks, board)


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def handed(run_dir: Path) -> int:
    return sum(1 for line in lines(run_dir / "requests.jsonl") if line["state"] == "handed")


def adapter(batch: Batch):
    return batch.register().adapter(BOARD.source_id)


def search(batch: Batch, op: str = "s1", owner: str = "user-a", reads=(1,)):
    """One search's reads, as the tools make them: the list, then each posting read; the operation ended."""
    operation = batch.begin(OperationKind.SEARCH, owner, op)

    try:
        source = adapter(batch)
        source.listing(30.0)

        for number in reads:
            source.read(ref(number), 30.0)
    finally:
        batch.end(operation)

    return operation


# --- the terms and the start --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"ROLERADAR_BATCH_SOURCE_APPROVAL_ID": "u3d2e-src-2026-11-20-a"}, "u3d6-src-"),
        ({"ROLERADAR_BATCH_SOURCE_APPROVAL_ID": "u3d5a-2026-11-20-a"}, "u3d6-src-"),
        ({"ROLERADAR_BATCH_ADMISSION_ID": "u3d2e-admit-2026-11-12-a"}, "u3d6-admit-"),
        ({"ROLERADAR_BATCH_BOARD": "zilliz"}, "只能读取 palantir"),
        ({"ROLERADAR_BATCH_MAX_REQUESTS": "51"}, "1 到 50"),
        ({"ROLERADAR_BATCH_SAVE": "everything"}, "none、facts 或 facts_quotes"),
        ({"ROLERADAR_BATCH_RUN_DATE": "2026-11-21"}, "日期和运行日不一致"),
        ({"ROLERADAR_BATCH_ADMISSION_UNTIL": "2026-12-31"}, "最长 31 天"),
    ],
)
def test_terms_not_as_they_must_be_keep_the_batch_from_starting(setup, changes, problem):
    with pytest.raises(BatchTermsError) as refused:
        setup.load(**changes)

    assert problem in str(refused.value)
    assert not (setup.run_dir / "tasks.jsonl").exists()


def test_old_model_approvals_and_other_purposes_are_not_taken(setup):
    with pytest.raises(BatchTermsError, match="u3d6-ai-"):
        setup.load(model=approval(approval_id="u3d2e-ai-2026-11-20-a"))

    with pytest.raises(BatchTermsError, match="u3d6-ai-"):
        setup.load(model=approval(approval_id="u3d5a-2026-11-20-a"))

    with pytest.raises(BatchTermsError, match="只能写 discovery"):
        setup.load(model=approval(purposes=frozenset({"discovery", "direction"})))

    with pytest.raises(BatchTermsError, match="只能写 direction"):
        setup.load(Launch.DIRECTION, model=approval("discovery", DIRECTION_APPROVAL))


def test_the_save_permission_is_none_unless_the_admission_says_so(setup):
    batch = setup.load(ROLERADAR_BATCH_SAVE=None)

    assert batch.source.save is SavePermission.NONE
    assert batch_live.keep_of(batch.source) == (frozenset(), frozenset())
    batch.close()

    for save, analyses in (("facts", frozenset()), ("facts_quotes", frozenset({BOARD.source_id}))):
        batch = setup.load(ROLERADAR_BATCH_SAVE=save)
        assert batch_live.keep_of(batch.source) == (frozenset({BOARD.source_id}), analyses)
        batch.close()


def test_a_second_process_on_the_same_run_directory_is_kept_out(setup):
    first = setup.load()

    with pytest.raises(BatchTermsError, match="同一时间只能由一个进程使用"):
        setup.load()

    first.close()
    setup.load().close()


# --- one batch, many operations -------------------------------------------------------------------------------------


def test_two_searches_share_the_approvals_and_nothing_resets(setup):
    batch = setup.load()
    first = search(batch, "s1", reads=(1, 2))
    gate = setup.gates[-1]
    gate.state.requests_left, gate.state.micro_usd_left = 15, 4_400_000
    second = search(batch, "s2", reads=(3,))

    # robots once a launch, the list's one page, the reads: counted for the batch across both searches.
    assert (first.sent, second.sent) == (4, 2)
    assert batch.sent == handed(setup.run_dir) == 6
    assert first.robots == 1 and second.robots == 0


def test_the_operations_of_a_day_share_one_read_approval_and_its_count(setup):
    batch = setup.load()
    search(batch, "s1", reads=(1,))

    for kind, op, calls in (
        (OperationKind.RECHECK, "r1", ("read", "check")),
        (OperationKind.APPLY, "a1", ("read", "check")),
        (OperationKind.SUPPLY, "l1", ("check",)),
    ):
        operation = batch.begin(kind, "user-a", op)
        source = adapter(batch)

        for call in calls:
            getattr(source, call)(ref(2), 30.0)

        batch.end(operation)

    watch = batch.begin(OperationKind.WATCH, "user-a", "w1")
    adapter(batch).listing(30.0)
    batch.end(watch)

    assert batch.sent == handed(setup.run_dir) == 3 + 2 + 2 + 1 + 1
    assert [line["op_id"] for line in lines(setup.run_dir / "tasks.jsonl") if line["state"] == "end"] == ["s1", "r1", "a1", "l1", "w1"]


def test_an_operation_starts_only_when_what_is_left_holds_its_own_most_and_robots_on_top(setup):
    batch = setup.load(ROLERADAR_BATCH_MAX_REQUESTS="11")

    # A search's own most is 11; the launch's robots is reserved on top: 12 > 11, so it does not start.
    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.SEARCH, "user-a", "s1")

    assert refused.value.code == "requests"
    assert setup.board.urls == [] and not (setup.run_dir / "requests.jsonl").exists()

    # A check (3) with robots (1) fits; robots once read, the next check needs its own 3 alone.
    check = batch.begin(OperationKind.CHECK, "user-a", "c1")
    assert check.budget == 4
    adapter(batch).read(ref(1), 30.0)
    batch.end(check)
    again = batch.begin(OperationKind.CHECK, "user-a", "c2")
    assert again.budget == 3
    batch.end(again)


def test_what_the_batch_used_counts_against_the_next_operations_own_most(setup):
    batch = setup.load(ROLERADAR_BATCH_MAX_REQUESTS="6")
    check = batch.begin(OperationKind.CHECK, "user-a", "c1")
    source = adapter(batch)

    for number in (1, 2, 3):
        source.read(ref(number), 30.0)

    batch.end(check)
    assert batch.sent == 4

    # Two left: a check, whose own most is 3, does not start, and nothing is sent (the request's own check is a
    # second hold behind this one: test_the_batch_total_holds_at_each_request_whatever_was_reserved).
    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-a", "c2")

    assert refused.value.code == "requests" and "剩下 2 次" in str(refused.value)
    assert handed(setup.run_dir) == 4 and batch.current is None


def test_an_operation_stops_at_its_own_most_without_anyone_cancelling_and_the_batch_goes_on(setup):
    batch = setup.load()
    check = batch.begin(OperationKind.CHECK, "user-a", "c1")
    source = adapter(batch)

    for number in (1, 2, 3):
        source.read(ref(number), 30.0)

    # robots and three reads: its own 3 plus robots; the fifth asked for is not sent.
    with pytest.raises(OperationSpent):
        source.read(ref(1), 30.0)

    batch.end(check)
    assert batch.sent == handed(setup.run_dir) == 4
    assert batch.problem() is None

    nxt = batch.begin(OperationKind.CHECK, "user-a", "c2")
    adapter(batch).read(ref(1), 30.0)
    batch.end(nxt)
    assert batch.sent == 5


def test_the_batch_is_its_first_users_and_another_user_sends_nothing(setup):
    batch = setup.load()
    search(batch, "s1", owner="user-a")

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-b", "c1")

    assert refused.value.code == "owner"
    # robots, the list and one read: the other user's operation added nothing.
    assert handed(setup.run_dir) == 3


def test_one_operation_at_a_time(setup):
    batch = setup.load()
    running = batch.begin(OperationKind.SEARCH, "user-a", "s1")

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-a", "c1")

    assert refused.value.code == "busy"
    batch.end(running)
    batch.end(batch.begin(OperationKind.CHECK, "user-a", "c1"))


def test_a_cancelled_operation_sends_nothing_more_and_the_next_goes_on(setup):
    batch = setup.load()
    flag = Flag()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1", flag)
    source = adapter(batch)
    source.listing(30.0)
    flag.set_ = True

    with pytest.raises(HeldBack):
        source.read(ref(1), 30.0)

    batch.end(operation, "cancelled")
    assert handed(setup.run_dir) == 2 and batch.problem() is None
    search(batch, "s2")
    assert batch.sent == 4


def test_requests_keep_two_seconds_apart_across_operations(setup, ticks):
    batch = setup.load()
    check = batch.begin(OperationKind.CHECK, "user-a", "c1")
    adapter(batch).read(ref(1), 30.0)
    batch.end(check)
    ticks.slept.clear()
    ticks.now += 0.5
    other = batch.begin(OperationKind.CHECK, "user-a", "c2")
    adapter(batch).read(ref(2), 30.0)
    batch.end(other)

    assert ticks.slept == [pytest.approx(1.5)]


def test_failures_in_a_row_count_across_operations_and_stop_the_batch(setup):
    batch = setup.load()
    first = batch.begin(OperationKind.CHECK, "user-a", "c1")
    source = adapter(batch)
    source.read(ref(1), 30.0)
    setup.board.answer(detail(2), TransportTimeout("slow"))

    with pytest.raises(SourceError):
        source.read(ref(2), 30.0)

    batch.end(first)
    assert batch.problem() is None and batch.failures == 1

    second = batch.begin(OperationKind.CHECK, "user-a", "c2")
    setup.board.answer(detail(3), TransportError("down"))

    with pytest.raises(SourceError):
        adapter(batch).read(ref(3), 30.0)

    batch.end(second)
    assert batch.problem()[0] == "stopped"
    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == "failures"

    with pytest.raises(BatchRefused):
        batch.begin(OperationKind.CHECK, "user-a", "c3")


@pytest.mark.parametrize(
    "stop, code",
    [
        ("robots", "source"),
        ("status", "refused"),
        ("forbidden", "refused"),
        ("redirect", "source"),
        ("identity", "source"),
        ("live_stop", "live_stop"),
    ],
)
def test_a_stop_of_the_source_stops_the_batch_and_its_model_for_good(setup, stop, code):
    if stop == "robots":
        setup.board.robots = b"User-agent: *\nDisallow: /\n"

    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    first_page = f"https://{API_HOST}/v0/postings/{BOARD.site}?mode=json&skip=0&limit={PAGE_SIZE}"
    # 429 and 403 ask for a stop; a redirect is never followed and is no documented reply; another posting's content
    # under this one's address is not this posting (D6 plan, 3.2: each stops the whole batch).
    replies = {
        "status": Reply(429, JSON_TYPE, b"{}"),
        "forbidden": Reply(403, JSON_TYPE, b"{}"),
        "redirect": Reply(302, "text/html; charset=utf-8", b""),
    }

    if stop in replies:
        setup.board.answer(first_page, replies[stop])

    if stop == "identity":
        setup.board.answer(detail(1), Reply(200, JSON_TYPE, json.dumps(postings()[1]).encode("utf-8")))

    if stop == "live_stop":
        (setup.run_dir / "LIVE_STOP").touch()

    with pytest.raises((SourceAnomaly, SourceError)):
        if stop == "identity":
            adapter(batch).read(ref(1), 30.0)
        else:
            adapter(batch).listing(30.0)

    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == code
    # The model's next request is kept back too: one stop stops both sides.
    assert binding.refusal() is not None
    binding.release()

    with pytest.raises(BatchRefused):
        batch.begin(OperationKind.CHECK, "user-a", "c1")

    batch.close()

    if stop == "live_stop":
        (setup.run_dir / "LIVE_STOP").unlink()

    # A restart does not lift it.
    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()


def test_a_paused_model_approval_stops_the_source_too_and_a_restart_does_not_lift_it(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")
    setup.gates[-1].state.paused_reason = "unknown_usage"

    with pytest.raises(HeldBack):
        adapter(batch).read(ref(1), 30.0)

    batch.end(operation)
    assert setup.board.urls == []
    batch.close()
    gate = Gate(approval())
    gate.state.paused_reason = "interrupted"

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load(gate=gate)


def test_a_model_request_in_flight_at_a_crash_pauses_its_approval_and_no_restart_goes_on(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    batch_live.Binding(batch, operation).bind()
    batch.close()  # the process ended with a model request out: nothing ended the operation
    # Reopened, the gate pauses the approval for the reservation left unsettled (live_gate's recovery): the batch,
    # finding it so at the restart, stops for good, before anything is sent.
    gate = Gate(approval())
    gate.state.paused_reason = "interrupted"

    with pytest.raises(BatchTermsError, match="已经暂停"):
        setup.load(gate=gate)

    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == "model_paused"

    # Even with the gate standing again, the stop holds.
    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()

    assert setup.board.urls == []


def test_the_batch_total_holds_at_each_request_whatever_was_reserved(setup):
    batch = setup.load()
    search(batch, "s1")
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")
    urls = list(setup.board.urls)
    # As if the batch's total were reached by the time of the request: begin reserved it, and the guard holds anyway.
    batch.sent = batch.source.max_requests

    with pytest.raises(SourceError, match="读取批准的请求次数用完了"):
        adapter(batch).read(ref(1), 30.0)

    batch.end(operation)
    assert setup.board.urls == urls and handed(setup.run_dir) == 3


def test_an_admission_past_its_date_does_not_start(setup):
    # The day after the admission's last: the launch is refused, whatever the read approval says. While it runs the
    # admission cannot lapse before the run date does (its last day is never before the run date at the start), and
    # the run date passing keeps everything back (the next test).
    with pytest.raises(BatchTermsError, match="已经过期"):
        setup.load(ROLERADAR_BATCH_ADMISSION_UNTIL="2026-11-19")

    assert setup.board.urls == [] and not (setup.run_dir / "requests.jsonl").exists()
    assert not (setup.run_dir.parent / "home" / ".roleradar").exists()


def test_the_run_date_passing_keeps_everything_back(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")
    setup.day = TODAY + timedelta(days=1)

    with pytest.raises(HeldBack):
        adapter(batch).read(ref(1), 30.0)

    batch.end(operation)

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-a", "c2")

    assert refused.value.code == "not_today"


# --- the model's side -----------------------------------------------------------------------------------------------


def test_a_search_starts_only_when_the_model_approval_holds_its_most(setup):
    batch = setup.load()
    setup.gates[-1].state.micro_usd_left = SEARCH_WORST - 1

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.SEARCH, "user-a", "s1")

    assert refused.value.code == "model" and "2.08" in str(refused.value)
    setup.gates[-1].state.micro_usd_left = SEARCH_WORST
    setup.gates[-1].state.requests_left = 9

    with pytest.raises(BatchRefused):
        batch.begin(OperationKind.SEARCH, "user-a", "s1")

    setup.gates[-1].state.requests_left = 10
    batch.end(batch.begin(OperationKind.SEARCH, "user-a", "s1"))


def test_the_approvals_are_claimed_once_for_the_batch_and_held_across_its_operations(setup):
    batch = setup.load()
    first = batch.begin(OperationKind.SEARCH, "user-a", "s1")

    # Nothing may go to the model before the approvals are claimed for the batch: bind comes first, always.
    assert batch_live.Binding(batch, first).refusal()[0] == "unbound"
    batch_live.Binding(batch, first).bind()
    batch.end(first)
    second = batch.begin(OperationKind.SEARCH, "user-a", "s2")
    binding = batch_live.Binding(batch, second)
    binding.bind()

    assert binding.refusal() is None
    home = setup.run_dir.parent / "home" / ".roleradar" / "batch-approvals"
    claims = {path.name: json.loads(path.read_text()) for path in home.glob("*.claim")}

    assert set(claims) == {f"{MODEL_APPROVAL}.claim", f"{SOURCE_APPROVAL}.claim"}
    assert {claim["batch_id"] for claim in claims.values()} == {batch.batch_id}
    assert {claim["owner_id"] for claim in claims.values()} == {"user-a"}
    assert {claim["run_dir"] for claim in claims.values()} == {str(setup.run_dir)}
    binding.release()


def test_an_ended_operation_cannot_send_to_the_model(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    binding.release()

    assert binding.refusal()[0] == "not_current"

    with pytest.raises(ApprovalsHeld):
        binding.bind()


def test_the_two_launches_are_one_batch_for_one_user_in_one_run_directory(setup):
    direction = setup.load(Launch.DIRECTION)
    explored = direction.begin(OperationKind.DIRECTION, "user-a", "d1")
    batch_live.Binding(direction, explored).bind()
    direction.end(explored)
    batch_id = direction.batch_id
    direction.close()  # the direction launch stopped with Ctrl-C

    # The discovery launch, in the same run directory on the same day: the same batch and user, its own approvals.
    discovery = setup.load()
    assert (discovery.batch_id, discovery.owner_id) == (batch_id, "user-a")

    with pytest.raises(BatchRefused) as refused:
        discovery.begin(OperationKind.CHECK, "user-b", "c1")

    assert refused.value.code == "owner"
    searching = discovery.begin(OperationKind.SEARCH, "user-a", "s1")
    batch_live.Binding(discovery, searching).bind()
    adapter(discovery).listing(30.0)
    discovery.end(searching)
    discovery.close()
    home = setup.run_dir.parent / "home" / ".roleradar" / "batch-approvals"
    claims = {path.stem: json.loads(path.read_text()) for path in home.glob("*.claim")}

    assert set(claims) == {DIRECTION_APPROVAL, MODEL_APPROVAL, SOURCE_APPROVAL}
    assert {claim["batch_id"] for claim in claims.values()} == {batch_id}
    assert [line["kind"] for line in lines(setup.run_dir / "tasks.jsonl") if line["state"] == "end"] == ["direction", "search"]

    # The direction launch does not come back once the source was read: its approvals never named the reads.
    with pytest.raises(BatchTermsError, match="没有读取批准"):
        setup.load(Launch.DIRECTION)


def test_a_pause_of_the_other_launchs_model_approval_stops_the_whole_batch(setup):
    direction = setup.load(Launch.DIRECTION)
    explored = direction.begin(OperationKind.DIRECTION, "user-a", "d1")
    batch_live.Binding(direction, explored).bind()
    direction.close()  # the process ended with the exploration's request out
    # The discovery launch's gate, opened on the same ledger, recovered that request and paused its approval.
    gate = Gate(approval())
    gate.paused_others = {DIRECTION_APPROVAL: "interrupted"}

    with pytest.raises(BatchTermsError, match=f"{DIRECTION_APPROVAL} 已经暂停"):
        setup.load(gate=gate)

    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == "model_paused"

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()

    assert setup.board.urls == []


def test_a_pause_seen_as_a_model_operation_ends_is_written_down_at_once(setup):
    direction = setup.load(Launch.DIRECTION)
    explored = direction.begin(OperationKind.DIRECTION, "user-a", "d1")
    batch_live.Binding(direction, explored).bind()
    # Its one request settled over its reservation: the gate paused the approval.
    setup.gates[-1].state.paused_reason = "over_reserve"
    direction.end(explored)

    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == "model_paused"
    direction.close()

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()


def test_an_exploration_is_the_direction_launchs_alone(setup):
    batch = setup.load(Launch.DIRECTION)

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.SEARCH, "user-a", "s1")

    assert refused.value.code == "purpose"
    operation = batch.begin(OperationKind.DIRECTION, "user-a", "d1")
    batch_live.Binding(batch, operation).bind()
    assert batch.model_need(OperationKind.DIRECTION) == (1, 320_000)
    batch.end(operation)


# --- restarts and records ---------------------------------------------------------------------------------------------


def test_a_restart_in_the_same_run_directory_goes_on_from_what_was_used(setup, ticks):
    batch = setup.load()
    search(batch, "s1", reads=(1, 2))
    assert batch.sent == 4
    batch.close()
    ticks.slept.clear()
    ticks.now += 0.25
    again = setup.load()

    assert again.sent == 4 and again.batch_id == batch.batch_id and again.owner_id == "user-a"
    # robots again for the new launch, on top; the spacing from the last request before the restart holds.
    check = again.begin(OperationKind.CHECK, "user-a", "c1")
    assert check.robots == 1
    adapter(again).read(ref(3), 30.0)
    again.end(check)
    assert ticks.slept[0] == pytest.approx(1.75)
    assert again.sent == handed(setup.run_dir) == 6


def test_an_operation_running_at_a_crash_stays_used_and_is_recorded_as_interrupted(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    batch_live.Binding(batch, operation).bind()
    adapter(batch).listing(30.0)
    batch.close()  # the process ended without ending the operation
    again = setup.load()

    assert again.sent == 2
    assert lines(setup.run_dir / "tasks.jsonl")[-1]["state"] == "interrupted"
    # The interrupted search does not come back: a new search is a new operation, confirmed again.
    assert again.current is None


def test_a_request_whose_outcome_is_unknown_counts(setup):
    batch = setup.load()
    search(batch, "s1")
    batch.close()
    log = setup.run_dir / "requests.jsonl"
    entries = lines(log)
    # The last request handed with no result: as if the process ended while it was out.
    kept = [line for line in entries if not (line["state"] == "answered" and line["seq"] == entries[-1]["seq"])]
    log.write_text("".join(json.dumps(line) + "\n" for line in kept), encoding="utf-8")
    again = setup.load()

    assert again.sent == 3


@pytest.mark.parametrize("damage", ["claim_local", "claim_home", "requests_line", "requests_gone", "tasks_gone", "foreign_batch"])
def test_records_missing_damaged_or_not_matching_keep_the_batch_from_going_on(setup, damage):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    batch_live.Binding(batch, operation).bind()
    adapter(batch).listing(30.0)
    batch.end(operation)
    batch.close()
    home = setup.run_dir.parent / "home" / ".roleradar" / "batch-approvals" / f"{SOURCE_APPROVAL}.claim"

    if damage == "claim_local":
        (setup.run_dir / "approvals" / f"{SOURCE_APPROVAL}.json").unlink()
    elif damage == "claim_home":
        record = json.loads(home.read_text())
        os.chmod(home, 0o600)
        home.write_text(json.dumps({**record, "batch_id": "someone-else"}))
    elif damage == "requests_line":
        with open(setup.run_dir / "requests.jsonl", "a", encoding="utf-8") as out:
            out.write('{"state": "handed", "seq":\n')
    elif damage == "requests_gone":
        (setup.run_dir / "requests.jsonl").unlink()
    elif damage == "tasks_gone":
        (setup.run_dir / "tasks.jsonl").unlink()
    else:
        with open(setup.run_dir / "tasks.jsonl", "a", encoding="utf-8") as out:
            out.write(json.dumps({"op_id": "x", "state": "begin", "batch_id": "other", "owner_id": "user-a"}) + "\n")

    with pytest.raises(BatchTermsError):
        setup.load()


def test_a_record_changed_while_the_batch_runs_stops_it_before_the_next_operation(setup):
    batch = setup.load()
    search(batch, "s1")
    (setup.run_dir / "requests.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-a", "c1")

    assert refused.value.code == "records"
    assert json.loads((setup.run_dir / "batch-stop.json").read_text())["code"] == "records"


def test_the_same_approvals_from_another_run_directory_are_refused(setup, tmp_path, ticks, board):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    batch_live.Binding(batch, operation).bind()
    batch.end(operation)
    batch.close()
    other = tmp_path / "copy"
    other.mkdir()

    for path in setup.run_dir.iterdir():
        if path.is_dir():
            (other / path.name).mkdir()

            for item in path.iterdir():
                (other / path.name / item.name).write_bytes(item.read_bytes())
        elif path.name != "batch.lock":
            (other / path.name).write_bytes(path.read_bytes())

    elsewhere = Setup(other, ticks, board)

    with pytest.raises(BatchTermsError, match="别的运行目录"):
        elsewhere.load()

    # A new folder holding nothing of the batch: refused all the same, and said so.
    fresh = tmp_path / "fresh"
    fresh.mkdir()

    with pytest.raises(BatchTermsError, match="别的运行目录"):
        Setup(fresh, ticks, board).load()

    assert sorted(path.name for path in fresh.iterdir()) == ["batch.lock"]


def test_a_record_that_cannot_be_written_keeps_every_request_back(setup, monkeypatch):
    batch = setup.load()
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")

    def refuse(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(batch_live, "_append_bytes", refuse)

    with pytest.raises(HeldBack):
        adapter(batch).read(ref(1), 30.0)

    assert setup.board.urls == []
    assert batch.problem()[0] == "unrecorded"


def test_records_hold_metadata_only(setup):
    batch = setup.load()
    search(batch, "s1", reads=(1, 2))
    text = "".join(path.read_text(encoding="utf-8") for path in setup.run_dir.glob("*.jsonl"))

    for item in postings():
        assert item["id"] not in text and item["text"] not in text

    assert "https://" not in text


# --- the records whole, and the ledger the batch's (D6-0 R1: Codex's F1 and F2) --------------------------------------
#
# Each of Codex's independent cases (/private/tmp/roleradar-d6-review-jN7uqX/test_d6_review.py, run as it is) is here
# too, with the edges around it: a record cut at a whole line, put back with another, damaged, added to or gone, at the
# start and while an operation runs; a claim of either launch; HOME's progress; the ledger put back or another
# library's; and the crashes a restart must still go on from.

LEDGER = Path(".roleradar", "batch-approvals")


def home_of(setup) -> Path:
    return setup.run_dir.parent / "home" / LEDGER


def progress_of(setup, batch_id: str) -> Path:
    return home_of(setup) / f"{batch_id}.progress.jsonl"


def files_of(folder: Path) -> dict:
    """Every file under folder and its bytes, but the lock's (taken at every start)."""
    return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file() and path.name != "batch.lock"}


def stopped_for_good(setup, batch_id: str, code: str | None = None) -> None:
    record = json.loads((setup.run_dir / "batch-stop.json").read_text(encoding="utf-8"))

    assert code is None or record["code"] == code
    assert json.loads((home_of(setup) / f"{batch_id}.stop.json").read_text(encoding="utf-8"))["batch_id"] == batch_id


def test_a_request_record_cut_at_a_whole_line_does_not_restart_and_is_left_as_found(setup):
    batch = setup.load()
    search(batch)
    assert batch.sent == 3
    batch.close()
    log = setup.run_dir / "requests.jsonl"
    (claimed,) = [line for line in lines(log) if line["state"] == "claimed"]
    # Cut back to a whole, valid line: the task record and both claims as they were.
    log.write_text(json.dumps(claimed) + "\n", encoding="utf-8")
    found = files_of(setup.run_dir.parent)

    with pytest.raises(BatchTermsError, match="对不上|截断"):
        setup.load()

    # Refused at the start, nothing written: the records stay as they were found, as evidence.
    assert files_of(setup.run_dir.parent) == found


def test_records_put_back_together_to_an_earlier_state_do_not_restart(setup):
    batch = setup.load()
    search(batch, "s1")
    earlier = {name: (setup.run_dir / name).read_bytes() for name in ("tasks.jsonl", "requests.jsonl")}
    search(batch, "s2", reads=(2,))
    used = batch.sent
    batch.close()

    # Both records put back as they were after the first search: each whole, and consistent with the other.
    for name, data in earlier.items():
        (setup.run_dir / name).write_bytes(data)

    with pytest.raises(BatchTermsError, match=f"比主目录记下的 {used} 次少"):
        setup.load()


@pytest.mark.parametrize("change", ["gone", "damaged", "lowered", "foreign"])
def test_homes_progress_gone_damaged_lowered_or_foreign_does_not_restart(setup, change):
    batch = setup.load()
    search(batch)
    batch_id = batch.batch_id
    batch.close()
    progress = progress_of(setup, batch_id)
    text = progress.read_text(encoding="utf-8")

    if change == "gone":
        progress.unlink()
    elif change == "damaged":
        progress.write_text(text + "{broken json\n", encoding="utf-8")
    elif change == "lowered":
        # Only its first line kept: whole and growing, but far short of the request record.
        progress.write_text(text.splitlines(keepends=True)[0], encoding="utf-8")
    else:
        last = json.loads(text.splitlines()[-1])
        progress.write_text(text + json.dumps({**last, "batch_id": "another-batch"}) + "\n", encoding="utf-8")

    with pytest.raises(BatchTermsError, match="进度|主目录"):
        setup.load()


@pytest.mark.parametrize(
    "change",
    ["request_broken", "request_line", "task_broken", "task_cut", "claim_local", "claim_home", "claim_added", "progress_line"],
)
def test_a_record_changed_while_an_operation_runs_stops_the_source_and_the_model_for_good(setup, change):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    source = adapter(batch)
    source.listing(30.0)
    urls, sent = list(setup.board.urls), batch.sent
    run, home = setup.run_dir, home_of(setup)

    def rewrite(path: Path, **values) -> None:
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **values}), encoding="utf-8")

    if change == "request_broken":
        (run / "requests.jsonl").write_text((run / "requests.jsonl").read_text(encoding="utf-8") + "{broken json\n", encoding="utf-8")
    elif change == "request_line":
        # A whole, valid line the batch did not write.
        text = (run / "requests.jsonl").read_text(encoding="utf-8")
        (run / "requests.jsonl").write_text(text + text.splitlines(keepends=True)[-1], encoding="utf-8")
    elif change == "task_broken":
        (run / "tasks.jsonl").write_text("{broken json\n", encoding="utf-8")
    elif change == "task_cut":
        (run / "tasks.jsonl").write_text("", encoding="utf-8")
    elif change == "claim_local":
        rewrite(run / "approvals" / f"{SOURCE_APPROVAL}.json", owner_id="user-b")
    elif change == "claim_home":
        rewrite(home / f"{SOURCE_APPROVAL}.claim", owner_id="user-b")
    elif change == "claim_added":
        (run / "approvals" / f"{DIRECTION_APPROVAL}.json").write_text((run / "approvals" / f"{MODEL_APPROVAL}.json").read_text(encoding="utf-8"), encoding="utf-8")
    else:
        progress = progress_of(setup, batch.batch_id)
        progress.write_text(progress.read_text(encoding="utf-8") + progress.read_text(encoding="utf-8").splitlines(keepends=True)[-1], encoding="utf-8")

    # The model's next request is kept back, and so is the source's: nothing more goes out of either.
    assert binding.refusal() is not None

    with pytest.raises(HeldBack):
        source.read(ref(1), 30.0)

    assert setup.board.urls == urls and batch.sent == sent
    stopped_for_good(setup, batch.batch_id)
    batch.end(operation)
    batch.close()

    with pytest.raises(BatchTermsError):
        setup.load()


def test_a_record_changed_between_two_requests_of_one_operation_keeps_the_next_back(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")
    batch_id = batch.batch_id

    def damage():
        # At the very moment the first read arrives at the board: the request out, its result not yet recorded.
        log = setup.run_dir / "tasks.jsonl"
        log.write_text(log.read_text(encoding="utf-8") + "{broken json\n", encoding="utf-8")

    setup.board.answer(detail(1), damage)
    source = adapter(batch)
    source.read(ref(1), 30.0)
    urls = list(setup.board.urls)

    with pytest.raises(HeldBack):
        source.read(ref(2), 30.0)

    # The request out when it happened counts and keeps its result; the next was never sent.
    assert setup.board.urls == urls and batch.sent == 2
    # The one kept back is recorded as refused, never as handed.
    assert [line["state"] for line in lines(setup.run_dir / "requests.jsonl")][-3:] == ["handed", "answered", "refused"]
    stopped_for_good(setup, batch_id, "records")


def test_ordinary_appends_from_both_sides_and_restarts_never_look_changed(setup):
    batch = setup.load()

    for number, op in ((1, "s1"), (2, "s2")):
        operation = batch.begin(OperationKind.SEARCH, "user-a", op)
        binding = batch_live.Binding(batch, operation)
        binding.bind()
        source = adapter(batch)
        source.listing(30.0)
        assert binding.refusal() is None
        source.read(ref(number), 30.0)
        setup.gates[-1].settled[MODEL_APPROVAL] = (number * 3, number * 30_000)
        assert binding.refusal() is None
        batch.end(operation)

    batch.close()
    again = setup.load(gate=gate_settled(MODEL_APPROVAL, (6, 60_000)))
    check = again.begin(OperationKind.CHECK, "user-a", "c1")
    adapter(again).read(ref(3), 30.0)
    again.end(check)

    assert again.problem() is None and not (setup.run_dir / "batch-stop.json").exists()


def gate_settled(approval_id: str, settled: tuple[int, int], purpose: str = "discovery") -> "Gate":
    gate = Gate(approval(purpose, DIRECTION_APPROVAL) if purpose == "direction" else approval())
    gate.settled[approval_id] = settled

    return gate


@pytest.mark.parametrize("change", ["local_damaged", "local_gone", "home_damaged", "home_gone", "home_changed"])
def test_the_previous_launchs_claim_damaged_changed_or_gone_keeps_the_next_launch_from_starting(setup, change):
    direction = setup.load(Launch.DIRECTION)
    explored = direction.begin(OperationKind.DIRECTION, "user-a", "d1")
    batch_live.Binding(direction, explored).bind()
    direction.end(explored)
    direction.close()
    local = setup.run_dir / "approvals" / f"{DIRECTION_APPROVAL}.json"
    home = home_of(setup) / f"{DIRECTION_APPROVAL}.claim"

    if change == "local_damaged":
        local.write_text("{broken json\n", encoding="utf-8")
    elif change == "local_gone":
        local.unlink()
    elif change == "home_damaged":
        home.write_text("{broken json\n", encoding="utf-8")
    elif change == "home_gone":
        home.unlink()
    else:
        home.write_text(json.dumps({**json.loads(home.read_text(encoding="utf-8")), "owner_id": "user-b"}), encoding="utf-8")

    with pytest.raises(BatchTermsError, match="占用记录"):
        setup.load()

    assert setup.board.urls == []


def test_a_crash_between_a_requests_record_and_its_note_in_home_restarts_and_counts_it(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.CHECK, "user-a", "c1")
    adapter(batch).read(ref(1), 30.0)
    assert batch.sent == 2
    batch_id = batch.batch_id
    batch.close()  # the process ended with the operation running
    # As a crash right after the request's own record and before its note in HOME leaves them: no note, no result.
    log, progress = setup.run_dir / "requests.jsonl", progress_of(setup, batch_id)
    log.write_text("".join(line for line in log.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]), encoding="utf-8")
    progress.write_text("".join(progress.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]), encoding="utf-8")
    found = progress.read_bytes()
    again = setup.load()

    # It counts, as a request of unknown outcome does; the operation ends as interrupted. Nothing is written in HOME
    # at the start (a check before a launch may write nothing there): the request is noted when the next one begins.
    assert again.sent == 2
    assert lines(setup.run_dir / "tasks.jsonl")[-1]["state"] == "interrupted"
    assert progress.read_bytes() == found
    again.close()
    restarted = setup.load()
    assert restarted.sent == 2 and progress.read_bytes() == found
    restarted.end(restarted.begin(OperationKind.CHECK, "user-a", "c2"))
    assert lines(progress)[-1]["handed"] == 2
    restarted.close()
    assert setup.load().sent == 2


def test_the_stop_noted_in_home_holds_without_the_run_directorys(setup):
    batch = setup.load()
    search(batch)
    (setup.run_dir / "LIVE_STOP").touch()
    assert batch.problem()[0] == "live_stop"
    batch_id = batch.batch_id
    batch.close()
    # The run directory's stop record and LIVE_STOP both taken away: HOME's stop still holds.
    (setup.run_dir / "batch-stop.json").unlink()
    (setup.run_dir / "LIVE_STOP").unlink()

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()

    assert (home_of(setup) / f"{batch_id}.stop.json").exists()


@pytest.mark.parametrize("which", ["this_launch", "the_other_launch", "another_library"])
def test_a_ledger_showing_less_than_noted_or_another_librarys_stops_the_batch_for_good(setup, which):
    if which == "the_other_launch":
        direction = setup.load(Launch.DIRECTION, gate=gate_settled(DIRECTION_APPROVAL, (0, 0), "direction"))
        explored = direction.begin(OperationKind.DIRECTION, "user-a", "d1")
        binding = batch_live.Binding(direction, explored)
        binding.bind()
        assert binding.refusal() is None
        # Its one request settled: what the ledger holds for it, noted in HOME when the operation ends.
        setup.gates[-1].settled[DIRECTION_APPROVAL] = (1, 946)
        direction.end(explored)
        batch_id = direction.batch_id
        direction.close()
        put_back = Gate(approval())
        put_back.settled[DIRECTION_APPROVAL] = (0, 0)
    else:
        batch = setup.load()
        operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
        binding = batch_live.Binding(batch, operation)
        binding.bind()
        setup.gates[-1].settled[MODEL_APPROVAL] = (7, 66_622)
        assert binding.refusal() is None
        batch.end(operation)
        batch_id = batch.batch_id
        batch.close()
        put_back = gate_settled(MODEL_APPROVAL, (0, 0) if which == "this_launch" else (7, 66_622))

    with pytest.raises(BatchTermsError, match="少|编号对不上"):
        setup.load(gate=put_back, library="synthetic-library-2" if which == "another_library" else LIBRARY)

    stopped_for_good(setup, batch_id, "ledger")
    # Even with the ledger as it was, the stop holds.
    right = gate_settled(MODEL_APPROVAL, (7, 66_622))
    right.settled[DIRECTION_APPROVAL] = (1, 946)

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load(gate=right)


def test_a_ledger_put_back_while_the_app_runs_keeps_the_next_model_request_back(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    setup.gates[-1].settled[MODEL_APPROVAL] = (3, 30_000)
    assert binding.refusal() is None
    # The library's ledger now holds less than noted: swapped under the running app.
    setup.gates[-1].settled[MODEL_APPROVAL] = (1, 10_000)

    assert binding.refusal()[0] == "ledger"
    stopped_for_good(setup, batch.batch_id, "ledger")
    batch.end(operation)


# --- each model request noted before it goes (D6-0 R2, Codex's F1 residue) -------------------------------------------
#
# Codex's boundary cases (/private/tmp/roleradar-d6-r1-check-uZD0V4/test_d6_r1_boundaries.py, run as it is) take one
# request through the real gate and crash before its operation ends; test_discovery_batch_pages has them with the real
# gate and both launches. Here the ledger is the gate's double, each request as the test sets it at the moment the
# gate asks the batch, right after reserving it and before handing it on.


def ledger_rows(setup, approval_id: str = MODEL_APPROVAL, **rows) -> None:
    """The requests the ledger holds of an approval now: request id → (state, reservation)."""
    setup.gates[-1].requests[approval_id] = dict(rows)


def noted_requests(setup, batch_id: str) -> list[tuple[str, str, int]]:
    return [
        (line["approval_id"], line["request_id"], line["reserved_micro_usd"])
        for line in lines(progress_of(setup, batch_id))
        if line["kind"] == "model_request"
    ]


def handing(setup, launch: Launch = Launch.DISCOVERY):
    """A launch's operation bound, its first model request reserved (r1, 210,000) and the batch asked about it."""
    batch = setup.load(launch)
    kind = OperationKind.DIRECTION if launch is Launch.DIRECTION else OperationKind.SEARCH
    operation = batch.begin(kind, "user-a", "op1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    ledger_rows(setup, DIRECTION_APPROVAL if launch is Launch.DIRECTION else MODEL_APPROVAL, r1=("reserved", 210_000))

    assert binding.refusal() is None

    return batch, operation, binding


def test_each_model_request_is_noted_by_its_id_and_reservation_before_it_goes_and_only_once(setup):
    batch, operation, binding = handing(setup)
    assert noted_requests(setup, batch.batch_id) == [(MODEL_APPROVAL, "r1", 210_000)]

    # Asked again for the same reservation: not noted twice.
    assert binding.refusal() is None
    assert noted_requests(setup, batch.batch_id) == [(MODEL_APPROVAL, "r1", 210_000)]

    # Settled, the next one reserved: the next one noted, and how far the use has come with it.
    setup.gates[-1].settled[MODEL_APPROVAL] = (1, 946)
    ledger_rows(setup, r1=("settled", 210_000), r2=("reserved", 210_000))

    assert binding.refusal() is None
    assert noted_requests(setup, batch.batch_id) == [(MODEL_APPROVAL, "r1", 210_000), (MODEL_APPROVAL, "r2", 210_000)]
    progress = [line for line in lines(progress_of(setup, batch.batch_id)) if line["kind"] == "model"]
    assert (progress[-1]["final_requests"], progress[-1]["final_micro_usd"]) == (1, 946)
    batch.end(operation)


def test_a_request_that_cannot_be_noted_in_home_is_not_handed_on(setup, monkeypatch):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    ledger_rows(setup, r1=("reserved", 210_000))
    written = batch_live._append_bytes

    def refuse_home(path, data):
        if path == progress_of(setup, batch.batch_id):
            raise OSError("disk full")

        written(path, data)

    monkeypatch.setattr(batch_live, "_append_bytes", refuse_home)

    assert binding.refusal()[0] == "unrecorded"
    assert noted_requests(setup, batch.batch_id) == []


def test_two_requests_reserved_at_once_stop_the_batch(setup):
    batch = setup.load()
    operation = batch.begin(OperationKind.SEARCH, "user-a", "s1")
    binding = batch_live.Binding(batch, operation)
    binding.bind()
    # The batch's operations send one model request at a time (decisions and analyses in turn; the gate itself would
    # let a task's requests overlap): two reservations at once are not this batch's sending, and it stops.
    ledger_rows(setup, r1=("reserved", 210_000), r2=("reserved", 210_000))
    code, words = binding.refusal()

    assert code == "ledger" and "同时有 2 个请求在预留" in words
    assert noted_requests(setup, batch.batch_id) == []
    stopped_for_good(setup, batch.batch_id, "ledger")
    batch.end(operation)


@pytest.mark.parametrize("change, words", [("gone", "发出前记下的 1 个请求"), ("reservation_changed", "预留和发出前记下的对不上")])
def test_a_noted_request_gone_from_the_ledger_or_changed_keeps_the_next_request_back(setup, change, words):
    batch, operation, binding = handing(setup)
    # The library swapped under the running app for one from before the request, or the request's row changed.
    ledger_rows(setup, **({} if change == "gone" else {"r1": ("settled", 99_000)}))
    code, said = binding.refusal()

    assert code == "ledger" and words in said
    stopped_for_good(setup, batch.batch_id, "ledger")
    batch.end(operation)


@pytest.mark.parametrize(
    "rows, settled, paused, problem, stop",
    [
        # A library from before the request: settled or not, sent or released, it is not there.
        ({}, (0, 0), None, "少了批准 .* 发出前记下的 1 个请求", "ledger"),
        ({"r1": ("settled", 99_000)}, (1, 946), None, "预留和发出前记下的对不上", "ledger"),
        # Of unknown cost or over its reservation, its approval not paused: the pause lost (a library changed).
        ({"r1": ("unknown", 210_000)}, (1, 210_000), None, "却没有暂停", "ledger"),
        ({"r1": ("over", 210_000)}, (1, 300_000), None, "却没有暂停", "ledger"),
        # Of unknown cost and paused, as the gate's recovery leaves a request out at a crash: the batch stops.
        ({"r1": ("unknown", 210_000)}, (1, 210_000), "unknown_usage", "已经暂停", "model_paused"),
    ],
)
def test_a_restart_after_a_crash_with_a_request_out_does_not_start_without_it_in_the_ledger(setup, rows, settled, paused, problem, stop):
    batch, operation, binding = handing(setup)
    batch_id = batch.batch_id
    batch.close()  # the process ends with the request out: nothing ended the operation or settled the request
    gate = Gate(approval())
    gate.requests[MODEL_APPROVAL] = rows
    gate.settled[MODEL_APPROVAL] = settled
    gate.state.paused_reason = paused

    with pytest.raises(BatchTermsError, match=problem):
        setup.load(gate=gate)

    stopped_for_good(setup, batch_id, stop)


@pytest.mark.parametrize("state, settled", [("settled", (1, 946)), ("released", (0, 0))])
def test_a_restart_with_the_noted_request_settled_or_released_as_never_sent_goes_on(setup, state, settled):
    batch, operation, binding = handing(setup)
    batch.close()
    gate = Gate(approval(), requests_left=20 - settled[0])
    gate.requests[MODEL_APPROVAL] = {"r1": (state, 210_000)}
    gate.settled[MODEL_APPROVAL] = settled

    restarted = setup.load(gate=gate)
    operation = restarted.begin(OperationKind.SEARCH, "user-a", "op2")
    binding = batch_live.Binding(restarted, operation)
    binding.bind()
    gate.requests[MODEL_APPROVAL]["r2"] = ("reserved", 210_000)

    # The next request is noted in turn, the first kept: what the first came to counts as the ledger has it.
    assert binding.refusal() is None
    assert noted_requests(setup, restarted.batch_id) == [(MODEL_APPROVAL, "r1", 210_000), (MODEL_APPROVAL, "r2", 210_000)]
    restarted.end(operation)


@pytest.mark.parametrize("library", ["as_left", "from_before"])
def test_the_direction_launchs_request_out_at_a_crash_is_looked_for_by_the_discovery_launch(setup, library):
    direction, explored, binding = handing(setup, Launch.DIRECTION)
    direction.close()  # the exploration's request out, its operation never ended
    gate = Gate(approval())

    if library == "as_left":
        gate.requests[DIRECTION_APPROVAL] = {"r1": ("settled", 210_000)}
        gate.settled[DIRECTION_APPROVAL] = (1, 946)
        setup.load(gate=gate).close()
    else:
        with pytest.raises(BatchTermsError, match=f"少了批准 {DIRECTION_APPROVAL} 发出前记下的 1 个请求"):
            setup.load(gate=gate)


@pytest.mark.parametrize("change", ["twice", "no_id", "negative", "not_a_number", "not_claimed", "another_batch"])
def test_a_request_line_in_homes_progress_the_batch_did_not_write_does_not_restart(setup, change):
    batch, operation, binding = handing(setup)
    batch_id = batch.batch_id
    batch.close()
    path = progress_of(setup, batch_id)
    entries = lines(path)
    (index,) = [number for number, line in enumerate(entries) if line["kind"] == "model_request"]
    line = entries[index]

    if change == "twice":
        entries.append(dict(line))
    else:
        entries[index] = {
            "no_id": {**line, "request_id": ""},
            "negative": {**line, "reserved_micro_usd": -1},
            "not_a_number": {**line, "reserved_micro_usd": True},
            "not_claimed": {**line, "approval_id": DIRECTION_APPROVAL},
            "another_batch": {**line, "batch_id": "another-batch"},
        }[change]

    path.write_text("".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")
    gate = Gate(approval())
    gate.requests[MODEL_APPROVAL] = {"r1": ("settled", 210_000)}
    gate.settled[MODEL_APPROVAL] = (1, 946)

    with pytest.raises(BatchTermsError, match="不认识的请求行"):
        setup.load(gate=gate)


def test_an_operations_end_saying_other_than_its_requests_does_not_restart(setup):
    batch = setup.load()
    search(batch)
    batch.close()
    log = setup.run_dir / "tasks.jsonl"
    entries = lines(log)
    # The request record and HOME's progress as they were: only the operation's own count changed.
    entries[-1] = {**entries[-1], "sent": entries[-1]["sent"] - 1}
    log.write_text("".join(json.dumps(line) + "\n" for line in entries), encoding="utf-8")

    with pytest.raises(BatchTermsError, match="任务记录和请求记录对不上"):
        setup.load()


def test_a_record_changed_between_operations_keeps_the_next_from_starting(setup):
    batch = setup.load()
    search(batch, "s1")
    log = setup.run_dir / "requests.jsonl"
    # The last request's result cut away: whole lines all, the requests handed still as many as the batch sent.
    log.write_text("".join(log.read_text(encoding="utf-8").splitlines(keepends=True)[:-1]), encoding="utf-8")
    urls = list(setup.board.urls)

    with pytest.raises(BatchRefused) as refused:
        batch.begin(OperationKind.CHECK, "user-a", "c1")

    assert refused.value.code == "records" and setup.board.urls == urls
    stopped_for_good(setup, batch.batch_id, "records")


def test_the_run_directorys_stop_holds_without_homes(setup):
    batch = setup.load()
    search(batch)
    (setup.run_dir / "LIVE_STOP").touch()
    assert batch.problem()[0] == "live_stop"
    batch_id = batch.batch_id
    batch.close()
    # HOME's stop and LIVE_STOP taken away: the run directory's stop record still holds.
    (home_of(setup) / f"{batch_id}.stop.json").unlink()
    (setup.run_dir / "LIVE_STOP").unlink()

    with pytest.raises(BatchTermsError, match="已经停止"):
        setup.load()
