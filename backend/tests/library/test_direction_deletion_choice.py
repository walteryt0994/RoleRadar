"""U3-C3b A: deleting along, from a record's or a version's deletion page, the saved explorations resting on it.

By default only what the page names is deleted. The page lists the saved explorations in use resting on it and
offers, unticked, to move them to the recycle bin with it, freezing each as "<id>.<revision>". Only when the user ticks
it are they taken, and only as listed: read again in the deletion's own transaction (the user's, in use, resting on
it, at the revision shown), any difference refuses the whole deletion and shows the page again as things are now,
the choice unticked (C precheck, 19.2). What came to rest on it after the page was shown is never taken, and no
decision is ever deleted. Restoring brings back what was taken; purging takes it for good, and the pages say so. All
material is synthetic and nothing leaves the process.
"""
import re
import uuid

import pytest
from sqlalchemy import select

from app.directions import records
from app.library.models import DirectionDecision, DirectionExploration, ExperienceItem, ExperienceVersion
from app.library.references import BasisState, decision_state
from app.library.web import ALONG_CHANGED
from job_helpers import confirmed, text_of
from library_helpers import form_for
from test_direction_pages import BASE as DIRECTIONS, decide, directions, explored, inline, library, no_service_client, saved_id
from test_direction_records import direction, quote, trusted

PROJECT = dict(title="校园订单看板（合成）", actions="用 Python 清洗订单数据，并用 SQL 汇总每周销量。", outcome="看板在课程展示中使用。")
OTHER = dict(title="社团招新问卷（合成）", actions="整理问卷结果，写成一页总结。")
PREFS = dict(directions_ideal="数据分析", work_content_ideal="希望多分析数据，少做重复事务", cities_ideal="北京")
NAMES = ("数据分析", "产品运营", "用户研究")


# --- building what the server would build ---------------------------------------------------------------------


def database_of(app):
    return app.state.library.database


def owner_and_version(app, item_id):
    with database_of(app).transaction() as db:
        item = db.get(ExperienceItem, item_id)

        return item.owner_id, item.confirmed_version_id


def preferences_of(app, browser):
    """User A's preferences, saved through the pages: their id, owner and latest saved version."""
    assert browser.submit("/job-preferences/edit", "/job-preferences", **PREFS).status_code == 303

    with database_of(app).transaction() as db:
        item = db.scalar(select(ExperienceItem).where(ExperienceItem.category == "preferences", ExperienceItem.deleted_at.is_(None)))
        version = db.scalar(
            select(ExperienceVersion.id)
            .where(ExperienceVersion.item_id == item.id, ExperienceVersion.kind == "saved")
            .order_by(ExperienceVersion.number.desc())
        )

        return item.id, item.owner_id, version


def save_on(app, clock, owner, item_id, version_id, *, kind="experience", field="actions", text="清洗订单数据", names=NAMES):
    """A saved exploration whose first direction quotes this version of the record: as evidence from an experience,
    as the condition of an ideal link from the preferences."""
    source = (kind, item_id, version_id, (field,))
    cited = quote(source, field, text)
    first = (
        direction(names[0], ideal=[{"condition": cited, "relation": "fits", "basis": "日常主要是整理和分析数据"}])
        if kind == "preferences"
        else direction(names[0], [cited])
    )
    result = trusted(owner, clock, (source,), [first, *(direction(name) for name in names[1:])])

    with database_of(app).transaction() as db:
        return records.save_exploration(db, owner, result, clock.now()).id


def row(app, model, record_id):
    with database_of(app).transaction() as db:
        return db.get(model, record_id)


def deletion_form(browser, page):
    """The deletion page as it is shown, and its form: hidden fields filled in, the choice unticked."""
    response = browser.get(page)
    assert response.status_code == 200, response.text

    return response.text, form_for(response.text, page)


def along(app, *exploration_ids) -> str:
    return ",".join(f"{exploration_id}.{row(app, DirectionExploration, exploration_id).revision}" for exploration_id in exploration_ids)


def edit_page(browser, item, **changes):
    assert browser.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", **changes).status_code == 303
    assert browser.submit(f"/experiences/{item}", f"/experiences/{item}/confirm").status_code == 303


def a_history_version(app, browser, clock):
    """A project confirmed twice: its first confirmed version, now history, and an exploration on each version."""
    item = confirmed(browser, **PROJECT)
    owner, first = owner_and_version(app, item)
    on_first = save_on(app, clock, owner, item, first)
    edit_page(browser, item, actions="新的做法：用 SQL 汇总每周销量。")
    _, second = owner_and_version(app, item)
    on_second = save_on(app, clock, owner, item, second, text="汇总每周销量")

    return item, owner, first, on_first, on_second


# --- by default, and when chosen ------------------------------------------------------------------------------


def test_by_default_only_the_record_goes_and_the_explorations_resting_on_it_stay(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    exploration = save_on(app, clock, owner, item, version)

    with database_of(app).transaction() as db:
        decision = records.decide_on_saved(db, owner, exploration, "数据分析", "keep", None, clock.now()).id

    page, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    shown = text_of(page)

    # Listed and offered, never ticked: the form holds each listed exploration with its revision.
    assert "以这条项目经历为依据的方向探索" in shown and "“数据分析”、“产品运营”、“用户研究”，保存于" in shown
    assert "同时把这 1 次方向探索移到回收站（恢复这条项目经历时一起恢复；方向决定不会删除）" in shown
    assert '<input type="checkbox" name="delete_along" value="yes">' in page
    assert form.fields["along"] == along(app, exploration) and "delete_along" not in form.fields

    assert browser_a.post(f"/experiences/{item}/delete", form.fields).status_code == 303
    assert row(app, ExperienceItem, item).deleted_at is not None
    assert row(app, DirectionExploration, exploration).deleted_at is None
    assert row(app, DirectionDecision, decision).deleted_at is None


def test_a_record_nothing_rests_on_shows_no_choice(app, browser_a, clock):
    item = confirmed(browser_a, **OTHER)
    page, form = deletion_form(browser_a, f"/experiences/{item}/delete")

    assert "delete_along" not in page and "along" not in form.fields and "以这条项目经历为依据" not in page
    assert browser_a.post(f"/experiences/{item}/delete", form.fields).status_code == 303


def test_ticked_the_listed_explorations_go_to_the_bin_with_the_record_and_come_back_with_it(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    first, second = save_on(app, clock, owner, item, version), save_on(app, clock, owner, item, version, names=("商业分析", "数据产品", "运营"))

    with database_of(app).transaction() as db:
        decision = records.decide_on_saved(db, owner, first, "数据分析", "keep", None, clock.now()).id

    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    assert browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes"}).status_code == 303
    record = row(app, ExperienceItem, item)

    for exploration in (first, second):
        assert row(app, DirectionExploration, exploration).deletion_id == record.deletion_id is not None

    # The decision stays: it now rests on an exploration in the bin, as C1 says.
    with database_of(app).transaction() as db:
        kept = db.get(DirectionDecision, decision)
        assert kept.deleted_at is None and decision_state(db, owner, kept, clock.now()) is BasisState.EXPLORATION_IN_BIN

    trash = browser_a.get("/experiences?tab=trash").text
    assert "连同 2 次方向探索" in text_of(trash)
    # They come back with the record only: the bin's own direction group does not list them.
    assert f"{DIRECTIONS}/explorations/{first}/restore" not in trash and f"{DIRECTIONS}/explorations/{second}/restore" not in trash

    assert browser_a.submit("/experiences?tab=trash", f"/experiences/{item}/restore").status_code == 303
    assert [row(app, DirectionExploration, exploration).deleted_at for exploration in (first, second)] == [None, None]

    with database_of(app).transaction() as db:
        assert decision_state(db, owner, db.get(DirectionDecision, decision), clock.now()) is BasisState.CURRENT


def test_purging_the_record_says_so_and_takes_what_went_along_for_good(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    exploration = save_on(app, clock, owner, item, version)
    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    assert browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes"}).status_code == 303

    page, purge = deletion_form(browser_a, f"/experiences/{item}/purge")
    assert "一起删除的 1 次方向探索也会永久删除。" in text_of(page)
    assert browser_a.post(f"/experiences/{item}/purge", {**purge.fields, "acknowledge": "yes"}).status_code == 303

    assert row(app, ExperienceItem, item) is None and row(app, DirectionExploration, exploration) is None


def test_a_version_takes_along_only_what_rests_on_it_and_brings_it_back(app, browser_a, clock):
    item, owner, first, on_first, on_second = a_history_version(app, browser_a, clock)
    page_url = f"/experiences/{item}/versions/{first}/delete"
    page, form = deletion_form(browser_a, page_url)

    assert "以这个版本为依据的方向探索" in text_of(page) and form.fields["along"] == along(app, on_first)
    assert browser_a.post(page_url, {**form.fields, "delete_along": "yes"}).status_code == 303
    version = row(app, ExperienceVersion, first)
    assert row(app, DirectionExploration, on_first).deletion_id == version.deletion_id is not None
    assert row(app, DirectionExploration, on_second).deleted_at is None

    history = text_of(browser_a.get(f"/experiences/{item}/history").text)
    assert "连同 1 次方向探索，恢复这个版本时一起恢复" in history
    assert "一起删除的 1 次方向探索也会永久删除。" in text_of(browser_a.get(f"/experiences/{item}/versions/{first}/purge").text)
    # Purging the whole record would take it too, with the record's own: the page counts both ways.
    assert browser_a.submit(f"/experiences/{item}/delete", f"/experiences/{item}/delete").status_code == 303
    assert "一起删除的 1 次方向探索也会永久删除。" in text_of(browser_a.get(f"/experiences/{item}/purge").text)
    assert browser_a.submit("/experiences?tab=trash", f"/experiences/{item}/restore").status_code == 303

    assert browser_a.submit(f"/experiences/{item}/history", f"/experiences/{item}/versions/{first}/restore").status_code == 303
    assert row(app, DirectionExploration, on_first).deleted_at is None


def test_the_preferences_deletion_page_offers_the_same(app, browser_a, clock):
    item, owner, version = preferences_of(app, browser_a)
    exploration = save_on(app, clock, owner, item, version, kind="preferences", field="work_content_ideal", text="希望多分析数据")
    page, form = deletion_form(browser_a, f"/experiences/{item}/delete")

    assert "以你的求职偏好为依据的方向探索" in text_of(page)
    assert browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes"}).status_code == 303
    assert row(app, DirectionExploration, exploration).deletion_id == row(app, ExperienceItem, item).deletion_id

    assert browser_a.submit("/experiences?tab=trash", f"/experiences/{item}/restore").status_code == 303
    assert row(app, DirectionExploration, exploration).deleted_at is None


# --- only as the page listed them (C precheck, 19.2) ---------------------------------------------------------------


@pytest.mark.parametrize("entry", ["record", "version"])
def test_an_exploration_deleted_and_restored_since_the_page_refuses_the_whole_deletion(app, browser_a, clock, entry):
    if entry == "record":
        item = confirmed(browser_a, **PROJECT)
        owner, version = owner_and_version(app, item)
        exploration = save_on(app, clock, owner, item, version)
        page_url, deleted = f"/experiences/{item}/delete", (ExperienceItem, item)
    else:
        item, owner, first, exploration, _ = a_history_version(app, browser_a, clock)
        page_url, deleted = f"/experiences/{item}/versions/{first}/delete", (ExperienceVersion, first)

    _, form = deletion_form(browser_a, page_url)
    shown = row(app, DirectionExploration, exploration).revision

    with database_of(app).transaction() as db:
        records.delete_exploration(db, owner, exploration, shown, clock.now())

    with database_of(app).transaction() as db:
        records.restore_exploration(db, owner, exploration, shown + 1, clock.now())

    refused = browser_a.post(page_url, {**form.fields, "delete_along": "yes"})

    # Nothing at all is deleted: back in use as before, the exploration is not the one the page showed.
    assert refused.status_code == 409 and ALONG_CHANGED in text_of(refused.text)
    assert row(app, *deleted).deleted_at is None and row(app, DirectionExploration, exploration).deleted_at is None

    # The page again, as things are now, the choice unticked: ticked again, it goes as listed now.
    again = form_for(refused.text, page_url)
    assert again.fields["along"] == f"{exploration}.{shown + 2}" and "delete_along" not in again.fields
    assert browser_a.post(page_url, {**again.fields, "delete_along": "yes"}).status_code == 303
    assert row(app, DirectionExploration, exploration).deleted_at is not None


def test_an_exploration_saved_after_the_page_was_shown_is_never_taken(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    listed = save_on(app, clock, owner, item, version)
    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    later = save_on(app, clock, owner, item, version, names=("商业分析", "数据产品", "运营"))

    assert browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes"}).status_code == 303
    assert row(app, DirectionExploration, listed).deleted_at is not None
    assert row(app, DirectionExploration, later).deleted_at is None


@pytest.mark.parametrize("named", ["another_users", "resting_elsewhere", "unknown"])
def test_only_the_users_own_explorations_resting_on_the_record_can_be_named(app, browser_a, browser_b, clock, named):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    listed = save_on(app, clock, owner, item, version)
    elsewhere_item = confirmed(browser_a, **OTHER)
    _, elsewhere_version = owner_and_version(app, elsewhere_item)
    elsewhere = save_on(app, clock, owner, elsewhere_item, elsewhere_version, text="整理问卷结果")
    theirs_item = confirmed(browser_b, **PROJECT)
    their_owner, their_version = owner_and_version(app, theirs_item)
    theirs = save_on(app, clock, their_owner, theirs_item, their_version, names=("别人的方向", "产品运营", "用户研究"))
    forged = {
        "another_users": along(app, theirs),
        "resting_elsewhere": along(app, listed, elsewhere),
        "unknown": f"{uuid.uuid4()}.1",
    }[named]
    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    refused = browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes", "along": forged})

    assert refused.status_code == 409 and ALONG_CHANGED in text_of(refused.text) and "别人的方向" not in refused.text
    assert row(app, ExperienceItem, item).deleted_at is None
    assert [row(app, DirectionExploration, one).deleted_at for one in (listed, elsewhere, theirs)] == [None, None, None]


def test_a_choice_that_cannot_be_read_is_a_bad_request_and_deletes_nothing(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    exploration = save_on(app, clock, owner, item, version)
    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    revision = row(app, DirectionExploration, exploration).revision

    for changes in (
        {"delete_along": "yes", "along": ""},
        {"delete_along": "yes", "along": "not-a-reference"},
        {"delete_along": "yes", "along": f"{exploration}.{revision},{exploration}.{revision + 1}"},
        {"delete_along": "yes", "along": f"{exploration}.0{revision}"},
        {"delete_along": "maybe"},
    ):
        assert browser_a.post(f"/experiences/{item}/delete", {**form.fields, **changes}).status_code == 400, changes

    assert row(app, ExperienceItem, item).deleted_at is None and row(app, DirectionExploration, exploration).deleted_at is None


def test_a_page_older_than_the_record_is_answered_as_before_and_takes_nothing(app, browser_a, clock):
    item = confirmed(browser_a, **PROJECT)
    owner, version = owner_and_version(app, item)
    exploration = save_on(app, clock, owner, item, version)
    _, form = deletion_form(browser_a, f"/experiences/{item}/delete")
    assert browser_a.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", outcome="后来补充的结果。").status_code == 303

    refused = browser_a.post(f"/experiences/{item}/delete", {**form.fields, "delete_along": "yes"})

    # The record's own check comes first, with its own page: the exploration list is not what stopped it.
    assert refused.status_code == 409 and "内容已经更新" in text_of(refused.text) and ALONG_CHANGED not in text_of(refused.text)
    assert row(app, ExperienceItem, item).deleted_at is None and row(app, DirectionExploration, exploration).deleted_at is None


# --- with the exploration pages (U3-C3a) --------------------------------------------------------------------------


def test_an_exploration_saved_from_a_task_that_went_along_is_decided_on_again_only_once_the_record_is_back(
    app, inline, browser_a, library
):
    task = explored(browser_a, [library])
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 303
    exploration = saved_id(app)
    _, form = deletion_form(browser_a, f"/experiences/{library}/delete")
    assert re.fullmatch(rf"{exploration}\.\d+", form.fields["along"])
    assert browser_a.post(f"/experiences/{library}/delete", {**form.fields, "delete_along": "yes"}).status_code == 303

    # The task's result rested on the record and is withdrawn (U3-C3a); what was saved from it is in the bin with it.
    assert browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()}).status_code == 409
    assert browser_a.get(f"{DIRECTIONS}/explorations/{exploration}").status_code == 404
    assert rows_of(app, DirectionDecision) == []

    assert browser_a.submit("/experiences?tab=trash", f"/experiences/{library}/restore").status_code == 303
    assert decide(browser_a, f"{DIRECTIONS}/explorations/{exploration}", "数据分析", "keep").status_code == 303
    [decision] = rows_of(app, DirectionDecision)
    assert (decision.basis, decision.exploration_id) == ("saved_exploration", exploration)


def rows_of(app, model):
    with database_of(app).transaction() as db:
        return list(db.scalars(select(model)))
