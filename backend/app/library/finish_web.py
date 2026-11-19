"""Finishing a resume import (U2-B3b): keeping its original as an attachment, then ending it.

Two separate, explicit steps. Keeping the original (/imports/{id}/keep-file)
copies the whole file into an attachment of the experience the user picks
(originals.py); nothing is picked for them, and the import is left as it
was, whatever happens. Ending it (/imports/{id}/finish) first shows what
stays (what was adopted and where, where the original is kept) and what
goes; a temporary import is then ended at once (imports.discard), a saved
draft is moved to the recycle bin (imports.delete_draft) and says so on its
button. Leaving either page ends nothing: a saved draft is kept until the
user ends or deletes it.

Keeping the file and ending the import never depend on each other's
success: an original kept before an ending that failed is still kept (and
the page says so), and ending again copies nothing.

What the pages call kept is checked on the files themselves each time
(originals.copies_of): a copy that is missing, changed or unreadable, in
the recycle bin or still being kept is said to be so and is no kept
original. The finish form carries the copies it called kept; an ending
whose copies are no longer those (another tab deleted one, a file went
bad) ends nothing and shows the page again, to be confirmed anew, also
when the answer now is that no copy is kept (Codex U2B3B-F1/F2).
"""
import logging
import secrets

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response

from app.library.attachments import TYPE_LABELS, UPLOAD_KEY
from app.library.errors import NotFound, StorageError
from app.library.identity import CurrentUser
from app.library.imports import Ended, ImportChanged, SavedDraft, delete_draft, discard, draft_of, open_import
from app.library.imports_web import BASE, NOTICES, _labels, gone_page
from app.library.models import CandidateState
from app.library.originals import (
    Health,
    KeepingChanged,
    KeptFile,
    NoOriginal,
    Place,
    adopted_into,
    copies_of,
    full_targets,
    keep_original,
    keep_targets,
    keeping_token,
    original_of,
    unchanged_since,
)
from app.library.review import ACTION_LABELS, adoption_views, candidates_of, structuring_of
from app.library.review_web import candidate_view
from app.library.web import _expected_revision, checked_form, current_user, message_page, render, runtime, see_other
from app.i18n import MessageMap, lazy, t

logger = logging.getLogger("roleradar.library")
router = APIRouter()

NO_ORIGINAL = lazy("imp.finish.1")
NOT_KEPT = {
    KeptFile.ALREADY_THERE: (409, lazy("imp.finish.2")),
    KeptFile.EARLIER_UNUSABLE: (
        409,
        lazy("imp.finish.3"),
    ),
    KeptFile.FULL: (
        409,
        lazy("imp.finish.4"),
    ),
    KeptFile.TARGET_GONE: (409, lazy("imp.finish.5")),
    KeptFile.FORM_USED: (
        409,
        lazy("imp.finish.6"),
    ),
    KeptFile.IN_PROGRESS: (409, lazy("imp.finish.7")),
    KeptFile.SOURCE_CHANGED: (
        500,
        lazy("imp.finish.8"),
    ),
    KeptFile.FAILED: (
        500,
        lazy("imp.finish.9"),
    ),
}
KEEPING_CHANGED = (
    lazy("imp.finish.10")
)
HEALTH_TEXTS = MessageMap({
    Health.MISSING: "imp.finish.11",
    Health.CHANGED: "imp.finish.12",
    Health.UNREADABLE: "imp.finish.13",
})
FINISH_FAILED = (
    lazy("imp.finish.14")
)


def install_finish(app: FastAPI) -> None:
    app.include_router(router)


def _back(import_id: str, next_page: str) -> str:
    return f"{BASE}/{import_id}/finish" if next_page == "finish" else f"{BASE}/{import_id}"


# --- keeping the original ------------------------------------------------------------------


def keep_page(
    request: Request,
    user: CurrentUser,
    import_id: str,
    *,
    next_page: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """The experiences the original can be kept in; none is picked."""
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
            original = original_of(row)

            if original is not None:
                targets = keep_targets(db, user.id, row.id)
                adopted = adopted_into(db, user.id, row.id)
                full = full_targets(db, targets)
    except NotFound:
        return gone_page(request, user)

    if original is None:
        return message_page(
            request, title=t("imp.finish.15"), message=NO_ORIGINAL, status_code=409, user=user,
            links=[(f"{BASE}/{import_id}", t("imp.finish.16"))],
        )

    copies = copies_of(rt.database, rt.files, user.id, original, rt.now)
    # Per experience: what stands in the way of another copy, and where only an unusable one is.
    blocked = {}

    for copy in copies:
        if copy.blocks:
            blocked.setdefault(copy.item.id, copy.place)

    return render(
        request,
        "import_keep_file.html",
        {
            "section": "imports",
            "row": row,
            "name": row.filename,
            "type_label": TYPE_LABELS.get(row.media_type, row.media_type),
            "targets": targets,
            "adopted": adopted,
            "full": full,
            "blocked": blocked,
            "broken": {copy.item.id for copy in copies if copy.health not in (None, Health.USABLE)},
            "kept": [copy for copy in copies if copy.kept],
            "unusable": [copy for copy in copies if copy.health not in (None, Health.USABLE)],
            "HEALTH_TEXTS": HEALTH_TEXTS,
            "Place": Place,
            "upload_key": secrets.token_urlsafe(24),
            "next_page": next_page if next_page == "finish" else "",
            "back": _back(row.id, next_page),
            "error": error,
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


@router.get("/imports/{import_id}/keep-file")
def keep_file_page(import_id: str, request: Request, next: str = "", user: CurrentUser = Depends(current_user)):
    return keep_page(request, user, import_id, next_page=next)


@router.post("/imports/{import_id}/keep-file")
def keep_file(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    target = form.get("target", "")
    upload_key = form.get("upload_key", "")
    next_page = form.get("next", "")

    if not target or not UPLOAD_KEY.fullmatch(upload_key):
        return message_page(request, title=t("imp.finish.17"), message=t("imp.finish.18"), status_code=400, user=user)

    try:
        kept = keep_original(rt.database, rt.files, user.id, import_id, target, upload_key, rt.now)
    except NotFound:
        return gone_page(request, user)
    except NoOriginal:
        return message_page(
            request, title=t("imp.finish.15"), message=NO_ORIGINAL, status_code=409, user=user,
            links=[(f"{BASE}/{import_id}", t("imp.finish.16"))],
        )

    if kept in (KeptFile.ADDED, KeptFile.ALREADY_ADDED):
        notice = "original_kept" if kept == KeptFile.ADDED else "original_kept_already"

        return see_other(f"{_back(import_id, next_page)}?notice={notice}")

    if kept == KeptFile.SOURCE_GONE:
        return message_page(
            request,
            title=t("imp.finish.19"),
            message=t("imp.finish.20"),
            status_code=409,
            user=user,
            links=[(BASE, t("imp.finish.21"))],
        )

    status_code, message = NOT_KEPT[kept]

    return keep_page(request, user, import_id, next_page=next_page, error=message, status_code=status_code)


# --- ending ---------------------------------------------------------------------------------


def finish_page(
    request: Request,
    user: CurrentUser,
    import_id: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """What stays and what goes, then the one button that ends it."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, rt.now())
            draft = draft_of(db, row.id)
            structured = structuring_of(db, row.id) is not None
            candidates = candidates_of(db, user.id, row.id)
            adoptions = adoption_views(db, user.id, candidates)
    except NotFound:
        return gone_page(request, user)

    original = original_of(row)
    copies = copies_of(rt.database, rt.files, user.id, original, rt.now)
    segments = row.segments or []
    adopted = [
        candidate_view(candidate, segments, None, adoptions.get(candidate.id))
        for candidate in candidates
        if candidate.state == CandidateState.ADOPTED
    ]

    return render(
        request,
        "import_finish.html",
        {
            "section": "imports",
            "row": row,
            "draft": draft,
            "name": row.filename or t("imp.finish.22"),
            "original": original,
            # Only copies in use whose files were just read as kept count as kept.
            "kept": [copy for copy in copies if copy.kept],
            "not_kept": [copy for copy in copies if not copy.kept],
            "keeping": keeping_token(copies),
            "HEALTH_TEXTS": HEALTH_TEXTS,
            "Place": Place,
            "adopted": adopted,
            "counts": {
                "total": len(candidates),
                "pending": sum(1 for candidate in candidates if candidate.state == CandidateState.PENDING),
                "excluded": sum(1 for candidate in candidates if candidate.state == CandidateState.EXCLUDED),
            },
            "back": f"{BASE}/{row.id}/review" if structured else f"{BASE}/{row.id}",
            "ADOPTION_LABELS": ACTION_LABELS,
            "notice": notice,
            "error": error,
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


@router.get("/imports/{import_id}/finish")
def finish_import_page(import_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return finish_page(request, user, import_id, notice=NOTICES.get(notice))


@router.post("/imports/{import_id}/finish")
def finish_import(
    import_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    ending = form.get("ending", "")
    keeping = form.get("keeping", "")

    if ending not in ("discard", "bin") or not keeping:
        return message_page(request, title=t("imp.finish.17"), message=t("imp.finish.23"), status_code=400, user=user)

    try:
        with rt.database.transaction() as db:
            original = original_of(open_import(db, user.id, import_id, rt.now()))
    except NotFound:
        return gone_page(request, user)

    # What the page called kept must still be so, checked on the files now; else it is said anew.
    copies = copies_of(rt.database, rt.files, user.id, original, rt.now)

    if keeping != keeping_token(copies):
        return finish_page(request, user, import_id, status_code=409, error=KEEPING_CHANGED)

    check = unchanged_since(user.id, original, copies, rt.now) if original is not None else None

    try:
        if ending == "discard":
            ended = discard(rt.database, rt.files, rt.readings, user.id, import_id, rt.now(), check=check, runs=rt.runs)
        else:
            delete_draft(rt.database, user.id, import_id, _expected_revision(form), rt.now, check=check, runs=rt.runs)
    except KeepingChanged:
        return finish_page(request, user, import_id, status_code=409, error=KEEPING_CHANGED)
    except NotFound:
        return gone_page(request, user)
    except SavedDraft:
        # Saved as a draft (in another tab) after this page was shown: ending it is another decision now.
        return finish_page(
            request, user, import_id, status_code=409,
            error=t("imp.finish.24"),
        )
    except ImportChanged:
        return finish_page(
            request, user, import_id, status_code=409,
            error=t("imp.finish.25"),
        )
    except StorageError as error:
        # The ending's own transaction was rolled back (a clean-up failing after it comes back as PENDING).
        logger.error("ending an import failed (%s)", type(error.__cause__).__name__)

        return finish_page(request, user, import_id, status_code=500, error=FINISH_FAILED)

    if ending == "bin":
        # A reading still running stops now; its result would be dropped anyway.
        rt.readings.stop(import_id)

        return see_other(f"{BASE}?notice=draft_finished")

    return see_other(f"{BASE}?notice={'import_finished' if ended == Ended.CLEARED else 'import_finish_pending'}")
