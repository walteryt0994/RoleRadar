"""The pages of 方向探索 (U3-C3a): choosing what to send, reviewing it, running one exploration on the local
simulation, comparing its directions, and keeping what the user chooses to keep.

Every page and post is the signed-in user's; every post carries the session's CSRF token and comes from this site
(app.job_analysis.web.checked_fields). A task is the user's and the session's (app.directions.tasks): its id alone
opens nothing for anyone else. A task runs only the local simulation (RECIPIENT): no real service is built or
reached from here, and the workflow refuses any other recipient anyway (app.directions.explore).

Nothing is saved unless the user asks: an exploration is saved by "保存这次探索", a decision by the buttons of a
direction, each through app.directions.actions, which checks the library again in the same transaction. Each view
of a finished task checks the sources its result rests on: a result whose sources were edited since is shown as
not current and cannot be saved or decided on; one whose sources were deleted is withdrawn for good.

C3b is not here: the deletion pages offer no "delete along" yet, and no direction is handed to 找岗位.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from starlette.datastructures import FormData

from app.directions import presentation
from app.directions.actions import (
    Saved,
    basis_now,
    change_decision,
    changeable,
    decide_on_result,
    decide_on_saved_exploration,
    make_own_choice,
    save_result,
    saved_of,
)
from app.directions.contracts import ExplorationResult, name_key
from app.directions.handoff import held_back, kept_for_search
from app.directions.explore import FIELD_GROUPS, FIELD_ORDER, ExplorationInputs, build_plan, build_request, fingerprint
from app.directions.records import (
    SeenDecision,
    decisions_in_use,
    delete_decision,
    delete_exploration,
    explorations_in_use,
    request_decision_purge,
    request_exploration_purge,
    restore_decision,
    restore_exploration,
)
from app.directions.runner import run_task
from app.directions.tasks import TASK_TTL_SECONDS, Claim, ExplorationTaskStore, Stage, Start, TooManyTasks
from app.job_analysis import presentation as analysis_text
from app.job_analysis.scope import BLOCKED, LEFT_OUT, MAX_EXPERIENCES, PREFERENCES_KEY, SEND, InputProblem
from app.job_analysis.service import local_provider
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.job_analysis.tasks import SWEEP_SECONDS
from app.job_analysis.web import checked_fields
from app.library.content import CATEGORY_LABELS, display_title
from app.library.direction_export import SOURCE_STATE_LABELS
from app.library.errors import NotAllowed, NotFound, RecordConflict
from app.library.identity import CurrentUser
from app.library.lifecycle import active_direction_record, decision_in_use, trashed_direction_record
from app.library.models import DecisionState, DirectionDecision, DirectionExploration, ExperienceItem, ExperienceVersion
from app.library.projection import current_preferences, project_confirmed_library
from app.library.references import BasisState, SourceState, decision_state, exploration_state
from app.library.sends import sends_text
from app.library.service import list_experiences
from app.library.web import NOTICES as LIBRARY_NOTICES
from app.library.web import current_user, message_page, render, runtime, safe_next, see_other

BASE = "/directions"
RECORDS = f"{BASE}/records"
STATES = {state.value for state in DecisionState}
WRITING = "这次结果正在保存或记下决定，请稍后再试。"
CONFLICT = (
    "你打开这一页之后，这条决定在别处改过（可能是另一个标签页），所以这次没有改。下面是它现在的状态，看过后再选。"
)

router = APIRouter()


@dataclass
class DirectionsRuntime:
    store: ExplorationTaskStore
    executor: ThreadPoolExecutor
    # Only for trying the pages locally (web_main --simulation-delay).
    simulation_delay: float = 0.0
    # How often the app's lifespan sweeps the store (web_main).
    sweep_seconds: float = SWEEP_SECONDS
    # The lifespan's sweeping task, while the app runs.
    sweeper: object = None

    @property
    def recipient(self):
        """The local simulation: the only service a direction exploration goes to (C3a)."""
        return RECIPIENT

    def make_provider(self, task):
        # The simulated service waits on the task's cancel signal, so cancelling ends its wait.
        return SimulatedProvider(self.simulation_delay, stop=task.cancel)

    def provider_for(self, task, sending):
        """The task's provider as a context (runner.run_task): the simulation, counted by the store."""
        return local_provider(self.make_provider)(task, sending)

    def end_session(self, session: str) -> None:
        self.store.end_session(session)

    def sweep(self) -> None:
        self.store.sweep()

    def shutdown(self) -> None:
        self.store.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)


def install_directions(
    app: FastAPI,
    *,
    simulation_delay: float = 0.0,
    sweep_seconds: float = SWEEP_SECONDS,
    clock=None,
    executor=None,
) -> DirectionsRuntime:
    directions = DirectionsRuntime(
        store=ExplorationTaskStore(clock or time.monotonic),
        executor=executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="directions"),
        simulation_delay=simulation_delay,
        sweep_seconds=sweep_seconds,
    )
    app.state.directions = directions
    # Logging out, or signing in as someone else, ends the session's tasks.
    app.state.library.session_end_listeners.append(directions.end_session)
    app.include_router(router)

    return directions


def directions_of(request: Request) -> DirectionsRuntime:
    return request.app.state.directions


# --- choosing what to send ------------------------------------------------------------------------------------------


def _skips_preferences(inputs: ExplorationInputs) -> bool:
    return all(f"{PREFERENCES_KEY}:{name}" in inputs.left_out for name in FIELD_ORDER)


@router.get(BASE)
def input_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    inputs = ExplorationInputs()
    origin = request.query_params.get("from", "")

    if origin:
        task = directions_of(request).store.get(origin, user.id, user.token_hash)

        if task is not None:
            inputs = task.inputs

    # Saving from the leave dialog of a library form can land here: say what was saved.
    return input_form(request, user, inputs, notice=LIBRARY_NOTICES.get(notice))


def input_form(
    request: Request, user: CurrentUser, inputs: ExplorationInputs, problem: InputProblem | None = None, *, notice: str | None = None
) -> Response:
    """The page to choose from; a choice whose version is gone is not ticked again."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        snapshot = project_confirmed_library(db, user.id, rt.now())
        unconfirmed = len(list_experiences(db, user.id)) - len(snapshot.experiences)
        saved = current_preferences(db, user.id)

    offered = {(entry.item_id, entry.version_id) for entry in snapshot.experiences}
    ticked = {item_id for item_id, version_id in inputs.chosen if (item_id, version_id) in offered}
    filled = [group for group in presentation.GROUP_NAMES if any(saved.get(name) for name in FIELD_ORDER if FIELD_GROUPS[name] == group)]

    return render(
        request,
        "direction_input.html",
        {
            "area": "directions",
            "section": "explore",
            "experiences": snapshot.experiences,
            "unconfirmed": unconfirmed,
            "ticked": ticked,
            "dropped": len(inputs.chosen) - len(ticked) if problem is None else 0,
            "filled_groups": filled,
            "skip_preferences": _skips_preferences(inputs),
            "problem": problem,
            "notice": notice,
            "error": problem.message if problem else None,
            "MAX_EXPERIENCES": MAX_EXPERIENCES,
            "CATEGORY_LABELS": CATEGORY_LABELS,
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
    left_out = frozenset(f"{PREFERENCES_KEY}:{name}" for name in FIELD_ORDER) if form.get("skip_preferences") == "yes" else frozenset()

    return new_review(request, user, ExplorationInputs(chosen=_chosen(form.getlist("source")), left_out=left_out))


def new_review(request: Request, user: CurrentUser, inputs: ExplorationInputs, *, replaces: str | None = None, notice: str = "") -> Response:
    """Work out what the input would send, keep it as a new task and show its review."""
    rt = runtime(request)
    directions = directions_of(request)

    try:
        with rt.database.transaction() as db:
            plan = build_plan(db, user.id, inputs, rt.now())
    except InputProblem as problem:
        return input_form(request, user, inputs, problem)

    exploration_request = build_request(user.id, plan, directions.recipient)

    try:
        task = directions.store.create(user.id, user.token_hash, inputs, plan, exploration_request, fingerprint(exploration_request))
    except TooManyTasks:
        return message_page(
            request,
            title="暂时不能开始新的探索",
            message="服务器上正在保留的临时探索太多了。请稍后再试，或先取消你不需要的探索。",
            status_code=503,
            user=user,
            links=[(BASE, "返回方向探索")],
        )

    if replaces:
        directions.store.discard(replaces, user.id, user.token_hash)

    return see_other(f"{BASE}/tasks/{task.id}" + (f"?notice={notice}" if notice else ""))


# --- one task -------------------------------------------------------------------------------------------------------


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这次探索",
        message=(
            f"它可能已经过期（临时结果保留 {TASK_TTL_SECONDS // 60} 分钟）、服务已重启、你已退出登录，或者它属于其他会话。"
            "需要的话，请重新开始一次探索。"
        ),
        status_code=404,
        user=user,
        links=[(BASE, "返回方向探索")],
    )


def _sendable(plan) -> set[str]:
    sources = {f"{source.entry.item_id}:{field.name}" for source in plan.sources for field in source.fields if field.status != BLOCKED}

    return sources | {f"{PREFERENCES_KEY}:{item.field}" for item in plan.preferences if item.status in (SEND, LEFT_OUT)}


def _sent(plan) -> set[str]:
    sources = {f"{source.entry.item_id}:{name}" for source in plan.sources for name in source.sent}

    return sources | {f"{PREFERENCES_KEY}:{item.field}" for item in plan.sent_preferences}


@router.get(f"{BASE}/tasks/{{task_id}}")
def task_page(task_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    task = directions_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    if task.stage is Stage.REVIEW:
        return review_page(request, user, task, notice=presentation.NOTICES.get(notice))

    return task_view(request, user, task, notice=presentation.NOTICES.get(notice))


def review_page(request: Request, user: CurrentUser, task, *, notice=None, error=None, status_code=200) -> Response:
    groups = []

    for group, items in presentation.preference_groups(task.plan.preferences):
        groups.append(
            (
                group,
                [item for item in items if item.status in (SEND, LEFT_OUT, BLOCKED)],
                presentation.unfilled_text(item.field for item in items if item.status not in (SEND, LEFT_OUT, BLOCKED)),
            )
        )

    return render(
        request,
        "direction_review.html",
        {
            "area": "directions",
            "section": "explore",
            "task": task,
            "plan": task.plan,
            "groups": groups,
            "notice": notice,
            "error": error,
            "NOTICE": presentation.NOTICE,
            "SEND": SEND,
            "LEFT_OUT": LEFT_OUT,
            "BLOCKED": BLOCKED,
            "PREFERENCES_KEY": PREFERENCES_KEY,
            "found_labels": analysis_text.found_labels,
            "preference_label": presentation.condition_label,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{BASE}/tasks/{{task_id}}/confirm")
def confirm(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Update the scope, or confirm it and start: the only post that starts a task.

    Fields the form unticked since the review are never sent: a start whose ticks differ from the reviewed scope
    becomes a new review instead.
    """
    directions = directions_of(request)
    task = directions.store.get(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    if task.stage is not Stage.REVIEW:
        return see_other(f"{BASE}/tasks/{task.id}" + ("" if task.stage is Stage.CANCELLED else "?notice=already"))

    sendable = _sendable(task.plan)
    ticked = set(form.getlist("send")) & sendable

    if ticked != _sent(task.plan):
        inputs = replace(task.inputs, left_out=frozenset(sendable - ticked))
        notice = "scope_changed" if form.get("action") == "start" else "scope_updated"

        return updated_review(request, user, task, inputs, notice)

    if form.get("action") != "start":
        return see_other(f"{BASE}/tasks/{task.id}")

    outcome, started = directions.store.begin(task.id, user.id, user.token_hash, form.get("fingerprint", ""))

    if outcome is Start.STARTED:
        rt = runtime(request)
        directions.executor.submit(run_task, directions.store, rt.database, rt.clock, started.id, directions.provider_for)

        return see_other(f"{BASE}/tasks/{started.id}")

    if outcome is Start.ALREADY:
        return see_other(f"{BASE}/tasks/{task.id}?notice=already")

    if outcome is Start.BUSY:
        return review_page(
            request, user, task, error="你已经有一个探索正在进行。请等它结束，或者取消它之后再开始。", status_code=409
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


def updated_review(request: Request, user: CurrentUser, task, inputs: ExplorationInputs, notice: str) -> Response:
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            build_plan(db, user.id, inputs, rt.now())
    except InputProblem as problem:
        return review_page(request, user, task, error=problem.message, status_code=422)

    return new_review(request, user, inputs, replaces=task.id, notice=notice)


@router.post(f"{BASE}/tasks/{{task_id}}/cancel")
def cancel(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    task = directions_of(request).store.cancel(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    return see_other(safe_next(form.get("next", "")) or f"{BASE}/tasks/{task.id}")


@router.post(f"{BASE}/tasks/{{task_id}}/again")
def again(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Review the same choice once more, read again from the library, as a new task to confirm."""
    task = directions_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    return new_review(request, user, task.inputs)


@router.get(f"{BASE}/tasks/{{task_id}}/status")
def task_status(task_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    task = directions_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return JSONResponse({"stage": "gone"}, status_code=404)

    return JSONResponse(
        {
            "stage": task.stage.value,
            "step": presentation.STEP if task.stage is Stage.RUNNING else None,
            "sends": sends_text(task.handed),
        }
    )


# --- the result -----------------------------------------------------------------------------------------------------


def checked_task(request: Request, user: CurrentUser, task):
    """A finished task as it may be shown now, and how what its result rests on stands (None without a result).

    Checked on every view, for a complete result and a stale one alike: a source deleted since withdraws the result
    for good (ExplorationTaskStore.withdraw); None for the task when it is gone meanwhile."""
    outcome = task.outcome

    if outcome is None or outcome.result is None:
        return task, None

    rt = runtime(request)

    with rt.database.transaction() as db:
        state = basis_now(db, user.id, task.basis, rt.now())

    if state in (SourceState.CURRENT, SourceState.CHANGED):
        return task, state

    return directions_of(request).store.withdraw(task.id, user.id, user.token_hash), None


def task_view(request: Request, user: CurrentUser, task, *, notice=None, error=None, status_code=200) -> Response:
    context = {
        "area": "directions",
        "section": "explore",
        "task": task,
        "notice": notice,
        "error": error,
        "STEP": presentation.STEP,
        "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
    }

    if task.stage is Stage.DONE:
        task, state = checked_task(request, user, task)

        if task is None:
            return gone_page(request, user)

        context.update(task=task, **result_context(request, user, task, state))

    if task.stage is Stage.CANCELLED:
        context["cancelled_by"] = presentation.CANCELLED_BY.get(task.cancelled_by, "")

    return render(request, "direction_task.html", context, user=user, status_code=status_code)


def _decisions_for(db, owner_id: str, result: ExplorationResult) -> dict:
    decisions = {}

    for direction in result.directions:
        decision = decision_in_use(db, owner_id, name_key(direction.name))

        if decision is not None:
            decisions[name_key(direction.name)] = decision

    return decisions


def result_context(request: Request, user: CurrentUser, task, state) -> dict:
    outcome = task.outcome
    rt = runtime(request)
    current = outcome.trusted is not None and state is SourceState.CURRENT
    saved, row, decisions = Saved.NEVER, None, {}

    if outcome.result is not None:
        with rt.database.transaction() as db:
            if outcome.trusted is not None:
                # The same judgement as saving and deciding make (app.directions.actions.saved_of).
                saved, row = saved_of(db, user.id, outcome.trusted.result_id, task.saved_id, rt.now())

            decisions = _decisions_for(db, user.id, outcome.result)

    places = {(source.entry.item_id, source.entry.version_id): (source.title, source.entry.category) for source in task.plan.sources}

    return {
        "outcome": outcome,
        "message": presentation.outcome_message(outcome, task.withdrawn),
        "changed_since": outcome.trusted is not None and state is SourceState.CHANGED,
        "cards": presentation.cards(outcome.result, decisions, places) if outcome.result is not None else [],
        "can_decide": current and saved in (Saved.NEVER, Saved.IN_USE),
        "can_save": current and saved is Saved.NEVER,
        "saved": row if saved is Saved.IN_USE else None,
        "saved_in_bin": saved is Saved.IN_BIN,
        "saved_gone": saved is Saved.GONE,
        "SAVED_GONE": presentation.SAVED_GONE,
        "expires_at": rt.now() + timedelta(seconds=directions_of(request).store.remaining(task)),
        "CHANGED_SINCE": presentation.CHANGED_SINCE,
        "DECISIONS": presentation.DECISIONS,
        "decision_text": presentation.decision_text,
    }


def _trusted_of(task):
    """The trusted snapshot of a task's complete result still shown, or None."""
    if task.stage is not Stage.DONE or task.outcome is None or task.withdrawn:
        return None

    return task.outcome.trusted


def _seen(form: FormData) -> SeenDecision | None:
    decision_id = form.get("decision_id", "")

    if not decision_id:
        return None

    try:
        return SeenDecision(decision_id, int(form.get("revision", "")))
    except ValueError:
        raise HTTPException(status_code=400) from None


def _state(form: FormData) -> str:
    state = form.get("state", "")

    if state not in STATES:
        raise HTTPException(status_code=400)

    return state


def _revision(form: FormData) -> int:
    try:
        return int(form.get("expected_revision", ""))
    except ValueError:
        raise HTTPException(status_code=400) from None


@router.post(f"{BASE}/tasks/{{task_id}}/save")
def save(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Save the whole result, once the user asks: checked again here, never on the page's word.

    The task is claimed for the whole write and released naming the exploration saved, so the task knows it saved
    one before any other write of it can start (app.directions.tasks)."""
    store = directions_of(request).store
    claim, task = store.claim_write(task_id, user.id, user.token_hash)

    if claim is Claim.GONE:
        return gone_page(request, user)

    if claim is Claim.BUSY:
        return task_view(request, user, task, error=WRITING, status_code=409)

    saved_id = None

    try:
        trusted = _trusted_of(task)

        if trusted is None:
            return task_view(request, user, task, error="这次结果不能保存。", status_code=409)

        rt = runtime(request)

        try:
            with rt.database.transaction() as db:
                exploration_id = save_result(db, user.id, trusted, rt.now(), task.saved_id).id
        except NotAllowed as refused:
            return task_view(request, user, task, error=refused.reason, status_code=409)

        # Committed: from here on the task knows its result was saved, whatever becomes of the record.
        saved_id = exploration_id
    finally:
        store.release_write(task.id, saved_id)

    return see_other(f"{BASE}/tasks/{task.id}?notice=saved")


@router.post(f"{BASE}/tasks/{{task_id}}/decide")
def decide_on_task(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    state, seen = _state(form), _seen(form)
    store = directions_of(request).store
    claim, task = store.claim_write(task_id, user.id, user.token_hash)

    if claim is Claim.GONE:
        return gone_page(request, user)

    if claim is Claim.BUSY:
        return task_view(request, user, task, error=WRITING, status_code=409)

    try:
        trusted = _trusted_of(task)

        if trusted is None:
            return task_view(request, user, task, error="不能根据这次结果做决定。", status_code=409)

        rt = runtime(request)

        try:
            with rt.database.transaction() as db:
                decide_on_result(db, user.id, trusted, form.get("name", ""), state, seen, rt.now(), task.saved_id)
        except NotAllowed as refused:
            return task_view(request, user, task, error=refused.reason, status_code=409)
        except RecordConflict:
            return task_view(request, user, task, error=CONFLICT, status_code=409)
    finally:
        store.release_write(task.id)

    return see_other(f"{BASE}/tasks/{task.id}?notice=decided")


# --- the records ----------------------------------------------------------------------------------------------------


@router.get(RECORDS)
def records_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return records_view(request, user, notice=presentation.NOTICES.get(notice))


def records_view(request: Request, user: CurrentUser, *, notice=None, error=None, status_code=200) -> Response:
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        decisions = []

        for decision in decisions_in_use(db, user.id):
            state, free = decision_state(db, user.id, decision, now), changeable(db, user.id, decision, now)

            # On a saved exploration in use, a record it rests on beyond this direction's quotes was changed: said
            # so, as deciding on that exploration again would refuse it.
            if decision.basis == "saved_exploration" and state is BasisState.CURRENT and not free:
                state = BasisState.CHANGED

            decisions.append((decision, state, free))
        explorations = [
            (
                exploration,
                [direction["name"] for direction in exploration.content["directions"]],
                exploration_state(db, user.id, exploration, now),
            )
            for exploration in explorations_in_use(db, user.id)
        ]
        # For a search (U3-C3b): C1's rule of each decision's own basis, not whether it may be decided on again.
        handoff, held = kept_for_search(db, user.id, now), held_back(db, user.id, now)

    groups = [
        (label, [entry for entry in decisions if entry[0].state == state]) for state, label in presentation.DECISIONS
    ]

    return render(
        request,
        "direction_records.html",
        {
            "area": "directions",
            "section": "records",
            "groups": groups,
            "has_decisions": bool(decisions),
            "explorations": explorations,
            "handoff": handoff,
            "held": [(entry.decision.name, presentation.held_text(entry.basis)) for entry in held],
            "notice": notice,
            "error": error,
            "DECISIONS": presentation.DECISIONS,
            "basis_text": presentation.basis_text,
            "SOURCE_STATE_LABELS": SOURCE_STATE_LABELS,
            "SourceState": SourceState,
        },
        user=user,
        status_code=status_code,
    )


def _saved_places(db, owner_id: str, result: ExplorationResult) -> dict:
    """How to name the source of each experience quote of a saved exploration: the title of the version quoted."""
    wanted = {(quote.item_id, quote.version_id) for direction in result.directions for quote in direction.evidence}
    places = {}

    for version, category in db.execute(
        select(ExperienceVersion, ExperienceItem.category)
        .join(ExperienceItem, ExperienceItem.id == ExperienceVersion.item_id)
        .where(ExperienceVersion.owner_id == owner_id, ExperienceVersion.id.in_({version_id for _, version_id in wanted}))
    ):
        if (version.item_id, version.id) in wanted:
            places[(version.item_id, version.id)] = (display_title(version.content, category), category)

    return places


def _not_found(request: Request, user: CurrentUser, what: str = "这条记录") -> Response:
    return message_page(
        request,
        title=f"找不到{what}",
        message="它可能已经删除、恢复期已过，或者不属于你。",
        status_code=404,
        user=user,
        links=[(RECORDS, "回到探索记录")],
    )


@router.get(f"{BASE}/explorations/{{exploration_id}}")
def exploration_page(exploration_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return exploration_view(request, user, exploration_id, notice=presentation.NOTICES.get(notice))


def exploration_view(request: Request, user: CurrentUser, exploration_id: str, *, notice=None, error=None, status_code=200) -> Response:
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            exploration = active_direction_record(db, user.id, DirectionExploration, exploration_id)
            state = exploration_state(db, user.id, exploration, now)
            result = ExplorationResult.model_validate(exploration.content)
            decisions = _decisions_for(db, user.id, result)
            places = _saved_places(db, user.id, result)
    except NotFound:
        return _not_found(request, user, "这次探索")

    return render(
        request,
        "direction_exploration.html",
        {
            "area": "directions",
            "section": "records",
            "exploration": exploration,
            "generation": exploration.generation,
            "state": state,
            "state_label": SOURCE_STATE_LABELS[state],
            "current": state is SourceState.CURRENT,
            "cards": presentation.cards(result, decisions, places),
            "notice": notice,
            "error": error,
            "DECISIONS": presentation.DECISIONS,
            "decision_text": presentation.decision_text,
        },
        user=user,
        status_code=status_code,
    )


@router.post(f"{BASE}/explorations/{{exploration_id}}/decide")
def decide_on_exploration(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    state, seen = _state(form), _seen(form)
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            decide_on_saved_exploration(db, user.id, exploration_id, form.get("name", ""), state, seen, rt.now())
    except NotFound:
        return _not_found(request, user, "这次探索")
    except NotAllowed as refused:
        return exploration_view(request, user, exploration_id, error=refused.reason, status_code=409)
    except RecordConflict:
        return exploration_view(request, user, exploration_id, error=CONFLICT, status_code=409)

    return see_other(f"{BASE}/explorations/{exploration_id}?notice=decided")


def _confirm(request: Request, user: CurrentUser, *, title: str, points: list[str], action: str, button: str, cancel: str, hidden: dict) -> Response:
    return render(
        request,
        "confirm.html",
        {
            "area": "directions",
            "section": "records",
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


def _refused(request: Request, user: CurrentUser, reason: str, back: str = RECORDS) -> Response:
    return message_page(
        request, title="这个操作现在不能执行", message=reason, status_code=409, user=user, links=[(back, "返回")]
    )


def _conflicted(request: Request, user: CurrentUser, back: str = RECORDS) -> Response:
    return message_page(
        request,
        title="操作没有执行：这条记录刚刚变了",
        message="你打开页面之后，这条记录在别处改过（可能是另一个标签页）。为了不处理你没有看到的变化，这次操作没有执行。请先看最新状态。",
        status_code=409,
        user=user,
        links=[(back, "查看最新状态")],
    )


def _record_action(request: Request, user: CurrentUser, act, done: str, back: str = RECORDS) -> Response:
    """Run one change of a record in its own transaction; tell what stopped it, never a broken page."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            act(db, rt.now())
    except NotFound:
        return _not_found(request, user)
    except NotAllowed as refused:
        return _refused(request, user, refused.reason, back)
    except RecordConflict:
        return _conflicted(request, user, back)

    return see_other(done)


@router.get(f"{BASE}/explorations/{{exploration_id}}/delete")
def exploration_delete_page(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            exploration = active_direction_record(db, user.id, DirectionExploration, exploration_id)
    except NotFound:
        return _not_found(request, user, "这次探索")

    return _confirm(
        request,
        user,
        title="删除这次探索",
        points=[
            "将删除：这次探索保存的方向、引文和生成信息。",
            "根据它做的决定会保留，标为“依据的方向探索已删除”，之后不能再按它直接修改。",
            "7 天内可以在回收站恢复，也可以提前永久删除；到期后会被永久删除。",
            "已经下载到本机的导出文件不受影响，需要你自己处理。",
        ],
        action=f"{BASE}/explorations/{exploration.id}/delete",
        button="移到回收站",
        cancel=f"{BASE}/explorations/{exploration.id}",
        hidden={"expected_revision": str(exploration.revision)},
    )


@router.post(f"{BASE}/explorations/{{exploration_id}}/delete")
def exploration_delete(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    revision = _revision(form)

    return _record_action(
        request, user, lambda db, now: delete_exploration(db, user.id, exploration_id, revision, now), f"{RECORDS}?notice=deleted"
    )


@router.post(f"{BASE}/explorations/{{exploration_id}}/restore")
def exploration_restore(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    revision = _revision(form)

    return _record_action(
        request,
        user,
        lambda db, now: restore_exploration(db, user.id, exploration_id, revision, now),
        safe_next(form.get("next", "")) or f"{RECORDS}?notice=restored",
    )


@router.get(f"{BASE}/explorations/{{exploration_id}}/purge")
def exploration_purge_page(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            exploration = trashed_direction_record(db, user.id, DirectionExploration, exploration_id, rt.now())
    except NotFound:
        return _not_found(request, user, "这次探索")

    return _confirm(
        request,
        user,
        title="永久删除这次探索",
        points=[
            "这次探索保存的方向、引文和生成信息会被永久删除，不能恢复。",
            "根据它做的决定会保留，标为“依据的方向探索已永久删除”。",
            "已经下载到本机的导出文件不受影响，需要你自己处理。",
        ],
        action=f"{BASE}/explorations/{exploration.id}/purge",
        button="永久删除",
        cancel="/experiences?tab=trash",
        hidden={"expected_revision": str(exploration.revision)},
    )


@router.post(f"{BASE}/explorations/{{exploration_id}}/purge")
def exploration_purge(exploration_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    revision = _revision(form)

    return _record_action(
        request,
        user,
        lambda db, now: request_exploration_purge(db, user.id, exploration_id, revision, now),
        "/experiences?tab=trash",
        back="/experiences?tab=trash",
    )


# --- decisions ------------------------------------------------------------------------------------------------------


@router.post(f"{BASE}/decisions/{{decision_id}}/state")
def decision_state_change(decision_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Change a decision's state, keeping what it rests on (app.directions.actions.change_decision)."""
    state, revision = _state(form), _revision(form)
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            change_decision(db, user.id, decision_id, revision, state, rt.now())
    except NotFound:
        return _not_found(request, user)
    except NotAllowed as refused:
        return records_view(request, user, error=refused.reason, status_code=409)
    except RecordConflict:
        return records_view(request, user, error=CONFLICT, status_code=409)

    return see_other(f"{RECORDS}?notice=decided")


@router.get(f"{BASE}/decisions/{{decision_id}}/own")
def own_choice_page(decision_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """Ask before a decision resting on an exploration becomes the user's own choice."""
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            decision = active_direction_record(db, user.id, DirectionDecision, decision_id)
            state = decision_state(db, user.id, decision, now)
    except NotFound:
        return _not_found(request, user, "这条决定")

    if decision.basis == "personal":
        return see_other(RECORDS)

    return render(
        request,
        "direction_own.html",
        {
            "area": "directions",
            "section": "records",
            "decision": decision,
            "basis": presentation.basis_text(decision, state),
            "DECISIONS": presentation.DECISIONS,
        },
        user=user,
    )


@router.post(f"{BASE}/decisions/{{decision_id}}/own")
def own_choice(decision_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    state, revision = _state(form), _revision(form)

    return _record_action(
        request,
        user,
        lambda db, now: make_own_choice(db, user.id, decision_id, revision, state, now),
        f"{RECORDS}?notice=own",
    )


@router.post(f"{BASE}/decisions/{{decision_id}}/undo")
def decision_undo(decision_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Undo a decision: back to undecided; the decision waits in the recycle bin."""
    revision = _revision(form)
    back = safe_next(form.get("next", "")) or RECORDS

    return _record_action(
        request,
        user,
        lambda db, now: delete_decision(db, user.id, decision_id, revision, now),
        f"{back}{'&' if '?' in back else '?'}notice=undone",
        back=back,
    )


@router.post(f"{BASE}/decisions/{{decision_id}}/restore")
def decision_restore(decision_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    revision = _revision(form)

    return _record_action(
        request,
        user,
        lambda db, now: restore_decision(db, user.id, decision_id, revision, now),
        safe_next(form.get("next", "")) or f"{RECORDS}?notice=restored",
    )


@router.get(f"{BASE}/decisions/{{decision_id}}/purge")
def decision_purge_page(decision_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            decision = trashed_direction_record(db, user.id, DirectionDecision, decision_id, rt.now())
    except NotFound:
        return _not_found(request, user, "这条决定")

    return _confirm(
        request,
        user,
        title=f"永久删除决定：{decision.name}",
        points=[
            "这条决定会被永久删除，不能恢复。",
            "已经下载到本机的导出文件不受影响，需要你自己处理。",
        ],
        action=f"{BASE}/decisions/{decision.id}/purge",
        button="永久删除",
        cancel="/experiences?tab=trash",
        hidden={"expected_revision": str(decision.revision)},
    )


@router.post(f"{BASE}/decisions/{{decision_id}}/purge")
def decision_purge(decision_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    revision = _revision(form)

    return _record_action(
        request,
        user,
        lambda db, now: request_decision_purge(db, user.id, decision_id, revision, now),
        "/experiences?tab=trash",
        back="/experiences?tab=trash",
    )
