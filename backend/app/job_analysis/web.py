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

The service is the simulated one unless the app was built with a real one
(app.job_analysis.service, U2-C2). A task binds the service's recipient and
notice version in its fingerprint; confirming a task reviewed under another
service or notice starts nothing and reviews it afresh. While the real
service's gate refuses to start (stopped, paused, no request left), the
review offers no start, and a start posted anyway is refused.
"""
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from starlette.datastructures import FormData

from app.analysis_contracts import AnalysisStatus, RequirementGroup, task_fingerprint
from app.job_analysis import presentation
from app.job_analysis.runner import run_task
from app.job_analysis.service import LiveService, local_provider
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
from app.library import job_texts
from app.library.errors import NotFound
from app.library.identity import CurrentUser
from app.library.projection import current_preferences, project_confirmed_library, read_selected_confirmations
from app.library.service import list_experiences
from app.library.sends import sends_text
from app.library.web import NOTICES as LIBRARY_NOTICES
from app.library.web import CsrfRejected, check_same_origin, current_user, message_page, render, runtime, safe_next, see_other
from app import i18n
from app.discovery import records as job_records
from app.discovery.supplied import LINK
from app.i18n import MessageMap, lazy, t
from app.library.lifecycle import active_job_record
from app.library.models import JobAnalysis, JobPosting

BASE = "/job-analysis"
# The version of what the review page says about a task; part of its fingerprint.
NOTICE_VERSION = "1"
MAX_FORM_FIELDS = 300
NOTICES = MessageMap({
    "analysis_saved": "job.standalone.saved",
    "already": "job.aweb.notices.1",
    "scope_updated": "job.aweb.notices.2",
    "scope_changed": "job.aweb.notices.3",
    "service_changed": "job.aweb.notices.4",
})

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
    # The real service, when the app was built with one (web_main); otherwise the simulation answers.
    live: LiveService | None = None

    @property
    def recipient(self):
        return self.live.recipient if self.live is not None else RECIPIENT

    @property
    def notice_version(self) -> str:
        return self.live.notice_version if self.live is not None else NOTICE_VERSION

    def make_provider(self, task):
        # The simulated service waits on the task's cancel signal, so cancelling ends its wait.
        return SimulatedProvider(self.simulation_delay, stop=task.cancel)

    def provider_for(self, task, sending):
        """The task's provider as a context (runner.run_task): the real service's, else the simulation's."""
        if self.live is not None:
            return self.live.provider_for(task, sending)

        return local_provider(self.make_provider)(task, sending)

    def refusal(self):
        """Why no task may start now (app.live_gate.Blocked), or None; the simulation never refuses."""
        return self.live.refusal() if self.live is not None else None

    def end_session(self, session: str) -> None:
        self.store.end_session(session)

    def sweep(self) -> None:
        self.store.sweep()

    def shutdown(self) -> None:
        self.store.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)

        if self.live is not None:
            self.live.close()


def install_job_analysis(
    app: FastAPI,
    *,
    simulation_delay: float = 0.0,
    sweep_seconds: float = SWEEP_SECONDS,
    clock=None,
    executor=None,
    live: LiveService | None = None,
) -> JobAnalysisRuntime:
    jobs = JobAnalysisRuntime(
        store=TaskStore(clock or time.monotonic),
        executor=executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="job-analysis"),
        simulation_delay=simulation_delay,
        sweep_seconds=sweep_seconds,
        live=live,
    )
    app.state.job_analysis = jobs
    # Logging out, or signing in as someone else, ends the session's tasks.
    app.state.library.session_end_listeners.append(jobs.end_session)
    app.include_router(router)

    if simulation_delay:
        # The banner says it in the page's language (app.i18n, app.simulation_note).
        app.state.library.templates.env.globals["simulation_seconds"] = f"{simulation_delay:g}"

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


SUPPLIED_GONE = lazy("job.aweb.5")
SUPPLIED_FILLED = lazy("job.aweb.6")


@router.get(BASE)
def input_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    inputs = TaskInputs(chosen=(), posting="", use_preferences=False, result_language=i18n.language())
    origin = request.query_params.get("from", "")
    said = LIBRARY_NOTICES.get(notice)

    if origin:
        task = jobs_of(request).store.get(origin, user.id, user.token_hash)

        if task is not None:
            inputs = task.inputs

    # 去分析 from a posting the user supplied (U3-D4d): its text only, filled in for this page's own choice of
    # experiences, review and confirmation; filling it in sends nothing. What was read or pasted lasts as long as memory
    # holds it for this session (app.state.supplied, never imported from the discovery pages); a pasted posting kept
    # has its text as long as its record is in use and the user's.
    supplied_token = request.query_params.get("supplied", "")
    record_id = request.query_params.get("record", "")

    if supplied_token or record_id:
        text = None

        if supplied_token:
            holder = getattr(request.app.state, "supplied", None)
            text = None if holder is None else holder.prefill(supplied_token, user.id, user.token_hash)
        else:
            with runtime(request).database.transaction() as db:
                text = job_texts.text_in_use(db, user.id, record_id)

        if text:
            # The reference goes with the form; the review verifies it (the user's own, still there, the very text).
            reference = f"supplied:{supplied_token}" if supplied_token else f"record:{record_id}"
            inputs = TaskInputs(chosen=(), posting=text, use_preferences=False, result_language=i18n.language(), origin=reference)

        said = SUPPLIED_FILLED if text else SUPPLIED_GONE

    # Saving from the leave dialog of a library form can land here: say what was saved.
    return input_form(request, user, inputs, notice=said)


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
            "section": "discover",
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
            # The service a task started from here would name (JobAnalysisRuntime.recipient), never a fixed text.
            "service_label": presentation.service_label(jobs_of(request).recipient),
            "MAX_POSTING_CHARS": MAX_POSTING_CHARS,
            "MAX_EXPERIENCES": MAX_EXPERIENCES,
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
        # Only a whitelisted language; anything else is the interface language (plan 6.3).
        result_language=i18n.result_language(form.get("result_language")),
        origin=form.get("origin", "")[:200],
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

    analysis_request = build_request(user.id, plan, jobs.recipient, jobs.notice_version, inputs.result_language)
    origin = resolve_origin(request, user, inputs)

    try:
        task = jobs.store.create(
            user.id, user.token_hash, inputs, plan, analysis_request, task_fingerprint(analysis_request), origin=origin
        )
    except TooManyTasks:
        return message_page(
            request,
            title=t("job.aweb.7"),
            message=t("job.aweb.8"),
            status_code=503,
            user=user,
            links=[(BASE, t("job.aweb.9"))],
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
        title=t("job.aweb.10"),
        message=(
            t("job.aweb.11")
        ),
        status_code=404,
        user=user,
        links=[(BASE, t("job.aweb.9"))],
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
            "section": "discover",
        "task": task,
        "notice": NOTICES.get(notice),
        "running_label": presentation.running_label(task),
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
            "section": "discover",
            "task": task,
            "service": presentation.service_view(jobs_of(request)),
            "plan": task.plan,
            "notice": notice,
            "error": error,
            "SEND": SEND,
            "LEFT_OUT": LEFT_OUT,
            "BLOCKED": BLOCKED,
            "PREFERENCES_KEY": PREFERENCES_KEY,
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

    if (task.request.recipient, task.request.notice_version) != (jobs.recipient, jobs.notice_version):
        # Reviewed under another service or notice: that review is not confirmed, it is redone.
        return new_review(request, user, task.inputs, replaces=task.id, notice="service_changed")

    sendable = _sendable(task.plan)
    ticked = set(form.getlist("send")) & sendable

    if ticked != _sent(task.plan):
        inputs = replace(task.inputs, left_out=frozenset(sendable - ticked))
        notice = "scope_changed" if form.get("action") == "start" else "scope_updated"

        return updated_review(request, user, task, inputs, notice)

    if form.get("action") != "start":
        return see_other(f"{BASE}/tasks/{task.id}")

    refusal = jobs.refusal()

    if refusal is not None:
        return review_page(request, user, task, error=presentation.REFUSALS[refusal], status_code=409)

    outcome, started = jobs.store.begin(task.id, user.id, user.token_hash, form.get("fingerprint", ""))

    if outcome is Start.STARTED:
        rt = runtime(request)
        jobs.executor.submit(run_task, jobs.store, rt.database, started.id, jobs.provider_for)
        return see_other(f"{BASE}/tasks/{started.id}")

    if outcome is Start.ALREADY:
        return see_other(f"{BASE}/tasks/{task.id}?notice=already")

    if outcome is Start.BUSY:
        return review_page(
            request,
            user,
            task,
            error=t("job.aweb.12"),
            status_code=409,
        )

    if outcome is Start.STALE_FORM:
        return review_page(
            request,
            user,
            task,
            error=t("job.aweb.13"),
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
            # What the page says after 已交给服务的请求：counts and generations, those still out as maybe.
            "sends": sends_text(task.handed),
        }
    )


# --- where the posting came from, and keeping the analysis (UI bilingual batch, plan 5.3) ------------------------


def resolve_origin(request: Request, user: CurrentUser, inputs: TaskInputs) -> "job_records.AnalysisOrigin":
    """The posting's source as the server can vouch for it now: a pasted posting the user keeps in 我的岗位, or one
    补充岗位 read or took in this session, only while it is theirs, still there and the very text posted here (an
    edited text is the user's own pasted text); anything else, or nothing said, is text pasted here only."""
    kind, _, reference = inputs.origin.partition(":")
    posted = same_lines(inputs.posting)

    if kind == "record" and reference:
        with runtime(request).database.transaction() as db:
            text = job_texts.text_in_use(db, user.id, reference)

            if text is None or same_lines(text) != posted:
                return job_records.PASTED_ORIGIN

            row = active_job_record(db, user.id, JobPosting, reference)
            facts = row.facts or {}

            return job_records.AnalysisOrigin(
                row.source_id,
                row.posting_id,
                job_texts.USER_PROVIDED,
                job_texts.USER_PROVIDED,
                title=facts.get("title") or "",
                company=facts.get("company") or "",
                record_id=row.id,
            )

    if kind == "supplied" and reference:
        holder = getattr(request.app.state, "supplied", None)
        held = None if holder is None else holder.get(reference, user.id, user.token_hash)
        content = None if held is None else held.content
        text = None if content is None else content.text

        if text is None or same_lines(text) != posted:
            return job_records.PASTED_ORIGIN

        if content.kind == LINK:
            facts = content.reading.facts or {}

            return job_records.AnalysisOrigin(
                content.ref.source_id,
                content.ref.posting_id,
                content.access,
                facts.get("evidence") or "official",
                source_name=content.source_name,
                title=facts.get("title") or "",
                company=facts.get("company") or "",
                url=content.reading.url,
                supplied_token=reference,
            )

        if not content.posting_id:
            return job_records.PASTED_ORIGIN

        # The pasted posting's own id since it was supplied (Codex review F2): the posting it becomes in 我的岗位 has
        # it too, so the analysis is that posting's whichever is saved first. Kept already, it names that record.
        record_id = None

        if held.saved is not None:
            with runtime(request).database.transaction() as db:
                row = db.get(JobPosting, held.saved)

                if row is not None and row.owner_id == user.id and row.deleted_at is None and row.posting_id == content.posting_id:
                    record_id = row.id

        return job_records.AnalysisOrigin(
            job_records.SUPPLIED_SOURCE,
            content.posting_id,
            job_texts.USER_PROVIDED,
            job_texts.USER_PROVIDED,
            title=content.title,
            company=content.company,
            record_id=record_id,
            supplied_token=reference,
        )

    return job_records.PASTED_ORIGIN


def same_lines(text: str) -> str:
    """Text as it compares with what a form sends back: a browser sends a textarea's line breaks as CR LF, so the
    text it was filled with comes back with other line endings; only those, and the ends, are evened out."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def saved_state(request: Request, user: CurrentUser, task) -> dict:
    """Whether this task's analysis may be kept, or is kept (in use, in the bin, or gone for good since)."""
    rt = runtime(request)
    origin = task.origin or job_records.PASTED_ORIGIN
    keepable = origin.access in job_records.STANDALONE_KEEPABLE_ACCESS

    with rt.database.transaction() as db:
        row = job_records.standalone_of(db, user.id, task.id)

        if row is not None and row.deleted_at is not None and _expired(row, rt.now()):
            row = None
            gone = True
        else:
            gone = row is None and task.saved_id is not None

    return {
        "saved_row": row if row is not None and row.deleted_at is None else None,
        "saved_in_bin": row is not None and row.deleted_at is not None,
        "saved_gone": gone,
        "keepable": keepable,
        "origin": origin,
        "posting_entry": posting_entry(request, user, origin),
    }


def posting_entry(request: Request, user: CurrentUser, origin) -> tuple[str, str | None]:
    """Where the result page leads for the posting it analysed (Codex review F2), as things are now: the user's posting
    of the same identity ("record"); else the 补充岗位 result still held, to add it to 我的岗位 from ("supply"); else
    that result is gone ("expired"); text pasted here only has no posting identity ("pasted"). Saving the analysis and
    adding the posting stay separate choices: this only leads to the page where the user adds it."""
    if origin.source_id == job_records.PASTED_ANALYSIS_SOURCE:
        return "pasted", None

    rt = runtime(request)

    with rt.database.transaction() as db:
        row = db.scalar(
            select(JobPosting).where(
                JobPosting.owner_id == user.id,
                JobPosting.source_id == origin.source_id,
                JobPosting.posting_id == origin.posting_id,
                JobPosting.deleted_at.is_(None),
            )
        )

    if row is not None:
        return "record", row.id

    holder = getattr(request.app.state, "supplied", None)

    if origin.supplied_token and holder is not None and holder.get(origin.supplied_token, user.id, user.token_hash) is not None:
        return "supply", origin.supplied_token

    return "expired", None


def _expired(row, now) -> bool:
    from app.library.service import is_expired

    return is_expired(row, now)


@router.post(f"{BASE}/tasks/{{task_id}}/save")
def save(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Keep this task's analysis on its own (plan 5.3): no posting is saved or bookmarked by it. Kept already, it is
    the one kept; in the bin or deleted for good since, it is not kept again; a page of a task gone finds nothing."""
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is not Stage.DONE:
        return see_other(f"{BASE}/tasks/{task.id}")

    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = job_records.keep_standalone_analysis(
                db,
                user.id,
                task.id,
                task.request,
                task.result,
                task.origin or job_records.PASTED_ORIGIN,
                analysed_at=task.finished_at,
                mode="live" if jobs_of(request).live is not None else "simulated",
                saved_id=task.saved_id,
                now=rt.now(),
            )
            saved_id = row.id
    except job_records.KeepRefused as refused:
        return message_page(
            request,
            title=t("job.standalone.not_saved"),
            message=str(refused.reason),
            status_code=409,
            user=user,
            links=[(f"{BASE}/tasks/{task.id}", t("job.standalone.back"))],
        )

    jobs_of(request).store.note_saved(task.id, user.id, user.token_hash, saved_id)

    return see_other(f"{BASE}/tasks/{task.id}?notice=analysis_saved")


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
        **saved_state(request, user, task),
        "result": result,
        "changed_since": changed,
        "outcome": presentation.outcome_message(result),
        "mode_label": presentation.mode_label(result),
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
        "SELECTION_PROBLEMS": presentation.SELECTION_PROBLEMS,
        "Status": AnalysisStatus,
        "Group": RequirementGroup,
    }
