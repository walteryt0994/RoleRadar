"""每日查新 (U3-D4c): a fixed flow over a kept search summary's own searches, never an agent and never a model.

A watch is the user's, started on a kept summary of theirs whose sources the app runs on and keeps, all reached as
synthetic data. A run makes the summary's searches again, each to the source it was made of with its words and
filters, held first to the very checks a search made them under (app.discovery.conditions); a source answering from
its whole list (Lever's shape) is read once a run and gets no words at all. Nothing else leaves: no experience,
preference, posting text, decision or note is read, and nothing calls a model.

What a run keeps (app.library.job_watches), in one write at its end:
- every posting its answered searches gave, by source and id, whatever came of it, and a lead's official posting
  too: a posting seen once is never new again, however the scope, a skip or a kept record changes later;
- each search's first answer is its baseline, never new: only searches answered before find new postings;
- the new postings, every one, by the kept postings' whitelist, for KEEP_NEW_FOR from when each was read. Not new: a
  posting the user skipped or one of its leads (U3-D3c), one kept (我的岗位, a current recommendation, the recycle bin),
  one out of the scope, the same posting twice, or the same numbered job again (rules.same_posting): each only
  counted. A list read in part says so on every posting first read from it;
- the run's own times, outcome and counts.

When a run begins (begin): its watch stands, on a summary in use, its sources still the app's, fewer than
SEEN_THRESHOLD postings seen. 定时检查 takes a watch 24 hours after its last run that was not interrupted began
(attempted_at), whatever came of that run and whether any baseline was built; one never run at once. 现在查新 waits
FRESH_SECONDS after any run began. A run interrupted writes nothing and counts for neither, so the app catches up
once when it starts again: an error, or the app closing at any point before the run's one write, during its last call
too (asked again in the write's own transaction, F1).

Stopping (F2): a watch is stopped by removing it, 停止并清除 or its summary deleted; nothing in memory says so. Before
each call to a source (admit), one transaction that takes the write lock asks that the watch still stands with this
run's token, and only then lets that one call go and pays its budget for it: the commit of a removal is the point where
a watch stops. A removal committed first refuses that call and every later one; a call let go first is not called
back (a list read in pages is read to its end, or to its time), and nothing that comes of it is written, since the one
write asks the same again. Letting a call go is not sending it.
"""
import hashlib
import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.discovery import conditions, rules
from app.discovery.contracts import (
    Access,
    Dimension,
    DimensionScope,
    EvidenceLevel,
    Limits,
    Others,
    Posting,
    PostingRef,
    Screen,
    SearchScope,
)
from app.discovery.sources import HeldBack, ListingSource, Query, Register, SourceAnomaly, SourceError, SourceTimeout, keyword_scope
from app.discovery.updates import DUE_SECONDS, FRESH_SECONDS, Budget, _OutOfBudget, failure_reason
from app.library import job_watches
from app.library.errors import NotAllowed, NotFound
from app.library.job_watches import KEEP_NEW_FOR, SEEN_THRESHOLD
from app.library.lifecycle import active_job_record, check_record_revision, posting_in_bin, posting_in_use, skipped_postings
from app.library.models import JobSearch, JobWatch, JobWatchPosting, JobWatchSeen
from app.library.service import new_id

# The accesses a watch may use (U3-D6): synthetic data, and the D6 batch's board; either only while the set the app runs
# on keeps it (keep) and can call it.
WATCHABLE_ACCESS = frozenset({Access.SYNTHETIC.value, Access.LOCAL_FETCH.value})

# The words of the page that starts a watch (records_web): changing one changes the version, and a start posted under
# another version is not taken. The page says the source as the running set reads it (U3-D6 close-out, F1): the
# synthetic and replay sets go nowhere ("watch-1", its words as before); a real source, the D6 batch's board, is read
# at once under the batch's read approval ("watch-batch-1"). A start posted under the other's words is not taken
# either, and a page shown before these words were written ("watch-1" on a real source) starts nothing.
NOTICE_VERSION = "watch-1"
LIVE_NOTICE_VERSION = "watch-batch-1"


def notice_version_for(sources) -> str:
    """The version of the words the page shows for this set (app.discovery.web.SourceSet): its mode alone decides."""
    return LIVE_NOTICE_VERSION if sources.mode == "real" else NOTICE_VERSION
# Watches a user may have at once.
MAX_WATCHES = 3
# A search's limits on its words and filters, as the summary's searches were made under them.
LIMITS = Limits()
COMPLETE, PARTIAL, FAILED, INTERRUPTED = "complete", "partial", "failed", "interrupted"
# How each search of a run ended.
ANSWERED, NOT_ANSWERED, REFUSED, UNREACHED = "answered", "failed", "refused", "unreached"
# What a run's budget ran out on, as its outcome says why it stopped.
STOP_REASONS = {"calls_used_up": "calls_limit", "time_used_up": "time_limit", "stopped": "app_closing"}

NOT_READ = "这份摘要读不出检索的来源、检索词和筛选，不能开启每日查新。"
NO_SEARCH = "这份摘要里没有可以重查的检索，不能开启每日查新。"
ON_ALREADY = "这份摘要已经开启了每日查新。"
TOO_MANY = f"每个人最多同时开启 {MAX_WATCHES} 个每日查新；要开启这一个，请先停止并清除另一个。"
FULL = f"这个每日查新见过的岗位已经达到 {SEEN_THRESHOLD} 个，不再开始新的一轮；要继续，请停止并清除以后重新开启（会重新建立基准）。"


class Trigger(StrEnum):
    # The first run, as the user starts the watch: its baseline.
    ENABLE = "enable"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class Stopped(Exception):
    """The watch was removed (停止并清除, or its summary deleted), or another run holds it: nothing more is let go."""


class Unavailable(Exception):
    """A watch that may not start, in words for its page."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NoticeChanged(Exception):
    """A start posted under other words than the page shows now: nothing started."""


@dataclass(frozen=True)
class RunLimits:
    calls: int = LIMITS.searches
    seconds: float = 60.0


@dataclass(frozen=True)
class Item:
    """One search of the summary, as a run makes it again."""

    key: str
    source_id: str
    words: str
    filters: tuple[tuple[Dimension, str | None], ...]

    @property
    def query(self) -> Query:
        return Query(self.words, *(value for _, value in self.filters))


@dataclass(frozen=True)
class Conditions:
    """What a watch runs on, read from its summary: the sources as confirmed, the scope and the searches."""

    sources: tuple[dict, ...]
    scope: SearchScope
    items: tuple[Item, ...]

    def source(self, source_id: str) -> dict:
        return next(source for source in self.sources if source.get("source_id") == source_id)


@dataclass(frozen=True)
class Begun:
    watch_id: str
    owner_id: str
    token: str
    trigger: Trigger
    started_at: datetime
    conditions: Conditions
    baselined: frozenset[str]


@dataclass
class Answer:
    item: Item
    status: str
    postings: tuple[Posting, ...] = ()
    # How many postings its list held as read, when the list was read only in part; None when read to its end.
    partial_read: int | None = None
    reason: str | None = None
    answered_at: datetime | None = None


@dataclass(frozen=True)
class RunResult:
    # None when nothing ran: no watch to run, or not one to run now. Then nothing was sent or written.
    watch_id: str | None
    outcome: str | None = None
    written: bool = False
    counts: dict = field(default_factory=dict)


# --- what a watch runs on ------------------------------------------------------------------------------------------------


def conditions_of(summary: dict) -> Conditions | None:
    """The sources, scope and searches of a kept summary (records.search_summary), each search once: the same source,
    words (spaces and case aside) and filters are one search. None when the summary does not hold them."""
    try:
        scope = _scope(summary["scope"])
        sources = tuple(dict(source) for source in summary["sources"])
        items: list[Item] = []
        keys: set[str] = set()

        for entry in summary["searches"]:
            words = " ".join(str(entry["keywords"]).split())
            given = entry["filters"]
            filters = tuple((dimension, _value(given.get(dimension.value))) for dimension in Dimension)
            key = item_key(entry["source_id"], words, filters)

            if key not in keys:
                keys.add(key)
                items.append(Item(key, str(entry["source_id"]), words, filters))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

    return Conditions(sources, scope, tuple(items[: LIMITS.searches]))


def item_key(source_id: str, words: str, filters) -> str:
    covered = [None if value is None else rules.normalized(dimension, value) for dimension, value in filters]

    return hashlib.sha256(json.dumps([source_id, words.casefold(), covered], ensure_ascii=False).encode("utf-8")).hexdigest()


def unavailable(found: Conditions | None, keep: frozenset[str], register: Register) -> str | None:
    """Why a watch on these conditions may not start or run now, in words; None when it may. Every source of the
    summary must be one the set the app runs on keeps, reached as synthetic data then and now, still registered as the
    summary names it and callable."""
    if found is None:
        return NOT_READ

    if not found.items:
        return NO_SEARCH

    named = {source.get("source_id") for source in found.sources}

    if any(item.source_id not in named for item in found.items):
        return NOT_READ

    for source in found.sources:
        source_id = source.get("source_id")
        entry = register.entry(source_id)
        name = source.get("name") or source_id

        if (
            source_id not in keep
            or source.get("access") not in WATCHABLE_ACCESS
            or entry is None
            or entry.access.value not in WATCHABLE_ACCESS
            or (entry.name, entry.evidence.value) != (source.get("name"), source.get("evidence"))
            or register.refusal(source_id) is not None
        ):
            return f"来源“{name}”现在不能用于每日查新：这次运行的来源里没有它，或者这次运行不能保存它的结果。没有发出请求。"

    return None


def due(watch: JobWatch, now: datetime) -> bool:
    """定时检查: 24 hours after the last run that was not interrupted began, whatever came of it; one never run, now."""
    return watch.attempted_at is None or (now - watch.attempted_at).total_seconds() >= DUE_SECONDS


def fresh(watch: JobWatch, now: datetime) -> bool:
    """A run of it began less than FRESH_SECONDS ago, whatever came of it: 现在查新 waits."""
    return watch.last_started_at is not None and (now - watch.last_started_at).total_seconds() < FRESH_SECONDS


# --- starting and stopping, as the user asks ----------------------------------------------------------------------------


def enable(
    db: Session,
    owner_id: str,
    search_record_id: str,
    revision: int,
    notice_version: str,
    now: datetime,
    *,
    keep: frozenset[str],
    register: Register,
    expected: str = NOTICE_VERSION,
) -> JobWatch:
    """A watch on the user's own kept summary in use, as its page showed it (its revision and the notice's words):
    expected is the version of the words the running set's page shows (notice_version_for), and the watch keeps it."""
    summary = active_job_record(db, owner_id, JobSearch, search_record_id)
    check_record_revision(summary, revision)

    if notice_version != expected:
        raise NoticeChanged()

    if job_watches.watch_of(db, owner_id, summary.id) is not None:
        raise NotAllowed(ON_ALREADY)

    if len(job_watches.watches_of(db, owner_id)) >= MAX_WATCHES:
        raise NotAllowed(TOO_MANY)

    problem = unavailable(conditions_of(summary.summary), keep, register)

    if problem is not None:
        raise Unavailable(problem)

    watch = JobWatch(
        id=new_id(),
        owner_id=owner_id,
        search_record_id=summary.id,
        notice_version=expected,
        baselined=[],
        baseline_at=None,
        run_token=None,
        last_trigger=None,
        last_started_at=None,
        last_finished_at=None,
        last_outcome=None,
        last_reason=None,
        last_calls=0,
        last_counts=None,
        attempted_at=None,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(watch)
    # What the summary carries changed: a page shown before (its delete page) is stale now.
    _touch(summary, now)
    db.flush()

    return watch


def stop(db: Session, owner_id: str, search_record_id: str, watch_id: str, now: datetime) -> None:
    """停止并清除: the watch, what it has seen and its new postings, removed at once and never through the bin. Its
    commit is where the watch stops: a run of it lets no call go after it, and writes nothing."""
    summary = active_job_record(db, owner_id, JobSearch, search_record_id)
    watch = db.get(JobWatch, watch_id)

    if watch is None or watch.owner_id != owner_id or watch.search_record_id != summary.id:
        raise NotFound()

    job_watches.remove(db, watch.id)
    _touch(summary, now)


def startable(db: Session, watch: JobWatch, trigger: Trigger, now: datetime, *, keep: frozenset[str], register: Register) -> str | None:
    """Why a run of this watch may not begin now, as a code (running, full, unavailable, fresh, not_due); None when it
    may. Asked again by begin, under the write lock."""
    if watch.run_token is not None:
        return "running"

    summary = db.scalar(select(JobSearch).where(JobSearch.id == watch.search_record_id, JobSearch.deleted_at.is_(None)))

    if summary is None:
        return "unavailable"

    if job_watches.full(db, watch.id):
        return "full"

    if unavailable(conditions_of(summary.summary), keep, register) is not None:
        return "unavailable"

    if trigger is Trigger.SCHEDULED and not due(watch, now):
        return "not_due"

    if trigger is Trigger.MANUAL and fresh(watch, now):
        return "fresh"

    return None


def due_watches(db: Session, now: datetime, *, keep: frozenset[str], register: Register) -> list[tuple[str, str]]:
    """定时检查's watches: (owner, watch) of every watch a scheduled run may begin on now."""
    watches = db.scalars(select(JobWatch).order_by(JobWatch.owner_id, JobWatch.created_at, JobWatch.id))

    return [
        (watch.owner_id, watch.id)
        for watch in watches
        if startable(db, watch, Trigger.SCHEDULED, now, keep=keep, register=register) is None
    ]


def close_unfinished(database) -> int:
    """At start-up: every run left open (the app stopped or failed while it ran) is said to be interrupted, never done;
    it counts for neither 定时检查 nor the last success, and wrote nothing. How many there were."""
    with database.transaction() as db:
        watches = list(db.scalars(select(JobWatch).where(JobWatch.run_token.is_not(None))))

        for watch in watches:
            watch.run_token, watch.last_outcome, watch.last_reason = None, INTERRUPTED, "not_finished"

        return len(watches)


# --- a run --------------------------------------------------------------------------------------------------------------


def begin(db: Session, owner_id: str, watch_id: str, trigger: Trigger, now: datetime, *, keep: frozenset[str], register: Register) -> Begun | None:
    """The run of a watch that may run now, its token set; None when there is none to run (removed while it was queued,
    running already, full, unavailable, not due, or run just now): nothing sent, nothing written."""
    watch = db.get(JobWatch, watch_id)

    if watch is None or watch.owner_id != owner_id:
        return None

    if startable(db, watch, trigger, now, keep=keep, register=register) is not None:
        return None

    summary = db.get(JobSearch, watch.search_record_id)
    found = conditions_of(summary.summary)
    token = secrets.token_urlsafe(18)
    watch.run_token, watch.last_trigger, watch.last_started_at = token, trigger.value, now
    watch.last_finished_at, watch.last_outcome, watch.last_reason, watch.last_calls, watch.last_counts = None, None, None, 0, None
    watch.updated_at = now

    return Begun(watch.id, owner_id, token, trigger, now, found, frozenset(watch.baselined))


def admit(database, begun: Begun, budget: Budget) -> float:
    """Let one call of the run go, or say why not: the budget spent (_OutOfBudget), or the watch removed or held by
    another run (Stopped). One transaction that takes the write lock asks that the watch stands with this run's token:
    a removal that committed first refuses it. Only then is the call paid for, once: the time it is given is for that
    one call. Letting it go is not sending it."""
    reason = budget.spent()

    if reason is not None:
        raise _OutOfBudget(reason)

    with database.transaction() as db:
        if not _holds(db, begun):
            raise Stopped()

    return budget.take()


def run(
    database,
    owner_id: str,
    watch_id: str,
    trigger: Trigger,
    *,
    keep: frozenset[str],
    register: Callable[[], Register],
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    stop: threading.Event,
    limits: RunLimits = RunLimits(),
) -> RunResult:
    """One run of one watch (the caller holds the owner's slot): begun, its searches made again with a call let go
    one at a time, and what came of it written once, at its end; or nothing written, when the watch was removed."""
    sources = register()

    with database.transaction() as db:
        begun = begin(db, owner_id, watch_id, trigger, now(), keep=keep, register=sources)

    if begun is None:
        return RunResult(None)

    budget = Budget(limits.calls, limits.seconds, monotonic, stop)
    answers: list[Answer] = []
    reasons: list[str] = []
    outcome = INTERRUPTED
    closed = False

    try:
        try:
            _ask(database, begun, sources, budget, answers, reasons, now)
        except Stopped:
            # Removed, or held by another run: nothing of this run is written, and there is nothing of it to close.
            return RunResult(begun.watch_id, None)

        outcome = INTERRUPTED if "app_closing" in reasons else _outcome(answers)

        with database.transaction() as db:
            # The app may have begun closing while the last call was out, or since it came back, with no call after it
            # to find out (U3-D4c F1): asked again here, in the write's own transaction, before anything is written. A
            # run it reaches is interrupted and writes nothing of what it found; closing after this point finds the
            # write made, and what was committed stays as it was.
            if outcome != INTERRUPTED and stop.is_set():
                outcome = INTERRUPTED
                reasons.append("app_closing")

            if outcome == INTERRUPTED:
                # The app is closing: nothing it found is written, and the run counts for neither.
                closed = _close(db, begun, INTERRUPTED, reasons, budget.made, _counts(answers), now())

                return RunResult(begun.watch_id, INTERRUPTED, False, _counts(answers))

            counts = _write(db, begun, answers, outcome, reasons, budget.made, now())
            closed = counts is not None

        return RunResult(begun.watch_id, outcome if closed else None, closed, counts or {})
    except Exception:
        outcome = INTERRUPTED
        reasons.append("error")
        raise
    finally:
        if not closed and outcome == INTERRUPTED:
            # An error half way: said to be interrupted (when the watch still stands with this run), nothing written.
            with database.transaction() as db:
                _close(db, begun, INTERRUPTED, reasons, budget.made, _counts(answers), now())


def _ask(database, begun: Begun, sources: Register, budget: Budget, answers: list[Answer], reasons: list[str], now) -> None:
    """Make each search again, one call let go at a time; a source's list read once."""
    items = begun.conditions.items
    lists: dict[str, object] = {}
    failed_lists: dict[str, str] = {}
    stopped_sources: set[str] = set()
    timeouts: dict[str, int] = {}

    for position, item in enumerate(items):
        if item.source_id in stopped_sources:
            answers.append(Answer(item, UNREACHED, reason="source_stopped"))

            if "source_stopped" not in reasons:
                reasons.append("source_stopped")

            continue

        adapter = sources.adapter(item.source_id)
        problem = refused_because(item, begun.conditions.scope, adapter)

        if problem is not None:
            # Held to the checks a search made it under: not sent.
            answers.append(Answer(item, REFUSED, reason=problem))
            continue

        try:
            if isinstance(adapter, ListingSource):
                if item.source_id in failed_lists:
                    # Its list was not read in this run: not asked again.
                    answers.append(Answer(item, NOT_ANSWERED, reason=failed_lists[item.source_id]))
                    continue

                listing = lists.get(item.source_id)

                if listing is None:
                    seconds = admit(database, begun, budget)

                    try:
                        listing = conditions._whole_list(adapter, seconds)
                    except SourceError as error:
                        failed_lists[item.source_id] = failure_reason(error)
                        raise

                    lists[item.source_id] = listing

                postings = listing.select(item.query)
                partial = None if listing.complete else listing.read
            else:
                seconds = admit(database, begun, budget)
                postings = adapter.search(item.query, seconds)
                partial = None
        except _OutOfBudget as out:
            reason = STOP_REASONS[out.reason]
            answers.extend(Answer(rest, UNREACHED, reason=reason) for rest in items[position:])

            if reason not in reasons:
                reasons.append(reason)

            return
        except SourceError as error:
            code = failure_reason(error)
            answers.append(Answer(item, NOT_ANSWERED, reason=code))

            # A source that answered wrongly or kept a request back is asked nothing more in this run; nor one that
            # timed out twice in a row.
            if isinstance(error, (SourceAnomaly, HeldBack)):
                stopped_sources.add(item.source_id)
            elif isinstance(error, SourceTimeout):
                timeouts[item.source_id] = timeouts.get(item.source_id, 0) + 1

                if timeouts[item.source_id] >= 2:
                    stopped_sources.add(item.source_id)

            continue

        timeouts[item.source_id] = 0
        answers.append(Answer(item, ANSWERED, tuple(postings), partial, answered_at=now()))


def refused_because(item: Item, scope: SearchScope, adapter) -> str | None:
    """Why a search of the summary may not be sent again, by the very checks a search made it under; None when it may."""
    problem = (
        conditions._words_problem("检索词", item.words, LIMITS.keyword_chars)
        or conditions._filters_problem(scope, item.filters, LIMITS.keyword_chars)
        or conditions._barred_problem(scope, item.words)
    )

    if problem is not None:
        return problem

    declared = keyword_scope(adapter)

    if declared is not None and conditions.named_words(declared, scope, item.words)[0]:
        return "检索词里写了这个来源只按筛选匹配的城市或类型"

    return None


def _outcome(answers: list[Answer]) -> str:
    answered = sum(answer.status == ANSWERED for answer in answers)

    if answered == len(answers) and answers:
        return COMPLETE

    return PARTIAL if answered else FAILED


def _holds(db: Session, begun: Begun) -> bool:
    """The watch stands with this run's token, on a summary in use."""
    watch = db.get(JobWatch, begun.watch_id)

    if watch is None or watch.owner_id != begun.owner_id or watch.run_token != begun.token:
        return False

    return db.scalar(select(JobSearch.id).where(JobSearch.id == watch.search_record_id, JobSearch.deleted_at.is_(None))) is not None


def _close(db: Session, begun: Begun, outcome: str, reasons: list[str], calls: int, counts: dict, now: datetime | None = None) -> bool:
    """The run's own record on its watch, when the watch still stands with this run: its outcome, why, its calls and
    counts. An interrupted run counts for neither 定时检查 nor the last success."""
    if not _holds(db, begun):
        return False

    watch = db.get(JobWatch, begun.watch_id)
    watch.run_token = None
    watch.last_outcome = outcome
    watch.last_reason = ",".join(dict.fromkeys(reasons))[:60] or None
    watch.last_calls = calls
    watch.last_counts = counts
    watch.last_finished_at = now
    watch.updated_at = now or watch.updated_at

    if outcome != INTERRUPTED:
        watch.attempted_at = begun.started_at

    if outcome == COMPLETE:
        watch.completed_at = now

    return True


def _counts(answers: list[Answer]) -> dict:
    return {
        "searches": len(answers),
        "answered": sum(answer.status == ANSWERED for answer in answers),
        "failed": sum(answer.status == NOT_ANSWERED for answer in answers),
        "refused": sum(answer.status == REFUSED for answer in answers),
        "unreached": sum(answer.status == UNREACHED for answer in answers),
    }


@dataclass
class _Returned:
    posting: Posting
    order: int
    # Whether a search answered before this run gave it: only those may be new.
    from_baselined: bool
    # How many postings its list held as read, while every list that gave it was read only in part.
    partial_read: int | None
    answered_at: datetime


def _write(db: Session, begun: Begun, answers: list[Answer], outcome: str, reasons: list[str], calls: int, now: datetime) -> dict | None:
    """The one write of a run: judged against what the watch had seen before it, then made one within the run, then
    written whole. None, writing nothing, when the watch was removed or another run holds it."""
    if not _holds(db, begun):
        return None

    owner_id = begun.owner_id
    found = begun.conditions
    evidence = {source.get("source_id"): source.get("evidence") for source in found.sources}
    names = {source.get("source_id"): source.get("name") for source in found.sources}
    counts = _counts(answers)
    seen_rows = {(row.source_id, row.posting_id): row for row in db.scalars(select(JobWatchSeen).where(JobWatchSeen.watch_id == begun.watch_id))}
    seen_keys = {row.job_key for row in seen_rows.values() if row.job_key}
    returned: dict[PostingRef, _Returned] = {}
    foreign = unplain = 0

    for answer in answers:
        if answer.status != ANSWERED:
            continue

        baselined = answer.item.key in begun.baselined

        for posting in answer.postings:
            # Another source's posting is not this source's to give; an id that is not plain is never kept or named.
            if posting.ref.source_id != answer.item.source_id:
                foreign += 1
                continue

            if not conditions._plain_posting(posting):
                unplain += 1
                continue

            known = returned.get(posting.ref)

            if known is None:
                returned[posting.ref] = _Returned(posting, len(returned), baselined, answer.partial_read, answer.answered_at)
            else:
                known.from_baselined = known.from_baselined or baselined
                known.partial_read = None if answer.partial_read is None or known.partial_read is None else known.partial_read

    # The user's skips on every source of the summary, as they stand at this write (U3-D3c), a lead's official posting's
    # included.
    skipped = {
        PostingRef(source_id, posting_id)
        for source_id, posting_id in skipped_postings(db, owner_id, set(evidence), tuple(WATCHABLE_ACCESS))
    }
    tally = {"baseline": 0, "seen_before": 0, "skipped": 0, "kept": 0, "out_of_scope": 0, "duplicates": 0}
    candidates: list[tuple[_Returned, object]] = []

    for ref, entry in returned.items():
        posting = entry.posting

        if not entry.from_baselined:
            tally["baseline"] += 1
            continue

        if (ref.source_id, ref.posting_id) in seen_rows or (
            posting.official is not None and (posting.official.source_id, posting.official.posting_id) in seen_rows
        ):
            tally["seen_before"] += 1
            continue

        key = job_key(posting, evidence.get(ref.source_id))

        if key is not None and key in seen_keys:
            tally["seen_before"] += 1
            continue

        if ref in skipped or (posting.official is not None and posting.official in skipped):
            # Only counted: never which, nor why the user skipped it.
            tally["skipped"] += 1
            continue

        if _kept(db, owner_id, posting):
            tally["kept"] += 1
            continue

        screening = rules.screen(found.scope, posting)

        if screening.status is Screen.OUT:
            tally["out_of_scope"] += 1
            continue

        candidates.append((entry, screening))

    new: list[tuple[_Returned, object]] = []
    official = EvidenceLevel.OFFICIAL.value

    # Made one within the run: of a lead and the official posting it points to, the official one is listed.
    for entry, screening in sorted(candidates, key=lambda pair: (evidence.get(pair[0].posting.ref.source_id) != official, pair[0].order)):
        if any(_same(entry.posting, kept.posting, evidence) for kept, _ in new):
            tally["duplicates"] += 1
            continue

        new.append((entry, screening))

    ranked = sorted(new, key=lambda pair: (pair[0].partial_read is not None, -rules.ideal_hits(found.scope, pair[0].posting), pair[0].order))

    for position, (entry, screening) in enumerate(ranked):
        posting = entry.posting
        db.add(
            JobWatchPosting(
                id=new_id(),
                owner_id=owner_id,
                watch_id=begun.watch_id,
                source_id=posting.ref.source_id,
                posting_id=posting.ref.posting_id,
                facts=watched_facts(posting, names.get(posting.ref.source_id), evidence.get(posting.ref.source_id)),
                screening={"status": screening.status.value, "reasons": list(screening.reasons), "notes": list(screening.notes)},
                partial_read=entry.partial_read,
                position=position,
                found_at=entry.answered_at,
                until=entry.answered_at + KEEP_NEW_FOR,
            )
        )

    # Everything given is seen now, whatever came of it, and a lead's official posting too.
    for ref, entry in returned.items():
        _see(db, seen_rows, begun, ref, job_key(entry.posting, evidence.get(ref.source_id)), entry.answered_at)

        official = entry.posting.official

        if official is not None and official.source_id in evidence and conditions._plain(official):
            _see(db, seen_rows, begun, official, None, entry.answered_at)

    watch = db.get(JobWatch, begun.watch_id)
    answered = {answer.item.key for answer in answers if answer.status == ANSWERED}

    if answered - begun.baselined:
        watch.baselined = sorted(set(watch.baselined) | answered)

        if watch.baseline_at is None:
            watch.baseline_at = now

    counts.update(
        tally,
        returned=len(returned),
        foreign=foreign,
        unplain=unplain,
        new=len(ranked),
        new_partial=sum(entry.partial_read is not None for entry, _ in ranked),
        partial={answer.item.source_id: answer.partial_read for answer in answers if answer.partial_read is not None},
    )
    _close(db, begun, outcome, reasons, calls, counts, now)

    return counts


def _see(db: Session, seen_rows: dict, begun: Begun, ref: PostingRef, key: str | None, at: datetime) -> None:
    row = seen_rows.get((ref.source_id, ref.posting_id))

    if row is None:
        row = JobWatchSeen(
            watch_id=begun.watch_id,
            source_id=ref.source_id,
            posting_id=ref.posting_id,
            owner_id=begun.owner_id,
            job_key=key,
            first_seen_at=at,
            last_seen_at=at,
        )
        db.add(row)
        seen_rows[(ref.source_id, ref.posting_id)] = row
    else:
        row.last_seen_at = max(row.last_seen_at, at)
        row.job_key = row.job_key or key


def _kept(db: Session, owner_id: str, posting: Posting) -> bool:
    """Kept already: in 我的岗位, held as a current recommendation, or in the recycle bin; a lead, when its official
    posting is."""
    refs = [posting.ref] + ([posting.official] if posting.official is not None else [])

    return any(
        posting_in_use(db, owner_id, ref.source_id, ref.posting_id) is not None or posting_in_bin(db, owner_id, ref.source_id, ref.posting_id)
        for ref in refs
    )


def _same(first: Posting, second: Posting, evidence: dict) -> bool:
    official = EvidenceLevel.OFFICIAL.value
    employers_own = evidence.get(first.ref.source_id) == official and evidence.get(second.ref.source_id) == official

    return rules.same_posting(first, second, employers_own=employers_own)


def job_key(posting: Posting, evidence: str | None) -> str | None:
    """A digest of what makes an employer's numbered job one (rules.same_posting): its number and the facts it states,
    never the facts themselves. None for a lead, or a posting without a number."""
    number = rules._folded(posting.identity)

    if evidence != EvidenceLevel.OFFICIAL.value or not number:
        return None

    return hashlib.sha256(json.dumps([number, *rules._facts(posting)], ensure_ascii=False).encode("utf-8")).hexdigest()


def watched_facts(posting: Posting, source_name: str | None, evidence: str | None) -> dict:
    """A new posting's public facts, by the whitelist of a kept unanalysed one (records.posting_facts): never its text,
    an analysis, a link or anything of the user."""
    return {
        "kind": "lead",
        "source_name": source_name,
        "evidence": evidence,
        "title": posting.title,
        "company": posting.company,
        "cities": list(posting.cities),
        "places_stated": list(posting.places_stated),
        "job_type": posting.job_type,
        "type_stated": posting.type_stated,
        "posted_on": _day(posting.posted_on),
        "deadlines": [{"source_id": posting.ref.source_id, "posting_id": posting.ref.posting_id, "deadline": _day(posting.deadline)}],
        "url": None,
        "apply_url": None,
        "read_at": None,
    }


def _scope(scope: dict) -> SearchScope:
    return SearchScope(
        **{
            dimension.value: DimensionScope(
                values=tuple(scope[dimension.value]["values"]),
                others=Others(scope[dimension.value]["others"]),
                ideal=tuple(scope[dimension.value]["ideal"]),
                excluded=tuple(scope[dimension.value]["excluded"]),
            )
            for dimension in Dimension
        },
        targets=tuple(scope["targets"]),
    )


def _value(value) -> str | None:
    if value is None:
        return None

    return " ".join(str(value).split()) or None


def _touch(row, now: datetime) -> None:
    row.revision += 1
    row.updated_at = now


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()
