"""Running a confirmed task in the background (U2-A2, U2-C2).

A started task runs on a small thread pool of its own, so pages, cancelling
and the library keep answering while a service is busy. The task's provider
reports the step before each request is asked for, which is how the page
knows the step.

The analysis checks the task's signal before and after each request
(app.experience_analysis). That signal is the store's own judgement: the
task was not cancelled and is still within its time (TaskStore.still_running),
so a task past its limit stops at the next check even if no page asks about
it. The provider asks the store once more before a request is asked for.

The store has the last word right before a request is handed to the
provider (TaskStore.hand_on), after the gate of a real service has reserved
its cost and made its own checks: a stop that came first keeps the request
back, and the gate releases the reservation. From there the request is out:
a stop cannot call it back, and its late answer is dropped (TaskStore.finish).
It is counted once it comes back, by kind (a real service's count of the
input, or a generation) and as sent or maybe sent (app.live_gate.delivery),
the rule the ledger is settled by: a timeout or a lost connection counts as
maybe; a request its provider stopped before sending, with nothing reported
for it, not at all.

A request the gate stops ends the task as rejected with the gate's reason,
a count that failed or gave no usable number among them (no rule fallback
follows it); a provider that cannot be built, or is not the approved one,
as failed with nothing sent; a request its provider stops on its own setup,
as failed with that reason. Each keeps the count of the requests sent
before, a count that went out included.
"""
import logging
from dataclasses import replace

from app.ai_provider import AIProvider, AIProviderConfigError
from app.analysis_contracts import AnalysisFailure, AnalysisResult, AnalysisStatus, RejectionReason, TaskApproval
from app.experience_analysis import analyze_selected_experiences
from app.job_analysis.service import ProviderUnavailable
from app.job_analysis.tasks import Step, TaskStore
from app.library.sends import Call, Delivery, Sends
from app.live_gate import Blocked, LiveBlocked, LiveRefused

logger = logging.getLogger("roleradar.job_analysis")
# A failure of this program, not of the service or the answer.
INTERNAL_ERROR = "internal_error"
GATE_REASONS = {
    Blocked.STOPPED: RejectionReason.GATE_STOPPED,
    Blocked.PAUSED: RejectionReason.GATE_PAUSED,
    Blocked.BUSY: RejectionReason.GATE_BUSY,
    Blocked.PURPOSE: RejectionReason.GATE_PURPOSE,
    Blocked.REQUESTS: RejectionReason.GATE_REQUESTS,
    Blocked.COST: RejectionReason.GATE_COST,
    Blocked.CLOSED: RejectionReason.GATE_CLOSED,
    Blocked.ENDED: RejectionReason.GATE_ENDED,
    Blocked.COUNT: RejectionReason.GATE_COUNT,
    Blocked.TIME: RejectionReason.GATE_TIME,
    Blocked.INPUT: RejectionReason.GATE_INPUT,
    Blocked.PAYLOAD: RejectionReason.GATE_PAYLOAD,
}


class TaskStopped(Exception):
    """The task may not go on: cancelled, ended with its session, or out of time."""


class _Signal:
    """What the analysis asks before and after each request: may this task go on?"""

    def __init__(self, store: TaskStore, task_id: str):
        self._store = store
        self._task_id = task_id

    def is_set(self) -> bool:
        return not self._store.still_running(self._task_id)


class _Reporting(AIProvider):
    """The task's provider, telling the store the step before each request is asked for."""

    def __init__(self, provider: AIProvider, store: TaskStore, task_id: str):
        self._provider = provider
        self._store = store
        self._task_id = task_id
        self._asked = 0

    def identity(self):
        return self._provider.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        # A task stopped already reserves nothing; one that stops later is still kept back (TaskStore.hand_on).
        if not self._store.still_running(self._task_id):
            raise TaskStopped()

        self._asked += 1
        self._store.progress(self._task_id, Step.PARSING if self._asked == 1 else Step.ANALYSING)

        return self._provider.generate_text(
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            json_schema_format=json_schema_format,
        )


class _Sending:
    """Where the task's requests are counted (app.live_gate.Sending): in the store, and here for its result."""

    def __init__(self, store: TaskStore, task_id: str):
        self._store = store
        self._task_id = task_id
        self.sends = Sends()

    def hand_on(self, call: Call) -> None:
        if not self._store.hand_on(self._task_id, call):
            raise TaskStopped()

    def came_back(self, call: Call, delivered: Delivery) -> None:
        self._store.came_back(self._task_id, call, delivered)
        self.sends = self.sends.plus(call, delivered)


def run_task(store: TaskStore, database, task_id: str, provider_for) -> None:
    """Run a task the store has started; its outcome goes back to the store, nowhere else.

    provider_for(task, sending) gives the task's provider as a context
    (app.job_analysis.service): the simulation's, or a real one behind its gate.
    """
    task = store.running(task_id)

    if task is None:
        return

    sending = _Sending(store, task_id)

    def ended(status: AnalysisStatus, problem: str | None = None) -> AnalysisResult:
        return AnalysisResult(
            status=status,
            task_fingerprint=task.fingerprint,
            mode="simulation" if task.request.recipient.provider == "simulation" else "live",
            requests_sent=sending.sends.total,
            problem=problem,
        )

    try:
        with provider_for(task, sending) as provider:
            result = analyze_selected_experiences(
                database,
                task.request,
                TaskApproval(task.fingerprint),
                _Reporting(provider, store, task_id),
                cancel=_Signal(store, task_id),
            )
    except TaskStopped:
        # The store already says why it stopped; this only carries the count of what went out.
        result = ended(AnalysisStatus.CANCELLED)
    except LiveBlocked as blocked:
        # The gate stopped a request before it was handed on; any sent before it still count.
        result = ended(AnalysisStatus.REJECTED, GATE_REASONS[blocked.reason].value)
    except (ProviderUnavailable, LiveRefused) as error:
        logger.error("a real analysis service could not be used (%s)", type(error).__name__)
        result = ended(AnalysisStatus.FAILED, AnalysisFailure.PROVIDER_UNAVAILABLE.value)
    except AIProviderConfigError:
        # Stopped by its provider's own setup before it went out; any sent before it still count.
        logger.error("an analysis request was stopped by its provider's setup")
        result = ended(AnalysisStatus.FAILED, AnalysisFailure.PROVIDER_CONFIG.value)
    except Exception as error:  # the page must learn that it failed; no text is logged
        logger.error("an analysis task failed unexpectedly (%s)", type(error).__name__)
        result = ended(AnalysisStatus.FAILED, INTERNAL_ERROR)

    # One count for every outcome, the store's own: counts and generations that went out or may have
    # (the store keeps them apart, by kind and by how sure).
    store.finish(task_id, replace(result, requests_sent=sending.sends.total))
