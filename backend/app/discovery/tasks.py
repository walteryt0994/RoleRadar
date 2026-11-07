"""Searches for postings as tasks of one user and one session, kept in this process only (U3-A2).

A task holds what the user confirmed for one search (ParentConfirmation) and which flow runs it: the agent, or the
fixed flow it is compared with (Flow). It starts once, from the confirmation kept here, and only with the
fingerprint of that very confirmation (begin), which makes its approval (SearchApproval): nothing a page sends can
widen it. As with the analysis tasks (app.job_analysis.tasks), each belongs to one user and one session: another
user, another session or an ended session can neither see, start nor cancel it, even with its id. A session runs
one search at a time. Tasks live in memory, a limited number (MAX_TASKS, MAX_TASKS_PER_SESSION) for a limited time
(TASK_TTL_SECONDS after their last change); nothing of them is written to the database, a file, a cookie or a log,
and a restart forgets them all. Ending a session cancels its running search and forgets its tasks.

A running search has the time its confirmation gives it (Limits.seconds, ten minutes by default), counted from when
it started. The store's word on whether a request may be handed on is final (hand_on): once a task is cancelled,
logged out, shut down or out of its time, nothing more goes. What comes back after that is still counted
(came_back), and what it carried is never taken: the run takes nothing once it stopped (app.discovery.tools). The
run keeps time with the store's own clock, from the same start, so a search out of time ends as a timeout, and only
a user, a logout or a shutdown cancels it (stopped).

One executor runs a started task (A2 acceptance, 7.3): it takes the right atomically before anything else (claim), and
a second one dispatched for the same task gets nothing and must leave it alone. Only the holder may say how the search
ended, and only once (finish): nothing said afterwards, by it or anyone, changes that.

What a search leaves when it ends is its outcome and its short trace (app.discovery.agent.Finished), or why it did not
run (problem). Whether that is taken is decided under the store's lock, against the task as it is then (A2 acceptance,
7.2): still running within its time, the task is done and the result published. Stopped meanwhile, it stays stopped,
and its result is kept only when the search itself ended for that very stop (STOP_ENDS), so the task, the outcome and
the trace say the same; a result that came back complete after the stop is late and dropped, as a late answer of an
analysis task is. Either way the requests it sent stay counted.
"""
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum

from app.discovery.agent import Finished
from app.discovery.contracts import EndReason, ParentConfirmation, SearchApproval, search_fingerprint
from app.library.sends import Call, Delivery, Sends

TASK_TTL_SECONDS = 30 * 60
MAX_TASKS = 32
MAX_TASKS_PER_SESSION = 4
# Who may stop a task, besides its time running out.
STOPPED_BY = ("user", "logout", "shutdown")
# How a search must itself have ended for its result to be kept once its task was stopped, by who stopped it.
STOP_ENDS = {"user": EndReason.CANCELLED, "logout": EndReason.CANCELLED, "shutdown": EndReason.CANCELLED, "timeout": EndReason.TIMEOUT}


class Flow(StrEnum):
    AGENT = "agent"
    # The fixed flow of U3 proposal 4.3, for comparing (app.discovery.baseline).
    BASELINE = "baseline"


class Stage(StrEnum):
    # Waiting for the user's confirmation: nothing was sent.
    REVIEW = "review"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class Start(StrEnum):
    STARTED = "started"
    # Started before: a second click, another tab or a replayed form.
    ALREADY = "already"
    # The form belongs to another confirmation than the one kept for this task.
    STALE_FORM = "stale_form"
    # Another search of this session is running.
    BUSY = "busy"
    GONE = "gone"


class TooManyTasks(Exception):
    pass


@dataclass
class DiscoveryTask:
    id: str
    owner_id: str
    # The hash of the session's token, never the token.
    session: str
    confirmation: ParentConfirmation
    flow: Flow
    fingerprint: str
    stage: Stage = Stage.REVIEW
    # Made when it starts, from the fingerprint the user confirmed.
    approval: SearchApproval | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Requests that came back having gone out or perhaps gone out, by kind; also for a cancelled task.
    sends: Sends = Sends()
    # Requests handed on that have not come back yet, each counted as maybe sent.
    out: Sends = Sends()
    finished: Finished | None = None
    # Why it did not run, when it did not (app.discovery.runner), and the problems found, if any.
    problem: str | None = None
    problems: tuple[str, ...] = ()
    # "user", "logout", "timeout" or "shutdown".
    cancelled_by: str | None = None
    touched: float = 0.0
    started: float | None = None
    # The token of the one executor that claimed it; none other may run it or say how it ended.
    worker: str | None = None
    # Whether its executor has said how it ended: nothing may change that afterwards.
    over: bool = False

    @property
    def seconds(self) -> float:
        return self.confirmation.limits.seconds

    @property
    def handed(self) -> Sends:
        """The requests handed on: those that came back, and those still out, which may have gone out."""
        return self.sends + self.out


class DiscoveryTaskStore:
    """All searches of this process. Every method holds one lock; callers get copies."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, DiscoveryTask] = {}

    def create(self, owner_id: str, session: str, confirmation: ParentConfirmation, flow: Flow = Flow.AGENT) -> DiscoveryTask:
        if confirmation.owner_id != owner_id:
            raise ValueError("a search is confirmed by its own user")

        with self._lock:
            self._sweep()
            mine = [task for task in self._tasks.values() if task.session == session]
            self._drop_oldest(mine, len(mine) - MAX_TASKS_PER_SESSION + 1)
            self._drop_oldest(list(self._tasks.values()), len(self._tasks) - MAX_TASKS + 1)

            if len(self._tasks) >= MAX_TASKS:
                raise TooManyTasks()

            task = DiscoveryTask(
                id=secrets.token_urlsafe(16),
                owner_id=owner_id,
                session=session,
                confirmation=confirmation,
                flow=Flow(flow),
                fingerprint=search_fingerprint(confirmation),
                touched=self.clock(),
            )
            self._tasks[task.id] = task

            return replace(task)

    def get(self, task_id: str, owner_id: str, session: str) -> DiscoveryTask | None:
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            return replace(task) if task else None

    def begin(self, task_id: str, owner_id: str, session: str, fingerprint: str) -> tuple[Start, DiscoveryTask | None]:
        """Start a task once, with the fingerprint of the confirmation kept for it: the only way to RUNNING."""
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

            task.stage, task.approval = Stage.RUNNING, SearchApproval(task.fingerprint)
            task.started = task.touched = self.clock()

            return Start.STARTED, replace(task)

    def running(self, task_id: str) -> DiscoveryTask | None:
        with self._lock:
            task = self._tasks.get(task_id)

            return replace(task) if task is not None and self._in_time(task) else None

    def claim(self, task_id: str) -> tuple[str, DiscoveryTask] | None:
        """The one right to run a started task, taken atomically: a token with a copy of the task, or None when the
        task is not running, is out of its time, or another executor holds it."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None or task.worker is not None or not self._in_time(task):
                return None

            task.worker = secrets.token_urlsafe(16)

            return task.worker, replace(task)

    def still_running(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)

            return task is not None and self._in_time(task)

    def stopped(self, task_id: str) -> bool:
        """Whether someone stopped the task (the user, a logout, a shutdown) or it is gone: the search's cancel signal.

        Its time running out is not among them: the search keeps the same time and ends as a timeout.
        """
        with self._lock:
            task = self._tasks.get(task_id)

            return task is None or task.cancelled_by in STOPPED_BY

    def hand_on(self, task_id: str, call: Call = Call.GENERATION) -> bool:
        """Whether a request of the task may be handed on now; if so, it is out from here (see the module docstring)."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None or not self._in_time(task):
                return False

            task.out = task.out.plus(call, Delivery.MAYBE)

            return True

    def came_back(self, task_id: str, call: Call, delivered: Delivery) -> None:
        """A request handed on came back: counted as sent or as maybe sent, even for a task stopped meanwhile."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is not None:
                task.out = task.out - Sends().plus(call, Delivery.MAYBE)
                task.sends = task.sends.plus(call, delivered)

    def finish(
        self,
        task_id: str,
        *,
        worker: str | None = None,
        finished: Finished | None = None,
        problem: str | None = None,
        problems: tuple[str, ...] = (),
    ) -> None:
        """How the search ended, or why it did not run: said once, by the executor holding the task; anything else is
        ignored. Taken or dropped under the lock, against the task as it is now (see the module docstring)."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None or task.over or worker is None or task.worker != worker:
                return

            task.over, task.touched = True, self.clock()

            if self._in_time(task):
                task.stage = Stage.DONE
                task.finished, task.problem, task.problems = finished, problem, problems
            elif finished is not None and finished.outcome.end is STOP_ENDS.get(task.cancelled_by):
                # Stopped, and the search ended for that very stop: what it had found by then may be shown.
                task.finished = finished

    def cancel(self, task_id: str, owner_id: str, session: str) -> DiscoveryTask | None:
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            if task is None:
                return None

            if task.stage in (Stage.REVIEW, Stage.RUNNING):
                self._cancel(task, "user")

            return replace(task)

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

    def _mine(self, task_id: str, owner_id: str, session: str) -> DiscoveryTask | None:
        task = self._tasks.get(task_id)

        if task is None or task.owner_id != owner_id or task.session != session:
            return None

        return task

    def _in_time(self, task: DiscoveryTask) -> bool:
        """Running and within its time; a running task past it is cancelled here, as a timeout."""
        if task.stage is not Stage.RUNNING:
            return False

        if self.clock() - task.started >= task.seconds:
            self._cancel(task, "timeout")

            return False

        return True

    def _cancel(self, task: DiscoveryTask, by: str) -> None:
        if task.stage in (Stage.REVIEW, Stage.RUNNING):
            task.stage, task.cancelled_by = Stage.CANCELLED, by
            task.touched = self.clock()

        task.cancel.set()

    def _sweep(self) -> None:
        now = self.clock()

        for task in list(self._tasks.values()):
            if task.stage is Stage.RUNNING:
                self._in_time(task)
            elif now - task.touched > TASK_TTL_SECONDS:
                task.cancel.set()
                del self._tasks[task.id]

    def _drop_oldest(self, tasks: list[DiscoveryTask], count: int) -> None:
        """Forget the oldest tasks that are not running, to make room."""
        for task in sorted((task for task in tasks if task.stage is not Stage.RUNNING), key=lambda task: task.touched)[
            : max(count, 0)
        ]:
            task.cancel.set()
            del self._tasks[task.id]
