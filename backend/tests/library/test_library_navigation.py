"""UI-NAV1: 我的资料 with its three parts and 资料管理, and the overview of
the library that /experiences now is.

The overview shows the list that the address's filter finds, in the main
area; record pages keep their list on the left and lead back to it.
"""
import re
import sqlite3
from html.parser import HTMLParser

import pytest
from sqlalchemy.exc import OperationalError

from app.library import web
from app.library.content import CATEGORY_LABELS, EXPERIENCE_CATEGORIES
from library_helpers import PDF, Browser, form_for

SENTINEL = "总览外哨兵-nav1-3b7e"
FUTURE_MODULES = ("方向探索", "岗位机会", "我的简历", "投递跟踪")
# One record of each category, named by its title field.
TITLED = {
    "project": {"title": "合成项目", "actions": "项目里做了什么"},
    "education": {"school": "合成大学", "major": "合成专业"},
    "work": {"organization": "合成公司", "role": "合成岗位"},
    "activity": {"name": "合成活动", "actions": "活动里做了什么"},
    "award": {"name": "合成证书", "issuer": "合成机构"},
    "other": {"title": "合成其他", "notes": "暂时不知道怎么分类"},
}


class _Texts(HTMLParser):
    """The text of each element of one class, in order."""

    def __init__(self, cls: str):
        super().__init__(convert_charrefs=True)
        self.cls, self.texts, self._depth = cls, [], 0

    def handle_starttag(self, tag, attrs):
        if self._depth:
            self._depth += 1
        elif self.cls in (dict(attrs).get("class") or "").split():
            self._depth = 1
            self.texts.append("")

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.texts[-1] += data


def texts(html: str, cls: str) -> list[str]:
    reader = _Texts(cls)
    reader.feed(html)

    return [" ".join(text.split()) for text in reader.texts]


def region(html: str, start: str, end: str) -> str:
    return html[html.index(start): html.index(end, html.index(start))]


def main_of(html: str) -> str:
    return region(html, '<main id="main"', "</main>")


def create(browser, category: str, **content) -> str:
    response = browser.submit(f"/experiences/new?kind={category}", "/experiences", **content)
    assert response.status_code == 303, response.text

    return response.headers["location"].split("?")[0].rsplit("/", 1)[1]


def confirm(browser, item_id: str) -> None:
    assert browser.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303


# --- 我的资料 and 资料管理 ----------------------------------------------------------


def test_my_data_has_three_parts_and_keeps_the_bin_and_the_export_in_a_disclosure(app, browser_a):
    item_id = browser_a.create(title="导航检查", actions="合成内容")
    pages = (
        "/experiences",
        "/experiences/new",
        f"/experiences/{item_id}",
        f"/experiences/{item_id}/edit",
        f"/experiences/{item_id}/history",
        f"/experiences/{item_id}/delete",
        "/basic-profile",
        "/job-preferences/edit",
        "/experiences?tab=trash",
    )

    for path in pages:
        page = browser_a.get(path).text
        top = region(page, '<nav class="nav"', "</nav>")
        parts = region(page, '<nav class="subnav"', "</nav>")
        menu = region(page, '<details class="manage" data-menu>', "</details>")

        assert re.findall(r">([^<>]+)</a>", top) == ["我的资料"], path
        assert re.findall(r'href="([^"]+)"', parts) == ["/experiences", "/basic-profile", "/job-preferences"], path
        assert re.findall(r">([^<>]+)</a>", parts) == ["经历库", "基本资料", "求职偏好"], path
        # A native disclosure: findable and usable by keyboard and touch, also without JavaScript.
        assert "<summary" in menu and ">资料管理<" in menu, path
        assert '<a href="/experiences?tab=trash"' in menu, path
        # The export is still a real download, offered once on every page.
        assert '<a href="/export/all.zip" download>导出全部资料</a>' in menu, path
        assert page.count('href="/export/all.zip"') == 1, path
        # Who is signed in, and logging out, stay outside the menu.
        assert "data-logout" not in menu and "当前：" not in menu, path
        assert "本地开发模式：只用合成资料" in page, path
        # Later modules get no entry, not even a placeholder.
        assert not any(name in page for name in FUTURE_MODULES), path

    # Signed out, there is nothing of anyone's data to navigate.
    assert "subnav" not in Browser(app).get("/login").text


def test_the_bin_and_the_export_still_work_from_the_menu(browser_a):
    item_id = browser_a.create(title="要删除的经历", actions="合成内容")
    assert browser_a.upload(item_id, "附件.pdf", PDF).status_code == 303
    assert browser_a.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete").status_code == 303
    trash = browser_a.get("/experiences?tab=trash")
    export = browser_a.get("/export/all.zip")

    assert trash.status_code == 200
    assert "要删除的经历" in trash.text and "连同 1 个附件" in trash.text and "之前可以恢复" in trash.text
    assert export.status_code == 200 and export.headers["content-type"] == "application/zip"


# --- the overview --------------------------------------------------------------------


def test_the_overview_lists_every_category_with_its_real_state(browser_a):
    ids = {category: create(browser_a, category, **content) for category, content in TITLED.items()}
    confirm(browser_a, ids["education"])
    confirm(browser_a, ids["work"])
    # Confirmed, then edited again: waiting for review again.
    assert browser_a.submit(
        f"/experiences/{ids['work']}/edit", f"/experiences/{ids['work']}/draft", role="改过的合成岗位"
    ).status_code == 303
    page = browser_a.get("/experiences").text
    main = main_of(page)
    rows = dict(zip(re.findall(r'class="overview-item" href="/experiences/([0-9a-f-]{36})"', main), texts(main, "overview-item")))

    assert '<aside class="library"' not in page
    assert texts(main, "overview-count") == ["共 6 条"]
    assert set(rows) == set(ids.values())

    for category, item_id in ids.items():
        assert CATEGORY_LABELS[category] in rows[item_id], category

    assert "已确认" in rows[ids["education"]]
    assert "已修改，待核对" in rows[ids["work"]]
    assert "待核对" in rows[ids["project"]] and "已确认" not in rows[ids["project"]]
    # A record opens only when chosen: nothing of one is shown on the overview.
    assert 'class="record-head"' not in page


def test_the_overview_shows_the_filtered_list_and_tells_the_counts_apart(browser_a):
    data_project = create(browser_a, "project", title="数据清洗项目", actions="清洗数据")
    data_work = create(browser_a, "work", organization="合成数据公司", actions="整理数据")
    create(browser_a, "award", name="合成证书")
    filtered = browser_a.get("/experiences?category=project&q=数据").text
    words_only = browser_a.get("/experiences?q=数据").text
    nothing = browser_a.get("/experiences?q=不存在的词").text

    assert texts(main_of(filtered), "overview-count") == ["找到 1 条，共 3 条"]
    assert re.findall(r'class="overview-item" href="([^"]+)"', filtered) == [
        f"/experiences/{data_project}?category=project&amp;q=%E6%95%B0%E6%8D%AE"
    ]
    assert 'value="数据"' in filtered and '<option value="project" selected>' in filtered
    assert texts(main_of(words_only), "overview-count") == ["找到 2 条，共 3 条"]
    assert set(re.findall(r'class="overview-item" href="/experiences/([0-9a-f-]{36})', words_only)) == {data_project, data_work}
    # Records but no match: said as such, with a way back to everything; not an empty library.
    assert "没有符合条件的记录" in nothing and "overview-item" not in nothing
    assert '<a class="library-clear" href="/experiences">清空筛选</a>' in nothing
    assert "先记下一段经历" not in nothing


def test_names_in_the_overview_are_kept_safe_and_private_notes_stay_out(browser_a):
    untitled = browser_a.create(actions="没有标题时，用写下的第一句话认出它")
    long_title = "长" * 200
    browser_a.create(title=long_title, actions="合成内容")
    hostile = browser_a.create(title='<script>alert("t")</script>', actions="<img src=x onerror=alert(1)>")
    browser_a.create(title="有备注的经历", actions="合成内容", private_note=f"备注{SENTINEL}")
    page = browser_a.get("/experiences").text

    assert "（未写标题）没有标题时，用写下的第一句话认出它" in page
    # The whole title is there; only the screen shortens it.
    assert long_title in page
    assert "<script>alert" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(&#34;t&#34;)&lt;/script&gt;" in page and "&lt;img src=x onerror=alert(1)&gt;" in page
    assert f'href="/experiences/{untitled}"' in page and f'href="/experiences/{hostile}"' in page
    assert SENTINEL not in page
    assert "没有符合条件的记录" in browser_a.get("/experiences", params={"q": SENTINEL}).text


def test_each_user_sees_only_their_own_overview(browser_a, browser_b):
    browser_a.create(title="A 的合成经历", actions="x")
    browser_a.create(title="A 的另一条", actions="y")
    browser_b.create(title="B 的合成经历", actions="z")
    a_page, b_page = browser_a.get("/experiences").text, browser_b.get("/experiences").text

    assert texts(main_of(a_page), "overview-count") == ["共 2 条"]
    assert texts(main_of(b_page), "overview-count") == ["共 1 条"]
    assert "B 的合成经历" not in a_page and "A 的合成经历" not in b_page


def test_an_empty_library_invites_the_first_record_once(browser_a):
    page = browser_a.get("/experiences?category=work&q=任何词").text
    main = main_of(page)

    assert "先记下一段经历" in main
    assert re.findall(r'href="([^"]+)"', main) == ["/experiences/new"]
    assert "overview-count" not in main and "没有符合条件的记录" not in main


def test_an_unreadable_list_is_said_to_be_unreadable_not_empty(browser_a, monkeypatch):
    browser_a.create(title="读不到时也存在的经历", actions="x")

    def unavailable(db, owner_id, **filters):
        raise OperationalError("SELECT", None, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(web, "list_experiences", unavailable)
    response = browser_a.get("/experiences?category=project&q=数据")
    main = main_of(response.text)

    assert response.status_code == 200
    assert "经历列表暂时无法显示" in main
    # Reloading keeps the search; nothing claims the library is empty.
    assert 'href="/experiences?category=project&amp;q=%E6%95%B0%E6%8D%AE">重新加载</a>' in main
    assert "先记下一段经历" not in main and "共 0 条" not in main and "没有符合条件的记录" not in main
    assert 'value="数据"' in main


def test_a_record_opened_from_the_overview_leads_back_to_it_with_its_filter(browser_a):
    item_id = create(browser_a, "project", title="数据项目", actions="清洗数据")
    page = browser_a.get(f"/experiences/{item_id}?category=project&q=数据").text
    aside = region(page, '<aside class="library"', "</aside>")

    assert '<a class="library-back" href="/experiences?category=project&amp;q=%E6%95%B0%E6%8D%AE">' in aside
    # The record pages keep their own list on the left, filtered the same way.
    assert f'href="/experiences/{item_id}?category=project&amp;q=%E6%95%B0%E6%8D%AE" aria-current="page"' in aside


def test_new_records_of_every_category_can_be_started_from_a_filtered_overview(browser_a):
    browser_a.create(title="已有的记录", actions="x")

    for category in EXPERIENCE_CATEGORIES:
        page = browser_a.get(f"/experiences?category={category}").text
        [start] = re.findall(r'<a class="button primary" href="([^"]+)"', main_of(page))

        assert start == f"/experiences/new?kind={category}&amp;category={category}", category
        form = form_for(browser_a.get(start.replace("&amp;", "&")).text, "/experiences")

        assert form.fields["category"] == category, category
