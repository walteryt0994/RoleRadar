"""Candidates read from an import, and their review (U2-B2a).

All material is synthetic. Candidates come from the local simulated rules,
or from stand-in structurers that answer badly, late or not at all. Nothing
here adopts a candidate: this batch writes nothing to the library, and the
tests compare the library's actual rows, column by column, before and after.
"""
import io
import json
import logging
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.library.candidates import (
    CANDIDATE_FIELDS,
    BAD_SEGMENT,
    CUT_CONTEXT,
    FORMAT,
    INVALID_ANSWER,
    LOCAL_ONLY,
    MASKED,
    MAX_CANDIDATES,
    NEGATED,
    NONE_VALID,
    NOT_IN_SOURCE,
    NOT_ONE_LINE,
    NOTHING_FOUND,
    PLAN,
    TEAM,
    TOO_LONG,
    TOO_MANY,
    UNCERTAIN_DATE,
    UNKNOWN_FIELD,
    UNKNOWN_KIND,
    UNSPLIT,
    check_answer,
    check_sent_answer,
    current_values,
    where,
)
from app.library.imports import IMPORT_WINDOW, discard
from app.library.outbound_resume import outbound
from app.library.models import (
    Attachment,
    AuditEvent,
    CandidateState,
    ExperienceItem,
    ExperienceVersion,
    ImportCandidate,
    ImportStructuring,
    PurgeJob,
    ResumeImport,
    StructuringState,
    User,
)
from app.library.resume_rules import SIMULATION_NAME, SimulatedStructurer, structure
from app.library.review import (
    CONTAINED,
    DIFFERS,
    FILLS,
    SAME,
    STALE_STRUCTURING_AFTER,
    Asked,
    field_state,
    structure_import,
)
from import_samples import UNREADABLE, make_pdf
from library_helpers import Browser, form_for, forms_in

RESUME = """合成学生 Synthetic Student
姓名：林合成 | 城市：杭州
电话：138 0000 0000 | 邮箱：lin.synthetic@example.com
作品集：https://example.com/portfolio

教育经历
合成大学 | 计算机科学与技术 | 本科 | 2021.09-2025.06（预计毕业）
主修课程：数据结构、数据库

实习经历
合成数据科技有限公司 | 数据分析实习生 | 2025.06 - 2025.09
• 参与搭建销售看板，团队共 4 人
• 负责每周的数据清洗
• 没有使用 Docker，部署由同事完成

项目经历
校园二手书交换小程序 团队项目 2025.03-2025.06
- 使用 Python 和 Flask 编写接口
- 计划下学期加入推荐功能

获奖情况
2024.05 全国大学生数学建模竞赛 省级二等奖
英语六级

专业技能
Python、SQL、Tableau

求职意向
数据分析实习 | 上海 / 杭州
"""
PASTED = [{"kind": "text", "number": 1, "text": RESUME}]
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'
# ASCII, so it would show in the database file as it is (JSON keeps other text escaped).
CANARY = "CANARY-b2a-7f3e"


# --- helpers ------------------------------------------------------------------


def rt(app):
    return app.state.library


def owner_id(app, label: str = "合成用户 A") -> str:
    with rt(app).database.transaction() as db:
        return db.scalar(select(User.id).where(User.display_label == label))


def paste(browser, text: str = RESUME) -> str:
    """Import pasted text through the import page's form; return the import's id."""
    form = browser.form("/imports", "/imports")
    fields = {name: value for name, value in form.fields.items() if name not in ("file", "text", "consent")}
    response = browser.client.post(
        "/imports",
        data={**fields, "text": text, "consent": "yes"},
        files={"file": ("", b"", "application/octet-stream")},
    )
    assert response.status_code == 303, response.text

    return re.match(r"^/imports/([0-9a-f-]{36})", response.headers["location"]).group(1)


def upload(browser, filename: str, data: bytes, media_type: str) -> str:
    form = browser.form("/imports", "/imports")
    fields = {name: value for name, value in form.fields.items() if name not in ("file", "text", "consent")}
    response = browser.client.post("/imports", data={**fields, "consent": "yes"}, files={"file": (filename, data, media_type)})
    assert response.status_code == 303, response.text

    return re.match(r"^/imports/([0-9a-f-]{36})", response.headers["location"]).group(1)


def ask(browser, import_id: str):
    """Press the import page's button that asks for candidates."""
    return browser.submit(f"/imports/{import_id}", f"/imports/{import_id}/structure")


def rows(app, import_id: str) -> list[ImportCandidate]:
    with rt(app).database.transaction() as db:
        return list(
            db.scalars(select(ImportCandidate).where(ImportCandidate.import_id == import_id).order_by(ImportCandidate.position))
        )


def one(app, import_id: str, kind: str) -> ImportCandidate:
    (found,) = [row for row in rows(app, import_id) if row.kind == kind][:1]

    return found


def structuring(app, import_id: str) -> ImportStructuring | None:
    with rt(app).database.transaction() as db:
        return db.get(ImportStructuring, import_id)


def import_row(app, import_id: str) -> ResumeImport | None:
    with rt(app).database.transaction() as db:
        return db.get(ResumeImport, import_id)


def group(page: str, kind: str) -> str:
    """One kind's part of the review page."""
    start = page.index(f'id="g-{kind}"')
    end = page.find('<section class="review-group"', start)

    return page[start:] if end == -1 else page[start:end]


def library_rows(app) -> dict:
    """The library's own rows, every column of each: content, versions and how they are tied together."""
    def all_of(model) -> list[dict]:
        found = [{column.key: getattr(row, column.key) for column in model.__table__.columns} for row in db.scalars(select(model))]

        return sorted(found, key=lambda row: str(row.get("id")))

    with rt(app).database.transaction() as db:
        return {
            "items": all_of(ExperienceItem),
            "versions": all_of(ExperienceVersion),
            "attachments": all_of(Attachment),
            "purge_jobs": all_of(PurgeJob),
            "events": [row for row in all_of(AuditEvent) if row["object_type"] != "resume_import"],
        }


def fill_library(browser) -> None:
    """Some of everything the library holds: experiences (one confirmed), an attachment, a profile, preferences."""
    school = browser.create(category="education", school="合成大学", major="数学", stage="本科")
    project = browser.create(title="合成项目", actions="合成的行动")
    browser.submit(f"/experiences/{project}", f"/experiences/{project}/confirm")
    assert browser.upload(school).status_code == 303
    page = browser.get("/basic-profile/edit").text
    [form] = [form for form in forms_in(page) if form.method == "post" and form.action.startswith("/basic-profile")]
    assert browser.post(form.action, {**form.fields, "phone": "139 1111 2222", "city": "杭州"}).status_code == 303
    page = browser.get("/job-preferences/edit").text
    [form] = [form for form in forms_in(page) if form.method == "post" and form.action.startswith("/job-preferences")]
    assert browser.post(form.action, {**form.fields, "cities_ideal": "上海"}).status_code == 303


def answer(*candidates) -> str:
    return json.dumps({"candidates": list(candidates)}, ensure_ascii=False)


def piece(quote: str, segment: int = 1) -> list[dict]:
    return [{"segment": segment, "quote": quote}]


@contextmanager
def refused_updates(app, table: str):
    """While open, the database refuses every UPDATE of this table, as a full or failing disk would."""
    engine = rt(app).database.engine

    def refuse(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(f"UPDATE {table.upper()}"):
            raise OperationalError(statement, {}, Exception("synthetic disk failure"))

    event.listen(engine, "before_cursor_execute", refuse)

    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", refuse)


def field_entry(kind: str, name: str):
    return next(entry for entry in CANDIDATE_FIELDS[kind] if entry.name == name)


class Fixed:
    """A stand-in structurer that always gives the same answer, or raises."""

    name = "test:fixed"

    def __init__(self, reply: str | Exception):
        self.reply = reply

    def structure(self, segments):
        if isinstance(self.reply, Exception):
            raise self.reply

        return self.reply


class Gate:
    """A stand-in structurer that waits until released, then answers as the simulation would."""

    name = "test:gated"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def structure(self, segments):
        self.entered.set()
        assert self.release.wait(10)

        return SimulatedStructurer().structure(segments)


# --- the contract: words found in the text, nothing cut out of what it said ------------------------


def test_a_value_is_kept_only_where_its_words_stand_in_the_text():
    segments = [{"kind": "text", "number": 1, "text": "合成数据科技有限公司 | 数据分析实习生\n负责每周的数据清洗"}]
    checked = check_answer(
        answer(
            {"kind": "work", "fields": {"organization": piece("合成数据科技有限公司"), "actions": piece("负责每周的数据清洗")}},
            {"kind": "work", "fields": {"organization": piece("合成数据科技有限公司（总部）")}},
        ),
        segments,
    )

    (found,) = checked.candidates
    assert found.extracted == {"organization": piece("合成数据科技有限公司"), "actions": piece("负责每周的数据清洗")}
    assert [where(segments, item) for item in found.extracted["actions"]] == ["粘贴的文字第 2 行"]
    assert checked.problems == [{"position": 2, "kind": "work", "segments": [1], "reason": NOT_IN_SOURCE}]


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("没有使用 Docker，部署由同事完成", "使用 Docker"),
        ("参与搭建销售看板，团队共 4 人", "搭建销售看板"),
        ("计划下学期加入推荐功能", "下学期加入推荐功能"),
        ("合成大学 | 2027.06（预计）", "2027.06"),
        ("I did not deploy the service", "deploy the service"),
        ("Assisted the team with data cleaning", "data cleaning"),
        # A marker the words only half include was cut too.
        ("没有用过 Docker", "有用过 Docker"),
    ],
)
def test_words_cut_out_of_a_negation_team_work_a_plan_or_an_uncertain_date_are_dropped(text, quote):
    segments = [{"kind": "text", "number": 1, "text": text}]
    field = "graduation" if quote == "2027.06" else "notes"
    checked = check_answer(answer({"kind": "education", "fields": {field: piece(quote)}}), segments)

    assert checked.candidates == [] and checked.failure == NONE_VALID
    assert checked.problems == [{"position": 1, "kind": "education", "segments": [1], "reason": CUT_CONTEXT}]


def test_the_same_words_inside_a_value_are_kept_and_marked():
    lines = ["没有使用 Docker，部署由同事完成", "参与搭建销售看板", "计划下学期加入推荐功能", "2027.06（预计）"]
    segments = [{"kind": "text", "number": 1, "text": "\n".join(lines)}]
    checked = check_answer(
        answer(
            {
                "kind": "work",
                "fields": {"actions": [item for line in lines[:3] for item in piece(line)], "period": piece(lines[3])},
            }
        ),
        segments,
    )

    (found,) = checked.candidates
    assert {(caution["field"], caution["code"]) for caution in found.cautions} == {
        ("actions", NEGATED),
        ("actions", TEAM),
        ("actions", PLAN),
        ("period", UNCERTAIN_DATE),
    }


@pytest.mark.parametrize(
    ("text", "field", "quote"),
    [
        # Another cell, or another sentence, is not what these words said.
        ("合成大学 | 2021.09-2025.06（预计毕业）", "school", "合成大学"),
        ("负责数据清洗。没有使用 Docker", "notes", "负责数据清洗"),
        # A time is no achievement, whoever it was shared with.
        ("校园二手书交换小程序 团队项目 2025.03-2025.06", "graduation", "2025.03-2025.06"),
    ],
)
def test_markers_elsewhere_do_not_drop_a_value(text, field, quote):
    checked = check_answer(answer({"kind": "education", "fields": {field: piece(quote)}}), [{"kind": "text", "number": 1, "text": text}])

    (found,) = checked.candidates
    assert found.cautions == [] and checked.problems == []


def test_each_broken_candidate_is_dropped_with_its_place_and_reason_only():
    segments = [
        {"kind": "page", "number": 1, "text": "合成大学\n负责数据清洗"},
        {"kind": "page", "number": 2, "text": "", "unreadable": True},
    ]
    good = {"kind": "education", "fields": {"school": piece("合成大学")}}
    broken = [
        {"kind": "salary", "fields": {"text": piece("合成大学")}},
        {"kind": "education", "fields": {"private_note": piece("合成大学")}},
        {"kind": "education", "fields": {"school": piece("合成大学", 3)}},
        {"kind": "education", "fields": {"school": piece("合成大学", 2)}},
        {"kind": "education", "fields": {"school": [{"segment": True, "quote": "合成大学"}]}},
        {"kind": "education", "fields": {"school": piece("合成大学") + piece("负责数据清洗")}},
        {"kind": "education", "fields": {"school": piece("合成大学\n负责数据清洗")}},
        {"kind": "education", "fields": {"school": piece("合成大学")}, "notes": ["invented"]},
        {"kind": "award", "fields": {"name": piece("合成大学")}, "extra": 1},
    ]
    checked = check_answer(answer(good, *broken), segments)

    assert [found.extracted for found in checked.candidates] == [good["fields"]]
    assert [(problem["position"], problem["reason"]) for problem in checked.problems] == [
        (2, UNKNOWN_KIND),
        (3, UNKNOWN_FIELD),
        (4, BAD_SEGMENT),
        (5, BAD_SEGMENT),
        (6, FORMAT),
        (7, NOT_ONE_LINE),
        (8, NOT_ONE_LINE),
        (9, FORMAT),
        (10, FORMAT),
    ]
    assert checked.problems[0]["kind"] is None and checked.problems[2]["segments"] == [] and checked.problems[3]["segments"] == [2]
    # Places and reasons only: none of the words.
    assert "合成大学" not in json.dumps(checked.problems, ensure_ascii=False)


def test_the_longest_texts_are_checked_in_moments():
    # One line said twelve thousand times: far too long for its field, so it is never searched for.
    repeated = [{"kind": "text", "number": 1, "text": ("项目经历\n合成项目 | 后端 | 2025\n" + "• 负责数据清洗\n" * 12_000)[:100_000]}]
    # A short piece that stands thirty thousand times, each time in its own sentence.
    frequent = [{"kind": "text", "number": 1, "text": "数据。" * 30_000}]
    started = time.perf_counter()
    long_one = check_answer(SimulatedStructurer().structure(repeated), repeated)
    common_one = check_answer(answer({"kind": "skills", "fields": {"text": piece("数据")}}), frequent)

    assert time.perf_counter() - started < 5
    assert long_one.problems == [{"position": 1, "kind": "project", "segments": [1], "reason": TOO_LONG}]
    assert [found.extracted for found in common_one.candidates] == [{"text": piece("数据")}]


def test_a_value_longer_than_its_field_is_dropped():
    line = "合" * 201
    checked = check_answer(answer({"kind": "work", "fields": {"role": piece(line)}}), [{"kind": "text", "number": 1, "text": line}])

    assert checked.problems == [{"position": 1, "kind": "work", "segments": [1], "reason": TOO_LONG}]


@pytest.mark.parametrize(
    ("reply", "failure"),
    [
        ("不是 JSON", INVALID_ANSWER),
        ("[]", INVALID_ANSWER),
        ('{"candidates": {}}', INVALID_ANSWER),
        ('{"candidates": [], "extra": 1}', INVALID_ANSWER),
        (answer(*[{"kind": "other", "fields": {"title": piece("合成")}}] * (MAX_CANDIDATES + 1)), TOO_MANY),
        (answer(), NOTHING_FOUND),
        (answer({"kind": "other", "fields": {"title": piece("不在原文里")}}), NONE_VALID),
    ],
)
def test_an_answer_that_gives_nothing_usable_keeps_nothing(reply, failure):
    checked = check_answer(reply, [{"kind": "text", "number": 1, "text": "合成"}])

    assert checked.candidates == [] and checked.failure == failure


# --- the local simulation ----------------------------------------------------------------------


def test_the_simulation_finds_the_usual_sections_of_a_chinese_resume():
    checked = check_answer(SimulatedStructurer().structure(PASTED), PASTED)
    found = {}

    for candidate in checked.candidates:
        found.setdefault(candidate.kind, []).append(candidate)

    values = {kind: [current_values(kind, item.extracted, None) for item in items] for kind, items in found.items()}
    cautions = {kind: [{caution["code"] for caution in item.cautions} for item in items] for kind, items in found.items()}

    assert checked.problems == [] and checked.failure is None
    assert [candidate.kind for candidate in checked.candidates] == [
        "profile", "education", "work", "project", "award", "award", "skills", "intention",
    ]
    # The name comes only from its label, never from where a line stands.
    assert values["profile"][0] == {
        "name": "林合成",
        "phone": "138 0000 0000",
        "email": "lin.synthetic@example.com",
        "city": "杭州",
        "links": "https://example.com/portfolio",
    }
    # A date range is never guessed into a graduation date.
    assert values["education"][0]["graduation"] == ""
    assert values["education"][0]["notes"] == "2021.09-2025.06（预计毕业）\n主修课程：数据结构、数据库"
    assert cautions["education"] == [{UNCERTAIN_DATE}]
    assert values["work"][0]["role"] == "数据分析实习生" and values["work"][0]["period"] == "2025.06 - 2025.09"
    assert cautions["work"] == [{TEAM, NEGATED}]
    assert values["project"][0]["title"] == "校园二手书交换小程序 团队项目"
    assert cautions["project"] == [{TEAM, PLAN, UNSPLIT}]
    assert [item["name"] for item in values["award"]] == ["全国大学生数学建模竞赛 省级二等奖", "英语六级"]
    assert values["skills"][0]["text"] == "Python、SQL、Tableau"
    assert values["intention"][0]["text"] == "数据分析实习 | 上海 / 杭州"


def test_the_simulation_reads_pages_paragraphs_and_tables_and_skips_what_was_not_read():
    segments = [
        {"kind": "page", "number": 1, "text": "Education\nExample University | B.S. Data Science | Expected May 2027"},
        {"kind": "page", "number": 2, "text": "", "unreadable": True},
        {"kind": "page", "number": 3, "text": "Projects\nCampus Book Exchange | Backend | 2025\n- Built the API with Flask"},
        {"kind": "paragraph", "number": 1, "text": "项目经历"},
        {"kind": "table", "number": 1, "rows": [["校园活动报名工具", "后端", "2025.03-2025.06"]]},
        {"kind": "paragraph", "number": 2, "text": "• 设计报名接口"},
    ]
    checked = check_answer(SimulatedStructurer().structure(segments), segments)
    education, english, chinese = checked.candidates

    assert checked.problems == []
    assert current_values("education", education.extracted, None)["graduation"] == "Expected May 2027"
    assert {caution["code"] for caution in education.cautions} == {UNCERTAIN_DATE}
    assert current_values("project", english.extracted, None)["actions"] == "Built the API with Flask"
    assert where(segments, english.extracted["actions"][0]) == "第 3 页第 3 行"
    assert where(segments, chinese.extracted["title"][0]) == "表格 1 第 1 行"
    assert where(segments, chinese.extracted["actions"][0]) == "第 2 段"
    assert all(item["segment"] != 2 for found in checked.candidates for items in found.extracted.values() for item in items)


def test_text_without_known_sections_gives_nothing_to_review():
    assert structure([{"kind": "text", "number": 1, "text": "一段没有标题的合成文字\n第二行"}]) == []


# --- asking for candidates on the pages ----------------------------------------------------------


def test_candidates_are_shown_with_their_sources_and_nothing_reaches_the_library(app, browser_a):
    fill_library(browser_a)
    before = library_rows(app)
    import_id = paste(browser_a)
    page = browser_a.get(f"/imports/{import_id}").text

    assert '<button type="submit" class="primary">整理成待核对的条目</button>' in page
    response = ask(browser_a, import_id)
    assert response.status_code == 303 and response.headers["location"] == f"/imports/{import_id}/review?notice=structured"
    page = browser_a.get(response.headers["location"]).text

    assert page.count("已整理出待核对的条目") == 1
    assert "本机模拟整理规则（不是 AI）" in page and "什么都没有发送出去" in page
    assert "这不代表内容已经核实" in page and "已验证" not in page and "属实" not in page
    assert "这一栏逐字摘自原文粘贴的文字第 11 行" in page
    assert "原文写的是团队工作" in page and "原文这里是否定的说法" in page and "原文写的是计划或打算" in page
    assert "技能与求职意向" in page and "不会改动你现在的求职偏好" in page and "不会被当作做过某件事的证据" in page
    assert "共 8 条：待核对 8 条" in page
    # No adopting: the only posts are leaving out, and logging out.
    posts = {form.action for form in forms_in(page) if form.method == "post"}
    assert posts == {"/logout"} | {f"/imports/{import_id}/candidates/{row.id}/exclude" for row in rows(app, import_id)}
    assert "加入经历库</button>" not in page
    assert library_rows(app) == before


def test_candidates_are_asked_for_once_so_edits_and_choices_stay(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    browser_a.submit(edit, edit, role=f"实习生 {CANARY}")
    first = [(row.id, row.edits) for row in rows(app, import_id)]

    response = browser_a.post(f"/imports/{import_id}/structure", {"csrf_token": browser_a.csrf_token()})

    assert response.headers["location"] == f"/imports/{import_id}/review?notice=already_structured"
    assert [(row.id, row.edits) for row in rows(app, import_id)] == first
    assert "这次没有重新整理" in browser_a.get(response.headers["location"]).text


def test_an_answer_with_nothing_usable_keeps_nothing_and_can_be_asked_again(app, browser_a):
    import_id = paste(browser_a)
    rt(app).structurer = Fixed("不是 JSON")
    response = ask(browser_a, import_id)
    page = browser_a.get(response.headers["location"]).text

    assert rows(app, import_id) == [] and structuring(app, import_id).state == StructuringState.FAILED
    assert "整理结果不是约定的格式，什么都没有保存" in page and ">重新整理</button>" in page
    assert "上次没有整理出可以核对的条目" in browser_a.get(f"/imports/{import_id}").text

    rt(app).structurer = SimulatedStructurer()
    page = browser_a.get(browser_a.submit(f"/imports/{import_id}/review", f"/imports/{import_id}/structure").headers["location"]).text

    assert len(rows(app, import_id)) == 8 and structuring(app, import_id).structurer == SIMULATION_NAME
    assert "共 8 条" in page and "重新整理" not in page


def test_dropped_candidates_are_listed_by_place_and_reason_without_their_words(app, browser_a):
    import_id = paste(browser_a)
    rt(app).structurer = Fixed(
        answer(
            {"kind": "work", "fields": {"organization": piece("合成数据科技有限公司")}},
            {"kind": "work", "fields": {"outcome": piece(f"销售额提升 30% {CANARY}")}},
        )
    )
    page = browser_a.get(ask(browser_a, import_id).headers["location"]).text

    assert len(rows(app, import_id)) == 1
    assert "另有 1 条整理结果没有通过原文核对" in page
    assert "第 2 条（实习/工作/兼职），出自粘贴的文字：引用的文字在原文里找不到。" in page
    assert CANARY not in page and "30%" not in page
    record = json.loads(browser_a.get(f"/imports/{import_id}/candidates.json").content)
    assert record["dropped"] == [
        {
            "position": 2, "kind": "work", "kind_label": "实习/工作/兼职", "where": ["粘贴的文字"],
            "reason": NOT_IN_SOURCE, "reason_text": "引用的文字在原文里找不到",
        }
    ]
    assert CANARY not in json.dumps(record, ensure_ascii=False)


def test_a_failing_structurer_keeps_nothing_and_logs_no_text(app, browser_a, caplog):
    import_id = paste(browser_a)
    rt(app).structurer = Fixed(RuntimeError(f"合成正文 {CANARY}"))

    with caplog.at_level(logging.WARNING, logger="roleradar.library"):
        page = browser_a.get(ask(browser_a, import_id).headers["location"]).text

    assert rows(app, import_id) == [] and "整理时出了错，什么都没有保存" in page
    assert "RuntimeError" in caplog.text and CANARY not in caplog.text


@pytest.mark.parametrize("ending", ["discard", "expire"])
def test_an_answer_that_comes_after_its_import_ended_is_dropped(app, browser_a, clock, ending):
    import_id = paste(browser_a)
    owner, gate = owner_id(app), Gate()

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(structure_import, rt(app).database, gate, owner, import_id, clock.now)
        assert gate.entered.wait(10)

        if ending == "discard":
            discard(rt(app).database, rt(app).files, rt(app).readings, owner, import_id, clock.now())
        else:
            clock.advance(days=IMPORT_WINDOW.days, seconds=1)

        gate.release.set()
        assert running.result(timeout=10) == Asked.DROPPED

    assert rows(app, import_id) == []
    browser_a.sign_in("合成用户 A")
    assert browser_a.get(f"/imports/{import_id}/review").status_code == 404
    rt(app).run_cleanup()
    assert structuring(app, import_id) is None and import_row(app, import_id).state in ("discarded", "expired")


def test_a_newer_run_wins_over_an_older_one_that_answers_late(app, browser_a, clock):
    import_id = paste(browser_a)
    owner, gate = owner_id(app), Gate()

    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(structure_import, rt(app).database, gate, owner, import_id, clock.now)
        assert gate.entered.wait(10)
        # The first run looks lost by now (its request would be gone), so it may be started again.
        clock.advance(seconds=STALE_STRUCTURING_AFTER.total_seconds() + 1)
        assert structure_import(rt(app).database, SimulatedStructurer(), owner, import_id, clock.now) == Asked.STORED
        kept = [row.id for row in rows(app, import_id)]
        gate.release.set()
        assert late.result(timeout=10) == Asked.DROPPED

    assert [row.id for row in rows(app, import_id)] == kept and structuring(app, import_id).structurer == SIMULATION_NAME


def test_two_asks_at_once_make_one_set_of_candidates(app, browser_a, clock):
    import_id = paste(browser_a)
    owner, gate = owner_id(app), Gate()

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(structure_import, rt(app).database, gate, owner, import_id, clock.now)
        assert gate.entered.wait(10)
        assert structure_import(rt(app).database, SimulatedStructurer(), owner, import_id, clock.now) == Asked.RUNNING
        # The import page offers no button meanwhile; a post from an older page is told the same.
        assert "正在整理" in browser_a.get(f"/imports/{import_id}").text
        response = browser_a.post(f"/imports/{import_id}/structure", {"csrf_token": browser_a.csrf_token()})
        assert "正在整理" in browser_a.get(response.headers["location"]).text
        gate.release.set()
        assert first.result(timeout=10) == Asked.STORED

    assert len(rows(app, import_id)) == 8


def test_a_pdf_is_structured_with_page_and_line_sources(app, browser_a):
    data = make_pdf([["项目经历", "校园活动报名工具 | 后端 | 2025.03-2025.06", "- 设计报名接口"], UNREADABLE])
    import_id = upload(browser_a, "合成简历.pdf", data, "application/pdf")
    page = browser_a.get(ask(browser_a, import_id).headers["location"]).text

    assert [row.kind for row in rows(app, import_id)] == ["project"]
    assert "这一栏逐字摘自原文第 1 页第 3 行" in page and "有页面没有读出来，整理结果可能缺内容" in page


# --- reviewing them -------------------------------------------------------------------------------


def test_edits_are_kept_apart_from_what_was_found(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    page = browser_a.get(edit).text

    assert "自动整理（逐字摘自原文粘贴的文字第 11 行）：</span><span class=\"text\">数据分析实习生" in page
    assert "不是长期草稿" in page and "不会当作原文引用" in page
    response = browser_a.submit(edit, edit, role="数据分析实习生（暑期）")
    assert response.headers["location"] == f"/imports/{import_id}/review?notice=candidate_saved#c-{work.id}"

    saved = one(app, import_id, "work")
    assert saved.edits == {"role": "数据分析实习生（暑期）"} and saved.extracted == work.extracted and saved.revision == 2
    page = browser_a.get(f"/imports/{import_id}/review").text
    assert "自动整理的内容：<span class=\"text\">数据分析实习生</span>" in page and "其中你改过 1 条" in page

    record = json.loads(browser_a.get(f"/imports/{import_id}/candidates.json").content)
    (entry,) = [item for item in record["candidates"] if item["id"] == work.id]
    assert entry["fields"]["role"] == {
        "label": "岗位或角色",
        "found": "数据分析实习生",
        "sources": [{"where": "粘贴的文字第 11 行", "segment": 1, "quote": "数据分析实习生"}],
        "edited": "数据分析实习生（暑期）",
    }

    # Writing the found words again is no edit.
    browser_a.submit(edit, edit, role="数据分析实习生")
    assert one(app, import_id, "work").edits is None


def test_an_edit_from_a_stale_page_is_refused_and_keeps_what_was_typed(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    stale = browser_a.form(edit, edit)
    browser_a.submit(edit, edit, role="另一个标签页写的")

    # A browser sends the line breaks of a text area as CRLF; that is no change.
    sent = {**stale.fields, "role": f"我刚才写的 {CANARY}", "actions": stale.fields["actions"].replace("\n", "\r\n")}
    response = browser_a.post(edit, sent)

    assert response.status_code == 409
    assert "没有保存：这条在你打开这个页面之后被改过" in response.text and "data-initially-dirty" in response.text
    # Only the field that really differs from what is saved is marked.
    assert f'value="我刚才写的 {CANARY}"' in response.text
    assert response.text.count('<span class="badge diff">现在已保存的</span>') == 1
    assert one(app, import_id, "work").edits == {"role": "另一个标签页写的"}

    # Saving again from that page, having seen the difference, goes through.
    again = form_for(response.text, edit)
    assert browser_a.post(edit, again.fields).status_code == 303
    assert one(app, import_id, "work").edits == {"role": f"我刚才写的 {CANARY}"}


def test_a_save_the_database_refuses_keeps_what_was_typed_and_can_be_tried_again(app, browser_a, caplog):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    typed = {"role": f"重试的角色 {CANARY}", "actions": f"第一行改写 {CANARY}\n第二行改写"}

    with refused_updates(app, "import_candidates"), caplog.at_level(logging.ERROR, logger="roleradar.library"):
        failed = browser_a.submit(edit, edit, **typed)

    # Nothing was written: the old words and revision stand.
    assert one(app, import_id, "work").edits is None and one(app, import_id, "work").revision == 1
    assert failed.status_code == 500 and "没有保存：写入数据库时出错" in failed.text
    # What was typed is back, marked unsaved, with the leave guard and a form to save it again.
    assert f'value="重试的角色 {CANARY}"' in failed.text and f">第一行改写 {CANARY}\n第二行改写</textarea>" in failed.text
    assert "data-leave-guard data-initially-dirty" in failed.text and 'id="leave-dialog"' in failed.text
    again = form_for(failed.text, edit)
    assert again.fields["expected_revision"] == "1" and again.fields["actions"] == typed["actions"]
    # The log names the error, never the words.
    assert "OperationalError" in caplog.text and CANARY not in caplog.text

    # Once the database works again, saving the same page goes through.
    response = browser_a.post(edit, again.fields)

    assert response.status_code == 303 and "notice=candidate_saved" in response.headers["location"]
    assert one(app, import_id, "work").edits == typed and one(app, import_id, "work").revision == 2


@pytest.mark.parametrize("meanwhile", ["another tab", "left out", "expired", "another user"])
def test_trying_again_after_a_failed_save_meets_every_check(app, browser_a, browser_b, clock, meanwhile):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"

    with refused_updates(app, "import_candidates"):
        failed = browser_a.submit(edit, edit, role=f"失败后重试 {CANARY}")

    again = form_for(failed.text, edit)

    if meanwhile == "another tab":
        browser_a.submit(edit, edit, role="另一个标签页写的")
        response = browser_a.post(edit, again.fields)
        # Refused again, with what was typed kept next to what is saved now.
        assert response.status_code == 409 and f'value="失败后重试 {CANARY}"' in response.text
        assert '<span class="badge diff">现在已保存的</span>' in response.text
        assert one(app, import_id, "work").edits == {"role": "另一个标签页写的"}
    elif meanwhile == "left out":
        browser_a.submit(f"/imports/{import_id}/review", f"/imports/{import_id}/candidates/{work.id}/exclude")
        assert browser_a.post(edit, again.fields).status_code == 409
        # Saving once more, now against the current revision, still finds it left out.
        current = {**again.fields, "expected_revision": str(one(app, import_id, "work").revision)}
        assert "这条已标为不加入" in browser_a.post(edit, current).text
        assert one(app, import_id, "work").edits is None and one(app, import_id, "work").state == CandidateState.EXCLUDED
    elif meanwhile == "expired":
        clock.advance(days=IMPORT_WINDOW.days)
        browser_a.sign_in("合成用户 A")
        assert browser_a.post(edit, {**again.fields, "csrf_token": browser_a.csrf_token()}).status_code == 404
        assert one(app, import_id, "work").edits is None
    else:
        assert browser_b.post(edit, {**again.fields, "csrf_token": browser_b.csrf_token()}).status_code == 404
        assert one(app, import_id, "work").edits is None and one(app, import_id, "work").revision == 1


def test_edits_are_checked_like_the_library_checks_its_fields(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work, profile = one(app, import_id, "work"), one(app, import_id, "profile")
    work_edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    profile_edit = f"/imports/{import_id}/candidates/{profile.id}/edit"

    too_long = browser_a.submit(work_edit, work_edit, role="合" * 201)
    bad_email = browser_a.submit(profile_edit, profile_edit, email="不是邮箱")
    empty = browser_a.submit(
        work_edit, work_edit, organization="", role="", period="", actions="", outcome="", reflection=""
    )

    assert (too_long.status_code, bad_email.status_code, empty.status_code) == (422, 422, 422)
    assert "最多 200 个字，现在是 201 个字" in too_long.text and "邮箱格式看起来不对" in bad_email.text
    assert "请至少保留一栏内容" in empty.text
    assert one(app, import_id, "work").edits is None and one(app, import_id, "profile").edits is None


def test_leaving_out_and_taking_back_name_the_revision(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    award = one(app, import_id, "award")
    review = f"/imports/{import_id}/review"
    exclude = f"/imports/{import_id}/candidates/{award.id}/exclude"
    restore = f"/imports/{import_id}/candidates/{award.id}/restore"
    stale = browser_a.form(review, exclude)

    assert browser_a.post(exclude, stale.fields).headers["location"].endswith("notice=candidate_excluded#c-" + award.id)
    # The same click again (an old page): already so, nothing changes.
    assert "notice=candidate_already" in browser_a.post(exclude, stale.fields).headers["location"]
    page = browser_a.get(review).text
    assert "已标为不加入" in page and "不加入 1 条" in page
    assert browser_a.get(f"/imports/{import_id}/candidates/{award.id}/edit").status_code == 409

    assert "notice=candidate_restored" in browser_a.submit(review, restore).headers["location"]
    # Leaving it out from the page shown before it was taken back is refused.
    refused = browser_a.post(exclude, stale.fields)
    assert refused.status_code == 409 and "为了不覆盖，这次没有更改" in refused.text
    assert one(app, import_id, "award").state == CandidateState.PENDING and one(app, import_id, "award").revision == 3


def test_another_user_cannot_see_or_change_the_candidates(app, browser_a, browser_b):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    before = [(row.id, row.revision, row.state, row.edits) for row in rows(app, import_id)]
    token = browser_b.csrf_token()
    base = f"/imports/{import_id}"

    for response in (
        browser_b.get(f"{base}/review"),
        browser_b.get(f"{base}/candidates.json"),
        browser_b.get(f"{base}/candidates/{work.id}/edit"),
        browser_b.post(f"{base}/candidates/{work.id}/edit", {"csrf_token": token, "expected_revision": "1", "role": "B"}),
        browser_b.post(f"{base}/candidates/{work.id}/exclude", {"csrf_token": token, "expected_revision": "1"}),
        browser_b.post(f"{base}/candidates/{work.id}/restore", {"csrf_token": token, "expected_revision": "1"}),
        browser_b.post(f"{base}/structure", {"csrf_token": token}),
    ):
        assert response.status_code == 404 and "找不到这次导入" in response.text

    # The owner's own candidate, named under another import of theirs, is not found either.
    other = paste(browser_a, "教育经历\n另一所合成学校")
    assert browser_a.get(f"/imports/{other}/candidates/{work.id}/edit").status_code == 404
    assert browser_a.post(
        f"/imports/{other}/candidates/{work.id}/exclude", {"csrf_token": browser_a.csrf_token(), "expected_revision": "1"}
    ).status_code == 404
    assert [(row.id, row.revision, row.state, row.edits) for row in rows(app, import_id)] == before


def test_changes_need_a_session_and_its_token(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    exclude = f"/imports/{import_id}/candidates/{work.id}/exclude"

    assert browser_a.post(exclude, {"expected_revision": "1"}).status_code == 403
    assert browser_a.post(exclude, {"csrf_token": "forged", "expected_revision": "1"}).status_code == 403
    assert Browser(app).get(f"/imports/{import_id}/review").headers["location"] == "/login"
    assert Browser(app).post(exclude, {"expected_revision": "1"}).status_code == 401
    assert one(app, import_id, "work").state == CandidateState.PENDING


# --- comparing with what the user has --------------------------------------------------------------


def test_same_names_are_only_clues_and_every_match_is_listed(app, browser_a):
    bachelor = browser_a.create(category="education", school="合成大学", major="数学", stage="本科")
    master = browser_a.create(category="education", school="北京合成大学", stage="硕士")
    browser_a.create(category="education", school="合成")
    browser_a.create(category="work", organization="另一家合成公司")
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    page = browser_a.get(f"/imports/{import_id}/review").text
    education, work = group(page, "education"), group(page, "work")

    assert "可能冲突" in education and "只按名称比对：可能是同一段，也可能是另一段" in education
    assert f'href="/experiences/{bachelor}"' in education and f'href="/experiences/{master}"' in education
    assert education.count("<a href=\"/experiences/") == 2
    assert "专业：不同，已有的是“<span class=\"text\">数学</span>”" in education
    assert "学习阶段：相同" in education and "学习阶段：已有记录这一栏是空的" not in education
    assert ">新增</span>" in work and "/experiences/" not in work


def test_the_profile_is_compared_field_by_field(app, browser_a):
    fill_library(browser_a)
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    page = browser_a.get(f"/imports/{import_id}/review").text
    profile = group(page, "profile")

    assert "与现有基本资料有不同" in profile
    assert "手机：不同，已有的是“<span class=\"text\">139 1111 2222</span>”" in profile
    assert "当前所在城市：相同" in profile and "邮箱：已有记录这一栏是空的" in profile


@pytest.mark.parametrize(
    ("kind", "field", "existing", "incoming", "state"),
    [
        # A symbol, a sign or a negation changes what is said.
        ("project", "actions", "使用 C++ 编写工具", "使用 C 编写工具", DIFFERS),
        ("project", "actions", "结果为 -1", "结果为 1", DIFFERS),
        ("project", "actions", "没有使用 Docker", "使用 Docker", DIFFERS),
        ("project", "actions", "运行 0.5 秒", "运行 5 秒", DIFFERS),
        # What is not plainly alike differs: another minus sign, another letter case.
        ("project", "role", "结果为 −1", "结果为 -1", DIFFERS),
        ("project", "role", "Python", "python", DIFFERS),
        # Superscripts, subscripts, circled numbers and fractions stay as written: 10⁵ is a hundred thousand.
        ("project", "actions", "处理 10⁵ 条记录", "处理 105 条记录", DIFFERS),
        ("project", "role", "减少 CO₂ 排放", "减少 CO2 排放", DIFFERS),
        ("project", "role", "完成 ① 项", "完成 1 项", DIFFERS),
        ("project", "role", "用时 ½ 天", "用时 1/2 天", DIFFERS),
        # Width, spacing and line breaks alone are no difference.
        ("project", "actions", "使用 Python 编写接口", "使用Python编写接口", SAME),
        ("project", "actions", "负责数据清洗\n\n负责看板 ", "负责数据清洗\n负责看板", SAME),
        ("project", "role", "Ｃ＋＋ 开发", "C++ 开发", SAME),
        ("project", "role", "ＡＢＣ　测试", "ABC 测试", SAME),
        # Contained only line by line, and only in a field of several lines.
        ("project", "actions", "负责数据清洗\n负责看板\n负责报表", "负责看板", CONTAINED),
        # A whole line is compared by the same rule: 10⁵ in a line of the record does not contain 105.
        ("project", "actions", "处理 10⁵ 条记录\n负责看板", "处理 105 条记录", DIFFERS),
        ("project", "role", "后端开发\n前端开发", "后端开发", DIFFERS),
        ("project", "role", "", "后端开发", FILLS),
    ],
)
def test_fields_are_compared_by_their_words_not_by_their_names(kind, field, existing, incoming, state):
    assert field_state(field_entry(kind, field), incoming, existing) == state


@pytest.mark.parametrize(
    ("existing", "incoming"),
    [
        ("使用 C++ 编写工具", "使用 C 编写工具"),
        ("结果为 -1", "结果为 1"),
        ("没有使用 Docker", "使用 Docker"),
        ("运行 0.5 秒", "运行 5 秒"),
        ("处理 10⁵ 条记录", "处理 105 条记录"),
    ],
)
def test_a_meaningful_difference_is_shown_with_both_texts(app, browser_a, existing, incoming):
    browser_a.create(title="合成对照项目", actions=existing)
    import_id = paste(browser_a, "项目经历\n合成对照项目\n" + incoming)
    ask(browser_a, import_id)
    project = group(browser_a.get(f"/imports/{import_id}/review").text, "project")

    assert one(app, import_id, "project").extracted["actions"] == piece(incoming)
    assert "可能冲突" in project and "标题：相同" in project
    assert f"做了什么：不同，已有的是“<span class=\"text\">{existing}</span>”" in project


@pytest.mark.parametrize(
    ("existing", "incoming", "says"),
    [
        ("负责数据清洗\n\n负责看板 ", "- 负责数据清洗\n- 负责看板", "做了什么：相同"),
        ("使用 Python 编写接口", "使用Python编写接口", "做了什么：相同"),
        ("使用 Ｃ＋＋ 编写工具", "使用 C++ 编写工具", "做了什么：相同"),
        ("负责数据清洗\n负责看板\n负责报表", "- 负责看板", "做了什么：这里的每一行都是已有内容中的一整行"),
    ],
)
def test_only_spacing_line_breaks_or_whole_lines_read_as_no_conflict(app, browser_a, existing, incoming, says):
    browser_a.create(title="合成对照项目", actions=existing)
    import_id = paste(browser_a, "项目经历\n合成对照项目\n" + incoming)
    ask(browser_a, import_id)
    project = group(browser_a.get(f"/imports/{import_id}/review").text, "project")

    assert says in project and "疑似重复" in project and "可能冲突" not in project


# --- how long they are kept, and taking them away ------------------------------------------------------


def test_nothing_moves_the_deadline_and_the_candidates_end_with_the_import(app, browser_a, clock):
    import_id = paste(browser_a)
    deadline = import_row(app, import_id).expires_at
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    clock.advance(days=6)
    browser_a.sign_in("合成用户 A")
    browser_a.submit(edit, edit, role=CANARY)
    browser_a.get(f"/imports/{import_id}/review")

    assert import_row(app, import_id).expires_at == deadline
    clock.advance(days=1)
    browser_a.sign_in("合成用户 A")
    # Gone for every page at once, before any clean-up ran.
    assert browser_a.get(f"/imports/{import_id}/review").status_code == 404
    assert browser_a.get(f"/imports/{import_id}/candidates.json").status_code == 404
    rt(app).run_cleanup()
    assert rows(app, import_id) == [] and structuring(app, import_id) is None


def test_discarding_removes_the_candidates_and_the_edits_from_the_file(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work = one(app, import_id, "work")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    browser_a.submit(edit, edit, role=CANARY)
    page = browser_a.get(f"/imports/{import_id}/discard").text

    assert "以及整理出的条目和你对它们的修改、选择" in page
    browser_a.submit(f"/imports/{import_id}/discard", f"/imports/{import_id}/discard")

    assert rows(app, import_id) == [] and structuring(app, import_id) is None
    assert CANARY.encode() not in rt(app).config.db_path.read_bytes()


def test_export_all_holds_the_candidates_their_sources_and_the_review(app, browser_a):
    import_id = paste(browser_a)
    ask(browser_a, import_id)
    work, award = one(app, import_id, "work"), one(app, import_id, "award")
    edit = f"/imports/{import_id}/candidates/{work.id}/edit"
    browser_a.submit(edit, edit, role="数据分析实习生（暑期）")
    browser_a.submit(f"/imports/{import_id}/review", f"/imports/{import_id}/candidates/{award.id}/exclude")
    archive = zipfile.ZipFile(io.BytesIO(browser_a.get("/export/all.zip").content))
    files = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    (entry,) = manifest["imports"]
    record = json.loads(files[entry["candidates_path"]])
    data = json.loads(files["data.json"])

    assert manifest["format_version"] == 7 and entry["candidates"] == 8
    assert data["imports"][0]["review"] == record
    assert "待核对的条目和核对进度（`candidates.json`）" in files["README.md"].decode("utf-8")
    assert record["structured_by"] == "本机模拟整理规则（不是 AI）" and "没有经过核实" in record["note"]
    by_id = {item["id"]: item for item in record["candidates"]}
    assert by_id[award.id]["state"] == "excluded" and by_id[work.id]["fields"]["role"]["edited"] == "数据分析实习生（暑期）"
    # Skills may become the user's own words in the profile (U2-B2b); only the job intention is reference alone.
    assert {item["kind"] for item in record["candidates"] if item["reference_only"]} == {"intention"}


def test_the_candidates_download_alone_as_a_file(app, browser_a):
    import_id = paste(browser_a)

    assert browser_a.get(f"/imports/{import_id}/candidates.json").status_code == 409
    ask(browser_a, import_id)
    response = browser_a.get(f"/imports/{import_id}/candidates.json")

    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert len(json.loads(response.content)["candidates"]) == 8


def test_resume_text_is_shown_as_text_never_as_markup(app, browser_a):
    import_id = paste(browser_a, RESUME.replace("- 使用 Python 和 Flask 编写接口", f"- {HOSTILE}"))
    ask(browser_a, import_id)
    project = one(app, import_id, "project")
    pages = [
        browser_a.get(f"/imports/{import_id}/review").text,
        browser_a.get(f"/imports/{import_id}/candidates/{project.id}/edit").text,
    ]

    for page in pages:
        assert HOSTILE not in page and "&lt;script&gt;alert(" in page


# --- an outside service's answer (U2-C3) -------------------------------------------------------------


def outside(kind: str, **fields) -> dict:
    return {"kind": kind, "fields": {name: [{"segment": 1, "quote": quote}] for name, quote in fields.items()}}


def test_an_outside_answer_is_kept_only_where_it_stands_in_what_was_sent_and_in_the_import():
    own = [{"kind": "text", "number": 1, "text": "姓名：林合成\n邮箱：lin@example.com\n合成数据科技有限公司 | 数据分析实习生\nwe@example.com 负责数据清洗"}]
    sent = outbound(own).segments
    answer = json.dumps(
        {
            "candidates": [
                outside("work", organization="合成数据科技有限公司", role="数据分析实习生"),
                # A placeholder stands in what was sent, never in the import.
                outside("other", title="邮箱：［已隐去邮箱］"),
                # In the import, but its line was never sent.
                outside("other", title="姓名：林合成"),
                # Whole in what was sent; in the import, a team word around it was masked away.
                outside("work", actions="负责数据清洗"),
                outside("profile", email="lin@example.com"),
            ]
        },
        ensure_ascii=False,
    )

    checked = check_sent_answer(answer, sent, own)

    assert [found.extracted for found in checked.candidates] == [
        {
            "organization": [{"segment": 1, "quote": "合成数据科技有限公司"}],
            "role": [{"segment": 1, "quote": "数据分析实习生"}],
        }
    ]
    assert [(problem["position"], problem["reason"]) for problem in checked.problems] == [
        (2, MASKED),
        (3, NOT_IN_SOURCE),
        (4, CUT_CONTEXT),
        (5, LOCAL_ONLY),
    ]
    # The same answer, checked as the local rules' would be, keeps more: the outside one is checked twice.
    assert len(check_answer(answer, own).candidates) == 3
