"""Running a confirmed direction exploration in the background (U3-C3a).

A started task runs on a small thread pool of its own, so pages and cancelling keep answering while the service is
busy. It runs the fixed workflow (app.directions.explore.run_exploration) with the task's own approval: the
fingerprint the store keeps, never one a page sent.

The workflow checks the task's signal before and after its request: the store's own judgement that the task was not
cancelled and is still within its time (ExplorationTaskStore.still_running). The task's provider asks the store once
more before the request is asked for, and the store has the last word right before it is handed on (hand_on, told by
the counting wrapper of app.job_analysis.service.local_provider, or by the real service's gate after its own last
checks): a stop that came first keeps the request back, and the run ends as cancelled with nothing sent. A request
that went out is counted when it comes back, as the store counts it; a late answer is dropped by the store (finish).

The provider is the one the runtime hands over (app.directions.web): the local simulation, or the app's one real
service behind its gate when the approval covers direction exploration. Nothing here builds a service, a client or a
gate, and nothing here knows the gate: the runtime turns what the gate says into the three exceptions below, so a
refusal reads as a refusal (with the gate's reason), an unusable service as that, and a ledger that could not be
read or written as a failure with no result. Whichever it is, what went out or may have stays counted: the count
is the store's, told by the provider as each request was handed on and came back. A task whose request names a real
service runs the workflow for that service only (run_exploration(live=True)); the simulation, for the simulation only.
"""
import logging
from dataclasses import replace

from app.ai_provider import AIProvider, AIProviderConfigError
from app.analysis_contracts import SIMULATION_PROVIDER, AnalysisFailure, TaskApproval
from app.directions.explore import ExplorationOutcome, ExplorationStatus, run_exploration
from app.directions.tasks import ExplorationTaskStore
from app.library.sends import Call, Delivery, Sends

logger = logging.getLogger("roleradar.directions")
# A failure of this program, not of the service or the answer.
INTERNAL_ERROR = "internal_error"
# The real service's ledger could not be read or written: no result is taken from such a run.
LEDGER_FAILED = "ledger_failed"
# What a refusal of the real service's gate is called in an outcome: gate_ and the gate's own reason, as the analysis
# names them (app.analysis_contracts.RejectionReason).
GATE_PREFIX = "gate_"


class TaskStopped(Exception):
    """The task may not go on: cancelled, ended with its session, or out of time."""


class ServiceRefused(Exception):
    """The real service's gate let no request through (nothing of it was sent); reason is the gate's word for why."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ServiceUnavailable(Exception):
    """The real service could not be used for this task: nothing was sent."""


class LedgerFailed(Exception):
    """The real service's ledger could not be read or written: a request that went out, or may have, is counted all
    the same; its answer is not taken."""


class _Signal:
    """What the workflow asks before and after its request: may this task go on?"""

    def __init__(self, store: ExplorationTaskStore, task_id: str):
        self._store = store
        self._task_id = task_id

    def is_set(self) -> bool:
        return not self._store.still_running(self._task_id)


class _Stoppable(AIProvider):
    """The task's provider: a task stopped already asks for nothing."""

    def __init__(self, provider: AIProvider, store: ExplorationTaskStore, task_id: str):
        self._provider = provider
        self._store = store
        self._task_id = task_id

    def identity(self):
        return self._provider.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        if not self._store.still_running(self._task_id):
            raise TaskStopped()

        return self._provider.generate_text(
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            json_schema_format=json_schema_format,
        )


class _Sending:
    """Where the task's request is counted (the Sending of app.live_gate): in the store, and here for its outcome."""

    def __init__(self, store: ExplorationTaskStore, task_id: str):
        self._store = store
        self._task_id = task_id
        self.sends = Sends()

    def hand_on(self, call: Call) -> None:
        if not self._store.hand_on(self._task_id, call):
            raise TaskStopped()

    def came_back(self, call: Call, delivered: Delivery) -> None:
        self._store.came_back(self._task_id, call, delivered)
        self.sends = self.sends.plus(call, delivered)


def run_task(store: ExplorationTaskStore, database, clock, task_id: str, provider_for) -> None:
    """Run a task the store has started; its outcome goes back to the store, nowhere else.

    provider_for(task, sending) gives the task's provider as a context. clock: anything with now() (the library's
    runtime), the time the workflow stamps a result with.
    """
    task = store.running(task_id)

    if task is None:
        return

    sending = _Sending(store, task_id)

    def ended(status: ExplorationStatus, problem: str | None = None) -> ExplorationOutcome:
        return ExplorationOutcome(status=status, task_fingerprint=task.fingerprint, requests_sent=0, problem=problem)

    try:
        with provider_for(task, sending) as provider:
            outcome = run_exploration(
                database,
                task.request,
                TaskApproval(task.fingerprint),
                _Stoppable(provider, store, task_id),
                clock=clock,
                cancel=_Signal(store, task_id),
                # The recipient the user confirmed decides where the request may go, never the provider handed over.
                live=task.request.recipient.provider != SIMULATION_PROVIDER,
            )
    except TaskStopped:
        # The store already says why it stopped; nothing of the answer is taken.
        outcome = ended(ExplorationStatus.CANCELLED)
    except ServiceRefused as refused:
        # Stopped before it was handed on; anything that went out before it still counts.
        outcome = ended(ExplorationStatus.REJECTED, GATE_PREFIX + refused.reason)
    except ServiceUnavailable:
        logger.error("a real direction service could not be used")
        outcome = ended(ExplorationStatus.FAILED, AnalysisFailure.PROVIDER_UNAVAILABLE.value)
    except AIProviderConfigError:
        # Stopped by its provider's own setup before it went out.
        logger.error("a direction request was stopped by its provider's setup")
        outcome = ended(ExplorationStatus.FAILED, AnalysisFailure.PROVIDER_CONFIG.value)
    except LedgerFailed:
        logger.error("the real service's ledger could not be read or written for a direction exploration")
        outcome = ended(ExplorationStatus.FAILED, LEDGER_FAILED)
    except Exception as error:  # the page must learn that it failed; no text is logged
        logger.error("a direction exploration failed unexpectedly (%s)", type(error).__name__)
        outcome = ended(ExplorationStatus.FAILED, INTERNAL_ERROR)

    # One count for every outcome, the store's own: the request that went out or may have.
    store.finish(task_id, replace(outcome, requests_sent=sending.sends.total))
