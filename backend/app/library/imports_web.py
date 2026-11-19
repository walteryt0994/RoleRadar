"""Pages of the resume import (U2-B1): 我的资料 → 导入简历.

The import page takes one file or pasted text after the user agreed that it
is kept here for at most seven days. An import's page shows what was read,
where it was found and when it expires; it offers the original file and the
text for download, reading again when a failure was not the file's, asking
for candidates to check (review_web, U2-B2a), and discarding. Nothing here
writes to the experiences, the profile or the preferences, and nothing is
sent to any outside service.

Since U2-B3a an import can be saved as a draft to keep checking it later:
it then has no expiry, is never discarded (an older page's discard form is
refused), and is deleted instead, into the recycle bin for seven days, from
where it is restored or removed for good.

The upload is read as it streams in (upload.receive_form with
imports.IMPORT_FORM), like an attachment's; the other posts are ordinary
small forms. Every route finds the import through the session's owner, so
another user's import, or one that ended or expired, is simply not found.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.library.attachments import UploadRejected, UploadTokenRejected
from app.library.errors import FileStoreError, NotFound, StorageError
from app.library.files import IMPORTS, read_chunks
from app.library.identity import CurrentUser
from app.library.imports import (
    BOTH,
    IMPORT_FORM,
    IMPORT_WINDOW,
    INTERRUPTED,
    MAX_OPEN_IMPORTS,
    MISSING_FILE,
    NO_CONSENT,
    NOTHING,
    TEXT_TOO_LONG,
    Ended,
    ImportChanged,
    RestoreClosed,
    Saved,
    SavedDraft,
    Stored,
    binned_draft,
    binned_drafts,
    clearing_count,
    delete_draft,
    discard,
    draft_of,
    import_text,
    open_import,
    open_imports,
    pasted_text,
    purge_draft,
    read_import,
    restore_draft,
    retryable,
    save_draft,
    start_again,
    store_file,
    store_text,
    taken_places,
)
from app.library.lifecycle import RESTORE_WINDOW
from app.library.models import CandidateState, ImportSource, ImportState, StructuringState
from app.library.resume_reader import (
    BUSY,
    CRASHED,
    DAMAGED,
    EMPTY_PAGES,
    ENCRYPTED,
    MACROS,
    MAX_PDF_PAGES,
    MAX_TEXT_CHARS,
    NO_TEXT,
    NOT_WORD,
    READ_SECONDS,
    TIMEOUT,
    TOO_BIG_INSIDE,
    TOO_MANY_PAGES,
    TRUNCATED,
    UNREADABLE_PAGES,
)
from app.library.review import STRUCTURABLE, can_ask_again, candidates_of, failure_text, is_live, structuring_of
from app.library.web import NOTICES as LIBRARY_NOTICES
from app.library.web import (
    DOWNLOAD_POLICY,
    CsrfRejected,
    NotAuthenticated,
    _drain,
    _expected_revision,
    _tokens_match,
    check_same_origin,
    checked_form,
    confirm_page,
    content_disposition,
    current_user,
    message_page,
    render,
    runtime,
    see_other,
)
from app.library.upload import receive_form
from app.i18n import MessageMap, lazy, t

BASE = "/imports"

SOURCE_LABELS = {ImportSource.PDF: "PDF", ImportSource.DOCX: lazy("imp.web.1"), ImportSource.TEXT: lazy("imp.web.2")}
STATE_LABELS = MessageMap({
    ImportState.READING: "imp.web.3",
    ImportState.READ: "imp.web.4",
    ImportState.PARTIAL: "imp.web.5",
    ImportState.FAILED: "imp.web.6",
})
FAILURES = {
    ENCRYPTED: lazy("imp.web.7"),
    NO_TEXT: lazy("imp.web.8"),
    DAMAGED: lazy("imp.web.9"),
    TOO_MANY_PAGES: lazy("imp.web.10", p0=MAX_PDF_PAGES),
    NOT_WORD: lazy("imp.web.11"),
    MACROS: lazy("imp.web.12"),
    TOO_BIG_INSIDE: lazy("imp.web.13"),
    TIMEOUT: lazy("imp.web.14", p0=format(READ_SECONDS, 'g')),
    CRASHED: lazy("imp.web.15"),
    BUSY: lazy("imp.web.16"),
    MISSING_FILE: lazy("imp.web.17"),
    INTERRUPTED: lazy("imp.web.18"),
}
RETRY_HINT = lazy("imp.web.19")
FALLBACK_HINT = lazy("imp.web.20")
NOTICES = {
    "import_added": lazy("imp.web.21"),
    "import_already_added": lazy("imp.web.22"),
    "import_read_again": lazy("imp.web.23"),
    "import_discarded": lazy("imp.web.24"),
    "import_discard_pending": (
        lazy("imp.web.25")
    ),
    "import_ended": lazy("imp.web.26"),
    "import_ended_structuring": lazy("imp.web.27"),
    "draft_saved": (
        lazy("imp.web.28")
    ),
    "draft_already": lazy("imp.web.29"),
    "draft_deleted": (
        lazy("imp.web.30", p0=RESTORE_WINDOW.days)
    ),
    "draft_restored": lazy("imp.web.31"),
    # Keeping the original and ending the import (U2-B3b, finish_web).
    "original_kept": (
        lazy("imp.web.32")
    ),
    "original_kept_already": lazy("imp.web.33"),
    "import_finished": (
        lazy("imp.web.34")
    ),
    "import_finish_pending": (
        lazy("imp.web.35")
    ),
    "draft_finished": (
        lazy("imp.web.36", p0=RESTORE_WINDOW.days)
    ),
}
TRASH = "/experiences?tab=trash"
STORE_PROBLEMS = {
    Stored.FORM_USED: (
        409,
        lazy("imp.web.37"),
    ),
    Stored.FULL: (
        409,
        lazy("imp.web.38", p0=MAX_OPEN_IMPORTS),
    ),
    Stored.FAILED: (
        500,
        lazy("imp.web.39"),
    ),
    Stored.FAILED_LEFTOVER: (
        500,
        lazy("imp.web.40"),
    ),
}
# A refusal at the limit gives the typed text back with the form, so it is not lost.
FULL_WITH_TEXT = (
    lazy("imp.web.41", p0=MAX_OPEN_IMPORTS)
)
DRAFT_NOT_DISCARDED = (
    lazy("imp.web.42", p0=RESTORE_WINDOW.days)
)

logger = logging.getLogger("roleradar.library")

router = APIRouter()


def install_imports(app: FastAPI) -> None:
    """Attach the import pages; the app's library runtime holds what they use."""
    app.include_router(router)


# --- pages ----------------------------------------------------------------------------


def imports_page(
    request: Request,
    user: CurrentUser,
    *,
    notice: str | None = None,
    error: str | None = None,
    text: str = "",
    status_code: int = 200,
    start_mode: str = "",
) -> Response:
    """The import form (with a new one-time key) and the owner's open imports."""
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        entries = open_imports(db, user.id, now)
        drafts = {row.id: draft_of(db, row.id) for row in entries}
        binned = len(binned_drafts(db, user.id, now))
        clearing = clearing_count(db, user.id, now)
        full = taken_places(db, user.id, now) >= MAX_OPEN_IMPORTS

    return render(
        request,
        "imports.html",
        {
            "section": "imports",
            "entries": entries,
            "drafts": drafts,
            "binned": binned,
            "clearing": clearing,
            "full": full,
            "upload_key": secrets.token_urlsafe(24),
            "text": text,
            "start_mode": "text" if start_mode == "text" else "",
            "unsaved_input": bool(text.strip()),
            "notice": notice,
            "error": error,
            "now": now,
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


def _labels() -> dict:
    return {
        "SOURCE_LABELS": SOURCE_LABELS,
        "STATE_LABELS": STATE_LABELS,
        "MAX_OPEN_IMPORTS": MAX_OPEN_IMPORTS,
        "MAX_TEXT_CHARS": MAX_TEXT_CHARS,
        "IMPORT_DAYS": IMPORT_WINDOW.days,
        "RESTORE_DAYS": RESTORE_WINDOW.days,
    }


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title=t("imp.web.43"),
        message=(
            t("imp.web.44", p0=IMPORT_WINDOW.days)
        ),
        status_code=404,
        user=user,
        links=[(BASE, t("imp.web.45")), (TRASH, t("imp.web.46"))],
    )


def _problem_lines(row) -> list[str]:
    """What the page says about a failed or partial reading."""
    if row.state == ImportState.FAILED:
        reason = FAILURES.get(row.outcome, FAILURES[CRASHED])

        return [reason, RETRY_HINT if retryable(row) else FALLBACK_HINT]

    lines = []
    details = row.details or {}

    if details.get(UNREADABLE_PAGES):
        pages = t("imp.web.47").join(str(number) for number in details[UNREADABLE_PAGES])
        lines.append(t("imp.web.48", p0=pages))

    if details.get(EMPTY_PAGES):
        pages = t("imp.web.47").join(str(number) for number in details[EMPTY_PAGES])
        lines.append(t("imp.web.49", p0=pages))

    if details.get(TRUNCATED):
        lines.append(t("imp.web.50", p0=MAX_TEXT_CHARS // 10000))

    return lines


@router.get("/imports")
def imports_home(request: Request, notice: str = "", mode: str = "", user: CurrentUser = Depends(current_user)):
    # A save made from another page's leave dialog lands here too, and says what it saved. ?mode=text opens the paste
    # panel first (改为粘贴原文 after a failed reading): it only chooses which panel shows.
    return imports_page(request, user, notice=NOTICES.get(notice) or LIBRARY_NOTICES.get(notice), start_mode=mode)


@router.get("/imports/{import_id}")
def import_detail(import_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
            draft = draft_of(db, row.id)
            structuring = structuring_of(db, row.id)
            candidates = candidates_of(db, user.id, row.id)
    except NotFound:
        return gone_page(request, user)

    failed = structuring is not None and structuring.state == StructuringState.FAILED
    can_structure = row.state in STRUCTURABLE and can_ask_again(structuring, now)
    # With a real service for structuring, what it would send of this import, against its limits (U2-C3).
    live_offer = request.app.state.structuring.offer(row.segments) if can_structure and row.segments else None

    return render(
        request,
        "import.html",
        {
            "section": "imports",
            "row": row,
            "draft": draft,
            "name": row.filename or t("imp.web.2"),
            # A file it holds can be kept as an attachment (U2-B3b); pasted text has none.
            "keepable": row.storage_key is not None and row.source != ImportSource.TEXT,
            "problems": _problem_lines(row),
            "can_retry": retryable(row),
            "structurable": row.state in STRUCTURABLE,
            "structuring": structuring,
            "can_structure": can_structure,
            "structuring_failure": failure_text(structuring.structurer, structuring.outcome) if failed else None,
            "live_running": structuring is not None and is_live(structuring.structurer),
            "live_offer": live_offer,
            "candidate_counts": {
                "total": len(candidates),
                "pending": sum(1 for candidate in candidates if candidate.state == CandidateState.PENDING),
                "excluded": sum(1 for candidate in candidates if candidate.state == CandidateState.EXCLUDED),
                "adopted": sum(1 for candidate in candidates if candidate.state == CandidateState.ADOPTED),
            },
            "notice": NOTICES.get(notice),
            "now": now,
            **_labels(),
        },
        user=user,
    )


# --- taking in a file or text ---------------------------------------------------------


@router.post("/imports")
async def start_import(request: Request):
    """Receive one file or pasted text, keep it, and read a file at once.

    The session and the post's origin are checked before the body is read,
    the CSRF token, which comes first in the body, before any byte of a file
    is written. Nothing is kept without the consent box ticked.
    """
    rt = runtime(request)
    chunks = request.stream()
    # FormSizeLimit has made sure the length is there and within the import limit.
    length = int(request.headers["content-length"])

    try:
        user = await run_in_threadpool(current_user, request)
        check_same_origin(request)
    except (NotAuthenticated, CsrfRejected):
        await _drain(chunks, length)
        raise

    try:
        received = await receive_form(
            chunks,
            content_type=request.headers.get("content-type", ""),
            content_length=length,
            files=rt.files,
            token_ok=lambda value: _tokens_match(user.csrf_token, value),
            form=IMPORT_FORM,
        )
    except UploadTokenRejected:
        await _drain(chunks, length)
        raise CsrfRejected()
    except UploadRejected as rejected:
        if rejected.drain:
            await _drain(chunks, length)

        return await run_in_threadpool(
            imports_page,
            request,
            user,
            error=rejected.message,
            text=pasted_text(rejected.values.get("text", "")),
            status_code=rejected.status_code,
        )

    text = pasted_text(received.values.get("text", ""))
    has_file = received.file_type is not None
    has_text = bool(text.strip())
    problem = None
    # The form switches between uploading a file and pasting text (owner redesign, plan 3.4); only the way chosen is
    # read. A client that does not say (an older page) still has to send one of the two.
    mode = received.values.get("mode", "")

    if mode == "text" and has_file:
        if received.staging is not None:
            await run_in_threadpool(received.staging.discard)

        has_file = False
    elif mode == "file" and has_text:
        text, has_text = "", False

    if has_file and has_text:
        problem = (BOTH, 422)
    elif not has_file and not has_text:
        problem = (NOTHING, 422)
    elif received.values.get("consent") != "yes":
        problem = (NO_CONSENT, 422)
    elif len(text) > MAX_TEXT_CHARS:
        problem = (TEXT_TOO_LONG, 413)

    if problem is not None:
        if received.staging is not None:
            await run_in_threadpool(received.staging.discard)

        message, status_code = problem

        return await run_in_threadpool(imports_page, request, user, error=message, text=text, status_code=status_code)

    if has_file:
        stored, import_id = await run_in_threadpool(
            store_file,
            rt.database,
            rt.files,
            user.id,
            received.upload_key,
            received.filename,
            received.file_type,
            received.staging,
            rt.now(),
        )

        if stored == Stored.ADDED:
            state = await run_in_threadpool(read_import, rt.database, rt.files, rt.readings, user.id, import_id, rt.now)

            if state is None:
                return see_other(f"{BASE}?notice=import_ended")
    else:
        stored, import_id = await run_in_threadpool(store_text, rt.database, user.id, received.upload_key, text, rt.now())

    if stored in (Stored.ADDED, Stored.ALREADY_ADDED):
        return see_other(f"{BASE}/{import_id}?notice=import_{stored}")

    status_code, message = STORE_PROBLEMS[stored]
    kept = text if not has_file else ""

    if stored == Stored.FULL and kept:
        message = FULL_WITH_TEXT

    return await run_in_threadpool(imports_page, request, user, error=message, text=kept, status_code=status_code)


# --- taking it away ----------------------------------------------------------------------


@router.get("/imports/{import_id}/original")
def download_original(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """The uploaded file as it was received, after checking it against its size and checksum."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    if row.storage_key is None:
        return gone_page(request, user)

    try:
        handle = rt.files.open_checked(user.id, row.storage_key, row.size, row.sha256, area=IMPORTS)
    except FileStoreError:
        return message_page(
            request,
            title=t("imp.web.51"),
            message=t("imp.web.52"),
            status_code=500,
            user=user,
            links=[(f"{BASE}/{row.id}", t("imp.web.53"))],
        )

    return StreamingResponse(
        read_chunks(handle),
        media_type=row.media_type,
        headers={
            "Content-Disposition": content_disposition(row.filename),
            "Content-Length": str(row.size),
            "Content-Security-Policy": DOWNLOAD_POLICY,
        },
    )


@router.get("/imports/{import_id}/text")
def download_text(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """What was read, as a plain text file; it is never shown as a page."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    if not row.segments:
        return message_page(
            request,
            title=t("imp.web.54"),
            message=t("imp.web.55"),
            status_code=409,
            user=user,
            links=[(f"{BASE}/{row.id}", t("imp.web.53"))],
        )

    return Response(
        import_text(row).encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(text_filename(row)),
            "Content-Security-Policy": DOWNLOAD_POLICY,
        },
    )


def text_filename(row) -> str:
    if row.filename:
        stem = row.filename.rsplit(".", 1)[0] or t("imp.web.56")

        return t("imp.web.57", p0=stem)

    return t("imp.web.58")


# --- reading again and discarding ----------------------------------------------------------


@router.post("/imports/{import_id}/retry")
def retry_import(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    try:
        started = start_again(rt.database, user.id, import_id, _expected_revision(form), rt.now())
    except NotFound:
        return gone_page(request, user)

    if not started:
        return message_page(
            request,
            title=t("imp.web.59"),
            message=t("imp.web.60"),
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", t("imp.web.53"))],
        )

    state = read_import(rt.database, rt.files, rt.readings, user.id, import_id, rt.now)

    if state is None:
        return see_other(f"{BASE}?notice=import_ended")

    return see_other(f"{BASE}/{import_id}?notice=import_read_again")


def _discard_points(row, structured: bool) -> list[str]:
    if row.filename:
        # A file that could not be read has nothing read to clear.
        what = t("imp.web.61", p0=row.filename) + (t("imp.web.62") if row.segments else "")
    else:
        what = t("imp.web.63")

    if structured:
        what += t("imp.web.64")

    return [
        t("imp.web.65", p0=what),
        t("imp.web.66"),
        t("imp.web.67"),
    ]


@router.get("/imports/{import_id}/discard")
def discard_page(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
            saved = draft_of(db, row.id) is not None
            structured = structuring_of(db, row.id) is not None
    except NotFound:
        return gone_page(request, user)

    if saved:
        return _not_discarded(request, user, row.id)

    return confirm_page(
        request,
        user,
        title=t("imp.web.68"),
        points=_discard_points(row, structured),
        action=f"{BASE}/{row.id}/discard",
        button=t("imp.web.69"),
        cancel=f"{BASE}/{row.id}",
        section="imports",
    )


@router.post("/imports/{import_id}/discard", dependencies=[Depends(checked_form)])
def discard_import(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        ended = discard(rt.database, rt.files, rt.readings, user.id, import_id, rt.now(), runs=rt.runs)
    except NotFound:
        return gone_page(request, user)
    except SavedDraft:
        # Saved (in another tab) after this form was shown: the older decision is not carried out.
        return _not_discarded(request, user, import_id)

    notice = "import_discarded" if ended == Ended.CLEARED else "import_discard_pending"

    return see_other(f"{BASE}?notice={notice}")


def _not_discarded(request: Request, user: CurrentUser, import_id: str) -> Response:
    return message_page(
        request,
        title=t("imp.web.70"),
        message=DRAFT_NOT_DISCARDED,
        status_code=409,
        user=user,
        links=[(f"{BASE}/{import_id}", t("imp.web.71"))],
    )


# --- saved drafts (U2-B3a) -------------------------------------------------------------------


def _not_done(request: Request, user: CurrentUser, title: str, message: str, links: list, status_code: int = 409):
    return message_page(request, title=title, message=message, status_code=status_code, user=user, links=links)


@router.post("/imports/{import_id}/save", dependencies=[Depends(checked_form)])
def save_import(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    back = [(f"{BASE}/{import_id}", t("imp.web.53"))]

    try:
        saved = save_draft(rt.database, user.id, import_id, rt.now)
    except NotFound:
        return gone_page(request, user)
    except StorageError as error:
        logger.error("saving an import as a draft failed (%s)", type(error.__cause__).__name__)

        return _not_done(
            request, user, t("imp.web.72"),
            t("imp.web.73"),
            back, status_code=500,
        )

    if saved == Saved.BUSY:
        return _not_done(
            request, user, t("imp.web.74"),
            t("imp.web.75"),
            back,
        )

    return see_other(f"{BASE}/{import_id}?notice={'draft_saved' if saved == Saved.SAVED else 'draft_already'}")


def _delete_points(row) -> list[str]:
    name = t("imp.web.76", p0=row.filename) if row.filename else t("imp.web.2")

    return [
        t("imp.web.77", p0=name),
        t("imp.web.78", p0=RESTORE_WINDOW.days),
        t("imp.web.79"),
        t("imp.web.80"),
    ]


@router.get("/imports/{import_id}/delete")
def delete_page(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, rt.now())
            saved = draft_of(db, row.id) is not None
    except NotFound:
        return gone_page(request, user)

    if not saved:
        # A temporary import is discarded (no recycle bin), not deleted.
        return see_other(f"{BASE}/{row.id}/discard")

    return confirm_page(
        request,
        user,
        title=t("imp.web.81"),
        points=_delete_points(row),
        action=f"{BASE}/{row.id}/delete",
        button=t("imp.web.82"),
        cancel=f"{BASE}/{row.id}",
        hidden={"expected_revision": str(row.revision)},
        section="imports",
    )


@router.post("/imports/{import_id}/delete")
def delete_import(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    back = [(f"{BASE}/{import_id}", t("imp.web.71"))]

    try:
        delete_draft(rt.database, user.id, import_id, _expected_revision(form), rt.now, runs=rt.runs)
    except NotFound:
        return gone_page(request, user)
    except ImportChanged:
        return _not_done(
            request, user, t("imp.web.83"),
            t("imp.web.84"),
            back,
        )
    except StorageError as error:
        logger.error("deleting an import draft failed (%s)", type(error.__cause__).__name__)

        return _not_done(
            request, user, t("imp.web.83"),
            t("imp.web.85"),
            back, status_code=500,
        )

    # A reading still running stops now; its result would be dropped anyway.
    rt.readings.stop(import_id)

    return see_other(f"{BASE}?notice=draft_deleted")


@router.post("/imports/{import_id}/restore")
def restore_import(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    back = [(TRASH, t("imp.web.86"))]

    try:
        restore_draft(rt.database, user.id, import_id, _expected_revision(form), rt.now)
    except NotFound:
        return gone_page(request, user)
    except RestoreClosed:
        return _not_done(
            request, user, t("imp.web.87"),
            t("imp.web.88"),
            back,
        )
    except ImportChanged:
        return _not_done(
            request, user, t("imp.web.89"),
            t("imp.web.90"),
            back,
        )
    except StorageError as error:
        logger.error("restoring an import draft failed (%s)", type(error.__cause__).__name__)

        return _not_done(
            request, user, t("imp.web.89"),
            t("imp.web.91"),
            back, status_code=500,
        )

    return see_other(f"{BASE}/{import_id}?notice=draft_restored")


def _purge_confirm(request, user, row, error=None, status_code=200):
    name = t("imp.web.76", p0=row.filename) if row.filename else t("imp.web.2")

    return confirm_page(
        request,
        user,
        title=t("imp.web.92"),
        points=[
            t("imp.web.93"),
            t("imp.web.94", p0=name),
            t("imp.web.95"),
            t("imp.web.96"),
            t("imp.web.97"),
        ],
        action=f"{BASE}/{row.id}/purge",
        button=t("imp.web.98"),
        cancel=TRASH,
        hidden={"expected_revision": str(row.revision)},
        acknowledge=t("imp.web.99"),
        error=error,
        status_code=status_code,
        section="trash",
    )


@router.get("/imports/{import_id}/purge")
def purge_page(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row, _ = binned_draft(db, user.id, import_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    return _purge_confirm(request, user, row)


@router.post("/imports/{import_id}/purge")
def purge_import(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)
    back = [(TRASH, t("imp.web.86"))]

    if form.get("acknowledge") != "yes":
        try:
            with rt.database.transaction() as db:
                row, _ = binned_draft(db, user.id, import_id, rt.now())
        except NotFound:
            return gone_page(request, user)

        return _purge_confirm(request, user, row, error=t("imp.web.100"), status_code=422)

    try:
        ended = purge_draft(rt.database, rt.files, user.id, import_id, expected, rt.now)
    except NotFound:
        return gone_page(request, user)
    except ImportChanged:
        return _not_done(
            request, user, t("imp.web.101"),
            t("imp.web.102"),
            back,
        )
    except StorageError as error:
        # Only the deletion's own transaction raises (it was rolled back); a clean-up failure after
        # it was committed comes back as Ended.PENDING (Codex U2B3A-F1).
        logger.error("purging an import draft failed (%s)", type(error.__cause__).__name__)

        return _not_done(
            request, user, t("imp.web.101"),
            t("imp.web.91"),
            back, status_code=500,
        )

    notice = "import_draft_purged" if ended == Ended.CLEARED else "import_draft_purge_pending"

    return see_other(f"{TRASH}&notice={notice}")
