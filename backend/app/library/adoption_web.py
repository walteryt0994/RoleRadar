"""Pages for writing a checked candidate into the user's records (U2-B2b).

From its card on the review page a waiting candidate opens its adopt page.
An experience can be added as a new draft (also when one of the same name
is there: "this is another experience"), or written into an experience of
its kind that the user picks from the list, the ones of a like name first
and none picked for them. The field page then shows, field by field, the
candidate's words next to the record's, with "keep as it is" chosen for
every field until the user chooses otherwise. A profile or skills candidate
goes straight to the field page for the basic profile.

Every post names the records and revisions its page showed. A post that
cannot be written (nothing chosen, nothing that would change, the candidate
or the record changed, a database failure) shows its page again with the
choices kept and says why; nothing is written. Choices kept on such a page
are not written yet, so it keeps guarding them; leaving the page asks
first and never writes. A candidate written once is never written again: a
repeat says where it went.
"""
import logging

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response

from app.library.adoption import (
    APPEND,
    BAD_CHOICE,
    FILL,
    KEEP,
    LEFT_OUT,
    PROFILE_KINDS,
    REPLACE,
    AdoptionConflict,
    NoChange,
    NotAdoptable,
    NothingChosen,
    TargetGone,
    adopt_as_new,
    experience_target,
    field_rows,
    into_profile,
    supplement,
)
from app.library.candidates import CANDIDATE_FIELDS, KIND_LABELS, SKILLS, current_values
from app.library.content import EXPERIENCE_CATEGORIES, PROFILE, display_title
from app.library.errors import ContentError, NotFound, StorageError
from app.library.identity import CurrentUser
from app.library.imports_web import BASE, _labels, gone_page
from app.library.models import CandidateState
from app.library.review import compare, open_candidate
from app.library.service import current_document, list_experiences
from app.library.web import _expected_revision, checked_form, current_user, message_page, render, runtime, see_other

CHOICE_PREFIX = "choice-"
NEW_PROFILE = "none"
ACTION_LABELS = {KEEP: "保持不变", FILL: "填入", REPLACE: "换成导入的内容", APPEND: "追加到已有内容后面"}
SAVE_FAILED = (
    "没有写入：写入数据库时出错，这次什么都没有改动，已有资料保持原样。"
    "你的选择还在下面；可以稍后再提交一次，重试时仍会检查这条候选和要写入的资料是否已被改过。"
)
CHANGED = {
    "candidate": "没有写入：这条候选在你打开这个页面之后改过（可能是在另一个标签页）。下面是它现在的内容，请核对后再决定。",
    "target": (
        "没有写入：要写入的资料在你打开这个页面之后改过（可能是在另一个标签页）。为了不覆盖，这次什么都没有写入。"
        "下面是它现在的内容，你刚才的选择还在（已经不适用的除外），请核对后再提交。"
    ),
    "profile": (
        "没有写入：你打开这个页面之后，当时的基本资料已经删除，或者删除后又新建了一份。为了不写进另一份资料，这次什么都没有写入。"
        "下面按现在的情况重新列出，你刚才的选择还在（已经不适用的除外），请核对后再提交。"
    ),
}

logger = logging.getLogger("roleradar.library")
router = APIRouter()


def install_adoption(app: FastAPI) -> None:
    app.include_router(router)


def _review(import_id: str, candidate_id: str, notice: str) -> Response:
    return see_other(f"{BASE}/{import_id}/review?notice={notice}#c-{candidate_id}")


def _refused(request: Request, user: CurrentUser, import_id: str, candidate_id: str, reason: str) -> Response:
    messages = {
        LEFT_OUT: ("这条已标为不加入", "标为不加入的条目不能加入。需要的话，先在核对页把它恢复为待核对。"),
        "kind": ("这条不能这样加入", "这类条目不能这样加入。当时的求职意向只供参考，不会写进求职偏好。"),
    }
    title, message = messages.get(reason, messages["kind"])

    return message_page(
        request,
        title=title,
        message=message,
        status_code=409,
        user=user,
        links=[(f"{BASE}/{import_id}/review#c-{candidate_id}", "回到核对页")],
    )


# --- choosing how ----------------------------------------------------------------------------------


@router.get("/imports/{import_id}/candidates/{candidate_id}/adopt")
def adopt_page(import_id: str, candidate_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())
    except NotFound:
        return gone_page(request, user)

    if candidate.state == CandidateState.ADOPTED:
        return _review(import_id, candidate_id, "adopted_already")

    if candidate.state != CandidateState.PENDING:
        return _refused(request, user, import_id, candidate_id, LEFT_OUT)

    if candidate.kind in PROFILE_KINDS:
        return see_other(f"{BASE}/{import_id}/candidates/{candidate_id}/adopt/fields?target=profile")

    if candidate.kind not in EXPERIENCE_CATEGORIES:
        return _refused(request, user, import_id, candidate_id, "kind")

    return choose_page(request, user, row, candidate)


def choose_page(request: Request, user: CurrentUser, row, candidate, *, error: str | None = None, status_code: int = 200):
    """How to add an experience: new, or into one of the same kind (like names first, none picked)."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        experiences = list_experiences(db, user.id, category=candidate.kind)
        comparison = compare(db, user.id, [candidate]).get(candidate.id)

    alike = {match.item_id for match in comparison.matches} if comparison else set()
    values = current_values(candidate.kind, candidate.extracted, candidate.edits)

    return render(
        request,
        "import_adopt.html",
        {
            "section": "imports",
            "row": row,
            "candidate": candidate,
            "title": display_title(values, candidate.kind),
            "kind_label": KIND_LABELS[candidate.kind],
            "values": values,
            "fields": [entry for entry in CANDIDATE_FIELDS[candidate.kind] if values.get(entry.name)],
            "targets": sorted(experiences, key=lambda item: item.id not in alike),
            "alike": alike,
            "error": error,
            "now": rt.now(),
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


@router.post("/imports/{import_id}/candidates/{candidate_id}/adopt/new")
def adopt_new(
    import_id: str,
    candidate_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)

    try:
        outcome = adopt_as_new(rt.database, user.id, import_id, candidate_id, expected, rt.now())
    except NotFound:
        return gone_page(request, user)
    except NotAdoptable as refused:
        return _refused(request, user, import_id, candidate_id, refused.reason)
    except (AdoptionConflict, ContentError, StorageError) as problem:
        if isinstance(problem, StorageError):
            logger.error("adopting a candidate failed (%s)", type(problem.__cause__).__name__)

        try:
            with rt.database.transaction() as db:
                row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())
        except NotFound:
            return gone_page(request, user)

        if isinstance(problem, AdoptionConflict):
            error, status_code = CHANGED["candidate"], 409
        elif isinstance(problem, ContentError):
            error, status_code = f"没有加入：{problem.message} 可以先修改这条候选。", 422
        else:
            error, status_code = SAVE_FAILED, 500

        return choose_page(request, user, row, candidate, error=error, status_code=status_code)

    return _review(import_id, candidate_id, "adopted" if outcome.written else "adopted_already")


# --- choosing fields ---------------------------------------------------------------------------------


def _target_of(db, user: CurrentUser, candidate, target: str):
    """The record a field page writes into: (target key, item or None, its revision or None)."""
    if candidate.kind in PROFILE_KINDS:
        profile = current_document(db, user.id, PROFILE)

        return "profile", profile, profile.revision if profile else None

    item = experience_target(db, user.id, target, candidate.kind)

    return item.id, item, item.revision


def fields_page(
    request: Request,
    user: CurrentUser,
    import_id: str,
    candidate_id: str,
    target: str,
    *,
    choices: dict[str, str] | None = None,
    error: str | None = None,
    field_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """The candidate's fields next to the record's; each keeps the record as it is until chosen otherwise."""
    rt = runtime(request)

    try:
        with rt.database.transaction() as db:
            row, candidate = open_candidate(db, user.id, import_id, candidate_id, rt.now())

            if candidate.state != CandidateState.PENDING:
                state = candidate.state
            else:
                state = None
                key, item, revision = _target_of(db, user, candidate, target)
    except NotFound:
        return gone_page(request, user)
    except TargetGone:
        return _target_gone(request, user, import_id, candidate_id)

    if state == CandidateState.ADOPTED:
        return _review(import_id, candidate_id, "adopted_already")

    if state is not None:
        return _refused(request, user, import_id, candidate_id, LEFT_OUT)

    values = current_values(candidate.kind, candidate.extracted, candidate.edits)
    rows = field_rows(candidate.kind, values, item.content if item is not None else {})
    offered = {row_.source: row_ for row_ in rows}
    # A choice survives only where the page still offers it.
    kept = {
        source: action
        for source, action in (choices or {}).items()
        if source in offered and action in offered[source].actions
    }
    # Kept choices other than "keep as it is" were sent but not written: the page guards them from the start.
    unsaved = any(action != KEEP for action in kept.values())

    return render(
        request,
        "import_adopt_fields.html",
        {
            "section": "imports",
            "row": row,
            "candidate": candidate,
            "skills": candidate.kind == SKILLS,
            "target": key,
            "item": item,
            "title": display_title(values, candidate.kind) if candidate.kind in EXPERIENCE_CATEGORIES else KIND_LABELS[candidate.kind],
            "target_title": display_title(item.content, item.category) if item is not None else None,
            "expected_target": str(revision) if revision is not None else NEW_PROFILE,
            "rows": rows,
            "choices": kept,
            "unsaved_input": unsaved,
            "ACTION_LABELS": ACTION_LABELS,
            "KEEP": KEEP,
            "error": error,
            "field_errors": field_errors or {},
            "now": rt.now(),
            **_labels(),
        },
        user=user,
        status_code=status_code,
    )


def _target_gone(request: Request, user: CurrentUser, import_id: str, candidate_id: str) -> Response:
    return message_page(
        request,
        title="要写入的资料已经不在了",
        message="选择的经历已经删除、移到回收站，或者不属于这一类，这次什么都没有写入。可以回到这条候选重新选择。",
        status_code=409,
        user=user,
        links=[(f"{BASE}/{import_id}/candidates/{candidate_id}/adopt", "重新选择"), (f"{BASE}/{import_id}/review#c-{candidate_id}", "回到核对页")],
    )


@router.get("/imports/{import_id}/candidates/{candidate_id}/adopt/fields")
def adopt_fields_page(
    import_id: str, candidate_id: str, request: Request, target: str = "", user: CurrentUser = Depends(current_user)
):
    if not target:
        # The list was sent without a choice (the browser requires one): back to it.
        return see_other(f"{BASE}/{import_id}/candidates/{candidate_id}/adopt")

    return fields_page(request, user, import_id, candidate_id, target)


@router.post("/imports/{import_id}/candidates/{candidate_id}/adopt/fields")
def adopt_fields(
    import_id: str,
    candidate_id: str,
    request: Request,
    user: CurrentUser = Depends(current_user),
    form: dict[str, str] = Depends(checked_form),
):
    rt = runtime(request)
    expected = _expected_revision(form)
    target = form.get("target", "")
    expected_target = form.get("expected_target_revision", "")
    shown_id = form.get("expected_target_id", "")
    choices = {name[len(CHOICE_PREFIX):]: value for name, value in form.items() if name.startswith(CHOICE_PREFIX)}

    # A record's revision, or "none" for a profile that did not exist when the page was shown.
    numeric = expected_target.isascii() and expected_target.isdigit()
    # A profile shown is named by its id too: one made again after a delete starts at revision 1 again.
    shown_profile = (shown_id, int(expected_target)) if numeric and shown_id else None

    if target == "profile":
        complete = shown_profile is not None or (expected_target == NEW_PROFILE and not shown_id)
    else:
        complete = numeric

    if not complete:
        return message_page(request, title="请求无效", message="这个表单缺少必要的信息，没有写入。", status_code=400, user=user)

    try:
        if target == "profile":
            outcome = into_profile(
                rt.database, user.id, import_id, candidate_id, expected, shown_profile, choices, rt.now(),
            )
        else:
            outcome = supplement(
                rt.database, user.id, import_id, candidate_id, expected, target, int(expected_target), choices, rt.now(),
            )
    except NotFound:
        return gone_page(request, user)
    except TargetGone:
        return _target_gone(request, user, import_id, candidate_id)
    except NotAdoptable as refused:
        if refused.reason == BAD_CHOICE:
            return message_page(
                request, title="请求无效", message="提交的选择不是这个页面给出的选项，什么都没有写入。", status_code=400, user=user,
            )

        return _refused(request, user, import_id, candidate_id, refused.reason)
    except NothingChosen:
        return fields_page(
            request, user, import_id, candidate_id, target, choices=choices, status_code=422,
            error="还没有选择要写入的栏目，所以什么都没有写入。不需要这一条的话，可以回到核对页选择“不加入”。",
        )
    except NoChange:
        return fields_page(
            request, user, import_id, candidate_id, target, choices=choices, status_code=409,
            error="选中的栏目写入后和已有内容完全相同，所以什么都没有写入。",
        )
    except AdoptionConflict as conflict:
        return fields_page(request, user, import_id, candidate_id, target, choices=choices, status_code=409, error=CHANGED[conflict.what])
    except ContentError as problem:
        return fields_page(
            request, user, import_id, candidate_id, target, choices=choices, status_code=422,
            error=f"没有写入：{problem.message}", field_errors=problem.field_errors,
        )
    except StorageError as problem:
        logger.error("adopting a candidate failed (%s)", type(problem.__cause__).__name__)

        return fields_page(request, user, import_id, candidate_id, target, choices=choices, status_code=500, error=SAVE_FAILED)

    return _review(import_id, candidate_id, "adopted" if outcome.written else "adopted_already")
