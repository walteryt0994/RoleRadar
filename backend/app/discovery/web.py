"""Pages of 岗位机会 → 找岗位 (U3-A3): a preview of the search agent on synthetic postings.

    /discover                           what to look for: job titles, where, and which experiences
    /discover/review              POST  check it and keep it as a search to confirm
    /discover/tasks/<id>                the review, the running search, or what it found
    /discover/tasks/<id>/confirm  POST  update what each analysis sends, or confirm and start
    /discover/tasks/<id>/cancel   POST
    /discover/tasks/<id>/status         the running search's steps, for the page's script

Only a preview runs here (U3-A3 pre-check, 6.2). The postings come from the frozen synthetic sources
(app.discovery.synthetic), each decision from SimulatedDecider (fixed local rules, not AI) and each analysis from the
app's simulation (app.job_analysis.simulation). DiscoveryRuntime binds the three itself: it never asks the job
analysis pages which service they use, so a real service the app may have for those is never reached from here, and
nothing a page sends (a form, a URL parameter) nor the environment can switch a search to one. The sources, the
recipient and the notice are the server's, and every confirmation's fingerprint covers them.

Every page and post is the signed-in user's, and every post carries the session's CSRF token and comes from this site
(app.job_analysis.web.checked_fields, app.library.web). A GET starts nothing, and checking what was entered
(tools.scope_check) starts nothing either: no task, no service, no write. A search is the user's and the session's: an
id alone opens nothing. Only confirming a reviewed search starts it, once, with the fingerprint of the very
confirmation reviewed (DiscoveryTaskStore.begin), and start() checks it all again before the first step. Nothing a
search finds is kept: tasks, their steps and results live in memory for TASK_TTL_SECONDS and are gone with a restart,
nothing is saved to the library and nothing is applied for.
"""
import os
import time
import unicodedata
from functools import partial
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData

from app.analysis_contracts import RequirementGroup, SelectedPreference, SelectedSource
from app.discovery import presentation, rules, synthetic
from app.discovery.agent import MAX_DECISIONS, SimulatedDecider
from app.discovery.contracts import (
    CATEGORY_LABELS as RESULT_CATEGORIES,
    DIMENSION_LABELS,
    NOT_PERSONALISED,
    Category,
    Dimension,
    DimensionScope,
    Limits,
    Others,
    ParentConfirmation,
    SearchScope,
)
from app.discovery.runner import run_search, tracing_variables
from app.discovery.tasks import TASK_TTL_SECONDS, DiscoveryTaskStore, Stage, Start, TooManyTasks
from app.discovery.tools import scope_check
from app.experience_analysis import PREFERENCE_TOPICS
from app.job_analysis import presentation as analysis_text
from app.job_analysis.scope import (
    BLOCKED,
    LEFT_OUT,
    MAX_EXPERIENCES,
    MAX_OUTBOUND_CHARS,
    PREFERENCES_KEY,
    SEND,
    InputProblem,
    PreferencePlan,
    SourcePlan,
    preference_plans,
    source_plan,
)
from app.job_analysis.service import local_provider
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.job_analysis.tasks import SWEEP_SECONDS
from app.job_analysis.web import checked_fields
from app.library.content import CATEGORY_LABELS
from app.library.errors import NotFound
from app.library.identity import CurrentUser
from app.library.projection import current_preferences, project_confirmed_library, read_selected_confirmations
from app.library.service import list_experiences
from app.library.web import current_user, message_page, render, runtime, safe_next, see_other

BASE = "/discover"
# What every search here is confirmed for, decided by the server: the synthetic sources, the simulation as the
# recipient of each analysis, this notice and the default limits. Changing a word of the review changes the version.
SOURCES = synthetic.GRANTS
NOTICE_VERSION = "discovery-preview-1"
LIMITS = Limits()
# Free text a page takes: the titles looked for and the values added to a dimension; each value is at most what one
# search's words may be.
TEXT_CHARS = 200
VALUE_CHARS = LIMITS.keyword_chars
NOTICES = {
    "already": "这次确认已经开始过，没有重复执行。",
    "scope_updated": "已按你的选择更新核对内容，请再看一遍后确认。",
    "scope_changed": "你改动了发送范围，所以还没有开始。下面已按新的范围更新，请再看一遍后确认。",
}

router = APIRouter()


@dataclass
class DiscoveryRuntime:
    """The searches of this app, and the one way they run: synthetic sources, simulated decisions and analyses."""

    store: DiscoveryTaskStore
    executor: ThreadPoolExecutor
    # Only for trying the pages locally (web_main --simulation-delay): each simulated analysis request waits this long.
    simulation_delay: float = 0.0
    sweep_seconds: float = SWEEP_SECONDS
    # The lifespan's sweeping task, while the app runs.
    sweeper: object = None
    # Where the tracing variables are looked for; None is the process environment.
    environ: Mapping[str, str] | None = None

    def make_provider(self, task) -> SimulatedProvider:
        # The simulation waits on the task's cancel signal, so stopping the search ends its wait.
        return SimulatedProvider(self.simulation_delay, stop=task.cancel)

    def provider_for(self, task, sending):
        """The simulation, for the whole search (runner.run_search); never a real service's."""
        return local_provider(self.make_provider)(task, sending)

    def register(self):
        """The synthetic sources, new for each search."""
        return synthetic.register()

    def decider_for(self, provider) -> SimulatedDecider:
        return SimulatedDecider()

    def tracing(self) -> tuple[str, ...]:
        """The names of the variables that would keep a search from starting (runner.tracing_variables)."""
        return tracing_variables(os.environ if self.environ is None else self.environ)

    def end_session(self, session: str) -> None:
        self.store.end_session(session)

    def sweep(self) -> None:
        self.store.sweep()

    def shutdown(self) -> None:
        self.store.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)


def install_discovery(
    app: FastAPI,
    *,
    simulation_delay: float = 0.0,
    sweep_seconds: float = SWEEP_SECONDS,
    clock=None,
    executor=None,
) -> DiscoveryRuntime:
    """Mount the pages. Nothing here takes a real service: the searches only ever run the simulation."""
    searches = DiscoveryRuntime(
        store=DiscoveryTaskStore(clock or time.monotonic),
        executor=executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="discovery"),
        simulation_delay=simulation_delay,
        sweep_seconds=sweep_seconds,
    )
    app.state.discovery = searches
    # Logging out, or signing in as someone else, ends the session's searches.
    app.state.library.session_end_listeners.append(searches.end_session)
    app.include_router(router)

    return searches


def searches_of(request: Request) -> DiscoveryRuntime:
    return request.app.state.discovery


# --- what was entered ------------------------------------------------------------------


@dataclass(frozen=True)
class Wanted:
    """What the user entered about one dimension: the offered values ticked, values added, and whether others count."""

    ticked: tuple[str, ...] = ()
    added: str = ""
    # Others.INCLUDED or Others.EXCLUDED as chosen, or "" when nothing was chosen.
    others: str = ""


@dataclass(frozen=True)
class SearchInputs:
    targets: str = ""
    city: Wanted = Wanted()
    job_type: Wanted = Wanted()
    direction: Wanted = Wanted()
    # (experience id, confirmed version id), as the page offered them.
    chosen: tuple[tuple[str, str], ...] = ()
    use_preferences: bool = False
    # "<experience id>:<field>" or "preferences:<field>" the user chose to leave out on the review.
    left_out: frozenset[str] = field(default_factory=frozenset)

    def of(self, dimension: Dimension) -> Wanted:
        return getattr(self, dimension.value)


@dataclass(frozen=True)
class Material:
    """What each analysis of the search sends besides the posting, field by field, as the review shows it."""

    sources: tuple[SourcePlan, ...]
    use_preferences: bool
    preferences: tuple[PreferencePlan, ...]
    chars: int

    @property
    def sent_preferences(self) -> tuple[PreferencePlan, ...]:
        return tuple(plan for plan in self.preferences if plan.status == SEND)


@dataclass(frozen=True)
class Draft:
    """What a page entered and reviewed for one search (DiscoveryTask.draft)."""

    inputs: SearchInputs
    material: Material


def fresh_inputs(saved: Mapping[str, str]) -> SearchInputs:
    """A new search: the ideal and acceptable values of the preferences ticked, nothing else chosen."""
    offered = rules.prefill(saved)

    return SearchInputs(**{dimension.value: Wanted(ticked=offered[dimension].values) for dimension in Dimension})


def _chosen(values: list[str]) -> tuple[tuple[str, str], ...]:
    chosen = []

    for value in values:
        item_id, _, version_id = value.partition(":")

        if not item_id or not version_id:
            raise HTTPException(status_code=400)

        chosen.append((item_id, version_id))

    return tuple(chosen)


def inputs_from(form: FormData) -> SearchInputs:
    return SearchInputs(
        targets=form.get("targets", ""),
        **{
            dimension.value: Wanted(
                ticked=tuple(form.getlist(dimension.value)),
                added=form.get(f"{dimension.value}_added", ""),
                others=form.get(f"{dimension.value}_others", ""),
            )
            for dimension in Dimension
        },
        chosen=_chosen(form.getlist("source")),
        use_preferences=form.get("use_preferences") == "yes",
    )


def build_scope(inputs: SearchInputs, saved: Mapping[str, str]) -> SearchScope:
    """The scope as entered: offered values ticked and values added; what the user cannot accept comes from the
    saved preferences only, never from the page, and nothing the user did not choose is decided for them."""
    offered = rules.prefill(saved)
    parts = {}

    for dimension in Dimension:
        given, draft = inputs.of(dimension), offered[dimension]
        ticked = tuple(value for value in draft.values if value in given.ticked)
        seen = {rules.normalized(dimension, value) for value in ticked}
        added = []

        for value in rules.values_of(given.added):
            if rules.normalized(dimension, value) not in seen:
                seen.add(rules.normalized(dimension, value))
                added.append(value)

        others = Others(given.others) if given.others in (Others.INCLUDED.value, Others.EXCLUDED.value) else Others.UNDECIDED
        parts[dimension] = DimensionScope(
            values=(*ticked, *added),
            others=others,
            ideal=tuple(value for value in draft.ideal if value in ticked),
            excluded=draft.excluded,
        )

    return SearchScope(
        city=parts[Dimension.CITY],
        job_type=parts[Dimension.JOB_TYPE],
        direction=parts[Dimension.DIRECTION],
        targets=rules.values_of(inputs.targets),
    )


def text_problems(inputs: SearchInputs) -> list[str]:
    """Free text the page cannot take as it is: too long, or holding control characters."""
    texts = [("要找的岗位名称", inputs.targets)] + [
        (f"{DIMENSION_LABELS[dimension]}（也查）", inputs.of(dimension).added) for dimension in Dimension
    ]
    problems = []

    for label, text in texts:
        if len(text) > TEXT_CHARS:
            problems.append(f"{label}最多 {TEXT_CHARS} 字")
        elif any(unicodedata.category(char) == "Cc" for char in text):
            problems.append(f"{label}里有无法处理的控制字符，请重新输入")
        elif any(len(value) > VALUE_CHARS for value in rules.values_of(text)):
            problems.append(f"{label}：每一项最多 {VALUE_CHARS} 字")

    return problems


def build_material(db, owner_id: str, inputs: SearchInputs) -> Material:
    """Read the chosen experiences and preferences again, and decide field by field what each analysis may send.

    No experience is fine: the search then only finds, reads and checks postings, and sends no preference either.
    The preferences the scope is made of (city, job type, direction) are held against postings by the rules and never
    sent (rules.SCOPE_FIELDS).
    """
    chosen = inputs.chosen

    if len(chosen) > MAX_EXPERIENCES:
        raise InputProblem(f"一次最多选择 {MAX_EXPERIENCES} 段经历，现在选了 {len(chosen)} 段。", "sources")

    if len({item_id for item_id, _ in chosen}) != len(chosen):
        raise InputProblem("同一段经历只能选择一次，请重新选择。", "sources")

    entries, problems = read_selected_confirmations(db, owner_id, dict(chosen))

    if problems:
        raise InputProblem(
            f"你选择的经历里有 {len(problems)} 段已经修改、重新确认或删除，不能按原来的版本使用。下面已按当前内容刷新，请重新选择。",
            "sources",
        )

    sources = tuple(source_plan(entry, inputs.left_out) for entry in entries)

    for source in sources:
        if not source.sent:
            raise InputProblem(
                f"“{source.title}”没有可以发送的栏目（都被排除或含有疑似敏感信息）。请取消选择它，或修改这段经历后重新确认。",
                "sources",
            )

    use_preferences = inputs.use_preferences and bool(sources)
    preferences = (
        tuple(plan for plan in preference_plans(db, owner_id, inputs.left_out) if plan.field not in rules.SCOPE_FIELDS)
        if use_preferences
        else ()
    )
    chars = sum(len(plan.text) for source in sources for plan in source.fields if plan.status == SEND)
    chars += sum(len(plan.text) for plan in preferences if plan.status == SEND)

    # As start() counts it: not even the shortest posting would fit in one analysis with them.
    if chars >= MAX_OUTBOUND_CHARS:
        raise InputProblem(
            f"所选经历和偏好共 {chars} 字，加上岗位原文会超过一次分析 {MAX_OUTBOUND_CHARS} 字的上限。请少选几段经历，或排除一些栏目。",
            "sources",
        )

    return Material(sources=sources, use_preferences=use_preferences, preferences=preferences, chars=chars)


def confirmation_of(owner_id: str, scope: SearchScope, material: Material) -> ParentConfirmation:
    """The confirmation of exactly what the review shows, with the server's sources, recipient, notice and limits."""
    return ParentConfirmation(
        owner_id=owner_id,
        scope=scope,
        selection=tuple(
            SelectedSource(source.entry.item_id, source.entry.version_id, source.sent) for source in material.sources
        ),
        preferences=tuple(SelectedPreference(plan.field, plan.text) for plan in material.sent_preferences),
        recipient=RECIPIENT,
        sources=SOURCES,
        notice_version=NOTICE_VERSION,
        limits=LIMITS,
    )


# --- the input page --------------------------------------------------------------------


@router.get(BASE)
def input_page(request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    origin = request.query_params.get("from", "")
    inputs = None

    if origin:
        task = searches_of(request).store.get(origin, user.id, user.token_hash)

        if task is not None and task.draft is not None:
            inputs = task.draft.inputs

    if inputs is None:
        with rt.database.transaction() as db:
            inputs = fresh_inputs(current_preferences(db, user.id))

    return input_form(request, user, inputs)


def input_form(request: Request, user: CurrentUser, inputs: SearchInputs, problems=(), *, where: str = "") -> Response:
    """The input page with what the user entered; an experience whose version is gone is not ticked again."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        snapshot = project_confirmed_library(db, user.id, rt.now())
        unconfirmed = len(list_experiences(db, user.id)) - len(snapshot.experiences)
        saved = current_preferences(db, user.id)

    offered = {(entry.item_id, entry.version_id) for entry in snapshot.experiences}
    ticked = {item_id for item_id, version_id in inputs.chosen if (item_id, version_id) in offered}
    prefill = rules.prefill(saved)

    return render(
        request,
        "discover_input.html",
        {
            "area": "jobs",
            "section": "discover",
            "inputs": inputs,
            "dimensions": [
                (dimension, DIMENSION_LABELS[dimension], prefill[dimension], inputs.of(dimension)) for dimension in Dimension
            ],
            "experiences": snapshot.experiences,
            "unconfirmed": unconfirmed,
            "ticked": ticked,
            "dropped": len(inputs.chosen) - len(ticked) if not problems else 0,
            "analysed_preferences": [name for name in saved if name not in rules.SCOPE_FIELDS and saved.get(name)],
            "problems": problems,
            "where": where,
            "tracing": searches_of(request).tracing(),
            "unsaved_input": bool(problems),
            "PREVIEW": presentation.PREVIEW,
            "MAX_EXPERIENCES": MAX_EXPERIENCES,
            "VALUE_CHARS": VALUE_CHARS,
            "Others": Others,
            "CATEGORY_LABELS": CATEGORY_LABELS,
        },
        user=user,
        status_code=422 if problems else 200,
    )


@router.post(f"{BASE}/review")
def review(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    return new_review(request, user, inputs_from(form))


def new_review(request: Request, user: CurrentUser, inputs: SearchInputs, *, replaces: str | None = None, notice: str = "") -> Response:
    """Check what was entered and keep it as a search to confirm; nothing starts here.

    The scope is checked as start() checks it (tools.scope_check), read-only: what keeps it from starting is shown on
    the input page, with what was entered, and no search is kept.
    """
    rt = runtime(request)
    searches = searches_of(request)
    material, problems, where = None, (), ""

    with rt.database.transaction() as db:
        scope = build_scope(inputs, current_preferences(db, user.id))
        problems, where = text_problems(inputs) or list(scope_check(scope)), "scope"

        if not problems:
            try:
                material = build_material(db, user.id, inputs)
            except InputProblem as problem:
                problems, where = (problem.message,), problem.where

    # The input page reads the library again, in a transaction of its own.
    if material is None:
        return input_form(request, user, inputs, problems, where=where)

    try:
        task = searches.store.create(
            user.id, user.token_hash, confirmation_of(user.id, scope, material), draft=Draft(inputs, material)
        )
    except TooManyTasks:
        return message_page(
            request,
            title="暂时不能开始新的查找",
            message="服务器上正在保留的临时查找太多了。请稍后再试，或先取消你不需要的查找。",
            status_code=503,
            user=user,
            links=[(BASE, "返回找岗位")],
        )

    if replaces:
        searches.store.discard(replaces, user.id, user.token_hash)

    return see_other(f"{BASE}/tasks/{task.id}" + (f"?notice={notice}" if notice else ""))


# --- one search -------------------------------------------------------------------------


def _task(request: Request, user: CurrentUser, task_id: str):
    task = searches_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        raise NotFound()

    return task


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这次查找",
        message=(
            f"它可能已经过期（临时结果保留 {TASK_TTL_SECONDS // 60} 分钟）、服务已重启、你已退出登录，或者它属于其他会话。"
            "需要的话，请重新开始一次查找。"
        ),
        status_code=404,
        user=user,
        links=[(BASE, "返回找岗位")],
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
        "PREVIEW": presentation.PREVIEW,
        "status_line": presentation.running_status(task),
        "steps": [presentation.step_view(step) for step in task.steps],
        "requests": presentation.requests_text(task),
        "scope_lines": presentation.scope_lines(task.confirmation.scope),
        "material": task.draft.material,
        "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
    }

    if task.stage is Stage.CANCELLED:
        context["cancelled_by"] = presentation.CANCELLED_BY.get(task.cancelled_by, "这次查找已停止")

    # Why it did not run, when it did not: nothing was sent then.
    context["problem"] = presentation.problem_text(task.problem) if task.problem is not None else None

    if task.finished is not None:
        context.update(outcome_context(request, user, task))

    return render(request, "discover_task.html", context, user=user)


def review_page(request: Request, user: CurrentUser, task, *, notice=None, error=None, status_code=200) -> Response:
    material = task.draft.material

    return render(
        request,
        "discover_review.html",
        {
            "area": "jobs",
            "section": "discover",
            "task": task,
            "material": material,
            "scope_lines": presentation.scope_lines(task.confirmation.scope),
            "sources": [(grant.name, presentation.evidence_label(grant.evidence)) for grant in task.confirmation.sources],
            "limits": task.confirmation.limits,
            "notice": notice,
            "error": error,
            "tracing": searches_of(request).tracing(),
            "PREVIEW": presentation.PREVIEW,
            "MAX_DECISIONS": MAX_DECISIONS,
            "SEND": SEND,
            "LEFT_OUT": LEFT_OUT,
            "BLOCKED": BLOCKED,
            "PREFERENCES_KEY": PREFERENCES_KEY,
            "CATEGORY_LABELS": CATEGORY_LABELS,
            "found_labels": analysis_text.found_labels,
            "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
        },
        user=user,
        status_code=status_code,
    )


def _sendable(material: Material) -> set[str]:
    keys = {
        f"{source.entry.item_id}:{plan.name}" for source in material.sources for plan in source.fields if plan.status != BLOCKED
    }

    return keys | {f"{PREFERENCES_KEY}:{plan.field}" for plan in material.preferences if plan.status != BLOCKED}


def _sent(material: Material) -> set[str]:
    keys = {f"{source.entry.item_id}:{name}" for source in material.sources for name in source.sent}

    return keys | {f"{PREFERENCES_KEY}:{plan.field}" for plan in material.sent_preferences}


@router.post(f"{BASE}/tasks/{{task_id}}/confirm")
def confirm(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Update what each analysis sends, or confirm and start: the only post that starts a search.

    The form carries no source, recipient or address: those are the server's. Fields unticked since the review are
    never sent: a start whose ticks differ from the reviewed ones becomes a new review instead.
    """
    searches = searches_of(request)

    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is not Stage.REVIEW:
        notice = "" if task.stage is Stage.CANCELLED else "?notice=already"

        return see_other(f"{BASE}/tasks/{task.id}{notice}")

    material = task.draft.material
    sendable = _sendable(material)
    ticked = set(form.getlist("send")) & sendable

    if ticked != _sent(material):
        inputs = replace(task.draft.inputs, left_out=frozenset(sendable - ticked))
        notice = "scope_changed" if form.get("action") == "start" else "scope_updated"

        return new_review(request, user, inputs, replaces=task.id, notice=notice)

    if form.get("action") != "start":
        return see_other(f"{BASE}/tasks/{task.id}")

    if searches.tracing():
        return review_page(
            request,
            user,
            task,
            error="环境里设置了可能打开 LangSmith 追踪的变量，所以没有开始，什么都没有发送。请去掉这些变量后重新启动本地网页。",
            status_code=409,
        )

    outcome, started = searches.store.begin(task.id, user.id, user.token_hash, form.get("fingerprint", ""))

    if outcome is Start.STARTED:
        searches.executor.submit(
            partial(
                run_search,
                searches.store,
                runtime(request).database,
                started.id,
                searches.provider_for,
                register=searches.register(),
                clock=runtime(request).clock,
                decider_for=searches.decider_for,
                environ=searches.environ,
            )
        )

        return see_other(f"{BASE}/tasks/{started.id}")

    if outcome is Start.ALREADY:
        return see_other(f"{BASE}/tasks/{task.id}?notice=already")

    if outcome is Start.BUSY:
        return review_page(
            request, user, task, error="你已经有一个查找正在进行。请等它结束，或者取消它之后再开始。", status_code=409
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


@router.post(f"{BASE}/tasks/{{task_id}}/cancel")
def cancel(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    task = searches_of(request).store.cancel(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    # The search's own page says what cancelling did and did not stop.
    return see_other(safe_next(form.get("next", "")) or f"{BASE}/tasks/{task.id}")


@router.get(f"{BASE}/tasks/{{task_id}}/status")
def task_status(task_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """What the running page's script needs, and nothing more: no token, session, confirmation or material."""
    task = searches_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return JSONResponse({"stage": "gone"}, status_code=404)

    return JSONResponse(
        {
            "stage": task.stage.value,
            "status": presentation.running_status(task),
            "steps": [presentation.step_view(step) for step in task.steps],
        }
    )


# --- what a search found ---------------------------------------------------------------


def outcome_context(request: Request, user: CurrentUser, task) -> dict:
    """What the result shows. The chosen material is read again on every view: analyses made with material that has
    changed since are said to be so, never shown as current."""
    finished = task.finished
    outcome = finished.outcome
    confirmation = task.confirmation
    material = task.draft.material
    changed = False

    if outcome.results and (confirmation.selection or confirmation.preferences):
        rt = runtime(request)

        with rt.database.transaction() as db:
            chosen = {source.item_id: source.version_id for source in confirmation.selection}
            _, problems = read_selected_confirmations(db, user.id, chosen)
            saved = current_preferences(db, user.id) if confirmation.preferences else {}

        changed = bool(problems) or any(saved.get(item.field) != item.text for item in confirmation.preferences)

    names = {grant.source_id: grant.name for grant in confirmation.sources}

    return {
        "finished": finished,
        "outcome": outcome,
        "changed_since": changed,
        "results_by_category": [
            (category, RESULT_CATEGORIES[category], [result for result in outcome.results if result.category is category])
            for category in presentation.SHOWN_CATEGORIES
        ],
        "trace_steps": [presentation.step_view(step) for step in finished.trace.steps],
        "searches": [presentation.search_line(record, names) for record in outcome.searches],
        "source_names": "、".join(grant.name for grant in confirmation.sources),
        "end_label": presentation.end_label(outcome.end),
        "NOT_PERSONALISED": NOT_PERSONALISED,
        "UNLISTED_LABELS": presentation.UNLISTED_LABELS,
        "titles": {source.entry.item_id: source for source in material.sources},
        "versions": {source.entry.item_id: source.entry.version_number for source in material.sources},
        "stated": presentation.stated,
        "checked_text": presentation.checked_text,
        "deadlines_text": presentation.deadlines_text,
        "evidence_label": presentation.evidence_label,
        "shown_ref": synthetic_ref,
        "conclusion_label": analysis_text.conclusion_label,
        "preference_label": analysis_text.preference_label,
        "reason_labels": analysis_text.reason_labels,
        "BASIS_LABELS": analysis_text.BASIS_LABELS,
        "GROUP_HEADINGS": analysis_text.GROUP_HEADINGS,
        "PREFERENCE_TOPICS": PREFERENCE_TOPICS,
        "Group": RequirementGroup,
        "Category": Category,
    }


def synthetic_ref(ref) -> str:
    """A posting's id in its source, as a page shows it: the source's own id, never an address."""
    return ref.posting_id
