"""Pages of the resume import (U2-B1): 我的资料 → 导入简历.

The import page takes one file or pasted text after the user agreed that it
is kept here for at most seven days. An import's page shows what was read,
where it was found and when it expires; it offers the original file and the
text for download, reading again when a failure was not the file's, and
discarding. Nothing here writes to the experiences, the profile or the
preferences, and nothing is sent to any outside service.

The upload is read as it streams in (upload.receive_form with
imports.IMPORT_FORM), like an attachment's; the other posts are ordinary
small forms. Every route finds the import through the session's owner, so
another user's import, or one that ended or expired, is simply not found.
"""
import secrets

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.library.attachments import UploadRejected, UploadTokenRejected
from app.library.errors import FileStoreError, NotFound
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
    Stored,
    clearing_count,
    discard,
    import_text,
    open_import,
    open_imports,
    pasted_text,
    read_import,
    retryable,
    start_again,
    store_file,
    store_text,
    taken_places,
)
from app.library.models import ImportSource, ImportState
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

BASE = "/imports"

SOURCE_LABELS = {ImportSource.PDF: "PDF", ImportSource.DOCX: "Word（.docx）", ImportSource.TEXT: "粘贴的文字"}
STATE_LABELS = {
    ImportState.READING: "正在读取",
    ImportState.READ: "文字已读取",
    ImportState.PARTIAL: "部分读取",
    ImportState.FAILED: "读取失败",
}
FAILURES = {
    ENCRYPTED: "这个文件设置了密码或已加密，读不到文字。",
    NO_TEXT: "这个文件里没有读到文字，它可能是扫描件或图片。",
    DAMAGED: "这个文件无法读取，可能已经损坏。",
    TOO_MANY_PAGES: f"这个 PDF 超过 {MAX_PDF_PAGES} 页的上限，没有读取。",
    NOT_WORD: "这个文件无法作为 Word（.docx）文档读取：可能已经损坏，或者不是 .docx。",
    MACROS: "这个 Word 文件带有宏。为了安全，没有读取。",
    TOO_BIG_INSIDE: "这个文件解压后的内容超过了读取上限，没有读取。",
    TIMEOUT: f"读取超过了 {READ_SECONDS:g} 秒的时限，已经停止。",
    CRASHED: "读取时出了错，没有读到内容。",
    BUSY: "当时正有其他文件在读取，这次没有开始。",
    MISSING_FILE: "暂存的原文件找不到了，或者和上传时不一致，无法读取。",
    INTERRUPTED: "读取被中断了（可能是应用重启了）。",
}
RETRY_HINT = "可以重新读取一次。"
FALLBACK_HINT = "可以改为粘贴简历文字，或者放弃这次导入。"
NOTICES = {
    "import_added": "已暂存，没有加入经历库。",
    "import_already_added": "这份内容刚才已经提交过，这次没有重复暂存。",
    "import_read_again": "已重新读取。",
    "import_discarded": "已放弃这次导入：原文件和读到的文字已经从本机删除。",
    "import_discard_pending": (
        "已放弃这次导入：它已经不能查看或下载。文件清理暂未完成，系统会自动重试。"
    ),
    "import_ended": "这次导入在读取期间已被放弃或已到期，读取结果没有保存。",
}
STORE_PROBLEMS = {
    Stored.FORM_USED: (
        409,
        "这个导入表单已经用过了（可能是返回了旧页面），为了不重复暂存，这次没有读取。请重新提交。",
    ),
    Stored.FULL: (
        409,
        f"已经有 {MAX_OPEN_IMPORTS} 份导入在暂存，达到上限，这次没有读取。请先放弃不需要的导入，再重新选择文件。",
    ),
    Stored.FAILED: (
        500,
        "暂存没有完成：保存文件或记录时出错，这次的内容没有暂存，也不能查看。经历库和其他导入不受影响，请稍后再试。",
    ),
    Stored.FAILED_LEFTOVER: (
        500,
        "暂存没有完成：保存时出错。这次上传的文件还有一份留在本机的私有目录里，暂时没能删除；"
        "它不能查看或下载，也不会加入经历库，系统会自动重试清理。经历库和其他导入不受影响。",
    ),
}
# A refusal at the limit gives the typed text back with the form, so it is not lost.
FULL_WITH_TEXT = (
    f"已经有 {MAX_OPEN_IMPORTS} 份导入在暂存，达到上限，这次没有暂存。你输入的文字还在下面，可以先复制；"
    "也可以在新标签页打开下面不需要的导入并放弃，再回到这里提交。"
)

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
) -> Response:
    """The import form (with a new one-time key) and the owner's open imports."""
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        entries = open_imports(db, user.id, now)
        clearing = clearing_count(db, user.id, now)
        full = taken_places(db, user.id, now) >= MAX_OPEN_IMPORTS

    return render(
        request,
        "imports.html",
        {
            "section": "imports",
            "entries": entries,
            "clearing": clearing,
            "full": full,
            "upload_key": secrets.token_urlsafe(24),
            "text": text,
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
    }


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这次导入",
        message=(
            f"它可能已经到期清理（导入只暂存 {IMPORT_WINDOW.days} 天）、已经放弃，或者属于其他用户。"
            "需要的话，可以重新导入。"
        ),
        status_code=404,
        user=user,
        links=[(BASE, "回到导入简历")],
    )


def _problem_lines(row) -> list[str]:
    """What the page says about a failed or partial reading."""
    if row.state == ImportState.FAILED:
        reason = FAILURES.get(row.outcome, FAILURES[CRASHED])

        return [reason, RETRY_HINT if retryable(row) else FALLBACK_HINT]

    lines = []
    details = row.details or {}

    if details.get(UNREADABLE_PAGES):
        pages = "、".join(str(number) for number in details[UNREADABLE_PAGES])
        lines.append(f"第 {pages} 页无法读取，可能已经损坏；下面只有能读出的页。")

    if details.get(EMPTY_PAGES):
        pages = "、".join(str(number) for number in details[EMPTY_PAGES])
        lines.append(f"第 {pages} 页没有读到文字，可能是图片或扫描内容。")

    if details.get(TRUNCATED):
        lines.append(f"文字超过 {MAX_TEXT_CHARS // 10000} 万字，后面的部分没有读取。")

    return lines


@router.get("/imports")
def imports_home(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    # A save made from another page's leave dialog lands here too, and says what it saved.
    return imports_page(request, user, notice=NOTICES.get(notice) or LIBRARY_NOTICES.get(notice))


@router.get("/imports/{import_id}")
def import_detail(import_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
    except NotFound:
        return gone_page(request, user)

    return render(
        request,
        "import.html",
        {
            "section": "imports",
            "row": row,
            "name": row.filename or "粘贴的文字",
            "problems": _problem_lines(row),
            "can_retry": retryable(row),
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
            title="原文件没有下载",
            message="暂存的原文件现在找不到或无法读取，或者和上传时不一致，所以没有下载。可以放弃这次导入后重新导入。",
            status_code=500,
            user=user,
            links=[(f"{BASE}/{row.id}", "回到这次导入")],
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
            title="还没有可下载的文字",
            message="这次导入还没有读到文字，所以没有可下载的内容。",
            status_code=409,
            user=user,
            links=[(f"{BASE}/{row.id}", "回到这次导入")],
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
        stem = row.filename.rsplit(".", 1)[0] or "导入"

        return f"{stem}（读到的文字）.txt"

    return "粘贴的文字.txt"


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
            title="没有重新读取",
            message="这次导入现在不能重新读取：它可能已经读完、正在读取，或者页面不是最新的。请回到这次导入查看。",
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", "回到这次导入")],
        )

    state = read_import(rt.database, rt.files, rt.readings, user.id, import_id, rt.now)

    if state is None:
        return see_other(f"{BASE}?notice=import_ended")

    return see_other(f"{BASE}/{import_id}?notice=import_read_again")


def _discard_points(row) -> list[str]:
    if row.filename:
        # A file that could not be read has nothing read to clear.
        what = f"原文件“{row.filename}”" + ("和从中读到的文字" if row.segments else "")
    else:
        what = "你粘贴的文字"

    return [
        f"会清理这次导入在本机暂存的{what}。",
        "经历库、基本资料、求职偏好和其他导入都不受影响。",
        "放弃后不能再查看、下载或恢复。这不是回收站：没有 7 天恢复期。",
    ]


@router.get("/imports/{import_id}/discard")
def discard_page(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
    except NotFound:
        return gone_page(request, user)

    return confirm_page(
        request,
        user,
        title="放弃这次导入？",
        points=_discard_points(row),
        action=f"{BASE}/{row.id}/discard",
        button="放弃本次导入",
        cancel=f"{BASE}/{row.id}",
        section="imports",
    )


@router.post("/imports/{import_id}/discard", dependencies=[Depends(checked_form)])
def discard_import(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        ended = discard(rt.database, rt.files, rt.readings, user.id, import_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    notice = "import_discarded" if ended == Ended.CLEARED else "import_discard_pending"

    return see_other(f"{BASE}?notice={notice}")
