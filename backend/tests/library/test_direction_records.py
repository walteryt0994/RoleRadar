"""Saved direction explorations and decisions: contracts, checks, ownership, changes, deletion and export (U3-C1).

Everything here is synthetic and offline: results are built in the test as the server would build them, and
no page, route or AI call takes part, except the existing deletion pages, which must say what rests on a
record.
"""
import io
import json
import uuid
import zipfile

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.directions import records
from app.directions.contracts import ExplorationResult, IdealLink, TrustedExploration, name_key
from app.library import lifecycle
from app.library.content import PREFERENCES, parse_experience_form
from app.library.errors import NotAllowed, NotFound, RecordConflict, StorageError
from app.library.export import EXPORT_FORMAT_VERSION, NOT_INCLUDED, collect_export, write_export
from app.library.files import FileStore
from app.library.lifecycle import (
    delete_experience,
    delete_version,
    purge_due,
    request_experience_purge,
    request_version_purge,
    restore_experience,
    restore_version,
    run_purge_job,
    version_state,
)
from app.library.models import (
    AuditEvent,
    DirectionDecision,
    DirectionExploration,
    DirectionSource,
    ExperienceItem,
    ExperienceVersion,
    JobStatus,
    PurgeJob,
)
from app.library.projection import list_references, list_version_references
from app.library.references import BasisState, SourceState
from app.library.service import active_item, confirm_experience, create_document, create_experience, save_draft

ACTIONS = "用 Python 清洗订单数据，并用 SQL 汇总每周销量。"
PROJECT = {"title": "校园订单看板（合成）", "actions": ACTIONS, "outcome": "看板在课程展示中使用。", "reflection": "合成反思"}
EDUCATION = {"school": "合成大学", "major": "数据科学", "graduation": "2027 年 6 月（预计）"}
PREFS = {"work_content_ideal": "希望多分析数据，少做重复事务", "directions_ideal": "数据分析", "cities_ideal": "北京"}


# --- building what the server would build ---------------------------------------------------------------------


def confirmed(database, owner, clock, content=PROJECT, category="project"):
    with database.transaction() as db:
        item = create_experience(db, owner, parse_experience_form(content, category), clock.now())

    with database.transaction() as db:
        item = confirm_experience(db, owner, item.id, item.revision, clock.now())

        return item.id, item.confirmed_version_id


def preferences(database, owner, clock, content=PREFS):
    with database.transaction() as db:
        item_id = create_document(db, owner, parse_experience_form(content, PREFERENCES), clock.now()).id

    return item_id, latest_saved(database, item_id)


def latest_saved(database, item_id):
    with database.transaction() as db:
        return db.scalar(
            select(ExperienceVersion.id)
            .where(ExperienceVersion.item_id == item_id, ExperienceVersion.kind == "saved")
            .order_by(ExperienceVersion.number.desc())
        )


def edit(database, owner, clock, item_id, content, category="project", confirm=True):
    with database.transaction() as db:
        item = active_item(db, owner, item_id, (category,))
        item, _ = save_draft(db, owner, item_id, item.revision, parse_experience_form(content, category), clock.now())

        if confirm:
            item = confirm_experience(db, owner, item_id, item.revision, clock.now())

        return item.confirmed_version_id


def quote(source, field, text):
    _, item_id, version_id, _ = source

    return {"item_id": item_id, "version_id": version_id, "field": field, "text": text}


def direction(name, evidence=(), ideal=()):
    return {
        "name": name,
        "main_work": f"{name}的一般性介绍",
        "evidence": list(evidence),
        "ideal": list(ideal),
        "gaps": ["一般性差距"],
        "unknowns": ["具体公司的协作和节奏"],
        "difference": f"{name}和其他方向的区别",
        "next_steps": ["看几个公开岗位的职责"],
    }


def generation(clock):
    return {
        "provider": "simulation",
        "model": "roleradar-rules-1",
        "endpoint": "local",
        "prompt_version": "1",
        "notice_version": "direction-preview-1",
        "input_fingerprint": "a" * 64,
        "generated_at": clock.now(),
    }


def trusted(owner, clock, sources=(), directions=None, result_id=None) -> TrustedExploration:
    return TrustedExploration.model_validate(
        {
            "owner_id": owner,
            "result_id": result_id or str(uuid.uuid4()),
            "sources": [{"kind": kind, "item_id": item, "version_id": version, "fields": list(fields)} for kind, item, version, fields in sources],
            "result": {"directions": directions or [direction("数据分析"), direction("产品运营"), direction("用户研究")]},
            "generation": generation(clock),
        }
    )


@pytest.fixture
def world(database, owners, clock):
    """User A's project, education and preferences, and a result resting on all three."""
    a, _ = owners
    project_id, project_version = confirmed(database, a, clock)
    school_id, school_version = confirmed(database, a, clock, EDUCATION, "education")
    prefs_id, prefs_version = preferences(database, a, clock)
    project = ("experience", project_id, project_version, ("title", "actions", "outcome"))
    school = ("experience", school_id, school_version, ("school", "major"))
    prefs = ("preferences", prefs_id, prefs_version, ("work_content_ideal", "directions_ideal"))
    directions = [
        direction(
            "数据分析",
            [quote(project, "actions", "用 Python 清洗订单数据")],
            [{"condition": quote(prefs, "work_content_ideal", "希望多分析数据"), "relation": "fits", "basis": "日常主要是整理和分析数据"}],
        ),
        direction("数据产品", [quote(school, "major", "数据科学")], [{"condition": quote(prefs, "directions_ideal", "数据分析"), "relation": "unknown"}]),
        direction("用户研究"),
    ]

    return {"owner": a, "project": project, "school": school, "prefs": prefs, "sources": (project, school, prefs), "directions": directions}


def save(database, clock, world, **changes):
    result = trusted(world["owner"], clock, changes.pop("sources", world["sources"]), changes.pop("directions", world["directions"]), **changes)

    with database.transaction() as db:
        return records.save_exploration(db, world["owner"], result, clock.now()).id, result


def basis(database, clock, owner, exploration_id):
    with database.transaction() as db:
        return records.exploration_basis(db, owner, exploration_id, clock.now())


def decision_basis(database, clock, owner, decision_id):
    with database.transaction() as db:
        return records.decision_basis(db, owner, decision_id, clock.now())


def row(database, model, row_id):
    with database.transaction() as db:
        return db.get(model, row_id)


def count(database, model):
    with database.transaction() as db:
        return len(list(db.scalars(select(model))))


def for_search(database, clock, owner):
    with database.transaction() as db:
        return records.directions_for_search(db, owner, clock.now())


# --- the contracts ----------------------------------------------------------------------------------------------


def test_a_result_holds_three_to_five_differently_named_directions_and_no_score():
    three = [direction("数据分析"), direction("产品运营"), direction("用户研究")]

    assert len(ExplorationResult.model_validate({"directions": three}).directions) == 3

    for directions in (three[:2], three + [direction(f"方向{n}") for n in range(3)], three + [direction(" 数据 分析 ")]):
        with pytest.raises(ValidationError):
            ExplorationResult.model_validate({"directions": directions})

    with pytest.raises(ValidationError):
        ExplorationResult.model_validate({"directions": [{**three[0], "score": 0.9}, *three[1:]]})

    assert name_key(" 数据 分析 ") == name_key("数据分析") != name_key("数据产品")
    assert name_key("ＡＩ 应用") == name_key("ai应用")


def test_only_a_basis_on_the_directions_side_makes_a_relation_fit_or_conflict():
    condition = {"item_id": str(uuid.uuid4()), "version_id": str(uuid.uuid4()), "field": "work_content_no", "text": "不想频繁出差"}

    assert IdealLink.model_validate({"condition": condition, "relation": "unknown"}).basis is None
    assert IdealLink.model_validate({"condition": condition, "relation": "may_conflict", "basis": "常需要外出拜访客户"}).basis

    # The user's condition on its own proves nothing about the direction.
    for link in ({"relation": "may_conflict"}, {"relation": "fits", "basis": "  "}, {"relation": "unknown", "basis": "常出差"}):
        with pytest.raises(ValidationError):
            IdealLink.model_validate({"condition": condition, **link})


def test_quotes_must_come_from_the_records_and_fields_the_task_chose(owners, clock):
    a, _ = owners
    project = ("experience", str(uuid.uuid4()), str(uuid.uuid4()), ("actions",))
    prefs = ("preferences", str(uuid.uuid4()), str(uuid.uuid4()), ("work_content_ideal",))
    cases = [
        # A field the task did not choose.
        ((project,), [direction("数据分析", [quote(project, "outcome", "看板")])]),
        # A user condition quoted from an experience.
        ((project,), [direction("数据分析", ideal=[{"condition": quote(project, "actions", "清洗"), "relation": "unknown"}])]),
        # Evidence quoted from the preferences.
        ((prefs,), [direction("数据分析", [quote(prefs, "work_content_ideal", "分析")])]),
        # Two preferences, one record twice.
        ((prefs, ("preferences", str(uuid.uuid4()), str(uuid.uuid4()), ("cities_ideal",))), []),
        ((project, project), []),
    ]

    fillers = [direction(name) for name in ("产品运营", "用户研究", "运营分析")]

    for sources, directions in cases:
        with pytest.raises(ValidationError):
            trusted(a, clock, sources, directions + fillers[: 3 - len(directions)])


def test_no_experience_and_no_preferences_is_a_valid_exploration(database, owners, clock):
    a, _ = owners
    result = trusted(a, clock)

    with database.transaction() as db:
        exploration_id = records.save_exploration(db, a, result, clock.now()).id

    assert basis(database, clock, a, exploration_id) is SourceState.CURRENT
    assert count(database, DirectionSource) == 0
    assert all(not item["evidence"] for item in row(database, DirectionExploration, exploration_id).content["directions"])


# --- saving --------------------------------------------------------------------------------------------------------


def test_saving_checks_the_result_and_keeps_it_with_how_it_was_made(database, clock, world):
    exploration_id, result = save(database, clock, world)
    saved = row(database, DirectionExploration, exploration_id)

    assert saved.content == result.result.model_dump(mode="json")
    assert saved.generation["notice_version"] == "direction-preview-1" and saved.generation["input_fingerprint"] == "a" * 64
    assert saved.owner_id == world["owner"] and saved.result_id == result.result_id and saved.revision == 1

    with database.transaction() as db:
        kept = {(source.kind, source.item_id, source.version_id, tuple(source.fields)) for source in db.scalars(select(DirectionSource))}
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.object_id == exploration_id)))

    assert kept == set(world["sources"])
    # The audit says what happened, never what was in it.
    assert [event.action for event in events] == ["direction_exploration_saved"]
    assert basis(database, clock, world["owner"], exploration_id) is SourceState.CURRENT
    # No private note, no whole record: only the quotes the result holds.
    assert "合成反思" not in json.dumps(saved.content, ensure_ascii=False)


def test_saving_the_same_result_again_keeps_one(database, clock, world):
    result = trusted(world["owner"], clock, world["sources"], world["directions"])

    with database.transaction() as db:
        first = records.save_exploration(db, world["owner"], result, clock.now()).id

    with database.transaction() as db:
        again = records.save_exploration(db, world["owner"], result, clock.now()).id
        records.delete_exploration(db, world["owner"], first, 1, clock.now())

    assert again == first and count(database, DirectionExploration) == 1

    with pytest.raises(NotAllowed, match="回收站"):
        with database.transaction() as db:
            records.save_exploration(db, world["owner"], result, clock.now())

    saved = row(database, DirectionExploration, first)
    values = {column.name: getattr(saved, column.name) for column in DirectionExploration.__table__.columns}

    # The database itself keeps one row per result.
    with pytest.raises(StorageError) as refused:
        with database.transaction() as db:
            db.add(DirectionExploration(**{**values, "id": str(uuid.uuid4())}))

    assert isinstance(refused.value.__cause__, IntegrityError)


def forged(world, owners, database, clock, case):
    a, b = owners
    project = world["project"]
    directions = world["directions"]

    if case == "someone_elses_result":
        return b, trusted(a, clock, world["sources"], directions)

    if case == "someone_elses_record":
        other = confirmed(database, b, clock)
        source = ("experience", other[0], other[1], ("actions",))

        return a, trusted(a, clock, (source,), [direction("数据分析", [quote(source, "actions", "用 Python")]), direction("产品运营"), direction("用户研究")])

    if case == "an_unconfirmed_version":
        with database.transaction() as db:
            initial = db.scalar(select(ExperienceVersion.id).where(ExperienceVersion.item_id == project[1], ExperienceVersion.kind == "initial"))

        return a, trusted(a, clock, (("experience", project[1], initial, ("actions",)),))

    if case == "preferences_named_an_experience":
        return a, trusted(a, clock, (("experience", world["prefs"][1], world["prefs"][2], ("cities_ideal",)),))

    if case == "a_field_the_record_has_not":
        return a, trusted(a, clock, (("experience", project[1], project[2], ("actions", "salary")),))

    # A quote that is not in the text.
    source = ("experience", project[1], project[2], ("actions",))

    return a, trusted(a, clock, (source,), [direction("数据分析", [quote(source, "actions", "用 Java 写后端")]), direction("产品运营"), direction("用户研究")])


@pytest.mark.parametrize(
    "case",
    ["someone_elses_result", "someone_elses_record", "an_unconfirmed_version", "preferences_named_an_experience", "a_field_the_record_has_not", "a_quote_not_in_the_text"],
)
def test_forged_or_another_users_results_are_refused_and_nothing_is_saved(database, owners, clock, world, case):
    owner, result = forged(world, owners, database, clock, case)

    with pytest.raises((NotAllowed, NotFound)):
        with database.transaction() as db:
            records.save_exploration(db, owner, result, clock.now())

    with pytest.raises((NotAllowed, NotFound)):
        with database.transaction() as db:
            records.decide_on_unsaved(db, owner, result, "数据分析", "keep", None, clock.now())

    assert count(database, DirectionExploration) == count(database, DirectionDecision) == count(database, DirectionSource) == 0


def change(database, clock, world, case):
    a = world["owner"]

    if case == "used_field_changed_and_confirmed_again":
        edit(database, a, clock, world["project"][1], {**PROJECT, "actions": "改写后的做法"})
    elif case == "changed_and_waiting_for_confirmation":
        edit(database, a, clock, world["project"][1], {**PROJECT, "context": "补充背景"}, confirm=False)
    elif case == "used_preference_changed":
        edit(database, a, clock, world["prefs"][1], {**PREFS, "work_content_ideal": "希望多和用户沟通"}, PREFERENCES, confirm=False)
    elif case == "record_deleted":
        with database.transaction() as db:
            delete_experience(db, a, world["school"][1], active_item(db, a, world["school"][1]).revision, clock.now())
    elif case == "preferences_version_deleted":
        edit(database, a, clock, world["prefs"][1], {**PREFS, "cities_ideal": "上海"}, PREFERENCES, confirm=False)

        with database.transaction() as db:
            version = db.get(ExperienceVersion, world["prefs"][2])
            delete_version(db, a, world["prefs"][1], version.id, version_state(db, version), clock.now(), (PREFERENCES,))


REFUSALS = {
    "used_field_changed_and_confirmed_again": SourceState.CHANGED,
    "changed_and_waiting_for_confirmation": SourceState.CHANGED,
    "used_preference_changed": SourceState.CHANGED,
    "record_deleted": SourceState.RECORD_IN_BIN,
    "preferences_version_deleted": SourceState.VERSION_IN_BIN,
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_a_result_whose_sources_changed_before_saving_is_refused_and_never_swapped(database, clock, world, case):
    result = trusted(world["owner"], clock, world["sources"], world["directions"])
    change(database, clock, world, case)

    with pytest.raises(NotAllowed, match=records.REFUSED[REFUSALS[case]]):
        with database.transaction() as db:
            records.save_exploration(db, world["owner"], result, clock.now())

    assert count(database, DirectionExploration) == 0


def test_changes_to_fields_that_were_not_used_change_nothing(database, clock, world):
    result = trusted(world["owner"], clock, world["sources"], world["directions"])
    # The project confirmed again with another reflection, the preferences with another city: not used.
    edit(database, world["owner"], clock, world["project"][1], {**PROJECT, "reflection": "新的反思"})
    edit(database, world["owner"], clock, world["prefs"][1], {**PREFS, "cities_ideal": "上海"}, PREFERENCES, confirm=False)

    with database.transaction() as db:
        exploration_id = records.save_exploration(db, world["owner"], result, clock.now()).id

    assert basis(database, clock, world["owner"], exploration_id) is SourceState.CURRENT


# --- what changes afterwards ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", list(REFUSALS))
def test_later_changes_mark_a_saved_exploration_and_never_rewrite_it(database, clock, world, case):
    exploration_id, result = save(database, clock, world)
    change(database, clock, world, case)

    assert basis(database, clock, world["owner"], exploration_id) is REFUSALS[case]
    assert row(database, DirectionExploration, exploration_id).content == result.result.model_dump(mode="json")


def test_a_decision_stands_on_what_its_own_direction_quotes(database, clock, world):
    a = world["owner"]
    exploration_id, _ = save(database, clock, world)

    with database.transaction() as db:
        analysis = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id
        research = records.decide_on_saved(db, a, exploration_id, "用户研究", "keep", None, clock.now()).id

    # The school is quoted only by 数据产品: deleting it leaves both decisions standing.
    change(database, clock, world, "record_deleted")
    assert decision_basis(database, clock, a, analysis) is BasisState.CURRENT
    assert set(for_search(database, clock, a)) == {"数据分析", "用户研究"}

    change(database, clock, world, "used_field_changed_and_confirmed_again")
    assert decision_basis(database, clock, a, analysis) is BasisState.CHANGED
    # A direction that quoted nothing rests on no record.
    assert decision_basis(database, clock, a, research) is BasisState.CURRENT
    assert for_search(database, clock, a) == ("用户研究",)


# --- decisions -----------------------------------------------------------------------------------------------------------------


def test_a_decision_on_an_unsaved_result_keeps_no_copy_of_it(database, clock, world):
    a = world["owner"]
    result = trusted(a, clock, world["sources"], world["directions"])

    with database.transaction() as db:
        decision = records.decide_on_unsaved(db, a, result, "数据分析", "keep", None, clock.now())
        kept = [(s.kind, s.item_id, s.version_id, s.fields) for s in db.scalars(select(DirectionSource))]

    assert count(database, DirectionExploration) == 0
    assert (decision.basis, decision.exploration_id) == ("unsaved_exploration", None)
    # Only what the direction rests on, by ids and field names.
    assert sorted(kept) == sorted(
        [("experience", world["project"][1], world["project"][2], ["actions"]), ("preferences", world["prefs"][1], world["prefs"][2], ["work_content_ideal"])]
    )
    assert for_search(database, clock, a) == ("数据分析",)


def test_one_decision_per_name_and_every_change_names_the_revision(database, owners, clock):
    a, _ = owners

    with database.transaction() as db:
        decision = records.decide_personal(db, a, "数据分析", "keep", None, clock.now())

    # Another page that saw no decision, an older revision of it, or another decision, changes nothing.
    for name, seen in (
        ("数据 分析", None),
        ("数据分析", records.SeenDecision(decision.id, 7)),
        ("数据分析", records.SeenDecision(str(uuid.uuid4()), 1)),
    ):
        with pytest.raises(RecordConflict):
            with database.transaction() as db:
                records.decide_personal(db, a, name, "exclude", seen, clock.now())

    # A revision on its own names no decision.
    with pytest.raises(TypeError):
        with database.transaction() as db:
            records.decide_personal(db, a, "数据分析", "exclude", 1, clock.now())

    with database.transaction() as db:
        changed = records.decide_personal(db, a, "数据分析", "defer", records.SeenDecision(decision.id, 1), clock.now())

    assert (changed.id, changed.state, changed.revision) == (decision.id, "defer", 2)
    assert count(database, DirectionDecision) == 1

    # The database itself holds one decision in use per name.
    with pytest.raises(StorageError) as refused:
        with database.transaction() as db:
            db.add(DirectionDecision(**{**{c.name: getattr(changed, c.name) for c in DirectionDecision.__table__.columns}, "id": str(uuid.uuid4())}))

    assert isinstance(refused.value.__cause__, IntegrityError)

    for bad in ("", " ", "名" * 41):
        with pytest.raises(NotAllowed):
            with database.transaction() as db:
                records.decide_personal(db, a, bad, "keep", None, clock.now())


def test_an_old_decision_is_never_restored_over_a_newer_one_of_the_same_name(database, owners, clock):
    a, _ = owners

    with database.transaction() as db:
        old = records.decide_personal(db, a, "数据分析", "keep", None, clock.now()).id
        records.delete_decision(db, a, old, 1, clock.now())
        new = records.decide_personal(db, a, "数据分析", "exclude", None, clock.now()).id

    with pytest.raises(NotAllowed, match="同名"):
        with database.transaction() as db:
            records.restore_decision(db, a, old, 2, clock.now())

    with database.transaction() as db:
        records.delete_decision(db, a, new, 1, clock.now())
        records.restore_decision(db, a, old, 2, clock.now())

    assert row(database, DirectionDecision, old).deleted_at is None and row(database, DirectionDecision, new).deleted_at is not None


def test_deleting_an_exploration_keeps_the_decisions_made_on_it_and_their_link(database, clock, world):
    a = world["owner"]
    exploration_id, _ = save(database, clock, world)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id
        records.delete_exploration(db, a, exploration_id, 1, clock.now())

    assert decision_basis(database, clock, a, decision) is BasisState.EXPLORATION_IN_BIN
    assert for_search(database, clock, a) == ()

    with database.transaction() as db:
        job = lifecycle.request_direction_record_purge(db, a, DirectionExploration, exploration_id, 2, clock.now())

    assert run_purge_job(database, job, clock.now()) == "done"
    kept = row(database, DirectionDecision, decision)
    # Never made to look new: the link and what it rested on stay, marked.
    assert kept.exploration_id == exploration_id and kept.basis == "saved_exploration"
    assert decision_basis(database, clock, a, decision) is BasisState.EXPLORATION_GONE

    # Only the user making it their own choice again brings it back into use; the history stays.
    with database.transaction() as db:
        records.decide_personal(db, a, "数据分析", "keep", records.SeenDecision(decision, kept.revision), clock.now())
        sources = list(db.scalars(select(DirectionSource).where(DirectionSource.holder_id == decision)))

    again = row(database, DirectionDecision, decision)
    assert (again.basis, again.exploration_id, len(sources)) == ("personal", exploration_id, 2)
    assert for_search(database, clock, a) == ("数据分析",)


def test_a_deleted_source_cannot_be_got_round(database, clock, world):
    a = world["owner"]
    exploration_id, result = save(database, clock, world)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id
        delete_experience(db, a, world["project"][1], active_item(db, a, world["project"][1]).revision, clock.now())

    # Neither a new save nor a decision on the same result goes through the deleted source.
    for attempt in (
        lambda db: records.save_exploration(db, a, trusted(a, clock, world["sources"], world["directions"]), clock.now()),
        lambda db: records.decide_on_unsaved(db, a, result, "数据分析", "keep", None, clock.now()),
    ):
        with pytest.raises(NotAllowed):
            with database.transaction() as db:
                attempt(db)

    # Deciding again on the old exploration is allowed, and still not taken for a search.
    with database.transaction() as db:
        records.decide_on_saved(db, a, exploration_id, "数据分析", "exclude", records.SeenDecision(decision, 1), clock.now())
        records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", records.SeenDecision(decision, 2), clock.now())

    assert decision_basis(database, clock, a, decision) is BasisState.RECORD_IN_BIN
    assert for_search(database, clock, a) == ()


# --- deleting, restoring and purging -------------------------------------------------------------------------------


def rev(db, item_id):
    """An item's revision, read in the caller's own transaction."""
    return db.get(ExperienceItem, item_id).revision


def test_by_default_deleting_a_record_deletes_nothing_that_rests_on_it(database, clock, world):
    a = world["owner"]
    project = world["project"][1]
    exploration_id, _ = save(database, clock, world)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id
        references = list_references(db, a, project, clock.now())

    assert (references.explorations, references.decisions) == ((exploration_id,), (decision,))

    with database.transaction() as db:
        delete_experience(db, a, project, rev(db, project), clock.now())

    assert row(database, DirectionExploration, exploration_id).deleted_at is None
    assert basis(database, clock, a, exploration_id) is SourceState.RECORD_IN_BIN

    with database.transaction() as db:
        restore_experience(db, a, project, rev(db, project), clock.now())

    assert basis(database, clock, a, exploration_id) is SourceState.CURRENT
    assert for_search(database, clock, a) == ("数据分析",)

    with database.transaction() as db:
        delete_experience(db, a, project, rev(db, project), clock.now())

    with database.transaction() as db:
        job = request_experience_purge(db, a, project, rev(db, project), clock.now())

    assert run_purge_job(database, job, clock.now()) == "done"
    # Kept as the user chose: still there to look at, said to rest on a record that is gone.
    assert basis(database, clock, a, exploration_id) is SourceState.GONE
    assert row(database, DirectionExploration, exploration_id).content["directions"][0]["evidence"][0]["text"] == "用 Python 清洗订单数据"


def test_deleting_along_takes_only_explorations_in_use_that_rest_on_the_record(database, clock, world):
    a = world["owner"]
    project = world["project"][1]
    resting, _ = save(database, clock, world)
    elsewhere, _ = save(database, clock, world, sources=(world["school"],), directions=[direction("数据产品", [quote(world["school"], "major", "数据科学")]), direction("运营"), direction("研究")])

    # One that does not rest on the record: nothing is deleted, the record included.
    with pytest.raises(NotAllowed):
        with database.transaction() as db:
            delete_experience(db, a, project, rev(db, project), clock.now(), also_explorations=(elsewhere,))

    assert row(database, ExperienceItem, project).deleted_at is None and row(database, DirectionExploration, elsewhere).deleted_at is None

    with database.transaction() as db:
        item = delete_experience(db, a, project, rev(db, project), clock.now(), also_explorations=(resting,))
        deletion = item.deletion_id

    went = row(database, DirectionExploration, resting)
    assert (went.deletion_id, went.purge_after) == (deletion, row(database, ExperienceItem, project).purge_after)

    # It comes back only with the record.
    for attempt in (records.restore_exploration, records.request_exploration_purge):
        with pytest.raises(NotAllowed, match="一起删除"):
            with database.transaction() as db:
                attempt(db, a, resting, went.revision, clock.now())

    with database.transaction() as db:
        restore_experience(db, a, project, rev(db, project), clock.now())

    assert row(database, DirectionExploration, resting).deleted_at is None

    # Purged with the record, what it rests on included; the other one stays.
    with database.transaction() as db:
        delete_experience(db, a, project, rev(db, project), clock.now(), also_explorations=(resting,))

    with database.transaction() as db:
        job = request_experience_purge(db, a, project, rev(db, project), clock.now())

    assert run_purge_job(database, job, clock.now()) == "done"
    assert row(database, DirectionExploration, resting) is None and row(database, DirectionExploration, elsewhere) is not None

    with database.transaction() as db:
        assert not list(db.scalars(select(DirectionSource).where(DirectionSource.holder_id == resting)))


def test_restoring_brings_back_only_what_went_with_that_deletion(database, clock, world):
    a = world["owner"]
    project = world["project"][1]
    on_its_own, _ = save(database, clock, world)
    along, _ = save(database, clock, world)

    with database.transaction() as db:
        records.delete_exploration(db, a, on_its_own, 1, clock.now())
        delete_experience(db, a, project, rev(db, project), clock.now(), also_explorations=(along,))

    with database.transaction() as db:
        restore_experience(db, a, project, rev(db, project), clock.now())

    assert row(database, DirectionExploration, along).deleted_at is None
    assert row(database, DirectionExploration, on_its_own).deleted_at is not None


def test_with_two_sources_restoring_one_does_not_bring_back_use(database, clock, world):
    a = world["owner"]
    project, school = world["project"][1], world["school"][1]
    sources = (world["project"], world["school"])
    directions = [direction("数据分析", [quote(world["project"], "actions", "清洗订单数据"), quote(world["school"], "major", "数据科学")]), direction("运营"), direction("研究")]
    exploration_id, _ = save(database, clock, world, sources=sources, directions=directions)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id

    for item_id in (project, school):
        with database.transaction() as db:
            delete_experience(db, a, item_id, rev(db, item_id), clock.now())

    with database.transaction() as db:
        restore_experience(db, a, project, rev(db, project), clock.now())

    assert basis(database, clock, a, exploration_id) is SourceState.RECORD_IN_BIN
    assert decision_basis(database, clock, a, decision) is BasisState.RECORD_IN_BIN and for_search(database, clock, a) == ()

    with database.transaction() as db:
        restore_experience(db, a, school, rev(db, school), clock.now())

    assert basis(database, clock, a, exploration_id) is SourceState.CURRENT and for_search(database, clock, a) == ("数据分析",)


def test_preferences_and_single_versions_follow_the_same_rules(database, clock, world):
    a = world["owner"]
    project, prefs = world["project"][1], world["prefs"][1]
    exploration_id, _ = save(database, clock, world)
    edit(database, a, clock, project, {**PROJECT, "reflection": "新的反思"})

    with database.transaction() as db:
        old = db.get(ExperienceVersion, world["project"][2])
        references = list_version_references(db, a, project, old.id, clock.now())
        delete_version(db, a, project, old.id, version_state(db, old), clock.now(), also_explorations=(exploration_id,))

    assert references.explorations == (exploration_id,)
    assert row(database, DirectionExploration, exploration_id).deleted_at is not None

    with database.transaction() as db:
        old = db.get(ExperienceVersion, world["project"][2])
        restore_version(db, a, project, old.id, version_state(db, old), clock.now())

    # Back with its version; the used fields read as they did, so it stands as current.
    assert basis(database, clock, a, exploration_id) is SourceState.CURRENT

    with database.transaction() as db:
        old = db.get(ExperienceVersion, world["project"][2])
        delete_version(db, a, project, old.id, version_state(db, old), clock.now())

    assert basis(database, clock, a, exploration_id) is SourceState.VERSION_IN_BIN

    with database.transaction() as db:
        old = db.get(ExperienceVersion, world["project"][2])
        job = request_version_purge(db, a, project, old.id, version_state(db, old), clock.now())

    assert run_purge_job(database, job, clock.now()) == "done"
    assert basis(database, clock, a, exploration_id) is SourceState.GONE

    # The preferences: deleted as a whole, in the bin; their old version deleted on its own.
    with database.transaction() as db:
        delete_experience(db, a, prefs, rev(db, prefs), clock.now(), (PREFERENCES,))

    with database.transaction() as db:
        assert list_references(db, a, prefs, clock.now(), (PREFERENCES,)).explorations == (exploration_id,)


def test_purging_removes_the_rows_keeps_the_audit_and_a_failure_is_never_reported_as_done(database, clock, world, monkeypatch):
    a = world["owner"]
    exploration_id, _ = save(database, clock, world)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id
        records.delete_exploration(db, a, exploration_id, 1, clock.now())
        records.delete_decision(db, a, decision, 1, clock.now())
        job = records.request_exploration_purge(db, a, exploration_id, 2, clock.now())

    def fail(*args):
        raise OSError("disk")

    monkeypatch.setattr(lifecycle, "_remove", fail)
    assert run_purge_job(database, job, clock.now()) == "failed"
    failed = row(database, PurgeJob, job)
    assert failed.status == JobStatus.FAILED and failed.completed_at is None
    # Unreachable at once all the same: neither restorable nor in use.
    with pytest.raises(NotFound):
        with database.transaction() as db:
            records.restore_exploration(db, a, exploration_id, 3, clock.now())

    with pytest.raises(NotFound):
        with database.transaction() as db:
            records.exploration_basis(db, a, exploration_id, clock.now())

    monkeypatch.undo()
    assert run_purge_job(database, job, clock.now()) == "done"
    assert row(database, DirectionExploration, exploration_id) is None

    # The decision is purged when its seven days are over.
    clock.advance(days=7)
    assert purge_due(database, clock.now()).done >= 1
    assert row(database, DirectionDecision, decision) is None and count(database, DirectionSource) == 0

    with database.transaction() as db:
        actions = [event.action for event in db.scalars(select(AuditEvent).where(AuditEvent.object_id.in_((exploration_id, decision))))]

    assert actions.count("purge_completed") == 2 and "direction_exploration_saved" in actions


# --- ownership ------------------------------------------------------------------------------------------------------------


def test_another_user_reaches_none_of_them(database, owners, clock, world, tmp_path):
    a, b = owners
    exploration_id, _ = save(database, clock, world)

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, exploration_id, "数据分析", "keep", None, clock.now()).id

    for attempt in (
        lambda db: records.decide_on_saved(db, b, exploration_id, "数据分析", "keep", None, clock.now()),
        lambda db: records.delete_exploration(db, b, exploration_id, 1, clock.now()),
        lambda db: records.delete_decision(db, b, decision, 1, clock.now()),
        lambda db: records.exploration_basis(db, b, exploration_id, clock.now()),
        lambda db: records.decision_basis(db, b, decision, clock.now()),
    ):
        with pytest.raises(NotFound):
            with database.transaction() as db:
                attempt(db)

    with database.transaction() as db:
        assert records.explorations_in_use(db, b) == [] and records.decisions_in_use(db, b) == []
        content = collect_export(db, b, clock.now())

    assert content.directions.explorations == [] and content.directions.decisions == []


# --- the export ------------------------------------------------------------------------------------------------------------------


def exported(database, owner, clock, tmp_path):
    with database.transaction() as db:
        content = collect_export(db, owner, clock.now())

    store = FileStore(tmp_path / "store")
    store.claim(str(uuid.uuid4()))
    package = write_export(content, store, "合成用户 A", clock.now())

    with zipfile.ZipFile(io.BytesIO(package.handle.read())) as archive:
        files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}

    package.handle.close()

    return files


def test_the_export_holds_what_is_kept_with_how_it_stands_and_nothing_purged(database, clock, world, tmp_path):
    a = world["owner"]
    kept, _ = save(database, clock, world)
    binned, _ = save(database, clock, world)
    purged, _ = save(database, clock, world)

    with database.transaction() as db:
        records.decide_on_saved(db, a, kept, "数据分析", "keep", None, clock.now())
        records.decide_personal(db, a, "运营", "exclude", None, clock.now())
        records.delete_exploration(db, a, binned, 1, clock.now())
        records.delete_exploration(db, a, purged, 1, clock.now())
        job = records.request_exploration_purge(db, a, purged, 2, clock.now())

    assert run_purge_job(database, job, clock.now()) == "done"
    edit(database, a, clock, world["project"][1], {**PROJECT, "actions": "改写后的做法"})
    files = exported(database, a, clock, tmp_path)
    data, manifest = json.loads(files["data.json"]), json.loads(files["manifest.json"])

    assert data["format_version"] == manifest["format_version"] == EXPORT_FORMAT_VERSION == 8
    explorations = {entry["id"]: entry for entry in data["directions"]["explorations"]}
    assert set(explorations) == {kept, binned}
    assert (explorations[kept]["kept_as"], explorations[binned]["kept_as"], explorations[binned]["deleted_along"]) == ("in_use", "recycle_bin", False)
    assert explorations[kept]["basis_state"] == "changed" and explorations[kept]["directions"][0]["name"] == "数据分析"
    assert {entry["name"]: entry["basis_state"] for entry in data["directions"]["decisions"]} == {"数据分析": "changed", "运营": "personal"}
    counted = {key: manifest["counts"][key] for key in ("direction_explorations", "direction_decisions", "direction_recycle_bin")}
    assert counted == {"direction_explorations": 1, "direction_decisions": 2, "direction_recycle_bin": 1}
    assert "岗位分析结果、岗位简历和投递记录：这些功能还没有建立" in NOT_INCLUDED and manifest["not_included"] == list(NOT_INCLUDED)
    readable = files["directions.md"].decode("utf-8")
    assert "依据的资料之后改过" in readable and "你自己的选择" in readable and "回收站里的方向探索" in readable
    assert "directions.md" in files["README.md"].decode("utf-8")
    assert any(entry["path"] == "directions.md" for entry in manifest["files"])


def test_an_export_without_direction_records_still_says_so(database, owners, clock, tmp_path):
    a, _ = owners
    files = exported(database, a, clock, tmp_path)
    data = json.loads(files["data.json"])

    assert data["directions"] == {"explorations": [], "decisions": []} and "directions.md" not in files


# --- the existing deletion pages -----------------------------------------------------------------------------------------------


def test_the_deletion_pages_say_what_rests_on_a_record_and_delete_only_it(app, browser_a, clock):
    item_id = browser_a.create(title="合成项目", actions=ACTIONS)
    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    database = app.state.library.database

    with database.transaction() as db:
        item = db.get(ExperienceItem, item_id)
        owner, version = item.owner_id, item.confirmed_version_id

    source = ("experience", item_id, version, ("actions",))
    result = trusted(owner, clock, (source,), [direction("数据分析", [quote(source, "actions", "清洗订单数据")]), direction("运营"), direction("研究")])

    with database.transaction() as db:
        exploration_id = records.save_exploration(db, owner, result, clock.now()).id
        records.decide_on_saved(db, owner, exploration_id, "数据分析", "keep", None, clock.now())

    page = browser_a.get(f"/experiences/{item_id}/delete").text
    assert "有 1 次已保存的方向探索、1 条方向决定以它为依据：默认不会一起删除" in page
    assert "尚未建立与岗位分析、简历的关联" in page and "目前没有已保存的方向探索" not in page

    assert browser_a.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete").status_code == 303
    assert row(database, DirectionExploration, exploration_id).deleted_at is None

    purge_page = browser_a.get(f"/experiences/{item_id}/purge").text
    assert "这些探索里保存的引文不会随之清除" in purge_page


def test_a_versions_deletion_pages_add_a_line_only_when_something_rests_on_it(app, browser_a, clock):
    item_id = browser_a.create(title="合成项目", actions=ACTIONS)
    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    database = app.state.library.database

    with database.transaction() as db:
        item = db.get(ExperienceItem, item_id)
        owner, first = item.owner_id, item.confirmed_version_id
        initial = db.scalar(select(ExperienceVersion.id).where(ExperienceVersion.item_id == item_id, ExperienceVersion.kind == "initial"))

    source = ("experience", item_id, first, ("actions",))

    with database.transaction() as db:
        records.save_exploration(db, owner, trusted(owner, clock, (source,)), clock.now())

    edit(database, owner, clock, item_id, {"title": "合成项目", "actions": "新的做法"})

    referenced = browser_a.get(f"/experiences/{item_id}/versions/{first}/delete").text
    unreferenced = browser_a.get(f"/experiences/{item_id}/versions/{initial}/delete").text

    assert "有 1 次已保存的方向探索以这个版本为依据" in referenced
    # The page itself says nothing of directions (方向探索 is in the top navigation of every page since U3-C3a).
    assert "方向探索" not in unreferenced[unreferenced.index('<main id="main"'):]


# --- R1: a stale change to a decision made again, and what a record's permanent deletion takes at once --------------


def decide_through(entry, db, owner, clock, exploration_id, result, name, state, seen):
    """One of the three entries a decision is made or changed through."""
    if entry == "personal":
        return records.decide_personal(db, owner, name, state, seen, clock.now())

    if entry == "saved":
        return records.decide_on_saved(db, owner, exploration_id, name, state, seen, clock.now())

    return records.decide_on_unsaved(db, owner, result, name, state, seen, clock.now())


def decision_snapshot(database, decision_id):
    """Everything a change to a decision could touch: the row, what it rests on, what the audit says of it."""
    with database.transaction() as db:
        decision = db.get(DirectionDecision, decision_id)
        sources = sorted((s.kind, s.item_id, s.version_id, tuple(s.fields)) for s in db.scalars(select(DirectionSource).where(DirectionSource.holder_id == decision_id)))
        events = [event.action for event in db.scalars(select(AuditEvent).where(AuditEvent.object_id == decision_id))]

        return {
            "row": {column.name: getattr(decision, column.name) for column in DirectionDecision.__table__.columns},
            "sources": sources,
            "events": events,
        }


@pytest.mark.parametrize("again", ["数据分析", "数据 分析"])
@pytest.mark.parametrize("entry", ["personal", "saved", "unsaved"])
def test_a_stale_change_never_touches_a_decision_made_again_under_the_name(database, clock, world, entry, again):
    a = world["owner"]
    exploration_id, result = save(database, clock, world)

    # The decision a page saw (made through the entry under test), then deleted.
    with database.transaction() as db:
        first = decide_through(entry, db, a, clock, exploration_id, result, "数据分析", "keep", None)
        seen = records.SeenDecision(first.id, first.revision)
        records.delete_decision(db, a, first.id, first.revision, clock.now())

    # Made again under the same name (the same once compared), on another basis: its revision starts from 1 too.
    with database.transaction() as db:
        replacement = records.decide_personal(db, a, again, "exclude", None, clock.now()).id

    before = decision_snapshot(database, replacement)
    assert before["row"]["revision"] == seen.revision == 1

    with pytest.raises(RecordConflict):
        with database.transaction() as db:
            decide_through(entry, db, a, clock, exploration_id, result, "数据分析", "keep", seen)

    # Nothing of the decision made again moved: state, basis, link, what it rests on, its history.
    assert decision_snapshot(database, replacement) == before
    assert row(database, DirectionDecision, first.id).deleted_at is not None


@pytest.mark.parametrize("entry", ["personal", "saved", "unsaved"])
def test_a_change_to_a_decision_no_longer_in_use_makes_no_new_one(database, clock, world, entry):
    a = world["owner"]
    exploration_id, result = save(database, clock, world)

    with database.transaction() as db:
        first = decide_through(entry, db, a, clock, exploration_id, result, "数据分析", "keep", None)
        seen = records.SeenDecision(first.id, first.revision)
        records.delete_decision(db, a, first.id, first.revision, clock.now())

    with pytest.raises(RecordConflict):
        with database.transaction() as db:
            decide_through(entry, db, a, clock, exploration_id, result, "数据分析", "exclude", seen)

    with database.transaction() as db:
        assert records.decisions_in_use(db, a) == []

    # The page that saw it, and only that one, changes it while it is in use.
    with database.transaction() as db:
        records.restore_decision(db, a, first.id, 2, clock.now())
        changed = decide_through(entry, db, a, clock, exploration_id, result, "数据分析", "exclude", records.SeenDecision(first.id, 3))

    assert (changed.id, changed.state, changed.revision) == (first.id, "exclude", 4)


PERMANENT_CASES = {
    "experience": ("project", {**PROJECT, "reflection": "新的反思"}, True),
    "preferences": (PREFERENCES, {**PREFS, "cities_ideal": "上海"}, False),
}


@pytest.mark.parametrize("kind", list(PERMANENT_CASES))
def test_a_records_permanent_deletion_takes_at_once_what_went_with_its_versions(database, owners, clock, world, kind, monkeypatch):
    a, b = owners
    category, changed, confirm = PERMANENT_CASES[kind]
    source = world["project"] if kind == "experience" else world["prefs"]
    item_id, old_version = source[1], source[2]
    along, _ = save(database, clock, world)
    kept, _ = save(database, clock, world)
    alone, _ = save(database, clock, world)
    other_project = confirmed(database, b, clock)
    other_source = ("experience", other_project[0], other_project[1], ("actions",))
    others = trusted(b, clock, (other_source,), [direction("数据分析", [quote(other_source, "actions", "用 Python")]), direction("运营"), direction("研究")])

    with database.transaction() as db:
        decision = records.decide_on_saved(db, a, along, "数据分析", "keep", None, clock.now()).id
        theirs = records.save_exploration(db, b, others, clock.now()).id
        records.delete_exploration(db, a, alone, 1, clock.now())

    # The version used becomes history, is deleted with the exploration chosen to go along, then the record goes.
    edit(database, a, clock, item_id, changed, category, confirm=confirm)

    with database.transaction() as db:
        version = db.get(ExperienceVersion, old_version)
        delete_version(db, a, item_id, old_version, version_state(db, version), clock.now(), (category,), also_explorations=(along,))

    with database.transaction() as db:
        delete_experience(db, a, item_id, rev(db, item_id), clock.now(), (category,))

    with database.transaction() as db:
        job = request_experience_purge(db, a, item_id, rev(db, item_id), clock.now(), (category,))

    def check_reach():
        with database.transaction() as db:
            exported = {entry["id"]: entry["kept_as"] for entry in collect_export(db, a, clock.now()).directions.explorations}
            in_use = [exploration.id for exploration in records.explorations_in_use(db, a)]
            their_export = [entry["id"] for entry in collect_export(db, b, clock.now()).directions.explorations]

        # Gone from every way in at once; kept history and the one deleted on its own stay as they were.
        assert exported == {kept: "in_use", alone: "recycle_bin"} and in_use == [kept]
        assert their_export == [theirs]
        # Once purged there is no row left to name a revision of; any revision is refused all the same.
        along_row = row(database, DirectionExploration, along)
        along_revision = along_row.revision if along_row is not None else 1

        for attempt in (
            lambda db: records.restore_exploration(db, a, along, along_revision, clock.now()),
            lambda db: records.request_exploration_purge(db, a, along, along_revision, clock.now()),
            lambda db: records.exploration_basis(db, a, along, clock.now()),
            lambda db: records.decide_on_saved(db, a, along, "数据分析", "exclude", None, clock.now()),
        ):
            with pytest.raises(NotFound):
                with database.transaction() as db:
                    attempt(db)

        assert decision_basis(database, clock, a, decision) is BasisState.EXPLORATION_GONE
        assert basis(database, clock, a, kept) is SourceState.GONE

    # Requested, not cleaned up yet: the row is still there, out of reach.
    assert row(database, DirectionExploration, along) is not None
    check_reach()

    # The clean-up fails: said as failed, still out of reach.
    def fail(*args):
        raise OSError("disk")

    monkeypatch.setattr(lifecycle, "_remove", fail)
    assert run_purge_job(database, job, clock.now()) == "failed"
    assert row(database, PurgeJob, job).status == JobStatus.FAILED and row(database, DirectionExploration, along) is not None
    check_reach()

    # Tried again, it finishes; once more, nothing is left to do.
    monkeypatch.undo()
    assert run_purge_job(database, job, clock.now()) == "done"
    assert run_purge_job(database, job, clock.now()) == "skipped"
    assert row(database, DirectionExploration, along) is None

    with database.transaction() as db:
        assert not list(db.scalars(select(DirectionSource).where(DirectionSource.holder_id == along)))

    assert row(database, DirectionExploration, kept) is not None and row(database, DirectionExploration, alone) is not None
    assert row(database, DirectionExploration, theirs) is not None and row(database, DirectionDecision, decision) is not None
    check_reach()
