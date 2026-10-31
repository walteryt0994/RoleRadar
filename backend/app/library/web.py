"""Chinese web pages of the experience library (U1-A, U1-B).

Pages are rendered on the server with Jinja2 autoescaping. The owner of every
operation is the user of the server-side session (current_user), never a
value sent by the browser, and every POST must carry that session's CSRF
token. Forms that change an existing experience also carry the revision the
page was showing, so an older page cannot overwrite or confirm newer content.

U1-B adds five more categories of experience, filtering and search, and two
records a user keeps one of each: the basic profile and the job
preferences. Those two share the experiences' storage, history, deletion
and export, but are never confirmed and never listed as experiences.

U1-C adds private attachments to saved experiences. They are managed on the
experience's reading page; the upload has a route of its own that reads its
body as it streams in (upload.py), while every other post keeps the small
form limit. A file is only ever sent back through the owner's download
route, never as a static or public address.
"""
import ipaddress
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import jinja2
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect

from app.library.attachments import (
    MAX_ATTACHMENTS,
    TYPE_LABELS,
    UploadOutcome,
    UploadRejected,
    UploadTokenRejected,
    binned_attachments,
    has_free_place,
    kept_attachment,
    kept_attachments,
    places_taken,
    recover_uploads,
    referenced_files,
    store_upload,
)
from app.library.clock import Clock
from app.library.config import LibraryConfig
from app.library.content import (
    ALL_KINDS,
    CATEGORY_FIELDS,
    CATEGORY_LABELS,
    DOCUMENT_KINDS,
    EDUCATION,
    EXPERIENCE_CATEGORIES,
    NOT_DECIDED,
    NOT_FILLED,
    NOTE_FIELD,
    PREFERENCE_GROUPS,
    PREFERENCE_SINGLES,
    PREFERENCES,
    PROFILE,
    PROJECT,
    display_title,
    link_lines,
    parse_experience_form,
)
from app.library.db import Database
from app.library.errors import (
    AttachmentConflict,
    ChangedFile,
    Conflict,
    ContentError,
    FileStoreError,
    LibraryError,
    MissingFile,
    NotAllowed,
    NotFound,
    StorageError,
    VersionConflict,
)
from app.library.export import collect_export, single_export, write_export
from app.library.files import FileStore, read_chunks
from app.library.identity import (
    DEV_PROVIDER,
    CurrentUser,
    dev_users,
    end_session,
    remove_expired_sessions,
    resolve_session,
    start_session,
    token_hash,
)
from app.library.lifecycle import (
    RESTORE_WINDOW,
    PurgeReport,
    attachment_count,
    attachment_of,
    binned_documents,
    check_attachment_revision,
    check_version_state,
    deletable_version,
    delete_attachment,
    delete_experience,
    delete_version,
    deletion_scope,
    purge_due,
    request_attachment_purge,
    request_experience_purge,
    request_version_purge,
    restore_attachment,
    restore_experience,
    restore_version,
    run_purge_job,
    trash_view,
    trashed_item,
    trashed_version,
    version_cleanups,
    version_state,
)
from app.library.models import User
from app.library.presentation import (
    CATEGORY_GROUPS,
    CLEANUP_NOUNS,
    DOCUMENT_PAGES,
    EDIT_HEADINGS,
    FIELD_GROUPS,
    FOLDED_GROUPS,
    KIND_NOUNS,
    NEW_HEADINGS,
    READING_LABELS,
    READINGS,
    event_label,
    field_label,
    format_local,
    format_remaining,
    format_short,
    format_size,
    record_page,
    reference_note,
    section_of,
    state_label,
    summary_line,
    unfilled_labels,
    version_label,
)
from app.library.projection import list_references
from app.library.service import (
    active_item,
    check_revision,
    confirm_experience,
    create_document,
    create_experience,
    current_document,
    experience_history,
    get_version,
    is_current_confirmed,
    list_experiences,
    save_draft,
)
from app.library.upload import MAX_REQUEST_BYTES, receive_upload

PACKAGE_DIR = Path(__file__).parent
SESSION_COOKIE = "rr_session"
LOGIN_COOKIE = "rr_login"
# Sent with a save from the leave dialog when the user was logging out.
SAVE_THEN_LOGOUT = "logout"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
# The only path whose posts may be bigger than an ordinary form: an attachment upload.
UPLOAD_PATH = re.compile(r"/experiences/[0-9a-f-]{36}/attachments")
# A download is a file, never a page: if a browser shows it anyway, nothing in it may run.
DOWNLOAD_POLICY = "default-src 'none'; sandbox"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
SAVE_FAILED = (
    "保存失败：这次填写的内容还没有保存，最后一次成功保存的内容没有变化。"
    "你填写的内容还在下面，可以稍后再试。"
)
GONE_WHILE_EDITING = (
    "这条经历已经不存在，或已被删除，所以这次修改没有保存。你填写的内容还在下面，"
    "可以另存为一条新的经历，或者复制到别处。"
)
DOCUMENT_GONE = {
    PROFILE: (
        "基本资料在你编辑期间已被删除，所以这次修改没有保存。你填写的内容还在下面，"
        "可以保存为一份新的基本资料。"
    ),
    PREFERENCES: (
        "求职偏好在你编辑期间已被删除，所以这次修改没有保存。你填写的内容还在下面，"
        "可以保存为一份新的求职偏好。"
    ),
}
# The longest search the record list accepts; anything longer is cut there.
MAX_QUERY_LENGTH = 100
NOTICES = {
    # The record page right below says that saving is not confirming.
    "created": "已保存（待核对）。",
    "saved": "已保存修改（待核对）。",
    "unchanged": "内容没有变化，这次没有产生新的保存。",
    # After saving from the leave dialog the user lands on the page they
    # chose, which may show another record: say what was saved.
    "created_before_leaving": "已保存你刚才新建的记录（待核对）。",
    "saved_before_leaving": "已保存你刚才编辑的内容（待核对）。",
    "unchanged_before_leaving": "你刚才编辑的内容没有变化，这次没有产生新的保存。",
    "confirmed": "已确认属实：这一版已记为新的确认版本，可以在历史里查看。",
    "deleted": "已移到回收站：它已停止在经历列表和导出的当前部分中使用，7 天内可以恢复。",
    "restored": "已从回收站恢复。",
    "purged": "已永久删除，清理已完成。",
    "purge_pending": (
        "已永久删除：内容已经不能访问或恢复。数据清理暂未完成，系统会自动重试，进度见回收站。"
    ),
    "version_deleted": "已删除这个历史版本，7 天内可以在本页恢复。",
    "version_restored": "已恢复这个历史版本。",
    "version_purged": "已永久删除这个历史版本，清理已完成。",
    "version_purge_pending": (
        "已永久删除这个历史版本：内容已经不能访问或恢复。清理暂未完成，系统会自动重试。"
    ),
    "saved_logged_out": "已保存草稿（待核对），并已退出。",
    "logged_out": "已退出。",
    # The profile and the preferences are saved as they are; nothing waits for confirmation.
    "profile_created": "已保存基本资料。",
    "profile_saved": "已保存基本资料的修改。",
    "profile_unchanged": "基本资料没有变化，这次没有产生新的保存。",
    "profile_created_before_leaving": "已保存你刚才填写的基本资料。",
    "profile_saved_before_leaving": "已保存你刚才编辑的基本资料。",
    "profile_unchanged_before_leaving": "你刚才编辑的基本资料没有变化，这次没有产生新的保存。",
    "profile_deleted": "基本资料已移到回收站：它已停止使用，7 天内可以恢复。",
    "preferences_created": "已保存求职偏好。",
    "preferences_saved": "已保存求职偏好的修改。",
    "preferences_unchanged": "求职偏好没有变化，这次没有产生新的保存。",
    "preferences_created_before_leaving": "已保存你刚才填写的求职偏好。",
    "preferences_saved_before_leaving": "已保存你刚才编辑的求职偏好。",
    "preferences_unchanged_before_leaving": "你刚才编辑的求职偏好没有变化，这次没有产生新的保存。",
    "preferences_deleted": "求职偏好已移到回收站：它已停止使用，7 天内可以恢复。",
    "document_saved_logged_out": "已保存，并已退出。",
    "attachment_added": "已上传附件。",
    "attachment_already_added": "这个文件刚才已经上传过了，这次没有重复保存。",
    "attachment_deleted": "已删除这个附件：它已移到回收站，不能再下载；7 天内可以在本页下方恢复。",
    "attachment_restored": "已恢复这个附件。",
    "attachment_purged": "已永久删除这个附件，它的文件已从附件目录中删除。",
    "attachment_purge_pending": (
        "已永久删除这个附件：它已经不能下载或恢复。文件清理暂未完成，系统会自动重试。"
    ),
}
CHOOSE_AGAIN = "请重新选择文件后再上传（浏览器不会自动保留刚才选择的文件）。"
UPLOAD_FULL = (
    f"这条经历已经有 {MAX_ATTACHMENTS} 个附件（包括回收站里还能恢复的），达到上限，这个文件没有上传。"
)
UPLOAD_PROBLEMS = {
    UploadOutcome.IN_PROGRESS: (409, "同一次上传还在处理中，这次没有重复保存。请稍等一下，再刷新页面查看。"),
    UploadOutcome.FORM_USED: (
        409,
        "这个上传表单已经用过了（可能是返回了旧页面），为了不重复保存，这次没有上传。" + CHOOSE_AGAIN,
    ),
    UploadOutcome.FULL: (409, UPLOAD_FULL),
    UploadOutcome.FAILED: (
        500,
        "上传没有完成：保存文件或记录时出错，这个文件没有保存，已有的附件和经历内容不受影响。"
        "请稍后" + CHOOSE_AGAIN,
    ),
}
FILE_PROBLEMS = {
    MissingFile: "这个附件的文件在附件目录里找不到了，所以没有下载。附件记录还在：可以删除它，再重新上传。",
    ChangedFile: "这个附件的文件和上传时记录的大小或校验值不一致（可能已损坏），所以没有下载。",
}
UNREADABLE_FILE = "这个附件的文件现在无法读取（可能是权限问题，或文件被替换了），所以没有下载。"
HTTP_MESSAGES = {
    400: "请求的格式不对，没有执行任何操作。",
    404: "找不到这个页面或内容。它可能不存在、属于其他用户，或者已经超过恢复期限。",
    405: "这个地址不支持这种操作。",
}

logger = logging.getLogger("roleradar.library")
router = APIRouter()


@dataclass
class LibraryRuntime:
    config: LibraryConfig
    database: Database
    clock: Clock
    templates: Jinja2Templates
    files: FileStore

    def now(self):
        return self.clock.now()

    def run_cleanup(self) -> PurgeReport | None:
        """One clean-up pass: due purge jobs, what unfinished uploads left
        behind, leftover staging and export files, then expired sessions.

        A step that fails is logged and retried on the next pass; the steps
        after it still run. Returns the purge report, or None if a step failed.
        """
        now = self.now()
        report = None
        failed = False

        try:
            report = purge_due(self.database, now, self.files)
        except LibraryError:
            logger.exception("purging failed; it will be retried on the next run")
            failed = True

        try:
            recover_uploads(self.database, self.files, now)
            self.files.sweep(referenced_files(self.database))
        except LibraryError:
            logger.exception("cleaning up unfinished uploads failed; it will be retried on the next run")
            failed = True

        try:
            with self.database.transaction() as db:
                remove_expired_sessions(db, now)
        except LibraryError:
            logger.exception("removing expired sessions failed; it will be retried on the next run")
            failed = True

        return None if failed else report


class NotAuthenticated(Exception):
    pass


class CsrfRejected(Exception):
    pass


def build_templates(config: LibraryConfig) -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(PACKAGE_DIR / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["localtime"] = format_local
    env.filters["shorttime"] = format_short
    env.globals.update(
        FIELDS=CATEGORY_FIELDS[PROJECT],
        FIELD_GROUPS=FIELD_GROUPS,
        FOLDED_GROUPS=FOLDED_GROUPS,
        READING_LABELS=READING_LABELS,
        NOTE_FIELD=NOTE_FIELD,
        NOT_FILLED=NOT_FILLED,
        NOT_DECIDED=NOT_DECIDED,
        CATEGORY_FIELDS=CATEGORY_FIELDS,
        CATEGORY_GROUPS=CATEGORY_GROUPS,
        CATEGORY_LABELS=CATEGORY_LABELS,
        EXPERIENCE_CATEGORIES=EXPERIENCE_CATEGORIES,
        DOCUMENT_KINDS=DOCUMENT_KINDS,
        PROFILE=PROFILE,
        PREFERENCES=PREFERENCES,
        EDUCATION=EDUCATION,
        PREFERENCE_GROUPS=PREFERENCE_GROUPS,
        PREFERENCE_SINGLES=PREFERENCE_SINGLES,
        READINGS=READINGS,
        NEW_HEADINGS=NEW_HEADINGS,
        EDIT_HEADINGS=EDIT_HEADINGS,
        KIND_NOUNS=KIND_NOUNS,
        CLEANUP_NOUNS=CLEANUP_NOUNS,
        DOCUMENT_PAGES=DOCUMENT_PAGES,
        record_page=record_page,
        field_label=field_label,
        RESTORE_DAYS=RESTORE_WINDOW.days,
        clock_note=_clock_note(config),
        display_title=display_title,
        link_lines=link_lines,
        state_label=state_label,
        version_label=version_label,
        event_label=event_label,
        format_remaining=format_remaining,
        summary_line=summary_line,
        unfilled_labels=unfilled_labels,
        format_size=format_size,
        TYPE_LABELS=TYPE_LABELS,
        MAX_ATTACHMENTS=MAX_ATTACHMENTS,
    )

    return Jinja2Templates(env=env)


def _clock_note(config: LibraryConfig) -> str | None:
    if not config.dev_clock_offset:
        return None

    days = config.dev_clock_offset.total_seconds() / 86400

    return (
        f"开发时钟已往后拨 {days:g} 天：页面上的时间、恢复期限和清理都按偏移后的时间计算。"
        "只用于本地验证。"
    )


def install_web(app: FastAPI, config: LibraryConfig) -> None:
    """Attach the pages, error pages and request guards to the app."""
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.add_exception_handler(NotAuthenticated, _not_authenticated)
    app.add_exception_handler(CsrfRejected, _csrf_rejected)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(NotAllowed, _not_allowed)
    app.add_exception_handler(Conflict, _conflict)
    app.add_exception_handler(VersionConflict, _version_conflict)
    app.add_exception_handler(StorageError, _storage_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    # Starlette runs the middleware added last first: LocalOnly is outermost.
    app.add_exception_handler(AttachmentConflict, _attachment_conflict)
    app.middleware("http")(_security_headers)
    app.add_middleware(FormSizeLimit, max_bytes=config.max_form_bytes, upload_bytes=MAX_REQUEST_BYTES)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(LocalOnly)


# --- request guards -------------------------------------------------------


class LocalOnly:
    """Refuse any connection that does not come from this machine.

    The client address comes from the accepted socket (the controlled
    launcher turns proxy headers off), not from anything the request says
    about itself. This is a safety net for a local tool, not authentication.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _is_loopback(scope.get("client")):
            response = PlainTextResponse("本地开发版只接受本机访问。", status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _is_loopback(client) -> bool:
    if not client:
        return False

    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False

    mapped = getattr(address, "ipv4_mapped", None)

    return address.is_loopback or bool(mapped and mapped.is_loopback)


class FormSizeLimit:
    """Refuse oversized posts before the form is read.

    Every post must say how long it is. Only an attachment upload, on its
    own path, may be longer than an ordinary form (up to upload_bytes); its
    route also counts the bytes that really arrive.
    """

    def __init__(self, app, max_bytes: int, upload_bytes: int):
        self.app = app
        self.max_bytes = max_bytes
        self.upload_bytes = upload_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            length = dict(scope["headers"]).get(b"content-length")
            limit = self.upload_bytes if UPLOAD_PATH.fullmatch(scope["path"]) else self.max_bytes

            if length is None:
                response = PlainTextResponse("提交缺少长度信息。", status_code=411)
                await response(scope, receive, send)
                return

            if not length.isdigit() or int(length) > limit:
                response = PlainTextResponse("提交的内容太大。", status_code=413)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    # A download sets its own, stricter policy.
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"

    if not request.url.path.startswith("/static/"):
        # Private pages must not be kept in the browser cache after logout.
        response.headers["Cache-Control"] = "no-store"

    return response


def runtime(request: Request) -> LibraryRuntime:
    return request.app.state.library


def current_user(request: Request) -> CurrentUser:
    """The signed-in user, looked up from the session cookie only."""
    token = request.cookies.get(SESSION_COOKIE)

    if not token:
        raise NotAuthenticated()

    rt = runtime(request)

    with rt.database.transaction() as db:
        user = resolve_session(db, token, rt.now())

    if user is None:
        raise NotAuthenticated()

    return user


async def submitted_form(request: Request) -> dict[str, str]:
    """Text fields of a form post; the first value of a repeated name wins."""
    form = await request.form(max_files=0, max_fields=100)
    values = {}

    for key, value in form.multi_items():
        if not isinstance(value, str):
            raise HTTPException(status_code=400)

        values.setdefault(key, value)

    return values


def check_same_origin(request: Request) -> None:
    """Reject posts that the browser marks as coming from another site."""
    fetch_site = request.headers.get("sec-fetch-site")

    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        raise CsrfRejected()

    origin = request.headers.get("origin")

    if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise CsrfRejected()


def checked_form(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(submitted_form),
) -> dict[str, str]:
    """The form of a signed-in post that carries this session's CSRF token."""
    check_same_origin(request)

    if not _tokens_match(user.csrf_token, form.get("csrf_token")):
        raise CsrfRejected()

    return form


def _tokens_match(expected: str | None, submitted: str | None) -> bool:
    if not expected or not submitted:
        return False

    return secrets.compare_digest(expected.encode(), submitted.encode())


def _expected_revision(form: dict[str, str]) -> int:
    value = form.get("expected_revision", "")

    if not value.isascii() or not value.isdigit():
        raise HTTPException(status_code=400)

    return int(value)


def _expected_state(form: dict[str, str]) -> str:
    value = form.get("expected_state", "")

    if not value:
        raise HTTPException(status_code=400)

    return value


# --- rendering helpers ----------------------------------------------------


def render(
    request: Request,
    name: str,
    context: dict | None = None,
    *,
    user: CurrentUser | None = None,
    status_code: int = 200,
) -> Response:
    context = {"user": user, "notice": None, "error": None, **(context or {})}

    # A page about one record shows the part of 我的资料 it belongs to.
    if "section" not in context and context.get("item") is not None:
        context["section"] = section_of(context["item"])

    return runtime(request).templates.TemplateResponse(
        request,
        name,
        context,
        status_code=status_code,
    )


def message_page(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int,
    user: CurrentUser | None = None,
    links: list[tuple[str, str]] | None = None,
) -> Response:
    return render(
        request,
        "message.html",
        {"title": title, "message": message, "links": links or []},
        user=user,
        status_code=status_code,
    )


def confirm_page(
    request: Request,
    user: CurrentUser,
    *,
    title: str,
    points: list[str],
    action: str,
    button: str,
    cancel: str,
    hidden: dict[str, str] | None = None,
    acknowledge: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    section: str = "library",
) -> Response:
    return render(
        request,
        "confirm.html",
        {
            "title": title,
            "points": points,
            "action": action,
            "button": button,
            "cancel": cancel,
            "hidden": hidden or {},
            "acknowledge": acknowledge,
            "error": error,
            "section": section,
        },
        user=user,
        status_code=status_code,
    )


def list_filter(request: Request) -> tuple[str, str]:
    """The category and search words a workspace address asks the list to show.

    An unknown category means all of them; the search is cut to
    MAX_QUERY_LENGTH characters, without control characters.
    """
    category = request.query_params.get("category", "")

    if category not in EXPERIENCE_CATEGORIES:
        category = ""

    raw = request.query_params.get("q", "")
    query = " ".join(
        "".join(ch for ch in raw if unicodedata.category(ch) != "Cc").split()
    )[:MAX_QUERY_LENGTH]

    return category, query


def library_sidebar(
    request: Request,
    user: CurrentUser,
    *,
    selected_id: str | None = None,
    focus: bool = False,
    items: list | None = None,
    overview: bool = False,
) -> dict:
    """Context for the list of the user's records on the left of the workspace.

    The list only helps finding records. If it cannot be read, sidebar_items
    is None and the page still shows its own content, its error and the
    user's unsaved input unchanged; the list shows a short note instead.

    The list follows the filter in the page's own address (?category=…&q=…).
    Its record links keep that filter, so the list stays filtered while the
    records found are opened one after another.

    overview: the page is the overview of the library, which shows this
    same filtered list in its main area instead of beside it.
    """
    category, query = list_filter(request)
    filtering = bool(category or query)

    try:
        with runtime(request).database.transaction() as db:
            if items is None:
                items = list_experiences(db, user.id)

            shown = (
                list_experiences(db, user.id, category=category, query=query)
                if filtering
                else items
            )
    except StorageError as error:
        logger.error("reading the record list failed (%s)", type(error.__cause__).__name__)
        items = shown = None

    asked = {key: value for key, value in (("category", category), ("q", query)) if value}

    return {
        "sidebar_items": shown,
        "sidebar_total": len(items) if items is not None else 0,
        "selected_id": selected_id,
        "focus": focus,
        "filter_category": category,
        "filter_query": query,
        "filter_suffix": f"?{urlencode(asked)}" if asked else "",
        "overview": overview,
    }


def form_page(
    request: Request,
    user: CurrentUser,
    *,
    values: dict[str, str],
    category: str = PROJECT,
    item_id: str | None = None,
    item=None,
    expected_revision: int | None = None,
    error: str | None = None,
    field_errors: dict[str, str] | None = None,
    next_path: str = "",
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    """The new/edit form. Without item_id the form creates a new experience."""
    return render(
        request,
        "form.html",
        {
            "category": category,
            "item_id": item_id,
            "item": item,
            "values": values,
            "expected_revision": expected_revision,
            "error": error,
            "field_errors": field_errors or {},
            "next_path": safe_next(next_path) or "",
            "notice": notice,
            # A page shown again after a failed save may hold text that exists
            # nowhere else yet; the leave guard must treat it as unsaved.
            "unsaved_input": bool(error) and any(value.strip() for value in values.values()),
            "action": f"/experiences/{item_id}/draft" if item_id else "/experiences",
            **library_sidebar(request, user, selected_id=item_id, focus=True),
        },
        user=user,
        status_code=status_code,
    )


def value_names(category: str) -> list[str]:
    names = [field.name for field in CATEGORY_FIELDS[category]]

    if category in EXPERIENCE_CATEGORIES:
        names.append(NOTE_FIELD.name)

    return names


def stored_values(item=None, category: str = PROJECT) -> dict[str, str]:
    """The saved values of a record (or of an empty new one) for its form."""
    if item is not None:
        category = item.category

    values = {name: "" for name in value_names(category)}

    if item is not None:
        values.update({field.name: item.content.get(field.name, "") for field in CATEGORY_FIELDS[category]})

        if category in EXPERIENCE_CATEGORIES:
            values[NOTE_FIELD.name] = item.private_note

    return values


def submitted_values(form: dict[str, str], category: str = PROJECT) -> dict[str, str]:
    """Exactly what the user typed, so a failed save loses nothing."""
    return {name: form.get(name, "") for name in value_names(category)}


def submitted_category(form: dict[str, str]) -> str:
    """The category a form says it is for, used to show the form again only."""
    category = form.get("category", PROJECT)

    return category if category in EXPERIENCE_CATEGORIES else PROJECT


def safe_next(value: str) -> str | None:
    """A path inside this site to continue to, or None."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None

    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        return None

    parts = urlsplit(value)

    if parts.scheme or parts.netloc:
        return None

    return value


def with_notice(path: str, notice: str) -> str:
    parts = urlsplit(path)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "notice"]
    query.append(("notice", notice))

    return urlunsplit(("", "", parts.path, urlencode(query), ""))


def redirect_after_save(form: dict[str, str], default_path: str, notice: str) -> RedirectResponse:
    target = safe_next(form.get("next", ""))

    if target is None:
        return RedirectResponse(with_notice(default_path, notice), status_code=303)

    return RedirectResponse(with_notice(target, f"{notice}_before_leaving"), status_code=303)


def logout_response(rt: LibraryRuntime, user: CurrentUser, notice: str) -> RedirectResponse:
    """End this session on the server and drop its cookie."""
    with rt.database.transaction() as db:
        end_session(db, user.token_hash)

    response = see_other(f"/login?notice={notice}")
    response.delete_cookie(SESSION_COOKIE, path="/")

    return response


def finish_save(
    request: Request,
    user: CurrentUser,
    form: dict[str, str],
    default_path: str,
    notice: str,
    *,
    document: bool = False,
) -> Response:
    """After a successful save: log out if the user chose that, else continue.

    document: the profile or preferences, which are saved as they are rather
    than as drafts waiting for confirmation.
    """
    if form.get("after_save") != SAVE_THEN_LOGOUT:
        return redirect_after_save(form, default_path, notice)

    try:
        return logout_response(
            runtime(request), user, "document_saved_logged_out" if document else "saved_logged_out"
        )
    except StorageError as error:
        logger.error("logging out after a save failed (%s)", type(error.__cause__).__name__)
        return message_page(
            request,
            title="已保存，但还没有退出" if document else "草稿已保存，但还没有退出",
            message=(
                "内容已经保存" if document else "草稿已经保存（待核对）"
            ) + "，但退出时数据库操作失败，你仍处于登录状态。请再点一次“退出”。",
            status_code=500,
            user=user,
            links=[(default_path, "查看已保存的内容")],
        )


def conflict_page(request: Request, user: CurrentUser, current, mine: dict[str, str]) -> Response:
    """Both versions side by side after a save lost a race, with mine kept for saving."""
    fields = list(CATEGORY_FIELDS[current.category])

    if current.category in EXPERIENCE_CATEGORIES:
        fields.append(NOTE_FIELD)
        action = f"/experiences/{current.id}/draft"
    else:
        action = f"{DOCUMENT_PAGES[current.category]}/{current.id}/save"

    return render(
        request,
        "conflict.html",
        {
            "item": current,
            "fields": fields,
            "action": action,
            "mine": mine,
            "theirs": stored_values(current),
        },
        user=user,
        status_code=409,
    )


def see_other(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# --- error pages ----------------------------------------------------------


async def _not_authenticated(request: Request, exc: NotAuthenticated):
    if request.method in ("GET", "HEAD"):
        return see_other("/login")

    return message_page(
        request,
        title="需要重新进入",
        message="登录状态已失效，这次提交没有保存。请重新进入后再试。",
        status_code=401,
        links=[("/login", "重新进入")],
    )


async def _csrf_rejected(request: Request, exc: CsrfRejected):
    return message_page(
        request,
        title="表单已失效",
        message="这个表单来自旧页面或其他网站，为了安全没有执行。请回到页面刷新后重试。",
        status_code=403,
        links=[("/experiences", "回到我的经历")],
    )


async def _not_found(request: Request, exc: NotFound):
    return message_page(
        request,
        title="找不到这条内容",
        message=HTTP_MESSAGES[404],
        status_code=404,
        links=[("/experiences", "回到我的经历")],
    )


async def _not_allowed(request: Request, exc: NotAllowed):
    return message_page(
        request,
        title="这个操作现在不能执行",
        message=exc.reason,
        status_code=409,
        links=[("/experiences", "回到我的经历")],
    )


async def _conflict(request: Request, exc: Conflict):
    item = exc.current

    return message_page(
        request,
        title="操作没有执行：内容已经更新",
        message=(
            "这条经历在你打开页面之后有了新的保存或状态变化。为了不覆盖或处理你没有看到的内容，"
            "这次操作没有执行。请先查看最新内容，再决定下一步。"
        ),
        status_code=409,
        links=[(_latest_page(item), "查看最新内容")],
    )


async def _version_conflict(request: Request, exc: VersionConflict):
    return message_page(
        request,
        title="操作没有执行：这个历史版本的状态已经变化",
        message=(
            "你打开这个页面之后，这个历史版本又被删除或恢复过（可能是在另一个标签页）。"
            "为了不处理你没有看到的那一次删除，这次操作没有执行。请回到历史页查看它现在的状态，再决定下一步。"
        ),
        status_code=409,
        links=[(f"/experiences/{exc.item_id}/history", "查看历史")],
    )


async def _attachment_conflict(request: Request, exc: AttachmentConflict):
    return message_page(
        request,
        title="操作没有执行：这个附件的状态已经变化",
        message=(
            "你打开这个页面之后，这个附件又被删除或恢复过（可能是在另一个标签页）。"
            "为了不处理你没有看到的那一次删除，这次操作没有执行。请回到经历页面查看它现在的状态，再决定下一步。"
        ),
        status_code=409,
        links=[(f"/experiences/{exc.item_id}", "查看这条经历")],
    )


def _latest_page(item) -> str:
    if item.deleted_at is not None:
        return "/experiences?tab=trash"

    return record_page(item)


async def _storage_error(request: Request, exc: StorageError):
    logger.error(
        "database operation failed on %s %s (%s)",
        request.method,
        request.url.path,
        type(exc.__cause__).__name__,
    )

    return message_page(
        request,
        title="操作没有完成",
        message="数据库操作失败，这次操作没有生效，已保存的数据保持原样。请稍后再试。",
        status_code=500,
        links=[("/experiences", "回到我的经历")],
    )


async def _http_error(request: Request, exc: StarletteHTTPException):
    return message_page(
        request,
        title="没有执行",
        message=HTTP_MESSAGES.get(exc.status_code, "请求没有执行。"),
        status_code=exc.status_code,
        links=[("/experiences", "回到我的经历")],
    )


# --- pages ----------------------------------------------------------------


@router.get("/")
def home():
    return see_other("/experiences")


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/login")
def login_page(request: Request, notice: str = ""):
    rt = runtime(request)
    context = {"notice": NOTICES.get(notice), "users": [], "login_token": None}

    if not rt.config.dev_login:
        return render(request, "login.html", context)

    with rt.database.transaction() as db:
        context["users"] = dev_users(db)

    context["login_token"] = secrets.token_urlsafe(32)
    response = render(request, "login.html", context)
    response.set_cookie(
        LOGIN_COOKIE,
        context["login_token"],
        max_age=900,
        httponly=True,
        samesite="strict",
        path="/login",
    )

    return response


@router.post("/login/dev")
def dev_login(request: Request, form: dict[str, str] = Depends(submitted_form)):
    rt = runtime(request)

    if not rt.config.dev_login:
        raise HTTPException(status_code=404)

    check_same_origin(request)

    if not _tokens_match(request.cookies.get(LOGIN_COOKIE), form.get("login_token")):
        raise CsrfRejected()

    with rt.database.transaction() as db:
        user = db.get(User, form.get("user_id", ""))

        if user is None or user.auth_provider != DEV_PROVIDER:
            raise HTTPException(status_code=400)

        previous = request.cookies.get(SESSION_COOKIE)

        if previous:
            end_session(db, token_hash(previous))

        token = start_session(db, user.id, rt.now(), rt.config.session_lifetime)

    response = see_other("/experiences")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(rt.config.session_lifetime.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(LOGIN_COOKIE, path="/login")

    return response


@router.post("/logout", dependencies=[Depends(checked_form)])
def logout(request: Request, user: CurrentUser = Depends(current_user)):
    return logout_response(runtime(request), user, "logged_out")


@router.get("/experiences")
def experience_list(
    request: Request,
    tab: str = "current",
    notice: str = "",
    user: CurrentUser = Depends(current_user),
):
    """The overview of the user's experiences, or (tab=trash) the recycle bin.

    The overview shows the list that library_sidebar finds for the address's
    filter, in the main area. A list that cannot be read is said to be
    unreadable there, never shown as an empty library.
    """
    rt = runtime(request)
    now = rt.now()

    if tab != "trash":
        return render(
            request,
            "list.html",
            {"tab": "current", "notice": NOTICES.get(notice), **library_sidebar(request, user, overview=True)},
            user=user,
        )

    with rt.database.transaction() as db:
        items = list_experiences(db, user.id)
        trash = trash_view(db, user.id, now)

    return render(
        request,
        "list.html",
        {
            "tab": "trash",
            "trash": trash,
            "now": now,
            "notice": NOTICES.get(notice),
            **library_sidebar(request, user, items=items, focus=True),
        },
        user=user,
    )


@router.get("/experiences/new")
def new_experience(
    request: Request,
    kind: str = PROJECT,
    notice: str = "",
    user: CurrentUser = Depends(current_user),
):
    """The form for a new experience of one category (kind; project if not given).

    kind is separate from the list filter (category), so switching the kind
    of a new record leaves the list on the left as it was.
    """
    if kind not in EXPERIENCE_CATEGORIES:
        raise NotFound()

    # The leave dialog can save another record and continue here.
    return form_page(
        request,
        user,
        category=kind,
        values=stored_values(category=kind),
        notice=NOTICES.get(notice),
    )


@router.post("/experiences")
def create_experience_page(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    category = form.get("category", PROJECT)

    if category not in EXPERIENCE_CATEGORIES:
        raise HTTPException(status_code=400)

    try:
        entry = parse_experience_form(form, category)

        with rt.database.transaction() as db:
            item = create_experience(db, user.id, entry, rt.now())
    except ContentError as error:
        return form_page(
            request,
            user,
            category=category,
            values=submitted_values(form, category),
            error=error.message,
            field_errors=error.field_errors,
            next_path=form.get("next", ""),
            status_code=422,
        )
    except StorageError as error:
        logger.error("saving a new experience failed (%s)", type(error.__cause__).__name__)
        return form_page(
            request,
            user,
            category=category,
            values=submitted_values(form, category),
            error=SAVE_FAILED,
            next_path=form.get("next", ""),
            status_code=500,
        )

    return finish_save(request, user, form, f"/experiences/{item.id}", "created")


@router.get("/experiences/{item_id}")
def experience_detail(
    item_id: str,
    request: Request,
    notice: str = "",
    user: CurrentUser = Depends(current_user),
):
    return detail_page(request, user, item_id, notice=NOTICES.get(notice))


def detail_page(
    request: Request,
    user: CurrentUser,
    item_id: str,
    *,
    notice: str | None = None,
    upload_error: str | None = None,
    status_code: int = 200,
) -> Response:
    """The reading page of an experience, with its attachments and a fresh upload form."""
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item = active_item(db, user.id, item_id)
        attachments = kept_attachments(db, user.id, item)
        binned = binned_attachments(db, user.id, item, now)
        taken = places_taken(db, item.id)

    return render(
        request,
        "detail.html",
        {
            "item": item,
            "notice": notice,
            "attachments": attachments,
            # A record whose file is gone says so instead of offering a download that fails.
            "file_states": {
                attachment.id: rt.files.kept_file_state(attachment.owner_id, attachment.storage_key)
                for attachment in attachments
            },
            "binned_attachments": binned,
            "places_left": max(0, MAX_ATTACHMENTS - taken),
            # A new key for every page: posting this form again cannot add the file twice.
            "upload_key": secrets.token_urlsafe(24),
            "upload_error": upload_error,
            "now": now,
            **library_sidebar(request, user, selected_id=item.id, focus=True),
        },
        user=user,
        status_code=status_code,
    )


@router.get("/experiences/{item_id}/edit")
def edit_experience(item_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item = active_item(db, user.id, item_id)

    return form_page(
        request,
        user,
        category=item.category,
        values=stored_values(item),
        item_id=item.id,
        item=item,
        expected_revision=item.revision,
    )


@router.post("/experiences/{item_id}/draft")
def save_experience_draft(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)
    # The stored record decides which fields it has; the form's own category
    # is only used to show the form again if the record is gone.
    category = submitted_category(form)

    try:
        with rt.database.transaction() as db:
            category = active_item(db, user.id, item_id).category
            entry = parse_experience_form(form, category)
            item, changed = save_draft(db, user.id, item_id, expected, entry, rt.now())
    except ContentError as error:
        return form_page(
            request,
            user,
            category=category,
            values=submitted_values(form, category),
            item_id=item_id,
            expected_revision=expected,
            error=error.message,
            field_errors=error.field_errors,
            next_path=form.get("next", ""),
            status_code=422,
        )
    except Conflict as conflict:
        return conflict_page(request, user, conflict.current, submitted_values(form, category))
    except NotFound:
        return form_page(
            request,
            user,
            category=category,
            values=submitted_values(form, category),
            error=GONE_WHILE_EDITING,
            status_code=404,
        )
    except StorageError as error:
        logger.error("saving a draft failed (%s)", type(error.__cause__).__name__)
        return form_page(
            request,
            user,
            category=category,
            values=submitted_values(form, category),
            item_id=item_id,
            expected_revision=expected,
            error=SAVE_FAILED,
            next_path=form.get("next", ""),
            status_code=500,
        )

    return finish_save(
        request,
        user,
        form,
        f"/experiences/{item_id}",
        "saved" if changed else "unchanged",
    )


@router.post("/experiences/{item_id}/confirm")
def confirm_experience_page(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            confirm_experience(db, user.id, item_id, _expected_revision(form), rt.now())
    except Conflict:
        return message_page(
            request,
            title="确认没有生效",
            message=(
                "这条经历在你打开页面之后有了新的保存或状态变化。为了不确认你没有看到的内容，"
                "这次确认没有生效。请先查看最新内容，再决定是否确认。"
            ),
            status_code=409,
            user=user,
            links=[(f"/experiences/{item_id}", "查看最新内容")],
        )

    return see_other(f"/experiences/{item_id}?notice=confirmed")


@router.get("/experiences/{item_id}/history")
def history_page(
    item_id: str,
    request: Request,
    notice: str = "",
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        history = experience_history(db, user.id, item_id, now, ALL_KINDS)
        waiting, finished = version_cleanups(db, user.id, item_id, now)
        states = {
            version.id: version_state(db, version)
            for version in history.versions
            if version.deleted_at is not None
        }

    return render(
        request,
        "history.html",
        {
            "item": history.item,
            "versions": history.versions,
            "events": history.events,
            "waiting": waiting,
            "finished": finished,
            "now": now,
            "notice": NOTICES.get(notice),
            "is_current_confirmed": is_current_confirmed,
            "version_states": states,
        },
        user=user,
    )


@router.get("/experiences/{item_id}/versions/{version_id}")
def version_page(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item, version = get_version(db, user.id, item_id, version_id, ALL_KINDS)

    return render(
        request,
        "version.html",
        {
            "item": item,
            "version": version,
            "deletable": not is_current_confirmed(item, version),
        },
        user=user,
    )


@router.get("/experiences/{item_id}/versions/{version_id}/delete")
def version_delete_page(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item, version = deletable_version(db, user.id, item_id, version_id, ALL_KINDS)
        state = version_state(db, version)

    return confirm_page(
        request,
        user,
        title=f"删除历史版本：{version_label(version)}",
        points=[
            f"只删除这一个历史版本；{_owner_noun(item)}的当前内容和其他版本不受影响。",
            "删除后它立即不能查看；恢复期内导出时仍会带上它，并标明“已单独删除”和恢复期限。",
            f"{RESTORE_WINDOW.days} 天内（截至 {format_local(now + RESTORE_WINDOW)}）"
            "可以在历史页恢复，也可以提前永久删除。",
            "已经下载到本机的导出文件如果包含这个版本，需要你自己处理。",
        ],
        action=f"/experiences/{item.id}/versions/{version.id}/delete",
        button="删除这个历史版本",
        cancel=f"/experiences/{item.id}/history",
        hidden={"expected_state": state},
        section=section_of(item),
    )


@router.post("/experiences/{item_id}/versions/{version_id}/delete")
def version_delete(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        delete_version(db, user.id, item_id, version_id, _expected_state(form), rt.now(), ALL_KINDS)

    return see_other(f"/experiences/{item_id}/history?notice=version_deleted")


@router.post("/experiences/{item_id}/versions/{version_id}/restore")
def version_restore(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        restore_version(db, user.id, item_id, version_id, _expected_state(form), rt.now(), ALL_KINDS)

    return see_other(f"/experiences/{item_id}/history?notice=version_restored")


@router.get("/experiences/{item_id}/versions/{version_id}/purge")
def version_purge_page(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item, version = trashed_version(db, user.id, item_id, version_id, rt.now(), ALL_KINDS)
        state = version_state(db, version)

    return _version_purge_confirm(request, user, item, version, state)


def _version_purge_confirm(request, user, item, version, state, error=None, status_code=200):
    return confirm_page(
        request,
        user,
        title=f"永久删除历史版本：{version_label(version)}",
        points=[
            "永久删除后无法恢复。",
            f"只删除这一个历史版本，{_owner_noun(item)}的当前内容和其他版本不受影响。",
            "清理会立即开始；如果暂时没有完成，这个版本也已经不能访问或恢复，系统会自动重试。",
            "已经下载到本机的导出文件不受影响。",
        ],
        action=f"/experiences/{item.id}/versions/{version.id}/purge",
        button="永久删除这个历史版本",
        cancel=f"/experiences/{item.id}/history",
        hidden={"expected_state": state},
        acknowledge="我了解永久删除后无法恢复",
        error=error,
        status_code=status_code,
        section=section_of(item),
    )


@router.post("/experiences/{item_id}/versions/{version_id}/purge")
def version_purge(
    item_id: str,
    version_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_state(form)

    if form.get("acknowledge") != "yes":
        with rt.database.transaction() as db:
            item, version = trashed_version(db, user.id, item_id, version_id, rt.now(), ALL_KINDS)
            check_version_state(db, version, expected)

        return _version_purge_confirm(
            request,
            user,
            item,
            version,
            expected,
            error="请先勾选“我了解永久删除后无法恢复”。",
            status_code=422,
        )

    with rt.database.transaction() as db:
        job_id = request_version_purge(
            db, user.id, item_id, version_id, expected, rt.now(), ALL_KINDS
        )

    outcome = run_purge_job(rt.database, job_id, rt.now(), rt.files)
    notice = "version_purged" if outcome == "done" else "version_purge_pending"

    return see_other(f"/experiences/{item_id}/history?notice={notice}")


# What deleting the profile or the preferences leaves alone.
DOCUMENT_UNTOUCHED = {
    PROFILE: "你的经历（包括教育经历）和求职偏好不受影响。",
    PREFERENCES: "你的经历和基本资料不受影响。",
}


def _owner_noun(item) -> str:
    """Whose current content a history version belongs to, for its delete pages."""
    return KIND_NOUNS[item.category] if item.category in DOCUMENT_KINDS else "这条经历"


def _delete_title(item) -> str:
    if item.category in DOCUMENT_KINDS:
        return f"删除{CATEGORY_LABELS[item.category]}"

    return f"删除经历：{display_title(item.content, item.category)}"


@router.get("/experiences/{item_id}/delete")
def delete_page(item_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    with rt.database.transaction() as db:
        scope = deletion_scope(db, user.id, item_id, rt.now(), ALL_KINDS)
        references = list_references(db, user.id, item_id, rt.now(), ALL_KINDS)

    item = scope.item
    noun = KIND_NOUNS[item.category]

    if item.category in DOCUMENT_KINDS:
        points = [f"将删除：{noun}的当前内容，以及 {scope.kept_versions} 个保留中的历史版本。"]
    else:
        points = [
            f"将删除：{noun}的当前内容、私有备注，以及 {scope.kept_versions} 个保留中的历史版本。"
        ]

    if scope.versions_already_in_bin:
        points.append(
            f"另有 {scope.versions_already_in_bin} 个历史版本此前已经单独删除，会保持各自原来的恢复期限。"
        )

    if scope.kept_attachments:
        points.append(f"{scope.kept_attachments} 个附件会一起移到回收站，恢复这条经历时会一起恢复。")

    if scope.attachments_already_in_bin:
        points.append(
            f"另有 {scope.attachments_already_in_bin} 个附件此前已经单独删除，会保持各自原来的恢复期限，"
            "不会随这条经历一起恢复。"
        )

    if item.category in DOCUMENT_KINDS:
        points += [
            "删除后立即停止使用：不会出现在导出的当前部分，也不能再编辑或查看历史。",
            DOCUMENT_UNTOUCHED[item.category],
        ]
    else:
        points.append(
            "删除后立即停止使用：不会出现在经历列表和导出的当前部分，也不能再编辑、确认或查看历史。"
        )

    points += [
        f"{RESTORE_WINDOW.days} 天内（截至 {format_local(scope.restore_deadline)}）可以在回收站恢复，"
        "也可以提前永久删除；到期后会被永久删除。",
        "已经下载到本机的导出文件不受影响，需要你自己处理。",
        reference_note(references),
    ]

    return confirm_page(
        request,
        user,
        title=_delete_title(item),
        points=points,
        action=f"/experiences/{item.id}/delete",
        button="移到回收站",
        cancel=record_page(item),
        hidden={"expected_revision": str(item.revision)},
        section=section_of(item),
    )


@router.post("/experiences/{item_id}/delete")
def delete_item(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item = delete_experience(db, user.id, item_id, _expected_revision(form), rt.now(), ALL_KINDS)
        category = item.category

    if category in DOCUMENT_KINDS:
        return see_other(f"{DOCUMENT_PAGES[category]}?notice={category}_deleted")

    return see_other("/experiences?notice=deleted")


@router.post("/experiences/{item_id}/restore")
def restore_item(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item = restore_experience(db, user.id, item_id, _expected_revision(form), rt.now(), ALL_KINDS)
        page = record_page(item)

    return see_other(f"{page}?notice=restored")


@router.get("/experiences/{item_id}/purge")
def purge_page(item_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item = trashed_item(db, user.id, item_id, rt.now(), ALL_KINDS)
        attachments = attachment_count(db, item)
        references = list_references(db, user.id, item_id, rt.now(), ALL_KINDS)

    return _purge_confirm(request, user, item, attachments, references)


def _purge_confirm(request, user, item, attachments, references, error=None, status_code=200):
    if item.category in DOCUMENT_KINDS:
        what = f"会删除{KIND_NOUNS[item.category]}的全部内容和全部历史版本。"
    elif attachments:
        what = (
            f"会删除{KIND_NOUNS[item.category]}的全部内容、私有备注、全部历史版本，"
            f"以及它的 {attachments} 个附件文件（包括此前单独删除、还没到期的）。"
        )
    else:
        what = f"会删除{KIND_NOUNS[item.category]}的全部内容、私有备注和全部历史版本。"

    return confirm_page(
        request,
        user,
        title=f"永久{_delete_title(item)}",
        points=[
            "永久删除后无法恢复。",
            what,
            "清理会立即开始；如果暂时没有完成，内容也已经不能访问或恢复，系统会自动重试，并在回收站显示进度。",
            "已经下载到本机的导出文件不受影响。",
            reference_note(references),
        ],
        action=f"/experiences/{item.id}/purge",
        button="永久删除",
        cancel="/experiences?tab=trash",
        hidden={"expected_revision": str(item.revision)},
        acknowledge="我了解永久删除后无法恢复",
        error=error,
        status_code=status_code,
        section="trash",
    )


@router.post("/experiences/{item_id}/purge")
def purge_item(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)

    if form.get("acknowledge") != "yes":
        with rt.database.transaction() as db:
            item = trashed_item(db, user.id, item_id, rt.now(), ALL_KINDS)
            check_revision(item, expected)
            attachments = attachment_count(db, item)
            references = list_references(db, user.id, item_id, rt.now(), ALL_KINDS)

        return _purge_confirm(
            request,
            user,
            item,
            attachments,
            references,
            error="请先勾选“我了解永久删除后无法恢复”。",
            status_code=422,
        )

    with rt.database.transaction() as db:
        job_id = request_experience_purge(db, user.id, item_id, expected, rt.now(), ALL_KINDS)

    outcome = run_purge_job(rt.database, job_id, rt.now(), rt.files)
    notice = "purged" if outcome == "done" else "purge_pending"

    return see_other(f"/experiences?tab=trash&notice={notice}")


@router.get("/experiences/{item_id}/export.md")
def export_one(item_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    with rt.database.transaction() as db:
        filename, markdown = single_export(db, user.id, item_id, rt.now())

    return Response(
        markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/all.zip")
def export_all(request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        content = collect_export(db, user.id, now)

    try:
        package = write_export(content, rt.files, user.label, now)
    except (FileStoreError, OSError) as error:
        logger.error("building an export failed (%s)", type(error).__name__)
        return message_page(
            request,
            title="导出没有完成",
            message="生成导出文件时出错，没有下载任何内容。经历库里的内容不受影响，请稍后再试。",
            status_code=500,
            user=user,
            links=[("/experiences", "回到我的经历")],
        )

    return StreamingResponse(
        read_chunks(package.handle),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{package.filename}"',
            "Content-Length": str(package.size),
        },
    )


# --- attachments ----------------------------------------------------------------
#
# Only a saved experience of the six categories has attachments; the
# profile and the preferences do not. Every route finds the experience
# through the session's owner first and the attachment within it, so an
# attachment id under another experience, or of another user, is not found.


def content_disposition(filename: str) -> str:
    """A download header that keeps any file name and that no file name can break."""
    plain = "".join(ch if " " <= ch <= "~" and ch not in '"\\%' else "_" for ch in filename)

    return f"attachment; filename=\"{plain or 'attachment'}\"; filename*=UTF-8''{quote(filename, safe='')}"


async def _drain(chunks, length: int) -> None:
    """Read the rest of a refused upload and drop it, so the browser shows the answer.

    Without this, the connection would be closed while the browser is still
    sending, and it would show a broken connection instead. Stops at the
    announced length; nothing is kept.
    """
    received = 0

    try:
        async for chunk in chunks:
            received += len(chunk)

            if received > length:
                return
    except ClientDisconnect:
        return


@router.post("/experiences/{item_id}/attachments")
async def upload_attachment(item_id: str, request: Request):
    """Receive one file for a saved experience (attachments.py describes the steps).

    The session, the post's origin and the experience are checked before the
    body is read, and the CSRF token, which comes first in the body, before
    any byte of the file is written.
    """
    rt = runtime(request)
    chunks = request.stream()
    # FormSizeLimit has made sure the length is there and within the upload limit.
    length = int(request.headers["content-length"])

    try:
        user = await run_in_threadpool(current_user, request)
        check_same_origin(request)
        has_place = await run_in_threadpool(_has_place, rt, user.id, item_id)
    except (NotAuthenticated, CsrfRejected, NotFound):
        await _drain(chunks, length)
        raise

    if not has_place:
        await _drain(chunks, length)
        return await run_in_threadpool(
            detail_page, request, user, item_id, upload_error=UPLOAD_FULL, status_code=409
        )

    try:
        received = await receive_upload(
            chunks,
            content_type=request.headers.get("content-type", ""),
            content_length=length,
            files=rt.files,
            token_ok=lambda value: _tokens_match(user.csrf_token, value),
        )
    except UploadTokenRejected:
        await _drain(chunks, length)
        raise CsrfRejected()
    except UploadRejected as rejected:
        if rejected.drain:
            await _drain(chunks, length)

        return await run_in_threadpool(
            detail_page,
            request,
            user,
            item_id,
            upload_error=rejected.message,
            status_code=rejected.status_code,
        )

    outcome = await run_in_threadpool(store_upload, rt.database, rt.files, user.id, item_id, received, rt.now())

    if outcome in (UploadOutcome.ADDED, UploadOutcome.ALREADY_ADDED):
        return see_other(f"/experiences/{item_id}?notice=attachment_{outcome}#attachments")

    if outcome == UploadOutcome.PARENT_GONE:
        return message_page(
            request,
            title="附件没有上传",
            message="这条经历已经不存在，或在上传期间被删除了，所以这个文件没有上传。",
            status_code=404,
            user=user,
            links=[("/experiences", "回到我的经历")],
        )

    status_code, message = UPLOAD_PROBLEMS[outcome]

    return await run_in_threadpool(
        detail_page, request, user, item_id, upload_error=message, status_code=status_code
    )


def _has_place(rt: LibraryRuntime, owner_id: str, item_id: str) -> bool:
    with rt.database.transaction() as db:
        return has_free_place(db, owner_id, item_id)


@router.get("/experiences/{item_id}/attachments/{attachment_id}/download")
def download_attachment(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    """The attachment's bytes, after checking them against the recorded size and checksum."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        attachment = kept_attachment(db, user.id, item_id, attachment_id)

    try:
        handle = rt.files.open_checked(user.id, attachment.storage_key, attachment.size, attachment.sha256)
    except FileStoreError as error:
        logger.warning("attachment %s could not be sent (%s)", attachment.id, type(error).__name__)
        return message_page(
            request,
            title="附件没有下载",
            message=FILE_PROBLEMS.get(type(error), UNREADABLE_FILE),
            status_code=500,
            user=user,
            links=[(f"/experiences/{item_id}", "回到这条经历")],
        )

    return StreamingResponse(
        read_chunks(handle),
        media_type=attachment.media_type,
        headers={
            "Content-Disposition": content_disposition(attachment.filename),
            "Content-Length": str(attachment.size),
            "Content-Security-Policy": DOWNLOAD_POLICY,
        },
    )


@router.get("/experiences/{item_id}/attachments/{attachment_id}/delete")
def attachment_delete_page(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item, attachment = attachment_of(db, user.id, item_id, attachment_id, now, in_bin=False)

    return confirm_page(
        request,
        user,
        title=f"删除附件：{attachment.filename}",
        points=[
            "只删除这一个附件；这条经历的内容、历史版本和其他附件不受影响。",
            "删除后立即不能下载；恢复期内导出全部时，它会放在导出包的回收站部分，并标明恢复期限。",
            f"{RESTORE_WINDOW.days} 天内（截至 {format_local(now + RESTORE_WINDOW)}）"
            "可以在这条经历的页面恢复，也可以提前永久删除；在那之前它仍占用一个附件名额。",
            "已经下载到本机的文件需要你自己处理。",
        ],
        action=f"/experiences/{item.id}/attachments/{attachment.id}/delete",
        button="删除这个附件",
        cancel=f"/experiences/{item.id}",
        hidden={"expected_revision": str(attachment.revision)},
    )


@router.post("/experiences/{item_id}/attachments/{attachment_id}/delete")
def attachment_delete(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        delete_attachment(db, user.id, item_id, attachment_id, _expected_revision(form), rt.now())

    return see_other(f"/experiences/{item_id}?notice=attachment_deleted#attachments")


@router.post("/experiences/{item_id}/attachments/{attachment_id}/restore")
def attachment_restore(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        restore_attachment(db, user.id, item_id, attachment_id, _expected_revision(form), rt.now())

    return see_other(f"/experiences/{item_id}?notice=attachment_restored#attachments")


@router.get("/experiences/{item_id}/attachments/{attachment_id}/purge")
def attachment_purge_page(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
):
    rt = runtime(request)

    with rt.database.transaction() as db:
        item, attachment = attachment_of(db, user.id, item_id, attachment_id, rt.now(), in_bin=True)

    return _attachment_purge_confirm(request, user, item, attachment)


def _attachment_purge_confirm(request, user, item, attachment, error=None, status_code=200):
    return confirm_page(
        request,
        user,
        title=f"永久删除附件：{attachment.filename}",
        points=[
            "永久删除后无法恢复。",
            "只删除这一个附件的文件；这条经历和其他附件不受影响。",
            "清理会立即开始；如果暂时没有完成，这个附件也已经不能下载或恢复，系统会自动重试。",
            "已经下载到本机的文件不受影响。",
        ],
        action=f"/experiences/{item.id}/attachments/{attachment.id}/purge",
        button="永久删除这个附件",
        cancel=f"/experiences/{item.id}",
        hidden={"expected_revision": str(attachment.revision)},
        acknowledge="我了解永久删除后无法恢复",
        error=error,
        status_code=status_code,
    )


@router.post("/experiences/{item_id}/attachments/{attachment_id}/purge")
def attachment_purge(
    item_id: str,
    attachment_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)

    if form.get("acknowledge") != "yes":
        with rt.database.transaction() as db:
            item, attachment = attachment_of(db, user.id, item_id, attachment_id, rt.now(), in_bin=True)
            check_attachment_revision(attachment, expected)

        return _attachment_purge_confirm(
            request,
            user,
            item,
            attachment,
            error="请先勾选“我了解永久删除后无法恢复”。",
            status_code=422,
        )

    with rt.database.transaction() as db:
        job_id = request_attachment_purge(db, user.id, item_id, attachment_id, expected, rt.now())

    outcome = run_purge_job(rt.database, job_id, rt.now(), rt.files)
    notice = "attachment_purged" if outcome == "done" else "attachment_purge_pending"

    return see_other(f"/experiences/{item_id}?notice={notice}#attachments")


# --- the basic profile and the job preferences --------------------------------
#
# Each user keeps at most one of each. They are read and edited on their own
# pages; their history, deletion, restoring and export use the record routes
# above.


def document_view(request: Request, user: CurrentUser, kind: str, notice: str) -> Response:
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item = current_document(db, user.id, kind)
        # School and major live in the education experiences only, never twice.
        education = list_experiences(db, user.id, category=EDUCATION) if kind == PROFILE else []
        in_bin = bool(binned_documents(db, user.id, kind, now))

    return render(
        request,
        "document.html",
        {
            "kind": kind,
            "item": item,
            "education": education,
            "in_bin": in_bin,
            "notice": NOTICES.get(notice),
            "section": kind,
        },
        user=user,
    )


def document_form(
    request: Request,
    user: CurrentUser,
    kind: str,
    *,
    values: dict[str, str],
    item_id: str | None = None,
    expected_revision: int | None = None,
    error: str | None = None,
    field_errors: dict[str, str] | None = None,
    next_path: str = "",
    status_code: int = 200,
) -> Response:
    """The form of the profile or preferences. Without item_id it starts a new one."""
    base = DOCUMENT_PAGES[kind]

    return render(
        request,
        "document_form.html",
        {
            "kind": kind,
            "item_id": item_id,
            "values": values,
            "expected_revision": expected_revision,
            "error": error,
            "field_errors": field_errors or {},
            "next_path": safe_next(next_path) or "",
            # A page shown again after a failed save may hold text that exists
            # nowhere else yet; the leave guard must treat it as unsaved.
            "unsaved_input": bool(error) and any(value.strip() for value in values.values()),
            "action": f"{base}/{item_id}/save" if item_id else base,
            "section": kind,
        },
        user=user,
        status_code=status_code,
    )


def document_edit(request: Request, user: CurrentUser, kind: str) -> Response:
    rt = runtime(request)

    with rt.database.transaction() as db:
        item = current_document(db, user.id, kind)

    if item is None:
        return document_form(request, user, kind, values=stored_values(category=kind))

    return document_form(
        request,
        user,
        kind,
        values=stored_values(item),
        item_id=item.id,
        expected_revision=item.revision,
    )


def document_create(request: Request, user: CurrentUser, form: dict[str, str], kind: str) -> Response:
    rt = runtime(request)

    try:
        entry = parse_experience_form(form, kind)

        with rt.database.transaction() as db:
            create_document(db, user.id, entry, rt.now())
    except ContentError as error:
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            error=error.message,
            field_errors=error.field_errors,
            next_path=form.get("next", ""),
            status_code=422,
        )
    except Conflict as conflict:
        # Another page created it meanwhile: show both, overwrite nothing.
        return conflict_page(request, user, conflict.current, submitted_values(form, kind))
    except StorageError as error:
        logger.error("saving a new %s failed (%s)", kind, type(error.__cause__).__name__)
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            error=SAVE_FAILED,
            next_path=form.get("next", ""),
            status_code=500,
        )

    return finish_save(request, user, form, DOCUMENT_PAGES[kind], f"{kind}_created", document=True)


def document_save(
    request: Request,
    user: CurrentUser,
    form: dict[str, str],
    kind: str,
    item_id: str,
) -> Response:
    rt = runtime(request)
    expected = _expected_revision(form)

    try:
        with rt.database.transaction() as db:
            # Someone else's or a missing record is not found, whatever the form holds.
            active_item(db, user.id, item_id, kinds=(kind,))
            entry = parse_experience_form(form, kind)
            _, changed = save_draft(db, user.id, item_id, expected, entry, rt.now())
    except ContentError as error:
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            item_id=item_id,
            expected_revision=expected,
            error=error.message,
            field_errors=error.field_errors,
            next_path=form.get("next", ""),
            status_code=422,
        )
    except Conflict as conflict:
        return conflict_page(request, user, conflict.current, submitted_values(form, kind))
    except NotFound:
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            error=DOCUMENT_GONE[kind],
            status_code=404,
        )
    except StorageError as error:
        logger.error("saving the %s failed (%s)", kind, type(error.__cause__).__name__)
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            item_id=item_id,
            expected_revision=expected,
            error=SAVE_FAILED,
            next_path=form.get("next", ""),
            status_code=500,
        )

    notice = f"{kind}_saved" if changed else f"{kind}_unchanged"

    return finish_save(request, user, form, DOCUMENT_PAGES[kind], notice, document=True)


@router.get("/basic-profile")
def profile_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return document_view(request, user, PROFILE, notice)


@router.get("/basic-profile/edit")
def profile_edit(request: Request, user: CurrentUser = Depends(current_user)):
    return document_edit(request, user, PROFILE)


@router.post("/basic-profile")
def profile_create(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return document_create(request, user, form, PROFILE)


@router.post("/basic-profile/{item_id}/save")
def profile_save(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return document_save(request, user, form, PROFILE, item_id)


@router.get("/job-preferences")
def preferences_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return document_view(request, user, PREFERENCES, notice)


@router.get("/job-preferences/edit")
def preferences_edit(request: Request, user: CurrentUser = Depends(current_user)):
    return document_edit(request, user, PREFERENCES)


@router.post("/job-preferences")
def preferences_create(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return document_create(request, user, form, PREFERENCES)


@router.post("/job-preferences/{item_id}/save")
def preferences_save(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return document_save(request, user, form, PREFERENCES, item_id)
