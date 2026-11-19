"""数据管理 / Data management: an overview, the recycle bin and the export (UI bilingual batch, plan 2 and 5.6).

The overview only counts what the user has here and links to it: it changes nothing and sends nothing. The recycle bin
is the library's own page (app.library.web.trash_page), under this area.

The export is built once and its result shown before anything is downloaded (5.6): complete, it can be downloaded
whole; with files left out, the page lists each with why and where to find it, from that very build, and the user
chooses to download what is there. The package waits for the download under a one-time token bound to this user and
this session for at most ten minutes, in memory, as an open file without a name (app.library.export.write_export): no
lasting export record, and the token holds no path, text or personal detail. Logging out, the time running out, a
deletion or purge in the library since the build, cancelling or downloading it frees it. A download already begun or
done cannot be called back. The direct /export/all.zip stays as it was, for the pages and links of before.

Deletions spoil a package as they are written (Codex review F1): every flush that deletes something of a user's (a row
removed, a deleted_at newly set, or a deletion or purge event) spoils that user's held packages and closes them, before
its transaction commits; so a result page or a download that comes after a deletion never hands out the package built
before it, whatever moment of its checks the deletion fell between. A build takes a ticket before it reads anything: a
deletion of the user's, or the end of the session, after the ticket makes the finished build refused and closed, never
held. The library's own count of deletion events is checked as well, for a deletion this process did not write.
"""
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, inspect, or_, select

from app.i18n import t
from app.library.errors import FileStoreError
from app.library.export import INCLUDED, STATUS_TEXT, collect_export, write_export
from app.library.files import read_chunks
from app.library.identity import CurrentUser
from app.library.models import (
    AuditEvent,
    DirectionDecision,
    DirectionExploration,
    ExperienceItem,
    JobAnalysis,
    JobPosting,
    JobSearch,
)
from app.library.web import NOTICES, checked_form, current_user, message_page, render, runtime, see_other, trash_page

logger = logging.getLogger("roleradar.data")
router = APIRouter()

TOKEN_TTL_SECONDS = 10 * 60
# How often the app frees what is past its time while it runs (Codex R1 review): packages held past TOKEN_TTL_SECONDS
# are closed within this, whether or not anyone asks for an export again; the clean-up pass frees them as well.
SWEEP_SECONDS = 30.0
# How long the end of a session is remembered, so that a build it began never fills a hold after it: far longer than
# any build takes.
ENDED_MEMORY_SECONDS = 24 * 3600


@dataclass
class _Held:
    owner_id: str
    session: str
    package: object
    built: float
    # The newest deletion or purge event of the user's when the package was built: a newer one spoils it.
    deletions: int


@dataclass(frozen=True)
class BuildTicket:
    """Taken before a build reads anything: the session it is for and the user's deletion count at that moment."""

    owner_id: str
    session: str
    generation: int


class ExportHolds:
    """Built packages waiting for their one download: one per session, by a random token; nothing is written.

    A deletion of the user's spoils their packages at once (spoil), and a build taken before it is refused when it
    finishes (put); so is a build whose session ended meanwhile (end_session). A refused or dropped package is closed
    there and then. Tokens dropped by a deletion are remembered for a while, so their pages can say why.
    """

    def __init__(self, clock=time.monotonic, ttl: float = TOKEN_TTL_SECONDS):
        self._clock = clock
        self._ttl = ttl
        # The app's own sweep of these packages while it runs (web_main._sweep_loop), every sweep_seconds.
        self.sweep_seconds = SWEEP_SECONDS
        self.sweeper = None
        self._lock = threading.Lock()
        self._held: dict[str, _Held] = {}
        # Deletions seen per user, counted up by spoil; a ticket keeps the count of its moment.
        self._generations: dict[str, int] = {}
        self._ended: dict[str, float] = {}
        self._spoiled: dict[str, tuple[str, str, float]] = {}

    def begin(self, owner_id: str, session: str) -> BuildTicket | None:
        """A ticket for a build of this session, or None when the session has already ended."""
        with self._lock:
            self._sweep()

            if session in self._ended:
                return None

            return BuildTicket(owner_id, session, self._generations.get(owner_id, 0))

    def put(self, ticket: BuildTicket, package, deletions: int) -> str | None:
        """Hold a package built under ticket for its one download; None, with the package closed, when the session
        ended or the user deleted or purged something after the ticket was taken."""
        with self._lock:
            self._sweep()

            if ticket.session in self._ended or self._generations.get(ticket.owner_id, 0) != ticket.generation:
                _close(package)

                return None

            for token, held in list(self._held.items()):
                if held.session == ticket.session:
                    self._drop(token)

            token = secrets.token_urlsafe(24)
            self._held[token] = _Held(ticket.owner_id, ticket.session, package, self._clock(), deletions)

            return token

    def peek(self, token: str, owner_id: str, session: str) -> _Held | None:
        with self._lock:
            self._sweep()
            held = self._held.get(token)

            return held if held is not None and (held.owner_id, held.session) == (owner_id, session) else None

    def take(self, token: str, owner_id: str, session: str) -> _Held | None:
        """The package for its one download: it leaves the store, and the caller closes it."""
        with self._lock:
            self._sweep()
            held = self._held.get(token)

            if held is None or (held.owner_id, held.session) != (owner_id, session):
                return None

            del self._held[token]

            return held

    def drop(self, token: str, owner_id: str, session: str) -> bool:
        with self._lock:
            held = self._held.get(token)

            if held is None or (held.owner_id, held.session) != (owner_id, session):
                return False

            self._drop(token)

            return True

    def spoil(self, owner_id: str, token: str | None = None) -> None:
        """A deletion or purge of the user's: their held packages (or the one token) are closed and dropped, and any
        build begun before now is refused when it finishes."""
        with self._lock:
            if token is None:
                self._generations[owner_id] = self._generations.get(owner_id, 0) + 1

            for held_token, held in list(self._held.items()):
                if held.owner_id == owner_id and token in (None, held_token):
                    self._spoiled[held_token] = (held.owner_id, held.session, self._clock())
                    self._drop(held_token)

    def was_spoiled(self, token: str, owner_id: str, session: str) -> bool:
        """Whether this session's token was dropped because of a deletion (so its page says so, not just "gone")."""
        with self._lock:
            entry = self._spoiled.get(token)

            return entry is not None and entry[:2] == (owner_id, session)

    def minutes_left(self, held: _Held) -> int:
        return max(0, int((held.built + self._ttl - self._clock()) // 60) + 1)

    def end_session(self, session: str) -> None:
        with self._lock:
            self._ended[session] = self._clock()

            for token, held in list(self._held.items()):
                if held.session == session:
                    self._drop(token)

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def shutdown(self) -> None:
        with self._lock:
            for token in list(self._held):
                self._drop(token)

    def __len__(self) -> int:
        return len(self._held)

    def _sweep(self) -> None:
        now = self._clock()

        for token, held in list(self._held.items()):
            if now - held.built >= self._ttl:
                self._drop(token)

        for token, (_, _, when) in list(self._spoiled.items()):
            if now - when >= self._ttl:
                del self._spoiled[token]

        for session, when in list(self._ended.items()):
            if now - when >= ENDED_MEMORY_SECONDS:
                del self._ended[session]

    def _drop(self, token: str) -> None:
        _close(self._held.pop(token).package)


def _close(package) -> None:
    try:
        package.handle.close()
    except OSError:
        pass


def deleting_owners(session) -> set[str]:
    """The users whose data a flush deletes: a row of theirs removed, a deleted_at of theirs newly set, or a deletion
    or purge event recorded for them. Read in the flush, before the transaction commits."""
    owners = set()

    # Values are read from what the rows hold already (state.dict), never loaded: nothing here touches the database.
    for row in session.deleted:
        if owner := inspect(row).dict.get("owner_id"):
            owners.add(owner)

    for row in session.dirty:
        state = inspect(row)
        owner = state.dict.get("owner_id")

        if owner and "deleted_at" in state.mapper.attrs:
            if any(value is not None for value in state.attrs.deleted_at.history.added):
                owners.add(owner)

    for row in session.new:
        if isinstance(row, AuditEvent) and _is_deletion(inspect(row).dict.get("action")):
            owners.add(inspect(row).dict.get("owner_id"))

    owners.discard(None)

    return owners


def _is_deletion(action: str) -> bool:
    return "delet" in (action or "") or "purg" in (action or "")


def install_data_management(app: FastAPI) -> ExportHolds:
    holds = ExportHolds()
    app.state.export_holds = holds
    # Logging out, signing in as someone else or the login's time running out (the clean-up removing it) frees what
    # the session built and refuses its late builds.
    app.state.library.session_end_listeners.append(holds.end_session)
    # Packages past their time are freed by the app's clean-up pass too, and by their own sweep while the app runs
    # (web_main): never left open until the next export request or the app's shutdown (Codex R1 review).
    app.state.library.cleanup_steps.append(holds.sweep)

    # A deletion spoils the user's packages as it is written (before its transaction commits).
    def spoil_on_delete(session) -> None:
        for owner in deleting_owners(session):
            holds.spoil(owner)

    app.state.library.database.on_flush(spoil_on_delete)
    app.include_router(router)

    return holds


def holds_of(request: Request) -> ExportHolds:
    return request.app.state.export_holds


def _deletions(db, owner_id: str) -> int:
    """The newest deletion or purge event of the user's, as one number (0 for none)."""
    newest = db.scalar(
        select(func.max(AuditEvent.id)).where(
            AuditEvent.owner_id == owner_id,
            or_(AuditEvent.action.like("%delet%"), AuditEvent.action.like("%purg%")),
        )
    )

    return newest or 0


# --- the overview -------------------------------------------------------------------------------------------------


@router.get("/data")
def overview(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    def count(db, model, *where):
        return db.scalar(select(func.count()).select_from(model).where(model.owner_id == user.id, *where)) or 0

    with rt.database.transaction() as db:
        counts = {
            "experiences": count(db, ExperienceItem, ExperienceItem.deleted_at.is_(None), ExperienceItem.category.not_in(("profile", "preferences"))),
            "explorations": count(db, DirectionExploration, DirectionExploration.deleted_at.is_(None)),
            "decisions": count(db, DirectionDecision, DirectionDecision.deleted_at.is_(None)),
            "postings": count(db, JobPosting, JobPosting.deleted_at.is_(None)),
            "analyses": count(db, JobAnalysis, JobAnalysis.deleted_at.is_(None)),
            "searches": count(db, JobSearch, JobSearch.deleted_at.is_(None)),
            "binned": sum(
                count(db, model, model.deleted_at.is_not(None))
                for model in (ExperienceItem, DirectionExploration, DirectionDecision, JobPosting, JobAnalysis, JobSearch)
            ),
        }

    # What a form saved on the way here, from its leave dialog (UI batch 1: every page a form links to says it).
    context = {"area": "data", "section": "overview", "counts": counts, "notice": NOTICES.get(notice)}

    return render(request, "data_overview.html", context, user=user)


@router.get("/data/trash")
def trash(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    return trash_page(request, user, notice)


# --- the export (5.6) ---------------------------------------------------------------------------------------------


@router.get("/data/export")
def export_page(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    said = t("data.export.cancelled") if notice == "cancelled" else NOTICES.get(notice)

    return render(request, "data_export.html", {"area": "data", "section": "export", "notice": said}, user=user)


@router.post("/data/export/build")
def build(request: Request, user: CurrentUser = Depends(current_user), form: dict[str, str] = Depends(checked_form)):
    """Build the package once, in the interface language of this request (plan 6.4), and hold it for its result."""
    rt = runtime(request)
    now = rt.now()
    holds = holds_of(request)
    # Before anything is read: a deletion or the end of the session after this makes the finished build refused.
    ticket = holds.begin(user.id, user.token_hash)

    if ticket is None:
        return _not_kept(request, user)

    with rt.database.transaction() as db:
        content = collect_export(db, user.id, now)
        deletions = _deletions(db, user.id)

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

    token = holds.put(ticket, package, deletions)

    if token is None:
        return _not_kept(request, user)

    return see_other(f"/data/export/result/{token}")


def _not_kept(request: Request, user: CurrentUser) -> Response:
    """A build that finished after a deletion of the user's or after the session ended: closed, never held."""
    return message_page(
        request,
        title=t("data.export.not_kept_title"),
        message=t("data.export.not_kept"),
        status_code=409,
        user=user,
        links=[("/data/export", t("data.export.build_again"))],
    )


def _gone(request: Request, user: CurrentUser, token: str = "") -> Response:
    """A token that holds nothing (any more): spoiled by a deletion is said as such, anything else as gone."""
    if token and holds_of(request).was_spoiled(token, user.id, user.token_hash):
        return _spoiled(request, user)

    return message_page(
        request,
        title=t("data.export.gone_title"),
        message=t("data.export.gone"),
        status_code=404,
        user=user,
        links=[("/data/export", t("data.export.build_again"))],
    )


def _spoiled(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title=t("data.export.spoiled_title"),
        message=t("data.export.spoiled"),
        status_code=409,
        user=user,
        links=[("/data/export", t("data.export.build_again"))],
    )


def _current(request: Request, user: CurrentUser, token: str) -> "_Held | None":
    """The held package of this token, after the library's own count of deletions agrees it is current (a deletion this
    process did not write); one it does not is spoiled here."""
    holds = holds_of(request)
    held = holds.peek(token, user.id, user.token_hash)

    if held is None:
        return None

    with runtime(request).database.transaction() as db:
        current = _deletions(db, user.id) == held.deletions

    if not current:
        holds.spoil(user.id, token)

        return None

    return held


@router.get("/data/export/result/{token}")
def result(token: str, request: Request, user: CurrentUser = Depends(current_user)):
    holds = holds_of(request)
    held = _current(request, user, token)

    if held is None:
        return _gone(request, user, token)

    package = held.package
    missing = [{**entry, "why": STATUS_TEXT.get(entry["status"], entry["status"])} for entry in package.left_out if entry["status"] != INCLUDED]

    return render(
        request,
        "data_export_result.html",
        {
            "area": "data",
            "section": "export",
            "token": token,
            "complete": package.complete,
            "missing": missing,
            "size": package.size,
            "filename": package.filename,
            "minutes_left": holds.minutes_left(held),
        },
        user=user,
    )


@router.get("/data/export/download/{token}")
def download(token: str, request: Request, user: CurrentUser = Depends(current_user)):
    """The one download of a built package: checked again first (this user and session, in time, and no deletion or
    purge in the library since the build); the token is used up as the download begins."""
    holds = holds_of(request)

    if _current(request, user, token) is None:
        return _gone(request, user, token)

    # Taken under the store's lock: a deletion written since the check above has already dropped it (spoil), so what
    # is taken here is a package no deletion came after.
    held = holds.take(token, user.id, user.token_hash)

    if held is None:
        return _gone(request, user, token)

    package = held.package

    return StreamingResponse(
        read_chunks(package.handle),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{package.filename}"',
            "Content-Length": str(package.size),
        },
    )


@router.post("/data/export/cancel/{token}")
def cancel(token: str, request: Request, user: CurrentUser = Depends(current_user), form: dict[str, str] = Depends(checked_form)):
    holds_of(request).drop(token, user.id, user.token_hash)

    return see_other("/data/export?notice=cancelled")
