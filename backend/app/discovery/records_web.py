"""Pages of 岗位机会 → 岗位记录 (U3-D3a, D3b): keeping a posting, its analysis or a search's summary from a finished
search, and the records kept.

    /discover/tasks/<id>/keep?source=&posting=         one posting of a finished search: keep it, decide, or say a
                                                       fact is not right
    /discover/tasks/<id>/keep                    POST  action keep, decide, feedback, or analysis (keep its analysis)
    /discover/tasks/<id>/summary                 POST  keep the search's summary
    /job-records                                       the postings and summaries kept
    /job-records/postings/<id>                         one kept posting: facts, checks, decision, feedback
    /job-records/postings/<id>/decide            POST  change the decision, or take it back
    /job-records/postings/<id>/feedback          POST  say a fact is not right
    /job-records/postings/<id>/add               POST  加入我的岗位: a current recommendation becomes the user's own (U3-D4b-2)
    /job-records/postings/<id>/untrack           POST  不再跟踪 a current recommendation: removed now (U3-D4b-2)
    /job-records/postings/<id>/delete            GET, POST  to the recycle bin, restorable for RESTORE_WINDOW
    /job-records/postings/<id>/restore           POST
    /job-records/postings/<id>/purge             GET, POST  deleted for good before its deadline
    /job-records/searches/<id>                         one kept summary
    /job-records/searches/<id>/delete|restore|purge    as for a posting
    /job-records/analyses/<id>                         one kept analysis: as its result page showed it, and how what it
                                                       rests on stands now
    /job-records/analyses/<id>/feedback          POST  say a part of it is not right
    /job-records/analyses/<id>/delete|restore|purge    as for a posting; one deleted along with what it rests on comes
                                                       back, or goes, only with that
    /job-records/supply                                补充岗位 (U3-D4d): a link to read, or a posting's text to paste
    /job-records/supply/link                     POST  read the posting a link names, if the app may read its source now
    /job-records/supply/text                     POST  hold a pasted posting, to look at before saving
    /job-records/supply/<token>                        what was read or pasted, as it would be kept
    /job-records/supply/<token>/keep             POST  加入我的岗位
    /job-records/supply/<token>/discard          POST  放弃: dropped from memory now

Every write is the user's, posted with the session's CSRF token from this site (checked_fields), naming what its
page saw (records.Seen). What may be kept is the server's (SourceSet.keep, records.KEEPABLE_ACCESS), and what is kept
of a posting is the finished search's in memory, never a form's: a form names a posting by its source and id only.
Nothing here starts, changes or feeds a search, and nothing is sent anywhere.
"""
import math
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from starlette.datastructures import FormData

from app.analysis_contracts import RequirementGroup
from app.discovery import presentation, records, supplied, updates, watches
from app.discovery.contracts import CATEGORY_LABELS as RESULT_CATEGORIES
from app.discovery.contracts import Access
from app.discovery.contracts import DIMENSION_LABELS, Category, Dimension, DimensionScope, EndReason, Others, Outcome, SearchRecord, SearchScope
from app.discovery.batch_live import BatchRefused, OperationKind
from app.library.service import new_id
from app.discovery.tasks import TASK_TTL_SECONDS
from app.discovery.web import BASE as DISCOVER
from app.discovery.web import _task, gone_page, searches_of
from app.experience_analysis import PREFERENCE_TOPICS
from app.job_analysis import presentation as analysis_text
from app.job_analysis.web import checked_fields
from app.library.errors import NotAllowed, NotFound, RecordConflict
from app.library.identity import CurrentUser
from app.library.job_choices import (
    ANALYSIS_REASONS,
    CHECK_UNKNOWN,
    CHECK_WORDS,
    DECISION_LABELS,
    EVIDENCE_WORDS,
    FEEDBACK_FIELDS,
    FEEDBACK_REASONS,
    REASONS,
    TRIGGER_WORDS,
    analysis_feedback_label,
    batch_lines,
    basis_text,
    change_lines,
    check_words,
    closure_note,
    deadline_text,
    decision_label,
    feedback_label,
    history_line,
    is_success,
    reason_labels,
    summarize,
    summary_text,
)
from app.library import job_recommendations, job_texts, job_watches
from app.library.job_recommendations import KEEP_DAYS, RecState
from app.library.job_watches import KEEP_NEW_DAYS, SEEN_THRESHOLD
from app.library.lifecycle import (
    HELD_REASON,
    active_job_record,
    delete_job_record,
    request_job_record_purge,
    restore_job_record,
    trashed_job_record,
)
from app.library.content import CATEGORY_LABELS, display_title
from app.library.models import ExperienceItem, JobAnalysis, JobPosting, JobPostingRecheck, JobSearch, JobWatch, JobWatchSeen
from app.library.presentation import field_label, format_local, format_short
from app.library.service import is_expired
from app.library.web import current_user, message_page, render, runtime, safe_next, see_other

RECORDS = "/job-records"
TRASH = "/experiences?tab=trash"
NOTICES = {
    "decided": "决定已保存。",
    "undecided": "已撤销决定，这条岗位记录还在。",
    "feedback": "已记下你指出的不准。",
    "deleted": "已移到回收站，7 天内可以恢复。",
    "restored": "已恢复。",
    "analysis_feedback": "已记下你指出的不准。",
    "rechecked": "已核对一次，结果见下面的“来源核对”。你的决定没有变。",
    "refresh_started": "已开始核对跟踪中的岗位，结果见“核对跟踪中的岗位”一节的“上一轮”。你的决定都没有变。",
    "refresh_busy": "你的上一轮核对还没结束，这次没有重复开始。",
    "refresh_none": "现在没有要核对的岗位：跟踪中的岗位都在 10 分钟内核对过，或者还没有跟踪中的岗位。",
    "refresh_failed": "这次没有开始核对：后台没有接受这项任务（应用可能正在关闭）。你的决定都没有变。",
    # Current recommendations held for tracking (U3-D4b-2).
    "untracked": "已不再跟踪：这条临时留存的推荐已删除，不进回收站。以后的查找仍可能再推荐它；不想再看到，请用“跳过”。",
    "untrack_lapsed": "这条临时留存的推荐已经到期，已经删除。",
    "kept_added": "已加入我的岗位：这条不再是临时留存的推荐，不会到期删除。",
    "decided_added": "决定已保存，这个岗位已加入我的岗位：不再是临时留存的推荐，不会到期删除。",
    "feedback_added": "已记下你指出的不准，这个岗位已加入我的岗位：不再是临时留存的推荐，不会到期删除。",
    # 每日查新 (U3-D4c).
    "watch_kept": "已加入我的岗位：这是每日查新找到的岗位，它不再出现在新增岗位里，以后的查新也不会再把它当作新增。",
    # 补充岗位 (U3-D4d).
    "supplied_kept": "已加入我的岗位：补充链接读到的公开信息和那次核对一起保存了；岗位正文没有保存。",
    "pasted_kept": "已加入我的岗位：你提供的原文和链接按核对页的样子保存了，可以删除、恢复和导出。",
    "supplied_mine": "这个岗位已经在我的岗位里：没有读取，也没有重复保存。要看它现在的状态，请用下面的“再核对一次”。",
    "supplied_held": "这个岗位是你的临时留存推荐：没有读取，也没有重复保存。要留下它，请点“加入我的岗位”。",
    "supplied_already": "这个岗位刚刚已经在你的记录里：没有重复保存，这次读到的结果也没有写进这条记录。",
    "supplied_again": "这份补充结果已经对应过这条记录，没有重复保存。",
}
WATCH_NOTICES = {
    "watch_started": "已开启每日查新，正在建立基准：现在已有的岗位记为见过，不算新增。",
    "watch_busy": "已开启每日查新，但你的上一轮检查还没结束，基准还没建立。等它结束后点“现在查新”；自动检查开着时也会补上。",
    "watch_failed": "已开启每日查新，但后台没有接受建立基准的任务（应用可能正在关闭），基准还没建立。稍后点“现在查新”。",
    "watch_run_started": "已开始这一轮查新，结果见下面的“上次查新”。",
    "watch_run_busy": "你的上一轮检查还没结束，这次没有重复开始。",
    "watch_run_failed": "这次没有开始查新：后台没有接受这项任务（应用可能正在关闭）。",
    "watch_fresh": "这个查新 10 分钟内刚开始过一轮，这次没有重复开始。",
    "watch_full": watches.FULL,
    "watch_unavailable": "这个查新现在不能运行：它的来源不在这次运行的来源里，或者不是本机合成数据。没有发出请求。",
    "watch_stopped": "已停止并清除每日查新：它的条件、见过的岗位和还没处理的新增岗位都已删除，不进回收站。这份摘要和你加入我的岗位的都不受影响。",
    "watch_dismissed": "这条新增岗位不再显示；以后的查新也不会再把它当作新增。",
}
LAPSED_TITLE = "这条推荐已到期，不再保留"
LAPSED_TEXT = (
    f"按你在查找时的勾选，系统临时留存了这条推荐 {KEEP_DAYS} 天；时间已到，它不再保留，也不再核对，这次什么都没有写。"
    "需要的话，可以重新查找。"
)
CONFLICT = "这条记录在你打开页面之后变了（可能在另一个标签页里改过），这次没有执行。下面是最新状态，请再看一遍。"
DECISIONS = [(value.value, label, REASONS[value]) for value, label in DECISION_LABELS.items()]

router = APIRouter()


# --- keeping from a finished search ------------------------------------------------------------------------------------


@router.get(f"{DISCOVER}/tasks/{{task_id}}/keep")
def keep_page(task_id: str, request: Request, source: str = "", posting: str = "", user: CurrentUser = Depends(current_user)):
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    return _keep_view(request, user, task, source, posting)


@router.post(f"{DISCOVER}/tasks/{{task_id}}/keep")
def keep(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    source, posting, action = form.get("source", ""), form.get("posting", ""), form.get("action", "")

    try:
        task = _task(request, user, task_id)
    except NotFound:
        return _expired(request, user)

    keep_sources = searches_of(request).sources.keep
    rt = runtime(request)

    try:
        seen = records.seen_from(form.get("record", ""), form.get("revision", ""))
        found = records.found_in(task, keep_sources, source, posting)

        with rt.database.transaction() as db:
            now = rt.now()

            # A current recommendation whose time has come is removed first, with its text (U3-D4b-2).
            forget = searches_of(request).apply_checks.forget_record

            if action == "keep":
                records.keep_posting(db, user.id, found, seen, now, forget=forget)
            elif action == "decide":
                records.decide_on_found(
                    db, user.id, found, seen, form.get("decision", ""), form.getlist("reason"), form.get("note", ""), now, forget=forget
                )
            elif action == "feedback":
                records.feedback_on_found(
                    db, user.id, found, seen, form.get("field", ""), form.get("feedback_reason", ""), form.get("note", ""), now, forget=forget
                )
            elif action == "analysis":
                sources = searches_of(request).sources
                kept = records.keep_analysis(db, user.id, task, found, keep_sources, sources.keep_analyses, sources.mode, seen, now)
                kept_id = kept.id
            else:
                raise records.InputRejected("请从页面上的按钮提交。")
    except records.KeepRefused as refused:
        return _refused(request, user, refused.reason, f"{DISCOVER}/tasks/{task.id}")
    except NotFound:
        return _no_posting(request, user, task)
    except records.InputRejected as rejected:
        return _keep_view(request, user, task, source, posting, error=rejected.message, status_code=422)
    except RecordConflict:
        return _keep_view(request, user, task, source, posting, error=CONFLICT, status_code=409)

    if action == "analysis":
        # Remembered once committed, never cleared: kept from this search, it is never kept again from it (U3-D3b).
        searches_of(request).store.note_kept_analysis(task.id, user.id, user.token_hash, (source, posting), kept_id)

    return see_other(
        f"{DISCOVER}/tasks/{task.id}?notice={ {'keep': 'kept', 'decide': 'decided', 'feedback': 'feedback', 'analysis': 'analysis_kept'}[action] }"
    )


@router.post(f"{DISCOVER}/tasks/{{task_id}}/summary")
def keep_summary(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return _expired(request, user)

    sources = searches_of(request).sources
    rt = runtime(request)
    back = f"{DISCOVER}/tasks/{task.id}"

    try:
        seen = records.seen_from(form.get("record", ""), form.get("revision", ""))

        with rt.database.transaction() as db:
            records.keep_search(db, user.id, task, sources.keep, sources.mode, seen, rt.now())
    except records.KeepRefused as refused:
        return _refused(request, user, refused.reason, back)
    except records.InputRejected as rejected:
        return message_page(request, title="没有保存", message=rejected.message, status_code=422, user=user, links=[(back, "返回结果")])
    except RecordConflict:
        return message_page(
            request,
            title="没有保存：摘要刚刚变了",
            message="这次查找的摘要在你打开页面之后已经保存过或删除过了，这次没有执行。请先看最新状态。",
            status_code=409,
            user=user,
            links=[(back, "返回结果"), (RECORDS, "查看岗位记录")],
        )

    return see_other(f"{back}?notice=summary")


def _keep_view(request: Request, user: CurrentUser, task, source: str, posting: str, *, error=None, status_code=200) -> Response:
    sources = searches_of(request).sources

    try:
        found = records.found_in(task, sources.keep, source, posting)
    except records.KeepRefused as refused:
        return _refused(request, user, refused.reason, f"{DISCOVER}/tasks/{task.id}")
    except NotFound:
        return _no_posting(request, user, task)

    rt = runtime(request)
    ref = found.item.ref

    with rt.database.transaction() as db:
        now = rt.now()
        mine = records.kept_states(db, user.id, [ref], now).get((ref.source_id, ref.posting_id))
        held = records.held_states(db, [mine] if mine else [], now).get(mine.id) if mine else None
        analysis_row = records.kept_analyses(db, user.id, task.id, now).get((ref.source_id, ref.posting_id))

    return render(
        request,
        "job_keep.html",
        {
            # Keeping its analysis (U3-D3b): offered for a result of a search whose sources may keep analyses.
            "analysis_keep": {
                "allowed": isinstance(found.item, records.Result) and records.analyses_keepable(task, sources.keep, sources.keep_analyses),
                "row": analysis_row,
                "purged": analysis_row is None and (ref.source_id, ref.posting_id) in task.kept_analyses,
                "basis": _basis_lines(task, found.item) if isinstance(found.item, records.Result) else [],
            },
            "area": "jobs",
            "section": "discover",
            "task": task,
            "item": found.item,
            "is_result": isinstance(found.item, records.Result),
            "check_lines": _found_check_lines(found.item, sources.checked_note, kept=mine is not None),
            "mine": mine,
            "held": held,
            "error": error,
            "decision_label": decision_label,
            "SOURCE_SET": sources,
            "place_text": presentation.place_text,
            "type_text": presentation.type_text,
            "stated": presentation.stated,
            "deadlines_text": presentation.deadlines_text,
            "checked_text": partial(presentation.checked_text, note=sources.checked_note),
            "evidence_label": partial(presentation.evidence_label, note=sources.evidence_note),
            **_choices(),
        },
        user=user,
        status_code=status_code,
    )


def _expired(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="没有保存：找不到这次查找",
        message=(
            f"临时结果只保留 {TASK_TTL_SECONDS // 60} 分钟，服务重启、退出登录或换了会话也会清除；"
            "过期以后不能再保存，这次什么都没有保存。需要的话，请重新查找。"
        ),
        status_code=404,
        user=user,
        links=[(DISCOVER, "返回找岗位"), (RECORDS, "查看岗位记录")],
    )


def _no_posting(request: Request, user: CurrentUser, task) -> Response:
    return message_page(
        request,
        title="找不到这个岗位",
        message="这次查找的结果里没有这个岗位，什么都没有保存。",
        status_code=404,
        user=user,
        links=[(f"{DISCOVER}/tasks/{task.id}", "返回结果")],
    )


def _refused(request: Request, user: CurrentUser, reason: str, back: str) -> Response:
    return message_page(
        request,
        title="没有保存",
        message=reason,
        status_code=409 if reason in records.ANALYSIS_REFUSALS else 403,
        user=user,
        links=[(back, "返回")],
    )


# --- the records kept ----------------------------------------------------------------------------------------------------


@router.get(RECORDS)
def records_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    with rt.database.transaction() as db:
        now = rt.now()
        # 我的岗位, and apart from them the current recommendations held for tracking, open or closed (U3-D4b-2); one
        # whose time has come is shown nowhere.
        postings, current, closed = records.postings_by_state(db, user.id, now)
        shown = [*postings, *(row for row, _ in current), *(row for row, _ in closed)]
        # A search's checks and the record's own together (U3-D4a): the latest attempt, answer and success apart.
        states = {row.id: summarize([*records.checks_of(db, row), *updates.rechecks_of(db, row)]) for row in shown}
        # A posting the user pasted is checked by nothing (U3-D4d): said so, never "no check yet".
        pasted = {row.id for row in shown if row.source_access == job_texts.USER_PROVIDED}
        summaries = records.searches_in_use(db, user.id)
        analyses = records.analyses_in_use(db, user.id)
        bases = {row.id: records.basis_now(db, user.id, row, rt.now()).worst for row in analyses}
        refresh = _refresh_view(db, user, searches_of(request), rt.now())
        # 每日查新 (U3-D4c): each watch's summary, and how many new postings it holds now.
        watched = [
            (summary, watch, job_watches.new_count(db, watch.id, now))
            for watch in job_watches.watches_of(db, user.id)
            for summary in [db.get(JobSearch, watch.search_record_id)]
            if summary is not None and summary.deleted_at is None
        ]

    groups = [(label, [row for row in postings if row.decision == value.value]) for value, label in DECISION_LABELS.items()]
    groups.append(("没有决定", [row for row in postings if row.decision is None]))

    return render(
        request,
        "job_records.html",
        {
            "area": "jobs",
            "section": "job_records",
            "groups": groups,
            "has_postings": bool(postings),
            "current": current,
            "closed": closed,
            "KEEP_DAYS": KEEP_DAYS,
            "check_texts": {
                record_id: PASTED_CHECK if record_id in pasted else summary_text(state, format_short) for record_id, state in states.items()
            },
            "pasted": pasted,
            "summaries": summaries,
            "analyses": analyses,
            "basis_texts": {record_id: basis_text(state) for record_id, state in bases.items()},
            "CATEGORY_WORDS": RESULT_CATEGORIES,
            "notice": NOTICES.get(notice),
            "decision_label": decision_label,
            "reason_labels": reason_labels,
            "refresh": refresh,
            "watched": watched,
            "watch_auto": _auto_text(searches_of(request)),
            "KEEP_NEW_DAYS": KEEP_NEW_DAYS,
        },
        user=user,
    )


# --- 现在核对 and the automatic check of the postings tracked (U3-D4b-1) -----------------------------------------------


@router.post(f"{RECORDS}/refresh")
def records_refresh(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Start one batch of the user's tracked postings, unless one of theirs runs already or none is to be checked."""
    rt = runtime(request)
    searches = searches_of(request)

    with rt.database.transaction() as db:
        now = rt.now()
        ready = [item for item in updates.tracked(db, user.id, searches.sources.keep, now) if updates.eligible(item, updates.Trigger.MANUAL, now)]

    if not ready:
        return see_other(f"{RECORDS}?notice=refresh_none")

    # Held from now until the batch ends (F1): a second click, or the automatic check, finds it busy meanwhile.
    started = searches.start_batch(rt.database, user.id, updates.Trigger.MANUAL, rt.now)

    if started.startswith("batch:"):
        # The D6 batch would not begin it (D6 plan, 3.2): nothing was queued, and nothing sent.
        return message_page(request, title="没有开始核对", message=f"{started[6:]}；什么都没有发送。", status_code=409, user=user, links=[(RECORDS, "返回岗位记录")])

    return see_other(f"{RECORDS}?notice=refresh_{started}")


def _refresh_view(db, user: CurrentUser, searches, now: datetime) -> dict:
    """What is due now, and apart from it, what the last batch actually did."""
    items = updates.tracked(db, user.id, searches.sources.keep, now)
    last = updates.last_batch(db, user.id)

    return {
        "auto": searches.update_seconds is not None,
        "every": None if searches.update_seconds is None else round(searches.update_seconds / 60),
        "tracked": len(items),
        "recommended": sum(item.recommended for item in items),
        "due": sum(updates.eligible(item, updates.Trigger.SCHEDULED, now) for item in items),
        "never": sum(item.attempted_at is None for item in items),
        "running": searches.batches.holds(user.id),
        "last": None if last is None else batch_lines(last[0], last[1], format_local),
        # Which sources it tracks, and the most a round takes, as this run has them (D6-1 R3, L2).
        "scope": presentation.tracking_scope(searches.sources),
        "limits": presentation.refresh_limits_text(searches.refresh_limits()),
    }


@router.get(f"{RECORDS}/postings/{{record_id}}")
def posting_page(record_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return _posting_view(request, user, record_id, notice=NOTICES.get(notice))


def _posting_view(request: Request, user: CurrentUser, record_id: str, *, notice=None, error=None, status_code=200) -> Response:
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            now = rt.now()
            record = active_job_record(db, user.id, JobPosting, record_id)
            state = job_recommendations.state_of(db, record, now)
            marker = job_recommendations.marker_of(db, record.id)
            checks = records.checks_of(db, record)
            rechecks = updates.rechecks_of(db, record)
            triggers = updates.triggers_of(db, rechecks)
            feedback = records.feedback_of(db, record)
            analyses = records.analyses_of_posting(db, user.id, record.source_id, record.posting_id)
            # Added by the user themselves (U3-D4d): how it came, and the text of one they pasted.
            pasted = record.source_access == job_texts.USER_PROVIDED
            user_text = job_texts.text_in_use(db, user.id, record.id) if pasted else None
    except NotFound:
        return _not_found(request, user)

    if state is RecState.LAPSED:
        # Gone from the moment its time came (U3-D4b-2), as is anything memory holds of it.
        return _lapsed(request, user, record_id)

    return render(
        request,
        "job_record.html",
        {
            "held": None if state is RecState.MINE else {"state": state.value, "until": marker.until},
            "analyses": analyses,
            "area": "jobs",
            "section": "job_records",
            "record": record,
            "facts": record.facts,
            # Added from 每日查新 (U3-D4c): when the watch first read it.
            "via_watch": _via_watch(record.facts),
            # Added by the user (U3-D4d): from a link, or pasted, with the text as kept.
            "via_supplied": _via_supplied(record.facts),
            "pasted": pasted,
            "user_text": user_text,
            "checks": checks,
            **_check_view(record, checks, rechecks, triggers, analyses, rt.now()),
            "feedback": feedback,
            "notice": notice,
            "error": error,
            "synthetic": record.source_access == Access.SYNTHETIC.value,
            "iso_local": lambda value: format_local(datetime.fromisoformat(value)),
            "EVIDENCE_WORDS": EVIDENCE_WORDS,
            "decision_label": decision_label,
            "reason_labels": reason_labels,
            "feedback_label": feedback_label,
            **_choices(),
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{RECORDS}/postings/{{record_id}}/decide")
def posting_decide(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    decision = form.get("decision", "")

    return _posting_write(
        request,
        user,
        record_id,
        lambda db, now, revision: records.decide_on_kept(
            db, user.id, record_id, revision, decision, form.getlist("reason"), form.get("note", ""), now
        ),
        form,
        "undecided" if decision == "" else "decided",
    )


@router.post(f"{RECORDS}/postings/{{record_id}}/feedback")
def posting_feedback(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    return _posting_write(
        request,
        user,
        record_id,
        lambda db, now, revision: records.feedback_on_kept(
            db, user.id, record_id, revision, form.get("field", ""), form.get("feedback_reason", ""), form.get("note", ""), now
        ),
        form,
        "feedback",
    )


def _posting_write(request: Request, user: CurrentUser, record_id: str, act, form: FormData, notice: str) -> Response:
    rt = runtime(request)

    try:
        revision = _revision(form)

        with rt.database.transaction() as db:
            # A current recommendation decided on, or said not to be right, is added to 我的岗位 by it (U3-D4b-2): said so.
            added = job_recommendations.marker_of(db, record_id) is not None
            act(db, rt.now(), revision)
    except NotFound:
        return _not_found(request, user)
    except records.RecommendationLapsed:
        return _lapsed(request, user, record_id)
    except records.KeepRefused as refused:
        return _refused(request, user, refused.reason, f"{RECORDS}/postings/{record_id}")
    except records.InputRejected as rejected:
        return _posting_view(request, user, record_id, error=rejected.message, status_code=422)
    except RecordConflict:
        return _posting_view(request, user, record_id, error=CONFLICT, status_code=409)

    return see_other(f"{RECORDS}/postings/{record_id}?notice={notice}{'_added' if added else ''}")


@router.post(f"{RECORDS}/postings/{{record_id}}/add")
def posting_add(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """加入我的岗位 (U3-D4b-2): a current recommendation becomes the user's own, and never lapses."""
    return _posting_write(
        request, user, record_id, lambda db, now, revision: records.add_to_mine(db, user.id, record_id, revision, now), form, "kept"
    )


@router.post(f"{RECORDS}/postings/{{record_id}}/untrack")
def posting_untrack(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """不再跟踪 (U3-D4b-2): a current recommendation removed now, with its checks and what memory holds of it, never
    through the recycle bin; one whose time has come is removed as it is. 我的岗位 goes through the bin instead."""
    rt = runtime(request)
    forget = searches_of(request).apply_checks.forget_record

    try:
        revision = _revision(form)

        with rt.database.transaction() as db:
            outcome = records.stop_tracking(db, user.id, record_id, revision, rt.now(), forget=forget)
    except NotFound:
        return _not_found(request, user)
    except records.KeepRefused as refused:
        return message_page(
            request, title="不是临时留存的推荐", message=refused.reason, status_code=409, user=user, links=[(f"{RECORDS}/postings/{record_id}", "返回")]
        )
    except records.InputRejected as rejected:
        return _posting_view(request, user, record_id, error=rejected.message, status_code=422)
    except RecordConflict:
        return _posting_view(request, user, record_id, error=CONFLICT, status_code=409)

    return see_other(f"{RECORDS}?notice={'untracked' if outcome == 'stopped' else 'untrack_lapsed'}")


def _lapsed(request: Request, user: CurrentUser, record_id: str) -> Response:
    """A current recommendation whose time has come: shown never, its text dropped from memory at once (U3-D4b-2)."""
    searches_of(request).apply_checks.forget_record(record_id)

    return message_page(request, title=LAPSED_TITLE, message=LAPSED_TEXT, status_code=410, user=user, links=[(RECORDS, "返回岗位记录")])


# --- 补充岗位: a posting the user found elsewhere (U3-D4d) --------------------------------------------------------------
#
# A link is matched as text against the shapes of the registered sources, and read only from a source the app may read
# now, by that source's own adapter, as D4a reads one (app.discovery.supplied); the address itself is never opened.
# Otherwise the user pastes the text, said as 用户提供，系统未独立核验. What is read or pasted is held in memory
# (searches.supply) for the session that made it, 30 minutes at most. It is added to 我的岗位 only when the user asks
# (records.keep_supplied_link, keep_pasted), the record bound to what memory holds before the write commits; and it is
# analysed only on the 岗位分析 page, after that page's own choice of experiences, review and confirmation.

SUPPLY = f"{RECORDS}/supply"
PASTED_CHECK = "用户提供，系统不核对招聘状态"
SUPPLY_NOTICES = {"discarded": "这份补充结果已放弃：内存里的内容已经清掉，没有保存任何东西。"}
SUPPLY_GONE = (
    "这份补充结果已经过期、已放弃，或者不属于这次登录：它只在内存里保留 30 分钟，退出登录时也会清掉。"
    "这次什么都没有保存。需要的话，请重新补充。"
)
SUPPLY_BUSY = "这份补充结果正在保存，请稍后刷新再看。"
SUPPLY_STALE = "这一页在你打开以后更新过（可能在另一个标签页里打开过），这次没有保存。下面是最新的样子，请再看一遍再决定。"
# D4d F1: a save the server never showed the page of.
SUPPLY_UNREVIEWED = "还没有给你看过要保存的内容，这次没有保存。下面就是要保存的样子，请看一遍再决定。"
SUPPLY_NEW_MATCHES = (
    "保存前又找到了这一页没列出的相同或部分相同的岗位（可能刚在别处保存过），这次没有保存。下面已经重新列出，请看一遍再决定。"
)
SUPPLY_CHOICE = "你按的按钮和这一页列出的相同或部分相同的岗位对不上，这次没有保存。请看一遍下面的列表再决定。"
SUPPLY_ENDED = "这次登录已经结束，补充的内容已经清掉，没有保存；请重新登录后重新补充。"
SUPPLY_FULL = f"你已经有 {supplied.MAX_RESULTS} 份补充结果还在内存里：请先保存或放弃其中一份，或者等它们过期（30 分钟）。"
SUPPLY_READING = "你的上一次补充读取还没结束，请等它结束再试。"


@router.get(SUPPLY)
def supply_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return _supply_view(request, user, notice=SUPPLY_NOTICES.get(notice))


def _supply_view(
    request: Request,
    user: CurrentUser,
    *,
    link: str = "",
    pasted: dict | None = None,
    link_problem: str | None = None,
    problems: list[str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    searches = searches_of(request)

    return render(
        request,
        "job_supply.html",
        {
            "area": "jobs",
            "section": "job_records",
            "readable": supplied.readable_sources(searches.sources.keep, searches.supply_register()),
            "link": link,
            # What the paste form had, every field of it (an empty one when not given).
            "pasted": {name: (pasted or {}).get(name, "") for name in ("title", "company", "text", "link")},
            "link_problem": link_problem,
            "problems": problems or [],
            "notice": notice,
            "MAX_LINK_CHARS": supplied.MAX_LINK_CHARS,
            "MAX_NAME_CHARS": supplied.MAX_NAME_CHARS,
            "MAX_TEXT_CHARS": supplied.MAX_TEXT_CHARS,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{SUPPLY}/link")
def supply_link(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Read the posting a link names, once, if the app may read its source now; never the address itself."""
    rt = runtime(request)
    searches = searches_of(request)
    raw = form.get("link", "")
    # Carried to the paste form, for the user to keep as the posting's link or clear.
    carried = {"link": raw.strip()[: supplied.MAX_LINK_CHARS]}

    def refused(message: str, status_code: int = 422) -> Response:
        return _supply_view(request, user, link=raw, pasted=carried, link_problem=message, status_code=status_code)

    try:
        ref = supplied.match(raw)
    except supplied.LinkProblem as problem:
        return refused(problem.message)

    register = searches.supply_register()
    reason = supplied.refusal(ref, searches.sources.keep, register)

    if reason is not None:
        return refused(reason)

    with rt.database.transaction() as db:
        state, row = records.own_posting(db, user.id, ref, rt.now())

    if state in ("mine", "held"):
        # The user's own already: nothing is read, and nothing read is ever written to it.
        return see_other(f"{RECORDS}/postings/{row.id}?notice=supplied_{state}")

    if state == "bin":
        return refused(records.SUPPLIED_IN_BIN, 409)

    store = searches.supply
    # Reserved before the first call: ending the session from now on spoils it, so a read that comes back after is
    # never held or shown (17.2).
    ticket = store.reserve(user.id, user.token_hash)

    if ticket is None:
        return refused(SUPPLY_ENDED if store.ended(user.token_hash) else SUPPLY_READING, 409)

    try:
        # One operation of the D6 batch, begun before the source may be read (D6 plan, 3.2).
        operation = searches.begin_operation(OperationKind.SUPPLY, user.id, f"supply-{ticket}")
    except BatchRefused as batch_refused:
        store.cancel(ticket)

        return refused(f"{batch_refused}，所以没有读取，什么都没有发送。", 409)

    try:
        content = supplied.read(ref, register, delisting=searches.sources.delisting, now=rt.now)

        if content.reading.facts is None:
            return refused(supplied.failure_text(content.reading))

        try:
            token = store.publish(ticket, user.id, user.token_hash, content)
        except supplied.Full:
            return refused(SUPPLY_FULL, 409)

        if token is None:
            return message_page(request, title="这次读取的结果没有保留", message=SUPPLY_ENDED, status_code=409, user=user, links=[(SUPPLY, "重新补充")])

        return see_other(f"{SUPPLY}/{token}")
    finally:
        searches.end_operation(operation)
        store.cancel(ticket)


@router.post(f"{SUPPLY}/text")
def supply_text(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Hold a pasted posting, to be looked at before it is saved; nothing is saved or sent here."""
    rt = runtime(request)
    pasted = {name: form.get(name, "") for name in ("title", "company", "text", "link")}
    title, company = " ".join(pasted["title"].split()), " ".join(pasted["company"].split())
    text = supplied.kept_text(pasted["text"])
    problems = []

    for label, value in (("职位名", title), ("公司", company)):
        if not value:
            problems.append(f"请填写{label}。")
        elif len(value) > supplied.MAX_NAME_CHARS:
            problems.append(f"{label}最多 {supplied.MAX_NAME_CHARS} 字，现在有 {len(value)} 字。")

    if not text:
        problems.append("请粘贴岗位原文。")
    elif len(text) > supplied.MAX_TEXT_CHARS:
        problems.append(f"岗位原文最多 {supplied.MAX_TEXT_CHARS} 字，现在有 {len(text)} 字。")

    link = None

    if pasted["link"].strip():
        try:
            link = supplied.stored_link(pasted["link"])
        except supplied.LinkProblem as problem:
            problems.append(f"原始链接：{problem.message}")

    if problems:
        return _supply_view(request, user, pasted=pasted, problems=problems, status_code=422)

    content = supplied.PastedContent(title, company, text, supplied.text_digest(text), link, rt.now())

    try:
        token = searches_of(request).supply.put(user.id, user.token_hash, content)
    except supplied.Full:
        return _supply_view(request, user, pasted=pasted, problems=[SUPPLY_FULL], status_code=409)

    if token is None:
        return message_page(request, title="没有保留", message=SUPPLY_ENDED, status_code=409, user=user, links=[(SUPPLY, "重新补充")])

    return see_other(f"{SUPPLY}/{token}")


@router.get(f"{SUPPLY}/{{token}}")
def supplied_page(token: str, request: Request, user: CurrentUser = Depends(current_user)):
    return _supplied_view(request, user, token)


def _supplied_view(request: Request, user: CurrentUser, token: str, *, error: str | None = None, status_code: int = 200) -> Response:
    """What was read or pasted, as it would be kept: each showing a snapshot, under a random id the save names (F1).
    For a pasted one, the matches it has now are listed and kept with that snapshot (17.3)."""
    rt = runtime(request)
    store = searches_of(request).supply
    held = store.get(token, user.id, user.token_hash)

    if held is None:
        return _supply_gone(request, user)

    content = held.content
    matches, related, state, own = [], [], None, None

    with rt.database.transaction() as db:
        now = rt.now()
        saved = None if held.saved is None else _record_in_use(db, user.id, held.saved)

        if content.kind == supplied.LINK:
            facts = content.reading.facts or {}
            state, own = records.own_posting(db, user.id, content.ref, now)
            related = records.related_postings(db, user.id, facts.get("title") or "", facts.get("company") or "", pasted=True)
        else:
            matches = records.supplied_matches(db, user.id, content, now)
            related = records.related_postings(db, user.id, content.title, content.company, pasted=False)

    # Both kinds are saved only from a page shown (F1): a link's read as much as a pasted text.
    held = store.show(token, user.id, user.token_hash, frozenset(match.record_id for match in matches))

    if held is None:
        return _supply_gone(request, user)

    reading = content.reading if content.kind == supplied.LINK else None

    return render(
        request,
        "job_supplied.html",
        {
            "area": "jobs",
            "section": "job_records",
            "held": held,
            "content": content,
            "linked": content.kind == supplied.LINK,
            "reading": reading,
            "facts": (reading.facts or {}) if reading else {},
            "status_text": check_words(reading) if reading else None,
            "succeeded": is_success(reading) if reading else False,
            "answered_at": format_local(reading.answered_at) if reading and reading.answered_at else None,
            "deadline_text": deadline_text(reading.facts) if reading and reading.facts else None,
            "matches": matches,
            "exact": [match for match in matches if match.exact],
            "related": related,
            "own_state": state,
            "own": own,
            "saved": saved,
            "saved_before": held.saved is not None,
            "error": error,
            "EVIDENCE_WORDS": EVIDENCE_WORDS,
            "MAX_TEXT_CHARS": supplied.MAX_TEXT_CHARS,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{SUPPLY}/{{token}}/keep")
def supplied_keep(token: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """加入我的岗位: claimed, written with the record bound to it before the write commits, and released whatever
    happens (17.2); only from the last page the server showed of it (F1), and for a pasted posting only as that page
    showed its matches (17.3). A link's posting the user has already ends the result there, bound to it (F2)."""
    rt = runtime(request)
    searches = searches_of(request)
    store = searches.supply
    choice = form.get("choice", "")

    try:
        claim = store.claim(token, user.id, user.token_hash, form.get("review", ""))
    except supplied.Gone:
        return _supply_gone(request, user)
    except supplied.Busy:
        return _supplied_view(request, user, token, error=SUPPLY_BUSY, status_code=409)
    except supplied.Unreviewed:
        return _supplied_view(request, user, token, error=SUPPLY_UNREVIEWED, status_code=409)
    except supplied.Stale:
        return _supplied_view(request, user, token, error=SUPPLY_STALE, status_code=409)
    except supplied.Saved as saved:
        return _saved_again(request, user, saved.record_id)

    content = claim.content
    pasted = content.kind == supplied.PASTED
    committed, found, record_id, outcome = False, False, None, None

    try:
        if pasted and choice != ("separate" if claim.shown else "add"):
            outcome = ("error", SUPPLY_CHOICE)
        else:
            bind = partial(store.bind, token, claim.ticket)

            with rt.database.transaction() as db:
                if pasted:
                    row = records.keep_pasted(db, user.id, content, claim.shown, choice == "separate", rt.now(), bind=bind)
                else:
                    row = records.keep_supplied_link(
                        db, user.id, content, searches.sources.keep, rt.now(), bind=bind, forget=searches.apply_checks.forget_record
                    )

                record_id = row.id

            committed = True
    except records.NeedsReview:
        outcome = ("error", SUPPLY_NEW_MATCHES)
    except records.AlreadyKept as kept:
        # Bound to the record the user has, inside the write that found it (F2): the result is that record's now.
        found = True
        outcome = ("bin", records.SUPPLIED_IN_BIN) if kept.in_bin else ("kept", kept.record_id)
    except records.InputRejected as rejected:
        outcome = ("error", rejected.message)
    except records.KeepRefused as refused:
        outcome = ("error", refused.reason)
    except supplied.Gone:
        outcome = ("gone", None)
    finally:
        # Released before any page is shown, so a page shown now makes its snapshot anew (17.3, F1).
        store.release(token, claim.ticket, committed or found)

    if committed:
        return see_other(f"{RECORDS}/postings/{record_id}?notice={'pasted_kept' if pasted else 'supplied_kept'}")

    kind, value = outcome

    if kind == "kept":
        return see_other(f"{RECORDS}/postings/{value}?notice=supplied_already")

    if kind == "gone":
        return _supply_gone(request, user)

    # "bin" and every refusal: said on the page, which shows what the result is now.
    return _supplied_view(request, user, token, error=value, status_code=409)


@router.post(f"{SUPPLY}/{{token}}/discard")
def supplied_discard(token: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    try:
        searches_of(request).supply.discard(token, user.id, user.token_hash)
    except supplied.Busy:
        return _supplied_view(request, user, token, error=SUPPLY_BUSY, status_code=409)

    return see_other(f"{SUPPLY}?notice=discarded")


def _supply_gone(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="这份补充结果已经不在了",
        message=SUPPLY_GONE,
        status_code=410,
        user=user,
        links=[(SUPPLY, "重新补充"), (RECORDS, "返回岗位记录")],
    )


def _saved_again(request: Request, user: CurrentUser, record_id: str | None) -> Response:
    """A save of a result that made a record already: that record if in use, else said so; never another."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        row = None if record_id is None else _record_in_use(db, user.id, record_id)

    if row is not None:
        return see_other(f"{RECORDS}/postings/{row.id}?notice=supplied_again")

    return message_page(
        request,
        title="这份补充结果已经对应过一条记录",
        message=(
            "这份补充结果已经对应过你的一条岗位记录（它保存的，或者保存时发现你已经有的），那条记录现在不在我的岗位里："
            "可能在回收站里，可以恢复；也可能已经永久删除。同一份结果不会再保存一次；需要的话，请重新补充。"
        ),
        status_code=409,
        user=user,
        links=[(TRASH, "去回收站"), (SUPPLY, "重新补充"), (RECORDS, "返回岗位记录")],
    )


def _record_in_use(db, owner_id: str, record_id: str) -> JobPosting | None:
    try:
        return active_job_record(db, owner_id, JobPosting, record_id)
    except NotFound:
        return None


# --- checks of a kept posting from its record (U3-D4a) -----------------------------------------------------------------
#
# 再核对一次 and 申请前核对: one check of the posting's public facts, asked for here, on synthetic sources only
# (app.discovery.updates). Neither changes the user's decision; 申请前核对 also shows the text it read, from memory only,
# to the session that asked, for a while.

DISCARDED = (
    "核对期间这条岗位记录变了（可能在别处改过、删除，或删除后又恢复）。为了不让过时的结果覆盖，这次的结果和读到的岗位正文都没有保存；"
    "你的决定没有变。请回到记录，再核对一次。"
)
UNSHOWN = (
    "核对结果已经记在岗位记录里；但这次核对开始以后，这条记录被删除过、你已经退出登录，或者它作为临时留存的推荐到了核对时定的到期时间，"
    "所以这次读到的岗位正文不再显示。恢复的记录也不会带回以前读到的正文。要再看正文，请回到记录，再做一次申请前核对。"
)
GONE_TEXT = (
    "岗位正文只临时显示：只给发起核对的这次登录看，最多 30 分钟，记录删除以后立即失效；核对时它还是临时留存的推荐的，"
    "到核对时定的到期时间也会失效，之后加入我的岗位或续期都不会延长。核对结果（不含正文）已经记在岗位记录里；"
    "要再看正文，请回到记录，再做一次申请前核对。"
)


@router.post(f"{RECORDS}/postings/{{record_id}}/recheck")
def posting_recheck(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    finished = _check(request, user, record_id, form, updates.Trigger.RECHECK)

    if isinstance(finished, Response):
        return finished

    if not finished.written:
        return _discarded(request, user, record_id)

    return see_other(f"{RECORDS}/postings/{record_id}?notice=rechecked")


@router.post(f"{RECORDS}/postings/{{record_id}}/apply-check")
def posting_apply_check(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    texts = searches_of(request).apply_checks
    rt = runtime(request)

    # A current recommendation whose time has come is refused before anything is reserved or sent (U3-D4b-2).
    try:
        with rt.database.transaction() as db:
            lapsed = job_recommendations.lapsed(db, active_job_record(db, user.id, JobPosting, record_id), rt.now())
    except NotFound:
        return _not_found(request, user)

    if lapsed:
        return _lapsed(request, user, record_id)

    # Reserved before the check begins (F2): deleting the record or ending the session from now on spoils it, so a text
    # read before either is never shown, however late the check comes back.
    ticket = texts.reserve(user.id, user.token_hash, record_id)

    if ticket is None:
        ended = "这次登录已经结束，没有开始核对；请重新登录后再试。" if texts.ended(user.token_hash) else updates.BUSY

        return _posting_view(request, user, record_id, error=ended, status_code=409)

    try:
        finished = _check(request, user, record_id, form, updates.Trigger.APPLY)

        if isinstance(finished, Response):
            return finished

        if not finished.written:
            return _discarded(request, user, record_id)

        try:
            token = texts.put(user.id, user.token_hash, record_id, finished)
        except updates.TextLapsed:
            # Its time came after the check was written, by the time it was to be published (U3-D4b-2 F1): the store
            # published nothing, deciding by its own clock under its own lock.
            return _text_lapsed(request, user, record_id, _unshown)

        if token is None:
            return _unshown(request, user, record_id)

        return see_other(f"{RECORDS}/postings/{record_id}/apply-check/{token}")
    finally:
        texts.cancel(ticket)


@router.get(f"{RECORDS}/postings/{{record_id}}/apply-check/{{token}}")
def posting_apply_check_page(record_id: str, token: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    checks = searches_of(request).apply_checks

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobPosting, record_id)
            lapsed = job_recommendations.lapsed(db, record, rt.now())
            analyses = records.analyses_of_posting(db, user.id, record.source_id, record.posting_id)
    except NotFound:
        # Deleted since (its text went with the deletion), or never this user's: nothing is shown, nothing changes.
        return _not_found(request, user)

    if lapsed:
        # A current recommendation whose time has come: its text is never shown again, and goes now (U3-D4b-2).
        return _lapsed(request, user, record_id)

    try:
        # What decides is the store's own clock, read under the lock the text is read under (U3-D4b-2 F1): the
        # record's time may come after the look above, by the time the text is read.
        shown = checks.get(token, user.id, user.token_hash, record_id)
    except updates.TextLapsed:
        return _text_lapsed(request, user, record_id, _gone)

    if shown is None:
        return _gone(request, user, record_id)

    reading = shown.reading
    facts = reading.facts or {}

    return render(
        request,
        "job_apply_check.html",
        {
            "area": "jobs",
            "section": "job_records",
            "record": record,
            "kept": record.facts,
            "reading": reading,
            "facts": facts,
            "deadline_text": deadline_text(facts) if reading.facts else None,
            "status_text": check_words(reading),
            "succeeded": is_success(reading),
            "answered_at": format_local(reading.answered_at) if reading.answered_at else None,
            "changes": change_lines(shown.changes),
            "stale": [row for row in analyses if reading.content_sha256 is not None and row.content_sha256 != reading.content_sha256],
            "analyses": analyses,
            "minutes_left": checks.minutes_left(shown),
            "decision_label": decision_label,
            "synthetic": record.source_access == Access.SYNTHETIC.value,
        },
        user=user,
    )


def _check(request: Request, user: CurrentUser, record_id: str, form: FormData, trigger: updates.Trigger):
    """Run one check of the record as its page saw it, or say why not (a Response)."""
    rt = runtime(request)
    searches = searches_of(request)

    try:
        revision = _revision(form)
    except records.InputRejected as rejected:
        return _posting_view(request, user, record_id, error=rejected.message, status_code=422)

    try:
        # One operation of the D6 batch (a 再核对 or a 申请前核对), begun before anything may be read (D6 plan, 3.2).
        kind = OperationKind.APPLY if trigger is updates.Trigger.APPLY else OperationKind.CHECK
        operation = searches.begin_operation(kind, user.id, f"{kind.value}-{record_id}-{new_id()}")
    except BatchRefused as refused:
        return _posting_view(request, user, record_id, error=f"{refused}，所以没有核对，什么都没有发送。", status_code=409)

    try:
        return updates.run_check(
            rt.database,
            user.id,
            record_id,
            revision,
            trigger,
            keep=searches.sources.keep,
            delisting=searches.sources.delisting,
            register=lambda: searches.register(SimpleNamespace(id=f"check-{record_id}", cancel=None)),
            now=rt.now,
            checking=searches.checking,
        )
    except NotFound:
        return _not_found(request, user)
    except records.InputRejected as rejected:
        return _posting_view(request, user, record_id, error=rejected.message, status_code=422)
    except RecordConflict:
        return _posting_view(request, user, record_id, error=CONFLICT, status_code=409)
    except updates.CheckRefused as refused:
        if refused.reason == updates.LAPSED:
            return _lapsed(request, user, record_id)

        return _posting_view(request, user, record_id, error=refused.reason, status_code=409 if refused.reason == updates.BUSY else 403)
    finally:
        searches.end_operation(operation)


def _discarded(request: Request, user: CurrentUser, record_id: str) -> Response:
    return message_page(
        request,
        title="这次核对的结果没有保存",
        message=DISCARDED,
        status_code=409,
        user=user,
        links=[(f"{RECORDS}/postings/{record_id}", "返回这条岗位记录"), (RECORDS, "返回岗位记录")],
    )


def _unshown(request: Request, user: CurrentUser, record_id: str) -> Response:
    return message_page(
        request,
        title="这次读到的岗位正文不再显示",
        message=UNSHOWN,
        status_code=409,
        user=user,
        links=[(f"{RECORDS}/postings/{record_id}", "返回这条岗位记录"), (RECORDS, "返回岗位记录")],
    )


def _gone(request: Request, user: CurrentUser, record_id: str) -> Response:
    return message_page(
        request,
        title="这份申请前核对的正文已经不在了",
        message=GONE_TEXT,
        status_code=404,
        user=user,
        links=[(f"{RECORDS}/postings/{record_id}", "返回这条岗位记录")],
    )


def _text_lapsed(request: Request, user: CurrentUser, record_id: str, otherwise) -> Response:
    """A 申请前核对's text that reached its record's time as it was to be published or read (U3-D4b-2 F1): the store
    dropped it there and then, by its own clock. What the record is now decides only what is said: 已到期, with what
    memory still holds of it, if its time has come; otherwise (kept again or added to 我的岗位 since the text was read)
    only that the text is no longer shown (otherwise), touching nothing else of it: a 申请前核对 begun since keeps its
    own text."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            lapsed = job_recommendations.lapsed(db, active_job_record(db, user.id, JobPosting, record_id), rt.now())
    except NotFound:
        return _not_found(request, user)

    return _lapsed(request, user, record_id) if lapsed else otherwise(request, user, record_id)


def _check_view(record: JobPosting, checks, rechecks, triggers: dict, analyses, now: datetime) -> dict:
    """What the record page says of its checks: the status now, every check of both kinds, what the latest read found
    changed since the posting was kept, and whether a check can be asked for here."""
    summary = summarize([*checks, *rechecks])
    history = []

    for check in sorted([*checks, *rechecks], key=lambda row: (row.checked_at, row.recorded_at), reverse=True):
        if isinstance(check, JobPostingRecheck):
            origin = f"{TRIGGER_WORDS.get(triggers.get(check.run_id), '核对')} · 记录于 {format_local(check.recorded_at)}"
            history.append((history_line(check, format_local), origin, change_lines(check.changes)))
        else:
            history.append((history_line(check, format_local), f"记录于 {format_local(check.recorded_at)} · 来自查找 {check.search_id}", []))

    latest = updates.latest_read(rechecks)
    kept = updates.kept_version(record)
    deadline = (latest.facts.get("deadline") if latest is not None else kept["deadline"])
    deadline_note = None

    if summary.attempted is not None and not is_success(summary.attempted) and deadline and date.fromisoformat(deadline) < now.astimezone().date():
        deadline_note = (
            f"{'上次读到' if latest is not None else '保存时'}记的截止日期 {deadline} 已过；这次没能确认来源，截止日期可能延期，"
            "所以状态仍是待确认。"
        )

    # The server decides again when a check is asked for (updates.begin); a source the app does not run on now is
    # asked nothing, and the check says so (source_unavailable).
    if record.source_access == job_texts.USER_PROVIDED:
        blocked = updates.PASTED
    elif record.source_access not in updates.TRACKABLE_ACCESS:
        blocked = updates.NOT_TRACKABLE
    elif record.facts.get("evidence") != "official":
        blocked = updates.LEAD
    else:
        blocked = None

    return {
        "check_now": summary_text(summary, format_local),
        "history": history,
        "latest_read": latest,
        "latest_changes": [] if latest is None else change_lines(updates.changes_between(kept, None, latest.facts, None)),
        "deadline_note": deadline_note,
        # A real source's posting (U3-D6): what its status can and cannot show; the judgement itself is unchanged.
        "closure_note": closure_note(record.source_access, deadline),
        "can_check": blocked is None,
        "check_blocked": blocked,
        "stale": {
            row.id
            for row in analyses
            if latest is not None and latest.content_sha256 is not None and row.content_sha256 != latest.content_sha256
        },
    }


@router.get(f"{RECORDS}/searches/{{record_id}}")
def search_page(record_id: str, request: Request, notice: str = "", page: int = 1, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobSearch, record_id)
            analyses = records.analyses_of_search(db, user.id, record.search_id)
            watch = _watch_view(db, user, record, searches_of(request), rt.now(), page)
    except NotFound:
        return _not_found(request, user)

    summary = record.summary

    return render(
        request,
        "job_search.html",
        {
            "area": "jobs",
            "section": "job_records",
            "record": record,
            "summary": summary,
            "scope_lines": _scope_lines(summary["scope"]),
            "search_lines": _search_lines(summary["searches"], summary["sources"]),
            "end_label": presentation.end_label(EndReason(summary["end"])),
            "preference_names": [PREFERENCE_TOPICS[name][0] if name in PREFERENCE_TOPICS else name for name in summary["preference_fields"]],
            "step_lines": _step_lines(summary["steps"]["by_tool"]),
            # Who handled its requests, as its review named them: the simulation, or a real model (D6-1 R3, L2).
            "requests_line": presentation.kept_requests_text(summary),
            "analyses": analyses,
            "watch": watch,
            "notice": WATCH_NOTICES.get(notice),
        },
        user=user,
    )


# --- 每日查新 on a kept summary (U3-D4c) -------------------------------------------------------------------------------
#
# The user starts a watch on a kept summary of theirs (the review says what it repeats, keeps and for how long, and
# whether the app checks by itself), runs it now, or stops and clears it; adds a new posting to 我的岗位 or no longer
# shows it. What a watch runs on and finds is the server's: a form names a summary, a watch or a new posting by id.

WATCH_PAGE = 20
WATCH_TRIGGERS = {"enable": "开启时建立基准", "manual": "现在查新", "scheduled": "定时检查"}
WATCH_OUTCOMES = {
    "complete": "完成：每次检索都得到了回答",
    "partial": "部分完成：有的检索没有得到回答",
    "failed": "没有完成：没有一次检索得到回答",
    "interrupted": "中断（应用关闭或出错）：这一轮什么都没有写",
}
WATCH_REASONS = {
    "calls_limit": "调用次数用完",
    "time_limit": "时间用完",
    "app_closing": "应用关闭",
    "source_stopped": "有来源这一轮停用",
    "error": "出错",
    "not_finished": "应用停止时还没结束",
}
STOP_RUNNING = "这个查新正在运行：停止以后不再放行后续的来源调用；停止前已经放行的那一次可能仍会执行或完成，它的结果不会保存。"


def _auto_text(searches) -> str:
    """What the app does by itself, as it was started now: never more than it does."""
    if searches.update_seconds is None:
        return "现在不会自动检查：开启以后只建立基准，之后要你点“现在查新”才会再查。"

    every = round(searches.update_seconds / 60)

    return f"自动检查已开启：本机应用运行时，每 {every} 分钟看一次哪些查新到期，每个查新每 24 小时一轮；应用关闭期间不会查新。"


def _via_watch(facts: dict) -> str | None:
    via = facts.get("via") if isinstance(facts, dict) else None

    if not isinstance(via, dict) or via.get("kind") != "watch":
        return None

    try:
        return format_local(datetime.fromisoformat(via["first_read_at"]))
    except (KeyError, TypeError, ValueError):
        return "（时间不详）"


def _via_supplied(facts: dict) -> dict | None:
    """How a posting the user added themselves came (U3-D4d): its kind, when, and the link they gave with a text."""
    via = facts.get("via") if isinstance(facts, dict) else None

    if not isinstance(via, dict) or via.get("kind") not in (supplied.LINK, supplied.PASTED):
        return None

    try:
        at = format_local(datetime.fromisoformat(via.get("read_at") or via.get("provided_at")))
    except (TypeError, ValueError):
        at = "（时间不详）"

    return {"kind": via["kind"], "at": at, "link": via.get("link")}


def _item_line(item: "watches.Item", names: dict) -> str:
    shown = "、".join(f"{DIMENSION_LABELS[dimension]} {value}" for dimension, value in item.filters if value is not None)

    return f"{names.get(item.source_id, item.source_id)}：检索词“{item.words}”" + (f"，筛选 {shown}" if shown else "，不加筛选")


def _watch_lines(watch: JobWatch, names: dict) -> list[str]:
    """The last run as it went: its own times, outcome and counts, never a posting's check time."""
    if watch.last_started_at is None:
        return ["还没有运行过，基准还没建立。"]

    trigger = WATCH_TRIGGERS.get(watch.last_trigger, watch.last_trigger)

    if watch.run_token is not None:
        return [f"正在运行：开始于 {format_local(watch.last_started_at)}（{trigger}）。"]

    reasons = "、".join(WATCH_REASONS.get(code, code) for code in (watch.last_reason or "").split(",") if code)
    lines = [
        f"上次查新：开始于 {format_local(watch.last_started_at)}（{trigger}），"
        f"{WATCH_OUTCOMES.get(watch.last_outcome, watch.last_outcome or '没有结果')}" + (f"；原因：{reasons}" if reasons else "") + "。"
    ]
    counts = watch.last_counts or {}

    if counts and watch.last_outcome != watches.INTERRUPTED:
        lines.append(
            f"检索 {counts.get('searches', 0)} 次：得到回答 {counts.get('answered', 0)} 次，没有得到回答 {counts.get('failed', 0)} 次，"
            f"发送前检查没过（没有发出） {counts.get('refused', 0)} 次，没轮到 {counts.get('unreached', 0)} 次；放行的来源调用 {watch.last_calls} 次。"
        )

    if "returned" in counts:
        lines.append(
            f"返回的岗位 {counts['returned']} 个：新增 {counts.get('new', 0)} 个（其中来自没读完的列表 {counts.get('new_partial', 0)} 个）；"
            f"以前见过 {counts.get('seen_before', 0)} 个；第一次得到回答的检索只记基准 {counts.get('baseline', 0)} 个；"
            f"你跳过的 {counts.get('skipped', 0)} 个（不列出）；已在岗位记录里 {counts.get('kept', 0)} 个；"
            f"超出条件 {counts.get('out_of_scope', 0)} 个；同一轮里重复 {counts.get('duplicates', 0)} 个。"
        )

        if counts.get("partial"):
            read = "、".join(f"{names.get(source, source)}只读了前 {number} 个" for source, number in counts["partial"].items())
            lines.append(f"列表没读完（{read}）：新增可能不全；列出的也可能是以前就在、只是没读到的。")

        if counts.get("foreign") or counts.get("unplain"):
            lines.append(f"另有 {counts.get('foreign', 0)} 条记录不属于它的来源、{counts.get('unplain', 0)} 个岗位的编号不合要求，都没有采用。")

    lines.append(f"上次完成：{format_local(watch.completed_at)}。" if watch.completed_at else "还没有一轮完成（每次检索都得到回答）。")

    return lines


def _watch_view(db, user: CurrentUser, record: JobSearch, searches, now: datetime, page: int = 1) -> dict:
    """The 每日查新 block of a summary's page: off (and whether it may start, and why not) or on, its state and a page of
    its new postings; each new posting judged by now, none whose time has come."""
    found = watches.conditions_of(record.summary)
    names = {source.get("source_id"): source.get("name") for source in (found.sources if found else ())}
    view = {"auto": _auto_text(searches), "KEEP_NEW_DAYS": KEEP_NEW_DAYS, "SEEN_THRESHOLD": SEEN_THRESHOLD}
    watch = job_watches.watch_of(db, user.id, record.id)

    if watch is None:
        problem = watches.unavailable(found, searches.sources.keep, searches.watch_register())

        if problem is None and len(job_watches.watches_of(db, user.id)) >= watches.MAX_WATCHES:
            problem = watches.TOO_MANY

        return {**view, "on": False, "problem": problem}

    total = job_watches.new_count(db, watch.id, now)
    pages = max(1, math.ceil(total / WATCH_PAGE))
    page = min(max(page, 1), pages)
    items = job_watches.new_postings(db, watch.id, now, offset=(page - 1) * WATCH_PAGE, limit=WATCH_PAGE)
    last_seen = {
        (row.source_id, row.posting_id): row.last_seen_at
        for row in db.scalars(select(JobWatchSeen).where(JobWatchSeen.watch_id == watch.id, JobWatchSeen.posting_id.in_([item.posting_id for item in items])))
    }
    seen = job_watches.seen_count(db, watch.id)
    searched = len(found.items) if found else 0

    return {
        **view,
        "on": True,
        "watch": watch,
        "items": [(item, last_seen.get((item.source_id, item.posting_id))) for item in items],
        "total": total,
        "page": page,
        "pages": pages,
        "seen": seen,
        "full": seen >= SEEN_THRESHOLD,
        "running": watch.run_token is not None or searches.batches.holds(user.id),
        "baseline": f"已建立（{len(watch.baselined)}/{searched} 次检索），{format_local(watch.baseline_at)}" if watch.baseline_at else "还没建立",
        "lines": _watch_lines(watch, names),
        "searches": [_item_line(item, names) for item in (found.items if found else ())],
        "EVIDENCE_WORDS": EVIDENCE_WORDS,
    }


@router.get(f"{RECORDS}/searches/{{record_id}}/watch")
def watch_review(record_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """What 每日查新 on this summary repeats, keeps, for how long and when, before the user starts it."""
    rt = runtime(request)
    searches = searches_of(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobSearch, record_id)
            on = job_watches.watch_of(db, user.id, record.id) is not None
            mine = len(job_watches.watches_of(db, user.id))
    except NotFound:
        return _not_found(request, user)

    if on:
        return see_other(f"{RECORDS}/searches/{record_id}#watch")

    return _watch_review(request, user, record, searches, mine)


def _watch_review(request: Request, user: CurrentUser, record: JobSearch, searches, mine: int, *, notice: str | None = None, status_code: int = 200) -> Response:
    found = watches.conditions_of(record.summary)
    problem = watches.unavailable(found, searches.sources.keep, searches.watch_register())

    if problem is None and mine >= watches.MAX_WATCHES:
        problem = watches.TOO_MANY

    if problem is not None:
        return message_page(
            request, title="这份摘要现在不能开启每日查新", message=problem, status_code=403, user=user,
            links=[(f"{RECORDS}/searches/{record.id}", "返回这份摘要")],
        )

    names = {source.get("source_id"): source.get("name") for source in found.sources}

    return render(
        request,
        "job_watch.html",
        {
            "area": "jobs",
            "section": "job_records",
            "record": record,
            "scope_lines": _scope_lines(record.summary["scope"]),
            "searches": [_item_line(item, names) for item in found.items],
            "sources": [source.get("name") for source in found.sources],
            # What the start reads, as the running set reads its sources (U3-D6 close-out, F1), and the version of those words.
            "source_lines": presentation.watch_source_lines(searches.sources, [source.get("name") for source in found.sources]),
            "auto": _auto_text(searches),
            "KEEP_NEW_DAYS": KEEP_NEW_DAYS,
            "SEEN_THRESHOLD": SEEN_THRESHOLD,
            "MAX_WATCHES": watches.MAX_WATCHES,
            "notice_version": watches.notice_version_for(searches.sources),
            "notice": notice,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{RECORDS}/searches/{{record_id}}/watch")
def watch_start(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Start 每日查新 as the review showed it, and queue its first run, the baseline, on the user's slot."""
    rt = runtime(request)
    searches = searches_of(request)

    try:
        revision = _revision(form)

        with rt.database.transaction() as db:
            watch_id = watches.enable(
                db, user.id, record_id, revision, form.get("notice_version") or "", rt.now(),
                keep=searches.sources.keep, register=searches.watch_register(), expected=watches.notice_version_for(searches.sources),
            ).id
    except NotFound:
        return _not_found(request, user)
    except records.InputRejected as rejected:
        return message_page(request, title="没有开启", message=rejected.message, status_code=422, user=user, links=[(RECORDS, "返回")])
    except RecordConflict:
        return message_page(
            request, title="没有开启：这份摘要刚刚变了",
            message="你打开页面之后，这份摘要在别处有了变化（可能是另一个标签页）。这次没有开启，请先看最新状态。",
            status_code=409, user=user, links=[(f"{RECORDS}/searches/{record_id}", "查看最新状态")],
        )
    except watches.NoticeChanged:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobSearch, record_id)
            mine = len(job_watches.watches_of(db, user.id))

        return _watch_review(request, user, record, searches, mine, notice="这一页的说明在你打开之后有了变化，这次没有开启；请再看一遍。", status_code=409)
    except NotAllowed as refused:
        return message_page(request, title="没有开启", message=refused.reason, status_code=409, user=user, links=[(f"{RECORDS}/searches/{record_id}", "返回这份摘要")])
    except watches.Unavailable as refused:
        return message_page(request, title="没有开启", message=refused.reason, status_code=403, user=user, links=[(f"{RECORDS}/searches/{record_id}", "返回这份摘要")])

    started = searches.start_watch(rt.database, user.id, watch_id, watches.Trigger.ENABLE, rt.now)

    if started.startswith("batch:"):
        return message_page(request, title="每日查新已开启，但这次没有查", message=f"{started[6:]}；什么都没有发送。", status_code=409, user=user, links=[(f"{RECORDS}/searches/{record_id}", "返回这份摘要")])

    return see_other(f"{RECORDS}/searches/{record_id}?notice=watch_{started}#watch")


@router.post(f"{RECORDS}/searches/{{record_id}}/watch/run")
def watch_run(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """现在查新: one run of the user's watch on this summary, unless one of theirs runs, it ran just now, or it may not."""
    rt = runtime(request)
    searches = searches_of(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobSearch, record_id)
            watch = job_watches.watch_of(db, user.id, record.id)

            if watch is None or watch.id != (form.get("watch_id") or ""):
                raise NotFound()

            why = watches.startable(db, watch, watches.Trigger.MANUAL, rt.now(), keep=searches.sources.keep, register=searches.watch_register())
            watch_id = watch.id
    except NotFound:
        return _not_found(request, user)

    if why is not None:
        return see_other(f"{RECORDS}/searches/{record_id}?notice=" + {"running": "watch_run_busy"}.get(why, f"watch_{why}") + "#watch")

    started = searches.start_watch(rt.database, user.id, watch_id, watches.Trigger.MANUAL, rt.now)

    if started.startswith("batch:"):
        return message_page(request, title="没有开始查新", message=f"{started[6:]}；什么都没有发送。", status_code=409, user=user, links=[(f"{RECORDS}/searches/{record_id}", "返回这份摘要")])

    return see_other(f"{RECORDS}/searches/{record_id}?notice=watch_run_{started}#watch")


@router.get(f"{RECORDS}/searches/{{record_id}}/watch/stop")
def watch_stop_page(record_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobSearch, record_id)
            watch = job_watches.watch_of(db, user.id, record.id)

            if watch is None:
                raise NotFound()

            seen = job_watches.seen_count(db, watch.id)
            new = job_watches.new_count(db, watch.id, rt.now())
            running = watch.run_token is not None
    except NotFound:
        return _not_found(request, user)

    points = [
        f"将删除：这份摘要的每日查新、它见过的岗位 {seen} 个、还没处理的新增岗位 {new} 个。直接删除，不进回收站，不能恢复。",
        "已经加入我的岗位的、你手动保存的其他内容都不受影响；这份查找摘要也不变。",
        "以后再开启，是一个新的查新，会重新建立基准。",
    ]

    if running:
        points.insert(1, STOP_RUNNING)

    return _confirm(
        request,
        user,
        title="停止并清除每日查新",
        points=points,
        action=f"{RECORDS}/searches/{record.id}/watch/stop",
        button="停止并清除",
        cancel=f"{RECORDS}/searches/{record.id}#watch",
        hidden={"watch_id": watch.id},
    )


@router.post(f"{RECORDS}/searches/{{record_id}}/watch/stop")
def watch_stop(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """停止并清除: the watch named, removed with all it holds; its commit is where it stops."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            watches.stop(db, user.id, record_id, form.get("watch_id") or "", rt.now())
    except NotFound:
        return _not_found(request, user)

    return see_other(f"{RECORDS}/searches/{record_id}?notice=watch_stopped#watch")


@router.post(f"{RECORDS}/watch-postings/{{item_id}}/keep")
def watched_keep(item_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """加入我的岗位: a new posting of 每日查新, made a kept posting from what the watch kept."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = records.keep_watched(db, user.id, item_id, rt.now(), forget=searches_of(request).apply_checks.forget_record)
            record_id = row.id
    except NotFound:
        return _watched_gone(request, user)
    except records.InputRejected as rejected:
        return message_page(request, title="没有加入", message=rejected.message, status_code=422, user=user, links=[(RECORDS, "返回岗位记录")])
    except records.KeepRefused as refused:
        return message_page(request, title="没有加入", message=refused.reason, status_code=403, user=user, links=[(RECORDS, "返回岗位记录")])

    return see_other(f"{RECORDS}/postings/{record_id}?notice=watch_kept")


@router.post(f"{RECORDS}/watch-postings/{{item_id}}/dismiss")
def watched_dismiss(item_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """不再显示 a new posting of 每日查新; its id stays seen."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            search_record_id = records.dismiss_watched(db, user.id, item_id, rt.now())
    except NotFound:
        return _watched_gone(request, user)

    return see_other(f"{RECORDS}/searches/{search_record_id}?notice=watch_dismissed#watch")


def _watched_gone(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="这条新增岗位已经不在了",
        message=f"新增岗位从第一次读到起只保留 {KEEP_NEW_DAYS} 天；它可能已经到期、已经处理过、所在的查新已经停止并清除，或者属于其他用户。",
        status_code=404,
        user=user,
        links=[(RECORDS, "返回岗位记录")],
    )


# --- kept analyses (U3-D3b) ---------------------------------------------------------------------------------------------

@router.get(f"{RECORDS}/analyses/{{record_id}}")
def analysis_page(record_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return _analysis_view(request, user, record_id, notice=NOTICES.get(notice))


def _analysis_view(request: Request, user: CurrentUser, record_id: str, *, notice=None, error=None, status_code=200) -> Response:
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, JobAnalysis, record_id)
            basis = records.basis_now(db, user.id, record, rt.now())
            feedback = records.analysis_feedback_of(db, record)
            titles = _source_titles(db, user.id, record.content["sources"], rt.now())
            posting = records.kept_states(db, user.id, [_ref(record)], rt.now()).get((record.source_id, record.posting_id))
            summary = records.kept_search(db, user.id, record.search_id)
            # The posting as a check from its record last read it (U3-D4a): another text means the analysis may be
            # out of date. Only said; the analysis is never changed or made again here.
            latest = None if posting is None else updates.latest_read(updates.rechecks_of(db, posting))
    except NotFound:
        return _not_found(request, user)

    content = record.content
    stale = latest if latest is not None and latest.content_sha256 is not None and latest.content_sha256 != record.content_sha256 else None

    return render(
        request,
        "job_analysis.html",
        {
            "area": "jobs",
            "section": "job_records",
            "record": record,
            "facts": record.facts,
            "content": content,
            "analysis": records.shown_analysis(content),
            "generation": record.generation,
            "category_text": RESULT_CATEGORIES.get(Category(content["category"]), content["category"]),
            "basis": basis,
            "basis_text": basis_text,
            "source_lines": _source_lines(content["sources"], titles, basis),
            "posting": posting,
            "stale_since": None if stale is None else format_local(stale.checked_at),
            "summary": summary,
            "feedback": feedback,
            "parts": records.analysis_parts(content),
            "ANALYSIS_REASONS": ANALYSIS_REASONS,
            "analysis_feedback_label": analysis_feedback_label,
            "NOTE_CHARS": records.NOTE_CHARS,
            "notice": notice,
            "error": error,
            "synthetic": record.source_access == Access.SYNTHETIC.value,
            "iso_local": lambda value: format_local(datetime.fromisoformat(value)),
            "EVIDENCE_WORDS": EVIDENCE_WORDS,
            "CHECK_WORDS": CHECK_WORDS,
            # What finding_row and analysis_body read, as the result page gives them.
            "titles": titles,
            "versions": {source["item_id"]: source["version_number"] for source in content["sources"]},
            "conclusion_label": analysis_text.conclusion_label,
            "preference_label": analysis_text.preference_label,
            "reason_labels": analysis_text.reason_labels,
            "BASIS_LABELS": analysis_text.BASIS_LABELS,
            "PREFERENCE_TOPICS": PREFERENCE_TOPICS,
            "Group": RequirementGroup,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{RECORDS}/analyses/{{record_id}}/feedback")
def analysis_feedback(record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    rt = runtime(request)

    try:
        revision = _revision(form)

        with rt.database.transaction() as db:
            records.feedback_on_analysis(
                db, user.id, record_id, revision, form.get("part", ""), form.get("analysis_reason", ""), form.get("note", ""), rt.now()
            )
    except NotFound:
        return _not_found(request, user)
    except records.KeepRefused as refused:
        return _refused(request, user, refused.reason, f"{RECORDS}/analyses/{record_id}")
    except records.InputRejected as rejected:
        return _analysis_view(request, user, record_id, error=rejected.message, status_code=422)
    except RecordConflict:
        return _analysis_view(request, user, record_id, error=CONFLICT, status_code=409)

    return see_other(f"{RECORDS}/analyses/{record_id}?notice=analysis_feedback")


@dataclass(frozen=True)
class SourceTitle:
    """What a page calls a record a kept analysis rests on: its title now, as a label only, or that it is gone."""

    title: str


def _source_titles(db, owner_id: str, sources: list[dict], now: datetime) -> dict[str, SourceTitle]:
    """Each record's title as it reads now, only to name it; one deleted for good is named so, never guessed."""
    titles = {}

    for source in sources:
        item = db.scalar(select(ExperienceItem).where(ExperienceItem.id == source["item_id"], ExperienceItem.owner_id == owner_id))

        if item is None or is_expired(item, now) or not isinstance(item.content, dict):
            title = "已永久删除的经历"
        else:
            title = display_title(item.content, item.category)

        titles[source["item_id"]] = SourceTitle(title)

    return titles


def _source_lines(sources: list[dict], titles: dict, basis) -> list[str]:
    lines = []

    for source in sources:
        state = basis.states.get(("experience", source["item_id"]))
        fields = "、".join(field_label(source["category"], name) for name in source["fields"])
        lines.append(
            f"{titles[source['item_id']].title}（{CATEGORY_LABELS.get(source['category'], source['category'])}，确认版 {source['version_number']}）"
            f"：{fields or '全部已填栏目'} · {basis_text(state) if state is not None else '依据状态未知'}"
        )

    for (kind, _), state in basis.states.items():
        if kind == "preferences":
            lines.append(f"求职偏好 · {basis_text(state)}")

    return lines


def _basis_lines(task, item) -> list[str]:
    """What a result's analysis rests on, as its keep page says it: each experience by its title and confirmed version
    and the fields sent, and the preferences by topic; never their words."""
    titles = {source.entry.item_id: source.title for source in task.draft.material.sources}
    lines = [
        f"{titles.get(basis.item_id, '一段经历')}（{CATEGORY_LABELS.get(basis.category, basis.category)}，确认版 {basis.version_number}）："
        + ("、".join(field_label(basis.category, name) for name in basis.fields) or "全部已填栏目")
        for basis in item.analysis.sources
    ]

    if task.confirmation.preferences:
        lines.append("求职偏好：" + "、".join(_topic(preference.field) for preference in task.confirmation.preferences))

    return lines


def _topic(field: str) -> str:
    """A preferences field as the result page names it: its topic, and its stance where it has one."""
    if field not in PREFERENCE_TOPICS:
        return field

    topic, _, stance, _ = PREFERENCE_TOPICS[field]

    return f"{topic}（{stance}）" if stance else topic


def _ref(record):
    return records.PostingRef(record.source_id, record.posting_id)


# --- the recycle bin ----------------------------------------------------------------------------------------------------

KINDS = {
    "postings": (JobPosting, "这条岗位记录", "岗位记录：它的事实、核对记录、决定和指出的不准"),
    "searches": (JobSearch, "这份查找摘要", "这份查找摘要"),
    "analyses": (JobAnalysis, "这份岗位分析", "岗位分析：它的分类、各条判断、引文和上下文，以及指出的不准"),
}


@router.get(f"{RECORDS}/{{kind}}/{{record_id}}/delete")
def delete_page(kind: str, record_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    model, noun, what = _kind(kind)
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            record = active_job_record(db, user.id, model, record_id)
            held = model is JobPosting and job_recommendations.marker_of(db, record.id) is not None
            watch = job_watches.watch_of(db, user.id, record.id) if model is JobSearch else None
            watched = None if watch is None else (job_watches.seen_count(db, watch.id), job_watches.new_count(db, watch.id, rt.now()))
    except NotFound:
        return _not_found(request, user)

    if held:
        # Not the user's record: removed by 不再跟踪, never through the bin (U3-D4b-2).
        return message_page(
            request,
            title="这条推荐不进回收站",
            message=HELD_REASON,
            status_code=409,
            user=user,
            links=[(f"{RECORDS}/postings/{record.id}", "返回")],
        )

    return _confirm(
        request,
        user,
        title=f"删除{noun}",
        points=[
            f"将删除：{what}。",
            "7 天内可以在回收站恢复，也可以提前永久删除；到期后会被永久删除，不能再恢复或导出。",
            *(
                [
                    f"这份摘要开启了每日查新：它会一并停止并清除（见过的岗位 {watched[0]} 个、还没处理的新增岗位 {watched[1]} 个），"
                    "直接删除，不进回收站；恢复这份摘要也不会恢复查新。你加入我的岗位的不受影响。"
                ]
                if watched is not None
                else []
            ),
            "已经下载到本机的导出文件不受影响，需要你自己处理。",
        ],
        action=f"{RECORDS}/{kind}/{record.id}/delete",
        button="移到回收站",
        cancel=f"{RECORDS}/{kind}/{record.id}",
        hidden={"expected_revision": str(record.revision)},
    )


@router.post(f"{RECORDS}/{{kind}}/{{record_id}}/delete")
def delete(kind: str, record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    model, _, _ = _kind(kind)

    def act(db, now, revision):
        delete_job_record(db, user.id, model, record_id, revision, now)

        if model is JobPosting:
            # The text a 申请前核对 read of it goes as soon as the record is deleted (U3-D4a), and never comes back; so does
            # what memory holds of the supply that made it (U3-D4d), which then never makes another.
            searches_of(request).apply_checks.forget_record(record_id)
            searches_of(request).supply.forget_record(record_id)

    return _record_action(request, user, form, act, f"{RECORDS}?notice=deleted")


@router.post(f"{RECORDS}/{{kind}}/{{record_id}}/restore")
def restore(kind: str, record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    model, _, _ = _kind(kind)

    return _record_action(
        request,
        user,
        form,
        lambda db, now, revision: restore_job_record(db, user.id, model, record_id, revision, now),
        safe_next(form.get("next", "")) or f"{RECORDS}?notice=restored",
        back=TRASH,
    )


@router.get(f"{RECORDS}/{{kind}}/{{record_id}}/purge")
def purge_page(kind: str, record_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    model, noun, what = _kind(kind)
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            record = trashed_job_record(db, user.id, model, record_id, rt.now())
    except NotFound:
        return _not_found(request, user)

    return _confirm(
        request,
        user,
        title=f"永久删除{noun}",
        points=[
            f"{what}会被永久删除，不能恢复，也不会再出现在导出里。",
            "已经下载到本机的导出文件不受影响，需要你自己处理。",
        ],
        action=f"{RECORDS}/{kind}/{record.id}/purge",
        button="永久删除",
        cancel=TRASH,
        hidden={"expected_revision": str(record.revision)},
    )


@router.post(f"{RECORDS}/{{kind}}/{{record_id}}/purge")
def purge(kind: str, record_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    model, _, _ = _kind(kind)

    return _record_action(
        request, user, form, lambda db, now, revision: request_job_record_purge(db, user.id, model, record_id, revision, now), TRASH, back=TRASH
    )


def _record_action(request: Request, user: CurrentUser, form: FormData, act, done: str, back: str = RECORDS) -> Response:
    rt = runtime(request)

    try:
        revision = _revision(form)

        with rt.database.transaction() as db:
            act(db, rt.now(), revision)
    except NotFound:
        return _not_found(request, user)
    except NotAllowed as refused:
        return message_page(request, title="这个操作现在不能执行", message=refused.reason, status_code=409, user=user, links=[(back, "返回")])
    except records.InputRejected as rejected:
        return message_page(request, title="这个操作没有执行", message=rejected.message, status_code=422, user=user, links=[(back, "返回")])
    except RecordConflict:
        return message_page(
            request,
            title="操作没有执行：这条记录刚刚变了",
            message="你打开页面之后，这条记录在别处改过（可能是另一个标签页）。为了不处理你没有看到的变化，这次操作没有执行。请先看最新状态。",
            status_code=409,
            user=user,
            links=[(back, "查看最新状态")],
        )

    return see_other(done)


# --- small helpers ----------------------------------------------------------------------------------------------------


def _kind(kind: str):
    if kind not in KINDS:
        raise NotFound()

    return KINDS[kind]


def _revision(form: FormData) -> int:
    value = (form.get("expected_revision") or form.get("revision") or "").strip()

    if not value.isdigit():
        raise records.InputRejected("页面信息不完整，请刷新页面后再试。")

    return int(value)


def _choices() -> dict:
    return {
        "DECISIONS": DECISIONS,
        "FEEDBACK_FIELDS": FEEDBACK_FIELDS,
        "FEEDBACK_REASONS": FEEDBACK_REASONS,
        "NOTE_CHARS": records.NOTE_CHARS,
    }


def _confirm(request: Request, user: CurrentUser, *, title: str, points: list[str], action: str, button: str, cancel: str, hidden: dict) -> Response:
    return render(
        request,
        "confirm.html",
        {
            "area": "jobs",
            "section": "job_records",
            "title": title,
            "points": points,
            "action": action,
            "button": button,
            "cancel": cancel,
            "hidden": hidden,
            "acknowledge": None,
        },
        user=user,
    )


def _not_found(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这条记录",
        message="它可能已经删除、超过了恢复期限、属于其他用户，或者是临时留存的推荐，已经到期、被替换或不再跟踪。",
        status_code=404,
        user=user,
        links=[(RECORDS, "返回岗位记录")],
    )


def _scope_lines(scope: dict) -> list[tuple[str, str]]:
    """The scope as it was confirmed, in the very words its review said it (U3-D3a F3): each dimension's values with
    the ideal ones marked, what was not acceptable, and whether values outside were searched too."""
    kept = SearchScope(
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

    return presentation.scope_lines(kept)


def _search_lines(entries: list[dict], sources: list[dict]) -> list[str]:
    """The searches as kept, in the very words the result page said them (U3-D3a F2, U3-D5): a list read only in part
    said in full once."""
    names = {source["source_id"]: source["name"] for source in sources}

    return presentation.search_lines([_search_record(entry) for entry in entries], names)


def _search_line(entry: dict, sources: list[dict]) -> str:
    """One search as kept, in the very words the result page said it (U3-D3a F2): answered with how many, failed and
    why, or answered after the search had stopped; read from the list read before, or read in part."""
    names = {source["source_id"]: source["name"] for source in sources}

    return presentation.search_line(_search_record(entry), names)


def _search_record(entry: dict) -> SearchRecord:
    return SearchRecord(
        source_id=entry["source_id"],
        keywords=entry["keywords"],
        filters=tuple((Dimension(name), value) for name, value in entry["filters"].items()),
        found=entry["found"],
        outcome=Outcome(entry["outcome"]),
        unverifiable=entry["unverifiable"],
        foreign=entry["foreign"],
        listed=entry["listed"],
        partial=entry["partial"],
        reused=entry["reused"],
        # Kept since summary version 2; an older summary says no more than "没有完成".
        failure=entry.get("failure"),
    )


def _found_check_lines(item, note: str, kept: bool = False) -> list[str]:
    """What the search knew of a posting's checks (U3-D3a F1): the last answer and what it said, and apart, the latest
    check without an answer. A status is never guessed from a time or from why.

    R10 (U3-D6-3 F3): they are what this search's own checks said, and are said as such ("本次查找时"), never as the
    posting's last check: a later one (现在核对, 再核对一次) is kept in its record. With a record (kept), the lines say
    where the later ones are. Nothing is read from a source to show this."""
    lines = []

    if item.checked_at is not None:
        said = CHECK_WORDS[records.ANSWER_WORDS.get(item.answered_status, CHECK_UNKNOWN)]
        lines.append(f"本次查找时最后一次有回答：{presentation.checked_text(item.checked_at, note=note)}，{said}")

    if item.check_failed_at is not None:
        lines.append(f"本次查找时最近一次核对没有得到回答：{format_short(item.check_failed_at)}，状态待确认")

    lines = lines or ["这次查找没有得到回答的核对"]

    if kept:
        lines.append("查找以后的核对记在岗位记录里，以记录为准")

    return lines


def _step_lines(by_tool: dict) -> list[str]:
    """Each kind of step and how often each outcome came of it: counts only, never what a decider wrote."""
    tools = {tool.value: label for tool, label in presentation.TOOL_LABELS.items()}

    return [
        f"{tools.get(tool, '没有可用的动作')}："
        + "、".join(f"{presentation.STEP_OUTCOMES.get(outcome, outcome)} {count} 次" for outcome, count in sorted(outcomes.items()))
        for tool, outcomes in sorted(by_tool.items())
    ]
