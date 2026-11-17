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
        "# 岗位记录",
        "",
        f"导出时间：{format_local(now)}。这里是你在找岗位结果里自己保存的岗位和查找摘要；没有保存的结果不会出现在这里。",
        "岗位的事实是保存时那次查找里看到的公开信息，不是最新情况；“来源核对”是来源回答的时间，和保存时间分开写。",
        "分析是保存时那次查找按你当时的经历得出的，引文和分析时读的那句话都按保存时的样子写出，不按现在的经历重新解释。",
        "",
    ]

    if content.postings:
        lines += ["## 我的岗位", ""]

        for number, entry in enumerate(content.postings, start=1):
            lines += _posting_lines(number, entry)

    if content.recommendations:
        lines += [
            "## 当前推荐（临时留存跟踪）",
            "",
            "按你在查找时的勾选，系统临时留存的推荐：到期自动删除，还没加入我的岗位。",
            "",
        ]

        for number, entry in enumerate(content.recommendations, start=1):
            lines += _posting_lines(number, entry)

    if content.analyses:
        lines += ["## 已保存的分析", ""]

        for number, entry in enumerate(content.analyses, start=1):
            lines += _analysis_lines(number, entry)

    if content.searches:
        lines += ["## 查找摘要", ""]

        for number, entry in enumerate(content.searches, start=1):
            lines += _search_lines(number, entry)

    if content.watches:
        lines += [
            "## 每日查新",
            "",
            f"按你开启的每日查新，系统自动保存的：见过的岗位编号，以及新增岗位的公开信息（从第一次读到起保留 {job_watches.KEEP_NEW_DAYS} 天）。"
            "新增岗位没有分析，也没有核对招聘状态。",
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
        f"### {number}. 每日查新（开启于 {_local(entry['started_at'])}）",
        "",
        f"- 查找摘要：{entry['summary_record']}",
        f"- 基准：{'建立于 ' + _local(entry['baseline_at']) if entry['baseline_at'] else '还没建立'}",
        f"- 上次查新：{_local(entry['last_run']['started_at']) if entry['last_run']['started_at'] else '还没有运行过'}"
        + (f"，结果 {entry['last_run']['outcome']}" if entry["last_run"]["outcome"] else ""),
        f"- 见过的岗位：{len(entry['seen'])} 个",
        f"- 新增岗位：{len(entry['new_postings'])} 个",
    ]

    for item in entry["new_postings"]:
        facts = item["facts"]
        lines.append(
            f"  - {facts.get('title')}（{facts.get('company')}，{'、'.join(facts.get('cities') or []) or '地点未写'}）："
            f"第一次读到 {_local(item['found_at'])}，保留到 {_local(item['until'])}"
            + ("；来自没读完的列表，不能确定是新发布的" if item["list_read_in_part"] is not None else "")
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
        state = "已关闭的推荐（来源已明确关闭，不再跟踪）" if held["state"] == "closed" else "当前推荐（临时留存跟踪）"

        return f"- 状态：{state}，还没加入我的岗位，到 {_local(held['until'])} 自动删除"

    return f"- 状态：{'在用' if entry['kept_as'] == IN_USE else '在回收站，恢复截止 ' + _local(entry['restore_deadline'])}"


def _posting_lines(number: int, entry: dict) -> list[str]:
    facts = entry["facts"]
    supplied = entry.get("supplied") or {}

    if supplied.get("kind") == "pasted":
        # The user's own (U3-D4d): no source, no place or type read, and its status never checked.
        lines = [
            f"### {number}. {facts['title']}（{facts['company']}）",
            "",
            _status_line(entry),
            f"- 来源：你提供的原文（{EVIDENCE_WORDS['user_provided']}）。职位名和公司是你填写的；系统没有打开链接，也没有向任何来源核实，"
            "不能核对它的招聘状态",
            f"- 你提供的链接：{supplied['link']}（只是文字，系统没有打开过）" if supplied.get("link") else "- 你提供的链接：没有",
            f"- 保存时间：{_local(entry['kept_at'])}",
        ]
    else:
        lines = [
            f"### {number}. {facts['title']}（{facts['company']}）",
            "",
            _status_line(entry),
            f"- 来源：{facts['source_name']}，岗位编号 {entry['posting_id']}（{EVIDENCE_WORDS.get(facts['evidence'], facts['evidence'])}）",
            f"- 地点：{'、'.join(facts['cities']) or '未认出'}；来源原文：{'、'.join(facts['places_stated']) or '没有写'}",
            f"- 类型：{facts['job_type'] or '未认出'}；来源原文：{facts['type_stated'] or '没有写'}",
            f"- {'临时留存时间' if entry.get('held') else '保存时间'}：{_local(entry['kept_at'])}（不是来源核对的时间）",
        ]

        if supplied.get("kind") == "link":
            lines.append(f"- 来自你补充的链接：{_local(supplied.get('read_at'))} 读到（链接只用来认出来源和岗位编号，由来源自己的接口读取）")

    # What the checks kept say, never "still open" unless the latest answered so (U3-D3a F1).
    lines.append(f"- 来源核对：{entry['check_state']}（共 {len(entry['checks']) + len(entry['rechecks'])} 条核对记录）")
    # A real source's posting (U3-D6): what its status can and cannot show, by the latest deadline read of it.
    read = [check["facts"] for check in entry["rechecks"] if check["facts"]]
    note = closure_note(entry["source_access"], (read[-1] if read else facts).get("deadline"))

    if note:
        lines.append(f"- 核对说明：{note}")

    for check in entry["rechecks"]:
        said = check_words(SimpleNamespace(status=check["status"], reason=check["reason"], evidence=check["evidence"]))
        at = check["source_answered_at"] or check["unanswered_at"]
        line = f"- {TRIGGER_WORDS.get(check['trigger'], '核对')}（{'来源回答' if check['source_answered_at'] else '尝试核对'} {_local(at)}）：{said}"
        changes = change_lines(check["changes"])
        lines.append(line + (f"；和上一次读到的版本相比：{'；'.join(changes)}" if changes else ""))

    if entry["decision"] is not None:
        reasons = reason_labels(entry["decision"], entry["reasons"])
        lines.append(f"- 我的决定：{decision_label(entry['decision'])}" + (f"；原因：{'、'.join(reasons)}" if reasons else ""))

        if entry["note"]:
            lines.append(f"- 备注：{entry['note']}")

    for item in entry["feedback"]:
        lines.append(f"- 指出不准：{feedback_label(item['field'], item['reason'])}" + (f"（{item['note']}）" if item["note"] else ""))

    if entry.get("user_text"):
        # As it was pasted, never read as Markdown: its fence is longer than any run of backticks in it (U3-D4d).
        text = entry["user_text"]["text"]
        fence = _fence(text)
        lines += [f"- 你提供的原文（{entry['user_text']['chars']} 字，保存前你确认过的样子）：", "", fence, text, fence]

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
        f"### {number}. {facts['title']}（{facts['company']}）",
        "",
        f"- 状态：{'在用' if entry['kept_as'] == IN_USE else '在回收站，恢复截止 ' + _local(entry['restore_deadline'])}",
        f"- 分类：{CATEGORY_WORDS.get(content['category'], content['category'])}",
        f"- 依据：{entry['basis']}",
        f"- 来源：{facts['source_name']}，岗位编号 {entry['posting_id']}；来源核对：{CHECK_WORDS.get(facts['check']['status'], facts['check']['status'])}",
        f"- 分析时间：{_local(entry['analysed_at'])}；保存时间：{_local(entry['saved_at'])}",
    ]

    lines += [f"- 申请前先确认：{line}" for line in content["first_checks"]]
    lines += [f"- 说明：{note}" for note in content["notes"]]

    for finding in content["findings"]:
        # The condition its posting quote lays down and the items its words list (U3-D6 R4): absent before then.
        condition, state, fact = finding.get("condition", ""), finding.get("condition_state", ""), finding.get("condition_fact", "")
        lines.append(
            f"- 招聘条件：{requirement_text(finding['text'], condition, state)}（{group_heading(finding['group'])}；岗位原文：「{finding['job_posting_quote']}」）"
            f"：{conclusion_text(finding['group'], finding['conclusion'])}{_reasons(finding['reasons'])}"
        )

        if fact:
            lines.append(f"  - 岗位写明的事实：「{fact}」")

        if finding.get("listed"):
            shown, unshown = listed_parts(finding["listed"], finding.get("named", []))
            lines.append(
                f"  - 这一条列出的几项（{LISTED_MODES[finding['listed_mode']]}）：引文写到的：{'、'.join(shown) or '没有'}；"
                f"还没有依据的：{'、'.join(unshown) or '没有'}"
            )

        for citation in finding["citations"]:
            lines.append(f"  - 经历引文（{_cited_from(citation, versions)}）：「{citation['quote']}」")

            if citation["context"] and citation["context"] != [citation["quote"]]:
                lines.append(f"    - 分析时读的那句话：「{'／'.join(citation['context'])}」")

    if content["preferences"] != "compared":
        lines.append("- 偏好：本次未参考偏好")

    for finding in content["preference_findings"]:
        lines.append(
            f"- 偏好：「{finding['text']}」（{preference_name(finding['field'])}）"
            f"：{preference_text(finding['conclusion'])}{_reasons(finding['reasons'])}"
        )
        lines += [f"  - 岗位原文：「{quote}」" for quote in finding["job_quotes"]]

    for item in entry["feedback"]:
        lines.append(f"- 指出不准：{analysis_feedback_label(item['target'], item['reason'])}" + (f"（{item['note']}）" if item["note"] else ""))

    return lines + [""]


def preference_name(field: str) -> str:
    """A preferences field as the pages name it: its topic, and its stance where it has one (what
    app.experience_analysis.PREFERENCE_TOPICS makes of the same library fields; a test keeps the two the same)."""
    for _, label, fields in PREFERENCE_GROUPS:
        for item in fields:
            if item.name == field:
                return f"{label}（{item.label}）"

    for item in PREFERENCE_SINGLES:
        if item.name == field:
            return item.label

    return field


def _reasons(reasons) -> str:
    words = analysis_reasons(reasons)

    return f"；理由：{'；'.join(words)}" if words else ""


def _cited_from(citation: dict, versions: dict) -> str:
    """Where a quote stands, as kept: the record by its id, its kind, the confirmed version used, the field, and what
    the quote shows (its basis)."""
    number = versions.get(citation["item_id"])
    version = f"确认版 {number}" if number is not None else "版本未记录"

    return (
        f"经历 {citation['item_id'][:8]}，{CATEGORY_LABELS.get(citation['category'], citation['category'])}，{version}，"
        f"{field_label(citation['category'], citation['field'])}；{BASIS_LABELS.get(citation['basis'], citation['basis'])}"
    )


def _search_lines(number: int, entry: dict) -> list[str]:
    summary = entry["summary"]
    counts = summary["counts"]

    return [
        f"### {number}. 查找 {entry['search']}",
        "",
        f"- 状态：{'在用' if entry['kept_as'] == IN_USE else '在回收站，恢复截止 ' + _local(entry['restore_deadline'])}",
        f"- 保存时间：{_local(entry['kept_at'])}",
        f"- 要找的岗位：{'、'.join(summary['scope']['targets'])}",
        f"- 来源：{'、'.join(source['name'] for source in summary['sources'])}",
        f"- 完整分析结果 {counts['results']} 个，初筛候选 {counts['leads']} 个；检索 {len(summary['searches'])} 次"
        # Kept since summary version 3 (U3-D3c); an older summary says nothing of skips, and none is made up for it.
        + (f"；你跳过的岗位又遇到 {counts['skipped']} 个，没有列出" if counts.get("skipped") else ""),
        "",
    ]


def _iso(value: datetime | None) -> str | None:
    """As the rest of the export writes times: UTC, to the second."""
    return None if value is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value: str | None) -> str:
    return "未知" if value is None else format_local(datetime.fromisoformat(value))
