"""Temporary analysis tasks, kept in this process only (U2-A2).

A task is created when the user asks to review a scope: the server reads the
chosen material, works out what may go out (scope.ScopePlan) and keeps that
plan with the analysis request and its fingerprint. The browser only ever
sends the task's id and the fingerprint it was shown. A task starts once,
from the plan kept here; nothing the browser says can widen it.

Each task belongs to one user and one session: another user, another
session or an ended session cannot see, start or cancel it, even with its
id. Tasks live in memory, a limited number (MAX_TASKS, MAX_TASKS_PER_SESSION)
for a limited time (TASK_TTL_SECONDS after their last change; a running one
at most MAX_RUN_SECONDS). Nothing of them is written to the database, a
file, a cookie or a log, and a restart forgets them all. Ending a session
cancels its running task and forgets its tasks.

Time limits never wait for someone to look at a page. A running task is
checked before each of its requests goes out and when its result comes back
(still_running, finish): past MAX_RUN_SECONDS it is cancelled there, so its
next request is never sent and a late answer is dropped. The app also
sweeps every SWEEP_SECONDS (web_main), so an idle task's material is gone
at most TASK_TTL_SECONDS + SWEEP_SECONDS after its last change, and an
overdue running task is cancelled within SWEEP_SECONDS of its limit even
while its request is still out.
"""
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum

from app.analysis_contracts import AnalysisRequest, AnalysisResult
from app.job_analysis.scope import ScopePlan, TaskInputs

TASK_TTL_SECONDS = 30 * 60
MAX_RUN_SECONDS = 5 * 60
# How often the app sweeps the store while it runs.
SWEEP_SECONDS = 30
MAX_TASKS = 64
MAX_TASKS_PER_SESSION = 8


class Stage(StrEnum):
    # Reviewed, waiting for the user's confirmation; nothing was sent.
    REVIEW = "review"
    RUNNING = "running"
    # The analysis gave its result, whatever its status.
    DONE = "done"
    CANCELLED = "cancelled"


class Step(StrEnum):
    CHECKING = "checking"
    PARSING = "parsing"
    ANALYSING = "analysing"


class Start(StrEnum):
    STARTED = "started"
    # Started before: a second click, another tab or a replayed form.
    ALREADY = "already"
    # The form belongs to another review than the one kept for this task.
    STALE_FORM = "stale_form"
    # Another task of this session is running.
    BUSY = "busy"
    GONE = "gone"


class TooManyTasks(Exception):
    pass


@dataclass
class Task:
    id: str
    owner_id: str
    # The hash of the session's token, never the token.
    session: str
    inputs: TaskInputs
    plan: ScopePlan
    request: AnalysisRequest
    fingerprint: str
    stage: Stage = Stage.REVIEW
    step: Step | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Requests handed to the service so far; also counted for a cancelled task.
    requests_sent: int = 0
    result: AnalysisResult | None = None
    # "user", "logout", "timeout" or "shutdown".
    cancelled_by: str | None = None
    touched: float = 0.0
    started: float | None = None


class TaskStore:
    """All tasks of this process. Every method holds one lock; callers get copies."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}

    def create(self, owner_id: str, session: str, inputs, plan, request, fingerprint: str) -> Task:
        with self._lock:
            self._sweep()
            mine = [task for task in self._tasks.values() if task.session == session]
            self._drop_oldest(mine, len(mine) - MAX_TASKS_PER_SESSION + 1)
            self._drop_oldest(list(self._tasks.values()), len(self._tasks) - MAX_TASKS + 1)

            if len(self._tasks) >= MAX_TASKS:
                raise TooManyTasks()

            task = Task(
                id=secrets.token_urlsafe(16),
                owner_id=owner_id,
                session=session,
                inputs=inputs,
                plan=plan,
                request=request,
                fingerprint=fingerprint,
                touched=self._clock(),
            )
            self._tasks[task.id] = task

            return replace(task)

    def get(self, task_id: str, owner_id: str, session: str) -> Task | None:
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            return replace(task) if task else None

    def begin(self, task_id: str, owner_id: str, session: str, fingerprint: str) -> tuple[Start, Task | None]:
        """Start a reviewed task, once: the only way from REVIEW to RUNNING."""
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            if task is None:
                return Start.GONE, None

            if task.stage is not Stage.REVIEW:
                return Start.ALREADY, replace(task)

            if not secrets.compare_digest(fingerprint.encode(), task.fingerprint.encode()):
                return Start.STALE_FORM, replace(task)

            if any(other.session == session and other.stage is Stage.RUNNING for other in self._tasks.values()):
                return Start.BUSY, replace(task)

            task.stage, task.step = Stage.RUNNING, Step.CHECKING
            task.started = task.touched = self._clock()

            return Start.STARTED, replace(task)

    def running(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)

            return replace(task) if task is not None and self._in_time(task) else None

    def still_running(self, task_id: str) -> bool:
        """Whether the task may go on: running, not cancelled, within its time.

        Asked before each request goes out and around each call; a task past
        its time is cancelled right here, whether or not a page is open.
        """
        with self._lock:
            task = self._tasks.get(task_id)

            return task is not None and self._in_time(task)

    def progress(self, task_id: str, step: Step) -> None:
        """A request of the task is about to go out."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is not None:
                task.requests_sent += 1

                if task.stage is Stage.RUNNING:
                    task.step, task.touched = step, self._clock()

    def finish(self, task_id: str, result: AnalysisResult) -> None:
        """The analysis returned. A task cancelled meanwhile stays cancelled: its late answer is dropped."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None:
                return

            task.requests_sent = max(task.requests_sent, result.requests_sent)

            # An answer that comes back after the time limit is not taken either.
            if self._in_time(task):
                task.stage, task.step, task.result = Stage.DONE, None, result
                task.touched = self._clock()

    def cancel(self, task_id: str, owner_id: str, session: str) -> Task | None:
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            if task is None:
                return None

            if task.stage in (Stage.REVIEW, Stage.RUNNING):
                self._cancel(task, "user")

            return replace(task)

    def discard(self, task_id: str, owner_id: str, session: str) -> None:
        """Forget a task that was only reviewed, when a new review replaces it."""
        with self._lock:
            task = self._mine(task_id, owner_id, session)

            if task is not None and task.stage is not Stage.RUNNING:
                del self._tasks[task.id]

    def end_session(self, session: str) -> None:
        with self._lock:
            for task in [task for task in self._tasks.values() if task.session == session]:
                self._cancel(task, "logout")
                del self._tasks[task.id]

    def shutdown(self) -> None:
        with self._lock:
            for task in self._tasks.values():
                self._cancel(task, "shutdown")

            self._tasks.clear()

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def _mine(self, task_id: str, owner_id: str, session: str) -> Task | None:
        task = self._tasks.get(task_id)

        if task is None or task.owner_id != owner_id or task.session != session:
            return None

        return task

    def _in_time(self, task: Task) -> bool:
        """Running and within MAX_RUN_SECONDS; a running task past it is cancelled here."""
        if task.stage is not Stage.RUNNING:
            return False

        if self._clock() - task.started > MAX_RUN_SECONDS:
            self._cancel(task, "timeout")
            return False

        return True

    def _cancel(self, task: Task, by: str) -> None:
        if task.stage in (Stage.REVIEW, Stage.RUNNING):
            task.stage, task.step, task.cancelled_by = Stage.CANCELLED, None, by
            task.touched = self._clock()

        task.cancel.set()

    def _sweep(self) -> None:
        now = self._clock()

        for task in list(self._tasks.values()):
            if task.stage is Stage.RUNNING:
                self._in_time(task)
            elif now - task.touched > TASK_TTL_SECONDS:
                task.cancel.set()
                del self._tasks[task.id]

    def _drop_oldest(self, tasks: list[Task], count: int) -> None:
        """Forget the oldest tasks that are not running, to make room."""
        for task in sorted((task for task in tasks if task.stage is not Stage.RUNNING), key=lambda task: task.touched)[
            : max(count, 0)
        ]:
            task.cancel.set()
            del self._tasks[task.id]
