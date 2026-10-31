"""UI batch 1: a calm grey workspace whose reading page is a project record.
It keeps every field, every safety flow and readable contrast; its few
Lucide icons stay decorative, local and licensed; and its motion never
keeps content hidden."""
import hashlib
import re
import sqlite3
from html.parser import HTMLParser

import jinja2
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app.library import web
from app.library.content import FIELD_NAMES, NOTE_FIELD, PROJECT_FIELDS
from app.library.models import ExperienceItem
from app.library.presentation import FIELD_GROUPS, FOLDED_GROUPS, READING_ORDER
from app.library.web import PACKAGE_DIR
from library_helpers import Browser, item_id_from

GROUP_LABELS = ("基本信息", "过程与收获", "相关资料")
OPEN_GROUP_LABELS = ("基本信息", "过程与收获")
LIST_UNAVAILABLE = "经历列表暂时无法显示"
LUCIDE_DIR = PACKAGE_DIR / "third_party" / "lucide"
ICON_NAMES = ("arrow-left", "chevron-right", "lock-keyhole", "plus", "radar", "rotate-ccw-clock")
DRAWING = re.compile(r"<(?:path|circle|rect|line|polyline|polygon|ellipse)\b[^>]*/>")
CSS = (PACKAGE_DIR / "static" / "library.css").read_text(encoding="utf-8")
MOTION = (PACKAGE_DIR / "static" / "motion.js").read_text(encoding="utf-8")
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__()
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchors.append(dict(attrs))


def anchors_in(html: str) -> list[dict]:
    reader = _Anchors()
    reader.feed(html)

    return reader.anchors


def library_aside(html: str) -> str:
    start = html.index('<aside class="library"')

    return html[start: html.index("</aside>", start)]


def record_list(html: str) -> str:
    """Where a page lists the records: beside a record, or the overview itself."""
    return library_aside(html) if '<aside class="library"' in html else main_of(html)


def main_of(html: str) -> str:
    start = html.index('<main id="main"')

    return html[start: html.index("</main>", start)]


def folds_in(html: str) -> dict[str, bool]:
    """Each folding section's title, and whether it starts open."""
    found = re.findall(r'<details class="fold[^"]*"([^>]*)>\s*<summary>.*?<span class="fold-title">([^<]+)</span>', html, re.S)

    return {title: " open" in attributes for attributes, title in found}


def css_rules(css: str) -> list[tuple[list[str], str]]:
    """The selectors and declarations of every rule, those inside media queries included."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    return [
        ([selector.strip() for selector in selectors.split(",")], body)
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


class _Reveal(HTMLParser):
    """Urgent elements that are, or sit inside, a block waiting to be revealed."""

    URGENT_CLASSES = {"notice", "error", "warning", "form-actions"}

    def __init__(self):
        super().__init__()
        self.inside = []
        self.waiting = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        inside = (self.inside[-1] if self.inside else False) or "data-reveal" in attrs
        urgent = tag == "dialog" or attrs.get("role") in ("alert", "status")

        if inside and (urgent or set((attrs.get("class") or "").split()) & self.URGENT_CLASSES):
            self.waiting.append((tag, attrs))

        if tag not in VOID_ELEMENTS:
            self.inside.append(inside)

    def handle_endtag(self, tag):
        if tag not in VOID_ELEMENTS and self.inside:
            self.inside.pop()


def revision_of(app, item_id) -> int:
    with app.state.library.database.transaction() as db:
        return db.get(ExperienceItem, item_id).revision


def _fail_on_write(session, flush_context, instances):
    raise OperationalError("COMMIT", None, sqlite3.OperationalError("disk I/O error"))


@pytest.fixture
def icon(app):
    return app.state.library.templates.env.get_template("_icons.html").module.icon


# --- the field contract survives the layout ---------------------------------


def test_every_field_is_in_exactly_one_page_group():
    grouped = [field.name for _, fields in FIELD_GROUPS for field in fields]

    assert tuple(grouped) == FIELD_NAMES
    assert NOTE_FIELD.name not in grouped
    assert tuple(label for label, _ in FIELD_GROUPS) == GROUP_LABELS


def test_the_form_has_all_nine_fields_with_labels_hints_and_limits(browser_a, icon):
    item_id = browser_a.create(actions="一句话")

    for path in ("/experiences/new", f"/experiences/{item_id}/edit"):
        page = browser_a.get(path).text

        for label in OPEN_GROUP_LABELS:
            assert f"<legend>{label}</legend>" in page, (path, label)

        assert set(folds_in(page)) == {*FOLDED_GROUPS, NOTE_FIELD.label}, path
        assert f'<summary>{icon("lock-keyhole")}<span class="fold-title">{NOTE_FIELD.label}</span>' in page, path

        for field in (*PROJECT_FIELDS, NOTE_FIELD):
            assert f'name="{field.name}"' in page, (path, field.name)
            assert f'maxlength="{field.max_length}"' in page, (path, field.name)
            assert field.hint in page, (path, field.name)

        for field in PROJECT_FIELDS:
            assert f'<label for="f-{field.name}">{field.label}</label>' in page, (path, field.name)

        assert "分享导出文件之前请先检查" in page
        assert 'name="csrf_token"' in page
        assert '<input type="hidden" name="after_save" value="">' in page
        assert 'name="next"' in page

    assert 'name="expected_revision"' in browser_a.get(f"/experiences/{item_id}/edit").text


def test_folded_sections_open_for_what_they_hold_and_for_errors(browser_a):
    plain = browser_a.create(actions="没有链接也没有备注")
    full = browser_a.create(actions="有链接和备注", links="https://example.com/a", private_note="合成备注")

    assert folds_in(browser_a.get("/experiences/new").text) == {"相关资料": False, NOTE_FIELD.label: False}
    assert folds_in(browser_a.get(f"/experiences/{plain}/edit").text) == {"相关资料": False, NOTE_FIELD.label: False}
    # Links are part of the record; the private note stays folded until it is needed.
    assert folds_in(browser_a.get(f"/experiences/{full}/edit").text) == {"相关资料": True, NOTE_FIELD.label: False}

    note_only = browser_a.submit("/experiences/new", "/experiences", private_note="只有备注")
    too_long = browser_a.submit("/experiences/new", "/experiences", actions="有内容", links="x" * 2001)

    assert note_only.status_code == 422
    assert folds_in(note_only.text)[NOTE_FIELD.label] is True
    assert too_long.status_code == 422
    assert folds_in(too_long.text)["相关资料"] is True
    assert "最多 2000 个字" in too_long.text


# --- the reading page is a project record --------------------------------------


def test_reading_order_names_every_field_once():
    assert sorted(READING_ORDER) == sorted(FIELD_NAMES)


def test_the_reading_page_reads_as_a_project_record_with_the_outcome_first(browser_a):
    values = {field.name: f"{field.label}的合成内容" for field in PROJECT_FIELDS}
    values["links"] = "https://example.com/work"
    item_id = browser_a.create(**values, private_note="合成的私有备注")
    main = main_of(browser_a.get(f"/experiences/{item_id}").text)
    head = main[: main.index("</header>")]
    order = [
        main.index(marker)
        for marker in (
            "</header>",
            '<h2 class="section-label" id="read-outcome">结果与产出</h2>',
            '<h2 class="section-label" id="read-actions">我的贡献</h2>',
            '<span class="fold-title">收获与反思</span>',
            '<span class="fold-title">相关资料</span>',
            f'<span class="fold-title">{NOTE_FIELD.label}</span>',
        )
    ]

    assert order == sorted(order)
    assert main.count(values["title"]) == 1
    assert f"<h1>{values['title']}</h1>" in head

    for name in ("context", "role", "period"):
        assert values[name] in head, name

    assert "待核对" in head

    for name in ("outcome", "actions", "reflection"):
        assert f'<p class="prose">{values[name]}</p>' in main, name

    assert '<a href="https://example.com/work" rel="noopener noreferrer">https://example.com/work</a>' in main
    assert folds_in(main) == {"收获与反思": False, "相关资料": False, NOTE_FIELD.label: False}
    assert "合成的私有备注" in main
    assert "不会进入资料快照或简历" in main
    assert "尚未填写" not in main


def test_the_reading_page_keeps_the_users_own_words_and_lines(browser_a):
    actions = "- 第一点：画了流程图\n- 第二点：写了接口\n\n第二段，<b>不是加粗</b>，逗号，不拆开"
    item_id = browser_a.create(title="原文保留", actions=actions, outcome="第一行\n第二行")
    main = main_of(browser_a.get(f"/experiences/{item_id}").text)
    contribution = main[main.index('id="read-actions"'): main.index("</section>", main.index('id="read-actions"'))]

    assert (
        '<p class="prose">- 第一点：画了流程图\n- 第二点：写了接口\n\n第二段，&lt;b&gt;不是加粗&lt;/b&gt;，逗号，不拆开</p>'
        in contribution
    )
    assert '<p class="prose">第一行\n第二行</p>' in main
    assert "<b>" not in main
    assert "<li>" not in contribution


def test_empty_fields_are_named_once_and_nothing_is_made_up(browser_a):
    item_id = browser_a.create(actions="只写了做了什么")
    main = main_of(browser_a.get(f"/experiences/{item_id}").text)

    assert "尚未填写：标题、场景或组织、我的角色、时间、结果与产出、收获与反思、相关资料。" in main
    # One merged line: no empty block per field, and no sentence explaining the line again.
    assert main.count("未填写") == 1
    assert 'id="read-outcome"' not in main
    assert "<details" not in main
    assert "<dl" not in main


# --- the layout does the explaining -------------------------------------------


def test_each_action_and_label_is_offered_once_and_the_layout_carries_the_rest(browser_a, browser_b):
    item_id = browser_a.create(
        title="只说一次", actions="合成内容", reflection="合成反思", links="https://example.com", private_note="合成备注",
    )
    pages = {
        "listing": browser_a.get("/experiences").text,
        "detail": browser_a.get(f"/experiences/{item_id}").text,
        "edit": browser_a.get(f"/experiences/{item_id}/edit").text,
        "new": browser_a.get("/experiences/new").text,
        "empty": browser_b.get("/experiences").text,
    }

    for name, page in pages.items():
        # The list on the left and the top bar already say where you are and lead back.
        assert 'class="crumbs"' not in main_of(page), name
        # A folded row is its title and an arrow, with no hint such as "展开阅读" beside it.
        assert "fold-hint" not in page, name
        # Starting a record and exporting everything are each offered once.
        assert page.count('href="/experiences/new"') == 1, name
        assert page.count('href="/export/all.zip"') == 1, name

    detail = pages["detail"]

    # Reading offers editing once; the revision guards the next save without being shown.
    assert detail.count(f'href="/experiences/{item_id}/edit"') == 1
    assert "修订号" not in detail
    assert 'name="expected_revision"' in main_of(detail)
    # The overview is the list itself: one way to start a record, then each record
    # once, with nothing beside it. An empty library invites the first record once.
    assert '<aside class="library"' not in pages["listing"]
    assert [a["href"] for a in anchors_in(main_of(pages["listing"]))] == ["/experiences/new", f"/experiences/{item_id}"]
    assert [a["href"] for a in anchors_in(main_of(pages["empty"]))] == ["/experiences/new"]
    assert 'class="library-filter"' not in pages["empty"]


def test_what_must_be_said_is_still_said_where_it_matters(browser_a):
    draft = browser_a.create(title="待核对的经历", actions="合成内容", private_note="合成备注")
    confirmed = browser_a.create(title="确认过的经历", actions="合成内容")
    browser_a.submit(f"/experiences/{confirmed}", f"/experiences/{confirmed}/confirm")
    draft_page = main_of(browser_a.get(f"/experiences/{draft}").text)
    confirmed_page = main_of(browser_a.get(f"/experiences/{confirmed}").text)
    new_form = browser_a.get("/experiences/new").text
    confirmed_form = main_of(browser_a.get(f"/experiences/{confirmed}/edit").text)

    # Whole elements, so that hiding one of them is caught as well as deleting its words.
    # Saving is not confirming: said beside the button that confirms, and before saving.
    assert '<p class="state-explain">还没有确认属实，保存不等于确认。</p>' in draft_page
    assert "确认这些内容属实" in draft_page
    assert "待核对" in draft_page and "已确认" in confirmed_page and "确认于" in confirmed_page
    assert '<p class="lede">保存后是“待核对”的草稿，不等于确认属实。</p>' in main_of(new_form)
    assert '<p class="warning">这条经历目前是“已确认”。保存修改后会变成“已修改，待核对”，需要你再次确认。</p>' in confirmed_form
    # The private note's limits, the caveat about closing the page and the local-only banner.
    assert '<p class="private-notice">只给你自己看；不会进入资料快照或简历。导出时会包含，分享导出文件之前请检查。</p>' in draft_page
    assert f'<span class="hint" id="h-note">{NOTE_FIELD.hint}。分享导出文件之前请先检查。</span>' in new_form
    assert '<p class="muted small">直接关闭或刷新页面时，浏览器的提醒不一定出现，离开前请先保存草稿。</p>' in new_form
    assert '<div class="dev-banner">本地开发模式：只用合成资料。' in new_form


# --- the record list on the left --------------------------------------------


def test_workspace_pages_list_my_records_and_mark_the_open_one(browser_a):
    first = browser_a.create(title="第一条经历", actions="做了第一件事")
    second = browser_a.create(actions="没有标题的第二条")
    pages = {
        "/experiences": None,
        "/experiences/new": None,
        f"/experiences/{first}": first,
        f"/experiences/{first}/edit": first,
        "/experiences?tab=trash": None,
    }

    for path, selected in pages.items():
        aside = record_list(browser_a.get(path).text)
        current = [a for a in anchors_in(aside) if a.get("aria-current") == "page"]

        assert "第一条经历" in aside and "做了第一件事" in aside, path
        assert "（未写标题）没有标题的第二条" in aside, path
        assert [a["href"] for a in current] == ([f"/experiences/{selected}"] if selected else []), path

    hrefs = [a["href"] for a in anchors_in(library_aside(browser_a.get(f"/experiences/{second}").text))]

    assert "/experiences/new" in hrefs
    assert "/experiences" in hrefs


def test_the_navigation_marks_the_part_of_my_data_you_are_in(browser_a):
    item_id = browser_a.create(title="合成经历", actions="合成内容")
    profile = browser_a.submit("/basic-profile/edit", "/basic-profile", city="合成城")
    assert profile.status_code == 303
    [profile_id] = re.findall(r'href="/experiences/([0-9a-f-]{36})/history"', browser_a.get("/basic-profile").text)
    binned = browser_a.create(title="回收站里的", actions="x")
    assert browser_a.submit(f"/experiences/{binned}/delete", f"/experiences/{binned}/delete").status_code == 303

    def marks(path):
        page = browser_a.get(path).text

        def region(start, end):
            return page[page.index(start): page.index(end, page.index(start))]

        top = anchors_in(region('<nav class="nav"', "</nav>"))
        parts = anchors_in(region('<nav class="subnav"', "</nav>"))
        menu = anchors_in(region('<details class="manage"', "</details>"))

        assert [a.get("aria-current") for a in top] == ["true"], path
        return (
            [a["href"] for a in parts if a.get("aria-current") == "page"],
            [a["href"] for a in menu if a.get("aria-current") == "page"],
        )

    for path in ("/experiences", "/experiences/new", f"/experiences/{item_id}", f"/experiences/{item_id}/history"):
        assert marks(path) == (["/experiences"], []), path

    # The profile's own pages, also its history and deletion, belong to 基本资料.
    for path in ("/basic-profile", f"/experiences/{profile_id}/history", f"/experiences/{profile_id}/delete"):
        assert marks(path) == (["/basic-profile"], []), path

    assert marks("/job-preferences/edit") == (["/job-preferences"], [])
    # The recycle bin is under 资料管理, and so is purging what it holds.
    assert marks("/experiences?tab=trash") == ([], ["/experiences?tab=trash"])
    assert marks(f"/experiences/{binned}/purge") == ([], ["/experiences?tab=trash"])


def test_the_record_list_never_shows_another_users_records(browser_a, browser_b):
    item_id = browser_a.create(title="A 的合成标题-9f2c")

    for path in ("/experiences", "/experiences/new", "/experiences?tab=trash"):
        page = browser_b.get(path).text

        assert "A 的合成标题-9f2c" not in page
        assert ("先记下一段经历" if path == "/experiences" else "还没有记录") in record_list(page)

    assert browser_b.get(f"/experiences/{item_id}").status_code == 404


# --- leaving with unsaved changes ---------------------------------------------


def test_only_the_skip_link_is_exempt_from_the_leave_guard(browser_a):
    item_id = browser_a.create(title="正在编辑的经历")
    browser_a.create(title="另一条经历")
    page = browser_a.get(f"/experiences/{item_id}/edit").text
    anchors = anchors_in(page)
    exempt = [a for a in anchors if "data-skip-leave-guard" in a]
    guarded = [a["href"] for a in anchors if "data-skip-leave-guard" not in a and "download" not in a]

    assert exempt == [{"class": "skip-link", "href": "#main", "data-skip-leave-guard": None}]
    assert 'id="main"' in page
    assert "/experiences/new" in guarded
    assert f"/experiences/{item_id}" in guarded
    assert len([href for href in guarded if re.fullmatch(r"/experiences/[0-9a-f-]{36}", href)]) >= 2
    assert "/experiences?tab=trash" in guarded
    assert '<form method="post" action="/logout" class="inline" data-logout>' in page


@pytest.mark.parametrize("case", ["created", "saved", "unchanged"])
def test_saving_from_the_leave_dialog_names_what_was_saved(app, browser_a, case):
    target = browser_a.create(title="要前往的另一条经历", actions="保持原样")
    target_revision = revision_of(app, target)
    destination = f"/experiences/{target}"

    if case == "created":
        response = browser_a.submit("/experiences/new", "/experiences", actions="刚才新建的内容", next=destination)
        expected = ("created_before_leaving", "已保存你刚才新建的记录（待核对）。")
    else:
        source = browser_a.create(actions="刚才编辑的那条")
        changes = {"actions": "刚才编辑后的内容"} if case == "saved" else {}
        response = browser_a.submit(
            f"/experiences/{source}/edit",
            f"/experiences/{source}/draft",
            next=destination,
            **changes,
        )
        expected = (
            ("saved_before_leaving", "已保存你刚才编辑的内容（待核对）。")
            if case == "saved"
            else ("unchanged_before_leaving", "你刚才编辑的内容没有变化，这次没有产生新的保存。")
        )

    landing = browser_a.get(response.headers["location"])

    assert response.headers["location"] == f"{destination}?notice={expected[0]}"
    assert expected[1] in landing.text
    assert "要前往的另一条经历" in landing.text
    assert revision_of(app, target) == target_revision


@pytest.mark.parametrize("form", ["new", "edit"])
def test_every_page_the_form_links_to_says_what_was_saved(browser_a, form):
    source = browser_a.create(title="正在编辑的经历")
    browser_a.create(title="另一条经历")
    form_path, action, notice = (
        ("/experiences/new", "/experiences", "已保存你刚才新建的记录（待核对）。")
        if form == "new"
        else (f"/experiences/{source}/edit", f"/experiences/{source}/draft", "已保存你刚才编辑的内容（待核对）。")
    )
    links = anchors_in(browser_a.get(form_path).text)
    destinations = sorted({a["href"] for a in links if "data-skip-leave-guard" not in a and "download" not in a})

    assert "/experiences/new" in destinations and "/experiences?tab=trash" in destinations

    for destination in destinations:
        response = browser_a.submit(form_path, action, next=destination, actions=f"离开去 {destination} 之前写的内容")
        landing = browser_a.get(response.headers["location"])

        assert landing.status_code == 200, destination
        assert notice in landing.text, destination


# --- the list degrades on its own when it cannot be read ----------------------


@pytest.fixture
def unreadable_list(monkeypatch):
    def unavailable(db, owner_id):
        raise OperationalError("SELECT", None, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(web, "list_experiences", unavailable)


def test_a_rejected_save_keeps_its_error_and_input_when_the_list_is_unreadable(browser_a, unreadable_list):
    response = browser_a.submit("/experiences/new", "/experiences", title="  ", private_note="只有备注的输入")

    assert response.status_code == 422
    assert "请至少在经历栏目里写一项内容" in response.text
    assert "只有备注的输入" in response.text
    assert "data-initially-dirty" in response.text
    assert LIST_UNAVAILABLE in response.text


def test_a_failed_save_keeps_its_error_and_input_when_the_list_is_unreadable(app, browser_a, unreadable_list):
    sessions = app.state.library.database._sessions
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        response = browser_a.submit("/experiences/new", "/experiences", actions="没能保存的合成内容")
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    assert response.status_code == 500
    assert "保存失败" in response.text
    assert "已保存" not in response.text
    assert "没能保存的合成内容" in response.text
    assert LIST_UNAVAILABLE in response.text


def test_pages_still_show_their_record_when_the_list_is_unreadable(app, browser_a, monkeypatch):
    item_id = browser_a.create(title="列表读不到时仍要显示", actions="合成内容")

    def unavailable(db, owner_id):
        raise OperationalError("SELECT", None, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(web, "list_experiences", unavailable)

    for path in (f"/experiences/{item_id}", f"/experiences/{item_id}/edit", "/experiences/new"):
        response = browser_a.get(path)

        assert response.status_code == 200, path
        assert LIST_UNAVAILABLE in response.text, path

    assert "列表读不到时仍要显示" in browser_a.get(f"/experiences/{item_id}").text
    assert 'value="列表读不到时仍要显示"' in browser_a.get(f"/experiences/{item_id}/edit").text


# --- readable colours ----------------------------------------------------------


def _luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]

    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)

    return (lighter + 0.05) / (darker + 0.05)


def test_the_colour_tokens_keep_readable_contrast():
    token = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", CSS))

    assert (token["ink"], token["page"], token["paper"], token["outcome"], token["accent"]) == (
        "#303236", "#f3f4f4", "#fafafa", "#eceeef", "#a3623e"
    )
    assert token["field-line"] == "#85888d"

    for background in ("paper", "page", "outcome"):
        assert _contrast(token["ink"], token[background]) >= 4.5
        assert _contrast(token["ink-soft"], token[background]) >= 4.5

    for background in ("paper", "page", "white"):
        # Form-control borders and the dark focus ring must stand out from what surrounds them.
        assert _contrast(token["field-line"], token[background]) >= 3
        assert _contrast(token["ink"], token[background]) >= 3

    assert _contrast(token["ink"], token["tag"]) >= 4.5
    assert _contrast(token["ink-soft"], token["hover"]) >= 4.5
    # Sidebar text over the darkest band of the backdrop.
    assert _contrast(token["ink-soft"], token["backdrop-band"]) >= 4.5
    assert _contrast(token["white"], token["accent"]) >= 4.5
    assert _contrast(token["white"], token["accent-strong"]) >= 4.5
    # The difference badge on the conflict page: white words on the dark ink.
    assert _contrast(token["white"], token["ink"]) >= 4.5


def test_the_accent_only_marks_the_main_action():
    allowed = re.compile(
        r'(\.button\.primary|button\.primary|button\[data-save-button\]|button\[data-leave-choice="save"\])(:hover|:active)?'
    )

    for selectors, body in css_rules(CSS):
        if "var(--accent" in body:
            assert all(allowed.fullmatch(selector) for selector in selectors), selectors


def test_no_second_colour_slips_in():
    token = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", CSS))
    # The accent is the only colour: errors and dangerous actions are grey as well.
    coloured = {token[name] for name in ("accent", "accent-strong")}

    for colour in set(re.findall(r"#[0-9a-fA-F]{6}\b", CSS)):
        channels = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]

        # Anything else is a grey: its channels stay within a few steps of each other.
        assert colour.lower() in coloured or max(channels) - min(channels) <= 12, colour

    for rgb in re.findall(r"rgba?\(([^)]*)\)", CSS):
        channels = [int(part) for part in rgb.split(",")[:3]]

        assert max(channels) - min(channels) <= 12, rgb


def test_errors_and_dangerous_actions_are_told_by_words_a_heavier_border_and_their_place(browser_a):
    rules = {", ".join(selectors): body for selectors, body in css_rules(CSS)}

    for selector in (".error", "button.danger", ".has-error input, .has-error textarea", ".compare-row.differs"):
        assert "border-color: var(--ink)" in rules[selector], selector
        assert "box-shadow: inset 0 0 0 1px var(--ink)" in rules[selector], selector

    assert "font-weight: 600" in rules[".field-error"]
    # Deleting and discarding stand apart from the everyday choices beside them.
    assert "margin-left: auto" in rules[".record-actions .danger-link"]
    assert "margin-left: auto" in rules[".dialog-actions .danger"]
    # Without a set width the dialog shrinks to its three buttons and leaves no room apart.
    assert "width: min(440px, calc(100vw - 32px));" in rules["dialog"]

    item_id = browser_a.create(title="要删除的经历", actions="合成内容")
    detail = main_of(browser_a.get(f"/experiences/{item_id}").text)
    confirm = browser_a.get(f"/experiences/{item_id}/delete").text

    # The link only leads to a page that says what deleting does before anything happens.
    assert detail.index("导出为 Markdown") < detail.index(f'<a class="danger-link" href="/experiences/{item_id}/delete">删除…</a>')
    assert '<button type="submit" class="danger">移到回收站</button>' in confirm
    assert "删除后立即停止使用" in confirm and "天内" in confirm


def test_a_field_error_is_announced_with_its_field(browser_a):
    too_long = browser_a.submit("/experiences/new", "/experiences", actions="有内容", links="x" * 2001)
    long_note = browser_a.submit("/experiences/new", "/experiences", actions="有内容", private_note="x" * 5001)

    assert too_long.status_code == 422 and long_note.status_code == 422
    assert 'aria-describedby="h-links e-links" aria-invalid="true"' in too_long.text
    assert '<span class="field-error" id="e-links">' in too_long.text
    assert too_long.text.count('aria-invalid="true"') == 1
    assert 'aria-describedby="h-note e-note" aria-invalid="true"' in long_note.text
    assert '<span class="field-error" id="e-note">' in long_note.text


def test_each_page_has_at_most_one_main_action(browser_a, browser_b):
    item_id = browser_a.create(title="一条经历")
    paths = ("/experiences", "/experiences/new", f"/experiences/{item_id}", f"/experiences/{item_id}/edit", "/experiences?tab=trash")
    pages = [browser_a.get(path).text for path in paths] + [browser_b.get("/experiences").text]

    for page in pages:
        main = main_of(page).split("<dialog")[0]

        assert len(re.findall(r'class="button primary"|data-save-button', main)) <= 1


# --- a few local Lucide icons ----------------------------------------------------


def test_each_icon_draws_the_official_lucide_shape(icon):
    assert tuple(sorted(path.stem for path in (LUCIDE_DIR / "icons").glob("*.svg"))) == ICON_NAMES

    for name in ICON_NAMES:
        official = (LUCIDE_DIR / "icons" / f"{name}.svg").read_text(encoding="utf-8")

        assert DRAWING.findall(str(icon(name))) == DRAWING.findall(official), name


def test_icons_are_decorative_and_load_nothing(icon):
    opening = (
        '<svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
    )

    for name in ICON_NAMES:
        drawn = str(icon(name))

        # Only the official drawing elements sit inside: no <use>, <image>, <script>, <style>, links or url().
        assert drawn == opening + "".join(DRAWING.findall(drawn)) + "</svg>", name


def test_icons_only_come_from_the_icon_macro():
    used = set()

    for path in (PACKAGE_DIR / "templates").glob("*.html"):
        source = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'\bi\.icon\("([^"]+)"\)', source))

        if path.name != "_icons.html":
            assert "<svg" not in source, path.name

    assert used == set(ICON_NAMES)


def test_an_unknown_icon_name_stops_the_page_instead_of_drawing_nothing(icon):
    with pytest.raises(jinja2.UndefinedError):
        icon("history")  # Lucide's former name for rotate-ccw-clock


def test_each_icon_sits_beside_the_words_of_its_control(browser_a, browser_b, icon):
    item_id = browser_a.create(title="带图标的经历", actions="合成内容", reflection="合成反思", private_note="合成备注")
    listing = browser_a.get("/experiences").text
    detail = browser_a.get(f"/experiences/{item_id}").text
    edit = browser_a.get(f"/experiences/{item_id}/edit").text
    history = anchors_in(browser_a.get(f"/experiences/{item_id}/history").text)
    version = browser_a.get(next(a["href"] for a in history if "/versions/" in a["href"])).text
    chevron = f'<span class="chevron">{icon("chevron-right")}</span>'
    lock = icon("lock-keyhole")

    assert f'<a class="brand" href="/experiences"><span class="brand-mark">{icon("radar")}</span>RoleRadar</a>' in listing
    assert f'<a class="button primary" href="/experiences/new">{icon("plus")}记录经历</a>' in main_of(listing)
    assert f'<summary>资料管理{chevron}</summary>' in listing
    assert f'<a class="button secondary" href="/experiences/new">{icon("plus")}新建记录</a>' in detail
    assert f'<a class="library-back" href="/experiences">{icon("arrow-left")}返回经历库（1 条）</a>' in detail
    assert f'<a class="button secondary" href="/experiences/{item_id}/history">{icon("rotate-ccw-clock")}查看历史</a>' in detail
    assert f'<summary><span class="fold-title">收获与反思</span>{chevron}</summary>' in detail
    assert f'<summary>{lock}<span class="fold-title">{NOTE_FIELD.label}</span>{chevron}</summary>' in detail
    assert f'<summary>{lock}<span class="fold-title">{NOTE_FIELD.label}</span>{chevron}</summary>' in edit
    assert f"<h2>{lock}当时的私有备注</h2>" in version
    assert f'<a class="button secondary" href="/experiences/{item_id}/history">{icon("arrow-left")}回到历史</a>' in version
    assert f'<a class="button primary" href="/experiences/new">{icon("plus")}记录第一段经历</a>' in browser_b.get("/experiences").text


def test_headings_are_plain_words_and_only_the_private_note_has_a_lock(browser_a, icon):
    item_id = browser_a.create(
        title="克制的图标", actions="合成内容", outcome="合成成果", reflection="合成反思",
        links="https://example.com", private_note="合成备注",
    )
    lock = str(icon("lock-keyhole"))

    for path in (f"/experiences/{item_id}", f"/experiences/{item_id}/edit", "/experiences/new"):
        main = main_of(browser_a.get(path).text)

        assert main.count(lock) == 1, path
        assert not re.search(r"<(h1|h2|legend)[^>]*>\s*<svg", main), path
        assert set(re.findall(r"<summary>(<svg.*?</svg>)", main, re.S)) == {lock}, path


def test_the_icon_files_are_the_recorded_lucide_release():
    readme = (LUCIDE_DIR / "README.md").read_text(encoding="utf-8")
    recorded = dict(re.findall(r"^\| `([^`]+)` \| `[^`]+` \| `([0-9a-f]{40})` \|", readme, re.M))

    assert sorted(recorded) == ["LICENSE", *(f"icons/{name}.svg" for name in ICON_NAMES)]
    assert "release **1.64.0**" in readme and "be09619c1c1124ad09c19fc3e87ee3b939409bbd" in readme

    for path, sha in recorded.items():
        raw = (LUCIDE_DIR / path).read_bytes()

        # Git's blob hash, comparable with the official repository at that tag.
        assert hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() == sha, path


def test_the_icons_keep_their_lucide_and_feather_licences():
    licence = (LUCIDE_DIR / "LICENSE").read_text(encoding="utf-8")
    feather = re.search(r"derived from the Feather project:\n\n(.+)\n", licence).group(1).split(", ")

    assert licence.startswith("ISC License\n\nCopyright (c) 2026 Lucide Icons and Contributors\n")
    assert "The MIT License (MIT) (for the icons listed above)\n\nCopyright (c) 2013-present Cole Bemis\n" in licence
    # The source note says the MIT notice also covers these three.
    assert sorted(set(feather) & set(ICON_NAMES)) == ["arrow-left", "chevron-right", "plus"]


# --- motion that never keeps content hidden --------------------------------------


def test_every_page_loads_motion_from_the_site_and_has_a_still_backdrop(app, browser_a):
    item_id = browser_a.create(title="动效页面", actions="合成内容")
    paths = (
        "/experiences", "/experiences/new", f"/experiences/{item_id}", f"/experiences/{item_id}/edit",
        f"/experiences/{item_id}/history", "/experiences?tab=trash",
    )
    pages = [Browser(app).get("/login").text] + [browser_a.get(path).text for path in paths]

    for page in pages:
        assert '<script src="/static/motion.js"></script>' in page[: page.index("</head>")]
        assert '<div class="backdrop" aria-hidden="true"><div class="backdrop-layer" data-parallax></div></div>' in page


def test_content_is_only_hidden_while_motion_is_allowed():
    for selectors, body in css_rules(CSS):
        if re.search(r"opacity:\s*0[;\s]", body + ";"):
            assert all(selector.startswith(".motion ") for selector in selectors), selectors

    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]

    # Reduced motion shows every block and stills the skeleton, also when the setting changes later.
    assert ".motion [data-reveal]:not(.is-revealed) { opacity: 1; transform: none; transition: none; }" in reduced
    assert ".sk { animation: none; }" in reduced
    assert 'if (!reduce.matches && "IntersectionObserver" in window)' in MOTION
    assert MOTION.count('root.classList.remove("motion")') >= 3


def test_urgent_content_never_waits_for_an_animation(browser_a):
    item_id = browser_a.create(title="紧急内容", actions="合成内容")
    browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
    rejected = browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", title="", actions="")
    paths = (
        f"/experiences/{item_id}?notice=confirmed", f"/experiences/{item_id}/edit",
        "/experiences/new?notice=saved_before_leaving", "/experiences?notice=deleted", "/experiences?tab=trash",
    )

    for page in [browser_a.get(path).text for path in paths] + [rejected.text]:
        reader = _Reveal()
        reader.feed(page)

        assert reader.waiting == []

    # A page shown again after a failed save has nothing in its main area waiting for an animation.
    assert rejected.status_code == 422
    assert "data-reveal" not in main_of(rejected.text)


def test_the_skeleton_only_covers_a_real_wait_for_the_next_page(browser_a):
    item_id = browser_a.create(title="骨架", actions="合成内容")

    for path in ("/experiences", f"/experiences/{item_id}", f"/experiences/{item_id}/edit"):
        main = main_of(browser_a.get(path).text)

        assert '<div class="pane-body" data-pane-body>' in main
        assert '<div class="pane-skeleton" data-pane-skeleton hidden aria-hidden="true">' in main
        assert '<p class="visually-hidden" data-pane-status role="status"></p>' in main

    # Only after every click handler ran without cancelling the navigation, never for
    # downloads, in-page links or form submissions, and given up after a while.
    for part in (
        "event.defaultPrevented", 'link.hasAttribute("download")', "SKELETON_DELAY_MS = 150",
        "SKELETON_GIVE_UP_MS = 10000", "url.hash", '"pageshow"',
    ):
        assert part in MOTION, part

    for forbidden in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "eval(", "fetch(", "XMLHttpRequest", '"submit"'):
        assert forbidden not in MOTION, forbidden


def test_giving_up_on_a_slow_page_cancels_it_before_the_page_is_given_back():
    # A navigation still on its way would otherwise replace the page later, and
    # whatever was typed into it meanwhile, without a leave dialog (UI-F1). The
    # browser runs of this are in the batch report; this keeps the order in place.
    stopping = MOTION[MOTION.index("function stopOpening()"): MOTION.index("function show(")]
    giving_up = MOTION[MOTION.index("giveUpTimer = window.setTimeout("): MOTION.index("function offerRetry(")]

    assert stopping.index("window.stop();") < stopping.index("restore();")
    assert giving_up.index("stopOpening();") < giving_up.index("offerRetry(url);")
    assert 'if (event.key === "Escape") {\n        stopOpening();' in MOTION
    assert "SKELETON_GIVE_UP_MS = 10000" in MOTION
    # Nothing is saved for the user on the way.
    for forbidden in (".submit(", "requestSubmit", "fetch(", "sendBeacon"):
        assert forbidden not in MOTION, forbidden


def test_the_backdrop_scrolls_at_half_speed_and_stays_out_of_the_way():
    backdrop = next(body for selectors, body in css_rules(CSS) if selectors == [".backdrop"])

    for part in ("position: fixed", "inset: 0", "z-index: -1", "overflow: hidden", "pointer-events: none"):
        assert part in backdrop, part

    assert "height: calc(100% + var(--backdrop-period));" in CSS
    assert "const PARALLAX_RATE = 0.5;" in MOTION
    assert "window.scrollY * PARALLAX_RATE" in MOTION
