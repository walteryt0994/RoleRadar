"""Pages of a real structuring (U2-C3): what would be sent, confirming it, its progress, cancelling it.

Only an app with a real service whose approval covers structuring offers
them (live_structuring.StructuringRuntime.offered); otherwise they are not
found, and an import page offers the local rules alone, as before. The
confirmation page shows every word that would go out, after masking, what
was kept back and what the rules cannot find; its form carries the
fingerprint of what it showed, and starting checks it again. A started run
goes to the background. The review page shows its progress, which the
script of the analysis pages (job_analysis.js) asks for here, and a form
to cancel it.
"""
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.library.errors import NotFound
from app.library.identity import CurrentUser
from app.library.imports import draft_of, locator, open_import
from app.library.imports_web import BASE, gone_page
from app.live_structuring.service import (
    NOTICE,
    NotSendable,
    StructuringRuntime,
    Unconfirmed,
    cancel,
    fingerprint,
    run,
    start,
)
from app.library.models import StructuringState
from app.library.outbound_resume import MASK_NAMES, WITHHELD_NAMES
from app.library.review import (
    INTERRUPTED,
    LIVE_FAILURE_TEXTS,
    STRUCTURABLE,
    Asked,
    can_ask_again,
    is_live,
    running_here,
    split_outcome,
    structuring_of,
)
from app.library.sends import sends_text
from app.library.structuring_runs import Stop, Stopping
from app.library.web import checked_form, current_user, message_page, render, runtime, see_other
from app.i18n import MessageMap, lazy, t

# What the progress of a run says, after 正在 (job_analysis.js).
STEP = lazy("imp.live.step")
NOTICES = MessageMap({
    # A confirmation is used up by the run it begins: replayed afterwards, it no longer holds either.
    "changed": "imp.live.changed",
})
# Where a start or a cancel lands: the review page, with one of its notices (review_web.NOTICES).
STARTED = {Asked.ALREADY: "already_structured", Asked.RUNNING: "structuring_running", None: "structuring_started"}

router = APIRouter()


def install_structuring(app: FastAPI, *, live=None, executor=None) -> StructuringRuntime:
    """The app's real structuring: with live (a LiveService), offered when its approval covers it."""
    library = app.state.library
    structuring = StructuringRuntime(
        runs=library.runs,
        executor=executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="structuring"),
        live=live,
    )
    app.state.structuring = structuring
    # Logging out, or signing in as someone else, stops the runs the session started.
    library.session_end_listeners.append(library.runs.end_session)
    app.include_router(router)

    return structuring


def structuring_runtime(request: Request) -> StructuringRuntime:
    return request.app.state.structuring


def _not_offered(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title=t("imp.live.1"),
        message=t("imp.live.2"),
        status_code=404,
        user=user,
        links=[(BASE, t("imp.live.3"))],
    )


@router.get("/imports/{import_id}/structure/confirm")
def confirm_page(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    return _confirmation(request, user, import_id)


def _confirmation(request: Request, user: CurrentUser, import_id: str, *, notice: str = "", status_code: int = 200):
    """Everything a real structuring of this import would send, to whom and under which terms."""
    rt = runtime(request)
    structuring = structuring_runtime(request)

    if not structuring.offered:
        return _not_offered(request, user)

    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
            draft = draft_of(db, row.id)
            current = structuring_of(db, row.id)
    except NotFound:
        return gone_page(request, user)

    if row.state not in STRUCTURABLE or not row.segments:
        return see_other(f"{BASE}/{row.id}")

    if current is not None and not can_ask_again(current, now):
        # Structured already, or running: the review page says which.
        return see_other(f"{BASE}/{row.id}/review")

    live = structuring.live
    offer = structuring.offer(row.segments)
    sent = offer.request.outbound
    refusal = live.refusal()
    recipient = live.recipient

    return render(
        request,
        "import_structure_confirm.html",
        {
            "section": "imports",
            "row": row,
            "draft": draft,
            "name": row.filename or t("imp.live.4"),
            "recipient": recipient,
            "notice_lines": NOTICE,
            "masked": [(label, sent.masked[kind]) for kind, label in MASK_NAMES.items() if sent.masked.get(kind)],
            "withheld": [(label, sent.withheld[kind]) for kind, label in WITHHELD_NAMES.items() if sent.withheld.get(kind)],
            "segments": [(locator(row.segments[number - 1]), text) for number, text in sent.sent],
            "offer": offer,
            "refusal": LIVE_FAILURE_TEXTS[f"gate_{refusal.value}"] if refusal is not None else None,
            "fingerprint": fingerprint(user.id, row, current.attempt if current is not None else None, offer.request, live),
            "notice": NOTICES.get(notice),
            "now": now,
        },
        user=user,
        status_code=status_code,
    )


@router.post("/imports/{import_id}/structure/confirm")
def confirm(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    """Begin the run the page showed, if it is still exactly that; nothing is sent in this request."""
    rt = runtime(request)
    structuring = structuring_runtime(request)

    if not structuring.offered:
        return _not_offered(request, user)

    if structuring.live.refusal() is not None:
        # The page says why; nothing began and nothing was reserved.
        return _confirmation(request, user, import_id, status_code=409)

    try:
        asked, attempt, sending = start(
            rt.database, structuring, user.id, user.token_hash, import_id, form.get("fingerprint", ""), rt.now
        )
    except NotFound:
        return gone_page(request, user)
    except Unconfirmed:
        return _confirmation(request, user, import_id, notice="changed", status_code=409)
    except NotSendable:
        return _confirmation(request, user, import_id, status_code=409)

    if asked == Asked.NOT_READ:
        return see_other(f"{BASE}/{import_id}")

    if attempt is not None:
        structuring.executor.submit(run, rt.database, structuring, user.id, import_id, attempt, sending, rt.now)

    return see_other(f"{BASE}/{import_id}/review?notice={STARTED[asked]}")


@router.post("/imports/{import_id}/structure/cancel")
def cancel_structuring(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    """Cancel the run the page showed: what it did not hand on is never sent; what it did is not called back."""
    rt = runtime(request)
    structuring = structuring_runtime(request)

    try:
        with rt.database.transaction() as db:
            open_import(db, user.id, import_id, rt.now())
            current = structuring_of(db, import_id)
    except NotFound:
        return gone_page(request, user)

    attempt = form.get("attempt", "")

    if (
        current is None
        or current.state != StructuringState.RUNNING
        or current.attempt != attempt
        or not is_live(current.structurer)
    ):
        return see_other(f"{BASE}/{import_id}/review?notice=structuring_not_running")

    stopping = cancel(rt.database, structuring, user.id, import_id, attempt, rt.now)

    return see_other(f"{BASE}/{import_id}/review?notice={_cancelled(rt, import_id, attempt, stopping)}")


def _cancelled(rt, import_id: str, attempt: str, stopping: Stopping) -> str:
    """What cancelling did, as the run's record says it now (whether its request went out is the record's to say)."""
    if stopping == Stopping.TOO_LATE:
        return "structuring_too_late"

    with rt.database.transaction() as db:
        current = structuring_of(db, import_id)

    if current is None or current.attempt != attempt or current.state != StructuringState.FAILED:
        return "structuring_not_running"

    if current.outcome == INTERRUPTED:
        return "structuring_interrupted"

    if current.outcome is not None and split_outcome(current.outcome)[0] == Stop.CANCELLED:
        return "structuring_cancelled"

    # It ended on its own before the cancel took effect.
    return "structuring_not_running"


@router.get("/imports/{import_id}/structure/status")
def structuring_status(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """Whether a real run of this import is going on here, and how many requests it handed on."""
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            open_import(db, user.id, import_id, now)
            current = structuring_of(db, import_id)
    except NotFound:
        return JSONResponse({"stage": "gone"}, status_code=404)

    handed = running_here(rt.runs, current, now)

    if handed is None:
        return JSONResponse({"stage": "done"})

    # What the page says after 已交给服务的请求：counts and generations, one still out as maybe.
    return JSONResponse({"stage": "running", "step": str(STEP), "sends": sends_text(handed)})

