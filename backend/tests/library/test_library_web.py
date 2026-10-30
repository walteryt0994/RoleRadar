"""The Chinese web pages: access control, safe rendering, conflicts and flows."""
import re
import sqlite3

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from app.library import lifecycle, web
from app.library.identity import token_hash
from app.library.lifecycle import version_state
from app.library.models import (
    Attachment,
    ExperienceItem,
    ExperienceVersion,
    LoginSession,
    PurgeJob,
    User,
)
from app.library.web import PACKAGE_DIR
from library_helpers import PNG, Browser, all_routes, form_for, forms_in, item_id_from

PUBLIC_ROUTES = {("GET", "/"), ("GET", "/health"), ("GET", "/login"), ("POST", "/login/dev")}
# Which prepared resource each path parameter is filled with.
GET_ROUTES = {
    "/experiences": {},
    "/experiences/new": {},
    "/experiences/{item_id}": {"item_id": "item"},
    "/experiences/{item_id}/edit": {"item_id": "item"},
    "/experiences/{item_id}/history": {"item_id": "item"},
    "/experiences/{item_id}/versions/{version_id}": {"item_id": "item", "version_id": "kept"},
    "/experiences/{item_id}/versions/{version_id}/delete": {"item_id": "item", "version_id": "kept"},
    "/experiences/{item_id}/versions/{version_id}/purge": {"item_id": "item", "version_id": "binned"},
    "/experiences/{item_id}/delete": {"item_id": "item"},
    "/experiences/{item_id}/purge": {"item_id": "trashed"},
    "/experiences/{item_id}/export.md": {"item_id": "item"},
    "/export/all.zip": {},
    "/basic-profile": {},
    "/basic-profile/edit": {},
    "/job-preferences": {},
    "/job-preferences/edit": {},
    "/experiences/{item_id}/attachments/{attachment_id}/download": {"item_id": "item", "attachment_id": "attachment"},
    "/experiences/{item_id}/attachments/{attachment_id}/delete": {"item_id": "item", "attachment_id": "attachment"},
    "/experiences/{item_id}/attachments/{attachment_id}/purge": {"item_id": "item", "attachment_id": "binned_attachment"},
}
POST_ROUTES = {
    "/logout": {},
    "/experiences": {},
    "/experiences/{item_id}/draft": {"item_id": "item"},
    "/experiences/{item_id}/confirm": {"item_id": "item"},
    "/experiences/{item_id}/versions/{version_id}/delete": {"item_id": "item", "version_id": "kept"},
    "/experiences/{item_id}/versions/{version_id}/restore": {"item_id": "item", "version_id": "binned"},
    "/experiences/{item_id}/versions/{version_id}/purge": {"item_id": "item", "version_id": "binned"},
    "/experiences/{item_id}/delete": {"item_id": "item"},
    "/experiences/{item_id}/restore": {"item_id": "trashed"},
    "/experiences/{item_id}/purge": {"item_id": "trashed"},
    "/basic-profile": {},
    "/basic-profile/{item_id}/save": {"item_id": "profile"},
    "/job-preferences": {},
    "/job-preferences/{item_id}/save": {"item_id": "preferences"},
    "/experiences/{item_id}/attachments/{attachment_id}/delete": {"item_id": "item", "attachment_id": "attachment"},
    "/experiences/{item_id}/attachments/{attachment_id}/restore": {"item_id": "item", "attachment_id": "binned_attachment"},
    "/experiences/{item_id}/attachments/{attachment_id}/purge": {"item_id": "item", "attachment_id": "binned_attachment"},
}
# The upload takes a multipart body; test_library_attachments.py checks its access rules.
UPLOAD_ROUTES = {"/experiences/{item_id}/attachments": {"item_id": "item"}}
# One field each kind of record accepts, so the same post is valid for every route.
SOME_CONTENT = {"actions": "", "name": "", "cities_ideal": ""}
SENTINEL = "哨兵-c41f"
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def fill(template: str, ids: dict, mapping: dict) -> str:
    return template.format(**{name: ids[resource] for name, resource in mapping.items()})


def database(app):
    return app.state.library.database


def prepare_resources(app, browser) -> dict:
    """An active draft with one kept and one binned version, one kept and one
    binned attachment, a binned experience, and the user's basic profile and
    job preferences."""
    item = browser.create(title="A 的经历", actions=f"A 的内容{SENTINEL}", private_note=f"A 的备注{SENTINEL}")
    assert browser.submit(f"/experiences/{item}", f"/experiences/{item}/confirm").status_code == 303
    edited = browser.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", actions=f"A 改过{SENTINEL}")
    assert edited.status_code == 303

    with database(app).transaction() as db:
        kinds = {
            version.kind: version.id
            for version in db.scalars(select(ExperienceVersion).where(ExperienceVersion.item_id == item))
        }

    binned_path = f"/experiences/{item}/versions/{kinds['confirmed']}/delete"
    assert browser.submit(binned_path, binned_path).status_code == 303
    assert browser.upload(item, f"A 的附件{SENTINEL}.pdf").status_code == 303
    assert browser.upload(item, f"A 删掉的附件{SENTINEL}.png", PNG).status_code == 303

    with database(app).transaction() as db:
        attachments = {row.filename[:4]: row.id for row in db.scalars(select(Attachment))}

    binned_attachment = f"/experiences/{item}/attachments/{attachments['A 删掉']}/delete"
    assert browser.submit(binned_attachment, binned_attachment).status_code == 303
    trashed = browser.create(actions=f"A 回收站里的内容{SENTINEL}")
    assert browser.submit(f"/experiences/{trashed}/delete", f"/experiences/{trashed}/delete").status_code == 303
    assert browser.submit("/basic-profile/edit", "/basic-profile", name=f"A 的名字{SENTINEL}").status_code == 303
    assert browser.submit("/job-preferences/edit", "/job-preferences", cities_ideal=f"A 的城市{SENTINEL}").status_code == 303

    with database(app).transaction() as db:
        documents = {
            row.category: row.id
            for row in db.scalars(select(ExperienceItem).where(ExperienceItem.category.in_(("profile", "preferences"))))
        }

    return {
        "item": item,
        "kept": kinds["initial"],
        "binned": kinds["confirmed"],
        "attachment": attachments["A 的附"],
        "binned_attachment": attachments["A 删掉"],
        "trashed": trashed,
        "profile": documents["profile"],
        "preferences": documents["preferences"],
    }


def snapshot(app):
    """Everything a request could change, to prove that nothing did."""
    with database(app).transaction() as db:
        items = [
            (row.id, row.owner_id, row.revision, row.state, row.deleted_at, row.content, row.private_note)
            for row in db.scalars(select(ExperienceItem).order_by(ExperienceItem.id))
        ]
        versions = [
            (row.id, row.deleted_at, row.purge_after)
            for row in db.scalars(select(ExperienceVersion).order_by(ExperienceVersion.id))
        ]
        jobs = [(row.id, row.status) for row in db.scalars(select(PurgeJob).order_by(PurgeJob.id))]
        attachments = [
            (row.id, row.state, row.revision, row.deleted_at, row.purge_after)
            for row in db.scalars(select(Attachment).order_by(Attachment.id))
        ]

    return items, versions, jobs, attachments


def state_of(app, version_id) -> str:
    with database(app).transaction() as db:
        return version_state(db, db.get(ExperienceVersion, version_id))


def revision_of(app, item_id) -> int:
    with database(app).transaction() as db:
        return db.get(ExperienceItem, item_id).revision


def attachment_revision(app, attachment_id) -> int:
    with database(app).transaction() as db:
        return db.get(Attachment, attachment_id).revision


def user_id(app, label) -> str:
    with database(app).transaction() as db:
        return db.scalar(select(User.id).where(User.display_label == label))


def _fail_on_write(session, flush_context, instances):
    raise OperationalError("COMMIT", None, sqlite3.OperationalError("disk I/O error"))


# --- access control -------------------------------------------------------


def test_every_route_is_either_public_or_listed_as_protected(app):
    found = set()

    for route in all_routes(app):
        methods = getattr(route, "methods", None)

        if methods is None:
            continue

        found |= {(method, route.path) for method in methods - {"HEAD"}}

    expected = (
        PUBLIC_ROUTES
        | {("GET", path) for path in GET_ROUTES}
        | {("POST", path) for path in POST_ROUTES}
        | {("POST", path) for path in UPLOAD_ROUTES}
    )

    assert found == expected


def test_static_files_hold_no_user_data_and_are_public(app):
    names = sorted(path.name for path in (PACKAGE_DIR / "static").iterdir())
    anonymous = Browser(app)

    assert names == ["leave_guard.js", "library.css", "motion.js", "upload.js"]

    for name in names:
        assert anonymous.get(f"/static/{name}").status_code == 200


def test_without_a_session_pages_redirect_to_login_and_posts_are_refused(app, browser_a):
    ids = prepare_resources(app, browser_a)
    before = snapshot(app)
    anonymous = Browser(app)

    for template, mapping in GET_ROUTES.items():
        response = anonymous.get(fill(template, ids, mapping))
        assert (response.status_code, response.headers.get("location")) == (303, "/login"), template

    for template, mapping in POST_ROUTES.items():
        data = {"csrf_token": "guess", "expected_revision": "1", "acknowledge": "yes", "actions": "匿名写入"}
        response = anonymous.post(fill(template, ids, mapping), data)
        assert response.status_code == 401, template
        assert SENTINEL not in response.text

    assert snapshot(app) == before


def test_posts_without_this_sessions_csrf_token_are_refused(app, browser_a, browser_b):
    ids = prepare_resources(app, browser_a)
    before = snapshot(app)
    other_sessions_token = browser_b.csrf_token()

    for template, mapping in POST_ROUTES.items():
        path = fill(template, ids, mapping)
        base = {"expected_revision": "1", "acknowledge": "yes", "actions": "伪造的写入"}

        for token in (None, "", "forged-token", other_sessions_token):
            data = dict(base) if token is None else {**base, "csrf_token": token}
            assert browser_a.post(path, data).status_code == 403, (template, token)

    assert snapshot(app) == before
    assert browser_a.get("/experiences").status_code == 200


def test_another_user_gets_404_for_every_one_of_my_resources(app, browser_a, browser_b):
    ids = prepare_resources(app, browser_a)
    before = snapshot(app)
    revisions = {name: revision_of(app, ids[name]) for name in ("item", "trashed", "profile", "preferences")}
    states = {name: state_of(app, ids[name]) for name in ("kept", "binned")}
    token = browser_b.csrf_token()

    for template, mapping in GET_ROUTES.items():
        if mapping:
            response = browser_b.get(fill(template, ids, mapping))
            assert response.status_code == 404, template
            assert SENTINEL not in response.text

    for template, mapping in POST_ROUTES.items():
        if mapping:
            data = {
                "csrf_token": token,
                "expected_revision": str(revisions[mapping["item_id"]]),
                "expected_state": states.get(mapping.get("version_id"), ""),
                "acknowledge": "yes",
                **{name: "B 写入的内容" for name in SOME_CONTENT},
            }
            response = browser_b.post(fill(template, ids, mapping), data)
            assert response.status_code == 404, template
            assert SENTINEL not in response.text

    assert snapshot(app) == before

    for path in ("/experiences", "/experiences?tab=trash", "/basic-profile", "/job-preferences"):
        assert SENTINEL not in browser_b.get(path).text


@pytest.mark.parametrize("template", [path for path, mapping in POST_ROUTES.items() if mapping])
def test_the_same_requests_work_for_the_owner(app, browser_a, template):
    """Control for the 404 test above: those requests were well formed."""
    ids = prepare_resources(app, browser_a)
    mapping = POST_ROUTES[template]
    revision = (
        attachment_revision(app, ids[mapping["attachment_id"]])
        if "attachment_id" in mapping
        else revision_of(app, ids[mapping["item_id"]])
    )
    data = {
        "csrf_token": browser_a.csrf_token(),
        "expected_revision": str(revision),
        "expected_state": state_of(app, ids[mapping["version_id"]]) if "version_id" in mapping else "",
        "acknowledge": "yes",
        **{name: "A 自己的修改" for name in SOME_CONTENT},
    }

    assert browser_a.post(fill(template, ids, mapping), data).status_code == 303


def test_owner_ids_sent_by_the_browser_are_ignored(app, browser_a, browser_b):
    other = user_id(app, "合成用户 B")
    form = browser_a.form("/experiences/new", "/experiences")
    response = browser_a.post(
        f"/experiences?owner_id={other}",
        {**form.fields, "actions": "A 写的内容", "owner_id": other, "user_id": other},
    )
    item_id = item_id_from(response.headers["location"])

    with database(app).transaction() as db:
        owner = db.get(ExperienceItem, item_id).owner_id

    assert owner == user_id(app, "合成用户 A")
    assert browser_b.get(f"/experiences/{item_id}").status_code == 404
    assert browser_a.get(f"/experiences/{item_id}").status_code == 200


# --- safe rendering -------------------------------------------------------


def test_user_text_is_displayed_as_text_not_markup(browser_a):
    item_id = browser_a.create(title=HOSTILE, actions=HOSTILE, private_note=HOSTILE)

    for path in (
        f"/experiences/{item_id}",
        "/experiences",
        f"/experiences/{item_id}/history",
        f"/experiences/{item_id}/edit",
    ):
        page = browser_a.get(path).text
        assert "<script>alert" not in page, path
        assert "<img src=x" not in page, path
        assert "&lt;script&gt;" in page, path


def test_only_plain_web_links_become_clickable(browser_a):
    links = "\n".join(
        [
            "https://example.com/a?x=1&y=2",
            "http://ok.example",
            "javascript:alert(1)",
            "JavaScript:alert(2)",
            "data:text/html,hi",
            "//evil.example/x",
            "ftp://files.example",
        ]
    )
    page = browser_a.get(f"/experiences/{browser_a.create(links=links)}").text
    hrefs = re.findall(r'href="([^"]*)"', page)

    assert "https://example.com/a?x=1&amp;y=2" in hrefs
    assert "http://ok.example" in hrefs
    assert not [href for href in hrefs if href.lower().startswith(("javascript:", "data:", "//", "ftp:"))]
    assert "javascript:alert(1)" in page
    assert "不是 http/https 链接" in page


def test_pages_only_run_scripts_from_the_site_itself(browser_a):
    item_id = browser_a.create(actions="普通内容")

    for path in ("/experiences", f"/experiences/{item_id}", f"/experiences/{item_id}/edit", "/experiences/new"):
        response = browser_a.get(path)
        csp = response.headers["content-security-policy"]

        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert all('src="/static/' in tag for tag in re.findall(r"<script[^>]*>", response.text))
        assert not re.search(r"\son[a-z]+\s*=", response.text)


# --- saving, failure and conflicts ---------------------------------------


def test_one_sentence_saves_as_a_draft_that_is_not_yet_confirmed(browser_a):
    response = browser_a.submit("/experiences/new", "/experiences", actions="做了一个小程序。")
    page = browser_a.get(response.headers["location"]).text

    assert response.headers["location"].endswith("?notice=created")
    assert "已保存（待核对）" in page
    assert "保存不等于确认" in page
    assert "确认这些内容属实" in page
    # Every empty field is named, once, in the reading page's merged line.
    assert "尚未填写：标题、场景或组织、我的角色、时间、结果与产出、收获与反思、相关资料。" in page


def test_an_empty_form_is_refused_with_a_chinese_hint(browser_a):
    response = browser_a.submit("/experiences/new", "/experiences", title="   ", private_note="只有备注")

    assert response.status_code == 422
    assert "请至少在经历栏目里写一项内容" in response.text
    assert "私有备注不算经历内容" in response.text
    assert "只有备注" in response.text
    assert "还没有记录" in browser_a.get("/experiences").text


def test_a_failed_save_never_shows_success_and_keeps_both_texts(app, browser_a):
    item_id = browser_a.create(actions="最后一次成功保存的内容")
    form = browser_a.form(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft")
    sessions = database(app)._sessions
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        response = browser_a.post(
            f"/experiences/{item_id}/draft",
            {**form.fields, "actions": "这次没能保存的新内容"},
        )
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    detail = browser_a.get(f"/experiences/{item_id}").text

    assert response.status_code == 500
    assert "保存失败" in response.text
    assert "已保存" not in response.text
    assert "这次没能保存的新内容" in response.text
    assert "data-initially-dirty" in response.text
    assert "最后一次成功保存的内容" in detail
    assert "这次没能保存的新内容" not in detail


def test_a_failed_first_save_keeps_the_input_and_creates_nothing(app, browser_a):
    sessions = database(app)._sessions
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        response = browser_a.submit("/experiences/new", "/experiences", actions="第一次保存就失败的内容")
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    assert response.status_code == 500
    assert "已保存" not in response.text
    assert "第一次保存就失败的内容" in response.text
    assert "还没有记录" in browser_a.get("/experiences").text


def test_two_tabs_cannot_silently_overwrite_each_other(browser_a):
    item_id = browser_a.create(actions="第一版")
    edit, action = f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft"
    tab_one = browser_a.form(edit, action)
    tab_two = browser_a.form(edit, action)

    assert browser_a.post(action, {**tab_one.fields, "actions": "标签页一的修改"}).status_code == 303

    clash = browser_a.post(action, {**tab_two.fields, "actions": "标签页二的修改"})

    assert clash.status_code == 409
    assert "保存没有生效" in clash.text
    assert "标签页一的修改" in clash.text
    assert "标签页二的修改" in clash.text
    assert "有差异" in clash.text
    assert "标签页一的修改" in browser_a.get(f"/experiences/{item_id}").text

    keep_mine = form_for(clash.text, action)

    assert browser_a.post(action, keep_mine.fields).status_code == 303
    assert "标签页二的修改" in browser_a.get(f"/experiences/{item_id}").text


def test_an_old_page_cannot_confirm_content_it_did_not_show(browser_a):
    item_id = browser_a.create(actions="原来的内容")
    stale = browser_a.form(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
    browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", actions="后来改过的内容")
    response = browser_a.post(f"/experiences/{item_id}/confirm", stale.fields)
    history = browser_a.get(f"/experiences/{item_id}/history").text

    assert response.status_code == 409
    assert "确认没有生效" in response.text
    assert "确认这些内容属实" in browser_a.get(f"/experiences/{item_id}").text
    assert len(set(re.findall(rf"/experiences/{item_id}/versions/[0-9a-f-]{{36}}\"", history))) == 1


def test_an_old_page_cannot_delete_after_newer_changes(browser_a):
    item_id = browser_a.create(actions="内容")
    stale = browser_a.form(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
    browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", actions="新内容")

    assert browser_a.post(f"/experiences/{item_id}/delete", stale.fields).status_code == 409
    assert browser_a.get(f"/experiences/{item_id}").status_code == 200


def test_editing_a_confirmed_experience_warns_and_returns_it_to_review(browser_a):
    item_id = browser_a.create(actions="确认过的内容")
    browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
    edit_page = browser_a.get(f"/experiences/{item_id}/edit").text
    browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", actions="改过的内容")
    detail = browser_a.get(f"/experiences/{item_id}").text

    assert "保存修改后会变成“已修改，待核对”" in edit_page
    assert "已修改，待核对" in detail
    assert "不再代表当前事实" in detail


# --- leaving with unsaved changes -----------------------------------------


def test_forms_carry_the_unsaved_changes_guard(browser_a):
    page = browser_a.get("/experiences/new").text

    assert "data-leave-guard" in page
    assert '<script src="/static/leave_guard.js">' in page

    for choice in ("保存草稿", "继续编辑", "放弃修改"):
        assert f">{choice}</button>" in page

    assert "不一定出现" in page


def test_the_guard_script_offers_three_choices_and_only_asks_the_browser(app):
    script = Browser(app).get("/static/leave_guard.js").text

    assert 'data-leave-choice' in script
    assert "requestSubmit" in script
    assert "beforeunload" in script
    assert "event.preventDefault()" in script


def test_saving_from_the_leave_dialog_continues_to_the_chosen_page(browser_a):
    item_id = browser_a.create(actions="内容")
    response = browser_a.submit(
        f"/experiences/{item_id}/edit",
        f"/experiences/{item_id}/draft",
        actions="离开前保存",
        next="/experiences?tab=trash",
    )

    assert response.headers["location"] == "/experiences?tab=trash&notice=saved_before_leaving"
    assert "已保存你刚才编辑的内容（待核对）。" in browser_a.get(response.headers["location"]).text


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", " /experiences"],
)
def test_the_continue_target_never_leaves_the_site(browser_a, target):
    response = browser_a.submit("/experiences/new", "/experiences", actions="内容", next=target)

    assert re.fullmatch(r"/experiences/[0-9a-f-]{36}\?notice=created", response.headers["location"])


# --- sessions -------------------------------------------------------------


def test_the_session_cookie_is_http_only_and_same_site(app):
    browser = Browser(app)
    form = [f for f in forms_in(browser.get("/login").text) if f.action == "/login/dev"][0]
    response = browser.post("/login/dev", form.fields)
    cookies = [c for c in response.headers.get_list("set-cookie") if c.startswith("rr_session=")]

    assert len(cookies) == 1
    assert "httponly" in cookies[0].lower()
    assert "samesite=lax" in cookies[0].lower()


def test_the_session_token_is_stored_only_as_a_hash(app, browser_a):
    raw = browser_a.client.cookies.get("rr_session")

    with database(app).transaction() as db:
        hashes = set(db.scalars(select(LoginSession.token_hash)))

    assert raw not in hashes
    assert token_hash(raw) in hashes


def test_logging_out_ends_the_session_on_the_server(app, browser_a):
    old_token = browser_a.client.cookies.get("rr_session")
    response = browser_a.post("/logout", {"csrf_token": browser_a.csrf_token()})
    replay = Browser(app)
    replay.client.cookies.set("rr_session", old_token)

    assert response.headers["location"] == "/login?notice=logged_out"
    assert replay.get("/experiences").headers.get("location") == "/login"


def test_sessions_expire_after_twelve_hours(browser_a, clock):
    clock.advance(hours=11, minutes=59)
    assert browser_a.get("/experiences").status_code == 200
    clock.advance(minutes=1)
    assert browser_a.get("/experiences").headers.get("location") == "/login"


def test_dev_login_needs_the_token_from_the_login_page(app):
    browser = Browser(app)
    form = [f for f in forms_in(browser.get("/login").text) if f.action == "/login/dev"][0]

    assert browser.post("/login/dev", {**form.fields, "login_token": "forged"}).status_code == 403
    assert Browser(app).post("/login/dev", form.fields).status_code == 403


def test_dev_login_only_accepts_synthetic_users(app):
    browser = Browser(app)
    form = [f for f in forms_in(browser.get("/login").text) if f.action == "/login/dev"][0]

    assert browser.post("/login/dev", {**form.fields, "user_id": "not-a-user"}).status_code == 400


def test_cross_site_posts_are_refused_even_with_a_valid_token(browser_a):
    form = browser_a.form("/experiences/new", "/experiences")
    data = {**form.fields, "actions": "跨站提交"}

    assert browser_a.post("/experiences", data, headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser_a.post("/experiences", data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert browser_a.post("/experiences", data, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert browser_a.post(
        "/experiences",
        data,
        headers={"Origin": "http://127.0.0.1", "Sec-Fetch-Site": "same-origin"},
    ).status_code == 303
    assert "跨站提交" in browser_a.get("/experiences").text


def test_oversized_posts_are_refused_before_the_form_is_read(browser_a):
    form = browser_a.form("/experiences/new", "/experiences")
    response = browser_a.post("/experiences", {**form.fields, "actions": "字" * 100_000})

    assert response.status_code == 413
    assert "还没有记录" in browser_a.get("/experiences").text


# --- recycle bin pages ----------------------------------------------------


def delete(browser, item_id):
    assert browser.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete").status_code == 303


def test_expired_content_is_shown_only_as_waiting_for_clean_up(browser_a, clock):
    item_id = browser_a.create(title="到期以后不该再显示的标题")
    delete(browser_a, item_id)
    restore = browser_a.form("/experiences?tab=trash", f"/experiences/{item_id}/restore")
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")
    page = browser_a.get("/experiences?tab=trash").text

    assert "到期以后不该再显示的标题" not in page
    assert "超过恢复期限" in page
    assert "正在等待清理" in page
    assert browser_a.post(f"/experiences/{item_id}/restore", {**restore.fields, "csrf_token": browser_a.csrf_token()}).status_code == 404


def test_a_failed_clean_up_is_visible_in_the_recycle_bin(app, browser_a, clock, monkeypatch):
    item_id = browser_a.create(actions="清理会失败的经历")
    delete(browser_a, item_id)
    clock.advance(days=7)

    def unavailable_disk(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(lifecycle, "_remove", unavailable_disk)
    app.state.library.run_cleanup()
    browser_a.sign_in("合成用户 A")
    page = browser_a.get("/experiences?tab=trash").text

    assert "清理失败（已尝试 1 次）" in page
    assert "系统会自动重试" in page


def test_permanent_deletion_that_cannot_finish_at_once_says_so(app, browser_a, monkeypatch):
    item_id = browser_a.create(actions="要永久删除的经历")
    delete(browser_a, item_id)

    def unavailable_disk(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(lifecycle, "_remove", unavailable_disk)
    response = browser_a.submit(f"/experiences/{item_id}/purge", f"/experiences/{item_id}/purge", acknowledge="yes")
    page = browser_a.get(response.headers["location"]).text

    assert response.headers["location"] == "/experiences?tab=trash&notice=purge_pending"
    assert "数据清理暂未完成" in page
    assert "你已于" in page and "申请永久删除" in page
    assert browser_a.get(f"/experiences/{item_id}/purge").status_code == 404


def test_permanent_deletion_needs_the_acknowledgement(browser_a):
    item_id = browser_a.create(actions="内容")
    delete(browser_a, item_id)
    form = browser_a.form(f"/experiences/{item_id}/purge", f"/experiences/{item_id}/purge")
    fields = {name: value for name, value in form.fields.items() if name != "acknowledge"}
    response = browser_a.post(f"/experiences/{item_id}/purge", fields)

    assert response.status_code == 422
    assert "请先勾选" in response.text
    assert "回收站" in browser_a.get("/experiences?tab=trash").text
    assert browser_a.get(f"/experiences/{item_id}/purge").status_code == 200


# --- the whole flow in one go ---------------------------------------------


def test_full_flow_from_one_sentence_to_deletion_and_export(browser_a):
    created = browser_a.submit("/experiences/new", "/experiences", actions="和同学做了一个记账小程序。")
    item_id = item_id_from(created.headers["location"])
    detail = f"/experiences/{item_id}"

    assert "待核对" in browser_a.get(detail).text

    saved = browser_a.submit(f"{detail}/edit", f"{detail}/draft", title="记账小程序", role="负责后端")
    assert "已保存修改（待核对）" in browser_a.get(saved.headers["location"]).text

    confirmed = browser_a.submit(detail, f"{detail}/confirm")
    assert "已确认属实" in browser_a.get(confirmed.headers["location"]).text

    browser_a.submit(f"{detail}/edit", f"{detail}/draft", outcome="还没上线，先停在原型")
    assert "已修改，待核对" in browser_a.get(detail).text

    history = browser_a.get(f"{detail}/history").text
    version_links = sorted(set(re.findall(rf"{detail}/versions/[0-9a-f-]{{36}}(?=\")", history)))
    assert len(version_links) == 2
    assert "初始版" in history and "确认版 1" in history

    for link in version_links:
        assert browser_a.get(link).status_code == 200

    removed = browser_a.submit(f"{version_links[0]}/delete", f"{version_links[0]}/delete")
    assert "已删除这个历史版本" in browser_a.get(removed.headers["location"]).text
    restored = browser_a.submit(f"{detail}/history", f"{version_links[0]}/restore")
    assert "已恢复这个历史版本" in browser_a.get(restored.headers["location"]).text

    assert browser_a.get(f"{detail}/export.md").status_code == 200
    assert browser_a.get("/export/all.zip").status_code == 200

    delete(browser_a, item_id)
    assert "记账小程序" not in browser_a.get("/experiences").text
    assert browser_a.get(detail).status_code == 404
    trash = browser_a.get("/experiences?tab=trash").text
    assert "记账小程序" in trash and "之前可以恢复" in trash

    back = browser_a.submit("/experiences?tab=trash", f"/experiences/{item_id}/restore")
    assert "已从回收站恢复" in browser_a.get(back.headers["location"]).text

    delete(browser_a, item_id)
    purged = browser_a.submit(f"/experiences/{item_id}/purge", f"/experiences/{item_id}/purge", acknowledge="yes")
    finished = browser_a.get(purged.headers["location"]).text
    assert "已永久删除，清理已完成" in finished
    assert "清理完成" in finished
    assert "记账小程序" not in finished


def test_only_a_failed_save_with_real_input_counts_as_unsaved(browser_a):
    empty = browser_a.submit("/experiences/new", "/experiences", title="  ")
    too_long = browser_a.submit("/experiences/new", "/experiences", title="字" * 201)

    assert "data-initially-dirty" not in empty.text
    assert "data-initially-dirty" in too_long.text


# --- pages prepared for an earlier deletion (Codex F1) ----------------------


def initial_version_path(app, item_id) -> tuple[str, str]:
    with database(app).transaction() as db:
        [version_id] = db.scalars(
            select(ExperienceVersion.id).where(ExperienceVersion.item_id == item_id)
        )

    return f"/experiences/{item_id}/versions/{version_id}", version_id


def test_an_old_permanent_delete_page_cannot_reach_a_new_deletion_of_the_version(app, browser_a):
    item_id = browser_a.create(actions="会被删除两次的历史版本")
    base, version_id = initial_version_path(app, item_id)
    history = f"/experiences/{item_id}/history"

    assert browser_a.submit(f"{base}/delete", f"{base}/delete").status_code == 303
    stale_purge = browser_a.form(f"{base}/purge", f"{base}/purge")
    assert browser_a.submit(history, f"{base}/restore").status_code == 303
    assert browser_a.submit(f"{base}/delete", f"{base}/delete").status_code == 303

    without_ack = browser_a.post(f"{base}/purge", stale_purge.fields)
    with_ack = browser_a.post(f"{base}/purge", {**stale_purge.fields, "acknowledge": "yes"})

    with database(app).transaction() as db:
        version = db.get(ExperienceVersion, version_id)

    assert without_ack.status_code == 409
    assert with_ack.status_code == 409
    assert "这个历史版本的状态已经变化" in with_ack.text
    assert version is not None and version.deleted_at is not None
    assert "已删除；在" in browser_a.get(history).text

    fresh_purge = browser_a.submit(f"{base}/purge", f"{base}/purge", acknowledge="yes")

    assert fresh_purge.headers["location"] == f"{history}?notice=version_purged"


def test_an_old_restore_button_cannot_undo_a_newer_deletion_of_the_version(app, browser_a):
    item_id = browser_a.create(actions="内容")
    base, version_id = initial_version_path(app, item_id)
    history = f"/experiences/{item_id}/history"

    browser_a.submit(f"{base}/delete", f"{base}/delete")
    stale_restore = browser_a.form(history, f"{base}/restore")
    browser_a.submit(history, f"{base}/restore")
    browser_a.submit(f"{base}/delete", f"{base}/delete")

    assert browser_a.post(f"{base}/restore", stale_restore.fields).status_code == 409

    with database(app).transaction() as db:
        assert db.get(ExperienceVersion, version_id).deleted_at is not None


def test_an_old_delete_page_is_refused_after_the_version_was_deleted_and_restored(app, browser_a):
    item_id = browser_a.create(actions="内容")
    base, version_id = initial_version_path(app, item_id)
    history = f"/experiences/{item_id}/history"

    stale_delete = browser_a.form(f"{base}/delete", f"{base}/delete")
    browser_a.submit(f"{base}/delete", f"{base}/delete")
    browser_a.submit(history, f"{base}/restore")

    assert browser_a.post(f"{base}/delete", stale_delete.fields).status_code == 409

    with database(app).transaction() as db:
        assert db.get(ExperienceVersion, version_id).deleted_at is None


def test_version_actions_without_a_state_are_refused(app, browser_a):
    item_id = browser_a.create(actions="内容")
    base, _ = initial_version_path(app, item_id)
    token = {"csrf_token": browser_a.csrf_token(), "acknowledge": "yes"}

    assert browser_a.post(f"{base}/delete", token).status_code == 400
    browser_a.submit(f"{base}/delete", f"{base}/delete")
    assert browser_a.post(f"{base}/restore", token).status_code == 400
    assert browser_a.post(f"{base}/purge", token).status_code == 400


def test_old_recycle_bin_pages_cannot_reach_a_new_deletion_of_the_experience(browser_a):
    item_id = browser_a.create(title="会被删除两次的经历")
    purge = f"/experiences/{item_id}/purge"
    restore = f"/experiences/{item_id}/restore"

    delete(browser_a, item_id)
    stale_purge = browser_a.form(purge, purge)
    stale_restore = browser_a.form("/experiences?tab=trash", restore)
    browser_a.submit("/experiences?tab=trash", restore)
    delete(browser_a, item_id)

    assert browser_a.post(purge, {**stale_purge.fields, "acknowledge": "yes"}).status_code == 409
    assert browser_a.post(purge, stale_purge.fields).status_code == 409
    assert browser_a.post(restore, stale_restore.fields).status_code == 409
    assert "会被删除两次的经历" in browser_a.get("/experiences?tab=trash").text


# --- saving from the leave dialog while logging out (Codex F2) --------------


def test_the_logout_button_and_forms_are_marked_for_the_leave_guard(app, browser_a):
    page = browser_a.get("/experiences/new").text
    script = Browser(app).get("/static/leave_guard.js").text

    assert '<form method="post" action="/logout" class="inline" data-logout>' in page
    assert '<input type="hidden" name="after_save" value="">' in page
    assert 'hasAttribute("data-logout")' in script
    assert "afterSaveField.value = leave.afterSave" in script


def test_saving_before_logging_out_saves_first_and_then_ends_the_session(app, browser_a):
    old_token = browser_a.client.cookies.get("rr_session")
    response = browser_a.submit(
        "/experiences/new",
        "/experiences",
        actions="退出前保存的内容",
        after_save="logout",
    )
    replay = Browser(app)
    replay.client.cookies.set("rr_session", old_token)

    assert response.headers["location"] == "/login?notice=saved_logged_out"
    assert replay.get("/experiences").headers.get("location") == "/login"
    assert browser_a.get("/experiences").headers.get("location") == "/login"
    assert "已保存草稿（待核对），并已退出。" in browser_a.get(response.headers["location"]).text

    browser_a.sign_in("合成用户 A")

    assert "退出前保存的内容" in browser_a.get("/experiences").text


def test_saving_an_edit_before_logging_out_also_ends_the_session(browser_a):
    item_id = browser_a.create(actions="原来的内容")
    response = browser_a.submit(
        f"/experiences/{item_id}/edit",
        f"/experiences/{item_id}/draft",
        actions="退出前改好的内容",
        after_save="logout",
    )

    assert response.headers["location"] == "/login?notice=saved_logged_out"
    assert browser_a.get("/experiences").headers.get("location") == "/login"

    browser_a.sign_in("合成用户 A")

    assert "退出前改好的内容" in browser_a.get(f"/experiences/{item_id}").text


def test_a_save_that_fails_before_logging_out_keeps_the_session_and_the_input(app, browser_a):
    empty = browser_a.submit("/experiences/new", "/experiences", title="   ", after_save="logout")

    assert empty.status_code == 422
    assert browser_a.get("/experiences").status_code == 200

    sessions = database(app)._sessions
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        failed = browser_a.submit(
            "/experiences/new",
            "/experiences",
            actions="没能保存，所以也不能退出",
            after_save="logout",
        )
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    assert failed.status_code == 500
    assert "没能保存，所以也不能退出" in failed.text
    # The retry form does not carry the logout over: saving again only saves.
    assert '<input type="hidden" name="after_save" value="">' in failed.text
    assert browser_a.get("/experiences").status_code == 200


def test_a_conflict_before_logging_out_keeps_the_session_and_the_input(browser_a):
    item_id = browser_a.create(actions="第一版")
    edit, action = f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft"
    stale = browser_a.form(edit, action)
    browser_a.submit(edit, action, actions="另一个标签页的修改")
    clash = browser_a.post(
        action,
        {**stale.fields, "actions": "退出前没保存成功的修改", "after_save": "logout"},
    )

    assert clash.status_code == 409
    assert "退出前没保存成功的修改" in clash.text
    assert browser_a.get("/experiences").status_code == 200


def test_saving_before_logging_out_still_needs_the_csrf_token(browser_a):
    form = browser_a.form("/experiences/new", "/experiences")
    data = {**form.fields, "csrf_token": "forged", "actions": "伪造的保存", "after_save": "logout"}

    assert browser_a.post("/experiences", data).status_code == 403

    listing = browser_a.get("/experiences")

    assert listing.status_code == 200
    assert "伪造的保存" not in listing.text


def test_if_logging_out_fails_after_a_save_the_page_says_so(browser_a, monkeypatch):
    def unavailable_database(db, session_token_hash):
        raise OperationalError("DELETE", None, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(web, "end_session", unavailable_database)
    response = browser_a.submit(
        "/experiences/new",
        "/experiences",
        actions="保存成功但退出失败",
        after_save="logout",
    )

    assert response.status_code == 500
    assert "草稿已保存，但还没有退出" in response.text
    assert "保存成功但退出失败" in browser_a.get("/experiences").text


def test_other_after_save_values_do_not_log_out(browser_a):
    response = browser_a.submit("/experiences/new", "/experiences", actions="普通保存", after_save="elsewhere")

    assert response.headers["location"].endswith("?notice=created")
    assert browser_a.get("/experiences").status_code == 200
