"""Pages of 岗位机会 → 岗位分析 (U2-A2).

    /job-analysis                     choose experiences, paste a posting
    /job-analysis/review        POST  work out the scope, keep it as a task
    /job-analysis/tasks/<id>          the review, the running task or its result
    /job-analysis/tasks/<id>/confirm  POST  update the scope, or confirm and start
    /job-analysis/tasks/<id>/cancel   POST
    /job-analysis/tasks/<id>/again    POST  review the same input again, as a new task
    /job-analysis/tasks/<id>/status   the running task's step, for the page's script

Every page and post is the signed-in user's, and every post carries the
session's CSRF token and comes from this site (app.library.web). A GET never
sends anything to a service; only confirming a reviewed task starts one,
once (tasks.TaskStore.begin). A task is the user's and the session's: an id
alone opens nothing.
"""
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData

from app.analysis_contracts import AnalysisStatus, RequirementGroup, task_fingerprint
from app.experience_analysis import PREFERENCE_TOPICS
from app.job_analysis import presentation
from app.job_analysis.runner import run_task
from app.job_analysis.scope import (
    BLOCKED,
    LEFT_OUT,
    MAX_EXPERIENCES,
    MAX_OUTBOUND_CHARS,
    MAX_POSTING_CHARS,
    PREFERENCES_KEY,
    SEND,
    InputProblem,
    TaskInputs,
    build_plan,
    build_request,
)
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.job_analysis.tasks import SWEEP_SECONDS, TASK_TTL_SECONDS, Stage, Start, TaskStore, TooManyTasks
from app.library.content import CATEGORY_LABELS, EXPERIENCE_CATEGORIES
from app.library.errors import NotFound
from app.library.identity import CurrentUser
from app.library.projection import current_preferences, project_confirmed_library, read_selected_confirmations
from app.library.service import list_experiences
from app.library.web import NOTICES as LIBRARY_NOTICES
from app.library.web import CsrfRejected, check_same_origin, current_user, message_page, render, runtime, safe_next, see_other

BASE = "/job-analysis"
# The version of what the review page says about a task; part of its fingerprint.
NOTICE_VERSION = "1"
MAX_FORM_FIELDS = 300
NOTICES = {
    "already": "这次确认已经开始过，没有重复执行。",
    "scope_updated": "已按你的选择更新核对内容，请再看一遍后确认。",
    "scope_changed": "你改动了发送范围，所以还没有开始。下面已按新的范围更新，请再看一遍后确认。",
}

router = APIRouter()


@dataclass
class JobAnalysisRuntime:
    store: TaskStore
    executor: ThreadPoolExecutor
    # Only for trying the pages locally (web_main --simulation-delay).
    simulation_delay: float = 0.0
    # How often the app's lifespan sweeps the store (web_main).
    sweep_seconds: float = SWEEP_SECONDS
    # The lifespan's sweeping task, while the app runs.
    sweeper: object = None

    def make_provider(self, task):
        # The simulated service waits on the task's cancel signal, so cancelling ends its wait.
        return SimulatedProvider(self.simulation_delay, stop=task.cancel)

    def end_session(self, session: str) -> None:
        self.store.end_session(session)

    def sweep(self) -> None:
        self.store.sweep()

    def shutdown(self) -> None:
        self.store.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)


def install_job_analysis(
    app: FastAPI,
    *,
    simulation_delay: float = 0.0,
    sweep_seconds: float = SWEEP_SECONDS,
    clock=None,
    executor=None,
) -> JobAnalysisRuntime:
    jobs = JobAnalysisRuntime(
        store=TaskStore(clock or time.monotonic),
        executor=executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="job-analysis"),
        simulation_delay=simulation_delay,
        sweep_seconds=sweep_seconds,
    )
    app.state.job_analysis = jobs
    # Logging out, or signing in as someone else, ends the session's tasks.
    app.state.library.session_end_listeners.append(jobs.end_session)
    app.include_router(router)

    if simulation_delay:
        app.state.library.templates.env.globals["simulation_note"] = (
            f"模拟服务每一步会故意等待 {simulation_delay:g} 秒：只用于在本地查看等待、取消和迟到的回答。"
        )

    return jobs


def jobs_of(request: Request) -> JobAnalysisRuntime:
    return request.app.state.job_analysis


async def submitted_fields(request: Request) -> FormData:
    """A form post with repeated names (the chosen experiences, the fields to send)."""
    form = await request.form(max_files=0, max_fields=MAX_FORM_FIELDS)

    if any(not isinstance(value, str) for _, value in form.multi_items()):
        raise HTTPException(status_code=400)

    return form


def checked_fields(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: FormData = Depends(submitted_fields),
) -> FormData:
    check_same_origin(request)
    token = form.get("csrf_token")

    if not user.csrf_token or not token or not secrets.compare_digest(user.csrf_token.encode(), token.encode()):
        raise CsrfRejected()

    return form


# --- the input page ------------------------------------------------------------------


@router.get(BASE)
def input_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    inputs = TaskInputs(chosen=(), posting="", use_preferences=False)
    origin = request.query_params.get("from", "")

    if origin:
        task = jobs_of(request).store.get(origin, user.id, user.token_hash)

        if task is not None:
            inputs = task.inputs

    # Saving from the leave dialog of a library form can land here: say what was saved.
    return input_form(request, user, inputs, notice=LIBRARY_NOTICES.get(notice))


def input_form(
    request: Request,
    user: CurrentUser,
    inputs: TaskInputs,
    problem: InputProblem | None = None,
    *,
    notice: str | None = None,
) -> Response:
    """The input page with what the user entered; a choice whose version is gone is not ticked again."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        snapshot = project_confirmed_library(db, user.id, rt.now())
        unconfirmed = len(list_experiences(db, user.id)) - len(snapshot.experiences)
        has_preferences = bool(current_preferences(db, user.id))

    offered = {(entry.item_id, entry.version_id) for entry in snapshot.experiences}
    ticked = {item_id for item_id, version_id in inputs.chosen if (item_id, version_id) in offered}
    dropped = len(inputs.chosen) - len(ticked)

    return render(
        request,
        "job_input.html",
        {
            "area": "jobs",
            "experiences": snapshot.experiences,
            "unconfirmed": unconfirmed,
            "has_preferences": has_preferences,
            "inputs": inputs,
            "ticked": ticked,
            "dropped": dropped if problem is None else 0,
            "problem": problem,
            "notice": notice,
            "error": problem.message if problem else None,
            "unsaved_input": problem is not None and bool(inputs.posting.strip()),
            "MAX_POSTING_CHARS": MAX_POSTING_CHARS,
            "MAX_EXPERIENCES": MAX_EXPERIENCES,
            "CATEGORY_LABELS": CATEGORY_LABELS,
            "categories": EXPERIENCE_CATEGORIES,
        },
        user=user,
        status_code=422 if problem else 200,
    )


def _chosen(values: list[str]) -> tuple[tuple[str, str], ...]:
    chosen = []

    for value in values:
        item_id, _, version_id = value.partition(":")

        if not item_id or not version_id:
            raise HTTPException(status_code=400)

        chosen.append((item_id, version_id))

    return tuple(chosen)


@router.post(f"{BASE}/review")
def review(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    inputs = TaskInputs(
        chosen=_chosen(form.getlist("source")),
        posting=form.get("posting", ""),
        use_preferences=form.get("use_preferences") == "yes",
    )

    return new_review(request, user, inputs)


def new_review(request: Request, user: CurrentUser, inputs: TaskInputs, *, replaces: str | None = None, notice: str = "") -> Response:
    """Work out the scope of the input, keep it as a new task and show its review."""
    rt = runtime(request)
    jobs = jobs_of(request)

    try:
        with rt.database.transaction() as db:
            plan = build_plan(db, user.id, inputs)
    except InputProblem as problem:
        return input_form(request, user, inputs, problem)

    analysis_request = build_request(user.id, plan, RECIPIENT, NOTICE_VERSION)

    try:
        task = jobs.store.create(
            user.id, user.token_hash, inputs, plan, analysis_request, task_fingerprint(analysis_request)
        )
    except TooManyTasks:
        return message_page(
            request,
            title="暂时不能开始新的分析",
            message="服务器上正在保留的临时分析太多了。请稍后再试，或先取消你不需要的分析。",
            status_code=503,
            user=user,
            links=[(BASE, "返回岗位分析")],
        )

    if replaces:
        jobs.store.discard(replaces, user.id, user.token_hash)

    return see_other(f"{BASE}/tasks/{task.id}" + (f"?notice={notice}" if notice else ""))


# --- one task --------------------------------------------------------------------------


def _task(request: Request, user: CurrentUser, task_id: str):
    task = jobs_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        raise NotFound()

    return task


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这次分析",
        message=(
            "它可能已经过期（临时结果保留 30 分钟）、服务已重启、你已退出登录，或者它属于其他会话。"
            "需要的话，请重新开始一次分析。"
        ),
        status_code=404,
        user=user,
        links=[(BASE, "返回岗位分析")],
    )


@router.get(f"{BASE}/tasks/{{task_id}}")
def task_page(task_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is Stage.REVIEW:
        return review_page(request, user, task, notice=NOTICES.get(notice))

    context = {
        "area": "jobs",
        "task": task,
        "notice": NOTICES.get(notice),
        "STEP_LABELS": presentation.STEP_LABELS,
        "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
    }

    if task.stage is Stage.DONE:
        context.update(result_context(request, user, task))

    if task.stage is Stage.CANCELLED:
        context["cancelled_by"] = presentation.CANCELLED_BY.get(task.cancelled_by, "")

    return render(request, "job_task.html", context, user=user)


def review_page(request: Request, user: CurrentUser, task, *, notice=None, error=None, status_code=200) -> Response:
    return render(
        request,
        "job_review.html",
        {
            "area": "jobs",
            "task": task,
            "plan": task.plan,
            "notice": notice,
            "error": error,
            "SEND": SEND,
            "LEFT_OUT": LEFT_OUT,
            "BLOCKED": BLOCKED,
            "PREFERENCES_KEY": PREFERENCES_KEY,
            "CATEGORY_LABELS": CATEGORY_LABELS,
            "found_labels": presentation.found_labels,
            "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
            "MAX_OUTBOUND_CHARS": MAX_OUTBOUND_CHARS,
        },
        user=user,
        status_code=status_code,
    )


def _sendable(plan) -> set[str]:
    keys = {
        f"{source.entry.item_id}:{field.name}"
        for source in plan.sources
        for field in source.fields
        if field.status != BLOCKED
    }

    return keys | {f"{PREFERENCES_KEY}:{item.field}" for item in plan.preferences if item.status != BLOCKED}


def _sent(plan) -> set[str]:
    keys = {f"{source.entry.item_id}:{name}" for source in plan.sources for name in source.sent}

    return keys | {f"{PREFERENCES_KEY}:{item.field}" for item in plan.sent_preferences}


@router.post(f"{BASE}/tasks/{{task_id}}/confirm")
def confirm(
    task_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: FormData = Depends(checked_fields),
):
    """Update the scope, or confirm it and start: the only post that starts a task.

    Fields the form unticked since the review are never sent: a start whose
    ticks differ from the reviewed scope becomes a new review instead.
    """
    jobs = jobs_of(request)

    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is not Stage.REVIEW:
        notice = "" if task.stage is Stage.CANCELLED else "?notice=already"

        return see_other(f"{BASE}/tasks/{task.id}{notice}")

    sendable = _sendable(task.plan)
    ticked = set(form.getlist("send")) & sendable

    if ticked != _sent(task.plan):
        inputs = replace(task.inputs, left_out=frozenset(sendable - ticked))
        notice = "scope_changed" if form.get("action") == "start" else "scope_updated"

        return updated_review(request, user, task, inputs, notice)

    if form.get("action") != "start":
        return see_other(f"{BASE}/tasks/{task.id}")

    outcome, started = jobs.store.begin(task.id, user.id, user.token_hash, form.get("fingerprint", ""))

    if outcome is Start.STARTED:
        rt = runtime(request)
        jobs.executor.submit(run_task, jobs.store, rt.database, started.id, jobs.make_provider)
        return see_other(f"{BASE}/tasks/{started.id}")

    if outcome is Start.ALREADY:
        return see_other(f"{BASE}/tasks/{task.id}?notice=already")

    if outcome is Start.BUSY:
        return review_page(
            request,
            user,
            task,
            error="你已经有一个分析正在进行。请等它结束，或者取消它之后再开始。",
            status_code=409,
        )

    if outcome is Start.STALE_FORM:
        return review_page(
            request,
            user,
            task,
            error="这个确认来自旧的核对页面，所以没有开始。请看一遍下面最新的核对内容后再确认。",
            status_code=409,
        )

    return gone_page(request, user)


def updated_review(request: Request, user: CurrentUser, task, inputs: TaskInputs, notice: str) -> Response:
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            build_plan(db, user.id, inputs)
    except InputProblem as problem:
        return review_page(request, user, task, error=problem.message, status_code=422)

    return new_review(request, user, inputs, replaces=task.id, notice=notice)


@router.post(f"{BASE}/tasks/{{task_id}}/cancel")
def cancel(
    task_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: FormData = Depends(checked_fields),
):
    task = jobs_of(request).store.cancel(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    # The task's own page says what cancelling did and did not stop.
    return see_other(safe_next(form.get("next", "")) or f"{BASE}/tasks/{task.id}")


@router.post(f"{BASE}/tasks/{{task_id}}/again")
def again(
    task_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: FormData = Depends(checked_fields),
):
    """Review the same input once more, read again from the library, as a new task to confirm."""
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    return new_review(request, user, task.inputs)


@router.get(f"{BASE}/tasks/{{task_id}}/status")
def task_status(task_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    task = jobs_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return JSONResponse({"stage": "gone"}, status_code=404)

    return JSONResponse(
        {
            "stage": task.stage.value,
            "step": presentation.STEP_LABELS[task.step] if task.step else None,
            "requests_sent": task.requests_sent,
        }
    )


# --- the result ------------------------------------------------------------------------


def result_context(request: Request, user: CurrentUser, task) -> dict:
    """What the result page shows. It reads the chosen material again on every view:
    a result whose material changed since is never shown as the current analysis."""
    result = task.result
    titles = {source.entry.item_id: source for source in task.plan.sources}
    changed = False

    if result.status is AnalysisStatus.COMPLETE:
        rt = runtime(request)

        with rt.database.transaction() as db:
            chosen = {source.item_id: source.version_id for source in task.request.selection}
            _, problems = read_selected_confirmations(db, user.id, chosen)
            saved = current_preferences(db, user.id) if task.request.preferences else {}

        changed = bool(problems) or any(saved.get(item.field) != item.text for item in task.request.preferences)

    findings = result.findings
    groups = {
        group: [finding for finding in findings if finding.group is group]
        for group in (RequirementGroup.REQUIRED, RequirementGroup.PREFERRED, RequirementGroup.UNCERTAIN)
    }

    return {
        "result": result,
        "changed_since": changed,
        "outcome": presentation.outcome_message(result),
        "conditions": [finding for finding in findings if finding.group is RequirementGroup.HARD_CONSTRAINT],
        "skill_groups": groups,
        "titles": titles,
        "versions": {source.item_id: source.version_number for source in result.sources},
        "score_text": presentation.score_text,
        "conclusion_label": presentation.conclusion_label,
        "preference_label": presentation.preference_label,
        "reason_labels": presentation.reason_labels,
        "BASIS_LABELS": presentation.BASIS_LABELS,
        "GROUP_HEADINGS": presentation.GROUP_HEADINGS,
        "PREFERENCE_TOPICS": PREFERENCE_TOPICS,
        "SELECTION_PROBLEMS": presentation.SELECTION_PROBLEMS,
        "Status": AnalysisStatus,
        "Group": RequirementGroup,
    }
