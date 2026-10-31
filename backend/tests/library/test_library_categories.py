"""U1-B: six categories of experience, and finding them again.

The same saving, confirming, history, deletion and export rules hold in every
category (tested once per category, not as six copies of each flow). The
list can be filtered by category and searched by words, only within the
user's own current experiences.
"""
import io
import json
import zipfile

import pytest
from sqlalchemy import select

from app.library.content import (
    CATEGORY_FIELDS,
    CATEGORY_LABELS,
    EDUCATION,
    EXPERIENCE_CATEGORIES,
    NOTE_FIELD,
    PROJECT,
    WORK,
)
from app.library.models import ExperienceItem, ExperienceVersion
from app.library.presentation import CATEGORY_GROUPS, READINGS
from library_helpers import form_for, forms_in, item_id_from

SENTINEL = "类别哨兵-5e2a"


def create(browser, category: str, **content) -> str:
    response = browser.submit(f"/experiences/new?kind={category}", "/experiences", **content)
    assert response.status_code == 303, response.text

    return item_id_from(response.headers["location"])


def every_field(category: str) -> dict[str, str]:
    """A value for every field of a category, each one recognisable."""
    return {
        field.name: (
            f"https://example.com/{category}/{field.name}"
            if field.name == "links"
            else f"{field.label}·{category}·合成"
        )
        for field in CATEGORY_FIELDS[category]
    }


def stored(app, item_id) -> ExperienceItem:
    with app.state.library.database.transaction() as db:
        return db.get(ExperienceItem, item_id)


def record_list(html: str) -> str:
    """Where a page lists the records: beside a record, or the overview's main area."""
    start, end = ('<aside class="library"', "</aside>") if '<aside class="library"' in html else ('<main id="main"', "</main>")

    return html[html.index(start): html.index(end, html.index(start))]


def listed_titles(html: str) -> str:
    """The records a page lists, without the filter form above them."""
    records = record_list(html)

    return records[records.index("</form>") if "</form>" in records else 0:]


def unzip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --- every category keeps all of its fields -----------------------------------


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_each_category_shows_every_one_of_its_fields_once(category):
    names = [field.name for field in CATEGORY_FIELDS[category]]
    grouped = [field.name for _, fields in CATEGORY_GROUPS[category] for field in fields]

    assert grouped == names
    assert sorted(READINGS[category].order) == sorted(names)


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_one_field_is_enough_in_every_category(app, browser_a, category):
    last = CATEGORY_FIELDS[category][-1]
    value = "https://example.com/x" if last.name == "links" else f"只写了{last.label}"
    item_id = create(browser_a, category, **{last.name: value})
    item = stored(app, item_id)

    assert item.category == category
    assert item.content[last.name] == value
    assert item.state == "draft"
    assert browser_a.get(f"/experiences/{item_id}").status_code == 200


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_every_field_round_trips_through_form_page_and_export(app, browser_a, category):
    values = every_field(category)
    item_id = create(browser_a, category, **values, private_note=f"备注{SENTINEL}")
    edit = form_for(browser_a.get(f"/experiences/{item_id}/edit").text, f"/experiences/{item_id}/draft")
    page = browser_a.get(f"/experiences/{item_id}").text
    markdown = browser_a.get(f"/experiences/{item_id}/export.md").content.decode("utf-8")

    assert stored(app, item_id).content == values
    assert edit.fields["category"] == category

    for field in CATEGORY_FIELDS[category]:
        assert edit.fields[field.name] == values[field.name], field.name
        assert values[field.name] in page, field.name
        assert f"**{field.label}**\n\n{values[field.name]}" in markdown, field.name

    assert f"- 类别：{CATEGORY_LABELS[category]}" in markdown
    assert f"## 私有备注\n\n备注{SENTINEL}" in markdown
    assert "尚未填写" not in page


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_neither_a_category_alone_nor_a_private_note_is_an_experience(app, browser_a, category):
    note_only = browser_a.submit(
        f"/experiences/new?kind={category}", "/experiences", private_note=f"只有备注{SENTINEL}"
    )
    nothing = browser_a.submit(f"/experiences/new?kind={category}", "/experiences")

    assert note_only.status_code == 422
    assert "请至少在经历栏目里写一项内容" in note_only.text
    assert "私有备注不算经历内容" in note_only.text
    assert f"只有备注{SENTINEL}" in note_only.text
    assert nothing.status_code == 422

    with app.state.library.database.transaction() as db:
        assert db.scalars(select(ExperienceItem)).all() == []


@pytest.mark.parametrize("category", EXPERIENCE_CATEGORIES)
def test_confirm_history_delete_restore_and_export_hold_in_every_category(app, browser_a, category):
    item_id = create(browser_a, category, **{CATEGORY_FIELDS[category][0].name: f"{category}{SENTINEL}"})

    assert browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    assert stored(app, item_id).state == "confirmed"

    history = browser_a.get(f"/experiences/{item_id}/history").text

    assert "初始版" in history and "确认版 1" in history

    deleted = browser_a.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
    trash = browser_a.get("/experiences?tab=trash").text

    assert deleted.status_code == 303
    assert f'<span class="trash-category">{CATEGORY_LABELS[category]}</span>' in trash
    assert browser_a.get(f"/experiences/{item_id}").status_code == 404

    restored = browser_a.submit("/experiences?tab=trash", f"/experiences/{item_id}/restore")

    assert restored.headers["location"] == f"/experiences/{item_id}?notice=restored"
    assert stored(app, item_id).deleted_at is None

    data = json.loads(
        next(payload for name, payload in unzip(browser_a.get("/export/all.zip")).items() if name.endswith("/data.json"))
    )

    assert [record["category"] for record in data["experiences"]] == [category]


def test_project_records_stay_what_u1a_made_them(app, browser_a):
    item_id = browser_a.create(title="老的项目记录", actions="做了一个小程序")
    item = stored(app, item_id)

    assert item.category == PROJECT
    assert set(item.content) == {field.name for field in CATEGORY_FIELDS[PROJECT]}
    assert "我的贡献" in browser_a.get(f"/experiences/{item_id}").text


def test_the_stored_category_decides_which_fields_a_save_writes(app, browser_a):
    item_id = create(browser_a, EDUCATION, school="合成大学")
    form = form_for(browser_a.get(f"/experiences/{item_id}/edit").text, f"/experiences/{item_id}/draft")
    response = browser_a.post(
        f"/experiences/{item_id}/draft",
        {**form.fields, "category": PROJECT, "school": "改过的合成大学", "title": f"不该保存{SENTINEL}"},
    )
    item = stored(app, item_id)

    assert response.status_code == 303
    assert item.category == EDUCATION
    assert item.content["school"] == "改过的合成大学"
    assert "title" not in item.content


def test_an_unknown_category_is_refused_and_nothing_is_saved(app, browser_a):
    form = form_for(browser_a.get("/experiences/new").text, "/experiences")

    for category in ("profile", "preferences", "unknown", ""):
        response = browser_a.post("/experiences", {**form.fields, "category": category, "actions": "x"})
        assert response.status_code == 400, category

    assert browser_a.get("/experiences/new?kind=profile").status_code == 404
    assert browser_a.get("/experiences/new?kind=unknown").status_code == 404

    with app.state.library.database.transaction() as db:
        assert db.scalars(select(ExperienceItem)).all() == []


def test_education_awards_and_others_have_no_contribution_block(browser_a):
    project = browser_a.create(title="项目", actions="写了接口", outcome="上线了")
    education = create(browser_a, EDUCATION, school="合成大学", major="计算机", notes="辅修数学")
    work = create(browser_a, WORK, organization="合成公司", actions="整理数据", outcome="做完了")

    assert '<h2 class="section-label" id="read-actions">我的贡献</h2>' in browser_a.get(f"/experiences/{project}").text

    education_page = browser_a.get(f"/experiences/{education}").text

    assert "我的贡献" not in education_page
    assert "<dt>专业</dt><dd>计算机</dd>" in education_page
    assert '<h2 class="section-label" id="read-notes">补充说明</h2>' in education_page
    assert '<h2 class="section-label" id="read-actions">做过的事</h2>' in browser_a.get(f"/experiences/{work}").text


def test_the_new_form_offers_every_category_and_keeps_the_list_filter(browser_a):
    browser_a.create(actions="已有记录")
    page = browser_a.get("/experiences/new?kind=education&category=work&q=数据").text

    assert "<h1>记录一段教育经历</h1>" in page
    assert '<span aria-current="page">教育</span>' in page
    assert 'href="/experiences/new?kind=project&amp;category=work&amp;q=%E6%95%B0%E6%8D%AE"' in page

    for category in EXPERIENCE_CATEGORIES:
        assert CATEGORY_LABELS[category] in page

    assert '<input type="hidden" name="category" value="education">' in page


# --- finding records ------------------------------------------------------------


def make_library(browser) -> dict[str, str]:
    return {
        "python_project": browser.create(title="Python 爬虫项目", actions="用 Python 抓取公开数据"),
        "education": create(browser, EDUCATION, school="合成大学", major="数据科学"),
        "work": create(browser, WORK, organization="合成数据公司", actions="清洗数据，写周报"),
        "other": create(browser, "other", title="社区志愿活动"),
    }


def test_category_and_words_filter_the_list_alone_and_together(browser_a):
    ids = make_library(browser_a)

    only_education = listed_titles(browser_a.get("/experiences?category=education").text)
    data_words = listed_titles(browser_a.get("/experiences?q=数据").text)
    both = listed_titles(browser_a.get("/experiences?category=work&q=数据").text)

    assert "合成大学" in only_education and "Python 爬虫项目" not in only_education
    assert all(word in data_words for word in ("Python 爬虫项目", "合成大学", "合成数据公司"))
    assert "社区志愿活动" not in data_words
    assert "合成数据公司" in both and "合成大学" not in both and "Python 爬虫项目" not in both
    # Opening a record from a filtered list keeps the list filtered.
    assert f'href="/experiences/{ids["work"]}?category=work&amp;q=%E6%95%B0%E6%8D%AE"' in both


def test_search_ignores_case_and_full_width_and_needs_every_word(browser_a):
    make_library(browser_a)

    assert "Python 爬虫项目" in listed_titles(browser_a.get("/experiences?q=python").text)
    assert "Python 爬虫项目" in listed_titles(browser_a.get("/experiences?q=ＰＹＴＨＯＮ").text)
    assert "Python 爬虫项目" in listed_titles(browser_a.get("/experiences?q=python 公开").text)
    assert "Python 爬虫项目" not in listed_titles(browser_a.get("/experiences?q=python 志愿").text)


def test_search_never_reads_private_notes_history_the_bin_or_other_users(app, browser_a, browser_b):
    noted = browser_a.create(title="有备注的记录", actions="正文", private_note=f"备注里的{SENTINEL}")
    revised = browser_a.create(title="改过的记录", actions=f"旧说法{SENTINEL}-old")
    assert browser_a.submit(f"/experiences/{revised}", f"/experiences/{revised}/confirm").status_code == 303
    assert browser_a.submit(
        f"/experiences/{revised}/edit", f"/experiences/{revised}/draft", actions="新说法"
    ).status_code == 303
    binned = browser_a.create(title=f"回收站里的{SENTINEL}-bin", actions="x")
    assert browser_a.submit(f"/experiences/{binned}/delete", f"/experiences/{binned}/delete").status_code == 303
    browser_b.create(title=f"B 的{SENTINEL}-b", actions="y")

    with app.state.library.database.transaction() as db:
        old_versions = db.scalars(select(ExperienceVersion).where(ExperienceVersion.item_id == revised)).all()

    assert any(f"旧说法{SENTINEL}-old" in version.content["actions"] for version in old_versions)

    for words in (SENTINEL, f"{SENTINEL}-old", f"{SENTINEL}-bin", f"{SENTINEL}-b"):
        page = browser_a.get("/experiences", params={"q": words}).text
        assert "没有符合条件的记录" in page, words
        assert "有备注的记录" not in listed_titles(page), words


def test_no_records_and_no_matches_are_told_apart(browser_a, browser_b):
    browser_a.create(title="唯一的记录", actions="x")
    no_match = browser_a.get("/experiences?q=不存在的词").text
    empty = browser_b.get("/experiences?q=任何词").text

    assert "没有符合条件的记录" in no_match and "先记下一段经历" not in no_match
    assert '<a class="library-clear" href="/experiences">清空筛选</a>' in no_match
    assert "先记下一段经历" in record_list(empty) and "没有符合条件的记录" not in empty


def test_search_words_are_echoed_as_text_and_cut_to_a_hundred_characters(browser_a):
    browser_a.create(actions="x")
    hostile = browser_a.get("/experiences", params={"q": '<script>alert("q")</script>'}).text
    long_query = browser_a.get("/experiences", params={"q": "长" * 150}).text

    assert "<script>alert" not in hostile
    assert "&lt;script&gt;" in hostile
    assert f'value="{"长" * 100}"' in long_query
    assert "长" * 101 not in long_query


def test_an_unknown_filter_category_shows_everything(browser_a):
    make_library(browser_a)
    page = listed_titles(browser_a.get("/experiences?category=profile").text)

    assert "Python 爬虫项目" in page and "合成大学" in page


def test_a_new_record_from_a_filtered_list_starts_in_that_category(browser_a):
    browser_a.create(actions="x")
    page = browser_a.get("/experiences?category=education").text

    assert 'href="/experiences/new?kind=education&amp;category=education"' in record_list(page)


def test_the_filter_form_needs_no_session_token_and_changes_nothing(app, browser_a):
    browser_a.create(actions="x")
    page = browser_a.get("/experiences").text
    [search] = [form for form in forms_in(page) if form.method == "get"]

    # A plain search form: no session token, nothing it could change.
    assert search.action == "/experiences"
    assert set(search.fields) == {"q"}  # the form reader skips <select>; the category one is here:
    assert '<select name="category" aria-label="类别">' in page


# --- the profile and preferences are never experiences ---------------------------


def test_the_profile_and_preferences_never_appear_as_experiences(app, browser_a):
    assert browser_a.submit("/basic-profile/edit", "/basic-profile", name=f"名字{SENTINEL}").status_code == 303
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", cities_ideal=f"城市{SENTINEL}").status_code == 303
    browser_a.create(actions="一条经历")

    with app.state.library.database.transaction() as db:
        documents = [row.id for row in db.scalars(select(ExperienceItem).where(ExperienceItem.category.in_(("profile", "preferences"))))]

    token = browser_a.csrf_token()

    for item_id in documents:
        assert browser_a.get(f"/experiences/{item_id}").status_code == 404
        assert browser_a.get(f"/experiences/{item_id}/edit").status_code == 404

        for action in ("draft", "confirm"):
            response = browser_a.post(
                f"/experiences/{item_id}/{action}",
                {"csrf_token": token, "expected_revision": "1", "actions": "冒充经历"},
            )
            assert response.status_code == 404, action

    listing = browser_a.get("/experiences").text

    assert SENTINEL not in listing
    assert "没有符合条件的记录" in browser_a.get("/experiences", params={"q": SENTINEL}).text

    with app.state.library.database.transaction() as db:
        assert all(db.get(ExperienceItem, item_id).state == "draft" for item_id in documents)
        assert all(db.get(ExperienceItem, item_id).revision == 1 for item_id in documents)
