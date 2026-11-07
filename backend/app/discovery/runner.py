"""Running a confirmed search for postings, as one parent task (U3-A2).

One search is one task (app.discovery.tasks), and it gets its provider once, for its whole run: provider_for(task,
sending), the simulation's (app.job_analysis.service.local_provider) or, for a real service, its gate's, entered
once for Purpose.DISCOVERY (live_provider_for). Its decisions and the analyses within it all go through that one
provider, so they share one task and one ledger, and no analysis enters the gate again. This batch is tested with
offline stand-ins only: nothing here builds a real service.

Tracing first: LangSmith tracing can be switched on by environment variables (LANGSMITH_*, LANGCHAIN_*). With any of
them set a search does not start (TRACING): nothing is built, entered or sent, and only the variables' names are
looked at. The isolated test runner removes them as well.

Requests: the task's sending (app.live_gate.Sending) has the last word right before one is handed on. None goes once
the search may not go on (cancelled, out of its time, ended) or the store says the task stopped (ParentStopped); the
gate then releases what it reserved, and the provider the search uses (Guarded) turns it into a failure that sent
nothing (RequestKeptBack), on which the decisions and the analyses already stop. A request handed on is counted when
it comes back, also for a stopped task; what it carried is not taken (app.discovery.tools).

Time: the search's ten minutes are the task's, from when it started: the run keeps the store's clock and deadline,
so it ends as a timeout when they run out, and as cancelled when a user, a logout or a shutdown stops it.

One executor (A2 acceptance, 7.3): before any provider, source or decision, run_search claims the task
(DiscoveryTaskStore.claim). A second one dispatched for the same task gets nothing and returns at once, without
ending the task for it; only the holder says how the search ended, and the store decides then whether its result
may be taken (DiscoveryTaskStore.finish).
"""
import logging
import os
from collections.abc import Callable, Mapping

from app.ai_provider import AIProvider, AIProviderConnectionError, SendState
from app.discovery import agent, baseline, tools
from app.discovery.sources import Register
from app.discovery.tasks import DiscoveryTaskStore, Flow
from app.job_analysis.service import LiveService, ProviderUnavailable
from app.library.sends import Call, Delivery, Sends
from app.live_gate import LiveBlocked, LiveRefused, Purpose

logger = logging.getLogger("roleradar.discovery")

# Why a search did not run (DiscoveryTask.problem).
TRACING = "tracing_enabled"
START_REFUSED = "start_refused"
PROVIDER_UNAVAILABLE = "provider_unavailable"
STOPPED = "stopped"
INTERNAL_ERROR = "internal_error"
# What could switch LangSmith tracing on, by the start of a variable's name.
TRACING_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")


def tracing_variables(environ: Mapping[str, str]) -> tuple[str, ...]:
    """The names, never the values, of the variables that could switch LangSmith tracing on."""
    return tuple(sorted(name for name in environ if name.upper().startswith(TRACING_PREFIXES)))


class ParentStopped(Exception):
    """The parent task may not hand on another request: the search ended or stopped, or the store stopped the task."""


class RequestKeptBack(AIProviderConnectionError):
    """Nothing went out: the parent task kept the request back."""

    SENT = SendState.NOT_SENT


class _Stops:
    """The search's cancel signal: someone stopped the task, or it is gone (DiscoveryTaskStore.stopped)."""

    def __init__(self, store: DiscoveryTaskStore, task_id: str):
        self._store = store
        self._task_id = task_id

    def is_set(self) -> bool:
        return self._store.stopped(self._task_id)


class _Sending:
    """Where the task's requests are counted (app.live_gate.Sending), and the last word before each is handed on."""

    def __init__(self, store: DiscoveryTaskStore, task_id: str):
        self._store = store
        self._task_id = task_id
        self.run: tools.SearchRun | None = None
        self.sends = Sends()

    def hand_on(self, call: Call) -> None:
        if (self.run is not None and self.run.stopping()) or not self._store.hand_on(self._task_id, call):
            raise ParentStopped()

    def came_back(self, call: Call, delivered: Delivery) -> None:
        self._store.came_back(self._task_id, call, delivered)
        self.sends = self.sends.plus(call, delivered)


class Guarded(AIProvider):
    """The provider a search, its decisions and its analyses use: a request the parent kept back is one nothing was
    sent for (RequestKeptBack), which they stop on as on any request that failed."""

    def __init__(self, provider: AIProvider):
        self._provider = provider

    def identity(self):
        return self._provider.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        try:
            return self._provider.generate_text(
                prompt,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                json_schema_format=json_schema_format,
            )
        except ParentStopped:
            raise RequestKeptBack("The parent task kept the request back.") from None


def live_provider_for(service: LiveService):
    """How a search gets a real service's provider: its gate, entered once for the whole search (Purpose.DISCOVERY)."""

    def provider_for(task, sending):
        return service.provider_of(task.owner_id, sending, Purpose.DISCOVERY)

    return provider_for


def run_search(
    store: DiscoveryTaskStore,
    database,
    task_id: str,
    provider_for,
    *,
    register: Register,
    clock,
    decider_for: Callable[[AIProvider], agent.Decider],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run a search the store has started, as its one executor; what came of it goes back to the store only.

    decider_for(provider) gives the agent's decider: the simulated one, a model's through this provider, or a
    test's script. The fixed flow takes none.
    """
    claimed = store.claim(task_id)

    if claimed is None:
        # Not running, or another executor holds it: leave it alone, and say nothing of how it ended.
        return

    worker, task = claimed
    environ = os.environ if environ is None else environ

    if tracing_variables(environ):
        logger.error("a search did not start: variables that could switch tracing on are set")
        store.finish(task_id, worker=worker, problem=TRACING)

        return

    sending = _Sending(store, task_id)
    finished, problem, problems = None, None, ()

    try:
        with provider_for(task, sending) as provider:
            guarded = Guarded(provider)
            run = tools.start(
                database,
                task.confirmation,
                task.approval,
                guarded,
                register=register,
                clock=clock,
                cancel=_Stops(store, task_id),
                monotonic=store.clock,
            )
            # The search's time is the task's: the same clock, from the same start.
            run.deadline = task.started + task.seconds
            sending.run = run

            if task.flow is Flow.BASELINE:
                finished = baseline.run_baseline(run)
            else:
                finished = agent.run_agent(run, decider_for(guarded))
    except tools.StartRefused as refused:
        # Nothing was sent: the confirmation does not hold as it is now.
        problem, problems = START_REFUSED, refused.problems
    except LiveBlocked as blocked:
        # The gate let the search's task no further before it began (another task, its purpose, closed).
        problem = f"gate_{blocked.reason.value}"
    except (ProviderUnavailable, LiveRefused) as error:
        logger.error("a search could not use its service (%s)", type(error).__name__)
        problem = PROVIDER_UNAVAILABLE
    except ParentStopped:
        problem = STOPPED
    except Exception as error:  # the store must learn that it failed; no text is logged
        logger.error("a search failed unexpectedly (%s)", type(error).__name__)
        problem = INTERNAL_ERROR

    store.finish(task_id, worker=worker, finished=finished, problem=problem, problems=problems)
