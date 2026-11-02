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

logger = logging.getLogger("roleradar.library")
router = APIRouter()

NO_ORIGINAL = "这次导入是粘贴的文字，没有原文件可以另存；需要的话，可以在导入页下载读到的文字。"
NOT_KEPT = {
    KeptFile.ALREADY_THERE: (409, "这条经历已经有这份原文件（也可能在它的回收站里，或正在保存），没有再保存一份。"),
    KeptFile.EARLIER_UNUSABLE: (
        409,
        "这个页面之前另存的附件现在不可用（文件缺失、内容和保存时不一致，或无法读取），这次没有再保存。"
        "下面已按现在的情况列出；可以选一条经历再存一份可用的。不可用的那份不会被自动删除或覆盖，需要时请你自己在经历里处理。",
    ),
    KeptFile.FULL: (
        409,
        "这条经历的附件已满（回收站里还能恢复的附件也算在内），没有保存。可以选择其他经历；"
        "或者先到这条经历里永久删除不需要的附件，或等它们过了恢复期限，再来另存。没有替你删除任何附件。",
    ),
    KeptFile.TARGET_GONE: (409, "选择的经历已经不在了（删除了，或不是你的经历），没有保存。请重新选择。"),
    KeptFile.FORM_USED: (
        409,
        "这个页面已经提交过另一次保存（可能是返回了旧页面），为了不重复保存，这次没有另存。请看下面的情况后重新选择。",
    ),
    KeptFile.IN_PROGRESS: (409, "刚才的保存还在进行中，这次没有重复保存。稍后刷新页面查看结果。"),
    KeptFile.SOURCE_CHANGED: (
        500,
        "暂存的原文件现在找不到或无法读取，或者和读取时不一致，所以没有另存。导入保持原样。",
    ),
    KeptFile.FAILED: (
        500,
        "另存没有完成：写入时出错，没有生成附件。导入和它的原文件保持原样，可以再试一次。",
    ),
}
KEEPING_CHANGED = (
    "没有结束：原文件的另存情况在你打开这个页面之后变了（例如附件被删除、移到回收站，或文件现在不可用），"
    "什么都没有清理。下面已按现在的情况重新说明，请再确认一次。"
)
HEALTH_TEXTS = {
    Health.MISSING: "文件找不到了",
    Health.CHANGED: "文件内容和保存时不一致",
    Health.UNREADABLE: "文件无法读取",
}
FINISH_FAILED = (
    "结束没有完成：写入数据库时出错，导入保持原样，什么都没有清理。已经另存的原文件附件（见下面“会留下”）"
    "不受影响；可以再试一次，再试不会重复另存原文件。"
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
            request, title="没有原文件", message=NO_ORIGINAL, status_code=409, user=user,
            links=[(f"{BASE}/{import_id}", "回到这次导入")],
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
        return message_page(request, title="请求无效", message="这个表单缺少必要的信息，没有另存。", status_code=400, user=user)

    try:
        kept = keep_original(rt.database, rt.files, user.id, import_id, target, upload_key, rt.now)
    except NotFound:
        return gone_page(request, user)
    except NoOriginal:
        return message_page(
            request, title="没有原文件", message=NO_ORIGINAL, status_code=409, user=user,
            links=[(f"{BASE}/{import_id}", "回到这次导入")],
        )

    if kept in (KeptFile.ADDED, KeptFile.ALREADY_ADDED):
        notice = "original_kept" if kept == KeptFile.ADDED else "original_kept_already"

        return see_other(f"{_back(import_id, next_page)}?notice={notice}")

    if kept == KeptFile.SOURCE_GONE:
        return message_page(
            request,
            title="没有另存",
            message="这次导入在另存期间已经结束、到期或被删除，原文件没有另存，也没有生成附件。",
            status_code=409,
            user=user,
            links=[(BASE, "回到导入简历")],
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
            "name": row.filename or "粘贴的文字",
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
        return message_page(request, title="请求无效", message="这个表单缺少必要的信息，没有结束。", status_code=400, user=user)

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
            ended = discard(rt.database, rt.files, rt.readings, user.id, import_id, rt.now(), check=check)
        else:
            delete_draft(rt.database, user.id, import_id, _expected_revision(form), rt.now, check=check)
    except KeepingChanged:
        return finish_page(request, user, import_id, status_code=409, error=KEEPING_CHANGED)
    except NotFound:
        return gone_page(request, user)
    except SavedDraft:
        # Saved as a draft (in another tab) after this page was shown: ending it is another decision now.
        return finish_page(
            request, user, import_id, status_code=409,
            error="这次导入在你打开这个页面之后被保存为草稿了（可能是在另一个标签页），这次没有结束。下面已按草稿重新说明。",
        )
    except ImportChanged:
        return finish_page(
            request, user, import_id, status_code=409,
            error="这份草稿在你打开这个页面之后有了变化（可能是在另一个标签页），这次没有结束。请看下面的说明后再决定。",
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
