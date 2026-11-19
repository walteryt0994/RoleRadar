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

U2-B1 adds resume imports (imports_web.py): their upload is the second post
that may be larger than a form, and the clean-up pass also ends expired
imports and removes their files.
"""
import ipaddress
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
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

from app.analysis_words import LISTED_MODES, listed_parts, requirement_group, requirement_words
from app import i18n
from app.i18n import LANGUAGE_NAMES, LANGUAGES, LanguageMiddleware, MessageMap, format_when, html_lang, lazy, t, tn, word
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
    TITLE_FIELDS,
    ALL_KINDS,
    BODY,
    BODY_SOURCES,
    MAIN_FIELDS,
    MORE_FIELDS,
    compose_body,
    split_fields_in,
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
    AlongChanged,
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
from app.library.imports import (
    MAX_IMPORT_REQUEST_BYTES,
    binned_drafts,
    cleanup_imports,
    drafts_clearing,
    referenced_import_files,
)
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
    delete_experience_and_chosen,
    delete_version_and_chosen,
    deletion_scope,
    direction_bin,
    analyses_along,
    analyses_purged_with,
    explorations_along,
    explorations_purged_with,
    job_bin,
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
from app.library.job_words import source_label, type_word
from app.library.models import User
from app.library.places import CITY_CHOICES
from app.library.months import MONTHS, PICKER_FIELDS, YEARS, period_fits, picker_values, with_period
from app.library.preference_rows import (
    JOB_TYPES,
    MAX_ROWS,
    ROWS_KEY,
    STEPS,
    fold_rows,
    posted_rows,
    requirement_parts,
    rows_kept,
    rows_of,
    start_of,
    type_label,
    with_rows,
)
from app.library.presentation import (
    CATEGORY_GROUPS,
    CATEGORY_NAMES,
    group_label,
    hint_of,
    label_of,
    preference_label,
    preference_parts,
    preference_topic,
    shown_fields,
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
    analysis_note,
    direction_note,
    record_page,
    reference_note,
    section_of,
    state_label,
    summary_line,
    unfilled_labels,
    version_label,
)
from app.library.projection import list_references, list_version_references
from app.library.references import read_record_refs, record_ref, resting_analyses, resting_explorations
from app.library.sends import sends_text
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
from app.library.candidates import Structurer
from app.library.resume_reader import Readings
from app.library.resume_rules import SimulatedStructurer
from app.library.structuring_runs import StructuringRuns
from app.library.upload import MAX_REQUEST_BYTES, receive_upload

PACKAGE_DIR = Path(__file__).parent
SESSION_COOKIE = "rr_session"
LOGIN_COOKIE = "rr_login"
# Sent with a save from the leave dialog when the user was logging out.
SAVE_THEN_LOGOUT = "logout"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
# The only paths whose posts may be bigger than an ordinary form: an attachment
# upload, and a resume import (a file or a long pasted text).
UPLOAD_PATH = re.compile(r"/experiences/[0-9a-f-]{36}/attachments")
IMPORT_PATH = re.compile(r"/imports")
# A download is a file, never a page: if a browser shows it anyway, nothing in it may run.
DOWNLOAD_POLICY = "default-src 'none'; sandbox"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
SAVE_FAILED = lazy("lib.save_failed")
GONE_WHILE_EDITING = lazy("lib.gone_while_editing")
DOCUMENT_GONE = MessageMap({PROFILE: "lib.profile_gone", PREFERENCES: "lib.preferences_gone"})
# The longest search the record list accepts; anything longer is cut there.
MAX_QUERY_LENGTH = 100
# The notices a page shows after an action, by code; the words are read in the page's language.
NOTICES = MessageMap({code: f"notice.{code}" for code in (
    "created",
    "saved",
    "unchanged",
    "created_before_leaving",
    "saved_before_leaving",
    "unchanged_before_leaving",
    "confirmed",
    "deleted",
    "restored",
    "purged",
    "purge_pending",
    "import_draft_purged",
    "import_draft_purge_pending",
    "version_deleted",
    "version_restored",
    "version_purged",
    "version_purge_pending",
    "saved_logged_out",
    "logged_out",
    "profile_created",
    "profile_saved",
    "profile_unchanged",
    "profile_created_before_leaving",
    "profile_saved_before_leaving",
    "profile_unchanged_before_leaving",
    "profile_deleted",
    "preferences_created",
    "preferences_saved",
    "preferences_unchanged",
    "preferences_created_before_leaving",
    "preferences_saved_before_leaving",
    "preferences_unchanged_before_leaving",
    "preferences_deleted",
    "document_saved_logged_out",
    "attachment_added",
    "attachment_already_added",
    "attachment_deleted",
    "attachment_restored",
    "attachment_purged",
    "attachment_purge_pending",
)})
UPLOAD_PROBLEMS = {
    UploadOutcome.IN_PROGRESS: (409, lazy("lib.upload_in_progress")),
    UploadOutcome.FORM_USED: (409, lazy("lib.upload_form_used")),
    UploadOutcome.FULL: (409, lazy("lib.upload_full", most=MAX_ATTACHMENTS)),
    UploadOutcome.FAILED: (500, lazy("lib.upload_failed")),
}
FILE_PROBLEMS = MessageMap({MissingFile: "lib.file_missing_problem", ChangedFile: "lib.file_changed_problem"})
UNREADABLE_FILE = lazy("lib.file_unreadable_problem")
HTTP_MESSAGES = MessageMap({400: "lib.http_400", 404: "lib.http_404", 405: "lib.http_405"})

logger = logging.getLogger("roleradar.library")
router = APIRouter()


@dataclass
class LibraryRuntime:
    config: LibraryConfig
    database: Database
    clock: Clock
    templates: Jinja2Templates
    files: FileStore
    # Told the token hash of a session that ended here (logging out, signing
    # in as another user, or its time running out and the clean-up removing
    # it), so work tied to it stops (U2-A2; Codex R1 review of the UI batch).
    session_end_listeners: list = field(default_factory=list)
    # More of the clean-up pass, run after its own steps: what other parts of
    # the app hold for a while and free when its time is up (the built export
    # packages, Codex R1 review), so a pass frees it without another request.
    cleanup_steps: list = field(default_factory=list)
    # The resume readings running in child processes (U2-B1).
    readings: Readings = field(default_factory=Readings)
    # Who turns an import's text into candidates here: the local simulation (U2-B2a).
    structurer: Structurer = field(default_factory=SimulatedStructurer)
    # The real structuring runs of this process (U2-C3); ending an import stops its run.
    runs: StructuringRuns = field(init=False)

    def __post_init__(self):
        self.runs = StructuringRuns(self.now)

    def now(self):
        return self.clock.now()

    def session_ended(self, session_token_hash: str) -> None:
        for listener in self.session_end_listeners:
            listener(session_token_hash)

    def run_cleanup(self) -> PurgeReport | None:
        """One clean-up pass: due purge jobs, what unfinished uploads left
        behind, expired and ended resume imports, leftover staging, export
        and import files, then expired sessions (each told to the session-end
        listeners once removed), then the steps others added (cleanup_steps).

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
            cleanup_imports(self.database, self.files, now)
        except LibraryError:
            logger.exception("cleaning up resume imports failed; it will be retried on the next run")
            failed = True

        try:
            recover_uploads(self.database, self.files, now)
            # Both sets are read before the sweep: a file placed after that is too new to be removed.
            self.files.sweep(referenced_files(self.database), imported=referenced_import_files(self.database))
        except LibraryError:
            logger.exception("cleaning up unfinished uploads failed; it will be retried on the next run")
            failed = True

        expired: list[str] = []

        try:
            with self.database.transaction() as db:
                expired = remove_expired_sessions(db, now)
        except LibraryError:
            logger.exception("removing expired sessions failed; it will be retried on the next run")
            failed = True

        # A login whose time ran out has ended like one signed out (Codex R1 review): told once it is removed, so
        # what was tied to it (a built export package, a running task) stops and is freed now, not at its own expiry.
        for session in expired:
            try:
                self.session_ended(session)
            except Exception:  # one listener's failure leaves the others and the next steps to run
                logger.exception("ending the work of an expired session failed")
                failed = True

        for step in self.cleanup_steps:
            try:
                step()
            except Exception:  # retried on the next pass; the steps after it still run
                logger.exception("a clean-up step failed; it will be retried on the next run")
                failed = True

        return None if failed else report


class NotAuthenticated(Exception):
    pass


class CsrfRejected(Exception):
    pass


PASTED_SOURCE_ID = "user-provided"


def posting_source(facts, source_id: str) -> str:
    """A kept posting's source as a page names it: a pasted one by the words of the interface language (its record
    keeps the stable source id it was saved under); any other by the name its source has."""
    if source_id == PASTED_SOURCE_ID:
        return t("job.pasted_source")

    return source_label((facts or {}).get("source_name")) or source_id


JS_MESSAGES = (
    "js.running", "js.unsaved", "js.import.both", "js.import.none", "js.import.doc", "js.import.chosen", "js.import.type", "js.import.empty",
    "js.import.large", "js.import.reading", "js.import.reading_note", "js.upload.none", "js.upload.type", "js.upload.empty",
    "js.upload.large", "js.upload.uploading", "js.upload.uploading_note", "js.motion.opening", "js.motion.retry", "js.motion.slow",
)


def ui_text() -> dict[str, str]:
    """The words the page scripts say, in the page's language: a data attribute of the page, never code (the CSP and
    the pages' own scripts stay as they are)."""
    return {key: t(key) for key in JS_MESSAGES}


def identity_names(parts) -> list[str]:
    """The parts a pasted posting is compared by (app.discovery.records.IDENTITY_PARTS, stable codes), as a page
    says them."""
    return [t(f"job.identity.{part}") for part in parts]


def _evidence_coverage(analysis):
    """岗位要求依据覆盖 for a page (app.evidence_coverage, owner redesign plan 7), loaded only when an analysis is
    shown: the library itself never loads the analysis contracts (test_library_isolation)."""
    from app.evidence_coverage import evidence_coverage

    return evidence_coverage(analysis)


def _counted_findings(analysis):
    """The requirements the share is made of, loaded the same way."""
    from app.evidence_coverage import counted_findings

    return counted_findings(analysis)


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
    # A kept source name as the page's language names the ones this app registers (UI R4); any other as written.
    env.filters["source_label"] = source_label
    env.filters["type_word"] = lambda value: type_word(value) or value
    # The interface language layer (app.i18n): every system text is a message key; the user's words never go through it.
    env.globals.update(
        posting_source=posting_source,
        source_label=source_label,
        type_word=type_word,
        ui_text=ui_text,
        identity_names=identity_names,
        t=t,
        tn=tn,
        word=word,
        html_lang=html_lang,
        when=format_when,
        list_join=i18n.list_join,
        LANGUAGES=LANGUAGES,
        LANGUAGE_NAMES=LANGUAGE_NAMES,
        label_of=label_of,
        hint_of=hint_of,
        group_label=group_label,
        preference_label=preference_label,
        preference_topic=preference_topic,
        preference_parts=preference_parts,
        shown_fields=shown_fields,
        compose_body=compose_body,
        split_fields_in=split_fields_in,
        BODY=BODY,
        BODY_SOURCES=BODY_SOURCES,
        MAIN_FIELDS=MAIN_FIELDS,
        MORE_FIELDS=MORE_FIELDS,
        # The year-and-month picker of an experience's time (owner redesign, plan 3.1).
        YEARS=YEARS,
        MONTHS=MONTHS,
        period_fits=period_fits,
        # The job preferences' overview and three-step editor (owner redesign, plan 3.3).
        PREFERENCE_STEPS=STEPS,
        JOB_TYPES=JOB_TYPES,
        MAX_PREFERENCE_ROWS=MAX_ROWS,
        preference_rows=rows_of,
        requirement_start=start_of,
        type_label=type_label,
        CITY_CHOICES=CITY_CHOICES,
        TITLE_FIELD_OF=TITLE_FIELDS,
        # 岗位要求依据覆盖 above every analysis (owner redesign, plan 7).
        evidence_coverage=_evidence_coverage,
        coverage_id=lambda analysis: format(id(analysis), "x"),
        counted_findings=_counted_findings,
    )
    env.globals.update(
        FIELDS=CATEGORY_FIELDS[PROJECT],
        FIELD_GROUPS=FIELD_GROUPS,
        FOLDED_GROUPS=FOLDED_GROUPS,
        READING_LABELS=READING_LABELS,
        NOTE_FIELD=NOTE_FIELD,
        # Read when a page renders, in its language (the stored Chinese names stay in app.library.content).
        NOT_FILLED=i18n.lazy("content.not_filled"),
        NOT_DECIDED=i18n.lazy("content.not_decided"),
        CATEGORY_FIELDS=CATEGORY_FIELDS,
        CATEGORY_GROUPS=CATEGORY_GROUPS,
        CATEGORY_LABELS=CATEGORY_NAMES,
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
        clock_days=_clock_days(config),
        # Set by the job analysis pages when the local simulation is slowed down on purpose (seconds, as written).
        simulation_seconds=None,
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
        sends_text=sends_text,
        # The shared findings rows (_findings.html), on every page that shows an analysis (U3-D6 R4; each row's group,
        # U3-D6 close-out).
        requirement_words=requirement_words,
        requirement_group=requirement_group,
        listed_parts=listed_parts,
        LISTED_MODES=LISTED_MODES,
    )

    return Jinja2Templates(env=env)


def _clock_days(config: LibraryConfig) -> str | None:
    """How many days the development clock is set ahead, as the banner writes it (None when it is not)."""
    if not config.dev_clock_offset:
        return None

    return f"{config.dev_clock_offset.total_seconds() / 86400:g}"


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
    app.add_middleware(
        FormSizeLimit,
        max_bytes=config.max_form_bytes,
        uploads=((UPLOAD_PATH, MAX_REQUEST_BYTES), (IMPORT_PATH, MAX_IMPORT_REQUEST_BYTES)),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(LocalOnly)
    # Outermost: every response, the refusals of the guards above included, is written in the browser's chosen language.
    app.add_middleware(LanguageMiddleware)


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
            response = PlainTextResponse(t("error.local_only"), status_code=403)
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

    Every post must say how long it is. Only an upload, on its own path,
    may be longer than an ordinary form (up to the limit uploads gives that
    path); its route also counts the bytes that really arrive.
    """

    def __init__(self, app, max_bytes: int, uploads: tuple[tuple[re.Pattern, int], ...]):
        self.app = app
        self.max_bytes = max_bytes
        self.uploads = uploads

    def limit_for(self, path: str) -> int:
        for pattern, limit in self.uploads:
            if pattern.fullmatch(path):
                return limit

        return self.max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            length = dict(scope["headers"]).get(b"content-length")
            limit = self.limit_for(scope["path"])

            if length is None:
                response = PlainTextResponse(t("error.no_length"), status_code=411)
                await response(scope, receive, send)
                return

            if not length.isdigit() or int(length) > limit:
                response = PlainTextResponse(t("error.too_large"), status_code=413)
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
    return await _form_fields(request, 100)


async def submitted_large_form(request: Request) -> dict[str, str]:
    """Like submitted_form, for the one form with many rows: the preferences editor (owner redesign, plan 3.3), whose
    five dimensions hold at most MAX_ROWS rows each."""
    return await _form_fields(request, 200)


async def _form_fields(request: Request, most: int) -> dict[str, str]:
    form = await request.form(max_files=0, max_fields=most)
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


def checked_large_form(
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(submitted_large_form),
) -> dict[str, str]:
    """checked_form for the preferences editor's longer form."""
    return checked_form(request, user, form)


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
        {"title": title, "message": message, "links": links or [], **place_of(request.url.path)},
        user=user,
        status_code=status_code,
    )


# Where a page of each address belongs in the side bar and among its area's parts (plan 2), for the pages every area
# shares, such as a message that a result is gone: a jobs page's message stays in 岗位机会.
PLACES = (
    ("/workspace", "workspace", "workspace"),
    ("/directions/records", "directions", "records"),
    ("/directions/explorations", "directions", "records"),
    ("/directions/decisions", "directions", "records"),
    ("/directions", "directions", "explore"),
    ("/job-records/history", "jobs", "history"),
    ("/job-records/searches", "jobs", "history"),
    ("/job-records/analyses", "jobs", "history"),
    ("/job-records", "jobs", "my_jobs"),
    ("/discover", "jobs", "discover"),
    ("/job-analysis", "jobs", "discover"),
    ("/data/trash", "data", "trash"),
    ("/data/export", "data", "export"),
    ("/data", "data", "overview"),
    ("/basic-profile", "profile", "profile"),
    ("/job-preferences", "profile", "preferences"),
    ("/imports", "profile", "imports"),
)


def place_of(path: str) -> dict[str, str]:
    """The area and part a shared page shows as current for this address; 我的资料's 经历库 when none is named."""
    for prefix, area, section in PLACES:
        if path == prefix or path.startswith(prefix + "/"):
            return {"area": area, "section": section}

    return {}


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
    along: "AlongChoice | None" = None,
    along_analyses: "AlongChoice | None" = None,
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
            "along": along,
            "along_analyses": along_analyses,
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


# The states the library's list can be narrowed to (owner redesign, image 04): confirmed records, and those still
# waiting for the person (待核对 and 修改后待核对, both drafts). Any other value shows every record.
LIST_STATES = ("confirmed", "review")


def list_state(request: Request) -> str:
    state = request.query_params.get("state", "")

    return state if state in LIST_STATES else ""


def in_state(item, state: str) -> bool:
    if state == "confirmed":
        return item.state == "confirmed"

    if state == "review":
        return item.state != "confirmed"

    return True


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
    state = list_state(request)
    filtering = bool(category or query)

    try:
        with runtime(request).database.transaction() as db:
            if items is None:
                items = list_experiences(db, user.id)

            found = (
                list_experiences(db, user.id, category=category, query=query)
                if filtering
                else items
            )
    except StorageError as error:
        logger.error("reading the record list failed (%s)", type(error.__cause__).__name__)
        items = found = None

    # The state chips count what the words and the category found; the list shows the chosen state of those.
    shown = [item for item in found if in_state(item, state)] if found is not None else None
    state_counts = (
        {"": len(found), **{name: sum(1 for item in found if in_state(item, name)) for name in LIST_STATES}}
        if found is not None
        else {}
    )
    asked = {key: value for key, value in (("category", category), ("q", query), ("state", state)) if value}
    # The same filter without the state: what each state chip links to, with its own state added.
    words_only = {key: value for key, value in (("category", category), ("q", query)) if value}

    return {
        "sidebar_items": shown,
        "sidebar_total": len(items) if items is not None else 0,
        "selected_id": selected_id,
        "focus": focus,
        "filter_category": category,
        "filter_query": query,
        "filter_state": state,
        "state_counts": state_counts,
        "state_links": {
            name: "/experiences" + (f"?{urlencode({**words_only, **({'state': name} if name else {})})}" if words_only or name else "")
            for name in ("",) + LIST_STATES
        },
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
    values = with_body(values, category)

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
            # The picker's own parts (which way, the time as read) are not something typed.
            "unsaved_input": bool(error)
            and any(value.strip() for name, value in values.items() if name not in PICKER_FIELDS or name.endswith(("_year", "_month"))),
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
            # The experience details offered for editing (plan 5.2): what the record says in one text, the older
            # split fields joined in their stable order. Shown only; nothing is written until the user saves. The
            # split fields themselves are shown to read beside it, never as inputs of their own.
            values[BODY] = compose_body(item.content, category)
            values.update({name: "" for name in BODY_SOURCES.get(category, ())})

    if has_period(category):
        values.update(picker_values(values.get("period", "")))

    return values


def has_period(category: str) -> bool:
    """Whether a category's time is offered in the year-and-month picker (owner redesign, plan 3.1)."""
    return any(field.name == "period" for field in CATEGORY_FIELDS.get(category, ()))


def with_body(values: dict[str, str], category: str) -> dict[str, str]:
    """Submitted values as the form shows them again: text a client sent in the older split fields (an older page,
    another tool) is joined into the experience details, so nothing typed is ever out of sight (plan 5.2)."""
    sources = BODY_SOURCES.get(category, ())

    if category not in EXPERIENCE_CATEGORIES or not any(values.get(name) for name in sources):
        return values

    return {**values, BODY: compose_body(values, category), **{name: "" for name in sources}}


def submitted_values(form: dict[str, str], category: str = PROJECT) -> dict[str, str]:
    """Exactly what the user typed, so a failed save loses nothing (the picker's choices included)."""
    values = {name: form.get(name, "") for name in value_names(category)}

    if has_period(category):
        values.update(picker_values(values.get("period", ""), form))

    return values


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

    rt.session_ended(user.token_hash)
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
            title=t("lib.saved_not_out_document" if document else "lib.saved_not_out_draft"),
            message=t("lib.logout_failed_document" if document else "lib.logout_failed_draft"),
            status_code=500,
            user=user,
            links=[(default_path, t("lib.view_saved"))],
        )


def conflict_page(request: Request, user: CurrentUser, current, mine: dict[str, str]) -> Response:
    """Both versions side by side after a save lost a race, with mine kept for saving."""
    fields = list(CATEGORY_FIELDS[current.category])
    # The comparison is of what is stored, never of the joined text an edit form offers (plan 5.2).
    theirs = {field.name: current.content.get(field.name, "") for field in fields}

    if current.category in EXPERIENCE_CATEGORIES:
        theirs[NOTE_FIELD.name] = current.private_note
        # An older split field empty on both sides has nothing to compare.
        split = BODY_SOURCES.get(current.category, ())
        fields = [field for field in fields if field.name not in split or mine.get(field.name) or theirs.get(field.name)]
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
            "theirs": theirs,
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
        title=t("lib.sign_in_again_title"),
        message=t("lib.sign_in_again"),
        status_code=401,
        links=[("/login", t("lib.sign_in_again_link"))],
    )


async def _csrf_rejected(request: Request, exc: CsrfRejected):
    return message_page(
        request,
        title=t("lib.form_expired_title"),
        message=t("lib.form_expired"),
        status_code=403,
        links=[("/workspace", t("common.go_workspace"))],
    )


async def _not_found(request: Request, exc: NotFound):
    return message_page(
        request,
        title=t("lib.not_found_title"),
        message=HTTP_MESSAGES[404],
        status_code=404,
        links=[("/workspace", t("common.go_workspace"))],
    )


async def _not_allowed(request: Request, exc: NotAllowed):
    return message_page(
        request,
        title=t("lib.not_allowed_title"),
        message=exc.reason,
        status_code=409,
        links=[("/workspace", t("common.go_workspace"))],
    )


async def _conflict(request: Request, exc: Conflict):
    item = exc.current

    return message_page(
        request,
        title=t("lib.conflict_title"),
        message=t("lib.conflict"),
        status_code=409,
        links=[(_latest_page(item), t("lib.view_latest"))],
    )


async def _version_conflict(request: Request, exc: VersionConflict):
    return message_page(
        request,
        title=t("lib.version_conflict_title"),
        message=t("lib.version_conflict"),
        status_code=409,
        links=[(f"/experiences/{exc.item_id}/history", t("lib.view_history"))],
    )


async def _attachment_conflict(request: Request, exc: AttachmentConflict):
    return message_page(
        request,
        title=t("lib.attachment_conflict_title"),
        message=t("lib.attachment_conflict"),
        status_code=409,
        links=[(f"/experiences/{exc.item_id}", t("lib.view_this_experience"))],
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
        title=t("lib.storage_title"),
        message=t("lib.storage"),
        status_code=500,
        links=[("/workspace", t("common.go_workspace"))],
    )


async def _http_error(request: Request, exc: StarletteHTTPException):
    return message_page(
        request,
        title=t("lib.http_title"),
        message=HTTP_MESSAGES.get(exc.status_code) or t("lib.http_other"),
        status_code=exc.status_code,
        links=[("/workspace", t("common.go_workspace"))],
    )


# --- pages ----------------------------------------------------------------


@router.get("/")
def home():
    # The workspace is where the product starts (UI bilingual batch, 5.1).
    return see_other("/workspace")


@router.post("/language")
def switch_language(request: Request, form: dict[str, str] = Depends(submitted_form)):
    """Remember this browser's interface language (plan 6.1): only a whitelisted value, from this site's own pages.

    Not an account setting and not a business write: it changes no record, task, result or permission. The choice
    is kept in a cookie for this browser; the page asked for comes back in that language.
    """
    check_same_origin(request)
    chosen = i18n.chosen(form.get("lang"))

    if chosen is None:
        raise HTTPException(status_code=400)

    response = see_other(safe_next(form.get("next", "")) or "/workspace")
    response.set_cookie(
        i18n.LANGUAGE_COOKIE,
        chosen,
        max_age=i18n.LANGUAGE_COOKIE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )

    return response


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

    if previous:
        rt.session_ended(token_hash(previous))

    response = see_other("/workspace")
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
    if tab != "trash":
        sidebar = library_sidebar(request, user, overview=True)
        # Beside the list, the first record found is open (owner redesign, image 04); narrow screens show the list.
        # Opened on its own, it keeps its private note back: that is shown when the record itself is opened.
        shown = sidebar["sidebar_items"] or []
        record = {**record_context(request, user, shown[0].id), "private_shown": False} if shown else {}

        return render(
            request,
            "list.html",
            {
                "tab": "current",
                "notice": NOTICES.get(notice),
                **sidebar,
                **record,
                "selected_id": shown[0].id if shown else None,
            },
            user=user,
        )

    # The recycle bin now belongs to 数据管理 (UI bilingual batch); this older address still shows it, as it was.
    return trash_page(request, user, notice)


def trash_page(request: Request, user: CurrentUser, notice: str = "") -> Response:
    """The recycle bin (数据管理 → 回收站): everything the user deleted that can still be restored, by kind."""
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        trash = trash_view(db, user.id, now)
        # Resume import drafts the user deleted (U2-B3a): restored or removed from here too.
        drafts = binned_drafts(db, user.id, now)
        drafts_waiting = drafts_clearing(db, user.id, now)
        # Direction explorations and decisions deleted on their own (U3-C3a): restored or removed by app.directions.web.
        directions = direction_bin(db, user.id, now)
        # Kept postings and search summaries (U3-D3a): restored or removed by app.discovery.records_web.
        jobs = job_bin(db, user.id, now)

    return render(
        request,
        "trash.html",
        {
            "tab": "trash",
            "trash": trash,
            "drafts": drafts,
            "drafts_waiting": drafts_waiting,
            "directions": directions,
            "jobs": jobs,
            "now": now,
            "notice": NOTICES.get(notice),
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

    # The time as the year-and-month picker says (owner redesign, plan 3.1); the person's own words otherwise.
    form, period_error = with_period(form)

    try:
        if period_error:
            raise ContentError(t("content.fix_and_save"), {"period": period_error})

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
    """The reading page of an experience, with its attachments and a fresh upload form, beside the library's list."""
    record = record_context(request, user, item_id, upload_error=upload_error)

    return render(
        request,
        "detail.html",
        {
            **record,
            "notice": notice,
            **library_sidebar(request, user, selected_id=record["item"].id, focus=True),
        },
        user=user,
        status_code=status_code,
    )


def record_context(request: Request, user: CurrentUser, item_id: str, *, upload_error: str | None = None) -> dict:
    """What the reading of one experience shows: the record, its attachments and a fresh upload form."""
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item = active_item(db, user.id, item_id)
        attachments = kept_attachments(db, user.id, item)
        binned = binned_attachments(db, user.id, item, now)
        taken = places_taken(db, item.id)

    return {
        "item": item,
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
    }


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
    # The time as the year-and-month picker says (owner redesign, plan 3.1); the person's own words otherwise.
    form, period_error = with_period(form)

    try:
        with rt.database.transaction() as db:
            category = active_item(db, user.id, item_id).category

            if period_error:
                raise ContentError(t("content.fix_and_save"), {"period": period_error})

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
            title=t("lib.confirm_failed_title"),
            message=t("lib.confirm_failed"),
            status_code=409,
            user=user,
            links=[(f"/experiences/{item_id}", t("lib.view_latest"))],
        )

    return see_other(f"/experiences/{item_id}?notice=confirmed")


@router.get("/experiences/{item_id}/history")
def history_page(
    item_id: str,
    request: Request,
    notice: str = "",
    tab: str = "versions",
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
        # The explorations the user deleted along with a version come back with it (U3-C3b).
        along = {
            version.id: len(explorations_along(db, user.id, version.deletion_id, now))
            for version in history.versions
            if version.deleted_at is not None
        }
        # And the kept analyses (U3-D3b).
        analyses_with = {
            version.id: len(analyses_along(db, user.id, version.deletion_id, now))
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
            "along": along,
            "analyses_with": analyses_with,
            # 历史版本 or 操作记录 (owner redesign, plan 3.1): two tabs, chosen in the address.
            "history_tab": "events" if tab == "events" else "versions",
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
    return _version_delete_confirm(request, user, item_id, version_id)


def _version_delete_confirm(request, user, item_id, version_id, error=None, status_code=200):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        item, version = deletable_version(db, user.id, item_id, version_id, ALL_KINDS)
        state = version_state(db, version)
        references = list_version_references(db, user.id, item.id, version.id, now, ALL_KINDS)
        resting = resting_explorations(db, user.id, item.id, version.id)
        resting_kept = resting_analyses(db, user.id, item.id, version.id)

    return confirm_page(
        request,
        user,
        title=t("lib.delete_version_title", version=version_label(version)),
        points=[
            t("lib.delete_version_only", owner=_owner_noun(item)),
            t("lib.delete_version_hidden"),
            t("lib.delete_version_restore", days=RESTORE_WINDOW.days, until=format_local(now + RESTORE_WINDOW)),
            t("lib.delete_version_downloads"),
            *_version_direction_points(references),
        ],
        action=f"/experiences/{item.id}/versions/{version.id}/delete",
        button=t("lib.delete_version_button"),
        cancel=f"/experiences/{item.id}/history",
        hidden={"expected_state": state},
        section=section_of(item),
        error=error,
        status_code=status_code,
        along=_along_choice(resting, t("lib.this_version")),
        along_analyses=_analyses_choice(resting_kept, t("lib.this_version")),
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
    chosen = _chosen_along(form)
    chosen_analyses = _chosen_along(form, ANALYSES_VALUE, ANALYSES_TOGGLE)

    try:
        with rt.database.transaction() as db:
            delete_version_and_chosen(
                db, user.id, item_id, version_id, _expected_state(form), rt.now(), ALL_KINDS, chosen, chosen_analyses
            )
    except AlongChanged:
        return _version_delete_confirm(request, user, item_id, version_id, error=ALONG_CHANGED, status_code=409)

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
        references = list_version_references(db, user.id, item.id, version.id, rt.now(), ALL_KINDS)
        along = len(explorations_along(db, user.id, version.deletion_id, rt.now()))
        kept_along = len(analyses_along(db, user.id, version.deletion_id, rt.now()))

    return _version_purge_confirm(request, user, item, version, state, references, along, kept_along)


# Deleting along the explorations resting on what is deleted (U3-C3b): offered only when some do, never ticked,
# and taken only as the page listed them (lifecycle.check_chosen_along).
ALONG_CHANGED = lazy("lib.along_changed")
# The kept analyses (U3-D3b) are offered apart from the explorations, under their own names.
ANALYSES_VALUE, ANALYSES_TOGGLE = "analyses_along", "delete_analyses_along"


@dataclass(frozen=True)
class AlongChoice:
    """What a deletion page lists to delete along (explorations, or kept analyses), frozen as "<id>.<revision>"
    pieces, and the choice to delete them along, posted under value_name and toggle_name."""

    legend: str
    lines: tuple[str, ...]
    value: str
    label: str
    value_name: str = "along"
    toggle_name: str = "delete_along"


def _along_choice(explorations, what: str) -> AlongChoice | None:
    if not explorations:
        return None

    return AlongChoice(
        legend=t("lib.along_explorations_legend", what=what),
        lines=tuple(
            t(
                "lib.along_exploration_line",
                names=i18n.list_join(t("lib.quoted", text=direction["name"]) for direction in exploration.content["directions"]),
                at=format_short(exploration.saved_at),
            )
            for exploration in explorations
        ),
        value=",".join(record_ref(exploration.id, exploration.revision) for exploration in explorations),
        label=t("lib.along_explorations_label", n=len(explorations), what=what),
    )


def _analyses_choice(analyses, what: str) -> AlongChoice | None:
    """The kept analyses resting on what is deleted (U3-D3b): offered only when some do, never ticked."""
    if not analyses:
        return None

    return AlongChoice(
        legend=t("lib.along_analyses_legend", what=what),
        lines=tuple(
            t(
                "lib.along_analysis_line",
                title=analysis.facts.get("title", ""),
                company=analysis.facts.get("company", ""),
                at=format_short(analysis.saved_at),
            )
            for analysis in analyses
        ),
        value=",".join(record_ref(analysis.id, analysis.revision) for analysis in analyses),
        label=t("lib.along_analyses_label", n=len(analyses), what=what),
        value_name=ANALYSES_VALUE,
        toggle_name=ANALYSES_TOGGLE,
    )


def _purged_along_points(count: int, analyses: int = 0) -> list[str]:
    """The lines a purge page adds for the explorations (U3-C3b) and the kept analyses (U3-D3b) the user deleted
    along, which go now too."""
    return [t("lib.purged_along_explorations", n=count)] * bool(count) + [
        t("lib.purged_along_analyses", n=analyses)
    ] * bool(analyses)


def _chosen_along(form: dict[str, str], value_name: str = "along", toggle_name: str = "delete_along") -> tuple[tuple[str, int], ...]:
    """What the user chose to delete along, as the page froze it; none unless the choice is ticked."""
    choice = form.get(toggle_name)

    if choice is None:
        return ()

    chosen = read_record_refs(form.get(value_name, "").split(",")) if choice == "yes" else None

    if not chosen:
        raise HTTPException(status_code=400)

    return chosen


def _version_direction_points(references, *, purge: bool = False) -> list[str]:
    """The line a version's deletion pages add when direction records rest on it (U3-C1); none otherwise."""
    notes = (direction_note(references, purge=purge, version=True), analysis_note(references, purge=purge, version=True))

    return [note for note in notes if note]


def _version_purge_confirm(request, user, item, version, state, references, along=0, kept_along=0, error=None, status_code=200):
    return confirm_page(
        request,
        user,
        title=t("lib.purge_version_title", version=version_label(version)),
        points=[
            t("lib.purge_cannot_undo"),
            t("lib.purge_version_only", owner=_owner_noun(item)),
            *_purged_along_points(along, kept_along),
            t("lib.purge_version_cleanup"),
            t("lib.purge_downloads"),
            *_version_direction_points(references, purge=True),
        ],
        action=f"/experiences/{item.id}/versions/{version.id}/purge",
        button=t("lib.purge_version_button"),
        cancel=f"/experiences/{item.id}/history",
        hidden={"expected_state": state},
        acknowledge=t("lib.purge_acknowledge"),
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
            references = list_version_references(db, user.id, item.id, version.id, rt.now(), ALL_KINDS)
            along = len(explorations_along(db, user.id, version.deletion_id, rt.now()))
            kept_along = len(analyses_along(db, user.id, version.deletion_id, rt.now()))

        return _version_purge_confirm(
            request,
            user,
            item,
            version,
            expected,
            references,
            along,
            kept_along,
            error=t("lib.purge_tick_first"),
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
DOCUMENT_UNTOUCHED = MessageMap({PROFILE: "lib.profile_untouched", PREFERENCES: "lib.preferences_untouched"})


def _owner_noun(item) -> str:
    """Whose current content a history version belongs to, for its delete pages."""
    return KIND_NOUNS[item.category] if item.category in DOCUMENT_KINDS else t("lib.this_experience")


def _delete_title(item) -> str:
    if item.category in DOCUMENT_KINDS:
        return t("lib.delete_document_title", what=CATEGORY_NAMES[item.category])

    return t("lib.delete_experience_title", title=display_title(item.content, item.category))


@router.get("/experiences/{item_id}/delete")
def delete_page(item_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    return _delete_confirm(request, user, item_id)


def _delete_confirm(request, user, item_id, error=None, status_code=200):
    rt = runtime(request)

    with rt.database.transaction() as db:
        scope = deletion_scope(db, user.id, item_id, rt.now(), ALL_KINDS)
        references = list_references(db, user.id, item_id, rt.now(), ALL_KINDS)
        resting = resting_explorations(db, user.id, item_id)
        resting_kept = resting_analyses(db, user.id, item_id)

    item = scope.item
    noun = KIND_NOUNS[item.category]

    if item.category in DOCUMENT_KINDS:
        points = [t("lib.delete_document_scope", noun=noun, n=scope.kept_versions)]
    else:
        points = [t("lib.delete_experience_scope", noun=noun, n=scope.kept_versions)]

    if scope.versions_already_in_bin:
        points.append(t("lib.delete_versions_already", n=scope.versions_already_in_bin))

    if scope.kept_attachments:
        points.append(t("lib.delete_attachments_along", n=scope.kept_attachments))

    if scope.attachments_already_in_bin:
        points.append(t("lib.delete_attachments_already", n=scope.attachments_already_in_bin))

    if item.category in DOCUMENT_KINDS:
        points += [t("lib.delete_document_stops"), DOCUMENT_UNTOUCHED[item.category]]
    else:
        points.append(t("lib.delete_experience_stops"))

    points += [
        t("lib.delete_restore_window", days=RESTORE_WINDOW.days, until=format_local(scope.restore_deadline)),
        t("lib.delete_downloads"),
        reference_note(references),
    ]

    return confirm_page(
        request,
        user,
        title=_delete_title(item),
        points=points,
        action=f"/experiences/{item.id}/delete",
        button=t("lib.move_to_trash"),
        cancel=record_page(item),
        hidden={"expected_revision": str(item.revision)},
        section=section_of(item),
        error=error,
        status_code=status_code,
        along=_along_choice(resting, KIND_NOUNS[item.category]),
        along_analyses=_analyses_choice(resting_kept, KIND_NOUNS[item.category]),
    )


@router.post("/experiences/{item_id}/delete")
def delete_item(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    chosen = _chosen_along(form)
    chosen_analyses = _chosen_along(form, ANALYSES_VALUE, ANALYSES_TOGGLE)

    try:
        with rt.database.transaction() as db:
            item = delete_experience_and_chosen(
                db, user.id, item_id, _expected_revision(form), rt.now(), ALL_KINDS, chosen, chosen_analyses
            )
            category = item.category
    except AlongChanged:
        return _delete_confirm(request, user, item_id, error=ALONG_CHANGED, status_code=409)

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
        along = explorations_purged_with(db, user.id, item, rt.now())
        kept_along = analyses_purged_with(db, user.id, item, rt.now())

    return _purge_confirm(request, user, item, attachments, references, along, kept_along)


def _purge_confirm(request, user, item, attachments, references, along=0, kept_along=0, error=None, status_code=200):
    if item.category in DOCUMENT_KINDS:
        what = t("lib.purge_document_scope", noun=KIND_NOUNS[item.category])
    elif attachments:
        what = t("lib.purge_experience_scope_files", noun=KIND_NOUNS[item.category], n=attachments)
    else:
        what = t("lib.purge_experience_scope", noun=KIND_NOUNS[item.category])

    return confirm_page(
        request,
        user,
        title=t("lib.purge_title", title=_delete_title(item)),
        points=[
            t("lib.purge_cannot_undo"),
            what,
            *_purged_along_points(along, kept_along),
            t("lib.purge_cleanup"),
            t("lib.purge_downloads"),
            reference_note(references, purge=True),
        ],
        action=f"/experiences/{item.id}/purge",
        button=t("common.delete_permanently"),
        cancel="/data/trash",
        hidden={"expected_revision": str(item.revision)},
        acknowledge=t("lib.purge_acknowledge"),
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
            along = explorations_purged_with(db, user.id, item, rt.now())
            kept_along = analyses_purged_with(db, user.id, item, rt.now())

        return _purge_confirm(
            request,
            user,
            item,
            attachments,
            references,
            along,
            kept_along,
            error=t("lib.purge_tick_first"),
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
            title=t("lib.export_failed_title"),
            message=t("lib.export_failed"),
            status_code=500,
            user=user,
            links=[("/data/export", t("nav.export"))],
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
            detail_page, request, user, item_id, upload_error=str(UPLOAD_PROBLEMS[UploadOutcome.FULL][1]), status_code=409
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
            title=t("lib.upload_not_done_title"),
            message=t("lib.upload_parent_gone"),
            status_code=404,
            user=user,
            links=[("/experiences", t("lib.back_to_library"))],
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
            title=t("lib.download_not_done_title"),
            message=str(FILE_PROBLEMS.get(type(error), UNREADABLE_FILE)),
            status_code=500,
            user=user,
            links=[(f"/experiences/{item_id}", t("lib.back_to_experience"))],
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
        title=t("lib.delete_attachment_title", name=attachment.filename),
        points=[
            t("lib.delete_attachment_only"),
            t("lib.delete_attachment_hidden"),
            t("lib.delete_attachment_restore", days=RESTORE_WINDOW.days, until=format_local(now + RESTORE_WINDOW)),
            t("lib.delete_attachment_downloads"),
        ],
        action=f"/experiences/{item.id}/attachments/{attachment.id}/delete",
        button=t("lib.delete_attachment_button"),
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
        title=t("lib.purge_attachment_title", name=attachment.filename),
        points=[
            t("lib.purge_cannot_undo"),
            t("lib.purge_attachment_only"),
            t("lib.purge_attachment_cleanup"),
            t("lib.purge_attachment_downloads"),
        ],
        action=f"/experiences/{item.id}/attachments/{attachment.id}/purge",
        button=t("lib.purge_attachment_button"),
        cancel=f"/experiences/{item.id}",
        hidden={"expected_revision": str(attachment.revision)},
        acknowledge=t("lib.purge_acknowledge"),
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
            error=t("lib.purge_tick_first"),
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


def _preference_error_step(field_errors: dict[str, str]) -> str:
    """The editor's step ("1"-"3") that holds the first of these errors, in the order the steps come, or ""."""
    for number, (_, keys) in enumerate(STEPS, start=1):
        names = {f"{key}_{stance}" for key in keys for stance in ("ideal", "ok", "no")}

        if number == 2:
            names.add("less_content")

        if number == 3:
            names.add("must_haves")

        if names & set(field_errors):
            return str(number)

    return "1" if field_errors else ""


def document_form(
    request: Request,
    user: CurrentUser,
    kind: str,
    *,
    values: dict[str, str],
    posted: dict[str, str] | None = None,
    item_id: str | None = None,
    expected_revision: int | None = None,
    error: str | None = None,
    field_errors: dict[str, str] | None = None,
    next_path: str = "",
    status_code: int = 200,
    stored: dict | None = None,
) -> Response:
    """The form of the profile or preferences. Without item_id it starts a new one.

    The preferences are edited in three steps, each dimension as rows (plan 3.3); after a failed save the rows come
    back as posted, so nothing typed is lost."""
    base = DOCUMENT_PAGES[kind]
    context = {
        "kind": kind,
        "item_id": item_id,
        "values": values,
        "expected_revision": expected_revision,
        "error": error,
        "field_errors": field_errors or {},
        "next_path": safe_next(next_path) or "",
        # A page shown again after a failed save may hold text that exists
        # nowhere else yet; the leave guard must treat it as unsaved.
        "unsaved_input": bool(error) and any(value.strip() for value in (posted or values).values()),
        "action": f"{base}/{item_id}/save" if item_id else base,
        "section": kind,
    }

    if kind == PREFERENCES:
        editing = posted is not None and posted.get("pref_rows") == "1"
        step = request.query_params.get("step", "")
        # A failed save opens the step of its first error (plan 3.3), worked out here: the field is shown, focused
        # (preferences.js) and named by its error; everything posted is shown again as it was.
        failed_step = _preference_error_step(field_errors or {})
        context.update(
            rows=posted_rows(posted) if editing else rows_of({**values, ROWS_KEY: rows_kept(stored or {})}),
            must=requirement_parts(values.get("must_haves", ""), posted if editing else None),
            step=failed_step or (step if step in ("1", "2", "3") else "1"),
            error_step=failed_step,
            less_content=(posted or values).get("less_content", ""),
        )

    return render(
        request,
        "preferences_form.html" if kind == PREFERENCES else "document_form.html",
        context,
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
        stored=item.content,
    )


def document_create(request: Request, user: CurrentUser, form: dict[str, str], kind: str) -> Response:
    rt = runtime(request)
    # The preferences editor posts rows; they are folded into the stored fields first (plan 3.3).
    posted = form

    try:
        if kind == PREFERENCES:
            form = fold_rows(form)

        entry = parse_experience_form(form, kind)

        if kind == PREFERENCES:
            entry = with_rows(entry, form)

        with rt.database.transaction() as db:
            create_document(db, user.id, entry, rt.now())
    except ContentError as error:
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            posted=posted,
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
            posted=posted,
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
    # The preferences editor posts rows; they are folded into the stored fields first (plan 3.3).
    posted = form

    try:
        with rt.database.transaction() as db:
            # Someone else's or a missing record is not found, whatever the form holds.
            active_item(db, user.id, item_id, kinds=(kind,))

            if kind == PREFERENCES:
                form = fold_rows(form)

            entry = parse_experience_form(form, kind)

            if kind == PREFERENCES:
                entry = with_rows(entry, form)

            _, changed = save_draft(db, user.id, item_id, expected, entry, rt.now())
    except ContentError as error:
        return document_form(
            request,
            user,
            kind,
            values=submitted_values(form, kind),
            posted=posted,
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
            posted=posted,
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
            posted=posted,
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
    form: dict[str, str] = Depends(checked_large_form),
):
    return document_create(request, user, form, PREFERENCES)


@router.post("/job-preferences/{item_id}/save")
def preferences_save(
    item_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_large_form),
):
    return document_save(request, user, form, PREFERENCES, item_id)
