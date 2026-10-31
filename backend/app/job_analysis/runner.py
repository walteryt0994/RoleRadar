"""Running a confirmed task in the background (U2-A2).

A started task runs on a small thread pool of its own, so pages, cancelling
and the library keep answering while a service is busy. The task's provider
reports each request just before it goes out, which is how the page knows
the step.

The analysis checks the task's signal before and after each request
(app.experience_analysis). That signal is the store's own judgement: the
task was not cancelled and is still within its time (TaskStore.still_running),
so a task past its limit stops at the next check even if no page asks about
it. The provider asks the store once more right before handing a request
over. A request already handed to the service cannot be called back; its
late answer is dropped (TaskStore.finish).
"""
import logging

from app.ai_provider import AIProvider
from app.analysis_contracts import AnalysisResult, AnalysisStatus, TaskApproval
from app.experience_analysis import analyze_selected_experiences
from app.job_analysis.tasks import Step, TaskStore

logger = logging.getLogger("roleradar.job_analysis")
# A failure of this program, not of the service or the answer.
INTERNAL_ERROR = "internal_error"


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
    """The task's provider, telling the store about each request before it goes out."""

    def __init__(self, provider: AIProvider, store: TaskStore, task_id: str):
        self._provider = provider
        self._store = store
        self._task_id = task_id
        self.calls = 0

    def identity(self):
        return self._provider.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        # The last check before the request is handed over; nothing is counted as sent before it.
        if not self._store.still_running(self._task_id):
            raise TaskStopped()

        self.calls += 1
        self._store.progress(self._task_id, Step.PARSING if self.calls == 1 else Step.ANALYSING)

        return self._provider.generate_text(
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            json_schema_format=json_schema_format,
        )


def run_task(store: TaskStore, database, task_id: str, make_provider) -> None:
    """Run a task the store has started; its outcome goes back to the store, nowhere else."""
    task = store.running(task_id)

    if task is None:
        return

    provider = _Reporting(make_provider(task), store, task_id)

    try:
        result = analyze_selected_experiences(
            database, task.request, TaskApproval(task.fingerprint), provider, cancel=_Signal(store, task_id)
        )
    except TaskStopped:
        # The store already says why it stopped; this only carries the count of what went out.
        result = AnalysisResult(
            status=AnalysisStatus.CANCELLED,
            task_fingerprint=task.fingerprint,
            mode="simulation" if task.request.recipient.provider == "simulation" else "live",
            requests_sent=provider.calls,
        )
    except Exception as error:  # the page must learn that it failed; no text is logged
        logger.error("an analysis task failed unexpectedly (%s)", type(error).__name__)
        result = AnalysisResult(
            status=AnalysisStatus.FAILED,
            task_fingerprint=task.fingerprint,
            mode="simulation" if task.request.recipient.provider == "simulation" else "live",
            requests_sent=provider.calls,
            problem=INTERNAL_ERROR,
        )

    store.finish(task_id, result)
