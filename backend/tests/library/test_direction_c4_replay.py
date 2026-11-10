"""Offline replay of the first real run's D2 answer (U3-C4 business F1; C4 report 9.2 and 9.4).

That run (approval u3c4-2026-11-09-a, the workflow's prompt version 1, digest 0e8dd1f0…) kept "组员负责搭建模型，我整理
了会议记录。" as the user's own basis in three directions, with the quotes set aside per direction at (1, 1, 3, 2). Its
answer, word for word, is fixtures/u3c4_d2_answer.json. It is checked again here by the local rules of today, against
D2's material as it was sent with the aliases of that run; no service is asked anything.
"""
import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from app.directions import live_cases
from app.directions.attribution import MIXED, whose
from app.directions.contracts import Relation
from app.directions.explore import FIELD_ORDER, SEND, SET_ASIDE_NOTE, UNFILLED, _Material, _Source, checked_result
from app.experience_analysis import quote_context, quote_flags

ANSWER = Path(__file__).parent / "fixtures" / "u3c4_d2_answer.json"
# What the run recorded (ROLERADAR_V0_1_U3_C4_LIVE_RUN/cases/D2.json, calls[0].answer), as UTF-8.
ANSWER_SHA256 = "bd09a54c9d0a889d0486c3ad9e0486ecabb6f65f6761ed34c551d91fa00f0333"
# The aliases the run's prompt gave D2's experiences.
ALIASES = {"S1": "work", "S2": "project", "S3": "activity"}
MIXED_SENTENCE = "组员负责搭建模型，我整理了会议记录。"


def d2():
    """D2's material as sent: the frozen case's every field, under the run's aliases; its one preference as F1."""
    case = next(case for case in live_cases.CASES if case.id == "D2")
    fields = dict(case.experiences)
    sources = tuple(_Source(alias, str(uuid.uuid4()), str(uuid.uuid4()), category, fields[category]) for alias, category in ALIASES.items())
    preferences = tuple(
        (name, SEND, "F1", case.preferences[name]) if name in case.preferences else (name, UNFILLED, None, "") for name in FIELD_ORDER
    )
    material = _Material(sources, preferences, str(uuid.uuid4()), str(uuid.uuid4()))

    return case, material, {source.item_id: source.category for source in sources}


def test_the_fixture_is_the_runs_own_answer():
    assert hashlib.sha256(ANSWER.read_bytes()).hexdigest() == ANSWER_SHA256


def test_the_runs_d2_answer_now_keeps_nothing_that_is_not_the_users_own_doing():
    case, material, categories = d2()
    result, set_aside = checked_result(ANSWER.read_text(encoding="utf-8"), material)

    assert [direction.name for direction in result.directions] == ["用户研究", "用户体验研究运营", "量化用户研究与产品数据分析", "市场研究与消费者洞察"]
    # The mixed sentence is set aside where it was kept, beside the plan, the denial and the claim set aside as before.
    assert set_aside == (2, 2, 3, 3)
    assert all(direction.evidence == () for direction in result.directions)
    assert [direction.unknowns[-1] for direction in result.directions] == [SET_ASIDE_NOTE.format(count) for count in set_aside]
    # The automatic checks that failed the run (hard criterion 5) find nothing now; the relations are as before.
    assert live_cases.judge(case, result, categories).problems == ()
    assert [link.relation for direction in result.directions for link in direction.ideal] == [
        Relation.FITS,
        Relation.UNKNOWN,
        Relation.UNKNOWN,
        Relation.UNKNOWN,
    ]


def test_why_the_run_kept_it_and_why_it_is_set_aside_now():
    # The reading of plans, others' work, denials and claims finds nothing in it: "组员" is none of its words.
    assert quote_flags(quote_context(MIXED_SENTENCE, MIXED_SENTENCE)) == ()
    # Read for whose doing it is, it mixes a teammate's clause with the user's.
    assert whose(MIXED_SENTENCE, MIXED_SENTENCE) == MIXED
    assert whose("我整理了会议记录", MIXED_SENTENCE) == "self"


# R2 (C4 report 11.2): the run's own answer, with D2's project telling something else and the first direction quoting
# it as the user's practice. What the user had others do, watched them do or did with them is not taken as the
# user's own doing unless the quote keeps the words saying so; the user's own clause beside it is.
REWORDED = [
    ("让：他人的动作", "我整理了会议记录，并让组员负责搭建模型。", "搭建模型", False),
    ("安排：他人的动作", "我安排队友搭建模型，并整理了会议记录。", "搭建模型", False),
    ("asked：他人的动作", "I asked my teammate to build the model, and I took notes.", "build the model", False),
    ("观察：他人的动作", "我观察组员搭建模型，并整理了会议记录。", "搭建模型", False),
    ("一起：只剩动作", "我和组员一起搭建了模型。", "搭建了模型", False),
    ("帮助：只剩动作", "帮助组员调试代码。", "调试代码", False),
    ("本人分句", "我整理了会议记录，并让组员负责搭建模型。", "我整理了会议记录", True),
    ("本人的安排，原话", "我整理了会议记录，并让组员负责搭建模型。", "让组员负责搭建模型", True),
    ("English: own clause", "I asked my teammate to build the model, and I took notes.", "I took notes", True),
    ("一起，原话", "我和组员一起搭建了模型。", "我和组员一起搭建了模型", True),
    ("帮助，原话", "帮助组员调试代码。", "帮助组员调试代码", True),
]


@pytest.mark.parametrize(("field", "quote", "kept"), [case[1:] for case in REWORDED], ids=[case[0] for case in REWORDED])
def test_the_runs_answer_quoting_other_wordings_keeps_only_what_the_user_did(field, quote, kept):
    _, material, _ = d2()
    sources = tuple(
        replace(source, content={**source.content, "actions": field}) if source.alias == "S2" else source for source in material.sources
    )
    payload = json.loads(ANSWER.read_text(encoding="utf-8"))

    for direction in payload["directions"]:
        direction["evidence"] = []

    payload["directions"][0]["evidence"] = [{"source_id": "S2", "field": "actions", "quote": quote, "basis": "practice"}]
    result, set_aside = checked_result(json.dumps(payload, ensure_ascii=False), replace(material, sources=sources))

    assert [item.text for item in result.directions[0].evidence] == ([quote] if kept else [])
    assert set_aside[0] == (0 if kept else 1)
