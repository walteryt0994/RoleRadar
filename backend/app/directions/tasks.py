"""Direction explorations as tasks of one user and one session, kept in this process only (U3-C3a).

A task is created when the user asks to review what an exploration would send: the server works out the plan
(app.directions.explore.build_plan) and keeps it with the request and its fingerprint. The browser only ever sends
the task's id and the fingerprint it was shown; a task starts once, from what is kept here (begin), and nothing a
page says can widen it. As with the analysis and search tasks (app.job_analysis.tasks, app.discovery.tasks), each
belongs to one user and one session: another user, another session or an ended session can neither see, start,
cancel nor save it, even with its id. A session runs one exploration at a time.

Tasks live in memory, a limited number (MAX_TASKS, MAX_TASKS_PER_SESSION) for a limited time: TASK_TTL_SECONDS
after their last change (being looked at is no change), a running one at most MAX_RUN_SECONDS. Nothing of them is
written to the database, a file, a cookie or a log, and a restart forgets them all. Ending a session cancels its
running task and forgets its tasks.

The store has the last word right before the request is handed to the service (hand_on): a stop that came first
keeps it back, unsent and uncounted; once it is out, a stop cannot call it back, it is counted when it comes back
(came_back), and its late answer is dropped (finish).

What a finished exploration leaves is its outcome (app.directions.explore.ExplorationOutcome) and, when the outcome
has a result (complete or stale), the sources that result rests on as the request sent them (basis), which every
view checks again. A result whose sources were deleted is withdrawn (withdraw): the outcome keeps its status and
its count, but its result, its trusted snapshot and everything derived from them are dropped for good, so restoring
the sources never brings it back.

A task also remembers the exploration saved from its result (saved_id), once saved, for as long as it lives: the
saved record may later be deleted and even removed from the database, and the task must still know that its result
was saved, never take it for one never saved (U3-C3a R1). Saving the result and deciding on it are the task's
writes, one at a time (claim_write, release_write): a write claims the task before it reads anything and releases
it once its transaction is over, naming the exploration it saved, so no other write of the task runs between a
save's commit and the task remembering it. A task past its time is never claimed: claiming sweeps first, as every
look at a task does (U3-C3a R2).
"""
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum

from app.directions.contracts import SourceKind, SourceRef
from app.directions.explore import ExplorationInputs, ExplorationOutcome, ExplorationPlan, ExplorationRequest
from app.library.sends import Call, Delivery, Sends

TASK_TTL_SECONDS = 30 * 60
MAX_RUN_SECONDS = 5 * 60
MAX_TASKS = 32
MAX_TASKS_PER_SESSION = 4


class Stage(StrEnum):
    # Reviewed, waiting for the user's confirmation; nothing was sent.
    REVIEW = "review"
    RUNNING = "running"
    # The exploration gave its outcome, whatever its status.
    DONE = "done"
    CANCELLED = "cancelled"


class Start(StrEnum):
    STARTED = "started"
    # Started before: a second click, another tab or a replayed form.
    ALREADY = "already"
    # The form belongs to another review than the one kept for this task.
    STALE_FORM = "stale_form"
    # Another exploration of this session is running.
    BUSY = "busy"
    GONE = "gone"


class Claim(StrEnum):
    CLAIMED = "claimed"
    # Another write of this task (a save, a decision) has not finished.
    BUSY = "busy"
    GONE = "gone"


class TooManyTasks(Exception):
    pass


@dataclass
class ExplorationTask:
    id: str
    owner_id: str
    # The hash of the session's token, never the token.
    session: str
    inputs: ExplorationInputs
    plan: ExplorationPlan
    request: ExplorationRequest
    fingerprint: str
    stage: Stage = Stage.REVIEW
    cancel: threading.Event = field(default_factory=threading.Event)
    # Requests that came back having gone out, or perhaps gone out (app.library.sends); also for a cancelled task.
    sends: Sends = Sends()
    # Requests handed on that have not come back yet, each counted as maybe sent.
    out: Sends = Sends()
    outcome: ExplorationOutcome | None = None
    # The sources a result rests on, as the request sent them: for a complete result and for a stale one.
    basis: tuple[SourceRef, ...] = ()
    # The result was dropped because a source it rests on was deleted (withdraw).
    withdrawn: bool = False
    # The exploration saved from the result, once the user saved it; never cleared (see the module docstring).
    saved_id: str | None = None
    # A write of the task (saving, deciding) is under way.
    writing: bool = False
    # "user", "logout", "timeout" or "shutdown".
    cancelled_by: str | None = None
    touched: float = 0.0
    started: float | None = None

    @property
    def handed(self) -> Sends:
        """The requests that came back, and those still out, which may have gone out."""
        return self.sends + self.out


def basis_of(request: ExplorationRequest) -> tuple[SourceRef, ...]:
    """The sources a result of this request rests on, as the request sent them: each experience with the fields
    sent, and the preferences with the saved version and the fields sent, when any were. The same a trusted
    snapshot names (app.directions.explore), and kept for a stale result too, which has no snapshot."""
    refs = [
        SourceRef(kind=SourceKind.EXPERIENCE, item_id=source.item_id, version_id=source.version_id, fields=tuple(source.fields))
        for source in request.sources
    ]
    preferences = request.preferences

    if preferences.sent:
        refs.append(
            SourceRef(
                kind=SourceKind.PREFERENCES,
                item_id=preferences.record_id,
                version_id=preferences.version_id,
                fields=tuple(item.field for item in preferences.sent),
            )
        )

    return tuple(refs)


class ExplorationTaskStore:
    """All exploration tasks of this process. Every method holds one lock; callers get copies."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tasks: dict[str, ExplorationTask] = {}

    def create(self, owner_id: str, session: str, inputs, plan, request, fingerprint: str) -> ExplorationTask:
        with self._lock:
            self._sweep()
            mine = [task for task in self._tasks.values() if task.session == session]
            self._drop_oldest(mine, len(mine) - MAX_TASKS_PER_SESSION + 1)
            self._drop_oldest(list(self._tasks.values()), len(self._tasks) - MAX_TASKS + 1)

            if len(self._tasks) >= MAX_TASKS:
                raise TooManyTasks()

            task = ExplorationTask(
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

    def get(self, task_id: str, owner_id: str, session: str) -> ExplorationTask | None:
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            return replace(task) if task else None

    def remaining(self, task: ExplorationTask) -> float:
        """Seconds left before a task that is not running is forgotten."""
        with self._lock:
            return max(0.0, TASK_TTL_SECONDS - (self._clock() - task.touched))

    def begin(self, task_id: str, owner_id: str, session: str, fingerprint: str) -> tuple[Start, ExplorationTask | None]:
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

            task.stage = Stage.RUNNING
            task.started = task.touched = self._clock()

            return Start.STARTED, replace(task)

    def running(self, task_id: str) -> ExplorationTask | None:
        with self._lock:
            task = self._tasks.get(task_id)

            return replace(task) if task is not None and self._in_time(task) else None

    def still_running(self, task_id: str) -> bool:
        """Whether the task may go on: running, not cancelled, within its time (cancelled here once past it)."""
        with self._lock:
            task = self._tasks.get(task_id)

            return task is not None and self._in_time(task)

    def hand_on(self, task_id: str, call: Call = Call.GENERATION) -> bool:
        """Whether the request may be handed on now; if so, it is out from here (see the module docstring)."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None or not self._in_time(task):
                return False

            task.out = task.out.plus(call, Delivery.MAYBE)

            return True

    def came_back(self, task_id: str, call: Call, delivered: Delivery) -> None:
        """A request handed on came back: counted as sent or as maybe sent, even for a cancelled task."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is not None:
                task.out = task.out - Sends().plus(call, Delivery.MAYBE)
                task.sends = task.sends.plus(call, delivered)

    def finish(self, task_id: str, outcome: ExplorationOutcome) -> None:
        """The exploration returned. A task cancelled meanwhile, or past its time, stays as it is: its late
        answer is dropped; what went out stays counted."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None:
                return

            unseen = outcome.requests_sent - task.handed.total

            if unseen > 0:
                task.sends = task.sends + Sends(generation_maybe=unseen)

            if self._in_time(task):
                task.stage, task.outcome = Stage.DONE, outcome
                task.basis = basis_of(task.request) if outcome.result is not None else ()
                task.touched = self._clock()

    def withdraw(self, task_id: str, owner_id: str, session: str) -> ExplorationTask | None:
        """Drop a finished task's result for good, keeping its status and its count: a source it rests on is gone."""
        with self._lock:
            task = self._mine(task_id, owner_id, session)

            if task is None:
                return None

            if task.outcome is not None and task.outcome.result is not None:
                task.outcome = replace(task.outcome, result=None, trusted=None, set_aside=())
                task.basis = ()
                task.withdrawn = True

            return replace(task)

    def claim_write(self, task_id: str, owner_id: str, session: str) -> tuple[Claim, ExplorationTask | None]:
        """Claim the task for one write (a save or a decision): one at a time, released by release_write.

        A task past its time is gone here as for every other look at it (_sweep, as get does), whether or not a
        page or the app's sweeping came first: an old form posted straight after the time is up writes nothing
        (U3-C3a R2). Claiming changes nothing of the task's time."""
        with self._lock:
            self._sweep()
            task = self._mine(task_id, owner_id, session)

            if task is None:
                return Claim.GONE, None

            if task.writing:
                return Claim.BUSY, replace(task)

            task.writing = True

            return Claim.CLAIMED, replace(task)

    def release_write(self, task_id: str, saved_id: str | None = None) -> None:
        """The write is over; saved_id names the exploration it saved, which the task keeps from now on."""
        with self._lock:
            task = self._tasks.get(task_id)

            if task is None:
                return

            task.writing = False

            if saved_id is not None and task.saved_id is None:
                task.saved_id = saved_id

    def cancel(self, task_id: str, owner_id: str, session: str) -> ExplorationTask | None:
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

    def _mine(self, task_id: str, owner_id: str, session: str) -> ExplorationTask | None:
        task = self._tasks.get(task_id)

        if task is None or task.owner_id != owner_id or task.session != session:
            return None

        return task

    def _in_time(self, task: ExplorationTask) -> bool:
        """Running and within MAX_RUN_SECONDS; a running task past it is cancelled here."""
        if task.stage is not Stage.RUNNING:
            return False

        if self._clock() - task.started > MAX_RUN_SECONDS:
            self._cancel(task, "timeout")
            return False

        return True

    def _cancel(self, task: ExplorationTask, by: str) -> None:
        if task.stage in (Stage.REVIEW, Stage.RUNNING):
            task.stage, task.cancelled_by = Stage.CANCELLED, by
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

    def _drop_oldest(self, tasks: list[ExplorationTask], count: int) -> None:
        """Forget the oldest tasks that are not running, to make room."""
        for task in sorted((task for task in tasks if task.stage is not Stage.RUNNING), key=lambda task: task.touched)[
            : max(count, 0)
        ]:
            task.cancel.set()
            del self._tasks[task.id]
