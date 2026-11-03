"""The real structuring runs of this process, and the boundary of each one's request (U2-C3).

A confirmed run (app.live_structuring) is entered here in the transaction that
starts it, before it is committed, with two moments: when its import stops
being in use (a temporary import's expiry, fixed when it was made; a saved
draft has none and ends only when it is deleted) and when the run itself is
over (review.STALE_STRUCTURING_AFTER after it began, by the clock the
database's records are judged by). Every stop takes this registry's lock:
cancelling, logging out, ending the import (in that operation's own
transaction, before it commits), a newer run of the same import, shutting
down.

hand_on is the boundary of each request of the run: the count of its input
and the generation after it, a real service's one request of an approval
(app.live_gate). Right before either is handed to the provider, after the
gate has reserved its cost and made its own checks, the run is checked
under this lock: not stopped, and within both its import's time and its
own. Only then is that request counted as out. A stop that comes first
keeps it back: before the count, the gate releases its reservation; after
it, only the count is charged. Once a request is out, no stop can call it
back; it is counted when it comes back, as sent or maybe sent, and settled.
Its answer is then claimed under the same lock, checked the same way: a run
stopped or out of time meanwhile drops it. A stop after the claim is too
late. The lock is only held to read and change these records and the clock:
never for the database, and never while a request is out.
"""
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.library.sends import Call, Delivery, Sends


class Stop(StrEnum):
    """Why a run may not hand on its request or keep its answer; its value is the run's outcome."""

    CANCELLED = "cancelled"
    LOGGED_OUT = "logged_out"
    # Its import stopped being in use by its time.
    EXPIRED = "expired"
    RUN_TIMEOUT = "run_timeout"
    # Its import was ended (discarded, finished, its draft deleted), or the run is unknown here.
    ENDED = "ended"
    # A newer run of the same import began.
    REPLACED = "replaced"
    SHUTDOWN = "shutdown"


class Stopping(StrEnum):
    """What stopping a run did."""

    # No run of this process: none began here, or it is over.
    UNKNOWN = "unknown"
    # Its answer was claimed first: the stop came too late.
    TOO_LATE = "too_late"
    # Nothing was handed on, and nothing will be.
    BEFORE = "before"
    # A request of it was handed on (its count, perhaps its generation too): settled when it comes back,
    # and its answer dropped.
    AFTER = "after"


class RunStopped(Exception):
    """The run may not hand on its request; nothing was handed on."""

    def __init__(self, why: Stop):
        super().__init__(why.value)
        self.why = why


@dataclass
class Run:
    attempt: str
    owner_id: str
    # The hash of the session's token, never the token.
    session: str
    import_id: str
    # When its import stops being in use; None for a saved draft.
    valid_until: datetime | None
    deadline: datetime
    stopped: Stop | None = None
    # Its requests handed on and not back yet, each counted as maybe sent; and those back, by kind
    # and by how sure (app.library.sends).
    out: Sends = Sends()
    sends: Sends = Sends()
    claimed: bool = False


class StructuringRuns:
    """All real structuring runs of this process. Every method holds one lock, briefly."""

    def __init__(self, clock: Callable[[], datetime]):
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}

    def begin(self, run: Run) -> None:
        """Enter a run (in the transaction that starts it); an older run of the same import is stopped."""
        with self._lock:
            for other in self._runs.values():
                if other.import_id == run.import_id:
                    other.stopped = other.stopped or Stop.REPLACED

            self._runs[run.attempt] = run

    def forget(self, attempt: str) -> None:
        with self._lock:
            self._runs.pop(attempt, None)

    def hand_on(self, attempt: str, call: Call) -> Stop | None:
        """Count a request of the run as out, or say why it may not go (see the module docstring)."""
        with self._lock:
            run = self._runs.get(attempt)

            if run is None:
                return Stop.ENDED

            why = self._why_not(run)

            if why is None:
                run.out = run.out.plus(call, Delivery.MAYBE)

            return why

    def came_back(self, attempt: str, call: Call, delivered: Delivery) -> None:
        """A request handed on came back: sent, maybe sent or not sent (app.live_gate.delivery)."""
        with self._lock:
            run = self._runs.get(attempt)

            if run is not None:
                run.out = run.out - Sends().plus(call, Delivery.MAYBE)
                run.sends = run.sends.plus(call, delivered)

    def claim(self, attempt: str) -> Stop | None:
        """Take the run's answer, or say why it is dropped; no stop takes effect after this."""
        with self._lock:
            run = self._runs.get(attempt)

            if run is None:
                return Stop.ENDED

            why = self._why_not(run)

            if why is None:
                run.claimed = True

            return why

    def stop(self, attempt: str, why: Stop) -> Stopping:
        """Stop one run, and say whether its request was handed on before."""
        with self._lock:
            run = self._runs.get(attempt)

            if run is None:
                return Stopping.UNKNOWN

            if run.claimed:
                return Stopping.TOO_LATE

            run.stopped = run.stopped or why

            return Stopping.AFTER if (run.out + run.sends).total else Stopping.BEFORE

    def stop_import(self, import_id: str) -> None:
        """Stop the runs of an import that is being ended: called in that transaction, before it commits."""
        self._stop_where(lambda run: run.import_id == import_id, Stop.ENDED)

    def end_session(self, session: str) -> None:
        """Stop the runs a session started (it logged out, or another user signed in on it)."""
        self._stop_where(lambda run: run.session == session, Stop.LOGGED_OUT)

    def shutdown(self) -> None:
        self._stop_where(lambda run: True, Stop.SHUTDOWN)

    def stopped(self, attempt: str) -> Stop | None:
        """The stop a run was given (cancelled, logged out, ended, replaced, shut down), if any."""
        with self._lock:
            run = self._runs.get(attempt)

            return None if run is None else run.stopped

    def handed(self, attempt: str) -> Sends | None:
        """What the pages call 已交给服务 for a run of this process (back, or still out as maybe); None if none."""
        with self._lock:
            run = self._runs.get(attempt)

            return None if run is None else run.sends + run.out

    def _stop_where(self, chosen: Callable[[Run], bool], why: Stop) -> None:
        with self._lock:
            for run in self._runs.values():
                if chosen(run):
                    run.stopped = run.stopped or why

    def _why_not(self, run: Run) -> Stop | None:
        """Why a run may not go on now, if it may not; call with the lock held."""
        if run.stopped is not None:
            return run.stopped

        now = self._clock()

        if run.valid_until is not None and now >= run.valid_until:
            return Stop.EXPIRED

        if now >= run.deadline:
            return Stop.RUN_TIMEOUT

        return None
