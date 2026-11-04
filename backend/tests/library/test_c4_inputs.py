"""The pre-send check of a frozen C4 job case's stored inputs (U2-C4 R1), on synthetic libraries only.

The archive here is made up in the form of the R11 one, and it stops at its
first line if run: every test that reads it shows it is only parsed.
"""
import hashlib
import sqlite3

import pytest

from analysis_helpers import confirmed, saved_preferences
from app.library.content import parse_experience_form
from app.library.service import create_experience
from c4_inputs import _read_only, check, load_cases, main

ARCHIVE = '''
raise SystemExit("an archive is parsed, never run")

CASES = (
    Case(
        name="case1",
        title="合成样例：偏好的立场",
        posting="合成岗位\\n要求：熟练使用 Python\\n简历投递：hr@example.com",
        experiences=(
            ("project", {"title": "合成看板", "actions": "用 Python 做了每周看板。"}),
            ("education", {"school": "合成大学", "graduation": "2027 年 6 月（预计）"}),
        ),
        preferences={"cities_ideal": "上海", "work_style_no": "频繁出差"},
        expects=(Expect("Python", (SUPPORTED,), anchor="Python"),),
    ),
    Case(
        name="case2",
        title="合成样例：不选偏好",
        posting="Synthetic role",
        experiences=(("other", {"title": "Skills note", "notes": "Synthetic."}),),
        preferences={},
        expects=(),
    ),
)
'''
# Case 1 of the paused C4 batch: 频繁出差 saved under the ideal, not under what cannot be accepted.
AS_ENTERED = {"cities_ideal": "上海", "work_style_ideal": "频繁出差"}


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.py"
    path.write_text(ARCHIVE, encoding="utf-8")

    return path


@pytest.fixture
def library(tmp_path, database, owners, clock):
    """seed(**preferences) stores case 1's experiences and the given preferences for user A; the library's file."""

    def seed(**preferences):
        confirmed(database, owners[0], clock, "project", title="合成看板", actions="用 Python 做了每周看板。")
        confirmed(database, owners[0], clock, "education", school="合成大学", graduation="2027 年 6 月（预计）")
        saved_preferences(database, owners[0], clock, **preferences)

        return tmp_path / "service.db"

    return seed


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_inputs_as_frozen_pass_and_the_page_choices_are_printed(archive, library, capsys):
    db_path = library(cities_ideal="上海", work_style_no="频繁出差")

    assert check(db_path, "合成用户 A", load_cases(archive)["case1"]) == []
    assert main(["--db", str(db_path), "--archive", str(archive), "--user", "A", "--case", "case1"]) == 0

    printed = capsys.readouterr().out
    assert "一致" in printed and "目标城市（理想）：上海；工作方式（不能接受）：频繁出差" in printed
    assert "项目/作品「合成看板」、教育「合成大学」" in printed and "简历投递：hr@example.com" in printed


def test_a_preference_under_another_stance_is_caught_and_nothing_may_be_sent(archive, library, capsys):
    db_path = library(**AS_ENTERED)
    problems = check(db_path, "合成用户 A", load_cases(archive)["case1"])

    assert problems == [
        "偏好多填了工作方式（理想）：“频繁出差”，冻结样例里没有这一项。",
        "偏好缺少工作方式（不能接受）：应为“频繁出差”（库里填在了工作方式（理想））。",
    ]
    assert main(["--db", str(db_path), "--archive", str(archive), "--user", "A", "--case", "case1"]) == 1
    assert "不一致，不要发送" in capsys.readouterr().out


@pytest.mark.parametrize(
    "fields, says",
    [
        ({"title": "合成看板", "actions": "用 Python 做了每周看板。", "outcome": "多写的一栏。"}, "结果或现状应为“”，库里是“多写的一栏。”"),
        ({"title": "合成看板", "actions": "用 Python 做了看板。"}, "做了什么应为“用 Python 做了每周看板。”，库里是“用 Python 做了看板。”"),
    ],
    ids=["a-field-more", "another-value"],
)
def test_an_experience_not_as_frozen_is_caught_field_by_field(archive, tmp_path, database, owners, clock, fields, says):
    confirmed(database, owners[0], clock, "project", **fields)
    confirmed(database, owners[0], clock, "education", school="合成大学", graduation="2027 年 6 月（预计）")
    saved_preferences(database, owners[0], clock, cities_ideal="上海", work_style_no="频繁出差")

    (problem,) = check(tmp_path / "service.db", "合成用户 A", load_cases(archive)["case1"])

    assert problem.startswith("项目/作品「合成看板」和冻结样例不同") and says in problem


def test_an_experience_left_as_a_draft_or_stored_for_the_other_user_is_not_there(archive, tmp_path, database, owners, clock):
    with database.transaction() as db:
        create_experience(db, owners[0], parse_experience_form({"title": "合成看板", "actions": "用 Python 做了每周看板。"}, "project"), clock.now())

    confirmed(database, owners[1], clock, "education", school="合成大学", graduation="2027 年 6 月（预计）")
    saved_preferences(database, owners[0], clock, cities_ideal="上海", work_style_no="频繁出差")

    assert check(tmp_path / "service.db", "合成用户 A", load_cases(archive)["case1"]) == [
        "没有已确认的项目/作品「合成看板」（草稿要先确认）。",
        "没有已确认的教育「合成大学」（草稿要先确认）。",
    ]


def test_a_case_without_preferences_asks_for_none_on_the_page(archive, tmp_path, database, owners, clock, capsys):
    confirmed(database, owners[0], clock, "other", title="Skills note", notes="Synthetic.")
    # Another case's preferences may be saved meanwhile: this case never uses them.
    saved_preferences(database, owners[0], clock, **AS_ENTERED)

    assert main(["--db", str(tmp_path / "service.db"), "--archive", str(archive), "--user", "A", "--case", "case2"]) == 0
    assert "不要勾选「参考我的求职偏好」" in capsys.readouterr().out


def test_the_check_changes_nothing_in_the_library_it_reads(archive, library, tmp_path):
    db_path = library(**AS_ENTERED)
    before = digest(db_path)

    main(["--db", str(db_path), "--archive", str(archive), "--user", "A", "--case", "case1"])

    assert digest(db_path) == before

    # Its connection refuses any write, and a library that is not there is never made.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        _read_only(db_path).execute("CREATE TABLE written (anything)")

    with pytest.raises(FileNotFoundError):
        check(tmp_path / "missing.db", "合成用户 A", load_cases(archive)["case1"])

    assert not (tmp_path / "missing.db").exists() and digest(db_path) == before
