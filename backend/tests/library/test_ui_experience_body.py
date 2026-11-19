"""UI bilingual batch: one "经历内容" field for an experience, with the records of before kept as they are (plan 5.2).

A new experience is written in one body; a record of the older split fields is read with their own labels, offered in
the form as one body (blank lines between, never a system label), and written in the new form only when the user saves
it: its older versions stay as they were. A plan, someone else's work or a "never did" keeps its sentence, so the
evidence rules read it as before. The structuring contract of resume imports names no body field.
"""
import re

from sqlalchemy import select

from app.library.candidates import CANDIDATE_FIELDS
from app.library.content import BODY, BODY_SOURCES, CATEGORY_FIELDS, ExperienceInput, compose_body
from app.library.models import ExperienceItem, ExperienceVersion, User
from app.library.service import confirm_experience, create_experience
from job_helpers import confirmed, text_of
from library_helpers import form_for

LEGACY = {
    "title": "旧格式项目（合成）",
    "actions": "我用 Python 清洗了订单数据。",
    "outcome": "计划下学期学习 SQL。",
    "reflection": "组员负责搭建模型。",
}


def owner_of(app, label="合成用户 A") -> str:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(User.id).where(User.display_label == label))


def legacy_item(app, browser) -> str:
    """A project written in the older split fields, confirmed, as records of before this batch are."""
    browser.get("/workspace")
    owner = owner_of(app)
    rt = app.state.library

    with rt.database.transaction() as db:
        content = {field.name: "" for field in CATEGORY_FIELDS["project"]}
        content.update(LEGACY)
        item = create_experience(db, owner, ExperienceInput(content=content, private_note="", category="project"), rt.now())
        item_id = item.id

    assert browser.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303

    return item_id


def test_a_new_experience_is_written_in_one_body(app, browser_a):
    form = form_for(browser_a.get("/experiences/new?kind=project").text, "/experiences")

    assert BODY in form.fields and not any(name in form.fields for name in BODY_SOURCES["project"])
    item = confirmed(browser_a, title="新格式项目（合成）", body="我用 SQL 汇总了每周销量。")

    with app.state.library.database.transaction() as db:
        content = db.get(ExperienceItem, item).content

    assert content[BODY] == "我用 SQL 汇总了每周销量。" and not any(content.get(name) for name in BODY_SOURCES["project"])


def test_a_record_of_before_is_read_with_its_own_labels(app, browser_a):
    item = legacy_item(app, browser_a)
    page = text_of(browser_a.get(f"/experiences/{item}").text)

    for text in LEGACY.values():
        assert text in page


def test_editing_a_record_of_before_offers_one_body_and_saving_a_change_writes_the_new_form(app, browser_a):
    item = legacy_item(app, browser_a)
    form = form_for(browser_a.get(f"/experiences/{item}/edit").text, f"/experiences/{item}/draft")
    offered = form.fields[BODY]

    assert offered == compose_body(LEGACY, "project")
    assert "\n\n" in offered and "做了什么" not in offered

    with app.state.library.database.transaction() as db:
        before = [version.content for version in db.scalars(select(ExperienceVersion).where(ExperienceVersion.item_id == item))]

    changed = offered + "\n\n后来又补了一张图表。"
    assert browser_a.post(form.action, {**form.fields, BODY: changed}).status_code == 303

    with app.state.library.database.transaction() as db:
        current = db.get(ExperienceItem, item).content
        versions = {version.id: version.content for version in db.scalars(select(ExperienceVersion).where(ExperienceVersion.item_id == item))}

    assert current[BODY] == changed and not any(current.get(name) for name in BODY_SOURCES["project"])
    # The older versions are as they were: nothing of before is rewritten.
    assert all(content in versions.values() for content in before)


def test_saving_a_record_of_before_as_it_was_shown_changes_nothing(app, browser_a):
    item = legacy_item(app, browser_a)
    form = form_for(browser_a.get(f"/experiences/{item}/edit").text, f"/experiences/{item}/draft")

    with app.state.library.database.transaction() as db:
        record = db.get(ExperienceItem, item)
        before = (dict(record.content), record.revision, record.state)

    # The form sends the body as it was offered and none of the older fields. Nothing was changed, so nothing is
    # written: no new revision, still confirmed, and an analysis or exploration that cites its fields still finds
    # the same text there.
    response = browser_a.post(form.action, form.fields)

    assert response.status_code == 303 and response.headers["location"].endswith("notice=unchanged")

    with app.state.library.database.transaction() as db:
        record = db.get(ExperienceItem, item)

        assert (record.content, record.revision, record.state) == before

    # Only the joining is forgiven: a word changed anywhere is a change.
    edited = form.fields[BODY].replace("Python", "Excel")
    assert browser_a.post(form.action, {**form.fields, BODY: edited}).headers["location"].endswith("notice=saved")


def test_only_viewing_a_record_of_before_changes_nothing(app, browser_a):
    item = legacy_item(app, browser_a)

    with app.state.library.database.transaction() as db:
        before = dict(db.get(ExperienceItem, item).content)

    browser_a.get(f"/experiences/{item}")
    browser_a.get(f"/experiences/{item}/edit")

    with app.state.library.database.transaction() as db:
        assert db.get(ExperienceItem, item).content == before


def test_composing_keeps_each_sentence_whole():
    body = compose_body(LEGACY, "project")

    # Each older field stays its own paragraph: a plan or someone else's work never runs into the user's own doing.
    assert body.split("\n\n") == [LEGACY["actions"], LEGACY["outcome"], LEGACY["reflection"]]


def test_the_structuring_contract_names_no_body():
    assert all(BODY not in fields for fields in CANDIDATE_FIELDS.values())


def test_a_body_over_its_limit_is_refused_not_cut(browser_a):
    limit = next(field.max_length for field in CATEGORY_FIELDS["project"] if field.name == BODY)
    form = form_for(browser_a.get("/experiences/new?kind=project").text, "/experiences")
    response = browser_a.post(form.action, {**form.fields, "title": "超长（合成）", BODY: "字" * (limit + 1)})

    assert response.status_code == 422
