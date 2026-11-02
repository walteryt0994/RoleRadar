"""Pages for checking the candidates read from an import (U2-B2a).

From an import's page the user asks for candidates, once; the review page
lists them by kind, each value with where it stands in the text, the
cautions the text calls for, and the records it may duplicate (by name
only). One candidate is changed on its own edit page; leaving it out and
taking it back are small forms on the review page. Every change names the
revision its page showed. An edit that the database could not keep is
shown again as typed, unsaved, to be saved once more under the same
checks. The candidates, edits and choices are kept with the import, for its
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
    FAILURE_TEXTS,
    KIND_LABELS,
    PROBLEM_TEXTS,
    REFERENCE_KINDS,
    current_values,
    extracted_value,
    parse_edits,
    where,
)
from app.library.content import PROFILE, display_title
from app.library.errors import ContentError, NotFound, StorageError
from app.library.identity import CurrentUser
from app.library.imports import locator, open_import
from app.library.imports_web import BASE, _labels, gone_page
from app.library.models import CandidateState, ImportCandidate, StructuringState
from app.library.review import (
    CONFLICTING,
    CONTAINED,
    DIFFERS,
    DUPLICATE,
    FILLS,
    NEW,
    SAME,
    STATE_LABELS,
    STRUCTURER_LABELS,
    Asked,
    CandidateConflict,
    Comparison,
    NotEditable,
    can_ask_again,
    candidates_of,
    compare,
    open_candidate,
    review_record,
    save_edits,
    set_candidate_state,
    structure_import,
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

NOTICES = {
    "structured": "已整理出待核对的条目。它们还没有加入经历库。",
    "already_structured": "这次导入已经整理过，这次没有重新整理；你的修改和选择都还在。",
    "structuring_running": "正在整理，请稍后刷新。",
    "structuring_dropped": "这次整理的结果没有保存：同一时间另有一次整理。下面是现在的状态。",
    "candidate_saved": "已保存你的修改。它只暂存在这次导入里，没有加入经历库。",
    "candidate_unchanged": "内容没有变化，什么都没有保存。",
    "candidate_excluded": "已标为不加入，可以随时恢复。",
    "candidate_restored": "已恢复为待核对。",
    "candidate_already": "这条已经是这个状态，没有重复更改。",
}
ASKED_NOTICES = {
    Asked.STORED: "structured",
    Asked.ALREADY: "already_structured",
    Asked.RUNNING: "structuring_running",
    Asked.DROPPED: "structuring_dropped",
}
COMPARISON_LABELS = {NEW: "新增", DUPLICATE: "疑似重复", CONFLICTING: "可能冲突"}
PROFILE_COMPARISON_LABELS = {
    NEW: "还没有基本资料",
    DUPLICATE: "与现有基本资料一致或可补充",
    CONFLICTING: "与现有基本资料有不同",
}
FIELD_STATE_LABELS = {
    SAME: "相同",
    CONTAINED: "这里的每一行都是已有内容中的一整行",
    FILLS: "已有记录这一栏是空的",
    DIFFERS: "不同",
}
LEFT_OUT = 409
SAVE_FAILED = (
    "没有保存：写入数据库时出错，这次的修改没有生效，已保存的内容保持原样。"
    "你刚才填写的内容还在下面；可以稍后再点“保存修改”重试，重试时仍会检查这条是否已被改过、标为不加入或到期。"
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


def candidate_view(candidate: ImportCandidate, segments: list, comparison: Comparison | None = None) -> CandidateView:
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
                label=entry.label,
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
    )


LINE = re.compile(r"(.*)第 (\d+) 行")


def places_text(places: list[str]) -> str:
    """Where pieces stand, each page or paragraph named once: 第 1 页第 2、3 行、表格 1 第 4 行."""
    groups: list[tuple[str, list[str]]] = []

    for place in dict.fromkeys(places):
        match = LINE.fullmatch(place)

        if match and groups and groups[-1][0] == match.group(1) and groups[-1][1]:
            groups[-1][1].append(match.group(2))
        elif match:
            groups.append((match.group(1), [match.group(2)]))
        else:
            groups.append((place, []))

    return "、".join(f"{prefix}第 {'、'.join(lines)} 行" if lines else prefix for prefix, lines in groups)


def _title(kind: str, values: dict[str, str]) -> str:
    if kind == PROFILE or kind in REFERENCE_KINDS:
        return KIND_LABELS[kind]

    return display_title(values, kind)


def _problem_lines(structuring, segments: list) -> list[str]:
    """The dropped candidates, by place and reason; never their words."""
    lines = []

    for problem in structuring.problems or []:
        places = "、".join(locator(segments[number - 1]) for number in problem["segments"] if number <= len(segments))
        kind = f"（{KIND_LABELS[problem['kind']]}）" if problem["kind"] else ""
        place = f"，出自{places}" if places else ""
        lines.append(f"第 {problem['position']} 条{kind}{place}：{PROBLEM_TEXTS.get(problem['reason'], '没有通过核对')}。")

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
            title="没有整理",
            message="这次导入还没有读到文字，没有可以整理的内容。",
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", "回到这次导入")],
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
            structuring = structuring_of(db, row.id)
            candidates = candidates_of(db, user.id, row.id)
            comparisons = compare(db, user.id, candidates)
    except NotFound:
        return gone_page(request, user)

    if structuring is None:
        return see_other(f"{BASE}/{row.id}")

    segments = row.segments or []
    views = [candidate_view(candidate, segments, comparisons.get(candidate.id)) for candidate in candidates]
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
            "name": row.filename or "粘贴的文字",
            "structuring": structuring,
            "structured_by": STRUCTURER_LABELS.get(structuring.structurer, "整理服务"),
            "running": structuring.state == StructuringState.RUNNING,
            "failure": FAILURE_TEXTS.get(structuring.outcome) if structuring.state == StructuringState.FAILED else None,
            "can_ask_again": can_ask_again(structuring, now),
            "problems": _problem_lines(structuring, segments),
            "groups": [group for group in groups if group[0] not in REFERENCE_KINDS],
            "references": [group for group in groups if group[0] in REFERENCE_KINDS],
            "counts": {
                "total": len(views),
                "pending": sum(1 for view in views if view.state == CandidateState.PENDING),
                "excluded": sum(1 for view in views if view.state == CandidateState.EXCLUDED),
                "edited": sum(1 for view in views if view.edited),
            },
            "notice": NOTICES.get(notice),
            "now": now,
            "PROFILE": PROFILE,
            "COMPARISON_LABELS": COMPARISON_LABELS,
            "PROFILE_COMPARISON_LABELS": PROFILE_COMPARISON_LABELS,
            "FIELD_STATE_LABELS": FIELD_STATE_LABELS,
            "CANDIDATE_FIELD_LABELS": {
                kind: {entry.name: entry.label for entry in fields} for kind, fields in CANDIDATE_FIELDS.items()
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
            "name": row.filename or "粘贴的文字",
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


def _left_out_page(request: Request, user: CurrentUser, import_id: str, candidate_id: str) -> Response:
    return message_page(
        request,
        title="这条已标为不加入",
        message="标为不加入的条目不能修改。需要的话，先在核对页把它恢复为待核对。",
        status_code=LEFT_OUT,
        user=user,
        links=[(f"{BASE}/{import_id}/review#c-{candidate_id}", "回到核对页")],
    )


@router.get("/imports/{import_id}/candidates/{candidate_id}/edit")
def candidate_edit(import_id: str, candidate_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    if candidate.state != CandidateState.PENDING:
        return _left_out_page(request, user, import_id, candidate_id)

    return edit_page(request, user, row, candidate)


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
            request, user, row, candidate, values=submitted, revision=expected, error=error.message,
            field_errors=error.field_errors, status_code=422,
        )
    except CandidateConflict as conflict:
        # Shown again with what was typed, the saved words next to it, and the current revision.
        return edit_page(
            request, user, row, candidate, values=values, revision=conflict.candidate.revision,
            saved=conflict.candidate.values, status_code=409,
            error=(
                "没有保存：这条在你打开这个页面之后被改过（可能是在另一个标签页）。"
                "输入框里是你刚才填写的内容，还没有保存；和现在已保存的内容不同的栏目下面标出了已保存的内容。"
                "核对后再保存一次，会用你的内容替换。"
            ),
        )
    except NotEditable:
        return _left_out_page(request, user, import_id, candidate_id)
    except NotFound:
        return gone_page(request, user)
    except StorageError as error:
        # Nothing was written. What was typed comes back, unsaved, with the revision it was typed
        # against, so saving it again meets every check once more. The log names the error only.
        logger.error("saving a candidate's edits failed (%s)", type(error.__cause__).__name__)

        return edit_page(
            request, user, row, candidate, values=values, revision=expected, error=SAVE_FAILED, status_code=500,
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
    except CandidateConflict:
        return message_page(
            request,
            title="没有更改",
            message="这条在你打开页面之后被改过（可能是在另一个标签页）。为了不覆盖，这次没有更改。请回到核对页看看现在的内容。",
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}/review#c-{candidate_id}", "回到核对页")],
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
            record = review_record(row, structuring, candidates) if structuring is not None else None
    except NotFound:
        return gone_page(request, user)

    if record is None:
        return message_page(
            request,
            title="还没有整理",
            message="这次导入还没有整理出候选，没有可下载的内容。",
            status_code=409,
            user=user,
            links=[(f"{BASE}/{import_id}", "回到这次导入")],
        )

    stem = (row.filename or "粘贴的文字").rsplit(".", 1)[0] or "导入"

    return Response(
        json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(f"{stem}（待核对的候选）.json"),
            "Content-Security-Policy": DOWNLOAD_POLICY,
        },
    )
