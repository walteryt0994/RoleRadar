"""Checks of a kept posting from its record (U3-D4a): 再核对一次, and 申请前核对 before the user applies.

One posting at a time, asked for by the user on their own record, and only on synthetic sources: the record's source
reached as synthetic data (TRACKABLE_ACCESS) and kept by the set the app runs on (SourceSet.keep), its entry still
synthetic and callable. Nothing here runs by itself, on a timer or for another posting.

What a check may conclude (D pre-check 10.2; D4 pre-check 2.1 and 10):
- open: its source answered for this very posting: a read naming it, then a check saying it is open; still published
  then, never that a place is free;
- closed, only with its evidence kept:
  - deadline: the source's own deadline, read in this check, before today (today does not count). The read and the
    check may each give one (F1): when both do and they differ, the check settles nothing (deadline_conflict) and keeps
    both; otherwise the one they give is the posting's deadline for its status, its facts, what changed, the pages
    and the export alike, with which answer gave it (deadline_answers, and the evidence's "from");
  - delisted: in this check, a 404 for it and a complete, unfiltered list without it, from a source whose 404 is
    confirmed (SourceSet.delisting: none yet in any set the app runs on);
  - synthetic: a synthetic source saying closed, said as such;
- unknown: its source answered and nothing it said settles it: a 404 alone, closed without evidence, another posting;
- unanswered: no answer: a timeout, a request kept back, an anomaly, any other failure, the source not connected.
Only open and closed are successes, so a failure never moves the time of the last success (job_choices.summarize).

Each check calls its source at most MAX_CALLS times (the read, the check, and after a 404 the list), each given
CALL_SECONDS, and never again. Nothing here reads an experience, the preferences or an analysis's content, and nothing
calls a model: it reads a public posting and compares its public facts with the version before. The posting's text is
held in memory only, for the session that asked, TEXT_TTL_SECONDS at most and never past its record's deletion, nor
past a current recommendation's time (ApplyChecks); it is never written, nor are its words in what changed.

A check is written only while its record stands as it did when the check began, at the same revision: deleted, purged
or changed since, nothing is written and its run says so. Whatever it finds, the user's decision is never touched.
"""
import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.discovery.contracts import Access, CheckStatus, EvidenceLevel, PostingDetail, PostingRef
from app.discovery.sources import HeldBack, NotFound, PartialListing, Register, SourceAnomaly, SourceError, SourceTimeout
from app.library import job_recommendations
from app.library.job_texts import USER_PROVIDED
from app.library.job_choices import CHECK_CLOSED, CHECK_OPEN, CHECK_UNANSWERED, CHECK_UNKNOWN, is_success, summarize
from app.library.lifecycle import active_job_record, check_record_revision
from app.library.models import (
    JobDecision,
    JobPosting,
    JobPostingCheck,
    JobPostingRecheck,
    JobUpdateRun,
    JobUpdateRunCount,
    TargetType,
)
from app.library.service import new_id, record_event

# Only synthetic data is checked from a record (U3-D4a), whatever a set lists: a real source needs its own approval.
# Synthetic data and (U3-D6) the D6 batch's board: checked only while the set the app runs on keeps it and can call it
# (_read); a source of another set is "source_unavailable". Closed is still said by synthetic sources alone.
TRACKABLE_ACCESS = frozenset({Access.SYNTHETIC, Access.LOCAL_FETCH})
# One check of one posting: the read, the check, and after a 404 the list; each call given its time, none made again.
MAX_CALLS = 3
CALL_SECONDS = 15.0
# How long the posting's text a 申请前核对 read stays to be shown, for the session that asked only.
TEXT_TTL_SECONDS = 30 * 60
# The public facts a check compares with the version before.
COMPARED = ("title", "company", "cities", "places_stated", "job_type", "type_stated", "posted_on", "deadline", "url", "apply_url")

NOT_TRACKABLE = "这个岗位来自的来源不能在这里核对：现在只核对合成来源的岗位。没有发出请求。"
# A posting the user pasted (U3-D4d): reached by no source, so nothing can check it.
PASTED = "这是你提供的原文，系统不能核对它的招聘状态；准备申请时，请到原网站确认。没有发出请求。"
LEAD = "这是待核验线索，不核对招聘状态；请到它指向的官方岗位核对。没有发出请求。"
BUSY = "这条岗位记录正在核对，请等这次核对结束再试。"
# A current recommendation whose time has come (U3-D4b-2): never checked again, whether or not it is removed yet.
LAPSED = "这条临时留存的推荐已经到期，不再保留，也不再核对。没有发出请求。"
# Why a check of a current recommendation that lapsed while it ran wrote nothing.
LAPSED_REASON = "recommendation_lapsed"

DONE, DISCARDED = "done", "discarded"


class Trigger(StrEnum):
    RECHECK = "recheck"
    APPLY = "apply"
    # A batch of the postings tracked (U3-D4b-1): asked for now on the records page, or run when due.
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    # The read of a posting from a link the user gave (U3-D4d), written as its first check only when they added it.
    LINK = "link"


class CheckRefused(Exception):
    """A check that may not start: nothing was sent and nothing written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Reading:
    """What one check of one posting found. text, url and apply_url are the read's, held in memory only."""

    status: str
    reason: str | None = None
    evidence: dict | None = None
    facts: dict | None = None
    content_sha256: str | None = None
    # When its source last answered in the check, or when it was tried for one without an answer.
    answered_at: datetime | None = None
    calls: int = 0
    text: str | None = None
    url: str | None = None
    apply_url: str | None = None


class _OutOfBudget(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Unstarted(Exception):
    """A check of a batch stopped before its first call (U3-D4b-1): nothing was sent, and nothing is written."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Budget:
    """What a batch of checks may still spend (U3-D4b-1), shared by every check of it: calls, seconds, and the app's
    stop signal. Asked before every call: none left, nothing is sent; else the call is given CALL_SECONDS or what is
    left of the batch's seconds, whichever is less."""

    def __init__(self, calls: int, seconds: float, monotonic: Callable[[], float], stop: threading.Event):
        self.calls_left = calls
        self.made = 0
        self.monotonic = monotonic
        self.ends = monotonic() + seconds
        self.stop = stop

    def spent(self) -> str | None:
        """Why no call may be made now, if none may: stopped, calls_used_up or time_used_up."""
        if self.stop.is_set():
            return "stopped"

        if self.calls_left <= 0:
            return "calls_used_up"

        if self.monotonic() >= self.ends:
            return "time_used_up"

        return None

    def take(self) -> float:
        reason = self.spent()

        if reason is not None:
            raise _OutOfBudget(reason)

        self.calls_left -= 1
        self.made += 1

        return min(CALL_SECONDS, self.ends - self.monotonic())


class _Calls:
    """The calls of one check: each given CALL_SECONDS (or less, what its batch has left), at most MAX_CALLS of them,
    none made again."""

    def __init__(self, budget: Budget | None = None):
        self.made = 0
        self.budget = budget

    def __call__(self, method, *args):
        if self.made >= MAX_CALLS:
            raise RuntimeError("a check calls its source at most MAX_CALLS times")

        seconds = CALL_SECONDS if self.budget is None else self.budget.take()
        self.made += 1

        return method(*args, seconds)


def check_posting(
    adapter,
    ref: PostingRef,
    *,
    synthetic: bool,
    delisting: bool,
    today: date,
    now: Callable[[], datetime],
    budget: Budget | None = None,
) -> Reading:
    """Read the posting, then ask whether it is open, and conclude only what the rules allow. A batch's budget spent
    before the first call raises Unstarted; spent half way, the check is one without an answer, saying why."""
    call = _Calls(budget)
    read: dict = {}

    try:
        return _check(adapter, ref, call, read, synthetic=synthetic, delisting=delisting, today=today, now=now)
    except _OutOfBudget as spent:
        if call.made == 0:
            raise Unstarted(spent.reason) from None

        return Reading(CHECK_UNANSWERED, spent.reason, answered_at=now(), calls=call.made, **read)


def _check(adapter, ref: PostingRef, call: _Calls, kept: dict, *, synthetic, delisting, today, now) -> Reading:
    tried = now()

    try:
        detail = call(adapter.read, ref)
    except NotFound:
        answered = now()

        if not delisting:
            return Reading(CHECK_UNKNOWN, "not_found", answered_at=answered, calls=call.made)

        return _delisting(adapter, ref, call, answered, now)
    except SourceError as error:
        return Reading(CHECK_UNANSWERED, failure_reason(error), answered_at=tried, calls=call.made)

    read_at = now()

    if detail.posting.ref != ref:
        return Reading(CHECK_UNKNOWN, "identity_mismatch", answered_at=read_at, calls=call.made)

    read = {
        "facts": read_facts(detail),
        "content_sha256": hashlib.sha256(detail.text.encode("utf-8")).hexdigest(),
        "text": detail.text,
        "url": detail.url,
        "apply_url": detail.apply_url,
    }
    # What was read, should the batch's budget end before the check.
    kept.update(read)

    try:
        answer = call(adapter.check, ref)
    except NotFound:
        return Reading(CHECK_UNKNOWN, "not_found", answered_at=now(), calls=call.made, **read)
    except SourceError as error:
        return Reading(CHECK_UNANSWERED, failure_reason(error), answered_at=now(), calls=call.made, **read)

    answered = now()
    # Each answer's deadline, as it gave it (F1): the read's and the check's, kept with the facts.
    stated, checked = detail.posting.deadline, answer.deadline
    read["facts"]["deadline_answers"] = {"read": _day(stated), "check": _day(checked)}

    if stated is not None and checked is not None and stated != checked:
        # Two answers of one check that disagree settle nothing: neither is the deadline, nor a close.
        read["facts"]["deadline"] = None

        return Reading(CHECK_UNKNOWN, "deadline_conflict", answered_at=answered, calls=call.made, **read)

    deadline = checked if checked is not None else stated
    read["facts"]["deadline"] = _day(deadline)

    if deadline is not None and deadline < today:
        evidence = {
            "kind": "deadline",
            "deadline": deadline.isoformat(),
            # Which answer gave it: both alike, or the one that did.
            "from": "both" if stated is not None and checked is not None else ("check" if checked is not None else "read"),
            "answered_at": answered.isoformat(),
        }

        return Reading(CHECK_CLOSED, evidence=evidence, answered_at=answered, calls=call.made, **read)

    if answer.status is CheckStatus.OPEN:
        return Reading(CHECK_OPEN, answered_at=answered, calls=call.made, **read)

    if answer.status is CheckStatus.CLOSED:
        if synthetic:
            evidence = {"kind": "synthetic", "answered_at": answered.isoformat()}

            return Reading(CHECK_CLOSED, evidence=evidence, answered_at=answered, calls=call.made, **read)

        return Reading(CHECK_UNKNOWN, "closed_without_evidence", answered_at=answered, calls=call.made, **read)

    return Reading(CHECK_UNKNOWN, "no_status", answered_at=answered, calls=call.made, **read)


def _delisting(adapter, ref: PostingRef, call: _Calls, not_found_at: datetime, now: Callable[[], datetime]) -> Reading:
    """After a 404: closed only if the source's complete, unfiltered list, read now, does not hold the posting."""
    listing = getattr(adapter, "listing", None)

    if listing is None:
        return Reading(CHECK_UNKNOWN, "not_found", answered_at=not_found_at, calls=call.made)

    try:
        listed = call(listing)
    except PartialListing:
        return Reading(CHECK_UNKNOWN, "listing_partial", answered_at=now(), calls=call.made)
    except SourceError:
        return Reading(CHECK_UNKNOWN, "listing_failed", answered_at=not_found_at, calls=call.made)

    listed_at = now()
    ids = getattr(listed, "posting_ids", None)

    if not listed.complete or ids is None:
        return Reading(CHECK_UNKNOWN, "listing_partial", answered_at=listed_at, calls=call.made)

    if ref.posting_id in ids():
        return Reading(CHECK_UNKNOWN, "not_found_but_listed", answered_at=listed_at, calls=call.made)

    evidence = {
        "kind": "delisted",
        "not_found_at": not_found_at.isoformat(),
        "status_code": 404,
        "listing_read_at": listed_at.isoformat(),
        "listing_count": listed.read,
        "listing_complete": True,
    }

    return Reading(CHECK_CLOSED, evidence=evidence, answered_at=listed_at, calls=call.made)


def failure_reason(error: SourceError) -> str:
    if isinstance(error, HeldBack):
        return "not_sent"

    if isinstance(error, SourceTimeout):
        return "timeout"

    if isinstance(error, SourceAnomaly):
        return "anomaly"

    return "failed"


def read_facts(detail: PostingDetail) -> dict:
    """The public facts a read gave, as a kept posting's are kept: never its text."""
    posting = detail.posting

    return {
        "title": posting.title,
        "company": posting.company,
        "cities": list(posting.cities),
        "places_stated": list(posting.places_stated),
        "job_type": posting.job_type,
        "type_stated": posting.type_stated,
        "posted_on": _day(posting.posted_on),
        "deadline": _day(posting.deadline),
        "url": detail.url,
        "apply_url": detail.apply_url,
        # What each answer of the check gave as the deadline (F1); the check's is added once it answers.
        "deadline_answers": {"read": _day(posting.deadline), "check": None},
    }


def kept_version(record: JobPosting) -> dict:
    """The facts the record was kept with, as a check compares them: the deadline the posting itself stated then."""
    facts = record.facts
    deadline = next(
        (
            entry.get("deadline")
            for entry in facts.get("deadlines") or []
            if entry.get("source_id") == record.source_id and entry.get("posting_id") == record.posting_id
        ),
        None,
    )

    return {
        "title": facts.get("title"),
        "company": facts.get("company"),
        "cities": list(facts.get("cities") or []),
        "places_stated": list(facts.get("places_stated") or []),
        "job_type": facts.get("job_type"),
        "type_stated": facts.get("type_stated"),
        "posted_on": facts.get("posted_on"),
        "deadline": deadline,
        "url": facts.get("url"),
        "apply_url": facts.get("apply_url"),
    }


def rechecks_of(db: Session, record: JobPosting) -> list[JobPostingRecheck]:
    """Its checks made from the record, the latest first."""
    return list(
        db.scalars(
            select(JobPostingRecheck)
            .where(JobPostingRecheck.posting_record_id == record.id, JobPostingRecheck.owner_id == record.owner_id)
            .order_by(JobPostingRecheck.checked_at.desc(), JobPostingRecheck.recorded_at.desc(), JobPostingRecheck.id)
        )
    )


def triggers_of(db: Session, rechecks) -> dict[str, str]:
    """How each check was asked for, by its run's id."""
    ids = {check.run_id for check in rechecks}

    if not ids:
        return {}

    return {run.id: run.trigger for run in db.scalars(select(JobUpdateRun).where(JobUpdateRun.id.in_(ids)))}


def latest_read(rechecks) -> JobPostingRecheck | None:
    """The latest check that read the posting, if any."""
    return next((check for check in rechecks if check.facts is not None), None)


def deadline_of(facts: dict):
    """A version's deadline as it is compared (F1): the one its check settled on, or both answers when they differed."""
    answers = facts.get("deadline_answers") or {}

    if answers.get("read") and answers.get("check") and answers["read"] != answers["check"]:
        return {"read": answers["read"], "check": answers["check"]}

    return facts.get("deadline")


def changes_between(before: dict, before_sha: str | None, facts: dict, sha: str | None) -> list[dict]:
    """Each compared fact that differs, with both values; of the text only that it changed, never its words."""
    def value(version: dict, name: str):
        return deadline_of(version) if name == "deadline" else version.get(name)

    changes = [
        {"field": name, "before": value(before, name), "after": value(facts, name)}
        for name in COMPARED
        if value(before, name) != value(facts, name)
    ]

    if before_sha is not None and sha is not None and before_sha != sha:
        changes.append({"field": "text", "before": None, "after": None})

    return changes


# --- one check, from its start to what is written -----------------------------------------------------------------------


@dataclass(frozen=True)
class Begun:
    run_id: str
    record_id: str
    # The record's revision as the check began: written only if it still is.
    revision: int
    ref: PostingRef
    synthetic: bool
    before: dict
    before_sha: str | None


@dataclass(frozen=True)
class Finished:
    run_id: str
    written: bool
    # Why nothing was written: record_deleted, record_changed or recommendation_lapsed.
    reason: str | None
    reading: Reading
    changes: tuple[dict, ...] = ()
    # Written: when its record's time comes, as the check was written, if it is a current recommendation (U3-D4b-2 F1);
    # None for 我的岗位. Its text is published and shown only before it (ApplyChecks).
    until: datetime | None = None


class Checking:
    """The records being checked now: one check of a record at a time."""

    def __init__(self):
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def take(self, record_id: str) -> bool:
        with self._lock:
            if record_id in self._ids:
                return False

            self._ids.add(record_id)

            return True

    def release(self, record_id: str) -> None:
        with self._lock:
            self._ids.discard(record_id)

    def holds(self, record_id: str) -> bool:
        with self._lock:
            return record_id in self._ids


class Slots:
    """One batch of a user's at a time, from the moment it is queued until it ends (U3-D4b-1 F1): a slot is claimed
    with a ticket of its own, and given back only with that ticket, so no batch ever gives back another's."""

    def __init__(self):
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}

    def claim(self, key: str) -> str | None:
        with self._lock:
            if key in self._held:
                return None

            ticket = secrets.token_urlsafe(12)
            self._held[key] = ticket

            return ticket

    def release(self, key: str, ticket: str) -> bool:
        with self._lock:
            if self._held.get(key) != ticket:
                return False

            del self._held[key]

            return True

    def holds(self, key: str, ticket: str | None = None) -> bool:
        with self._lock:
            return key in self._held if ticket is None else self._held.get(key) == ticket


def begin(
    db: Session, owner_id: str, record_id: str, revision: int | None, trigger: Trigger, now: datetime, *, run_id: str | None = None
) -> Begun:
    """The check of the owner's record in use: as its page saw it (revision; RecordConflict otherwise) with a run of
    its own, or within a batch's run (run_id) at the revision the record has as its turn comes."""
    row = active_job_record(db, owner_id, JobPosting, record_id)

    if revision is not None:
        check_record_revision(row, revision)

    if job_recommendations.lapsed(db, row, now):
        # A current recommendation whose time has come is checked by nothing (U3-D4b-2).
        raise CheckRefused(LAPSED)

    if row.source_access == USER_PROVIDED:
        raise CheckRefused(PASTED)

    if row.source_access not in TRACKABLE_ACCESS:
        raise CheckRefused(NOT_TRACKABLE)

    if (row.facts or {}).get("evidence") != EvidenceLevel.OFFICIAL.value:
        raise CheckRefused(LEAD)

    if run_id is None:
        run = JobUpdateRun(
            id=new_id(),
            owner_id=owner_id,
            trigger=trigger.value,
            calls=0,
            outcome=None,
            reason=None,
            started_at=now,
            finished_at=None,
        )
        db.add(run)
        run_id = run.id

    latest = latest_read(rechecks_of(db, row))

    if latest is None:
        before, before_sha = kept_version(row), None
    else:
        before, before_sha = dict(latest.facts), latest.content_sha256

    return Begun(
        run_id,
        row.id,
        row.revision,
        PostingRef(row.source_id, row.posting_id),
        row.source_access == Access.SYNTHETIC,
        before,
        before_sha,
    )


def finish(db: Session, owner_id: str, begun: Begun, reading: Reading, now: datetime) -> Finished:
    """One check of one posting (D4a): the check written if its record stands as it began, and its own run closed,
    saying why nothing was written if nothing was."""
    finished = write_check(db, owner_id, begun, reading, now)
    run = db.get(JobUpdateRun, begun.run_id)
    run.calls, run.finished_at = reading.calls, now
    run.outcome, run.reason = (DONE, None) if finished.written else (DISCARDED, finished.reason)

    return finished


def write_check(db: Session, owner_id: str, begun: Begun, reading: Reading, now: datetime) -> Finished:
    """Write the check if its record stands as it began; else nothing, and Finished says why. The run is left as it
    is: a batch closes its own once, at its end (U3-D4b-1)."""
    row = db.scalar(select(JobPosting).where(JobPosting.id == begun.record_id, JobPosting.owner_id == owner_id))

    if row is None or row.deleted_at is not None or row.revision != begun.revision:
        reason = "record_changed" if row is not None and row.deleted_at is None else "record_deleted"

        return Finished(begun.run_id, False, reason, reading)

    if job_recommendations.lapsed(db, row, now):
        # Its time came while it was checked (U3-D4b-2): nothing is written to it.
        return Finished(begun.run_id, False, LAPSED_REASON, reading)

    marker = job_recommendations.marker_of(db, row.id)
    changes =[] if reading.facts is None else changes_between(begun.before, begun.before_sha, reading.facts, reading.content_sha256)
    db.add(
        JobPostingRecheck(
            id=new_id(),
            owner_id=owner_id,
            posting_record_id=row.id,
            run_id=begun.run_id,
            status=reading.status,
            reason=reading.reason,
            evidence=reading.evidence,
            facts=reading.facts,
            content_sha256=reading.content_sha256,
            changes=changes,
            checked_at=reading.answered_at or now,
            recorded_at=now,
        )
    )
    row.revision += 1
    row.updated_at = now
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_rechecked", now)

    return Finished(begun.run_id, True, None, reading, tuple(changes), until=None if marker is None else marker.until)


def run_check(
    database,
    owner_id: str,
    record_id: str,
    revision: int,
    trigger: Trigger,
    *,
    keep: frozenset[str],
    delisting: frozenset[str],
    register: Callable[[], Register],
    now: Callable[[], datetime],
    checking: Checking,
) -> Finished:
    """One check of one kept posting, as the user asked on its page: begun in one transaction, its source called with
    none held, written in another. register gives the sources of the set the app runs on, asked only once begun."""
    if not checking.take(record_id):
        raise CheckRefused(BUSY)

    try:
        with database.transaction() as db:
            begun = begin(db, owner_id, record_id, revision, trigger, now())

        reading = _read(begun, keep=keep, delisting=delisting, register=register, now=now)

        with database.transaction() as db:
            return finish(db, owner_id, begun, reading, now())
    finally:
        checking.release(record_id)


def _read(begun: Begun, *, keep, delisting, register, now, budget: Budget | None = None) -> Reading:
    source_id = begun.ref.source_id
    sources = register()
    entry = sources.entry(source_id)

    # The set the app runs on must keep this source, still reach it as synthetic data, and be able to call it.
    if source_id not in keep or entry is None or entry.access not in TRACKABLE_ACCESS or sources.refusal(source_id) is not None:
        return Reading(CHECK_UNANSWERED, "source_unavailable", answered_at=now(), calls=0)

    return check_posting(
        sources.adapter(source_id),
        begun.ref,
        synthetic=begun.synthetic,
        delisting=source_id in delisting,
        today=now().astimezone().date(),
        now=now,
        budget=budget,
    )


# --- batches of checks of the postings tracked (U3-D4b-1) ---------------------------------------------------------------
#
# 现在核对 (asked for on the records page) and 定时检查 (run when due, while the app runs; off unless the app is started
# for it, and only on synthetic sources): one batch of one user's tracked postings, each checked as one check from its
# record is, under one budget shared by the batch. Tracked: kept, in use, not skipped, not closed with evidence, on a
# synthetic source the app runs on, an official posting. Due for 定时检查: its latest check with a record, of either kind
# and whatever it found, is DUE_SECONDS old or more, or it has none; 现在核对 takes any tracked posting but those with a
# check recorded in the last FRESH_SECONDS. A check that wrote nothing (discarded, skipped, unreached) moves no time.

DUE_SECONDS = 24 * 60 * 60
FRESH_SECONDS = 10 * 60
# How often the automatic check looks, when it is on.
TICK_SECONDS = 15 * 60
COMPLETE, PARTIAL, INTERRUPTED = "complete", "partial", "interrupted"
# What a batch's budget ran out on, as its run says why it stopped.
STOP_REASONS = {"calls_used_up": "calls_limit", "time_used_up": "time_limit", "stopped": "app_closing"}
# The order a batch takes them in: getting ready to apply first, then favourites, then the rest; the longest unchecked
# first within each.
RANK = {JobDecision.APPLY.value: 0, JobDecision.FAVORITE.value: 1}


@dataclass(frozen=True)
class BatchLimits:
    postings: int = 20
    calls: int = 60
    seconds: float = 120.0


@dataclass(frozen=True)
class Tracked:
    record_id: str
    source_id: str
    decision: str | None
    # Its latest check with a record, of either kind, whatever it found; None if it has none.
    attempted_at: datetime | None
    # A current recommendation held for tracking, not 我的岗位 (U3-D4b-2).
    recommended: bool = False


@dataclass(frozen=True)
class BatchResult:
    # None when there was nothing to check: no run is recorded then.
    run_id: str | None
    outcome: str | None = None
    reasons: tuple[str, ...] = ()
    counts: dict | None = None


def tracked(db: Session, owner_id: str, keep: frozenset[str], now: datetime) -> list[Tracked]:
    """The owner's postings a batch checks, as they stand now: 我的岗位 and current recommendations alike (U3-D4b-2)."""
    rows = db.scalars(
        select(JobPosting).where(JobPosting.owner_id == owner_id, JobPosting.deleted_at.is_(None)).order_by(JobPosting.id)
    )

    return [item for item in (_tracked(db, row, keep, now) for row in rows) if item is not None]


def _tracked(db: Session, row: JobPosting, keep: frozenset[str], now: datetime) -> Tracked | None:
    marker = job_recommendations.marker_of(db, row.id)

    if marker is not None and now >= marker.until:
        # A current recommendation whose time has come (U3-D4b-2): taken by no batch, nor when its turn comes.
        return None

    if row.decision == JobDecision.SKIP.value or row.source_access not in TRACKABLE_ACCESS or row.source_id not in keep:
        return None

    if (row.facts or {}).get("evidence") != EvidenceLevel.OFFICIAL.value:
        return None

    checks = [*_search_checks(db, row), *rechecks_of(db, row)]
    summary = summarize(checks)

    # Closed with its evidence kept (a success): no longer checked by a batch; 再核对一次 still may.
    if summary.succeeded is not None and summary.succeeded.status == CHECK_CLOSED:
        return None

    return Tracked(
        row.id, row.source_id, row.decision, None if summary.attempted is None else summary.attempted.checked_at, marker is not None
    )


def _search_checks(db: Session, row: JobPosting) -> list[JobPostingCheck]:
    return list(
        db.scalars(
            select(JobPostingCheck).where(JobPostingCheck.posting_record_id == row.id, JobPostingCheck.owner_id == row.owner_id)
        )
    )


def eligible(item: Tracked, trigger: Trigger, now: datetime) -> bool:
    if item.attempted_at is None:
        return True

    age = (now - item.attempted_at).total_seconds()

    return age >= (DUE_SECONDS if trigger is Trigger.SCHEDULED else FRESH_SECONDS)


def in_order(items) -> list[Tracked]:
    return sorted(
        items,
        key=lambda item: (RANK.get(item.decision, 2), item.attempted_at is not None, item.attempted_at or datetime.min, item.record_id),
    )


def owners_tracking(db: Session) -> list[str]:
    """The users with a posting kept in use and not skipped: those the automatic check looks at."""
    return sorted(
        set(
            db.scalars(
                select(JobPosting.owner_id).where(
                    JobPosting.deleted_at.is_(None), (JobPosting.decision.is_(None)) | (JobPosting.decision != JobDecision.SKIP.value)
                )
            )
        )
    )


def run_batch(
    database,
    owner_id: str,
    trigger: Trigger,
    *,
    keep: frozenset[str],
    delisting: frozenset[str],
    register: Callable[[], Register],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    checking: Checking,
    stop: threading.Event,
    limits: BatchLimits = BatchLimits(),
) -> BatchResult:
    """One batch of the owner's tracked postings (the caller holds the owner's batch lock). Its run is recorded when it
    begins; its calls, outcome, why it stopped, when it ended and its counts only once, when it ends."""
    with database.transaction() as db:
        due = in_order(item for item in tracked(db, owner_id, keep, now()) if eligible(item, trigger, now()))

        if not due:
            return BatchResult(None)

        selected = due[: limits.postings]
        run = JobUpdateRun(
            id=new_id(),
            owner_id=owner_id,
            trigger=trigger.value,
            calls=0,
            outcome=None,
            reason=None,
            started_at=now(),
            finished_at=None,
        )
        db.add(run)
        run_id = run.id

    budget = Budget(limits.calls, limits.seconds, monotonic, stop)
    counts = {"succeeded": 0, "failed": 0, "discarded": 0, "skipped": 0, "unreached": 0}
    reasons = ["postings_limit"] if len(due) > len(selected) else []
    stopped_sources: set[str] = set()
    timeouts: dict[str, int] = {}
    outcome = None

    try:
        for position, item in enumerate(selected):
            spent = budget.spent()

            if spent is not None:
                counts["unreached"] += len(selected) - position
                reasons.append(STOP_REASONS[spent])
                break

            if item.source_id in stopped_sources:
                # Stopped for this batch: asked nothing, left for the next.
                counts["unreached"] += 1

                if "source_stopped" not in reasons:
                    reasons.append("source_stopped")

                continue

            if not checking.take(item.record_id):
                # Being checked already, from its page: nothing sent, nothing written.
                counts["skipped"] += 1
                continue

            try:
                with database.transaction() as db:
                    begun = _begin_in_batch(db, owner_id, item.record_id, run_id, trigger, keep, now())

                if begun is None:
                    counts["skipped"] += 1
                    continue

                try:
                    reading = _read(begun, keep=keep, delisting=delisting, register=register, now=now, budget=budget)
                except Unstarted as unstarted:
                    counts["unreached"] += len(selected) - position
                    reasons.append(STOP_REASONS[unstarted.reason])
                    break

                with database.transaction() as db:
                    finished = write_check(db, owner_id, begun, reading, now())

                if not finished.written:
                    counts["discarded"] += 1
                elif is_success(reading):
                    counts["succeeded"] += 1
                else:
                    counts["failed"] += 1

                _note_source(reading, item.source_id, stopped_sources, timeouts)

                if reading.reason in STOP_REASONS:
                    # Its budget ended half way: the rest is left for the next batch.
                    counts["unreached"] += len(selected) - position - 1
                    reasons.append(STOP_REASONS[reading.reason])
                    break
            finally:
                checking.release(item.record_id)

        outcome = INTERRUPTED if "app_closing" in reasons else (PARTIAL if reasons else COMPLETE)
    except Exception:
        outcome = INTERRUPTED
        reasons.append("error")
        handled = sum(counts.values())
        counts["unreached"] += max(0, len(selected) - handled)
        raise
    finally:
        reason = ",".join(dict.fromkeys(reasons))[:40] or None

        with database.transaction() as db:
            run = db.get(JobUpdateRun, run_id)
            run.calls, run.outcome, run.reason, run.finished_at = budget.made, outcome, reason, now()
            db.add(JobUpdateRunCount(run_id=run_id, owner_id=owner_id, due=len(due), selected=len(selected), **counts))

    return BatchResult(run_id, outcome, tuple(dict.fromkeys(reasons)), dict(counts, due=len(due), selected=len(selected)))


def _begin_in_batch(db: Session, owner_id: str, record_id: str, run_id: str, trigger: Trigger, keep: frozenset[str], now: datetime):
    """The check of one tracked posting as its turn comes, or None: no longer one to check (deleted, skipped, closed,
    a current recommendation whose time has come (U3-D4b-2), no longer synthetic or official, or for 定时检查 no longer
    due), checked by no one and written nowhere."""
    row = db.scalar(select(JobPosting).where(JobPosting.id == record_id, JobPosting.owner_id == owner_id, JobPosting.deleted_at.is_(None)))

    if row is None:
        return None

    item = _tracked(db, row, keep, now)

    if item is None or not eligible(item, trigger, now):
        return None

    return begin(db, owner_id, record_id, None, trigger, now, run_id=run_id)


def _note_source(reading: Reading, source_id: str, stopped: set[str], timeouts: dict[str, int]) -> None:
    """A source that answered wrongly or kept a request back is asked nothing more in this batch; nor one that timed
    out twice in a row."""
    if reading.reason in ("anomaly", "not_sent"):
        stopped.add(source_id)
    elif reading.reason == "timeout":
        timeouts[source_id] = timeouts.get(source_id, 0) + 1

        if timeouts[source_id] >= 2:
            stopped.add(source_id)
    else:
        timeouts[source_id] = 0


def close_unfinished(database) -> int:
    """At start-up: every run left open (the app stopped or failed while it ran) is said to be interrupted, never done,
    and keeps no end time it never had. How many there were."""
    with database.transaction() as db:
        runs = list(db.scalars(select(JobUpdateRun).where(JobUpdateRun.outcome.is_(None))))

        for run in runs:
            run.outcome, run.reason = INTERRUPTED, "not_finished"

        return len(runs)


def last_batch(db: Session, owner_id: str) -> tuple[JobUpdateRun, JobUpdateRunCount | None] | None:
    """The owner's latest batch, and what it came to (None while it runs, or if it never closed)."""
    run = db.scalar(
        select(JobUpdateRun)
        .where(JobUpdateRun.owner_id == owner_id, JobUpdateRun.trigger.in_((Trigger.MANUAL.value, Trigger.SCHEDULED.value)))
        .order_by(JobUpdateRun.started_at.desc(), JobUpdateRun.id.desc())
    )

    return None if run is None else (run, db.get(JobUpdateRunCount, run.id))

# --- the text a 申请前核对 read, in memory only ------------------------------------------------------------------------


@dataclass(frozen=True)
class Shown:
    """One 申请前核对's result as its page shows it: the read's text, held for the session that asked only."""

    token: str
    user_id: str
    session: str
    record_id: str
    run_id: str
    reading: Reading
    changes: tuple[dict, ...]
    made: float
    # When its record's time comes, as the check was written, if it was a current recommendation (U3-D4b-2 F1): the
    # text goes then, if not before. That time only ever moves later (kept again) or away (加入我的岗位), each bumping the
    # record's revision, so a check begun before either writes nothing: this is never later than the record's time now.
    until: datetime | None = None


class TextLapsed(Exception):
    """A 申请前核对's text whose record's time had come as it was to be published or read (U3-D4b-2 F1): nothing was
    published, or it was dropped there and then; never shown."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Reserved:
    """A 申请前核对 of a record that may still show its text, once it is done (F2)."""

    ticket: str
    user_id: str
    session: str
    made: float
    # False once its record was deleted since it was reserved: then its text is never shown. (A session ended since is
    # told by ApplyChecks._ended, which put reads under the same lock.)
    valid: bool = True


class ApplyChecks:
    """The texts 申请前核对 read, in memory only: each for the session that asked, gone after TEXT_TTL_SECONDS (a look
    never keeps one longer), when that session ends, and as soon as its record is deleted.

    A text is shown only for a check reserved before it began (reserve) and published (put) with nothing ended since
    (F2): deleting the record (forget_record) drops what is shown of it and spoils its reservation; ending the session
    (end_session) drops what it was shown and marks it ended. Both happen under the one lock put checks and writes
    under, so a result that comes late is never shown, and a record restored never shows a text read before its
    deletion. A session ended stays known for TEXT_TTL_SECONDS, far
    longer than a check can run (MAX_CALLS of CALL_SECONDS): nothing is reserved, published or shown for it meanwhile.

    A current recommendation's text goes at its record's time too (U3-D4b-2 F1). That time comes with the check as it
    was written (Finished.until) and stays with the text (Shown.until); put, get and every sweep read the app's clock
    (now) under the one lock they publish, read and drop under, so the moment that decides is the moment of use, with
    no gap after a check made elsewhere: nothing is published once the time has come (TextLapsed), and from then on a
    text is dropped as soon as anything here looks, the get asking for it saying so (TextLapsed).
    """

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl: float = TEXT_TTL_SECONDS, now: Callable[[], datetime] | None = None
    ):
        self.clock = clock
        self.ttl = ttl
        # The app's clock, which a recommendation's time is on (DiscoveryRuntime gives it); the system's otherwise.
        self.now = now or _utc_now
        self._lock = threading.Lock()
        self._shown: dict[str, Shown] = {}
        # By record id: the one check of a record that may publish (one check of a record at a time, as Checking).
        self._reserved: dict[str, _Reserved] = {}
        # Sessions ended, and when.
        self._ended: dict[str, float] = {}

    def reserve(self, user_id: str, session: str, record_id: str) -> str | None:
        """A ticket for one 申请前核对 of the record about to begin, or None: a session ended, or one already running."""
        with self._lock:
            self._sweep(self.now())

            if session in self._ended or record_id in self._reserved:
                return None

            ticket = secrets.token_urlsafe(12)
            self._reserved[record_id] = _Reserved(ticket, user_id, session, self.clock())

            return ticket

    def put(self, user_id: str, session: str, record_id: str, finished: Finished) -> str | None:
        """Show a finished check's text, and say where; None if its reservation is gone or spoilt since: never shown.
        TextLapsed if its record's time has come by now, read here under the lock (U3-D4b-2 F1): nothing published."""
        with self._lock:
            at = self.now()
            self._sweep(at)
            reserved = self._reserved.pop(record_id, None)

            if (
                reserved is None
                or not reserved.valid
                or reserved.user_id != user_id
                or reserved.session != session
                or session in self._ended
            ):
                return None

            if finished.until is not None and at >= finished.until:
                raise TextLapsed()

            token = secrets.token_urlsafe(18)
            self._shown[token] = Shown(
                token, user_id, session, record_id, finished.run_id, finished.reading, finished.changes, self.clock(), finished.until
            )

            return token

    def ended(self, session: str) -> bool:
        with self._lock:
            self._sweep(self.now())

            return session in self._ended

    def cancel(self, ticket: str) -> None:
        """Give back a reservation not published (the check refused, discarded or failed); a no-op once published."""
        with self._lock:
            for record_id in [record_id for record_id, reserved in self._reserved.items() if reserved.ticket == ticket]:
                del self._reserved[record_id]

    def get(self, token: str, user_id: str, session: str, record_id: str) -> Shown | None:
        """The text, for the session that asked it, while it may be shown; None if not. TextLapsed if its record's time
        has come by now, read here under the lock (U3-D4b-2 F1): it was dropped as it was asked for, never shown."""
        with self._lock:
            lapsed = self._sweep(self.now())
            shown = self._shown.get(token, lapsed.get(token))

            if (
                shown is None
                or session in self._ended
                or shown.user_id != user_id
                or shown.session != session
                or shown.record_id != record_id
            ):
                return None

            if token in lapsed:
                raise TextLapsed()

            return shown

    def minutes_left(self, shown: Shown) -> int:
        """About how many minutes its page may still show the text: TEXT_TTL_SECONDS from when it was published, or its
        record's time if that comes first (U3-D4b-2 F1)."""
        with self._lock:
            seconds = self.ttl - (self.clock() - shown.made)

            if shown.until is not None:
                seconds = min(seconds, (shown.until - self.now()).total_seconds())

        return max(1, int(seconds // 60))

    def forget_record(self, record_id: str) -> None:
        with self._lock:
            for token in [token for token, shown in self._shown.items() if shown.record_id == record_id]:
                del self._shown[token]

            if record_id in self._reserved:
                self._reserved[record_id].valid = False

    def end_session(self, session: str) -> None:
        """Drop what the session was shown; from now on nothing is reserved, published or shown for it (_ended)."""
        with self._lock:
            self._ended[session] = self.clock()

            for token in [token for token, shown in self._shown.items() if shown.session == session]:
                del self._shown[token]

    def sweep(self) -> None:
        with self._lock:
            self._sweep(self.now())

    def __len__(self) -> int:
        with self._lock:
            return len(self._shown)

    def _sweep(self, at: datetime) -> dict[str, Shown]:
        """Drop what has had its time: texts and reservations TEXT_TTL_SECONDS old, sessions ended as long ago, and the
        texts whose record's time has come by at (U3-D4b-2 F1), these last given back by token."""
        cutoff = self.clock() - self.ttl
        lapsed = {token: shown for token, shown in self._shown.items() if shown.until is not None and at >= shown.until}

        for token in [token for token, shown in self._shown.items() if token in lapsed or shown.made <= cutoff]:
            del self._shown[token]

        for record_id in [record_id for record_id, reserved in self._reserved.items() if reserved.made <= cutoff]:
            del self._reserved[record_id]

        for session in [session for session, ended in self._ended.items() if ended <= cutoff]:
            del self._ended[session]

        return lapsed


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
