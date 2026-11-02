"""Adopting checked candidates into the user's own records (U2-B2b).

All material is synthetic and every candidate comes from the local
simulated rules. A candidate is written only at the user's explicit choice:
as a new draft experience, into chosen fields of an experience that is
there, or into chosen fields of the basic profile (skills into its own
自述技能 field). Tests compare stored rows, not only what pages say.
"""
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.library.candidates import UNKNOWN_FIELD, check_answer
from app.library.adoption import AdoptionConflict, adopt_as_new, into_profile
from app.library.content import EXPERIENCE_CATEGORIES, PROFILE
from app.library.imports import IMPORT_WINDOW
from app.library.lifecycle import request_experience_purge, run_purge_job
from app.library.models import (
    Attachment,
    AuditEvent,
    CandidateState,
    ExperienceItem,
    ExperienceVersion,
    ImportAdoption,
)
from app.library.resume_rules import structure
from app.library.service import current_document
from job_helpers import InlineExecutor, RecordingProvider, confirmed, review, start, task_of, use_provider
from library_helpers import Browser, form_for, forms_in
from test_library_candidates import CANARY, ask, library_rows, one, owner_id, paste, rows, rt

# --- helpers ------------------------------------------------------------------


def adopt_url(import_id: str, candidate_id: str) -> str:
    return f"/imports/{import_id}/candidates/{candidate_id}/adopt"


def fields_url(import_id: str, candidate_id: str, target: str) -> str:
    return f"{adopt_url(import_id, candidate_id)}/fields?target={target}"


def adopt_new(browser, import_id: str, candidate_id: str):
    url = adopt_url(import_id, candidate_id)

    return browser.submit(url, f"{url}/new")


def write_fields(browser, import_id: str, candidate_id: str, target: str, **choices):
    """Open the field page for this target and send it with these choices (the others kept)."""
    action = f"{adopt_url(import_id, candidate_id)}/fields"
    form = browser.form(fields_url(import_id, candidate_id, target), action)

    return browser.post(action, {**form.fields, **{f"choice-{name}": value for name, value in choices.items()}})


def item(app, item_id: str) -> ExperienceItem | None:
    with rt(app).database.transaction() as db:
        return db.get(ExperienceItem, item_id)


def items(app, category: str) -> list[ExperienceItem]:
    with rt(app).database.transaction() as db:
        return list(
            db.scalars(select(ExperienceItem).where(ExperienceItem.category == category).order_by(ExperienceItem.created_at))
        )


def versions(app, item_id: str) -> list[tuple]:
    with rt(app).database.transaction() as db:
        return [
            (version.id, version.kind, version.number, version.content, version.private_note)
            for version in db.scalars(
                select(ExperienceVersion).where(ExperienceVersion.item_id == item_id).order_by(ExperienceVersion.number)
            )
        ]


def actions_of(app, item_id: str) -> list[str]:
    with rt(app).database.transaction() as db:
        return [
            event.action
            for event in db.scalars(select(AuditEvent).where(AuditEvent.item_id == item_id).order_by(AuditEvent.id))
        ]


def adoptions(app) -> list[ImportAdoption]:
    with rt(app).database.transaction() as db:
        return list(db.scalars(select(ImportAdoption)))


def profile(app) -> ExperienceItem | None:
    with rt(app).database.transaction() as db:
        return current_document(db, owner_id(app), PROFILE)


def save_profile(browser, **values):
    page = browser.get("/basic-profile/edit").text
    [form] = [form for form in forms_in(page) if form.method == "post" and form.action.startswith("/basic-profile")]

    return browser.post(form.action, {**form.fields, **values})


@contextmanager
def refused(app, statement: str):
    """While open, the database refuses statements that start so, as a full or failing disk would."""
    engine = rt(app).database.engine

    def refuse(connection, cursor, sql, parameters, context, executemany):
        if sql.lstrip().upper().startswith(statement.upper()):
            raise OperationalError(sql, {}, Exception("synthetic disk failure"))

    event.listen(engine, "before_cursor_execute", refuse)

    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", refuse)


def work_record(browser, **content) -> str:
    """A work experience of the resume's organisation, confirmed, with a private note and an attachment."""
    fields = {
        "category": "work",
        "organization": "合成数据科技有限公司",
        "role": "数据实习生",
        "actions": "整理周报",
        "private_note": "私有备注-合成",
    }
    item_id = browser.create(**{**fields, **content})
    assert browser.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    assert browser.upload(item_id).status_code == 303

    return item_id


def imported(app, browser) -> str:
    import_id = paste(browser)
    ask(browser, import_id)

    return import_id


# --- a new draft --------------------------------------------------------------------------------------


def test_a_candidate_becomes_a_new_draft_only_when_chosen(app, browser_a):
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    browser_a.submit(edit, edit, role="数据分析实习生（暑期）")
    review_page = browser_a.get(f"/imports/{import_id}/review").text

    assert f'href="{adopt_url(import_id, work.id)}">加入…</a>' in review_page
    assert f'href="{adopt_url(import_id, one(app, import_id, "skills").id)}">保存为自述技能…</a>' in review_page
    assert adopt_url(import_id, one(app, import_id, "intention").id) not in review_page
    page = browser_a.get(adopt_url(import_id, work.id)).text
    assert "作为新的经历草稿加入" in page and "不会自动确认属实" in page and "你还没有这一类的经历" in page
    before = library_rows(app)

    response = adopt_new(browser_a, import_id, work.id)

    assert response.headers["location"] == f"/imports/{import_id}/review?notice=adopted#c-{work.id}"
    (made,) = items(app, "work")
    assert made.state == "draft" and made.last_confirmed_at is None and made.private_note == ""
    assert made.content == {
        "organization": "合成数据科技有限公司",
        "role": "数据分析实习生（暑期）",
        "period": "2025.06 - 2025.09",
        "actions": "参与搭建销售看板，团队共 4 人\n负责每周的数据清洗\n没有使用 Docker，部署由同事完成",
        "outcome": "",
        "reflection": "",
    }
    assert [kind for _, kind, *_ in versions(app, made.id)] == ["initial"]
    assert actions_of(app, made.id) == ["created", "adopted_from_import"]
    (adoption,) = adoptions(app)
    assert (adoption.item_id, adoption.action, adoption.fields) == (made.id, "new", ["organization", "role", "period", "actions"])
    assert one(app, import_id, "work").state == CandidateState.ADOPTED
    # Nothing else in the library moved.
    after = library_rows(app)
    assert {key: [row for row in value if made.id not in (row.get("id"), row.get("item_id"))] for key, value in after.items()} == before
    card = browser_a.get(f"/imports/{import_id}/review").text
    assert f'作为新的经历草稿加入：<a href="/experiences/{made.id}">合成数据科技有限公司</a>' in card
    assert "写入的栏目：组织、岗位或角色、时间、做了什么。" in card and "已加入 1 条" in card


def test_a_like_name_comes_first_but_is_never_picked_and_another_can_still_be_added(app, browser_a):
    other = browser_a.create(category="work", organization="另一家合成公司")
    same = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = browser_a.get(adopt_url(import_id, work.id)).text
    radios = [form for form in forms_in(page) if form.action.endswith("/adopt/fields")]

    assert page.index(f'value="{same}"') < page.index(f'value="{other}"')
    assert "名称相同或相近" in page and " checked" not in page.split("target-list")[1]
    assert radios and "target" not in radios[0].fields
    before = (item(app, same).content, item(app, same).revision, versions(app, same))

    adopt_new(browser_a, import_id, work.id)

    assert len(items(app, "work")) == 3
    assert (item(app, same).content, item(app, same).revision, versions(app, same)) == before


# --- writing chosen fields into an experience -------------------------------------------------------------


def test_supplementing_writes_only_the_chosen_fields_and_keeps_the_rest(app, browser_a):
    target = work_record(browser_a)
    old = item(app, target)
    old_versions = versions(app, target)
    with rt(app).database.transaction() as db:
        attachments = [(row.id, row.item_id, row.state, row.deleted_at) for row in db.scalars(select(Attachment))]
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = browser_a.get(fields_url(import_id, work.id, target)).text

    assert "目前是“已确认”" in page and "补充后会变回待核对的草稿" in page
    assert "和现在的内容相同，不需要处理" in page and 'value="keep" checked' in page

    response = write_fields(browser_a, import_id, work.id, target, role="replace", period="fill", actions="append")

    assert response.headers["location"] == f"/imports/{import_id}/review?notice=adopted#c-{work.id}"
    new = item(app, target)
    assert new.content == {
        "organization": "合成数据科技有限公司",
        "role": "数据分析实习生",
        "period": "2025.06 - 2025.09",
        "actions": "整理周报\n参与搭建销售看板，团队共 4 人\n负责每周的数据清洗\n没有使用 Docker，部署由同事完成",
        "outcome": "",
        "reflection": "",
    }
    # The note, the attachment and the history stay; the confirmation no longer stands for the new content.
    assert new.private_note == "私有备注-合成" and new.state == "draft"
    assert new.last_confirmed_at == old.last_confirmed_at and versions(app, target) == old_versions
    with rt(app).database.transaction() as db:
        assert [(row.id, row.item_id, row.state, row.deleted_at) for row in db.scalars(select(Attachment))] == attachments
    assert actions_of(app, target)[-1] == "supplemented_from_import"
    (adoption,) = adoptions(app)
    assert (adoption.action, adoption.fields) == ("supplement", ["role", "period", "actions"])
    card = browser_a.get(f"/imports/{import_id}/review").text
    assert "补充到已有经历：" in card and "写入的栏目：岗位或角色、时间、做了什么。" in card


def test_nothing_chosen_or_a_choice_the_page_never_offered_writes_nothing(app, browser_a):
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    old = (item(app, target).content, item(app, target).revision, item(app, target).state, versions(app, target))
    action = f"{adopt_url(import_id, work.id)}/fields"
    form = browser_a.form(fields_url(import_id, work.id, target), action)

    nothing = browser_a.post(action, form.fields)
    same_field = browser_a.post(action, {**form.fields, "choice-organization": "replace"})
    unknown = browser_a.post(action, {**form.fields, "choice-private_note": "replace"})
    odd = browser_a.post(action, {**form.fields, "choice-role": "overwrite"})

    assert nothing.status_code == 422 and "还没有选择要写入的栏目，所以什么都没有写入" in nothing.text
    assert (same_field.status_code, unknown.status_code, odd.status_code) == (400, 400, 400)
    assert (item(app, target).content, item(app, target).revision, item(app, target).state, versions(app, target)) == old
    assert one(app, import_id, "work").state == CandidateState.PENDING and adoptions(app) == []


def test_appending_past_the_field_limit_is_refused_and_the_choices_stay(app, browser_a):
    target = work_record(browser_a, actions="合" * 4990)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    revision = item(app, target).revision

    response = write_fields(browser_a, import_id, work.id, target, role="replace", actions="append")

    assert response.status_code == 422 and "最多 5000 个字" in response.text
    assert 'name="choice-actions" value="append" checked' in response.text
    assert 'name="choice-role" value="replace" checked' in response.text
    # Not written, so still unsaved on the page (Codex U2B2B-F2).
    assert "data-leave-guard data-initially-dirty" in response.text
    assert item(app, target).revision == revision and adoptions(app) == []


@pytest.mark.parametrize("changed", ["target", "candidate"])
def test_a_page_older_than_the_record_or_the_candidate_writes_nothing(app, browser_a, changed):
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    action = f"{adopt_url(import_id, work.id)}/fields"
    stale = browser_a.form(fields_url(import_id, work.id, target), action)

    if changed == "target":
        browser_a.submit(f"/experiences/{target}/edit", f"/experiences/{target}/draft", role="别处改的角色")
    else:
        edit = f"/imports/{import_id}/candidates/{work.id}/edit"
        browser_a.submit(edit, edit, role="候选改过的角色")

    revision = item(app, target).revision
    response = browser_a.post(action, {**stale.fields, "choice-role": "replace", "choice-period": "fill"})

    expected = "为了不覆盖" if changed == "target" else "这条候选在你打开这个页面之后改过"
    assert response.status_code == 409 and expected in response.text
    assert item(app, target).revision == revision and adoptions(app) == []
    # Shown again as it is now, with the choices that still apply.
    assert 'name="choice-period" value="fill" checked' in response.text
    assert ("别处改的角色" if changed == "target" else "候选改过的角色") in response.text


def test_the_field_page_guards_choices_that_are_not_written_yet(app, browser_a):
    """Codex U2B2B-F2. What a real browser does with the guard is checked in one (see the B2b report, R1)."""
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    action = f"{adopt_url(import_id, work.id)}/fields"
    page = browser_a.get(fields_url(import_id, work.id, target)).text
    script = Browser(app).get("/static/leave_guard.js").text

    # Guarded with nothing unsaved yet. Leaving offers to keep checking or to drop the choices: no save, no write.
    assert 'class="adopt-fields" data-leave-guard>' in page and '<script src="/static/leave_guard.js"></script>' in page
    assert ">继续核对</button>" in page and ">放弃选择并离开</button>" in page
    assert 'data-leave-choice="save"' not in page and "data-save-button" not in page
    # The shared guard follows radio buttons too, not only typed text.
    assert 'form.querySelectorAll("input[type=radio]")' in script and 'form.addEventListener("change", showState)' in script

    nothing = browser_a.post(action, browser_a.form(fields_url(import_id, work.id, target), action).fields)
    with refused(app, "UPDATE import_candidates"):
        failed = write_fields(browser_a, import_id, work.id, target, role="replace")
    stale = browser_a.form(fields_url(import_id, work.id, target), action)
    browser_a.submit(f"/experiences/{target}/edit", f"/experiences/{target}/draft", role="别处改的角色")
    conflict = browser_a.post(action, {**stale.fields, "choice-period": "fill"})

    # Nothing was chosen, so nothing waits to be written.
    assert nothing.status_code == 422 and 'class="adopt-fields" data-leave-guard>' in nothing.text
    # Choices sent but not written are still unsaved on the page shown again, not its new starting point.
    assert failed.status_code == 500 and 'name="choice-role" value="replace" checked' in failed.text
    assert conflict.status_code == 409 and 'name="choice-period" value="fill" checked' in conflict.text
    assert all('class="adopt-fields" data-leave-guard data-initially-dirty>' in shown.text for shown in (failed, conflict))
    assert adoptions(app) == [] and one(app, import_id, "work").state == CandidateState.PENDING


# --- the profile and the user's own skills ----------------------------------------------------------------


def test_a_profile_is_made_from_the_chosen_fields_only(app, browser_a):
    import_id = imported(app, browser_a)
    candidate = one(app, import_id, "profile")
    page = browser_a.get(fields_url(import_id, candidate.id, "profile")).text

    assert "你还没有基本资料；选中的栏目会建立一份新的基本资料" in page
    response = write_fields(browser_a, import_id, candidate.id, "profile", name="fill", email="fill")

    assert "notice=adopted" in response.headers["location"]
    made = profile(app)
    assert {name: value for name, value in made.content.items() if value} == {
        "name": "林合成",
        "email": "lin.synthetic@example.com",
    }
    assert [kind for _, kind, *_ in versions(app, made.id)] == ["saved"]
    assert actions_of(app, made.id) == ["created", "adopted_from_import"]


def test_an_existing_profile_keeps_its_details_unless_chosen(app, browser_a):
    save_profile(browser_a, phone="139 1111 2222", city="杭州")
    old = profile(app)
    import_id = imported(app, browser_a)
    candidate = one(app, import_id, "profile")
    page = browser_a.get(fields_url(import_id, candidate.id, "profile")).text

    assert "139 1111 2222" in page and "和现在的内容相同，不需要处理" in page
    write_fields(browser_a, import_id, candidate.id, "profile", email="fill")

    new = profile(app)
    assert new.id == old.id and new.content["phone"] == "139 1111 2222" and new.content["email"] == "lin.synthetic@example.com"
    assert new.content["name"] == "" and len(versions(app, new.id)) == len(versions(app, old.id)) == 2
    assert actions_of(app, new.id)[-1] == "supplemented_from_import"


def test_skills_are_kept_as_the_users_own_words_in_the_profile(app, browser_a):
    import_id = imported(app, browser_a)
    skills = one(app, import_id, "skills")
    page = browser_a.get(adopt_url(import_id, skills.id))

    assert page.headers["location"] == fields_url(import_id, skills.id, "profile")
    page = browser_a.get(page.headers["location"]).text
    assert "保存为自述技能" in page and "岗位分析也不会把它当作做过的证据" in page

    write_fields(browser_a, import_id, skills.id, "profile", text="fill")

    made = profile(app)
    assert made.content["skills"] == "Python、SQL、Tableau" and made.content["name"] == ""
    profile_page = browser_a.get("/basic-profile").text
    assert "自述技能" in profile_page and "Python、SQL、Tableau" in profile_page
    # It is edited, kept in the history and taken away like the rest of the profile.
    assert save_profile(browser_a, skills="Python、SQL、Tableau\nExcel").status_code == 303
    assert len(versions(app, made.id)) == 2
    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    profile_md = next(archive.read(name) for name in archive.namelist() if name.endswith("/profile.md")).decode("utf-8")
    assert "**自述技能**\n\nPython、SQL、Tableau\nExcel" in profile_md
    # It never becomes an experience.
    assert all(items(app, kind) == [] for kind in EXPERIENCE_CATEGORIES)


def test_the_profile_candidate_contract_is_as_before_and_mappings_cannot_be_forged(app, browser_a):
    import_id = imported(app, browser_a)
    candidate, work, skills, intention = (one(app, import_id, kind) for kind in ("profile", "work", "skills", "intention"))
    education = browser_a.create(category="education", school="合成大学")
    action = f"{adopt_url(import_id, candidate.id)}/fields"
    form = browser_a.form(fields_url(import_id, candidate.id, "profile"), action)

    # A structurer cannot fill the new profile field.
    answer = structure([{"kind": "text", "number": 1, "text": "姓名：林合成\n\n专业技能\nPython"}])
    assert all("skills" not in found["fields"] for found in answer if found["kind"] == "profile")
    forged_answer = json.dumps({"candidates": [{"kind": "profile", "fields": {"skills": [{"segment": 1, "quote": "Python"}]}}]})
    assert check_answer(forged_answer, [{"kind": "text", "number": 1, "text": "Python"}]).problems[0]["reason"] == UNKNOWN_FIELD
    # Nor can a post write a field, a kind or a target the pages never offer.
    assert browser_a.post(action, {**form.fields, "choice-skills": "fill"}).status_code == 400
    work_action = f"{adopt_url(import_id, work.id)}/fields"
    work_form = {"csrf_token": browser_a.csrf_token(), "expected_revision": "1"}
    assert browser_a.post(work_action, {**work_form, "target": education, "expected_target_revision": "1"}).status_code == 409
    assert browser_a.post(work_action, {**work_form, "target": "profile", "expected_target_revision": "none"}).status_code == 409
    skills_new = browser_a.post(f"{adopt_url(import_id, skills.id)}/new", {**work_form, "expected_revision": "1"})
    intention_page = browser_a.get(adopt_url(import_id, intention.id))
    assert skills_new.status_code == 409 and intention_page.status_code == 409 and "只供参考" in intention_page.text
    assert profile(app) is None and adoptions(app) == [] and len(items(app, "education")) == 1


def test_profiles_saved_before_the_new_field_stay_as_they_were(app, browser_a):
    save_profile(browser_a, name="旧资料-合成")
    old = profile(app)
    # As a profile saved before 自述技能 existed: no key for it, in the record or its version.
    with rt(app).database.transaction() as db:
        stored = db.get(ExperienceItem, old.id)
        stored.content = {key: value for key, value in stored.content.items() if key != "skills"}
        for version in db.scalars(select(ExperienceVersion).where(ExperienceVersion.item_id == old.id)):
            version.content = {key: value for key, value in version.content.items() if key != "skills"}
    before = (item(app, old.id).revision, versions(app, old.id))

    page = browser_a.get("/basic-profile").text
    unchanged = save_profile(browser_a)

    assert "自述技能" in page and "未填写" in page
    assert unchanged.headers["location"] == "/basic-profile?notice=profile_unchanged"
    assert (item(app, old.id).revision, versions(app, old.id)) == before
    assert "skills" not in item(app, old.id).content
    # A real change is kept as a new version; the old one is not rewritten.
    save_profile(browser_a, skills="SQL")
    assert versions(app, old.id)[:1] == before[1] and len(versions(app, old.id)) == 2


def test_two_first_writes_into_the_profile_make_one_profile(app, browser_a):
    import_id = imported(app, browser_a)
    candidate, skills = one(app, import_id, "profile"), one(app, import_id, "skills")
    first_action = f"{adopt_url(import_id, candidate.id)}/fields"
    second_action = f"{adopt_url(import_id, skills.id)}/fields"
    first = browser_a.form(fields_url(import_id, candidate.id, "profile"), first_action)
    second = browser_a.form(fields_url(import_id, skills.id, "profile"), second_action)

    assert first.fields["expected_target_revision"] == second.fields["expected_target_revision"] == "none"
    browser_a.post(first_action, {**first.fields, "choice-name": "fill"})
    refused_page = browser_a.post(second_action, {**second.fields, "choice-text": "fill"})

    assert refused_page.status_code == 409 and "为了不覆盖" in refused_page.text
    made = profile(app)
    assert made.content["name"] == "林合成" and made.content["skills"] == ""
    # Saving again from the page shown now writes into that one profile.
    again = form_for(refused_page.text, second_action)
    assert again.fields["expected_target_revision"] == str(made.revision)
    browser_a.post(second_action, {**again.fields, "choice-text": "fill"})
    with rt(app).database.transaction() as db:
        assert len(list(db.scalars(select(ExperienceItem).where(ExperienceItem.category == PROFILE)))) == 1
    assert profile(app).content["skills"] == "Python、SQL、Tableau" and profile(app).content["name"] == "林合成"


@pytest.mark.parametrize(
    ("kind", "field", "stored_as", "value"),
    [("profile", "name", "name", "林合成"), ("skills", "text", "skills", "Python、SQL、Tableau")],
)
@pytest.mark.parametrize("since", ["edited", "deleted", "made_again"])
def test_a_page_for_a_profile_that_is_no_longer_the_one_shown_writes_nothing(
    app, browser_a, kind, field, stored_as, value, since
):
    """Codex U2B2B-F1: a profile deleted and made again starts at revision 1 again, like the one the page showed."""
    assert save_profile(browser_a, name="原资料-合成", skills="原自述-合成").status_code == 303
    shown = profile(app)
    import_id = imported(app, browser_a)
    candidate = one(app, import_id, kind)
    action = f"{adopt_url(import_id, candidate.id)}/fields"
    stale = browser_a.form(fields_url(import_id, candidate.id, "profile"), action)
    assert (stale.fields["expected_target_id"], stale.fields["expected_target_revision"]) == (shown.id, "1")

    if since == "edited":
        save_profile(browser_a, name="别处改的资料-合成", skills="别处改的自述-合成")
    else:
        browser_a.submit(f"/experiences/{shown.id}/delete", f"/experiences/{shown.id}/delete")

        if since == "made_again":
            save_profile(browser_a, name="重建资料-合成", skills="重建自述-合成")

    now = profile(app)
    assert since != "made_again" or (now.id != shown.id and now.revision == shown.revision == 1)
    before = library_rows(app)

    response = browser_a.post(action, {**stale.fields, f"choice-{field}": "replace"})

    assert response.status_code == 409
    assert ("为了不覆盖" if since == "edited" else "当时的基本资料已经删除") in response.text
    # Nothing at all changed: the profile there now (or none), its versions and history, the one in the recycle bin.
    assert library_rows(app) == before
    assert one(app, import_id, kind).state == CandidateState.PENDING and adoptions(app) == []
    # Shown again for the profile there now; the choice stays where it still applies, guarded as not written.
    again = form_for(response.text, action)
    assert again.fields["expected_target_id"] == (now.id if now else "")
    assert again.fields["expected_target_revision"] == (str(now.revision) if now else "none")
    kept = f'name="choice-{field}" value="replace" checked' in response.text
    assert kept == (now is not None) and ("data-initially-dirty" in response.text) == kept
    # Only a new, explicit post writes, and only into the profile there now.
    written = browser_a.post(action, {**again.fields, f"choice-{field}": "replace" if now else "fill"})
    assert "notice=adopted#" in written.headers["location"]
    (adoption,) = adoptions(app)
    assert adoption.item_id == profile(app).id and (now is None or adoption.item_id == now.id)
    assert profile(app).content[stored_as] == value


def test_a_profile_post_must_name_the_very_profile_it_showed(app, browser_a, browser_b):
    save_profile(browser_a, name="原资料-合成")
    save_profile(browser_b, name="B 的资料-合成")
    mine = profile(app)
    (theirs,) = [found for found in items(app, PROFILE) if found.id != mine.id]
    experience = browser_a.create(title="合成经历")
    import_id = imported(app, browser_a)
    candidate = one(app, import_id, "profile")
    action = f"{adopt_url(import_id, candidate.id)}/fields"
    chosen = {**browser_a.form(fields_url(import_id, candidate.id, "profile"), action).fields, "choice-name": "replace"}
    before = library_rows(app)

    another_users = browser_a.post(action, {**chosen, "expected_target_id": theirs.id})
    an_experience = browser_a.post(action, {**chosen, "expected_target_id": experience})
    no_id = browser_a.post(action, {**chosen, "expected_target_id": ""})
    an_id_and_no_profile = browser_a.post(action, {**chosen, "expected_target_revision": "none"})

    assert (another_users.status_code, an_experience.status_code) == (409, 409)
    assert "B 的资料-合成" not in another_users.text
    assert (no_id.status_code, an_id_and_no_profile.status_code) == (400, 400)
    assert library_rows(app) == before and adoptions(app) == []
    assert one(app, import_id, "profile").state == CandidateState.PENDING


def test_self_described_skills_never_reach_the_job_analysis(app, browser_a):
    sentinel = f"自述技能哨兵 Docker {CANARY}"
    save_profile(browser_a, skills=sentinel)
    app.state.job_analysis.executor = InlineExecutor()
    recorder = use_provider(app, RecordingProvider())
    experience = confirmed(browser_a, title="订单看板（合成）", actions="用 Python 清洗订单数据。")
    task = task_of(review(browser_a, [experience]))
    preview = browser_a.get(task).text

    assert sentinel not in preview and CANARY not in preview
    assert start(browser_a, task).status_code == 303
    stored = app.state.job_analysis.store._tasks[task.rsplit("/", 1)[1]]
    assert CANARY not in "".join(recorder.prompts) and CANARY not in repr(stored.request)
    docker = [finding for finding in stored.result.findings if "Docker" in finding.text]
    # Only the user's own words name Docker: that is no support from experience.
    assert docker and all(finding.conclusion != "supported" for finding in docker)


# --- once, and only with every check ---------------------------------------------------------------------


def test_a_second_submit_or_another_tab_writes_once(app, browser_a):
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    url = adopt_url(import_id, work.id)
    new_form = browser_a.form(url, f"{url}/new")
    fields_action = f"{url}/fields"
    other_tab = browser_a.form(fields_url(import_id, work.id, target), fields_action)
    before = (item(app, target).content, item(app, target).revision)

    first = browser_a.post(f"{url}/new", new_form.fields)
    second = browser_a.post(f"{url}/new", new_form.fields)
    from_other_tab = browser_a.post(fields_action, {**other_tab.fields, "choice-role": "replace"})

    assert "notice=adopted#" in first.headers["location"]
    assert "notice=adopted_already" in second.headers["location"] and "notice=adopted_already" in from_other_tab.headers["location"]
    assert len(items(app, "work")) == 2 and len(adoptions(app)) == 1
    assert (item(app, target).content, item(app, target).revision) == before
    assert "这条之前已经加入过，这次没有再写入" in browser_a.get(second.headers["location"]).text


def test_requests_at_the_same_moment_write_once(app, browser_a, clock):
    import_id = imported(app, browser_a)
    owner = owner_id(app)
    work, candidate, skills = (one(app, import_id, kind) for kind in ("work", "profile", "skills"))
    gate = threading.Barrier(2)

    def new_draft():
        gate.wait()
        return adopt_as_new(rt(app).database, owner, import_id, work.id, work.revision, clock.now())

    def first_profile(kind):
        found = candidate if kind == "profile" else skills
        choice = {"name": "fill"} if kind == "profile" else {"text": "fill"}
        gate.wait()
        try:
            return into_profile(rt(app).database, owner, import_id, found.id, found.revision, None, choice, clock.now())
        except AdoptionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        drafts = [future.result(timeout=20) for future in [pool.submit(new_draft) for _ in range(2)]]
    gate.reset()
    with ThreadPoolExecutor(max_workers=2) as pool:
        profiles = [future.result(timeout=20) for future in [pool.submit(first_profile, kind) for kind in ("profile", "skills")]]

    assert sorted(outcome.written for outcome in drafts) == [False, True] and len(items(app, "work")) == 1
    assert drafts[0].item_id == drafts[1].item_id
    # One made the profile; the other found it there and wrote nothing over it.
    assert profiles.count("conflict") == 1 and len(items(app, PROFILE)) == 1
    assert len(adoptions(app)) == 2


def test_a_repeat_still_meets_every_check(app, browser_a, browser_b, clock):
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    url = adopt_url(import_id, work.id)
    form = browser_a.form(url, f"{url}/new")
    adopt_new(browser_a, import_id, work.id)

    assert browser_a.post(f"{url}/new", {"expected_revision": "1"}).status_code == 403
    assert browser_b.post(f"{url}/new", {**form.fields, "csrf_token": browser_b.csrf_token()}).status_code == 404
    assert Browser(app).post(f"{url}/new", form.fields).status_code == 401
    clock.advance(days=IMPORT_WINDOW.days)
    browser_a.sign_in("合成用户 A")
    late = browser_a.post(f"{url}/new", {**form.fields, "csrf_token": browser_a.csrf_token()})
    assert late.status_code == 404 and "找不到这次导入" in late.text
    assert len(items(app, "work")) == 1


def test_left_out_adopted_or_gone_targets_write_nothing(app, browser_a):
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work, award = one(app, import_id, "work"), one(app, import_id, "award")
    review_page = f"/imports/{import_id}/review"
    browser_a.submit(review_page, f"/imports/{import_id}/candidates/{award.id}/exclude")
    stale = browser_a.form(fields_url(import_id, work.id, target), f"{adopt_url(import_id, work.id)}/fields")
    browser_a.submit(f"/experiences/{target}/delete", f"/experiences/{target}/delete")

    left_out = browser_a.post(
        f"{adopt_url(import_id, award.id)}/new",
        {"csrf_token": browser_a.csrf_token(), "expected_revision": str(one(app, import_id, "award").revision)},
    )
    gone = browser_a.post(f"{adopt_url(import_id, work.id)}/fields", {**stale.fields, "choice-role": "replace"})

    assert left_out.status_code == 409 and "这条已标为不加入" in left_out.text
    assert gone.status_code == 409 and "要写入的资料已经不在了" in gone.text
    assert adoptions(app) == [] and item(app, target).deleted_at is not None
    # Once adopted, a candidate is not edited, left out or taken back any more.
    adopt_new(browser_a, import_id, work.id)
    edit = browser_a.get(f"/imports/{import_id}/candidates/{work.id}/edit")
    exclude = browser_a.post(
        f"/imports/{import_id}/candidates/{work.id}/exclude", {"csrf_token": browser_a.csrf_token(), "expected_revision": "2"}
    )
    assert edit.status_code == exclude.status_code == 409 and "这条已经加入" in edit.text and "这条已经加入" in exclude.text
    assert one(app, import_id, "work").state == CandidateState.ADOPTED


@pytest.mark.parametrize(
    ("way", "statement"),
    [
        ("new", "INSERT INTO import_adoptions"),
        ("supplement", "UPDATE import_candidates"),
        ("profile", "INSERT INTO import_adoptions"),
    ],
)
def test_a_failure_anywhere_in_the_write_undoes_all_of_it_and_can_be_retried(app, browser_a, caplog, way, statement):
    target = work_record(browser_a) if way == "supplement" else None
    import_id = imported(app, browser_a)
    candidate = one(app, import_id, "profile" if way == "profile" else "work")
    before = library_rows(app)

    with refused(app, statement):
        if way == "new":
            failed = adopt_new(browser_a, import_id, candidate.id)
        else:
            failed = write_fields(browser_a, import_id, candidate.id, target or "profile", **{"role" if target else "name": "replace" if target else "fill"})

    assert failed.status_code == 500 and "没有写入：写入数据库时出错" in failed.text
    assert library_rows(app) == before and adoptions(app) == [] and profile(app) is None
    assert one(app, import_id, candidate.kind).state == CandidateState.PENDING
    assert "OperationalError" in caplog.text and "林合成" not in caplog.text
    if way != "new":
        assert 'checked' in failed.text and ('value="replace" checked' in failed.text or 'value="fill" checked' in failed.text)

    # The disk works again: the same page writes, once.
    if way == "new":
        retried = browser_a.post(f"{adopt_url(import_id, candidate.id)}/new", form_for(failed.text, f"{adopt_url(import_id, candidate.id)}/new").fields)
    else:
        action = f"{adopt_url(import_id, candidate.id)}/fields"
        retried = browser_a.post(action, form_for(failed.text, action).fields)

    assert "notice=adopted#" in retried.headers["location"] and len(adoptions(app)) == 1


# --- after: the end of the import, deleted records, provenance and export ----------------------------------------


@pytest.mark.parametrize("ending", ["discard", "expire"])
def test_what_was_adopted_stays_when_the_import_ends(app, browser_a, clock, ending):
    target = browser_a.create(category="education", school="合成大学")
    import_id = imported(app, browser_a)
    work, education, skills, award = (one(app, import_id, kind) for kind in ("work", "education", "skills", "award"))
    adopt_new(browser_a, import_id, work.id)
    write_fields(browser_a, import_id, education.id, target, major="fill")
    write_fields(browser_a, import_id, skills.id, "profile", text="fill")
    browser_a.submit(f"/imports/{import_id}/review", f"/imports/{import_id}/candidates/{award.id}/exclude")
    kept = library_rows(app)

    if ending == "discard":
        browser_a.submit(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")
    else:
        clock.advance(days=IMPORT_WINDOW.days)
        rt(app).run_cleanup()

    assert rows(app, import_id) == [] and adoptions(app) == []
    # Every record, version, attachment and record event is as it was (the import's own events aside).
    assert library_rows(app) == kept
    assert item(app, target).content["major"] == "计算机科学与技术" and profile(app).content["skills"] == "Python、SQL、Tableau"


def test_deleting_or_purging_a_target_is_never_held_back_and_brings_nothing_back(app, browser_a, clock):
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    url = adopt_url(import_id, work.id)
    new_form = browser_a.form(url, f"{url}/new")
    browser_a.post(f"{url}/new", new_form.fields)
    (made,) = items(app, "work")
    browser_a.submit(f"/experiences/{made.id}/delete", f"/experiences/{made.id}/delete")

    card = browser_a.get(f"/imports/{import_id}/review").text
    assert "对应的经历已经删除（在回收站或已永久删除），这里不再提供链接" in card and f'href="/experiences/{made.id}"' not in card
    # Asking again (the old page) neither writes a new one nor offers the deleted one as a success.
    assert "notice=adopted_already" in browser_a.post(f"{url}/new", new_form.fields).headers["location"]
    assert browser_a.get(url).headers["location"].endswith("notice=adopted_already#c-" + work.id)
    with rt(app).database.transaction() as db:
        job = request_experience_purge(db, owner_id(app), made.id, item(app, made.id).revision, clock.now())
    run_purge_job(rt(app).database, job, clock.now())

    assert item(app, made.id) is None and len(adoptions(app)) == 1
    record = json.loads(browser_a.get(f"/imports/{import_id}/candidates.json").content)
    (entry,) = [entry for entry in record["candidates"] if entry["id"] == work.id]
    assert entry["adoption"]["item_gone"] is True and entry["adoption"]["item_title"] is None
    assert items(app, "work") == []


def test_provenance_is_a_note_in_the_records_history_without_any_words(app, browser_a):
    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work, project = one(app, import_id, "work"), one(app, import_id, "project")
    write_fields(browser_a, import_id, work.id, target, period="fill")
    adopt_new(browser_a, import_id, project.id)
    (made,) = items(app, "project")
    history = browser_a.get(f"/experiences/{made.id}/history").text

    assert "从一次简历导入加入（内容未经核实）" in history
    assert "用一次简历导入的内容补充了所选栏目（未经核实）" in browser_a.get(f"/experiences/{target}/history").text
    with rt(app).database.transaction() as db:
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.action.like("%from_import"))))
    assert {(event.item_id, event.object_id) for event in events} == {(target, target), (made.id, made.id)}

    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}
    data = json.loads(files["data.json"])
    by_id = {record["id"]: record for record in data["experiences"]}
    assert [entry["event"] for entry in by_id[made.id]["import_provenance"]] == ["adopted_from_import"]
    assert set(by_id[made.id]["import_provenance"][0]) == {"event", "label", "at"}
    assert by_id[target]["import_provenance"][0]["event"] == "supplemented_from_import"
    single = browser_a.get(f"/experiences/{made.id}/export.md").text
    assert "来源提示：" in single and "从一次简历导入加入（内容未经核实）" in single
    candidates = json.loads(next(payload for path, payload in files.items() if path.endswith("candidates.json")))
    (entry,) = [entry for entry in candidates["candidates"] if entry["id"] == work.id]
    assert entry["state"] == "adopted" and entry["adoption"]["action"] == "supplement"
    assert entry["adoption"]["item_id"] == target and entry["adoption"]["fields"] == ["时间"]
