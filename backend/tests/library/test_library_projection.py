"""The read-only views for later modules (U1-D): the confirmed-experience
snapshot and the reference status, and how the deletion pages state it.

Every record is synthetic. A snapshot is only a copy of confirmed material
at one moment: these tests also pin what it must leave out.
"""
import dataclasses
import re
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.library.content import (
    ACTIVITY,
    ALL_KINDS,
    AWARD,
    EDUCATION,
    EXPERIENCE_CATEGORIES,
    OTHER,
    PREFERENCES,
    PROFILE,
    PROJECT,
    WORK,
    parse_experience_form,
)
from app.library.db import LibraryBase
from app.library.errors import InconsistentRecord, NotFound
from app.library.identity import dev_users
from app.library.lifecycle import delete_experience, restore_experience, trashed_item
from app.library.models import ExperienceItem, ExperienceVersion, ItemState, VersionKind
from app.library.projection import (
    ConfirmedExperience,
    ReferenceStatus,
    list_references,
    project_confirmed_library,
)
from app.library.service import (
    active_item,
    confirm_experience,
    create_document,
    create_experience,
    save_draft,
)
from library_helpers import PDF, forms_in

SENTINEL = "快照外哨兵-u1d-7f3c"
REFERENCE_NOTE = "本经历库尚未建立与分析、简历的关联，无法列出它们是否用过这些内容；这次删除只处理经历库里的内容。"
# Some fields of each category filled in, the others left empty.
WRITTEN = {
    PROJECT: {"title": "合成项目", "actions": "只写了一句做了什么"},
    EDUCATION: {"school": "合成大学", "graduation": "2027 年 6 月（预计）"},
    WORK: {"organization": "合成公司", "outcome": "没有达到预期，也如实写下"},
    ACTIVITY: {"name": "合成活动"},
    AWARD: {"name": "合成证书", "issuer": "合成机构"},
    OTHER: {"notes": "暂时不知道怎么分类"},
}


def entry(category: str, **fields):
    return parse_experience_form(fields, category)


def confirmed(database, owner, clock, category=PROJECT, **fields) -> str:
    """Save an experience and confirm it, as the pages do."""
    with database.transaction() as db:
        item = create_experience(db, owner, entry(category, **fields), clock.now())

    with database.transaction() as db:
        confirm_experience(db, owner, item.id, active_item(db, owner, item.id).revision, clock.now())

    return item.id


def snapshot(database, owner, clock):
    with database.transaction() as db:
        return project_confirmed_library(db, owner, clock.now())


def ids(result) -> list[str]:
    return [experience.item_id for experience in result.experiences]


# --- where the content comes from ---------------------------------------------------


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_each_category_comes_from_its_confirmed_version_with_only_what_was_written(database, owners, clock, category):
    owner, _ = owners
    item_id = confirmed(database, owner, clock, category, **WRITTEN[category])

    with database.transaction() as db:
        version_id = active_item(db, owner, item_id).confirmed_version_id

    assert snapshot(database, owner, clock).experiences == (
        ConfirmedExperience(
            item_id=item_id,
            category=category,
            version_id=version_id,
            version_number=1,
            confirmed_at=clock.now(),
            source_revision=1,
            content=WRITTEN[category],
        ),
    )


def test_an_empty_library_or_only_drafts_give_a_valid_empty_snapshot(database, owners, clock):
    owner, _ = owners
    empty = snapshot(database, owner, clock)

    assert empty.experiences == ()
    assert empty.owner_id == owner
    assert uuid.UUID(empty.snapshot_id).version == 4
    assert empty.generated_at == clock.now() and empty.generated_at.tzinfo is not None

    with database.transaction() as db:
        create_experience(db, owner, entry(PROJECT, actions="只是一份草稿"), clock.now())

    again = snapshot(database, owner, clock)

    assert again.experiences == ()
    assert again.snapshot_id != empty.snapshot_id


def test_an_edit_after_confirming_leaves_it_out_until_it_is_confirmed_again(database, owners, clock):
    owner, _ = owners
    item_id = confirmed(database, owner, clock, PROJECT, title="第一次确认的标题")
    [first] = snapshot(database, owner, clock).experiences
    clock.advance(minutes=5)

    with database.transaction() as db:
        revision = active_item(db, owner, item_id).revision
        save_draft(db, owner, item_id, revision, entry(PROJECT, title="改过、还没确认的标题"), clock.now())

    # Waiting for review: neither the draft nor the earlier confirmation stands for it now.
    assert snapshot(database, owner, clock).experiences == ()

    clock.advance(minutes=5)

    with database.transaction() as db:
        confirm_experience(db, owner, item_id, active_item(db, owner, item_id).revision, clock.now())
        revision_now = active_item(db, owner, item_id).revision

    [second] = snapshot(database, owner, clock).experiences

    assert second.content == {"title": "改过、还没确认的标题"}
    assert (second.version_number, second.confirmed_at) == (2, clock.now())
    assert second.version_id != first.version_id
    # Confirming moves the revision on, so the source is the one before it.
    assert (second.source_revision, revision_now) == (3, 4)


def test_entries_come_in_category_order_then_by_confirmation_time(database, owners, clock):
    owner, _ = owners
    made = []

    for category, fields in (
        (OTHER, {"title": "其他"}),
        (PROJECT, {"title": "较早的项目"}),
        (EDUCATION, {"school": "学校"}),
        (PROJECT, {"title": "较晚的项目"}),
    ):
        clock.advance(minutes=1)
        made.append(confirmed(database, owner, clock, category, **fields))

    assert ids(snapshot(database, owner, clock)) == [made[1], made[3], made[2], made[0]]


# --- what stays out ---------------------------------------------------------------


def save_document(browser, page: str, **values):
    [form] = [
        form
        for form in forms_in(browser.get(f"{page}/edit").text)
        if form.method == "post" and form.action.startswith(page)
    ]
    response = browser.post(form.action, {**form.fields, **values})
    assert response.status_code == 303, response.text


def test_private_notes_the_profile_preferences_and_attachments_stay_out(app, browser_a):
    item_id = browser_a.create(title="快照里的经历", private_note=f"私有备注{SENTINEL}")
    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    assert browser_a.upload(item_id, f"附件{SENTINEL}.pdf", PDF + SENTINEL.encode()).status_code == 303
    save_document(browser_a, "/basic-profile", name=f"姓名{SENTINEL}", phone="13800138000", email="u1d@example.com")
    save_document(browser_a, "/job-preferences", cities_ideal=f"城市{SENTINEL}")
    database = app.state.library.database

    with database.transaction() as db:
        owner = dev_users(db)[0].id
        result = project_confirmed_library(db, owner, app.state.library.now())

    text = repr(result)

    assert ids(result) == [item_id]
    assert result.experiences[0].content == {"title": "快照里的经历"}
    assert SENTINEL not in text
    assert "13800138000" not in text and "u1d@example.com" not in text


def test_each_owner_gets_only_their_own_experiences(database, owners, clock):
    a, b = owners
    mine = confirmed(database, a, clock, PROJECT, title="A 的经历")
    theirs = confirmed(database, b, clock, PROJECT, title="B 的经历")

    assert ids(snapshot(database, a, clock)) == [mine]
    assert ids(snapshot(database, b, clock)) == [theirs]


def test_a_deleted_experience_leaves_at_once_and_a_restored_one_is_judged_again(database, owners, clock):
    owner, _ = owners
    kept = confirmed(database, owner, clock, PROJECT, title="一直保留")
    binned = confirmed(database, owner, clock, PROJECT, title="删除后恢复")
    edited = confirmed(database, owner, clock, PROJECT, title="确认后改过")

    with database.transaction() as db:
        revision = active_item(db, owner, edited).revision
        save_draft(db, owner, edited, revision, entry(PROJECT, title="确认后改过（待核对）"), clock.now())

    for item_id in (binned, edited):
        with database.transaction() as db:
            delete_experience(db, owner, item_id, active_item(db, owner, item_id).revision, clock.now())

    # Out at once: no waiting for the 7 days or the clean-up.
    assert ids(snapshot(database, owner, clock)) == [kept]

    for item_id in (binned, edited):
        with database.transaction() as db:
            revision = trashed_item(db, owner, item_id, clock.now()).revision
            restore_experience(db, owner, item_id, revision, clock.now())

    # Back by its actual state: the edited one is still waiting for review,
    # and its old confirmed version does not come back as current material.
    assert sorted(ids(snapshot(database, owner, clock))) == sorted([kept, binned])


@pytest.mark.parametrize(
    "damage",
    ["missing", "initial_version", "other_experience", "other_owner", "deleted_version", "no_pointer", "draft_with_pointer"],
)
def test_a_confirmation_that_points_wrong_fails_instead_of_being_guessed(database, owners, clock, damage):
    a, b = owners
    item_id = confirmed(database, a, clock, PROJECT, title="确认指针被改坏")
    other = confirmed(database, a, clock, PROJECT, title="另一条")
    foreign = confirmed(database, b, clock, PROJECT, title="B 的")

    with database.transaction() as db:
        item = db.get(ExperienceItem, item_id)
        version = db.get(ExperienceVersion, item.confirmed_version_id)

        if damage == "missing":
            item.confirmed_version_id = str(uuid.uuid4())
        elif damage == "initial_version":
            item.confirmed_version_id = db.scalar(
                select(ExperienceVersion.id).where(
                    ExperienceVersion.item_id == item_id, ExperienceVersion.kind == VersionKind.INITIAL
                )
            )
        elif damage == "other_experience":
            item.confirmed_version_id = db.get(ExperienceItem, other).confirmed_version_id
        elif damage == "other_owner":
            item.confirmed_version_id = db.get(ExperienceItem, foreign).confirmed_version_id
        elif damage == "deleted_version":
            version.deleted_at = clock.now()
            version.purge_after = clock.now() + timedelta(days=7)
            version.deletion_id = str(uuid.uuid4())
        elif damage == "no_pointer":
            item.confirmed_version_id = None
        else:
            item.state = ItemState.DRAFT

    with pytest.raises(InconsistentRecord) as error:
        snapshot(database, a, clock)

    assert error.value.item_id == item_id


# --- read only, one moment, a copy ---------------------------------------------------


def everything(database) -> dict:
    with database.transaction() as db:
        return {table.name: db.execute(select(table)).all() for table in LibraryBase.metadata.sorted_tables}


def test_taking_a_snapshot_writes_nothing(database, owners, clock):
    owner, _ = owners
    confirmed(database, owner, clock, PROJECT, title="已确认")

    with database.transaction() as db:
        create_experience(db, owner, entry(EDUCATION, school="草稿"), clock.now())

    before = everything(database)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(database.engine, "before_cursor_execute", record)

    try:
        snapshot(database, owner, clock)
    finally:
        event.remove(database.engine, "before_cursor_execute", record)

    assert everything(database) == before
    assert set(statements) <= {"BEGIN", "SELECT"}


def test_the_snapshot_is_a_copy(database, owners, clock):
    owner, _ = owners
    confirmed(database, owner, clock, PROJECT, title="原来的标题")
    [entry_] = snapshot(database, owner, clock).experiences
    entry_.content["title"] = "调用方改掉的标题"

    with pytest.raises(dataclasses.FrozenInstanceError):
        entry_.category = WORK

    with database.transaction() as db:
        assert db.get(ExperienceVersion, entry_.version_id).content["title"] == "原来的标题"

    assert snapshot(database, owner, clock).experiences[0].content["title"] == "原来的标题"


def test_one_snapshot_reads_one_moment_of_the_library(database, owners, clock):
    owner, _ = owners
    item_id = confirmed(database, owner, clock, PROJECT, title="读取时已确认")
    landed = threading.Event()
    seen_before_versions = []

    def edit_meanwhile():
        with database.transaction() as db:
            revision = active_item(db, owner, item_id).revision
            save_draft(db, owner, item_id, revision, entry(PROJECT, title="读取中途改的"), clock.now())

        landed.set()

    writer = threading.Thread(target=edit_meanwhile)

    def between_reads(conn, cursor, statement, *args):
        # Right before the versions are read, another writer tries to edit.
        if "FROM experience_versions" in statement and not seen_before_versions:
            writer.start()
            seen_before_versions.append(landed.wait(0.5))

    event.listen(database.engine, "before_cursor_execute", between_reads)

    try:
        result = snapshot(database, owner, clock)
    finally:
        event.remove(database.engine, "before_cursor_execute", between_reads)

    writer.join(10)

    # The edit had to wait for the snapshot's transaction, so the snapshot
    # is the library at one moment; the edit landed right after it.
    assert seen_before_versions == [False]
    assert landed.is_set()
    assert [experience.content["title"] for experience in result.experiences] == ["读取时已确认"]
    assert snapshot(database, owner, clock).experiences == ()


def test_the_time_given_is_the_reading_time_not_a_replay(database, owners, clock):
    owner, _ = owners

    with pytest.raises(ValueError), database.transaction() as db:
        project_confirmed_library(db, owner, datetime(2026, 10, 29, 16, 0))

    item_id = confirmed(database, owner, clock, PROJECT, title="现在已确认")
    earlier = clock.now() - timedelta(days=1)

    with database.transaction() as db:
        result = project_confirmed_library(db, owner, earlier)

    # The stamp is the time given; an earlier time does not read an earlier library.
    assert result.generated_at == earlier
    assert ids(result) == [item_id]


# --- the reference status --------------------------------------------------------


def test_references_are_not_established_which_is_not_an_empty_answer(database, owners, clock):
    owner, _ = owners
    item_id = confirmed(database, owner, clock, PROJECT, title="有确认版的经历")

    with database.transaction() as db:
        status = list_references(db, owner, item_id, clock.now())

    assert status == ReferenceStatus(status="not_established", modules=("analysis", "resume"))
    assert status != [] and status.modules


def test_references_are_only_given_for_the_owners_record_within_reach(database, owners, clock):
    a, b = owners
    item_id = confirmed(database, a, clock, PROJECT, title="A 的经历")

    with database.transaction() as db:
        profile_id = create_document(db, a, entry(PROFILE, city="合成城"), clock.now()).id

    def status_of(owner, target, kinds=EXPERIENCE_CATEGORIES):
        with database.transaction() as db:
            return list_references(db, owner, target, clock.now(), kinds).status

    for owner, target in ((b, item_id), (a, str(uuid.uuid4())), (a, profile_id)):
        with pytest.raises(NotFound):
            status_of(owner, target)

    assert status_of(a, profile_id, ALL_KINDS) == "not_established"

    with database.transaction() as db:
        delete_experience(db, a, item_id, active_item(db, a, item_id).revision, clock.now())

    # Still the owner's record while it can be restored, and gone after.
    assert status_of(a, item_id) == "not_established"

    clock.advance(days=7)

    with pytest.raises(NotFound):
        status_of(a, item_id)


def test_the_deletion_pages_say_only_what_the_library_knows_about_other_modules(app, browser_a, browser_b):
    item_id = browser_a.create(title="要删除的经历", actions="内容")
    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    assert browser_a.upload(item_id, "附件.pdf").status_code == 303
    page = browser_a.get(f"/experiences/{item_id}/delete").text

    assert REFERENCE_NOTE in page
    assert "没有其他地方保存这些内容的副本" not in page
    # The real scope is still stated next to it: two kept versions and the attachment.
    assert "以及 2 个保留中的历史版本" in page
    assert "1 个附件会一起移到回收站" in page
    assert browser_b.get(f"/experiences/{item_id}/delete").status_code == 404

    assert browser_a.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete").status_code == 303
    purge = f"/experiences/{item_id}/purge"
    purge_page = browser_a.get(purge).text
    unacknowledged = browser_a.submit(purge, purge)

    assert REFERENCE_NOTE in purge_page and "以及它的 1 个附件文件" in purge_page
    assert unacknowledged.status_code == 422 and REFERENCE_NOTE in unacknowledged.text
    assert browser_b.get(purge).status_code == 404

    # The profile's deletion page says the same.
    save_document(browser_a, "/basic-profile", city="合成城")
    [profile_delete] = re.findall(r'href="(/experiences/[0-9a-f-]{36}/delete)"', browser_a.get("/basic-profile").text)

    assert REFERENCE_NOTE in browser_a.get(profile_delete).text
