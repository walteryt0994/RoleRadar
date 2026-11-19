"""Pages for checking the candidates read from an import (U2-B2a).

From an import's page the user asks for candidates, once; the review page
lists them by kind, each value with where it stands in the text, the
cautions the text calls for, and the records it may duplicate (by name
only). One candidate is changed on its own edit page; leaving it out and
taking it back are small forms on the review page. Every change names the
revision its page showed. An edit that the database could not keep is
shown again as typed, unsaved, to be saved once more under the same
checks. Adopting a candidate has its own pages (adoption_web, U2-B2b); its
card then says where it went and which fields were written. The candidates, edits and choices are kept with the import, for its
seven days and no longer. Nothing here writes to the experiences, the
profile or the preferences: there is no adopting yet.
"""
import json
import logging
import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response

from app.library.candidates import (
    CANDIDATE_FIELDS,
    CANDIDATE_KINDS,
    CAUTION_TEXTS,
    KIND_LABELS,
    PROBLEM_TEXTS,
    REFERENCE_KINDS,
    UNSURE,
    current_values,
    extracted_value,
    parse_edits,
    where,
)
from app.library.content import EXPERIENCE_CATEGORIES, PROFILE, display_title
from app.library.errors import ContentError, NotFound, StorageError
from app.library.identity import CurrentUser
from app.library.imports import draft_of, locator, open_import
from app.library.imports_web import BASE, _labels, gone_page
from app.library.models import CandidateState, ImportCandidate, StructuringState
from app.library.service import list_experiences
from app.library.review import (
    ACTION_LABELS,
    CONFLICTING,
    CONTAINED,
    DIFFERS,
    DUPLICATE,
    FILLS,
    NEW,
    SAME,
    LIVE_FAILURE_TEXTS,
    STATE_LABELS,
    AdoptionView,
    Asked,
    CandidateConflict,
    Comparison,
    NotEditable,
    adoption_views,
    can_ask_again,
    candidates_of,
    compare,
    failure_text,
    is_live,
    open_candidate,
    running_here,
    review_record,
    save_edits,
    set_candidate_state,
    structure_import,
    structurer_label,
    structuring_of,
)
from app.library.web import (
    DOWNLOAD_POLICY,
    _expected_revision,
    checked_form,
    content_disposition,
    current_user,
    message_page,
    render,
    runtime,
    see_other,
)
from app.i18n import MessageMap, is_english, lazy, list_join, t
from app.library.presentation import label_of

NOTICES = MessageMap({
    "structured": "imp.review.1",
    "already_structured": "imp.review.2",
    "structuring_running": "imp.review.3",
    "structuring_dropped": "imp.review.4",
    "structuring_started": "imp.review.5",
    "structuring_cancelled": "imp.review.6",
    "structuring_too_late": "imp.review.7",
    "structuring_interrupted": "imp.review.8",
    "structuring_not_running": "imp.review.9",
    "candidate_saved": "imp.review.10",
    "candidate_unchanged": "imp.review.11",
    "candidate_excluded": "imp.review.12",
    "candidate_restored": "imp.review.13",
    "candidate_already": "imp.review.14",
    "adopted": "imp.review.15",
    "adopted_already": "imp.review.16",
})
ASKED_NOTICES = {
    Asked.STORED: "structured",
    Asked.ALREADY: "already_structured",
    Asked.RUNNING: "structuring_running",
    Asked.DROPPED: "structuring_dropped",
}
COMPARISON_LABELS = MessageMap({NEW: "imp.review.17", DUPLICATE: "imp.review.18", CONFLICTING: "imp.review.19"})
PROFILE_COMPARISON_LABELS = MessageMap({
    NEW: "imp.review.20",
    DUPLICATE: "imp.review.21",
    CONFLICTING: "imp.review.22",
})
FIELD_STATE_LABELS = MessageMap({
    SAME: "imp.review.23",
    CONTAINED: "imp.review.24",
    FILLS: "imp.review.25",
    DIFFERS: "imp.review.26",
})
LEFT_OUT = 409
SAVE_FAILED = (
    lazy("imp.review.27")
)

logger = logging.getLogger("roleradar.library")
router = APIRouter()


def install_review(app: FastAPI) -> None:
    app.include_router(router)


# --- what a page shows of a candidate ----------------------------------------------------


@dataclass
class FieldView:
    name: str
    label: str
    value: str
    found: str
    edited: bool
    # Where the found words stand, as people say it ("第 2 页第 3、4 行").
    places: str
    cautions: list[str]


@dataclass
class CandidateView:
    id: str
    kind: str
    title: str
    state: str
    revision: int
    edited: bool
    fields: list[FieldView]
    cautions: list[str]
    comparison: Comparison | None = None
    # Where it went, once adopted.
    adoption: AdoptionView | None = None


def candidate_view(
    candidate: ImportCandidate,
    segments: list,
    comparison: Comparison | None = None,
    adoption: AdoptionView | None = None,
) -> CandidateView:
    values = current_values(candidate.kind, candidate.extracted, candidate.edits)
    edits = candidate.edits or {}
    fields = []

    for entry in CANDIDATE_FIELDS[candidate.kind]:
        found = extracted_value(candidate.extracted, entry.name)

        if not found and entry.name not in edits:
            continue

        fields.append(
            FieldView(
                name=entry.name,
                label=label_of(candidate.kind, entry.name),
                value=values[entry.name],
                found=found,
                edited=entry.name in edits,
                places=places_text([where(segments, piece) for piece in candidate.extracted.get(entry.name, [])]),
                cautions=[
                    CAUTION_TEXTS[caution["code"]]
                    for caution in candidate.cautions
                    if caution["field"] == entry.name and caution["code"] in CAUTION_TEXTS
                ],
            )
        )

    return CandidateView(
        id=candidate.id,
        kind=candidate.kind,
        title=_title(candidate.kind, values),
        state=candidate.state,
        revision=candidate.revision,
        edited=bool(edits),
        fields=fields,
        cautions=[
            CAUTION_TEXTS[caution["code"]]
            for caution in candidate.cautions
            if caution["field"] is None and caution["code"] in CAUTION_TEXTS
        ],
        comparison=comparison,
        adoption=adoption,
    )


# How a place with one line reads in each language (candidates.where): "<place>第 N 行" or "<place>, line N".
LINE = re.compile(r"(.*)第 (\d+) 行")
LINE_EN = re.compile(r"(.*), line (\d+)")


def places_text(places: list[str]) -> str:
    """Where pieces stand, each page or paragraph named once: 第 1 页第 2、3 行、表格 1 第 4 行.

    A place whose line cannot be told (candidates.where) is kept whole, and
    then every place is set apart by "；", so that what it says stays with it.
    """
    groups: list[tuple[str, list[str]]] = []

    pattern = LINE_EN if is_english() else LINE

    for place in dict.fromkeys(places):
        match = pattern.fullmatch(place)

        if match and groups and groups[-1][0] == match.group(1) and groups[-1][1]:
            groups[-1][1].append(match.group(2))
        elif match:
            groups.append((match.group(1), [match.group(2)]))
        else:
            groups.append((place, []))

    apart = t("imp.review.28") if any(place.endswith(t("loc.unsure")) for place in places) else t("imp.review.29")

    return apart.join(
        (t("loc.grouped_lines", place=prefix, lines=list_join(lines)) if len(lines) > 1 else t("loc.grouped_line", place=prefix, lines=lines[0]))
        if lines
        else prefix
        for prefix, lines in groups
    )


def _title(kind: str, values: dict[str, str]) -> str:
    if kind == PROFILE or kind in REFERENCE_KINDS:
        return KIND_LABELS[kind]

    return display_title(values, kind)


def _live_refusal(request: Request) -> str | None:
    """Why a real structuring may not start now, when the app has one (U2-C3, app.live_structuring)."""
    structuring = request.app.state.structuring
    refusal = structuring.live.refusal() if structuring.offered else None

    return LIVE_FAILURE_TEXTS[f"gate_{refusal.value}"] if refusal is not None else None


def _problem_lines(structuring, segments: list) -> list[str]:
    """The dropped candidates, by place and reason; never their words."""
    lines = []

    for problem in structuring.problems or []:
        places = t("imp.review.29").join(locator(segments[number - 1]) for number in problem["segments"] if number <= len(segments))
        kind = t("imp.review.31", p0=KIND_LABELS[problem['kind']]) if problem["kind"] else ""
        place = t("imp.review.32", p0=places) if places else ""
        lines.append(t("imp.review.33", p0=problem['position'], p1=kind, p2=place, p3=PROBLEM_TEXTS.get(problem['reason'], '没有通过核对')))

    return lines


# --- asking for candidates -------------------------------------------------------------------


@router.post("/imports/{import_id}/structure", dependencies=[Depends(checked_form)])
def structure(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        asked = structure_import(rt.database, rt.structurer, user.id, import_id, rt.now)
    except NotFound:
        return gone_page(request, user)

    if asked == Asked.NOT_READ:
        return message_page(
            request,
            title=t("imp.review.34"),
            message=t("imp.review.35"),
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", t("imp.review.36"))],
        )

    if asked == Asked.DROPPED:
        with rt.database.transaction() as db:
            try:
                open_import(db, user.id, import_id, rt.now())
            except NotFound:
                # Discarded or expired while it was being structured: nothing was kept.
                return see_other(f"{BASE}?notice=import_ended_structuring")

    notice = ASKED_NOTICES.get(asked)

    return see_other(f"{BASE}/{import_id}/review" + (f"?notice={notice}" if notice else ""))


# --- the review page --------------------------------------------------------------------------


@router.get("/imports/{import_id}/review")
def review_page(import_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    now = rt.now()

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, now)
            draft = draft_of(db, row.id)
            structuring = structuring_of(db, row.id)
            candidates = candidates_of(db, user.id, row.id)
            comparisons = compare(db, user.id, candidates)
            adoptions = adoption_views(db, user.id, candidates)
            # 补充到已有经历 (owner redesign, plan 3.4, image 11): the user's experiences of each kind a candidate here
            # is, to choose one beside the candidate; the field-by-field choice follows on its own page, as before.
            kinds = {candidate.kind for candidate in candidates if candidate.kind in EXPERIENCE_CATEGORIES}
            targets = {kind: list_experiences(db, user.id, category=kind) for kind in kinds}
    except NotFound:
        return gone_page(request, user)

    if structuring is None:
        return see_other(f"{BASE}/{row.id}")

    handed = running_here(rt.runs, structuring, now)

    if notice == "structuring_started" and handed is None:
        # The page reloads with its address when the run ends: a run over is no news of a start.
        notice = ""

    segments = row.segments or []
    views = [
        candidate_view(candidate, segments, comparisons.get(candidate.id), adoptions.get(candidate.id))
        for candidate in candidates
    ]
    groups = [
        (kind, KIND_LABELS[kind], [view for view in views if view.kind == kind])
        for kind in CANDIDATE_KINDS
        if any(view.kind == kind for view in views)
    ]

    return render(
        request,
        "import_review.html",
        {
            "section": "imports",
            "row": row,
            "draft": draft,
            "name": row.filename or t("imp.review.37"),
            "structuring": structuring,
            "structured_by": structurer_label(structuring.structurer),
            "live_structured": is_live(structuring.structurer),
            "live_offered": request.app.state.structuring.offered,
            "live_refusal": _live_refusal(request),
            "handed": handed,
            "running": structuring.state == StructuringState.RUNNING,
            "failure": (
                failure_text(structuring.structurer, structuring.outcome)
                if structuring.state == StructuringState.FAILED
                else None
            ),
            "can_ask_again": can_ask_again(structuring, now),
            "problems": _problem_lines(structuring, segments),
            "groups": [group for group in groups if group[0] not in REFERENCE_KINDS],
            "references": [group for group in groups if group[0] in REFERENCE_KINDS],
            "targets": targets,
            "EXPERIENCE_CATEGORIES": EXPERIENCE_CATEGORIES,
            "KIND_LABELS": KIND_LABELS,
            "display_title": display_title,
            "counts": {
                "total": len(views),
                "pending": sum(1 for view in views if view.state == CandidateState.PENDING),
                "excluded": sum(1 for view in views if view.state == CandidateState.EXCLUDED),
                "adopted": sum(1 for view in views if view.state == CandidateState.ADOPTED),
                "edited": sum(1 for view in views if view.edited),
            },
            "ADOPTION_LABELS": ACTION_LABELS,
            "notice": NOTICES.get(notice),
            "now": now,
            "PROFILE": PROFILE,
            "COMPARISON_LABELS": COMPARISON_LABELS,
            "PROFILE_COMPARISON_LABELS": PROFILE_COMPARISON_LABELS,
            "FIELD_STATE_LABELS": FIELD_STATE_LABELS,
            "CANDIDATE_FIELD_LABELS": {
                kind: {entry.name: label_of(kind, entry.name) for entry in fields} for kind, fields in CANDIDATE_FIELDS.items()
            },
            "CANDIDATE_STATE_LABELS": STATE_LABELS,
            **_labels(),
        },
        user=user,
    )


# --- one candidate's edit page -----------------------------------------------------------------


def edit_page(
    request: Request,
    user: CurrentUser,
    row,
    candidate: ImportCandidate,
    *,
    draft=None,
    values: dict[str, str] | None = None,
    revision: int | None = None,
    saved: dict[str, str] | None = None,
    error: str | None = None,
    field_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """The candidate's fields to edit, each with what was found and where; values are what to show."""
    view = candidate_view(candidate, row.segments or [])
    current = current_values(candidate.kind, candidate.extracted, candidate.edits)

    return render(
        request,
        "import_candidate.html",
        {
            "section": "imports",
            "row": row,
            "draft": draft,
            "name": row.filename or t("imp.review.37"),
            "candidate": view,
            "kind_label": KIND_LABELS[candidate.kind],
            "fields": CANDIDATE_FIELDS[candidate.kind],
            "found": {
                entry.name: (
                    extracted_value(candidate.extracted, entry.name),
                    places_text([where(row.segments or [], piece) for piece in candidate.extracted.get(entry.name, [])]),
                )
                for entry in CANDIDATE_FIELDS[candidate.kind]
            },
            "values": values if values is not None else current,
            "revision": revision if revision is not None else candidate.revision,
            "saved": saved,
            "unsaved_input": values is not None,
            "error": error,
            "field_errors": field_errors or {},
            "now": runtime(request).now(),
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


def _left_out_page(
    request: Request, user: CurrentUser, import_id: str, candidate_id: str, state: str = CandidateState.EXCLUDED
) -> Response:
    if state == CandidateState.ADOPTED:
        title = t("imp.review.38")
        message = t("imp.review.39")
    else:
        title = t("imp.review.40")
        message = t("imp.review.41")

    return message_page(
        request,
        title=title,
        message=message,
        status_code=LEFT_OUT,
        user=user,
        links=[(f"{BASE}/{import_id}/review#c-{candidate_id}", t("imp.review.42"))],
    )


@router.get("/imports/{import_id}/candidates/{candidate_id}/edit")
def candidate_edit(import_id: str, candidate_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())
            draft = draft_of(db, row.id)
    except NotFound:
        return gone_page(request, user)

    if candidate.state != CandidateState.PENDING:
        return _left_out_page(request, user, import_id, candidate_id, candidate.state)

    return edit_page(request, user, row, candidate, draft=draft)


@router.post("/imports/{import_id}/candidates/{candidate_id}/edit")
def candidate_save(
    import_id: str,
    candidate_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)

    try:
        with rt.database.transaction() as db:
            row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())
            draft = draft_of(db, row.id)
    except NotFound:
        return gone_page(request, user)

    # As typed, with the line breaks a browser sends made plain, so they never read as a change.
    submitted = {
        entry.name: form.get(entry.name, "").replace("\r\n", "\n").replace("\r", "\n")
        for entry in CANDIDATE_FIELDS[candidate.kind]
    }

    try:
        values = parse_edits(form, candidate.kind)
        changed = save_edits(rt.database, user.id, import_id, candidate_id, expected, values, rt.now())
    except ContentError as error:
        return edit_page(
            request, user, row, candidate, draft=draft, values=submitted, revision=expected, error=error.message,
            field_errors=error.field_errors, status_code=422,
        )
    except CandidateConflict as conflict:
        # Shown again with what was typed, the saved words next to it, and the current revision.
        return edit_page(
            request, user, row, candidate, draft=draft, values=values, revision=conflict.candidate.revision,
            saved=conflict.candidate.values, status_code=409,
            error=(
                t("imp.review.43")
            ),
        )
    except NotEditable as refused:
        return _left_out_page(request, user, import_id, candidate_id, refused.state)
    except NotFound:
        return gone_page(request, user)
    except StorageError as error:
        # Nothing was written. What was typed comes back, unsaved, with the revision it was typed
        # against, so saving it again meets every check once more. The log names the error only.
        logger.error("saving a candidate's edits failed (%s)", type(error.__cause__).__name__)

        return edit_page(
            request, user, row, candidate, draft=draft, values=values, revision=expected, error=SAVE_FAILED,
            status_code=500,
        )

    notice = "candidate_saved" if changed else "candidate_unchanged"

    return see_other(f"{BASE}/{import_id}/review?notice={notice}#c-{candidate_id}")


# --- leaving out and taking back ------------------------------------------------------------------


def _change_state(request: Request, user: CurrentUser, form: dict, import_id: str, candidate_id: str, state: str):
    rt = runtime(request)

    try:
        changed = set_candidate_state(
            rt.database, user.id, import_id, candidate_id, _expected_revision(form), state, rt.now()
        )
    except NotFound:
        return gone_page(request, user)
    except NotEditable as refused:
        return _left_out_page(request, user, import_id, candidate_id, refused.state)
    except CandidateConflict:
        return message_page(
            request,
            title=t("imp.review.44"),
            message=t("imp.review.45"),
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}/review#c-{candidate_id}", t("imp.review.42"))],
        )

    if not changed:
        notice = "candidate_already"
    else:
        notice = "candidate_excluded" if state == CandidateState.EXCLUDED else "candidate_restored"

    return see_other(f"{BASE}/{import_id}/review?notice={notice}#c-{candidate_id}")


@router.post("/imports/{import_id}/candidates/{candidate_id}/exclude")
def candidate_exclude(
    import_id: str,
    candidate_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return _change_state(request, user, form, import_id, candidate_id, CandidateState.EXCLUDED)


@router.post("/imports/{import_id}/candidates/{candidate_id}/restore")
def candidate_restore(
    import_id: str,
    candidate_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    return _change_state(request, user, form, import_id, candidate_id, CandidateState.PENDING)


# --- taking it away ------------------------------------------------------------------------------


@router.get("/imports/{import_id}/candidates.json")
def download_candidates(import_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """The candidates and the review so far, as JSON; downloaded, never shown as a page."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row = open_import(db, user.id, import_id, rt.now())
            structuring = structuring_of(db, row.id)
            candidates = candidates_of(db, user.id, row.id)
            adoptions = adoption_views(db, user.id, candidates)
            draft = draft_of(db, row.id)
            record = review_record(row, structuring, candidates, adoptions, draft) if structuring is not None else None
    except NotFound:
        return gone_page(request, user)

    if record is None:
        return message_page(
            request,
            title=t("imp.review.46"),
            message=t("imp.review.47"),
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", t("imp.review.36"))],
        )

    stem = (row.filename or t("imp.review.37")).rsplit(".", 1)[0] or t("imp.review.48")

    return Response(
        json.dumps(record, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(t("imp.review.49", p0=stem)),
            "Content-Security-Policy": DOWNLOAD_POLICY,
        },
    )
