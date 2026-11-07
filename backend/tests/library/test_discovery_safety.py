"""U3-A2: a search as one parent task: whose it is, its one gate, what may go out, tracing and the network.

The service behind the gate is made up (discovery_helpers.SyntheticService) with synthetic prices: nothing real is
built or asked. Decisions come from scripts, through a model decider where the gate is under test. All material is
synthetic.
"""
import os
import sys
import threading

import pytest
from sqlalchemy import select

from app.discovery import agent, synthetic
from app.discovery.agent import (
    DECISION_OUTPUT_TOKENS,
    MAX_DECISION_BYTES,
    ModelDecider,
    ScriptedDecider,
    SimulatedDecider,
    make_action,
    request_problem,
    run_agent,
)
from app.discovery.contracts import EndReason, Limits, Outcome, Tool
from app.discovery.runner import TRACING, live_provider_for, run_search, tracing_variables
from app.discovery.synthetic import CAREERS
from app.discovery.tasks import DiscoveryTaskStore, Flow, Stage, Start
from app.experience_analysis import ANALYSIS_SCHEMA_NAME
from app.jd_service import SCHEMA_NAME
from app.job_analysis.service import LiveService, local_provider
from app.library.models import LiveRequest
from app.live_gate import Blocked, LiveBlocked, open_gate
from app.outbound_review import review
from discovery_helpers import (
    BOUND,
    PROJECT_ACTIONS,
    PROJECT_TITLE,
    SENTINEL,
    SYNTHETIC_RECIPIENT,
    WITHIN,
    SyntheticService,
    begin,
    confirmation_for,
    gate_terms,
    make_library,
    ref,
)
from job_helpers import RecordingProvider

SESSION, OTHER_SESSION = "session-hash-a", "session-hash-b"


@pytest.fixture
def library(database, owners, clock):
    return make_library(database, owners, clock)


def search(city="北京", words="数据分析 实习生", **filters):
    return make_action(Tool.SEARCH, source_id=CAREERS, keywords=words, city=city, **{**WITHIN, **filters}, note="检索")


def read(*posting_ids):
    return make_action(Tool.READ, postings=[ref(posting_id) for posting_id in posting_ids], note="读取")


def check(*posting_ids):
    return make_action(Tool.CHECK, postings=[ref(posting_id) for posting_id in posting_ids], note="核对")


def analyze(posting_id):
    return make_action(Tool.ANALYZE, postings=[ref(posting_id)], note="分析")


def finish(claim=EndReason.ENOUGH):
    return make_action(Tool.FINISH, claim=claim, note="结束")


def started(store, library, confirmation, flow=Flow.AGENT, session=SESSION):
    task = store.create(library.owner, session, confirmation, flow)
    state, task = store.begin(task.id, library.owner, session, task.fingerprint)
    assert state is Start.STARTED

    return task


def run_local(store, database, clock, task, decider, *, register=None, provider=None, environ=None):
    """Run a started search on this machine: the app's simulation, or the provider given, nothing gated."""
    register = register or synthetic.register()
    provider = provider or RecordingProvider()
    run_search(
        store, database, task.id, local_provider(lambda task: provider), register=register, clock=clock,
        decider_for=lambda guarded: decider, environ={} if environ is None else environ,
    )

    return register, provider


# --- whose a search is ------------------------------------------------------------------------


def test_a_search_is_seen_started_and_cancelled_only_by_its_own_user_and_session(library):
    store = DiscoveryTaskStore()
    task = store.create(library.owner, SESSION, confirmation_for(library))

    assert store.get(task.id, library.other, SESSION) is None
    assert store.get(task.id, library.owner, OTHER_SESSION) is None
    assert store.begin(task.id, library.other, SESSION, task.fingerprint) == (Start.GONE, None)
    assert store.cancel(task.id, library.other, SESSION) is None
    assert store.begin(task.id, library.owner, SESSION, "0" * 64)[0] is Start.STALE_FORM
    assert store.begin(task.id, library.owner, SESSION, task.fingerprint)[0] is Start.STARTED
    assert store.begin(task.id, library.owner, SESSION, task.fingerprint)[0] is Start.ALREADY

    second = store.create(library.owner, SESSION, confirmation_for(library))

    # One search at a time in a session; another session of the same user may run its own.
    assert store.begin(second.id, library.owner, SESSION, second.fingerprint)[0] is Start.BUSY
    assert store.get(task.id, library.owner, SESSION).stage is Stage.RUNNING

    with pytest.raises(ValueError):
        store.create(library.other, SESSION, confirmation_for(library))


def test_a_user_cancelling_stops_the_search_and_another_user_cannot(database, library, clock):
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library))

    def cancelling(observation):
        assert store.cancel(task.id, library.other, SESSION) is None
        assert store.cancel(task.id, library.owner, OTHER_SESSION) is None
        assert store.cancel(task.id, library.owner, SESSION).stage is Stage.CANCELLED

        return read("c-bj-analyst")

    register, provider = run_local(store, database, clock, task, ScriptedDecider([search(), cancelling, finish()]))
    task = store.get(task.id, library.owner, SESSION)

    assert (task.stage, task.cancelled_by, task.finished.outcome.end) == (Stage.CANCELLED, "user", EndReason.CANCELLED)
    assert task.finished.trace.steps[-1].outcome == "stopped"
    assert (len(register.adapter(CAREERS).calls), provider.prompts) == (1, [])


def test_ending_the_session_cancels_the_running_search_and_forgets_it(database, library, clock):
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library))

    def logging_out(observation):
        store.end_session(SESSION)

        return read("c-bj-analyst")

    register, provider = run_local(store, database, clock, task, ScriptedDecider([search(), logging_out, finish()]))

    assert store.get(task.id, library.owner, SESSION) is None and len(store) == 0
    assert (len(register.adapter(CAREERS).calls), provider.prompts) == (1, [])


def test_a_search_has_its_ten_minutes_from_when_it_started_and_then_ends_as_a_timeout(database, library, clock):
    now = [1000.0]
    store = DiscoveryTaskStore(clock=lambda: now[0])
    task = started(store, library, confirmation_for(library))

    def late(observation):
        now[0] += Limits().seconds

        return read("c-bj-analyst")

    register, provider = run_local(store, database, clock, task, ScriptedDecider([search(), late, finish()]))
    task = store.get(task.id, library.owner, SESSION)

    assert task.finished.outcome.end is EndReason.TIMEOUT
    assert (task.stage, task.cancelled_by) == (Stage.CANCELLED, "timeout")
    assert (len(register.adapter(CAREERS).calls), provider.prompts) == (1, [])


def test_the_fixed_flow_runs_as_a_search_task_too(database, library, clock):
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library), Flow.BASELINE)

    _, provider = run_local(store, database, clock, task, decider=None)
    task = store.get(task.id, library.owner, SESSION)

    assert (task.stage, task.finished.trace.flow, task.finished.trace.decisions) == (Stage.DONE, "baseline", 0)
    assert [step.tool for step in task.finished.trace.steps] == [Tool.SEARCH, Tool.READ, Tool.CHECK, *[Tool.ANALYZE] * 3, Tool.FINISH]
    # Analyses only: the simulation's requests, counted by the store.
    assert task.sends.generation_sent == len(provider.prompts) == 6


# --- tracing ----------------------------------------------------------------------------------


def test_a_search_with_tracing_variables_set_does_not_start(database, library, clock):
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library))
    asked = []
    environ = {"LANGSMITH_TRACING": "true", "LANGCHAIN_API_KEY": "lsv2-fake-not-a-key", "PATH": "/usr/bin"}

    def provider_for(task, sending):
        asked.append("provider")
        raise AssertionError("no service may be asked for")

    run_search(
        store, database, task.id, provider_for, register=synthetic.register(), clock=clock,
        decider_for=lambda provider: asked.append("decider"), environ=environ,
    )
    task = store.get(task.id, library.owner, SESSION)

    assert (task.problem, task.finished, asked) == (TRACING, None, [])
    # Only the names are looked at; no value is kept.
    assert tracing_variables(environ) == ("LANGCHAIN_API_KEY", "LANGSMITH_TRACING")
    assert "lsv2-fake" not in repr(task)


def test_the_isolated_runner_removes_tracing_variables(tmp_path, monkeypatch):
    import run_isolated

    # This very run was started by the launcher: none is set.
    assert tracing_variables(os.environ) == ()

    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGCHAIN_TRACING_V2", "LANGCHAIN_ENDPOINT"):
        monkeypatch.setenv(name, "fake-value-not-a-key")

    root = tmp_path / "root"
    root.mkdir()
    env = run_isolated.isolated_environment(root)

    assert tracing_variables(env) == ()
    assert "fake-value-not-a-key" not in env.values()


# --- one gate, one ledger ---------------------------------------------------------------------


@pytest.fixture
def gate_for(database, clock, tmp_path):
    opened = []

    def open_one(terms):
        gate = open_gate(database, tmp_path / "service.db", terms, BOUND, clock)
        opened.append(gate)

        return gate

    yield open_one

    for gate in opened:
        gate.close()


def run_gated(store, database, clock, task, gate, service):
    run_search(
        store, database, task.id, live_provider_for(LiveService(gate, lambda: service)), register=synthetic.register(),
        clock=clock, decider_for=ModelDecider, environ={},
    )

    return store.get(task.id, task.owner_id, task.session)


def purposes_of(database) -> list[str]:
    with database.transaction() as db:
        return [row.purpose for row in db.scalars(select(LiveRequest))]


def test_decisions_and_analyses_share_one_gate_task_and_one_ledger(database, library, clock, gate_for):
    # The approval covers job analyses as well: a second entry meanwhile is kept out as busy, not for its purpose.
    gate = gate_for(gate_terms(purposes=frozenset({"discovery", "analysis"})))
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))
    nested = []

    def busy_meanwhile():
        # A job analysis, or any second entry, is kept out while the search holds the gate.
        with pytest.raises(LiveBlocked) as blocked:
            with gate.task("analysis", library.owner, SyntheticService()):
                pass

        nested.append(blocked.value.reason)

        return read("c-bj-analyst")

    service = SyntheticService([search(), busy_meanwhile, check("c-bj-analyst"), analyze("c-bj-analyst"), finish()])

    task = run_gated(store, database, clock, task, gate, service)
    outcome = task.finished.outcome

    assert (task.stage, outcome.end, [result.ref for result in outcome.results]) == (Stage.DONE, EndReason.ENOUGH, [ref("c-bj-analyst")])
    assert nested == [Blocked.BUSY]
    # Five decisions and one analysis of two requests: seven requests of one task, one ledger, one purpose.
    assert service.kinds() == ["discovery_action"] * 4 + [SCHEMA_NAME, ANALYSIS_SCHEMA_NAME, "discovery_action"]
    assert (gate.status().requests_used, task.sends.generation_sent) == (7, 7)
    assert purposes_of(database) == ["discovery"] * 7
    # A decision asks for less than the approval allows; an analysis keeps the analysis limit.
    assert {limit for kind, _, limit in service.sent if kind == "discovery_action"} == {DECISION_OUTPUT_TOKENS}


def test_a_search_needs_an_approval_that_covers_it(database, library, clock, gate_for):
    gate = gate_for(gate_terms(purposes=frozenset({"analysis"})))
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))
    service = SyntheticService([search()])

    task = run_gated(store, database, clock, task, gate, service)

    assert (task.problem, task.finished, service.sent) == ("gate_purpose", None, [])


def test_the_gate_running_out_of_requests_ends_the_search_at_the_gate(database, library, clock, gate_for):
    gate = gate_for(gate_terms(max_requests=3))
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))
    service = SyntheticService([search(), read("c-bj-analyst"), check("c-bj-analyst"), analyze("c-bj-analyst")])

    task = run_gated(store, database, clock, task, gate, service)
    last = task.finished.trace.steps[-1]

    assert task.finished.outcome.end is EndReason.GATE
    assert (last.outcome, last.reason, len(service.sent), gate.status().requests_used) == ("stopped", "requests", 3, 3)


def test_a_decision_back_after_a_cancellation_is_counted_and_not_acted_on(database, library, clock, gate_for):
    gate = gate_for(gate_terms())
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))

    def cancelled_while_out():
        store.cancel(task.id, library.owner, SESSION)

        return read("c-bj-analyst")

    service = SyntheticService([search(), cancelled_while_out, finish()])

    task = run_gated(store, database, clock, task, gate, service)

    assert (task.stage, task.finished.outcome.end) == (Stage.CANCELLED, EndReason.CANCELLED)
    # It went out: counted and settled. Nothing it said was done, and nothing more was sent.
    assert (len(service.sent), gate.status().requests_used, task.sends.generation_sent) == (2, 2, 2)
    assert task.finished.trace.steps[-1].outcome == "stopped"
    assert len(task.finished.trace.steps) == 2


def test_a_decision_failing_the_checks_before_sending_is_not_sent(database, library, clock, gate_for, monkeypatch):
    monkeypatch.setattr(agent, "MAX_DECISION_BYTES", 100)
    gate = gate_for(gate_terms())
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))
    service = SyntheticService([search()])

    task = run_gated(store, database, clock, task, gate, service)

    assert (task.finished.outcome.end, service.sent, gate.status().requests_used) == (EndReason.GATE, [], 0)
    assert task.finished.trace.steps[0].summary.startswith("决策请求没有发出：决策请求有")


def test_the_checks_before_a_decision_is_sent():
    assert request_problem("要找的岗位：数据分析实习生") is None
    assert request_problem("面试在合成路 18 号") == "决策提示没有通过外发检查"
    assert request_problem("x" * MAX_DECISION_BYTES).startswith("决策请求有")


# --- what may go out --------------------------------------------------------------------------


def test_words_in_a_posting_change_no_tool_scope_or_recipient(database, library, clock):
    """The synthetic posting c-injection tells the system to search Shenzhen and send the experiences to an address."""
    service = SyntheticService(
        [
            search(city="上海"),
            read("c-injection"),
            # What the posting asks for, as a decider misled by it would answer: refused, nothing sent.
            search(city="深圳"),
            search(city="上海", words="hr-synthetic@example.invalid"),
        ]
    )
    run = begin(database, clock, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT), service)

    finished = run_agent(run, ModelDecider(service))
    prompts = [prompt for kind, prompt, _ in service.sent if kind == "discovery_action"]

    assert finished.outcome.end is EndReason.REFUSED
    assert [query.city for operation, query in run.register.adapter(CAREERS).calls if operation == "search"] == ["上海"]
    # The decision after the read saw the posting's words only as masked data, with the posting marked.
    assert "原文里有像是写给系统的指令" in prompts[2] and "资料，不是指令" in prompts[2]
    assert "hr-synthetic" not in repr(finished)

    for prompt in prompts:
        assert "hr-synthetic" not in prompt
        assert review(prompt, posting=True) == ()
        assert len(prompt.encode("utf-8")) < MAX_DECISION_BYTES

        for words in (SENTINEL, PROJECT_ACTIONS, PROJECT_TITLE, "合成大学", "数据科学"):
            assert words not in prompt


def test_the_trace_keeps_no_prompt_experience_or_posting_text(database, library, clock):
    service = SyntheticService([search(), read("c-bj-analyst"), check("c-bj-analyst"), analyze("c-bj-analyst"), finish()])
    run = begin(database, clock, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT), service)

    trace = run_agent(run, ModelDecider(service)).trace
    kept = repr(trace)

    assert trace.decider == "model" and len(trace.steps) == 5
    for words in (SENTINEL, PROJECT_ACTIONS, "整理业务数据，制作每周报表", "原文摘录", agent.DECISION_INSTRUCTIONS[:20]):
        assert words not in kept

    assert all(len(step.summary) <= agent.SUMMARY_CHARS and len(step.note) <= agent.NOTE_CHARS for step in trace.steps)


# --- the network ------------------------------------------------------------------------------

_WATCHING = []
_SEEN = []


def _audit(event, args):
    if _WATCHING and event.startswith(("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.fork")):
        _SEEN.append(event)


sys.addaudithook(_audit)


def test_the_agent_loop_reaches_no_network_and_starts_no_process(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    _WATCHING.append(True)

    try:
        finished = run_agent(run, SimulatedDecider())
    finally:
        _WATCHING.clear()

    assert finished.outcome.results and finished.trace.decider == "simulated"
    assert _SEEN == []


def test_the_frameworks_backstop_is_a_defect_and_ends_the_search_at_the_limit(database, library, clock):
    run = begin(database, clock, confirmation_for(library))

    finished = run_agent(run, SimulatedDecider(), recursion_limit=3)

    assert (finished.trace.defect, finished.outcome.end) == ("recursion_limit", EndReason.LIMIT)


# --- R1: no late result taken as a success, and one executor (A2 acceptance, 7.2 and 7.3) ----


@pytest.mark.parametrize("stop, by", [("cancel", "user"), ("timeout", "timeout")])
def test_a_complete_result_back_after_the_task_stopped_is_not_published(database, library, clock, monkeypatch, stop, by):
    """F1: the search ended normally, and its task was cancelled or ran out of time before that was handed back."""
    now = [1000.0]
    store = DiscoveryTaskStore(clock=lambda: now[0])
    task = started(store, library, confirmation_for(library))
    ended = []
    run_agent_as_it_is = agent.run_agent

    def stopped_before_handing_back(*args, **kwargs):
        finished = run_agent_as_it_is(*args, **kwargs)
        ended.append(finished)

        if stop == "cancel":
            store.cancel(task.id, library.owner, SESSION)
        else:
            now[0] += task.seconds

        return finished

    monkeypatch.setattr(agent, "run_agent", stopped_before_handing_back)
    run_local(store, database, clock, task, ScriptedDecider([search(), read("c-bj-analyst"), check("c-bj-analyst"), analyze("c-bj-analyst"), finish()]))
    stored = store.get(task.id, library.owner, SESSION)

    # It had ended normally with a result; the task stopped first, so none of it is published as a success.
    assert (ended[0].outcome.end, len(ended[0].outcome.results)) == (EndReason.ENOUGH, 1)
    assert (stored.stage, stored.cancelled_by, stored.finished, stored.problem) == (Stage.CANCELLED, by, None, None)
    # What went out stays counted: the analysis's two requests.
    assert stored.sends.generation_sent == 2


def test_a_second_executor_of_a_running_search_does_nothing(database, library, clock):
    """F2: the same task dispatched again while its executor decides."""
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library))
    duplicate_register, duplicate_decider = synthetic.register(), ScriptedDecider([search(), finish()])
    meanwhile = []

    def dispatched_again(observation):
        run_local(store, database, clock, task, duplicate_decider, register=duplicate_register)
        meanwhile.append(store.get(task.id, library.owner, SESSION).stage)

        return read("c-bj-analyst")

    run_local(store, database, clock, task, ScriptedDecider([search(), dispatched_again, check("c-bj-analyst"), analyze("c-bj-analyst"), finish()]))
    stored = store.get(task.id, library.owner, SESSION)

    assert (duplicate_decider.observations, duplicate_register.adapter(CAREERS).calls, meanwhile) == ([], [], [Stage.RUNNING])
    assert (stored.stage, stored.finished.outcome.end, len(stored.finished.outcome.results)) == (Stage.DONE, EndReason.ENOUGH, 1)


def test_how_a_search_ended_is_said_once_and_only_by_its_executor(database, library, clock):
    """F2: a second word on how a task ended, by anyone, changes nothing."""
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library))
    run_local(store, database, clock, task, ScriptedDecider([search(), finish()]))
    done = store.get(task.id, library.owner, SESSION)

    for worker in (None, "not-its-executor"):
        store.finish(task.id, worker=worker, problem="gate_busy")

    assert done.stage is Stage.DONE and done.finished.outcome.end is EndReason.ENOUGH
    assert store.get(task.id, library.owner, SESSION) == done

    # The executor itself says it once: the right is taken once, and a second word is ignored.
    other = started(store, library, confirmation_for(library), session=OTHER_SESSION)
    worker, _ = store.claim(other.id)

    assert store.claim(other.id) is None
    store.finish(other.id, worker=worker, problem="internal_error")
    store.finish(other.id, worker=worker, problem="gate_busy")
    assert store.get(other.id, library.owner, OTHER_SESSION).problem == "internal_error"


def test_a_second_executor_never_enters_the_gate_or_ends_the_running_search(database, library, clock, gate_for):
    """F2 behind a gate: the duplicate is turned away before the gate, and the search it would have spoiled goes on."""
    gate = gate_for(gate_terms())
    store = DiscoveryTaskStore()
    task = started(store, library, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT))
    duplicate = SyntheticService([finish()])
    meanwhile = []

    def dispatched_again():
        meanwhile.append(run_gated(store, database, clock, task, gate, duplicate).stage)

        return read("c-bj-analyst")

    service = SyntheticService([search(), dispatched_again, check("c-bj-analyst"), analyze("c-bj-analyst"), finish()])
    task = run_gated(store, database, clock, task, gate, service)

    assert (duplicate.sent, meanwhile) == ([], [Stage.RUNNING])
    assert (task.stage, task.finished.outcome.end, len(task.finished.outcome.results)) == (Stage.DONE, EndReason.ENOUGH, 1)
    assert gate.status().requests_used == len(service.sent) == task.sends.generation_sent == 7
