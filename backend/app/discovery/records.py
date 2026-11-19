"""Postings, search summaries and analyses the user keeps from a search (U3-D3a, D3b).

Nothing here runs by itself: every write is one the user asked for, on the result page of a finished search or on
their own records. A search never writes here, and its tools never read here. One thing reaches a later search, through
its pages only (U3-D3c, skipped_refs): the postings the user skipped, in use, on the sources the search is confirmed
for, by source and id alone; the search lists none of them, nor a lead pointing to one. Undone, changed to another
decision or deleted, a skip stops; restored, it is back. No reason and no note ever leaves here.

What may be kept, and from where, is the server's alone:
- a search's results may be kept only if every source it was confirmed for is one the server lets keep: listed in
  the set of sources the pages run on (SourceSet.keep) and reached as synthetic data (KEEPABLE_ACCESS). In D3a no
  real source may be kept, whatever a set says; a search that asked one keeps nothing, by any entry;
- the facts kept are taken from the finished search in memory (the task), never from a form: a form names a
  posting by its source and id, and says what the user decided, why and in their words, nothing else;
- a kept posting is written to again only while it was kept from such a source (source_access).

What is kept is a whitelist (posting_facts, search_summary): a posting's public facts and where they came from,
never its text, an analysis, an experience's words, a preference's words or a decider's own words. A check is kept
with the time its source answered, apart from when the user kept it. A decision and feedback are the user's codes
and words, kept in the library only and never sent anywhere.

Concurrency: every write names what its page saw, the record's id and revision or that there was none (Seen); a
record that changed since, or one made since, stops the write (RecordConflict), so an older page never overwrites a
newer record, nor one deleted and kept again.

Analyses (U3-D3b) are kept apart, only when the user asks, and never with a posting, a summary or a decision: one
posting's analysis from one search, named by the search, the source and the posting id, its search the user's own as
the session says; what is kept comes from the finished search in memory, by whitelist (analysis_content): the
analysis as its result page showed it, each experience quote with what the analysis read it with then and its flags,
never the posting's text, a whole experience, a prompt or a model's answer. Only sources the server lets keep
analyses (SourceSet.keep_analyses, ANALYSIS_KEEPABLE_ACCESS) may be kept, and only while what it rests on is as the
search had it: an experience or the preferences changed since, or a quote no longer in its version, keeps nothing. A
search's analysis of a posting is kept once: kept again it is the one kept; in the bin it is restored, not kept again;
deleted for good, never kept again from that search. A kept analysis is only looked at: nothing here or anywhere
feeds it to a search or another analysis.
"""
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analysis_contracts import (
    CitationBasis,
    EvidenceCitation,
    PreferenceFinding,
    RequirementFinding,
    RequirementGroup,
)
from app.discovery import rules, supplied, updates
from app.discovery.contracts import RECOMMENDED, Access, CheckStatus, Dimension, EndReason, EvidenceLevel, Lead, PostingRef, Result
from app.evidence_coverage import coverage_record
from app.library import job_recommendations, job_texts, job_watches
from app.library.content import PREFERENCES
from app.library.errors import NotFound, RecordConflict
from app.library.job_recommendations import KEEP_FOR, RecState
from app.library.job_choices import (
    ANALYSIS_REASONS,
    ANALYSIS_TARGETS,
    CHECK_CLOSED,
    CHECK_OPEN,
    CHECK_UNANSWERED,
    CHECK_UNKNOWN,
    FEEDBACK_FIELDS,
    FEEDBACK_REASONS,
    REASONS,
    CheckState,
    check_state,
)
from app.library.lifecycle import active_job_record, check_record_revision, posting_in_use, search_in_use, skipped_postings
from app.library.lifecycle import posting_in_bin as lifecycle_posting_in_bin
from app.library.models import (
    ExperienceItem,
    ExperienceVersion,
    JobAnalysis,
    JobAnalysisFeedback,
    JobAnalysisSource,
    JobDecision,
    JobFeedback,
    JobPosting,
    JobPostingCheck,
    JobPostingRecheck,
    JobRecommendation,
    JobRecommendationLatest,
    JobSearch,
    JobUpdateRun,
    JobWatch,
    JobWatchPosting,
    SourceKind,
    TargetType,
    VersionKind,
)
from app.library.projection import current_preferences
from app.library.references import SourceState, analysis_sources, state_of, worst
from app.library.service import is_expired, new_id, record_event
from app.i18n import lazy, t
from app.jd_record import fingerprint_job_posting

# D3a keeps synthetic data only: no real source, whatever a set of sources lists (U3-D3 pre-check, revision 2).
# Synthetic data, and (U3-D6) a source reached by RoleRadar's own requests: kept only where the set the app runs on
# lists it (SourceSet.keep), which only the D6 batch does, by its admission's save permission (batch_live.keep_of);
# validation B lists none. A source not listed is never kept, whatever its access.
KEEPABLE_ACCESS = frozenset({Access.SYNTHETIC, Access.LOCAL_FETCH})
NOTE_CHARS = 200
# What a kept check says, from what the search knew (U3-D3a F1): what its source answered at the last check it
# answered (Result/Lead.answered_status), and, apart, the time its latest check got no answer (check_failed_at). A lead
# whose source last answered closed, then gave no answer, is kept as closed then and unanswered after; one never
# answered as unanswered only. Nothing is taken from a time alone, nor from why or any other text.
ANSWER_WORDS = {CheckStatus.OPEN: CHECK_OPEN, CheckStatus.CLOSED: CHECK_CLOSED}
# 2: each search's failure, in the fixed words the result page said it (U3-D3a F2).
# 3: how many postings the user skipped the search came across, each once (U3-D3c). A summary of version 2 has none.
SUMMARY_VERSION = 3
NOT_KEEPABLE =lazy("job.recs.1")
NOT_FINISHED = lazy("job.recs.2")
# Analyses (U3-D3b): synthetic data only, as for postings; a set lists the sources it lets keep analyses of apart
# (SourceSet.keep_analyses), never wider than what it lets keep at all.
# The same for analyses, never wider than keep: listed by SourceSet.keep_analyses alone (the D6 batch, only when its
# admission allows quotes from the analyses too).
ANALYSIS_KEEPABLE_ACCESS = frozenset({Access.SYNTHETIC, Access.LOCAL_FETCH})
# 2 (U3-D6 R4): each requirement also keeps the items its words list and those the cited practice names, and the
# condition its posting quote lays down, whether it holds and the posting's line that shows it. Version 1 has none:
# it reads with them empty, as its page showed it then.
ANALYSIS_CONTENT_VERSION = 2
NO_ANALYSIS_KEEP = lazy("job.recs.3")
NO_ANALYSIS = lazy("job.recs.4")
ANALYSIS_IN_BIN = lazy("job.recs.5")
ANALYSIS_PURGED = lazy("job.recs.6")
MATERIAL_CHANGED = (
    lazy("job.recs.7")
)
NO_PREFERENCE_VERSION = (
    lazy("job.recs.8")
)
ANALYSIS_REFUSALS = (NOT_FINISHED, NO_ANALYSIS, ANALYSIS_IN_BIN, ANALYSIS_PURGED, MATERIAL_CHANGED, NO_PREFERENCE_VERSION)


class KeepRefused(Exception):
    """The server does not let this be kept: nothing was written."""

    def __init__(self, reason: str = NOT_KEEPABLE):
        super().__init__(reason)
        self.reason = reason


class InputRejected(Exception):
    """What the form said is not one of the choices, or too long: nothing was written."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RecommendationLapsed(Exception):
    """A current recommendation whose time has come (U3-D4b-2): nothing is written to it, and it is not added to 我的岗位."""


@dataclass(frozen=True)
class Seen:
    """What a page showed of a record: its id and revision, or that there was none in use (both None)."""

    record_id: str | None = None
    revision: int | None = None


@dataclass(frozen=True)
class Found:
    """A posting as a finished search in memory has it, with the source it was confirmed for: what is kept of it
    comes from here, never from a form."""

    search_id: str
    item: Result | Lead
    access: str
    source_name: str


# --- what the server lets keep ---------------------------------------------------------------------------------------


def keepable_grant(grant, keep: frozenset[str]) -> bool:
    return grant.source_id in keep and grant.access in KEEPABLE_ACCESS


def search_keepable(task, keep: frozenset[str]) -> bool:
    """Every source the search was confirmed for may be kept; a search that asked any other keeps nothing."""
    grants = task.confirmation.sources

    return bool(grants) and all(keepable_grant(grant, keep) for grant in grants)


def finished_outcome(task, keep: frozenset[str]):
    """The outcome of a search whose results may be kept, or why not (KeepRefused)."""
    if not search_keepable(task, keep):
        raise KeepRefused()

    if task.finished is None:
        raise KeepRefused(NOT_FINISHED)

    return task.finished.outcome


def analyses_keepable(task, keep: frozenset[str], keep_analyses: frozenset[str]) -> bool:
    """Every source the search was confirmed for may be kept, and its analyses too (U3-D3b)."""
    grants = task.confirmation.sources

    return search_keepable(task, keep) and all(
        grant.source_id in keep_analyses and grant.access in ANALYSIS_KEEPABLE_ACCESS for grant in grants
    )


def found_in(task, keep: frozenset[str], source_id: str, posting_id: str) -> Found:
    """The posting a form names, as the finished search has it among its results and leads."""
    outcome = finished_outcome(task, keep)
    ref = PostingRef(source_id, posting_id)
    grants = {grant.source_id: grant for grant in task.confirmation.sources}

    for item in (*outcome.results, *outcome.leads):
        if item.ref == ref and ref.source_id in grants:
            grant = grants[ref.source_id]

            return Found(task.id, item, grant.access.value, grant.name)

    raise NotFound()


# --- what is kept, by whitelist ---------------------------------------------------------------------------------------


def posting_facts(found: Found) -> dict:
    """A posting's public facts as the search had them: never its text, its analysis, its category, what to confirm
    before applying (made with the user's preferences) or any note."""
    item = found.item
    is_result = isinstance(item, Result)

    if is_result:
        deadlines = [
            {"source_id": ref.source_id, "posting_id": ref.posting_id, "deadline": _day(deadline)}
            for ref, deadline in item.deadlines
        ]
    else:
        deadlines = [{"source_id": item.ref.source_id, "posting_id": item.ref.posting_id, "deadline": _day(item.deadline)}]

    return {
        "kind": "result" if is_result else "lead",
        "source_name": found.source_name,
        "evidence": item.evidence.value,
        "title": item.title,
        "company": item.company,
        "cities": list(item.cities),
        "places_stated": list(item.places_stated),
        "job_type": item.job_type,
        "type_stated": item.type_stated,
        "posted_on": _day(item.posted_on),
        "deadlines": deadlines,
        "url": item.url if is_result else None,
        "apply_url": item.apply_url if is_result else None,
        "read_at": _iso(item.read_at) if is_result else None,
    }


def search_summary(task, mode: str) -> dict:
    """A finished search in short, by whitelist: what it was confirmed for, what it searched and how it ended, in
    counts. Never a posting's text or facts, an analysis, an experience's or a preference's words, a decider's notes,
    nor the words of a refused action."""
    confirmation = task.confirmation
    outcome = task.finished.outcome
    trace = task.finished.trace
    scope = confirmation.scope
    by_tool: dict[str, dict[str, int]] = {}

    for step in trace.steps:
        tool = step.tool.value if step.tool is not None else "none"
        counts = by_tool.setdefault(tool, {})
        counts[step.outcome] = counts.get(step.outcome, 0) + 1

    categories: dict[str, int] = {}

    for result in outcome.results:
        categories[result.category.value] = categories.get(result.category.value, 0) + 1

    return {
        "version": SUMMARY_VERSION,
        "mode": mode,
        # The language its explanatory text was asked for (UI bilingual batch, plan 6.3); a summary kept before has none.
        "result_language": confirmation.result_language,
        "stage": task.stage.value,
        "cancelled_by": task.cancelled_by,
        "sources": [
            {"source_id": grant.source_id, "name": grant.name, "access": grant.access.value, "evidence": grant.evidence.value}
            for grant in confirmation.sources
        ],
        "scope": {
            "targets": list(scope.targets),
            **{
                dimension.value: {
                    "values": list(scope.of(dimension).values),
                    "others": scope.of(dimension).others.value,
                    "ideal": list(scope.of(dimension).ideal),
                    "excluded": list(scope.of(dimension).excluded),
                }
                for dimension in Dimension
            },
        },
        # Ids, versions and field names only: never what the experiences say.
        "experiences": [
            {"item_id": source.item_id, "version_id": source.version_id, "fields": None if source.fields is None else list(source.fields)}
            for source in confirmation.selection
        ],
        # Which preferences were sent with each analysis, by name: never their words.
        "preference_fields": sorted(preference.field for preference in confirmation.preferences),
        "recipient": {"provider": confirmation.recipient.provider, "model": confirmation.recipient.model},
        "notice_version": confirmation.notice_version,
        "limits": asdict(confirmation.limits),
        "end": outcome.end.value,
        "personalised": outcome.personalised,
        "counts": {
            "results": len(outcome.results),
            "results_by_category": categories,
            "leads": len(outcome.leads) + outcome.more_leads,
            "unlisted": {reason.value: len(refs) for reason, refs in outcome.unlisted},
            "merged": len(outcome.merged),
            "unverifiable": outcome.unverifiable,
            "foreign": outcome.foreign,
            "skipped": outcome.skipped,
            "blocked": {codes: count for codes, count in outcome.blocked},
            "refusals": len(outcome.refusals),
        },
        "searches": [
            {
                "source_id": record.source_id,
                "keywords": record.keywords,
                "filters": {dimension.value: value for dimension, value in record.filters},
                "found": record.found,
                "outcome": record.outcome.value,
                "unverifiable": record.unverifiable,
                "foreign": record.foreign,
                "listed": record.listed,
                "partial": record.partial,
                "reused": record.reused,
                # Why a failed search failed, in the fixed words the result page said it (sources.failure_text).
                "failure": record.failure,
            }
            for record in outcome.searches
        ],
        "partial": [{"source_id": read.source_id, "read": read.read, "combinations": list(read.combinations)} for read in outcome.partial],
        "unsearched": list(outcome.unsearched),
        "unjudged": list(outcome.unjudged),
        "steps": {"decisions": trace.decisions, "by_tool": by_tool},
        "requests": {"handled": task.handed.total},
    }


def analysis_facts(found: Found) -> dict:
    """A kept analysis's posting facts: posting_facts, and the check its result rests on (the last one its source
    answered, and what it answered then), never a time taken for an answer."""
    item = found.item

    return {
        **posting_facts(found),
        "check": {"status": ANSWER_WORDS.get(item.answered_status, CHECK_UNKNOWN), "answered_at": _iso(item.checked_at)},
    }


def analysis_content(item: Result) -> dict:
    """An analysis as its result page showed it, by whitelist (U3-D3b): the category, what to confirm first and the
    notes; each requirement with the posting's words it was judged on, the conclusion and the reasons; each experience
    quote with where it stands (record, version, field), the words the analysis read it with then (context) and its
    flags, so a negation, a plan or a team's work it was read with is never lost when the record goes; the preferences
    compared with the user's own words and the posting's; and what it rests on, by ids, numbers and field names. Never
    the posting's text, a whole experience, a prompt or a model's answer as such."""
    return {
        "version": ANALYSIS_CONTENT_VERSION,
        "category": item.category.value,
        "first_checks": list(item.first_checks),
        "notes": list(item.notes),
        **analysis_body(item.analysis),
    }


def analysis_body(analysis) -> dict:
    """The part of a kept analysis that is the analysis itself (analysis_content), for a search's analysis or a
    standalone one (plan 5.3). It keeps the 岗位要求依据覆盖 count as it was made from these very findings
    (display_metrics, evidence_coverage_v1, owner redesign plan 7.3): optional, so a record kept before has none, and
    then none is shown for it rather than counted again."""
    return {
        "display_metrics": {"evidence_coverage": coverage_record(analysis)},
        "findings": [
            {
                "requirement_id": finding.requirement_id,
                "group": RequirementGroup(finding.group).value,
                "text": finding.text,
                "job_posting_quote": finding.job_posting_quote,
                "conclusion": finding.conclusion,
                "reasons": list(finding.reasons),
                "listed": list(finding.listed),
                "listed_mode": finding.listed_mode,
                "named": list(finding.named),
                "condition": finding.condition,
                "condition_state": finding.condition_state,
                "condition_fact": finding.condition_fact,
                "citations": [
                    {
                        "item_id": citation.item_id,
                        "version_id": citation.version_id,
                        "category": citation.category,
                        "field": citation.field,
                        "quote": citation.quote,
                        "basis": CitationBasis(citation.basis).value,
                        "context": list(citation.context),
                        "flags": list(citation.flags),
                    }
                    for citation in finding.citations
                ],
            }
            for finding in analysis.findings
        ],
        "preferences": analysis.preferences,
        "preference_findings": [
            {
                "preference_id": finding.preference_id,
                "field": finding.field,
                "text": finding.text,
                "conclusion": finding.conclusion,
                "job_quotes": list(finding.job_quotes),
                "reasons": list(finding.reasons),
                "firm": finding.firm,
            }
            for finding in analysis.preference_findings
        ],
        "sources": [
            {
                "item_id": basis.item_id,
                "version_id": basis.version_id,
                "category": basis.category,
                "version_number": basis.version_number,
                "fields": list(basis.fields),
            }
            for basis in analysis.sources
        ],
    }


def analysis_generation(task, item: Result, mode: str) -> dict:
    """What made a kept analysis: the search, the service it went to, the versions of what made it, by name and
    number only. task_fingerprint names the analysis, it is never evidence of anything."""
    analysis, confirmation = item.analysis, task.confirmation

    return {
        "search": task.id,
        "mode": mode,
        "analysis_mode": analysis.mode,
        "recipient": {"provider": confirmation.recipient.provider, "model": confirmation.recipient.model},
        "notice_version": confirmation.notice_version,
        "rules_version": rules.RULES_VERSION,
        "requests_sent": analysis.requests_sent,
        "task_fingerprint": analysis.task_fingerprint,
        "job": None if analysis.job is None else asdict(analysis.job),
        "analysis": None if analysis.analysis is None else asdict(analysis.analysis),
    }


# --- writes from a finished search -------------------------------------------------------------------------------------


def keep_posting(db: Session, owner_id: str, found: Found, seen: Seen, now: datetime, *, forget=None) -> JobPosting:
    """The posting kept as the page saw it: made if none is in use, else the one in use, its checks from this search
    added. A record made or changed since the page was shown stops it (RecordConflict).

    A current recommendation in use is added to 我的岗位 by this very write (U3-D4b-2). One whose time has come is gone
    already, as the page showed it: it is removed here, and what memory holds of it with it (forget), before the posting
    is kept anew; with nothing to drop that memory, nothing is written (RecommendationLapsed)."""
    if found.access not in KEEPABLE_ACCESS:
        # found_in let it through only from a search the server lets keep; this holds whatever a caller passes.
        raise KeepRefused()

    ref = found.item.ref
    row = posting_in_use(db, owner_id, ref.source_id, ref.posting_id)

    if row is not None and job_recommendations.lapsed(db, row, now):
        if forget is None:
            raise RecommendationLapsed()

        remove_recommendations(db, owner_id, [row.id], forget=forget)
        row = None

    _check_seen(row, seen)

    if row is None:
        row = _new_row(db, owner_id, found, now)
        record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_kept", now)
    else:
        _check_access(row)
        _add_to_mine(db, owner_id, row, now)

    if _add_checks(db, owner_id, row, found, now):
        _touch(row, now)

    return row


def decide_on_found(
    db: Session,
    owner_id: str,
    found: Found,
    seen: Seen,
    decision: str,
    reasons: list[str],
    note: str,
    now: datetime,
    *,
    forget=None,
) -> JobPosting:
    checked = checked_decision(decision, reasons, note)
    row = keep_posting(db, owner_id, found, seen, now, forget=forget)
    _set_decision(db, owner_id, row, checked, now)

    return row


def feedback_on_found(
    db: Session,
    owner_id: str,
    found: Found,
    seen: Seen,
    field: str,
    reason: str,
    note: str,
    now: datetime,
    *,
    forget=None,
) -> JobPosting:
    checked = checked_feedback(field, reason, note)
    row = keep_posting(db, owner_id, found, seen, now, forget=forget)
    _add_feedback(db, owner_id, row, found.search_id, checked, now)

    return row


def keep_search(db: Session, owner_id: str, task, keep: frozenset[str], mode: str, seen: Seen, now: datetime) -> JobSearch:
    """The search's summary kept as the page saw it: one in use per search."""
    finished_outcome(task, keep)
    row = search_in_use(db, owner_id, task.id)
    _check_seen(row, seen)

    if row is not None:
        return row

    row = JobSearch(
        id=new_id(),
        owner_id=owner_id,
        search_id=task.id,
        summary=search_summary(task, mode),
        revision=1,
        kept_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    db.flush()
    record_event(db, owner_id, row.id, TargetType.JOB_SEARCH, row.id, "job_search_kept", now)

    return row


def keep_analysis(
    db: Session,
    owner_id: str,
    task,
    found: Found,
    keep: frozenset[str],
    keep_analyses: frozenset[str],
    mode: str,
    seen: Seen,
    now: datetime,
) -> JobAnalysis:
    """A posting's analysis from this search, kept as the page saw it (U3-D3b): made once, with what it rests on, and
    only while that is as the search had it. Kept already, it is the one kept (a page that saw none is told so:
    RecordConflict); in the bin, or deleted for good since, it is not kept again (KeepRefused)."""
    if not analyses_keepable(task, keep, keep_analyses) or found.access not in ANALYSIS_KEEPABLE_ACCESS:
        raise KeepRefused(NO_ANALYSIS_KEEP)

    item = found.item

    if not isinstance(item, Result):
        raise KeepRefused(NO_ANALYSIS)

    ref = item.ref
    row = db.scalar(
        select(JobAnalysis).where(
            JobAnalysis.owner_id == owner_id,
            JobAnalysis.search_id == found.search_id,
            JobAnalysis.source_id == ref.source_id,
            JobAnalysis.posting_id == ref.posting_id,
        )
    )

    if row is not None and row.deleted_at is None:
        _check_seen(row, seen)

        return row

    if row is not None:
        raise KeepRefused(ANALYSIS_PURGED if is_expired(row, now) else ANALYSIS_IN_BIN)

    if (ref.source_id, ref.posting_id) in task.kept_analyses:
        # Kept from this search before and deleted for good since: never made again from an older page.
        raise KeepRefused(ANALYSIS_PURGED)

    _check_seen(None, seen)
    basis = _analysis_basis(db, owner_id, task, item, now)
    row = JobAnalysis(
        id=new_id(),
        owner_id=owner_id,
        search_id=found.search_id,
        source_id=ref.source_id,
        posting_id=ref.posting_id,
        source_access=found.access,
        facts=analysis_facts(found),
        content=analysis_content(item),
        generation=analysis_generation(task, item, mode),
        content_sha256=item.content_sha256,
        analysed_at=item.analysed_at,
        revision=1,
        saved_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    db.flush()

    for kind, item_id, version_id, fields in basis:
        db.add(
            JobAnalysisSource(
                id=new_id(), owner_id=owner_id, analysis_id=row.id, kind=kind, item_id=item_id, version_id=version_id, fields=fields
            )
        )

    record_event(db, owner_id, row.id, TargetType.JOB_ANALYSIS, row.id, "job_analysis_kept", now)

    return row


# --- standalone analyses (UI bilingual batch, plan 5.3) -------------------------------------------------------------
# An analysis the user made on the analysis page, kept on its own: it needs no saved posting and saves none. It is kept
# once per task (its search_id is the task's, under the same unique index), with what it rests on, its generation
# language and the posting's source as the server verified it when the analysis was reviewed (AnalysisOrigin), never
# as a form says at saving. A posting pasted into the analysis page alone has no official id, and is given none.
STANDALONE_PREFIX = "analysis:"
PASTED_ANALYSIS_SOURCE = "user-pasted"
PASTED_ANALYSIS_POSTING = "-"
# Synthetic sources and the user's own text; never a real source (the D6 batch keeps a search's analyses only).
STANDALONE_KEEPABLE_ACCESS = frozenset({Access.SYNTHETIC, job_texts.USER_PROVIDED})
STANDALONE_NOT_KEEPABLE = lazy("job.standalone.not_keepable")
STANDALONE_NO_ANALYSIS = lazy("job.standalone.no_analysis")
STANDALONE_IN_BIN = lazy("job.standalone.in_bin")
STANDALONE_PURGED = lazy("job.standalone.purged")
STANDALONE_CHANGED = lazy("job.standalone.changed")


@dataclass(frozen=True)
class AnalysisOrigin:
    """Where a standalone analysis's posting came from, as the server verified it when the task was reviewed: a pasted
    posting kept in 我的岗位 (record_id), one read from a registered source by 补充岗位, or text pasted here only."""

    source_id: str
    posting_id: str
    access: str
    evidence: str
    source_name: str = ""
    title: str = ""
    company: str = ""
    url: str | None = None
    record_id: str | None = None
    # The 补充岗位 result it came from, in this session's memory (never stored): where its page offers 加入我的岗位.
    supplied_token: str | None = None


PASTED_ORIGIN = AnalysisOrigin(PASTED_ANALYSIS_SOURCE, PASTED_ANALYSIS_POSTING, job_texts.USER_PROVIDED, job_texts.USER_PROVIDED)


def standalone_of(db: Session, owner_id: str, task_id: str) -> JobAnalysis | None:
    """The analysis kept from this task, in use or in the bin; None if none was kept, or it is gone for good."""
    return db.scalar(
        select(JobAnalysis).where(JobAnalysis.owner_id == owner_id, JobAnalysis.search_id == STANDALONE_PREFIX + task_id)
    )


def keep_standalone_analysis(
    db: Session,
    owner_id: str,
    task_id: str,
    request,
    result,
    origin: AnalysisOrigin,
    *,
    analysed_at: datetime | None,
    mode: str,
    saved_id: str | None,
    now: datetime,
) -> JobAnalysis:
    """A standalone analysis kept as its result page showed it: made once, with what it rests on, only while that is
    as the analysis had it. Kept already and in use, it is the one kept; in the bin, or deleted for good since (the
    task remembers what it kept: saved_id), it is never made again from the old page (KeepRefused)."""
    if origin.access not in STANDALONE_KEEPABLE_ACCESS:
        raise KeepRefused(STANDALONE_NOT_KEEPABLE)

    if result is None or result.status != "complete" or result.analysis is None:
        raise KeepRefused(STANDALONE_NO_ANALYSIS)

    row = standalone_of(db, owner_id, task_id)

    if row is not None and row.deleted_at is None:
        return row

    if row is not None:
        raise KeepRefused(STANDALONE_PURGED if is_expired(row, now) else STANDALONE_IN_BIN)

    if saved_id is not None:
        raise KeepRefused(STANDALONE_PURGED)

    sent = {preference.field: preference.text for preference in request.preferences}
    basis = _basis_rows(db, owner_id, result, sent, now, STANDALONE_CHANGED)
    row = JobAnalysis(
        id=new_id(),
        owner_id=owner_id,
        search_id=STANDALONE_PREFIX + task_id,
        source_id=origin.source_id,
        posting_id=origin.posting_id,
        source_access=origin.access,
        facts={
            "kind": "standalone",
            "source_name": origin.source_name,
            "evidence": origin.evidence,
            "title": origin.title,
            "company": origin.company,
            "cities": [],
            "places_stated": [],
            "job_type": None,
            "type_stated": None,
            "posted_on": None,
            "deadlines": [],
            "url": origin.url,
            "apply_url": None,
            "read_at": None,
            "record_id": origin.record_id,
            "check": {"status": CHECK_UNKNOWN, "answered_at": None},
        },
        content={
            "version": ANALYSIS_CONTENT_VERSION,
            "kind": "standalone",
            "category": None,
            "first_checks": [],
            "notes": [],
            **analysis_body(result),
        },
        generation={
            "standalone": task_id,
            "mode": mode,
            "analysis_mode": result.mode,
            "recipient": {"provider": request.recipient.provider, "model": request.recipient.model},
            "notice_version": request.notice_version,
            "requests_sent": result.requests_sent,
            "task_fingerprint": result.task_fingerprint,
            "job": None if result.job is None else asdict(result.job),
            "analysis": None if result.analysis is None else asdict(result.analysis),
            "result_language": request.result_language,
        },
        content_sha256=fingerprint_job_posting(request.job_posting),
        analysed_at=analysed_at,
        revision=1,
        saved_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    db.flush()

    for kind, item_id, version_id, fields in basis:
        db.add(
            JobAnalysisSource(
                id=new_id(), owner_id=owner_id, analysis_id=row.id, kind=kind, item_id=item_id, version_id=version_id, fields=fields
            )
        )

    record_event(db, owner_id, row.id, TargetType.JOB_ANALYSIS, row.id, "job_analysis_kept", now)

    return row


def feedback_on_analysis(
    db: Session, owner_id: str, record_id: str, revision: int, part: str, reason: str, note: str, now: datetime
) -> JobAnalysis:
    """Say a part of a kept analysis is not right: one of its own parts, a reason from the list, the user's words."""
    row = active_job_record(db, owner_id, JobAnalysis, record_id)
    check_record_revision(row, revision)
    _check_analysis_access(row)
    target, target_ref = checked_part(row.content, part)

    if reason not in dict(ANALYSIS_REASONS):
        raise InputRejected(t("job.recs.9"))

    db.add(
        JobAnalysisFeedback(
            id=new_id(),
            owner_id=owner_id,
            analysis_id=row.id,
            target=target,
            target_ref=target_ref,
            reason=reason,
            note=_note(note),
            created_at=now,
        )
    )
    _touch(row, now)
    record_event(db, owner_id, row.id, TargetType.JOB_ANALYSIS, row.id, "job_analysis_feedback_added", now)

    return row


# --- writes on a kept posting ------------------------------------------------------------------------------------------


class DecisionNeedsChoice(Exception):
    """Changing the decision would leave behind the reasons given for the one before (reasons belong to one decision):
    nothing is written, and the page asks first, with the new decision's reasons and the note shown (owner review)."""

    def __init__(self, decision: str):
        super().__init__(decision)
        self.decision = decision


@dataclass(frozen=True)
class Decided:
    row: JobPosting
    # False when the decision, its reasons and its note were already as asked: nothing was written.
    changed: bool


def decide_on_kept(
    db: Session,
    owner_id: str,
    record_id: str,
    revision: int,
    decision: str,
    reasons: list[str] | None,
    note: str | None,
    now: datetime,
) -> JobPosting:
    """Change the decision on a kept posting, or take it back (decision empty); see decide."""
    return decide(db, owner_id, record_id, revision, decision, reasons, note, now).row


def decide(
    db: Session,
    owner_id: str,
    record_id: str,
    revision: int,
    decision: str,
    reasons: list[str] | None,
    note: str | None,
    now: datetime,
) -> Decided:
    """Change the decision on a kept posting, or take it back (decision empty). Deciding on a current recommendation adds
    it to 我的岗位 (U3-D4b-2); one whose time has come is written to never (RecommendationLapsed).

    What is not sent is never taken for empty (owner review): reasons None and note None mean "as they are". Choosing
    the decision already made, with nothing else said, changes nothing (Decided.changed False). Choosing another one
    keeps the note; the reasons belong to the decision they were given for, so when there are some, nothing is written
    and DecisionNeedsChoice asks the page to show the new decision's reasons first. Taking a decision back clears its
    reasons and note, as it always did, and the page says so beside the button."""
    row = active_job_record(db, owner_id, JobPosting, record_id)

    if job_recommendations.lapsed(db, row, now):
        raise RecommendationLapsed()

    check_record_revision(row, revision)
    # A posting the user pasted is theirs to decide on too (U3-D4d): this alone, of all a kept posting may have done.
    _check_managed(row)

    if decision == "" and job_recommendations.marker_of(db, row.id) is not None:
        raise InputRejected(t("job.recs.10"))

    if decision == "":
        row.decision, row.reasons, row.note, row.decided_at = None, [], None, None
        _touch(row, now)
        record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_undecided", now)

        return Decided(row, True)

    same = row.decision == decision

    if reasons is None:
        if same:
            reasons = list(row.reasons or [])
        elif row.reasons:
            checked_decision(decision, [], "")
            raise DecisionNeedsChoice(decision)
        else:
            reasons = []

    if note is None:
        note = row.note or ""

    checked = checked_decision(decision, reasons, note)

    if same and checked.reasons == tuple(row.reasons or ()) and checked.note == row.note:
        return Decided(row, False)

    _add_to_mine(db, owner_id, row, now)
    _set_decision(db, owner_id, row, checked, now)

    return Decided(row, True)


def feedback_on_kept(
    db: Session, owner_id: str, record_id: str, revision: int, field: str, reason: str, note: str, now: datetime
) -> JobPosting:
    """Say a fact of a kept posting is not right; said of a current recommendation, it adds it to 我的岗位 (U3-D4b-2)."""
    row = active_job_record(db, owner_id, JobPosting, record_id)

    if job_recommendations.lapsed(db, row, now):
        raise RecommendationLapsed()

    check_record_revision(row, revision)
    _check_access(row)
    checked = checked_feedback(field, reason, note)
    _add_to_mine(db, owner_id, row, now)
    _add_feedback(db, owner_id, row, row.search_id, checked, now)

    return row


# --- current recommendations held for tracking (U3-D4b-2) ---------------------------------------------------------------
#
# 临时留存跟踪: when a search the user ticked 临时留存并跟踪这次的推荐 for ends as it should, its recommendations (full
# results of the first two categories) are held as postings with a row in job_recommendations, KEEP_FOR long, so the
# checks of D4a and D4b-1 track them. They are not 我的岗位: keeping, deciding or saying a fact is not right adds one
# to 我的岗位 (_add_to_mine); until then it lapses at its time, is replaced by a later search's, or goes when the user
# no longer tracks it, each time removed with its checks, never through the recycle bin, and with what memory holds of
# it dropped before the removal commits (remove_recommendations). A search is taken once, and only if it started later
# than the last one taken (JobRecommendationLatest): the same search again, or an older one coming back late, never
# brings back a recommendation removed.

# How a search must have ended for its recommendations to be held: by itself, without an error, a block or a stop.
# The two ends without a suitable result hold none, but are taken, so an older search coming back late changes nothing.
RETAINED_ENDS = frozenset({EndReason.ENOUGH, EndReason.SCOPE_DONE, EndReason.LIMIT, EndReason.NO_RESULTS, EndReason.PARTLY_NO_RESULTS})
NOT_HELD = lazy("job.recs.11")


@dataclass(frozen=True)
class Retention:
    """What became of a search's current recommendations, for its result page: why none were taken (reason, a code),
    or how many were held anew, held again for it, found in 我的岗位 or in the recycle bin (neither touched), and how
    many held before were removed for not being among them."""

    reason: str | None = None
    kept: int = 0
    renewed: int = 0
    mine: int = 0
    in_bin: int = 0
    removed: int = 0
    until: datetime | None = None


def recommendations_offered(grants, keep: frozenset[str], chosen: bool) -> bool:
    """Whether a search may hold its recommendations: it analyses (an experience is chosen), and every source it asks
    may be kept and tracked here, listed by the set the pages run on (synthetic data, or the D6 batch's board under its
    save permission)."""
    return chosen and bool(grants) and all(keepable_grant(grant, keep) for grant in grants)


def retention_problem(task, keep: frozenset[str]) -> str | None:
    """Why a search's recommendations may not be held, or None. DONE alone is not enough: the search must have run,
    returned its outcome, met no error and no stop, and ended by itself (RETAINED_ENDS)."""
    confirmation = task.confirmation

    if not confirmation.track_recommendations:
        return "not_ticked"

    if task.stage != "done" or task.cancelled_by is not None:
        return "stopped"

    if task.problem is not None or task.problems or task.finished is None or task.started_at is None:
        return "failed"

    if task.finished.outcome.end not in RETAINED_ENDS:
        return "ended_early"

    if not recommendations_offered(confirmation.sources, keep, bool(confirmation.selection)):
        return "not_offered"

    return None


def recommended_results(task, keep: frozenset[str]) -> list[Result]:
    """The search's recommendations: its full results of the first two categories, still published as it ended, official
    postings, on a source that may be kept."""
    grants = {grant.source_id: grant for grant in task.confirmation.sources}

    return [
        item
        for item in task.finished.outcome.results
        if item.category in RECOMMENDED
        and item.status is CheckStatus.OPEN
        and item.evidence is EvidenceLevel.OFFICIAL
        and item.ref.source_id in grants
        and keepable_grant(grants[item.ref.source_id], keep)
    ]


def keep_recommendations(db: Session, task, keep: frozenset[str], now: datetime, *, forget) -> Retention:
    """Hold a finished search's recommendations, in the caller's transaction, once, and only if it started later than
    the last search taken; or say why not. Nothing of 我的岗位, of the recycle bin or of a kept analysis is touched."""
    problem = retention_problem(task, keep)

    if problem is not None:
        return Retention(reason=problem)

    owner_id = task.owner_id
    latest = db.get(JobRecommendationLatest, owner_id)

    if latest is not None and (latest.search_started_at, latest.search_id) >= (task.started_at, task.id):
        # Taken already, or a later search was: a callback again, or late, changes nothing.
        return Retention(reason="superseded")

    if latest is None:
        db.add(JobRecommendationLatest(owner_id=owner_id, search_id=task.id, search_started_at=task.started_at, decided_at=now))
    else:
        latest.search_id, latest.search_started_at, latest.decided_at = task.id, task.started_at, now

    results = recommended_results(task, keep)

    if not results:
        # Weighed, with nothing to hold: the recommendations held before stay as they are.
        return Retention(reason="none_recommended")

    grants = {grant.source_id: grant for grant in task.confirmation.sources}
    until = now + KEEP_FOR
    held, counts = [], {"kept": 0, "renewed": 0, "mine": 0, "in_bin": 0}

    for item in results:
        ref = item.ref
        grant = grants[ref.source_id]
        found = Found(task.id, item, grant.access.value, grant.name)
        row = posting_in_use(db, owner_id, ref.source_id, ref.posting_id)

        if row is not None:
            state = job_recommendations.state_of(db, row, now)

            if state is RecState.MINE:
                counts["mine"] += 1
                continue

            if state is RecState.LAPSED:
                # Gone already: removed, and held anew under another id; never brought back.
                remove_recommendations(db, owner_id, [row.id], forget=forget)
                row = None

        if row is None:
            if posting_in_bin(db, owner_id, ref):
                counts["in_bin"] += 1
                continue

            row = _new_row(db, owner_id, found, now)
            db.add(JobRecommendation(posting_record_id=row.id, owner_id=owner_id, search_id=task.id, recommended_at=now, until=until))

            if _add_checks(db, owner_id, row, found, now):
                _touch(row, now)

            counts["kept"] += 1
        else:
            marker = job_recommendations.marker_of(db, row.id)
            marker.search_id, marker.recommended_at, marker.until = task.id, now, until
            _add_checks(db, owner_id, row, found, now)
            _touch(row, now)
            counts["renewed"] += 1

        held.append(row.id)

    db.flush()
    others = [marker.posting_record_id for marker in job_recommendations.markers(db, owner_id) if marker.posting_record_id not in held]
    removed = remove_recommendations(db, owner_id, others, forget=forget)

    return Retention(removed=len(removed), until=until, **counts)


def remove_recommendations(db: Session, owner_id: str, record_ids, *, forget) -> list[str]:
    """Remove these current recommendations of the owner's, each still held, in the caller's transaction: their rows
    from the database (job_recommendations.remove_rows), then what memory holds of each (forget: the text a 申请前核对
    read, and its reservation). Both before the transaction commits, as a deletion does in D4a: a check that comes back
    late is then shown nowhere, and nothing is published of one removed. A posting in 我的岗位 is never removed here."""
    removed = []

    for record_id in dict.fromkeys(record_ids):
        marker = job_recommendations.marker_of(db, record_id)

        if marker is None or marker.owner_id != owner_id:
            continue

        if job_recommendations.remove_rows(db, record_id):
            removed.append(record_id)

    for record_id in removed:
        forget(record_id)

    return removed


def remove_lapsed(db: Session, now: datetime, *, forget, owner_id: str | None = None) -> int:
    """Remove the current recommendations whose time has come, of one owner or of all, in the caller's transaction."""
    owners: dict[str, list[str]] = {}

    for marker in job_recommendations.lapsed_markers(db, now, owner_id):
        owners.setdefault(marker.owner_id, []).append(marker.posting_record_id)

    return sum(len(remove_recommendations(db, owner, ids, forget=forget)) for owner, ids in owners.items())


def add_to_mine(db: Session, owner_id: str, record_id: str, revision: int, now: datetime) -> JobPosting:
    """加入我的岗位 from a current recommendation's own page (U3-D4b-2), as the page saw it; never one whose time has
    come (RecommendationLapsed), and never twice."""
    row = active_job_record(db, owner_id, JobPosting, record_id)

    if job_recommendations.lapsed(db, row, now):
        raise RecommendationLapsed()

    check_record_revision(row, revision)
    _check_access(row)

    if not _add_to_mine(db, owner_id, row, now):
        raise InputRejected(t("job.recs.12"))

    return row


def stop_tracking(db: Session, owner_id: str, record_id: str, revision: int, now: datetime, *, forget) -> str:
    """不再跟踪: remove a current recommendation now, as its page saw it ("stopped"); one whose time has come is removed
    as it is ("lapsed"). A posting in 我的岗位 is not one (KeepRefused): it goes through the recycle bin."""
    row = active_job_record(db, owner_id, JobPosting, record_id)
    state = job_recommendations.state_of(db, row, now)

    if state is RecState.MINE:
        raise KeepRefused(NOT_HELD)

    if state is RecState.LAPSED:
        remove_recommendations(db, owner_id, [row.id], forget=forget)

        return "lapsed"

    check_record_revision(row, revision)
    remove_recommendations(db, owner_id, [row.id], forget=forget)

    return "stopped"


# --- 每日查新: a new posting the user adds to 我的岗位, or no longer shows (U3-D4c) ------------------------------------

WATCHED_IN_BIN = lazy("job.recs.13")


def keep_watched(db: Session, owner_id: str, item_id: str, now: datetime, *, forget) -> JobPosting:
    """A new posting a watch found, added to 我的岗位 as the user asks: made from the public facts the watch kept (the
    kept postings' whitelist), never a form's, saying it came from 每日查新 and when it was first read; no check is
    copied, so the next batch of 现在核对 or 定时检查 checks it. The new posting goes with this write; its id stays seen.

    One kept already is the one in use: a current recommendation is added to 我的岗位 by this very write (U3-D4b-2), one
    whose time has come removed first, as keep_posting does. One in the recycle bin is not kept again (InputRejected).
    One whose time has come, another user's, or one whose watch or summary is gone: NotFound."""
    item = job_watches.new_posting(db, owner_id, item_id, now)
    watch = None if item is None else db.get(JobWatch, item.watch_id)
    summary = None if watch is None else db.get(JobSearch, watch.search_record_id)

    if summary is None or summary.deleted_at is not None:
        raise NotFound()

    source = next((entry for entry in summary.summary.get("sources", []) if entry.get("source_id") == item.source_id), None)

    if source is None or source.get("access") not in {access.value for access in KEEPABLE_ACCESS}:
        raise KeepRefused()

    row = posting_in_use(db, owner_id, item.source_id, item.posting_id)

    if row is not None and job_recommendations.lapsed(db, row, now):
        remove_recommendations(db, owner_id, [row.id], forget=forget)
        row = None

    if row is None:
        if lifecycle_posting_in_bin(db, owner_id, item.source_id, item.posting_id):
            raise InputRejected(WATCHED_IN_BIN)

        row = JobPosting(
            id=new_id(),
            owner_id=owner_id,
            source_id=item.source_id,
            posting_id=item.posting_id,
            source_access=source["access"],
            facts={**item.facts, "via": {"kind": "watch", "first_read_at": _iso(item.found_at)}},
            search_id=summary.search_id,
            decision=None,
            reasons=[],
            note=None,
            decided_at=None,
            revision=1,
            kept_at=now,
            updated_at=now,
            deleted_at=None,
            purge_after=None,
            deletion_id=None,
        )
        db.add(row)
        db.flush()
        record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_kept", now)
    else:
        _check_access(row)
        _add_to_mine(db, owner_id, row, now)

    db.delete(item)

    return row


def dismiss_watched(db: Session, owner_id: str, item_id: str, now: datetime) -> str:
    """不再显示 a new posting: it goes, its id stays seen so it is never new again. The record id of its summary."""
    item = job_watches.new_posting(db, owner_id, item_id, now)
    watch = None if item is None else db.get(JobWatch, item.watch_id)

    if watch is None:
        raise NotFound()

    db.delete(item)

    return watch.search_record_id


def posting_in_bin(db: Session, owner_id: str, ref: PostingRef) -> bool:
    """The user's own kept posting in the recycle bin: holding one in use would keep it from being restored. The query
    lives in app.library.lifecycle (U3-D4c), shared with 每日查新."""
    return lifecycle_posting_in_bin(db, owner_id, ref.source_id, ref.posting_id)


# --- postings the user adds themselves (U3-D4d) ------------------------------------------------------------------------
#
# 补充岗位: a posting read from a link the user gave (app.discovery.supplied: one D4a check of a source the app may read
# now), or one they pasted, said as 用户提供，系统未独立核验. It is added only when the user asks, from what memory holds
# of it, never a form's, and the record made is bound to what memory holds before the write commits (bind), so no record
# is ever committed that memory does not know made it (D4d pre-check 17.2).
#
# A link's posting is its source's: one the user has in use (in 我的岗位, or a current recommendation) or in the bin is
# never made again, and nothing read from the link is written to it (AlreadyKept, SUPPLIED_IN_BIN). A pasted one is the
# user's own: whether it is another posting is theirs to say. The page before saving lists every posting they pasted
# that shares its text, its link or its company and title (supplied_matches), and the write looks again: one the page
# did not show, or one it showed without the user saying this is another posting, stops it (NeedsReview, 17.3).
# Nothing here merges two postings or changes one. A pasted posting is checked, tracked or watched by nothing, and only
# decided on (MANAGED_ACCESS); its text is kept with it (job_texts), the only text of a posting ever kept.

SUPPLIED_SOURCE = "user-provided"
LINK_SEARCH, PASTED_SEARCH = "supplied-link", "user-provided"
PASTED_SOURCE_NAME = "你提供的原文"
# Of everything a kept posting may have done, a pasted one may only be decided on (U3-D4d).
MANAGED_ACCESS = frozenset({*KEEPABLE_ACCESS, job_texts.USER_PROVIDED})
SUPPLIED_IN_BIN = lazy("job.recs.14")
NOT_SUPPLIABLE = lazy("job.recs.15")
NOTHING_READ = lazy("job.recs.16")
# The four things a pasted posting is compared by (D4d 6.3), in the order a page lists them.
IDENTITY_PARTS = ("公司", "职位名", "链接", "原文")


class AlreadyKept(Exception):
    """The source's posting is the user's already, in use (in 我的岗位, or a current recommendation) or in the recycle bin
    (in_bin): nothing was written to it or anywhere. The save bound its result to that record before saying so (F2)."""

    def __init__(self, record_id: str, in_bin: bool = False):
        super().__init__(record_id)
        self.record_id = record_id
        self.in_bin = in_bin


class NeedsReview(Exception):
    """A pasted posting matches one of the user's the page before saving did not show, or one it showed without the
    user saying this is another posting: nothing was written."""


@dataclass(frozen=True)
class Match:
    """One posting the user pasted before that this one shares something with (D4d 6.3): what is the same and what is
    not, by IDENTITY_PARTS; exact when all four are."""

    record_id: str
    title: str
    company: str
    in_bin: bool
    exact: bool
    same: tuple[str, ...]
    different: tuple[str, ...]


def own_posting(db: Session, owner_id: str, ref: PostingRef, now: datetime) -> tuple[str, JobPosting | None]:
    """What the user has of a source's posting now, and which record: "mine", "held" (a current recommendation), "bin"
    (the latest deleted), or "none" (a current recommendation whose time has come counts as none, as everywhere)."""
    row = posting_in_use(db, owner_id, ref.source_id, ref.posting_id)

    if row is not None and not job_recommendations.lapsed(db, row, now):
        return ("held" if job_recommendations.marker_of(db, row.id) is not None else "mine"), row

    binned = db.scalar(
        select(JobPosting)
        .where(
            JobPosting.owner_id == owner_id,
            JobPosting.source_id == ref.source_id,
            JobPosting.posting_id == ref.posting_id,
            JobPosting.deleted_at.is_not(None),
        )
        .order_by(JobPosting.deleted_at.desc(), JobPosting.id)
        .limit(1)
    )

    if binned is not None:
        return "bin", binned

    return "none", None


def supplied_matches(db: Session, owner_id: str, content, now: datetime) -> list[Match]:
    """The postings the user pasted, in use or in the bin still restorable, that share with this one its text, its link
    (one given) or its company and title: never another user's."""
    title, company = supplied.plain(content.title), supplied.plain(content.company)
    link = supplied.link_key(None if content.link is None else content.link.text)
    rows = db.scalars(
        select(JobPosting)
        .where(JobPosting.owner_id == owner_id, JobPosting.source_id == SUPPLIED_SOURCE)
        .order_by(JobPosting.kept_at, JobPosting.id)
    )
    matches = []

    for row in rows:
        if row.deleted_at is not None and is_expired(row, now):
            continue

        facts = row.facts or {}
        via = facts.get("via") or {}
        alike = {
            "公司": supplied.plain(facts.get("company")) == company,
            "职位名": supplied.plain(facts.get("title")) == title,
            "链接": supplied.link_key(via.get("link")) == link,
            "原文": via.get("text_sha256") == content.digest,
        }

        if not (alike["原文"] or (alike["链接"] and link) or (alike["公司"] and alike["职位名"])):
            continue

        matches.append(
            Match(
                row.id,
                facts.get("title") or "",
                facts.get("company") or "",
                row.deleted_at is not None,
                all(alike.values()),
                tuple(part for part in IDENTITY_PARTS if alike[part]),
                tuple(part for part in IDENTITY_PARTS if not alike[part]),
            )
        )

    return matches


def related_postings(db: Session, owner_id: str, title: str, company: str, *, pasted: bool) -> list[JobPosting]:
    """The user's postings in 我的岗位 with the same company and title, of the other kind (pasted ones for a source's,
    a source's for a pasted one): listed for the user to see, never merged."""
    title, company = supplied.plain(title), supplied.plain(company)

    return [
        row
        for row in postings_in_use(db, owner_id)
        if (row.source_access == job_texts.USER_PROVIDED) == pasted
        and job_recommendations.marker_of(db, row.id) is None
        and supplied.plain((row.facts or {}).get("title")) == title
        and supplied.plain((row.facts or {}).get("company")) == company
    ]


def keep_supplied_link(db: Session, owner_id: str, content, keep: frozenset[str], now: datetime, *, bind, forget) -> JobPosting:
    """A posting read from a link, added to 我的岗位 as the user asks: made from what memory holds of the read, with that
    read written as its first check (a run of its own, trigger link), and bound to what memory holds before the write
    commits (bind). One in use already is the user's: AlreadyKept, nothing written to it; one in the bin: InputRejected.
    A current recommendation whose time has come is removed first, as keep_posting does. The same posting among the
    user's new ones of 每日查新 goes with this write; its id stays seen.

    One the user has already, in use or in the bin, ends the result there (F2): it is bound to that record inside this
    write, while the write lock keeps that record from being deleted meanwhile, and the save says AlreadyKept. From
    then on the result is that record's, as if it had made it: deleting, restoring or purging the record never lets it
    make another."""
    ref = content.ref

    if ref.source_id not in keep or content.access not in updates.TRACKABLE_ACCESS:
        raise KeepRefused(NOT_SUPPLIABLE)

    reading = content.reading

    if reading.facts is None:
        raise KeepRefused(NOTHING_READ)

    state, row = own_posting(db, owner_id, ref, now)

    if state in ("mine", "held", "bin"):
        bind(row.id)

        raise AlreadyKept(row.id, in_bin=state == "bin")

    lapsed = posting_in_use(db, owner_id, ref.source_id, ref.posting_id)

    if lapsed is not None:
        remove_recommendations(db, owner_id, [lapsed.id], forget=forget)

    facts = reading.facts
    row = JobPosting(
        id=new_id(),
        owner_id=owner_id,
        source_id=ref.source_id,
        posting_id=ref.posting_id,
        source_access=content.access,
        facts={
            "kind": "supplied",
            "source_name": content.source_name,
            "evidence": EvidenceLevel.OFFICIAL.value,
            "title": facts.get("title"),
            "company": facts.get("company"),
            "cities": list(facts.get("cities") or []),
            "places_stated": list(facts.get("places_stated") or []),
            "job_type": facts.get("job_type"),
            "type_stated": facts.get("type_stated"),
            "posted_on": facts.get("posted_on"),
            "deadlines": [{"source_id": ref.source_id, "posting_id": ref.posting_id, "deadline": facts.get("deadline")}],
            "url": reading.url,
            "apply_url": reading.apply_url,
            "read_at": _iso(reading.answered_at),
            "via": {"kind": "link", "read_at": _iso(content.finished_at)},
        },
        search_id=LINK_SEARCH,
        decision=None,
        reasons=[],
        note=None,
        decided_at=None,
        revision=1,
        kept_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    run = JobUpdateRun(
        id=new_id(),
        owner_id=owner_id,
        trigger=updates.Trigger.LINK.value,
        calls=reading.calls,
        outcome=updates.DONE,
        reason=None,
        started_at=content.started_at,
        finished_at=content.finished_at,
    )
    db.add(run)
    db.flush()
    db.add(
        JobPostingRecheck(
            id=new_id(),
            owner_id=owner_id,
            posting_record_id=row.id,
            run_id=run.id,
            status=reading.status,
            reason=reading.reason,
            evidence=reading.evidence,
            facts=reading.facts,
            content_sha256=reading.content_sha256,
            changes=[],
            checked_at=reading.answered_at or content.finished_at,
            recorded_at=now,
        )
    )
    db.execute(
        delete(JobWatchPosting).where(
            JobWatchPosting.owner_id == owner_id, JobWatchPosting.source_id == ref.source_id, JobWatchPosting.posting_id == ref.posting_id
        )
    )
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_kept", now)
    db.flush()
    bind(row.id)

    return row


def keep_pasted(db: Session, owner_id: str, content, confirmed: frozenset[str], separate: bool, now: datetime, *, bind) -> JobPosting:
    """A posting the user pasted, added to 我的岗位 as they ask: its facts are what they typed, its text and its link as
    the page before saving showed them, and its id its own (never the text's: the same text may be another posting).
    The matches are looked for again here: any the page did not show (confirmed), or any at all unless the user said
    this is another posting (separate), stop it (NeedsReview). Bound to what memory holds before the write commits."""
    found = frozenset(match.record_id for match in supplied_matches(db, owner_id, content, now))

    if found - confirmed or (found and not separate):
        raise NeedsReview()

    row = JobPosting(
        id=new_id(),
        owner_id=owner_id,
        source_id=SUPPLIED_SOURCE,
        # The id it was given when supplied, so an analysis saved of it before is this posting's (Codex review F2).
        posting_id=content.posting_id or new_id(),
        source_access=job_texts.USER_PROVIDED,
        facts={
            "kind": "supplied",
            "source_name": PASTED_SOURCE_NAME,
            "evidence": EvidenceLevel.USER_PROVIDED.value,
            "title": content.title,
            "company": content.company,
            "cities": [],
            "places_stated": [],
            "job_type": None,
            "type_stated": None,
            "posted_on": None,
            "deadlines": [],
            "url": None,
            "apply_url": None,
            "read_at": None,
            "via": {
                "kind": "pasted",
                "provided_at": _iso(content.provided_at),
                "text_sha256": content.digest,
                "link": None if content.link is None else content.link.text,
            },
        },
        search_id=PASTED_SEARCH,
        decision=None,
        reasons=[],
        note=None,
        decided_at=None,
        revision=1,
        kept_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    db.flush()
    job_texts.keep(db, owner_id, row.id, content.text, content.digest, now)
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_kept", now)
    db.flush()
    bind(row.id)

    return row


# --- reading -------------------------------------------------------------------------------------------------------------


def postings_in_use(db: Session, owner_id: str) -> list[JobPosting]:
    return list(
        db.scalars(
            select(JobPosting)
            .where(JobPosting.owner_id == owner_id, JobPosting.deleted_at.is_(None))
            .order_by(JobPosting.updated_at.desc(), JobPosting.id)
        )
    )


def searches_in_use(db: Session, owner_id: str) -> list[JobSearch]:
    return list(
        db.scalars(
            select(JobSearch)
            .where(JobSearch.owner_id == owner_id, JobSearch.deleted_at.is_(None))
            .order_by(JobSearch.kept_at.desc(), JobSearch.id)
        )
    )


def checks_of(db: Session, record: JobPosting) -> list[JobPostingCheck]:
    """Its checks, the latest answer first: each with the time its source answered."""
    return list(
        db.scalars(
            select(JobPostingCheck)
            .where(JobPostingCheck.posting_record_id == record.id, JobPostingCheck.owner_id == record.owner_id)
            .order_by(JobPostingCheck.checked_at.desc(), JobPostingCheck.id)
        )
    )


def feedback_of(db: Session, record: JobPosting) -> list[JobFeedback]:
    return list(
        db.scalars(
            select(JobFeedback)
            .where(JobFeedback.posting_record_id == record.id, JobFeedback.owner_id == record.owner_id)
            .order_by(JobFeedback.created_at, JobFeedback.id)
        )
    )


def check_states(db: Session, records) -> dict[str, CheckState]:
    """Each record's checks in short, by record id: the latest its source answered, and the latest without an answer."""
    return {record.id: check_state(checks_of(db, record)) for record in records}


def kept_states(db: Session, owner_id: str, refs, now: datetime) -> dict[tuple[str, str], JobPosting]:
    """The postings in use among these refs, by (source id, posting id): what the result page shows as kept. A current
    recommendation whose time has come is gone already, and shown as none (U3-D4b-2)."""
    kept = {}

    for ref in refs:
        row = posting_in_use(db, owner_id, ref.source_id, ref.posting_id)

        if row is not None and not job_recommendations.lapsed(db, row, now):
            kept[(ref.source_id, ref.posting_id)] = row

    return kept


def held_states(db: Session, rows, now: datetime) -> dict[str, tuple[RecState, JobRecommendation]]:
    """For each of these postings held as a current recommendation, by record id: CURRENT or CLOSED, and its row."""
    held = {}

    for row in rows:
        marker = job_recommendations.marker_of(db, row.id)

        if marker is not None:
            held[row.id] = (job_recommendations.state_of(db, row, now), marker)

    return held


def postings_by_state(db: Session, owner_id: str, now: datetime):
    """The postings in use, apart: 我的岗位, current recommendations and closed ones (each with its row in
    job_recommendations). Those whose time has come are shown nowhere (U3-D4b-2)."""
    mine, current, closed = [], [], []

    for row in postings_in_use(db, owner_id):
        marker = job_recommendations.marker_of(db, row.id)

        if marker is None:
            mine.append(row)
        elif now < marker.until:
            (closed if job_recommendations.closed(db, row) else current).append((row, marker))

    return mine, current, closed


def kept_search(db: Session, owner_id: str, search_id: str) -> JobSearch | None:
    return search_in_use(db, owner_id, search_id)


def skipped_refs(db: Session, owner_id: str, source_ids) -> tuple[PostingRef, ...]:
    """The postings this user skipped on these sources (U3-D3c), each once and in order: kept postings in use whose
    decision is a skip. One undone, changed to another decision or in the recycle bin is not among them; one restored
    is again. By source and id only: never a reason or a note. The query lives in app.library.lifecycle (U3-D4c),
    shared with 每日查新."""
    pairs = skipped_postings(db, owner_id, source_ids, (access.value for access in KEEPABLE_ACCESS))
    refs = {PostingRef(source_id, posting_id) for source_id, posting_id in pairs}

    return tuple(sorted(refs, key=lambda ref: (ref.source_id, ref.posting_id)))


def analyses_in_use(db: Session, owner_id: str) -> list[JobAnalysis]:
    return list(
        db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.owner_id == owner_id, JobAnalysis.deleted_at.is_(None))
            .order_by(JobAnalysis.saved_at.desc(), JobAnalysis.id)
        )
    )


def kept_analyses(db: Session, owner_id: str, search_id: str, now: datetime) -> dict[tuple[str, str], JobAnalysis]:
    """The analyses kept from this search and not deleted for good, by (source id, posting id): in use or in the bin."""
    rows = db.scalars(select(JobAnalysis).where(JobAnalysis.owner_id == owner_id, JobAnalysis.search_id == search_id))

    return {(row.source_id, row.posting_id): row for row in rows if not is_expired(row, now)}


def analyses_of_posting(db: Session, owner_id: str, source_id: str, posting_id: str) -> list[JobAnalysis]:
    """The analyses in use of one posting, from any search, newest first."""
    return list(
        db.scalars(
            select(JobAnalysis)
            .where(
                JobAnalysis.owner_id == owner_id,
                JobAnalysis.source_id == source_id,
                JobAnalysis.posting_id == posting_id,
                JobAnalysis.deleted_at.is_(None),
            )
            .order_by(JobAnalysis.saved_at.desc(), JobAnalysis.id)
        )
    )


def analyses_of_search(db: Session, owner_id: str, search_id: str) -> list[JobAnalysis]:
    return list(
        db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.owner_id == owner_id, JobAnalysis.search_id == search_id, JobAnalysis.deleted_at.is_(None))
            .order_by(JobAnalysis.saved_at, JobAnalysis.id)
        )
    )


def analysis_feedback_of(db: Session, record: JobAnalysis) -> list[JobAnalysisFeedback]:
    return list(
        db.scalars(
            select(JobAnalysisFeedback)
            .where(JobAnalysisFeedback.analysis_id == record.id, JobAnalysisFeedback.owner_id == record.owner_id)
            .order_by(JobAnalysisFeedback.created_at, JobAnalysisFeedback.id)
        )
    )


@dataclass(frozen=True)
class BasisNow:
    """How what a kept analysis rests on stands now: each source's state by (kind, item id), and the worst."""

    states: dict
    worst: SourceState


def basis_now(db: Session, owner_id: str, record: JobAnalysis, now: datetime) -> BasisNow:
    states = {
        (row.kind, row.item_id): state_of(db, owner_id, row.kind, row.item_id, row.version_id, row.fields or [], now)
        for row in analysis_sources(db, owner_id, record.id)
    }

    return BasisNow(states, worst(states.values()))


@dataclass(frozen=True)
class ShownAnalysis:
    """A kept analysis as its result page showed it, rebuilt from what was kept for the same page parts: never read
    again from the experiences as they are now."""

    findings: tuple[RequirementFinding, ...]
    preferences: str
    preference_findings: tuple[PreferenceFinding, ...]
    # What was kept of the 岗位要求依据覆盖 count (analysis_body); None for a record kept without it.
    display_metrics: dict | None = None


def shown_analysis(content: dict) -> ShownAnalysis:
    return ShownAnalysis(
        display_metrics=content.get("display_metrics"),
        findings=tuple(
            RequirementFinding(
                requirement_id=finding["requirement_id"],
                group=RequirementGroup(finding["group"]),
                text=finding["text"],
                job_posting_quote=finding["job_posting_quote"],
                conclusion=finding["conclusion"],
                citations=tuple(
                    EvidenceCitation(
                        item_id=citation["item_id"],
                        version_id=citation["version_id"],
                        category=citation["category"],
                        field=citation["field"],
                        quote=citation["quote"],
                        basis=CitationBasis(citation["basis"]),
                        context=tuple(citation["context"]),
                        flags=tuple(citation["flags"]),
                    )
                    for citation in finding["citations"]
                ),
                reasons=tuple(finding["reasons"]),
                # Not in what version 1 kept: empty, as that page showed it.
                listed=tuple(finding.get("listed", ())),
                listed_mode=finding.get("listed_mode", ""),
                named=tuple(finding.get("named", ())),
                condition=finding.get("condition", ""),
                condition_state=finding.get("condition_state", ""),
                condition_fact=finding.get("condition_fact", ""),
            )
            for finding in content["findings"]
        ),
        preferences=content["preferences"],
        preference_findings=tuple(
            PreferenceFinding(
                preference_id=finding["preference_id"],
                field=finding["field"],
                text=finding["text"],
                conclusion=finding["conclusion"],
                job_quotes=tuple(finding["job_quotes"]),
                reasons=tuple(finding["reasons"]),
                firm=finding["firm"],
            )
            for finding in content["preference_findings"]
        ),
    )


def analysis_parts(content: dict) -> list[tuple[str, str]]:
    """Each part of a kept analysis the user may say is not right, as (form value, words): the category, each
    requirement, each experience quote, each preference compared."""
    parts = [("category", t("job.recs.17"))]

    for finding in content["findings"]:
        parts.append((f"requirement:{finding['requirement_id']}", t("job.recs.18", p0=_short(finding['text']))))

        for index, citation in enumerate(finding["citations"]):
            parts.append((f"citation:{finding['requirement_id']}:{index}", t("job.recs.19", p0=_short(citation['quote']))))

    for finding in content["preference_findings"]:
        parts.append((f"preference:{finding['preference_id']}", t("job.recs.20", p0=_short(finding['text']))))

    return parts


def checked_part(content: dict, part: str) -> tuple[str, str | None]:
    """(target, target_ref) of a part the form names, only one this analysis has."""
    if part not in dict(analysis_parts(content)):
        raise InputRejected(t("job.recs.21"))

    target, _, target_ref = part.partition(":")

    if target not in dict(ANALYSIS_TARGETS):
        raise InputRejected(t("job.recs.21"))

    return target, target_ref or None


# --- checking what a form says --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckedDecision:
    decision: JobDecision
    reasons: tuple[str, ...]
    note: str | None


@dataclass(frozen=True)
class CheckedFeedback:
    field: str
    reason: str
    note: str | None


def checked_decision(decision: str, reasons: list[str], note: str) -> CheckedDecision:
    try:
        chosen = JobDecision(decision)
    except ValueError:
        raise InputRejected(t("job.recs.22")) from None

    allowed = [code for code, _ in REASONS[chosen]]
    picked = []

    for code in reasons:
        if code not in allowed:
            raise InputRejected(t("job.recs.23"))

        if code not in picked:
            picked.append(code)

    return CheckedDecision(chosen, tuple(sorted(picked, key=allowed.index)), _note(note))


def checked_feedback(field: str, reason: str, note: str) -> CheckedFeedback:
    if field not in dict(FEEDBACK_FIELDS):
        raise InputRejected(t("job.recs.24"))

    if reason not in dict(FEEDBACK_REASONS):
        raise InputRejected(t("job.recs.9"))

    return CheckedFeedback(field, reason, _note(note))


def seen_from(record_id: str, revision: str) -> Seen:
    """What a form says its page saw: both empty for none, else the record's id and revision."""
    record_id, revision = (record_id or "").strip(), (revision or "").strip()

    if not record_id and not revision:
        return Seen()

    if not record_id or not revision.isdigit():
        raise InputRejected(t("job.recs.25"))

    return Seen(record_id, int(revision))


# --- inner workings ----------------------------------------------------------------------------------------------------------


def _check_seen(row, seen: Seen) -> None:
    if row is None:
        if seen.record_id is not None:
            # The record the page showed is gone since: never make another one in its place unseen.
            raise RecordConflict(seen.record_id)

        return

    if seen.record_id != row.id or seen.revision != row.revision:
        raise RecordConflict(row.id)


def _check_access(row: JobPosting) -> None:
    if row.source_access not in KEEPABLE_ACCESS:
        raise KeepRefused()


def _check_managed(row: JobPosting) -> None:
    if row.source_access not in MANAGED_ACCESS:
        raise KeepRefused()


def _check_analysis_access(row: JobAnalysis) -> None:
    if row.source_access not in ANALYSIS_KEEPABLE_ACCESS:
        raise KeepRefused(NO_ANALYSIS_KEEP)


def _analysis_basis(db: Session, owner_id: str, task, item: Result, now: datetime) -> list[tuple[str, str, str, list[str]]]:
    """What a result's analysis rests on, as rows to keep: each experience version it used, and the saved version of
    the preferences reading as what was sent. Only while all of it is as the search had it: an experience changed,
    re-confirmed or deleted since, a quote or what it was read with no longer in its version, preferences changed
    since, or no saved version of them reading as sent, keeps nothing (KeepRefused)."""
    sent = {preference.field: preference.text for preference in task.confirmation.preferences}

    return _basis_rows(db, owner_id, item.analysis, sent, now, MATERIAL_CHANGED)


def _basis_rows(db: Session, owner_id: str, analysis, sent: dict, now: datetime, changed) -> list[tuple[str, str, str, list[str]]]:
    """The rows an analysis rests on (_analysis_basis), for a search's analysis or a standalone one (plan 5.3): changed
    is what is said when any of it is no longer as the analysis had it."""
    rows = []

    for basis in analysis.sources:
        if state_of(db, owner_id, SourceKind.EXPERIENCE, basis.item_id, basis.version_id, basis.fields, now) is not SourceState.CURRENT:
            raise KeepRefused(changed)

        rows.append((SourceKind.EXPERIENCE.value, basis.item_id, basis.version_id, list(basis.fields)))

    used = {basis.version_id: basis for basis in analysis.sources}

    for finding in analysis.findings:
        for citation in finding.citations:
            basis = used.get(citation.version_id)
            version = None if basis is None else db.scalar(
                select(ExperienceVersion).where(
                    ExperienceVersion.id == citation.version_id,
                    ExperienceVersion.item_id == citation.item_id,
                    ExperienceVersion.owner_id == owner_id,
                )
            )
            text = _text(version.content if version is not None else None, citation.field)

            if version is None or not _stands_in(citation.quote, text) or not all(_stands_in(passage, text) for passage in citation.context):
                raise KeepRefused(changed)

    if sent:
        saved = current_preferences(db, owner_id)

        if any(saved.get(name) != text for name, text in sent.items()):
            raise KeepRefused(changed)

        found = _preference_version(db, owner_id, sent)

        if found is None:
            raise KeepRefused(NO_PREFERENCE_VERSION)

        rows.append((SourceKind.PREFERENCES.value, found[0], found[1], sorted(sent)))

    return rows


def _preference_version(db: Session, owner_id: str, sent: dict[str, str]) -> tuple[str, str] | None:
    """The preferences in use and their newest saved version not deleted whose fields read as sent (the rule of
    U3-C pre-check 16.2: not the newest version as such, which may have been deleted on its own)."""
    record = db.scalar(
        select(ExperienceItem).where(
            ExperienceItem.owner_id == owner_id,
            ExperienceItem.category == PREFERENCES,
            ExperienceItem.deleted_at.is_(None),
        )
    )

    if record is None:
        return None

    versions = db.scalars(
        select(ExperienceVersion)
        .where(
            ExperienceVersion.item_id == record.id,
            ExperienceVersion.owner_id == owner_id,
            ExperienceVersion.kind == VersionKind.SAVED,
        )
        .order_by(ExperienceVersion.number.desc())
    )

    for version in versions:
        if version.deleted_at is None and all(_text(version.content, name) == text for name, text in sent.items()):
            return record.id, version.id

    return None


def _text(content, name: str) -> str:
    value = content.get(name, "") if isinstance(content, dict) else ""

    return value if isinstance(value, str) else ""


def _stands_in(quote: str, text: str) -> bool:
    """Whether the words stand in the text; only whitespace may differ (as the analysis reads its quotes)."""
    words = quote.split()

    return bool(words) and re.search(r"\s+".join(re.escape(word) for word in words), text) is not None


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())

    return text if len(text) <= limit else text[: limit - 1] + "…"


def _new_row(db: Session, owner_id: str, found: Found, now: datetime) -> JobPosting:
    ref = found.item.ref
    row = JobPosting(
        id=new_id(),
        owner_id=owner_id,
        source_id=ref.source_id,
        posting_id=ref.posting_id,
        source_access=found.access,
        facts=posting_facts(found),
        search_id=found.search_id,
        decision=None,
        reasons=[],
        note=None,
        decided_at=None,
        revision=1,
        kept_at=now,
        updated_at=now,
        deleted_at=None,
        purge_after=None,
        deletion_id=None,
    )
    db.add(row)
    db.flush()

    return row


def _add_to_mine(db: Session, owner_id: str, row: JobPosting, now: datetime) -> bool:
    """A current recommendation the user keeps, decides on or says a fact of is not right is added to 我的岗位 by that
    very write (U3-D4b-2): its row in job_recommendations goes, kept_at becomes now (when the user kept it) and its
    revision goes up, so a check begun on it before writes nothing. One whose time has come never is
    (RecommendationLapsed). False for a posting in 我的岗位 already."""
    marker = job_recommendations.marker_of(db, row.id)

    if marker is None:
        return False

    if now >= marker.until:
        raise RecommendationLapsed()

    db.execute(delete(JobRecommendation).where(JobRecommendation.posting_record_id == row.id))
    row.kept_at = now
    _touch(row, now)
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_kept", now)

    return True


def _set_decision(db: Session, owner_id: str, row: JobPosting, checked: CheckedDecision, now: datetime) -> None:
    row.decision = checked.decision.value
    row.reasons = list(checked.reasons)
    row.note = checked.note
    row.decided_at = now
    _touch(row, now)
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_posting_decided", now)


def _add_feedback(db: Session, owner_id: str, row: JobPosting, search_id: str, checked: CheckedFeedback, now: datetime) -> None:
    db.add(
        JobFeedback(
            id=new_id(),
            owner_id=owner_id,
            posting_record_id=row.id,
            search_id=search_id,
            field=checked.field,
            reason=checked.reason,
            note=checked.note,
            created_at=now,
        )
    )
    _touch(row, now)
    record_event(db, owner_id, row.id, TargetType.JOB_POSTING, row.id, "job_feedback_added", now)


def _add_checks(db: Session, owner_id: str, row: JobPosting, found: Found, now: datetime) -> bool:
    """This search's checks of it, each once: the last one its source answered, with what it answered then, and the
    latest without an answer, with the time it was tried. True if either was new."""
    item = found.item
    checks = []

    if item.checked_at is not None:
        # An answer the search kept no status for stays unknown: never taken for open.
        checks.append((item.checked_at, ANSWER_WORDS.get(item.answered_status, CHECK_UNKNOWN)))

    if item.check_failed_at is not None:
        checks.append((item.check_failed_at, CHECK_UNANSWERED))

    added = False

    for at, status in checks:
        exists = db.scalar(
            select(JobPostingCheck.id).where(
                JobPostingCheck.posting_record_id == row.id,
                JobPostingCheck.search_id == found.search_id,
                JobPostingCheck.checked_at == at,
            )
        )

        if exists is not None:
            continue

        db.add(
            JobPostingCheck(
                id=new_id(),
                owner_id=owner_id,
                posting_record_id=row.id,
                search_id=found.search_id,
                status=status,
                checked_at=at,
                recorded_at=now,
            )
        )
        added = True

    return added


def _touch(row, now: datetime) -> None:
    row.revision += 1
    row.updated_at = now


def _note(note: str) -> str | None:
    text = " ".join((note or "").split())

    if len(text) > NOTE_CHARS:
        raise InputRejected(t("job.recs.26", p0=NOTE_CHARS, p1=len(text)))

    return text or None


def _day(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
