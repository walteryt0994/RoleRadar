"""The postings, search summaries and analyses the user kept (U3-D3a, D3b), as an export package has them.

Exported: the ones in use and the ones in the recycle bin still before their restore deadline, each saying how it is
kept. One past its deadline, or asked to be deleted for good, is never exported, nor are its checks or feedback; nor
is an analysis deleted along with an experience or a version the user then deleted for good. A kept analysis is
exported whole as it was kept, its quotes and what each was read with included, with how what it rests on stands now.
A posting's facts are the whitelisted ones kept (app.discovery.records.posting_facts); its checks keep the time its
source answered apart from when the user kept it, and its decision and feedback are the user's own codes and words.
Current recommendations held for tracking (U3-D4b-2) are exported apart (recommendations), never among 我的岗位, each
with when it was held and until when; one whose time has come is never exported. Each 每日查新 (U3-D4c) is exported
with its own state and last run, every posting it has seen by source and id with when, and its new postings whose
time has not come, by the kept postings' whitelist (watches). A posting the user added themselves (U3-D4d) says so
(supplied: read from a link they gave, or pasted by them, with the link as they confirmed it); one they pasted has its
text with it (user_text), as they confirmed it before saving: the only text of a posting ever exported.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.library import job_recommendations, job_texts, job_watches
from app.library.job_words import source_label, type_word
from app.library.job_choices import (
    CATEGORY_WORDS,
    CHECK_UNANSWERED,
    CHECK_WORDS,
    EVIDENCE_WORDS,
    TRIGGER_WORDS,
    analysis_feedback_label,
    basis_text,
    change_lines,
    check_words,
    closure_note,
    decision_label,
    feedback_label,
    reason_labels,
    summarize,
    summary_text,
)
from app.library.models import (
    JobAnalysis,
    JobAnalysisFeedback,
    JobFeedback,
    JobPosting,
    JobPostingCheck,
    JobPostingRecheck,
    JobRecommendation,
    JobSearch,
    JobUpdateRun,
    JobWatch,
    JobWatchSeen,
)
from app.library.references import analysis_sources, state_of, worst
from app.analysis_words import (
    BASIS_LABELS,
    LISTED_MODES,
    conclusion_text,
    group_heading,
    listed_parts,
    preference_text,
    requirement_text,
)
# The analysis's reasons, apart from a decision's (job_choices.reason_labels).
from app.analysis_words import reason_labels as analysis_reasons
from app.library.content import CATEGORY_LABELS, PREFERENCE_GROUPS, PREFERENCE_SINGLES
from app.library.presentation import field_label, format_local
from app.library.service import is_expired
from app.i18n import lazy, list_join, t

IN_USE, IN_BIN = "in_use", "recycle_bin"
# How a posting the user added themselves came (U3-D4d): read from a link they gave, or pasted by them.
SUPPLIED_KINDS = ("link", "pasted")


@dataclass
class JobExport:
    postings: list[dict] = field(default_factory=list)
    searches: list[dict] = field(default_factory=list)
    analyses: list[dict] = field(default_factory=list)
    # 临时留存跟踪 (U3-D4b-2): never among 我的岗位, nor counted with them.
    recommendations: list[dict] = field(default_factory=list)
    # 每日查新 (U3-D4c): never among 我的岗位 either.
    watches: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "job_postings": sum(entry["kept_as"] == IN_USE for entry in self.postings),
            "job_recommendations": len(self.recommendations),
            "job_searches": sum(entry["kept_as"] == IN_USE for entry in self.searches),
            "job_analyses": sum(entry["kept_as"] == IN_USE for entry in self.analyses),
            "job_recycle_bin": sum(entry["kept_as"] == IN_BIN for entry in self.postings + self.searches + self.analyses),
            "job_watches": len(self.watches),
            "job_watch_postings": sum(len(entry["new_postings"]) for entry in self.watches),
            "job_posting_texts": sum(entry.get("user_text") is not None for entry in self.postings),
        }

    def __bool__(self) -> bool:
        return bool(self.postings or self.searches or self.analyses or self.recommendations or self.watches)


def collect_job_records(db: Session, owner_id: str, now: datetime) -> JobExport:
    held = {marker.posting_record_id: marker for marker in db.scalars(select(JobRecommendation).where(JobRecommendation.owner_id == owner_id))}
    postings = [
        row
        for row in db.scalars(
            select(JobPosting).where(JobPosting.owner_id == owner_id).order_by(JobPosting.kept_at, JobPosting.id)
        )
        if not is_expired(row, now)
    ]
    # A current recommendation is exported apart, and one whose time has come never (U3-D4b-2).
    recommendations = [(row, held[row.id]) for row in postings if row.id in held and now < held[row.id].until]
    postings = [row for row in postings if row.id not in held]
    searches = [
        row
        for row in db.scalars(
            select(JobSearch).where(JobSearch.owner_id == owner_id).order_by(JobSearch.kept_at, JobSearch.id)
        )
        if not is_expired(row, now)
    ]

    analyses = [
        row
        for row in db.scalars(
            select(JobAnalysis).where(JobAnalysis.owner_id == owner_id).order_by(JobAnalysis.saved_at, JobAnalysis.id)
        )
        if not is_expired(row, now)
    ]

    return JobExport(
        postings=[_posting_data(db, row) for row in postings],
        searches=[_search_data(row) for row in searches],
        analyses=[_analysis_data(db, row, now) for row in analyses],
        recommendations=[_held_data(db, row, marker) for row, marker in recommendations],
        watches=[_watch_data(db, watch, now) for watch in job_watches.watches_of(db, owner_id)],
    )


def jobs_markdown(content: JobExport, now: datetime) -> str:
    lines = [
        t("jexp.1"),
        "",
        t("jexp.2", p0=format_local(now)),
        t("jexp.3"),
        t("jexp.4"),
        "",
    ]

    if content.postings:
        lines += [t("jexp.5"), ""]

        for number, entry in enumerate(content.postings, start=1):
            lines += _posting_lines(number, entry)

    if content.recommendations:
        lines += [
            t("jexp.6"),
            "",
            t("jexp.7"),
            "",
        ]

        for number, entry in enumerate(content.recommendations, start=1):
            lines += _posting_lines(number, entry)

    if content.analyses:
        lines += [t("jexp.8"), ""]

        for number, entry in enumerate(content.analyses, start=1):
            lines += _analysis_lines(number, entry)

    if content.searches:
        lines += [t("jexp.9"), ""]

        for number, entry in enumerate(content.searches, start=1):
            lines += _search_lines(number, entry)

    if content.watches:
        lines += [
            t("jexp.10"),
            "",
            t("jexp.11", p0=job_watches.KEEP_NEW_DAYS),
            "",
        ]

        for number, entry in enumerate(content.watches, start=1):
            lines += _watch_lines(number, entry)

    return "\n".join(lines).rstrip() + "\n"


def _watch_data(db: Session, watch: JobWatch, now: datetime) -> dict:
    """A watch as exported (U3-D4c): its state and last run, what it has seen and its new postings whose time has not
    come; never a posting's text."""
    seen = db.scalars(
        select(JobWatchSeen).where(JobWatchSeen.watch_id == watch.id).order_by(JobWatchSeen.first_seen_at, JobWatchSeen.source_id, JobWatchSeen.posting_id)
    )

    return {
        "id": watch.id,
        "summary_record": watch.search_record_id,
        "notice_version": watch.notice_version,
        "started_at": _iso(watch.created_at),
        "baseline_at": _iso(watch.baseline_at),
        "baselined_searches": len(watch.baselined),
        "last_run": {
            "trigger": watch.last_trigger,
            "started_at": _iso(watch.last_started_at),
            "finished_at": _iso(watch.last_finished_at),
            "outcome": watch.last_outcome,
            "reason": watch.last_reason,
            "calls": watch.last_calls,
            "counts": watch.last_counts,
        },
        "completed_at": _iso(watch.completed_at),
        "seen": [
            {"source_id": row.source_id, "posting_id": row.posting_id, "first_seen_at": _iso(row.first_seen_at), "last_seen_at": _iso(row.last_seen_at)}
            for row in seen
        ],
        "new_postings": [
            {
                "source_id": row.source_id,
                "posting_id": row.posting_id,
                "facts": dict(row.facts),
                "screening": dict(row.screening),
                "list_read_in_part": row.partial_read,
                "found_at": _iso(row.found_at),
                "until": _iso(row.until),
            }
            for row in job_watches.new_postings(db, watch.id, now)
        ],
    }


def _watch_lines(number: int, entry: dict) -> list[str]:
    lines = [
        t("jexp.12", p0=number, p1=_local(entry['started_at'])),
        "",
        t("jexp.13", p0=entry['summary_record']),
        t("jexp.14", p0=t("jexp.baseline_at", when=_local(entry['baseline_at'])) if entry['baseline_at'] else t("jexp.no_baseline")),
        t("jexp.15", p0=_local(entry['last_run']['started_at']) if entry['last_run']['started_at'] else t("jexp.never_run"))
        + (t("jexp.16", p0=entry['last_run']['outcome']) if entry["last_run"]["outcome"] else ""),
        t("jexp.17", p0=len(entry['seen'])),
        t("jexp.18", p0=len(entry['new_postings'])),
    ]

    for item in entry["new_postings"]:
        facts = item["facts"]
        lines.append(
            t("jexp.19", p0=facts.get('title'), p1=facts.get('company'), p2=list_join(facts.get('cities') or []) or t("job.place_unstated"), p3=_local(item['found_at']), p4=_local(item['until']))
            + (t("jexp.20") if item["list_read_in_part"] is not None else "")
        )

    return lines + [""]


def _kept(row) -> dict:
    return {
        "kept_as": IN_USE if row.deleted_at is None else IN_BIN,
        "deleted_at": _iso(row.deleted_at),
        "restore_deadline": _iso(row.purge_after),
    }


def _posting_data(db: Session, row: JobPosting) -> dict:
    checks = list(
        db.scalars(
            select(JobPostingCheck)
            .where(JobPostingCheck.posting_record_id == row.id, JobPostingCheck.owner_id == row.owner_id)
            .order_by(JobPostingCheck.checked_at, JobPostingCheck.id)
        )
    )
    feedback = db.scalars(
        select(JobFeedback)
        .where(JobFeedback.posting_record_id == row.id, JobFeedback.owner_id == row.owner_id)
        .order_by(JobFeedback.created_at, JobFeedback.id)
    )
    rechecks = list(
        db.scalars(
            select(JobPostingRecheck)
            .where(JobPostingRecheck.posting_record_id == row.id, JobPostingRecheck.owner_id == row.owner_id)
            .order_by(JobPostingRecheck.checked_at, JobPostingRecheck.recorded_at, JobPostingRecheck.id)
        )
    )
    runs = {check.run_id for check in rechecks}
    triggers = {run.id: run.trigger for run in db.scalars(select(JobUpdateRun).where(JobUpdateRun.id.in_(runs)))} if runs else {}
    # Added by the user themselves (U3-D4d): from no search, so it names none; one they pasted has its text.
    via = (row.facts or {}).get("via")
    supplied = dict(via) if isinstance(via, dict) and via.get("kind") in SUPPLIED_KINDS else None
    kept_text = job_texts.text_of(db, row.owner_id, row.id) if row.source_access == job_texts.USER_PROVIDED else None

    return {
        "id": row.id,
        **_kept(row),
        "source_id": row.source_id,
        "posting_id": row.posting_id,
        "source_access": row.source_access,
        "facts": dict(row.facts),
        "kept_from_search": None if supplied else row.search_id,
        "supplied": supplied,
        "user_text": None
        if kept_text is None
        else {"text": kept_text.text, "text_sha256": kept_text.text_sha256, "chars": len(kept_text.text)},
        "kept_at": _iso(row.kept_at),
        "decision": row.decision,
        "reasons": list(row.reasons),
        "note": row.note,
        "decided_at": _iso(row.decided_at),
        # Each check as it was kept (U3-D3a F1): an answer's time and what it said, or the time a check got no answer.
        "checks": [
            {
                "status": check.status,
                "source_answered_at": None if check.status == CHECK_UNANSWERED else _iso(check.checked_at),
                "unanswered_at": _iso(check.checked_at) if check.status == CHECK_UNANSWERED else None,
                "recorded_at": _iso(check.recorded_at),
                "search": check.search_id,
            }
            for check in checks
        ],
        # The checks made from the record since (U3-D4a): what each found and why, the evidence of a close, the public
        # facts it read and what changed since the version before; never the posting's text, only which version it was.
        "rechecks": [
            {
                "trigger": triggers.get(check.run_id),
                "status": check.status,
                "reason": check.reason,
                "evidence": check.evidence,
                "source_answered_at": None if check.status == CHECK_UNANSWERED else _iso(check.checked_at),
                "unanswered_at": _iso(check.checked_at) if check.status == CHECK_UNANSWERED else None,
                "recorded_at": _iso(check.recorded_at),
                "facts": check.facts,
                "content_sha256": check.content_sha256,
                "changes": list(check.changes),
            }
            for check in rechecks
        ],
        # Both kinds together: the latest attempt, answer and success apart (job_choices.summarize).
        "check_state": summary_text(summarize([*checks, *rechecks]), format_local),
        "feedback": [
            {"field": item.field, "reason": item.reason, "note": item.note, "search": item.search_id, "created_at": _iso(item.created_at)}
            for item in feedback
        ],
    }


def _analysis_data(db: Session, row: JobAnalysis, now: datetime) -> dict:
    sources = analysis_sources(db, row.owner_id, row.id)
    states = [state_of(db, row.owner_id, source.kind, source.item_id, source.version_id, source.fields or [], now) for source in sources]
    feedback = db.scalars(
        select(JobAnalysisFeedback)
        .where(JobAnalysisFeedback.analysis_id == row.id, JobAnalysisFeedback.owner_id == row.owner_id)
        .order_by(JobAnalysisFeedback.created_at, JobAnalysisFeedback.id)
    )

    return {
        "id": row.id,
        **_kept(row),
        "search": row.search_id,
        "source_id": row.source_id,
        "posting_id": row.posting_id,
        "source_access": row.source_access,
        "facts": dict(row.facts),
        "content": dict(row.content),
        "generation": dict(row.generation),
        # Which text of the posting it analysed, never what the text said, and never evidence of anything.
        "content_sha256": row.content_sha256,
        "analysed_at": _iso(row.analysed_at),
        "saved_at": _iso(row.saved_at),
        # What it rests on, as kept, and how each stands as this package is made.
        "sources": [
            {"kind": source.kind, "item_id": source.item_id, "version_id": source.version_id, "fields": list(source.fields or []), "state": state.value}
            for source, state in zip(sources, states)
        ],
        "basis": basis_text(worst(states)),
        "feedback": [
            {"target": item.target, "target_ref": item.target_ref, "reason": item.reason, "note": item.note, "created_at": _iso(item.created_at)}
            for item in feedback
        ],
    }


def _search_data(row: JobSearch) -> dict:
    return {
        "id": row.id,
        **_kept(row),
        "search": row.search_id,
        "kept_at": _iso(row.kept_at),
        "summary": dict(row.summary),
    }


def _held_data(db: Session, row: JobPosting, marker: JobRecommendation) -> dict:
    """A current recommendation as exported (U3-D4b-2): as a kept posting is, and how it is held."""
    entry = _posting_data(db, row)
    entry["held"] = {
        "state": "closed" if job_recommendations.closed(db, row) else "current",
        "held_for_search": marker.search_id,
        "held_at": _iso(marker.recommended_at),
        "until": _iso(marker.until),
    }

    return entry


def _status_line(entry: dict) -> str:
    held = entry.get("held")

    if held is not None:
        state = t("jexp.21") if held["state"] == "closed" else t("jexp.22")

        return t("jexp.23", p0=state, p1=_local(held['until']))

    return t("jexp.24", p0=t("jexp.in_use") if entry['kept_as'] == IN_USE else t("jexp.in_bin_until", when=_local(entry['restore_deadline'])))


def _posting_lines(number: int, entry: dict) -> list[str]:
    facts = entry["facts"]
    supplied = entry.get("supplied") or {}

    if supplied.get("kind") == "pasted":
        # The user's own (U3-D4d): no source, no place or type read, and its status never checked.
        lines = [
            t("jexp.25", p0=number, p1=facts['title'], p2=facts['company']),
            "",
            _status_line(entry),
            t("jexp.26", p0=EVIDENCE_WORDS['user_provided']),
            t("jexp.27", p0=supplied['link']) if supplied.get("link") else t("jexp.28"),
            t("jexp.29", p0=_local(entry['kept_at'])),
        ]
    else:
        lines = [
            t("jexp.25", p0=number, p1=facts['title'], p2=facts['company']),
            "",
            _status_line(entry),
            t("jexp.30", p0=source_label(facts['source_name']), p1=entry['posting_id'], p2=EVIDENCE_WORDS.get(facts['evidence'], facts['evidence'])),
            t("jexp.31", p0=list_join(facts['cities']) or t("job.unrecognised"), p1=list_join(facts['places_stated']) or t("jexp.not_written")),
            t("jexp.32", p0=type_word(facts['job_type']) or t("job.unrecognised"), p1=facts['type_stated'] or t("jexp.not_written")),
            t("jexp.33", p0=t("jexp.held_at") if entry.get('held') else t("jexp.kept_at"), p1=_local(entry['kept_at'])),
        ]

        if supplied.get("kind") == "link":
            lines.append(t("jexp.34", p0=_local(supplied.get('read_at'))))

    # What the checks kept say, never "still open" unless the latest answered so (U3-D3a F1).
    lines.append(t("jexp.35", p0=entry['check_state'], p1=len(entry['checks']) + len(entry['rechecks'])))
    # A real source's posting (U3-D6): what its status can and cannot show, by the latest deadline read of it.
    read = [check["facts"] for check in entry["rechecks"] if check["facts"]]
    note = closure_note(entry["source_access"], (read[-1] if read else facts).get("deadline"))

    if note:
        lines.append(t("jexp.36", p0=note))

    for check in entry["rechecks"]:
        said = check_words(SimpleNamespace(status=check["status"], reason=check["reason"], evidence=check["evidence"]))
        at = check["source_answered_at"] or check["unanswered_at"]
        line = t("jexp.37", p0=TRIGGER_WORDS.get(check['trigger'], t("job.check_word")), p1=t("jexp.answered") if check['source_answered_at'] else t("jexp.tried"), p2=_local(at), p3=said)
        changes = change_lines(check["changes"])
        lines.append(line + (t("jexp.38", p0=list_join(changes, "clauses")) if changes else ""))

    if entry["decision"] is not None:
        reasons = reason_labels(entry["decision"], entry["reasons"])
        lines.append(t("jexp.39", p0=decision_label(entry['decision'])) + (t("jexp.40", p0=list_join(reasons)) if reasons else ""))

        if entry["note"]:
            lines.append(t("jexp.41", p0=entry['note']))

    for item in entry["feedback"]:
        lines.append(t("jexp.42", p0=feedback_label(item['field'], item['reason'])) + (t("jexp.43", p0=item['note']) if item["note"] else ""))

    if entry.get("user_text"):
        # As it was pasted, never read as Markdown: its fence is longer than any run of backticks in it (U3-D4d).
        text = entry["user_text"]["text"]
        fence = _fence(text)
        lines += [t("jexp.44", p0=entry['user_text']['chars']), "", fence, text, fence]

    return lines + [""]


def _fence(text: str) -> str:
    """A code fence longer than the longest run of backticks in the text, so nothing in it ends the block."""
    longest = current = 0

    for character in text:
        current = current + 1 if character == "`" else 0
        longest = max(longest, current)

    return "`" * max(3, longest + 1)


def _analysis_lines(number: int, entry: dict) -> list[str]:
    """A kept analysis as it was kept, readable (U3-D3b F1): every requirement with its group, the posting's words, the
    judgment and the reasons; each experience quote whole, with where it stands (record, confirmed version, field,
    basis) and, when it was read with more, the words read with it then; every preference compared with the user's
    words, the judgment, the posting's words and the reasons. The words are the pages' own (app.analysis_words), and
    nothing is worked out again from the library as it is now."""
    facts, content = entry["facts"], entry["content"]
    versions = {source["item_id"]: source["version_number"] for source in content["sources"]}
    lines = [
        t("jexp.25", p0=number, p1=facts['title'], p2=facts['company']),
        "",
        t("jexp.24", p0=t("jexp.in_use") if entry['kept_as'] == IN_USE else t("jexp.in_bin_until", when=_local(entry['restore_deadline']))),
        t("jexp.45", p0=CATEGORY_WORDS.get(content['category'], content['category'])),
        t("jexp.46", p0=entry['basis']),
        t("jexp.47", p0=source_label(facts['source_name']), p1=entry['posting_id'], p2=CHECK_WORDS.get(facts['check']['status'], facts['check']['status'])),
        t("jexp.48", p0=_local(entry['analysed_at']), p1=_local(entry['saved_at'])),
    ]

    lines += [t("jexp.49", p0=line) for line in content["first_checks"]]
    lines += [t("jexp.50", p0=note) for note in content["notes"]]

    for finding in content["findings"]:
        # The condition its posting quote lays down and the items its words list (U3-D6 R4): absent before then.
        condition, state, fact = finding.get("condition", ""), finding.get("condition_state", ""), finding.get("condition_fact", "")
        lines.append(
            t("jexp.51", p0=requirement_text(finding['text'], condition, state), p1=group_heading(finding['group']), p2=finding['job_posting_quote'], p3=conclusion_text(finding['group'], finding['conclusion']), p4=_reasons(finding['reasons']))
        )

        if fact:
            lines.append(t("jexp.52", p0=fact))

        if finding.get("listed"):
            shown, unshown = listed_parts(finding["listed"], finding.get("named", []))
            lines.append(
                t("jexp.53", p0=LISTED_MODES[finding['listed_mode']], p1=list_join(shown) or t("common.none"), p2=list_join(unshown) or t("common.none"))
            )

        for citation in finding["citations"]:
            lines.append(t("jexp.54", p0=_cited_from(citation, versions), p1=citation['quote']))

            if citation["context"] and citation["context"] != [citation["quote"]]:
                lines.append(t("jexp.55", p0=list_join(citation['context'], "slash")))

    if content["preferences"] != "compared":
        lines.append(t("jexp.56"))

    for finding in content["preference_findings"]:
        lines.append(
            t("jexp.57", p0=finding['text'], p1=preference_name(finding['field']), p2=preference_text(finding['conclusion']), p3=_reasons(finding['reasons']))
        )
        lines += [t("jexp.58", p0=quote) for quote in finding["job_quotes"]]

    for item in entry["feedback"]:
        lines.append(t("jexp.42", p0=analysis_feedback_label(item['target'], item['reason'])) + (t("jexp.43", p0=item['note']) if item["note"] else ""))

    return lines + [""]


def preference_name(field: str) -> str:
    """A preferences field as the pages name it: its topic, and its stance where it has one (what
    app.experience_analysis.PREFERENCE_TOPICS makes of the same library fields; a test keeps the two the same)."""
    for _, label, fields in PREFERENCE_GROUPS:
        for item in fields:
            if item.name == field:
                return t("jexp.59", p0=label, p1=item.label)

    for item in PREFERENCE_SINGLES:
        if item.name == field:
            return item.label

    return field


def _reasons(reasons) -> str:
    words = analysis_reasons(reasons)

    return t("jexp.60", p0=list_join(words, "clauses")) if words else ""


def _cited_from(citation: dict, versions: dict) -> str:
    """Where a quote stands, as kept: the record by its id, its kind, the confirmed version used, the field, and what
    the quote shows (its basis)."""
    number = versions.get(citation["item_id"])
    version = t("jexp.61", p0=number) if number is not None else t("jexp.62")

    return (
        t("jexp.63", p0=citation['item_id'][:8], p1=CATEGORY_LABELS.get(citation['category'], citation['category']), p2=version, p3=field_label(citation['category'], citation['field']), p4=BASIS_LABELS.get(citation['basis'], citation['basis']))
    )


def _search_lines(number: int, entry: dict) -> list[str]:
    summary = entry["summary"]
    counts = summary["counts"]

    return [
        t("jexp.64", p0=number, p1=entry['search']),
        "",
        t("jexp.24", p0=t("jexp.in_use") if entry['kept_as'] == IN_USE else t("jexp.in_bin_until", when=_local(entry['restore_deadline']))),
        t("jexp.29", p0=_local(entry['kept_at'])),
        t("jexp.65", p0=list_join(summary['scope']['targets'])),
        t("jexp.66", p0=list_join(source['name'] for source in summary['sources'])),
        t("jexp.67", p0=counts['results'], p1=counts['leads'], p2=len(summary['searches']))
        # Kept since summary version 3 (U3-D3c); an older summary says nothing of skips, and none is made up for it.
        + (t("jexp.68", p0=counts['skipped']) if counts.get("skipped") else ""),
        "",
    ]


def _iso(value: datetime | None) -> str | None:
    """As the rest of the export writes times: UTC, to the second."""
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value: str | None) -> str:
    return t("jexp.69") if value is None else format_local(datetime.fromisoformat(value))
