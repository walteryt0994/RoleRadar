"""工作台 / Workspace: where a user picks up what they were doing and starts something new (UI bilingual batch, 5.1).

A read-only page over what already exists; it keeps no store of its own and copies nothing:

- 继续处理 (Continue): the user's saved resume-import drafts, newest first, and what this session still has running
  or holds for a while (an analysis, a search, an exploration, a posting read or pasted to be added). A task still
  running offers its progress, one finished its result; a task waiting for its review page, or cancelled, is left
  out. Nothing here is extended, started, claimed or sent: the stores' ``listed`` methods only read.
- 开始新任务 (Start a task): the four journeys' first pages.
- 最近保存 (Recently saved): at most RECENT records kept for good, newest saved first, with their kind: experiences,
  saved explorations, postings in 我的岗位, saved analyses and saved search summaries. A temporary recommendation, a
  record in the recycle bin and anything of another user's are never listed.

Rendering this page sends nothing to any source or model and writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, FastAPI, Request

from app.directions import records as direction_records
from app.discovery import records as job_records
from app.discovery.supplied import LinkContent
from app.library.job_words import source_label
from app.i18n import list_join, t
from app.library import imports, job_recommendations
from app.library.content import display_title
from app.library.identity import CurrentUser
from app.library.service import list_experiences
from app.library.web import NOTICES, current_user, render, runtime

RECENT = 5

router = APIRouter()


@dataclass(frozen=True)
class Continuing:
    """One thing to pick up again: what it is, when it was saved or until when it is held, and where to go."""

    kind: str
    title: str
    href: str
    # "draft", "running" or "held": a saved draft, a task still running, a result held for a while.
    state: str
    saved_at: datetime | None = None
    until: datetime | None = None
    detail: str = ""


@dataclass(frozen=True)
class Saved:
    title: str
    kind: str
    href: str
    saved_at: datetime


def install_workspace(app: FastAPI) -> None:
    app.include_router(router)


@router.get("/workspace")
def workspace(request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    with rt.database.transaction() as db:
        drafts = _drafts(db, user, now)
        recent = _recent(db, user, now)

    context = {
        "area": "workspace",
        "section": "workspace",
        # What a form saved on the way here, from its leave dialog (UI batch 1: every page a form links to says it).
        "notice": NOTICES.get(notice),
        "continuing": drafts + _held(request, user, now),
        "recent": recent,
        "now": now,
    }

    return render(request, "home.html", context, user=user)


def _drafts(db, user: CurrentUser, now: datetime) -> list[Continuing]:
    """The saved import drafts still in use (never one in the recycle bin), newest saved first."""
    found = []

    for row in imports.open_imports(db, user.id, now):
        draft = imports.draft_of(db, row.id)

        if draft is None or draft.deleted_at is not None:
            continue

        found.append(
            Continuing(
                kind="import",
                title=t("ws.continue_import"),
                href=f"/imports/{row.id}",
                state="draft",
                saved_at=draft.saved_at,
                detail=row.filename or "",
            )
        )

    return sorted(found, key=lambda entry: entry.saved_at, reverse=True)


def _held(request: Request, user: CurrentUser, now: datetime) -> list[Continuing]:
    """What this session still has running or holds for a while, in each page's own store (read only)."""
    found: list[Continuing] = []
    state = request.app.state

    for task, seconds in state.discovery.store.listed(user.id, user.token_hash):
        if task.stage.value not in ("running", "done"):
            continue

        scope = task.confirmation.scope
        wanted = scope.targets or scope.direction.values
        title = t("ws.search_results", what=list_join(wanted)) if wanted else t("ws.search_results_any")
        found.append(_task_entry("search", title, f"/discover/tasks/{task.id}", task.stage.value, seconds, now))

    for task, seconds in state.job_analysis.store.listed(user.id, user.token_hash):
        if task.stage.value not in ("running", "done"):
            continue

        found.append(_task_entry("analysis", t("ws.analysis_results"), f"/job-analysis/tasks/{task.id}", task.stage.value, seconds, now))

    for task, seconds in state.directions.store.listed(user.id, user.token_hash):
        if task.stage.value not in ("running", "done") or task.withdrawn:
            continue

        found.append(_task_entry("exploration", t("ws.exploration_results"), f"/directions/tasks/{task.id}", task.stage.value, seconds, now))

    for held in state.supplied.listed(user.id, user.token_hash):
        content = held.content

        if isinstance(content, LinkContent):
            facts = content.reading.facts or {}
            name = facts.get("title") or source_label(content.source_name)
        else:
            name = content.title

        found.append(
            Continuing(
                kind="supplied",
                title=t("ws.supplied_result", title=name),
                href=f"/job-records/supply/{held.token}",
                state="held",
                until=now + timedelta(seconds=held.seconds_left),
            )
        )

    return found


def _task_entry(kind: str, title: str, href: str, stage: str, seconds: float | None, now: datetime) -> Continuing:
    if stage == "running" or seconds is None:
        return Continuing(kind=kind, title=title, href=href, state="running")

    return Continuing(kind=kind, title=title, href=href, state="held", until=now + timedelta(seconds=seconds))


def _recent(db, user: CurrentUser, now: datetime) -> list[Saved]:
    """At most RECENT records kept for good, newest saved first; nothing temporary, binned or anyone else's."""
    saved: list[Saved] = []

    for item in list_experiences(db, user.id):
        saved.append(Saved(display_title(item.content, item.category), t("ws.kind_experience"), f"/experiences/{item.id}", item.updated_at))

    for row in direction_records.explorations_in_use(db, user.id):
        names = [direction.get("name", "") for direction in (row.content or {}).get("directions", [])]
        title = list_join([name for name in names if name]) or t("ws.untitled_exploration")
        saved.append(Saved(title, t("ws.kind_exploration"), f"/directions/explorations/{row.id}", row.saved_at))

    for row in job_records.postings_in_use(db, user.id):
        if job_recommendations.marker_of(db, row.id) is not None:
            # A temporary recommendation, never something kept for good.
            continue

        saved.append(Saved((row.facts or {}).get("title") or row.posting_id, t("ws.kind_job"), f"/job-records/postings/{row.id}", row.kept_at))

    for row in job_records.analyses_in_use(db, user.id):
        untitled = t("job.standalone.untitled") if row.search_id.startswith(job_records.STANDALONE_PREFIX) else row.posting_id
        saved.append(Saved((row.facts or {}).get("title") or untitled, t("ws.kind_analysis"), f"/job-records/analyses/{row.id}", row.saved_at))

    for row in job_records.searches_in_use(db, user.id):
        saved.append(Saved(_search_title(row), t("ws.kind_search"), f"/job-records/searches/{row.id}", row.kept_at))

    saved.sort(key=lambda entry: entry.saved_at, reverse=True)

    return saved[:RECENT]


def _search_title(row) -> str:
    scope = (row.summary or {}).get("scope", {})
    wanted = list(scope.get("targets") or []) or list((scope.get("direction") or {}).get("values") or [])

    return list_join(wanted) if wanted else t("ws.search_summary_any")
