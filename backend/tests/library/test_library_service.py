"""Saving, confirming and revision conflicts in the experience service."""
import sqlite3

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.library.content import FIELD_NAMES, parse_experience_form
from app.library.errors import Conflict, ContentError, NotFound, StorageError
from app.library.models import (
    AuditEvent,
    ExperienceVersion,
    ItemState,
    VersionKind,
)
from app.library.service import (
    active_item,
    confirm_experience,
    create_experience,
    list_experiences,
    save_draft,
)

ONE_SENTENCE = {"actions": "和同学一起做了一个记账小程序。"}


def entry(**fields):
    return parse_experience_form(fields)


def create(database, owner, clock, **fields):
    with database.transaction() as db:
        return create_experience(db, owner, entry(**(fields or ONE_SENTENCE)), clock.now()).id


def stored(database, owner, item_id):
    with database.transaction() as db:
        return active_item(db, owner, item_id)


def versions_of(database, item_id):
    with database.transaction() as db:
        return list(
            db.scalars(
                select(ExperienceVersion)
                .where(ExperienceVersion.item_id == item_id)
                .order_by(ExperienceVersion.created_at, ExperienceVersion.number)
            )
        )


def test_one_sentence_is_enough_to_save():
    saved = entry(**ONE_SENTENCE)

    assert saved.content["actions"] == ONE_SENTENCE["actions"]
    assert saved.content["title"] == ""
    assert set(saved.content) == set(FIELD_NAMES)


def test_an_empty_form_is_refused_with_a_chinese_hint():
    with pytest.raises(ContentError) as error:
        entry(title="   ", actions="\n\n", private_note="")

    assert "至少" in error.value.message


def test_a_private_note_alone_does_not_count_as_content():
    with pytest.raises(ContentError) as error:
        entry(private_note="只是写给自己的备注")

    assert "私有备注不算经历内容" in error.value.message


def test_over_long_fields_are_refused_rather_than_cut():
    with pytest.raises(ContentError) as error:
        entry(title="字" * 201, actions="正常内容")

    assert "200" in error.value.field_errors["title"]


def test_control_characters_are_refused():
    with pytest.raises(ContentError) as error:
        entry(actions="前面\x00后面")

    assert "actions" in error.value.field_errors


def test_line_endings_are_normalised_and_outer_spaces_trimmed():
    saved = entry(actions="  第一行\r\n第二行\r第三行  ")

    assert saved.content["actions"] == "第一行\n第二行\n第三行"


def test_fields_the_form_does_not_define_are_ignored():
    saved = entry(actions="内容", owner_id="someone-else", state="confirmed", revision="99")

    assert set(saved.content) == set(FIELD_NAMES)


def test_a_new_experience_is_a_draft_with_one_initial_version(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    item = stored(database, owner, item_id)
    versions = versions_of(database, item_id)

    assert item.state == ItemState.DRAFT
    assert item.revision == 1
    assert item.last_confirmed_at is None
    assert [version.kind for version in versions] == [VersionKind.INITIAL]
    assert versions[0].content == item.content


def test_saving_a_draft_replaces_content_without_piling_up_versions(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    for revision, text in enumerate(("补充一点", "又补充一点", "再补充一点"), start=1):
        clock.advance(minutes=5)

        with database.transaction() as db:
            _, changed = save_draft(db, owner, item_id, revision, entry(actions=text), clock.now())

        assert changed

    item = stored(database, owner, item_id)

    assert item.content["actions"] == "再补充一点"
    assert item.revision == 4
    assert item.updated_at == clock.now()
    assert len(versions_of(database, item_id)) == 1


def test_saving_unchanged_content_changes_nothing(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)
    clock.advance(minutes=5)

    with database.transaction() as db:
        _, changed = save_draft(db, owner, item_id, 1, entry(**ONE_SENTENCE), clock.now())

    item = stored(database, owner, item_id)

    assert changed is False
    assert item.revision == 1
    assert item.updated_at != clock.now()


def test_confirming_records_exactly_the_shown_content_as_a_fixed_version(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock, title="记账小程序", actions="做了后端", private_note="备注")
    clock.advance(minutes=1)

    with database.transaction() as db:
        confirm_experience(db, owner, item_id, 1, clock.now())

    item = stored(database, owner, item_id)
    confirmed = [v for v in versions_of(database, item_id) if v.kind == VersionKind.CONFIRMED]

    assert item.state == ItemState.CONFIRMED
    assert item.revision == 2
    assert item.last_confirmed_at == clock.now()
    assert len(confirmed) == 1
    assert confirmed[0].id == item.confirmed_version_id
    assert confirmed[0].content == item.content
    assert confirmed[0].private_note == "备注"
    assert confirmed[0].source_revision == 1


def test_editing_a_confirmed_experience_makes_it_wait_for_review_again(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock, actions="确认过的说法")

    with database.transaction() as db:
        confirm_experience(db, owner, item_id, 1, clock.now())

    confirmed_at = clock.now()
    clock.advance(hours=1)

    with database.transaction() as db:
        save_draft(db, owner, item_id, 2, entry(actions="改过的说法"), clock.now())

    item = stored(database, owner, item_id)
    confirmed = [v for v in versions_of(database, item_id) if v.kind == VersionKind.CONFIRMED]

    assert item.state == ItemState.DRAFT
    assert item.confirmed_version_id is None
    assert item.last_confirmed_at == confirmed_at
    assert confirmed[0].content["actions"] == "确认过的说法"


def test_confirmed_versions_are_numbered_in_order(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        confirm_experience(db, owner, item_id, 1, clock.now())
        save_draft(db, owner, item_id, 2, entry(actions="第二次的内容"), clock.now())
        confirm_experience(db, owner, item_id, 3, clock.now())

    numbers = [v.number for v in versions_of(database, item_id) if v.kind == VersionKind.CONFIRMED]

    assert sorted(numbers) == [1, 2]


def test_confirming_an_already_confirmed_experience_adds_nothing(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        confirm_experience(db, owner, item_id, 1, clock.now())
        confirm_experience(db, owner, item_id, 2, clock.now())

    assert stored(database, owner, item_id).revision == 2
    assert len(versions_of(database, item_id)) == 2


def test_an_older_page_cannot_overwrite_newer_content(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        save_draft(db, owner, item_id, 1, entry(actions="较新的内容"), clock.now())

    with pytest.raises(Conflict) as conflict:
        with database.transaction() as db:
            save_draft(db, owner, item_id, 1, entry(actions="旧页面的内容"), clock.now())

    assert conflict.value.current.content["actions"] == "较新的内容"
    assert stored(database, owner, item_id).content["actions"] == "较新的内容"


def test_an_older_page_cannot_confirm_content_it_did_not_show(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        save_draft(db, owner, item_id, 1, entry(actions="后来改过"), clock.now())

    with pytest.raises(Conflict):
        with database.transaction() as db:
            confirm_experience(db, owner, item_id, 1, clock.now())

    assert stored(database, owner, item_id).state == ItemState.DRAFT
    assert [v.kind for v in versions_of(database, item_id)] == [VersionKind.INITIAL]


def test_another_owner_can_neither_read_nor_change_the_experience(database, owners, clock):
    owner, other = owners
    item_id = create(database, owner, clock)

    with database.transaction() as db:
        with pytest.raises(NotFound):
            active_item(db, other, item_id)

        with pytest.raises(NotFound):
            save_draft(db, other, item_id, 1, entry(actions="别人改的"), clock.now())

        with pytest.raises(NotFound):
            confirm_experience(db, other, item_id, 1, clock.now())

        assert list_experiences(db, other) == []

    assert stored(database, owner, item_id).content == entry(**ONE_SENTENCE).content


def _fail_on_write(session, flush_context, instances):
    raise OperationalError("COMMIT", None, sqlite3.OperationalError("disk I/O error"))


def test_a_failed_write_rolls_back_and_keeps_the_last_saved_content(database, owners, clock):
    owner, _ = owners
    item_id = create(database, owner, clock, actions="最后一次成功保存")
    event.listen(database._sessions, "before_flush", _fail_on_write)

    try:
        with pytest.raises(StorageError):
            with database.transaction() as db:
                save_draft(db, owner, item_id, 1, entry(actions="没能保存的内容"), clock.now())
    finally:
        event.remove(database._sessions, "before_flush", _fail_on_write)

    item = stored(database, owner, item_id)

    with database.transaction() as db:
        actions = list(db.scalars(select(AuditEvent.action).where(AuditEvent.item_id == item_id)))

    assert item.content["actions"] == "最后一次成功保存"
    assert item.revision == 1
    assert actions == ["created"]


def test_a_failed_first_save_leaves_nothing_behind(database, owners, clock):
    owner, _ = owners
    event.listen(database._sessions, "before_flush", _fail_on_write)

    try:
        with pytest.raises(StorageError):
            create(database, owner, clock, actions="第一次保存就失败")
    finally:
        event.remove(database._sessions, "before_flush", _fail_on_write)

    with database.transaction() as db:
        assert list_experiences(db, owner) == []
