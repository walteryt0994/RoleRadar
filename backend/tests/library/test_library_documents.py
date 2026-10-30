"""U1-B: the basic profile and the job preferences.

Each user keeps at most one of each. They are saved as they are (never
"confirmed"), every changing save is kept as a history version, and they are
deleted, restored, purged and exported like experiences, without touching
any other record. Education lives in the education experiences only.
"""
import hashlib
import io
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import OperationalError

from app.library.content import PREFERENCE_FIELDS, PROFILE_FIELDS
from app.library.db import LibraryBase, make_engine
from app.library.errors import ConfigError
from app.library.lifecycle import purge_due
from app.library.models import SCHEMA_VERSION, ExperienceItem, ExperienceVersion, PurgeJob, create_schema
from library_helpers import Browser, form_for

SENTINEL = "资料哨兵-8d1c"
HOSTILE = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def database(app):
    return app.state.library.database


def save_profile(browser, **values):
    page = browser.get("/basic-profile/edit").text
    [form] = [form for form in _post_forms(page) if form.action.startswith("/basic-profile")]

    return browser.post(form.action, {**form.fields, **values})


def save_preferences(browser, **values):
    page = browser.get("/job-preferences/edit").text
    [form] = [form for form in _post_forms(page) if form.action.startswith("/job-preferences")]

    return browser.post(form.action, {**form.fields, **values})


def _post_forms(html):
    from library_helpers import forms_in

    return [form for form in forms_in(html) if form.method == "post" and form.action not in ("/logout",)]


def document(app, kind):
    with database(app).transaction() as db:
        return db.scalar(
            select(ExperienceItem).where(ExperienceItem.category == kind, ExperienceItem.deleted_at.is_(None))
        )


def all_documents(app, kind):
    with database(app).transaction() as db:
        return db.scalars(select(ExperienceItem).where(ExperienceItem.category == kind)).all()


def versions_of(app, item_id):
    with database(app).transaction() as db:
        return db.scalars(
            select(ExperienceVersion).where(ExperienceVersion.item_id == item_id).order_by(ExperienceVersion.number)
        ).all()


def delete(browser, item_id):
    response = browser.submit(f"/experiences/{item_id}/delete", f"/experiences/{item_id}/delete")
    assert response.status_code == 303, response.text

    return response


def unzip(response) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def by_suffix(files, suffix):
    [name] = [name for name in files if name.endswith(suffix)]

    return files[name]


# --- nothing is required, and education is kept once -----------------------------


def test_experiences_need_no_profile_or_preferences(browser_a):
    item_id = browser_a.create(actions="还没填姓名也能记")
    profile_page = browser_a.get("/basic-profile").text

    assert browser_a.get(f"/experiences/{item_id}").status_code == 200
    assert "记录经历不需要先填这些" in profile_page
    assert '<a class="button primary" href="/basic-profile/edit">填写基本资料</a>' in profile_page
    assert '<a class="button primary" href="/job-preferences/edit">填写求职偏好</a>' in browser_a.get("/job-preferences").text


def test_the_profile_keeps_what_was_written_and_checks_contacts(app, browser_a):
    values = {
        "name": "合成名字",
        "phone": "+86 138-0000-0000",
        "email": "someone@example.com",
        "city": "合成市",
        "links": "https://example.com/me\njavascript:alert(1)",
    }
    response = save_profile(browser_a, **values)
    page = browser_a.get("/basic-profile").text

    assert response.status_code == 303
    assert response.headers["location"] == "/basic-profile?notice=profile_created"
    assert document(app, "profile").content == values
    assert all(value in page for value in ("合成名字", "+86 138-0000-0000", "someone@example.com", "合成市"))
    assert '<a href="https://example.com/me" rel="noopener noreferrer">' in page
    assert 'href="javascript:' not in page

    for field, bad in (("email", "not-an-email"), ("phone", "打电话给我"), ("phone", "12")):
        rejected = save_profile(browser_a, **{field: bad})
        assert rejected.status_code == 422, (field, bad)
        assert f'aria-describedby="h-{field} e-{field}" aria-invalid="true"' in rejected.text
        assert bad in rejected.text

    assert document(app, "profile").content == values


def test_an_empty_profile_or_preferences_is_not_saved(app, browser_a):
    empty_profile = save_profile(browser_a)
    empty_preferences = save_preferences(browser_a)

    assert empty_profile.status_code == 422 and "请至少填写一项再保存" in empty_profile.text
    assert empty_preferences.status_code == 422 and "请至少写一项再保存" in empty_preferences.text
    assert all_documents(app, "profile") == [] and all_documents(app, "preferences") == []


def test_education_lives_only_in_education_experiences(browser_a):
    browser_a.submit("/experiences/new?kind=education", "/experiences", school="合成大学", major="合成专业")
    browser_a.create(title="不是教育的项目", actions="x")
    form_page = browser_a.get("/basic-profile/edit").text
    profile_page = browser_a.get("/basic-profile").text

    assert {field.name for field in PROFILE_FIELDS} == {"name", "phone", "email", "city", "links"}
    assert 'name="school"' not in form_page and 'name="major"' not in form_page
    assert "合成大学" in profile_page and "合成专业" in profile_page
    assert "不是教育的项目" not in profile_page
    assert 'href="/experiences/new?kind=education"' in profile_page


def test_the_current_city_and_the_target_cities_are_kept_apart(app, browser_a):
    save_profile(browser_a, city="当前城市-合成")
    save_preferences(browser_a, cities_ideal="目标城市-合成")

    profile_page = browser_a.get("/basic-profile").text
    preferences_page = browser_a.get("/job-preferences").text

    assert "当前城市-合成" in profile_page and "目标城市-合成" not in profile_page
    assert "目标城市-合成" in preferences_page and "当前城市-合成" not in preferences_page
    assert "city" not in document(app, "preferences").content
    assert "cities_ideal" not in document(app, "profile").content


# --- preferences say what was said, and nothing more ------------------------------


def test_unanswered_preferences_read_as_not_decided_never_as_acceptable(app, browser_a):
    save_preferences(browser_a, cities_ideal="合成城市")
    page = browser_a.get("/job-preferences").text
    markdown = browser_a.get(f"/experiences/{document(app, 'preferences').id}/export.md").content.decode("utf-8")

    answers = page.split('<div class="preferences">', 1)[1]

    # Four of the five three-way preferences and both single ones are unanswered.
    current = markdown.split("## 当前内容", 1)[1].split("## 历史版本", 1)[0]

    assert answers.count("尚未想好") == 6
    assert current.count("尚未想好") == 6
    assert "**目标城市**\n\n理想：合成城市" in markdown

    # The answers never turn silence into consent (the page's own note says so in words).
    for phrase in ("都能接受", "都可以接受", "无所谓"):
        assert phrase not in answers and phrase not in markdown


def test_ideal_acceptable_and_unacceptable_stay_distinct(browser_a):
    save_preferences(browser_a, cities_ideal="理想城-合成", cities_ok="可接受城-合成", cities_no="不能接受城-合成")
    page = browser_a.get("/job-preferences").text

    assert "<dt>理想</dt><dd class=\"text\">理想城-合成</dd>" in page
    assert "<dt>也可以接受</dt><dd class=\"text\">可接受城-合成</dd>" in page
    assert "<dt>不能接受</dt><dd class=\"text\">不能接受城-合成</dd>" in page


def test_no_demo_or_owner_values_are_filled_in(browser_a):
    for path in ("/basic-profile/edit", "/job-preferences/edit"):
        page = browser_a.get(path).text
        [form] = [form for form in _post_forms(page)]

        assert all(value == "" for name, value in form.fields.items() if name not in ("csrf_token", "next", "after_save"))

        for word in ("北京", "FDE", "AI 应用"):
            assert word not in page, (path, word)


def test_the_preference_folds_open_for_what_they_hold(browser_a):
    save_preferences(browser_a, cities_ideal="合成城", job_types_no="不考虑的类型")
    page = browser_a.get("/job-preferences/edit").text

    assert page.count('<details class="fold" open>') == 1
    assert page.count('<details class="fold">') == 4


# --- saving: versions, unchanged saves and conflicts ------------------------------


def test_every_changing_save_is_kept_as_a_version(app, browser_a):
    assert save_profile(browser_a, name="第一次").status_code == 303
    assert save_profile(browser_a, name="第二次").status_code == 303
    unchanged = save_profile(browser_a, name="第二次")
    item = document(app, "profile")

    assert unchanged.headers["location"] == "/basic-profile?notice=profile_unchanged"
    assert [(version.kind, version.number, version.content["name"]) for version in versions_of(app, item.id)] == [
        ("saved", 1, "第一次"),
        ("saved", 2, "第二次"),
    ]

    history = browser_a.get(f"/experiences/{item.id}/history").text

    assert "第 1 次保存" in history and "第 2 次保存" in history
    assert "每次保存有变化时都会留一份历史版本" in history
    assert "确认" not in history.split("<h2>操作记录</h2>")[0].split("</h1>")[1]


def test_a_stale_page_cannot_overwrite_a_newer_save(app, browser_a):
    save_profile(browser_a, name="最初")
    stale = form_for(browser_a.get("/basic-profile/edit").text, f"/basic-profile/{document(app, 'profile').id}/save")
    assert save_profile(browser_a, name=f"较新{SENTINEL}").status_code == 303
    response = browser_a.post(stale.action, {**stale.fields, "name": "旧页面写的"})

    assert response.status_code == 409
    assert "保存没有生效：你的基本资料已经有更新的保存" in response.text
    assert f"较新{SENTINEL}" in response.text and "旧页面写的" in response.text
    assert document(app, "profile").content["name"] == f"较新{SENTINEL}"


def test_two_pages_starting_the_profile_never_make_two(app, browser_a):
    first = form_for(browser_a.get("/basic-profile/edit").text, "/basic-profile")
    second = form_for(browser_a.get("/basic-profile/edit").text, "/basic-profile")

    assert browser_a.post("/basic-profile", {**first.fields, "name": "第一页"}).status_code == 303
    response = browser_a.post("/basic-profile", {**second.fields, "name": "第二页"})

    assert response.status_code == 409
    assert len(all_documents(app, "profile")) == 1
    assert document(app, "profile").content["name"] == "第一页"


def test_documents_are_never_confirmed(app, browser_a):
    save_profile(browser_a, name="合成")
    page = browser_a.get("/basic-profile").text

    assert "确认这些内容属实" not in page
    assert "待核对" not in page
    assert "保存草稿" not in browser_a.get("/basic-profile/edit").text


def _fail_on_write(session, flush_context, instances):
    raise OperationalError("COMMIT", None, sqlite3.OperationalError("disk I/O error"))


@pytest.mark.parametrize(
    ("kind", "path", "save", "field"),
    [
        ("profile", "/basic-profile", save_profile, "name"),
        ("preferences", "/job-preferences", save_preferences, "cities_ideal"),
    ],
)
def test_a_failed_save_shows_no_success_and_keeps_input_and_last_save(app, browser_a, kind, path, save, field):
    sessions = database(app)._sessions
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        first = save(browser_a, **{field: "第一次就没能保存"})
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    assert first.status_code == 500
    assert "保存失败" in first.text and "第一次就没能保存" in first.text
    assert all_documents(app, kind) == []

    save(browser_a, **{field: "最后一次成功保存的内容"})
    item = document(app, kind)
    event.listen(sessions, "before_flush", _fail_on_write)

    try:
        failed = save(browser_a, **{field: "这次没能保存的新内容"})
    finally:
        event.remove(sessions, "before_flush", _fail_on_write)

    assert failed.status_code == 500
    assert "保存失败" in failed.text and "已保存" not in failed.text
    assert "这次没能保存的新内容" in failed.text
    assert "data-initially-dirty" in failed.text
    assert document(app, kind).content[field] == "最后一次成功保存的内容"
    assert document(app, kind).revision == item.revision
    assert len(versions_of(app, item.id)) == 1
    assert "这次没能保存的新内容" not in browser_a.get(path).text


# --- deleting only what was chosen, for exactly seven days ------------------------


def test_deleting_the_profile_leaves_every_other_record_alone(app, browser_a):
    experience = browser_a.create(actions=f"经历{SENTINEL}")
    save_profile(browser_a, name="合成")
    save_preferences(browser_a, cities_ideal="合成城")
    profile_id = document(app, "profile").id
    confirm = browser_a.get(f"/experiences/{profile_id}/delete").text
    response = delete(browser_a, profile_id)
    trash = browser_a.get("/experiences?tab=trash").text

    assert "你的经历（包括教育经历）和求职偏好不受影响。" in confirm
    assert response.headers["location"] == "/basic-profile?notice=profile_deleted"
    assert document(app, "profile") is None
    assert document(app, "preferences") is not None
    assert browser_a.get(f"/experiences/{experience}").status_code == 200
    assert "<strong>基本资料</strong>" in trash
    assert "去回收站恢复" in browser_a.get("/basic-profile").text


def test_the_profile_can_be_restored_until_exactly_seven_days(app, browser_a, clock):
    save_profile(browser_a, name=f"名字{SENTINEL}")
    profile_id = document(app, "profile").id
    delete(browser_a, profile_id)
    clock.advance(days=3)
    browser_a.sign_in("合成用户 A")
    restored = browser_a.submit("/experiences?tab=trash", f"/experiences/{profile_id}/restore")

    assert restored.headers["location"] == "/basic-profile?notice=restored"
    assert document(app, "profile").id == profile_id

    delete(browser_a, profile_id)
    form = form_for(browser_a.get("/experiences?tab=trash").text, f"/experiences/{profile_id}/restore")
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")

    # The new session's token, so that only the deadline can refuse it.
    late = browser_a.post(f"/experiences/{profile_id}/restore", {**form.fields, "csrf_token": browser_a.csrf_token()})

    assert late.status_code == 404
    assert SENTINEL not in b"".join(unzip(browser_a.get("/export/all.zip")).values()).decode("utf-8")

    purge_due(database(app), clock.now())

    assert all_documents(app, "profile") == []
    assert versions_of(app, profile_id) == []


def test_an_old_profile_is_not_restored_over_a_newer_one(app, browser_a):
    save_profile(browser_a, name="旧的资料")
    old_id = document(app, "profile").id
    delete(browser_a, old_id)
    save_profile(browser_a, name="新的资料")
    new_id = document(app, "profile").id
    response = browser_a.submit("/experiences?tab=trash", f"/experiences/{old_id}/restore")

    assert response.status_code == 409
    assert "你现在已经有一份基本资料" in response.text
    assert document(app, "profile").id == new_id
    assert document(app, "profile").content["name"] == "新的资料"

    delete(browser_a, new_id)

    assert browser_a.submit("/experiences?tab=trash", f"/experiences/{old_id}/restore").status_code == 303
    assert document(app, "profile").content["name"] == "旧的资料"


def test_purging_the_preferences_removes_only_them(app, browser_a):
    experience = browser_a.create(actions="保留的经历")
    save_profile(browser_a, name="保留的资料")
    save_preferences(browser_a, cities_ideal="合成城")
    preferences_id = document(app, "preferences").id
    delete(browser_a, preferences_id)
    response = browser_a.submit(
        f"/experiences/{preferences_id}/purge", f"/experiences/{preferences_id}/purge", acknowledge="yes"
    )
    trash = browser_a.get("/experiences?tab=trash").text

    with database(app).transaction() as db:
        job = db.scalar(select(PurgeJob).where(PurgeJob.target_id == preferences_id))

    assert response.status_code == 303
    assert all_documents(app, "preferences") == []
    assert versions_of(app, preferences_id) == []
    assert job.category == "preferences" and job.status == "done"
    assert "一份求职偏好（你申请的永久删除）" in trash
    assert document(app, "profile").content["name"] == "保留的资料"
    assert browser_a.get(f"/experiences/{experience}").status_code == 200


def test_a_profile_version_is_deleted_restored_and_purged_on_its_own(app, browser_a):
    save_profile(browser_a, name="第一版")
    save_profile(browser_a, name="第二版")
    item = document(app, "profile")
    first = versions_of(app, item.id)[0]
    path = f"/experiences/{item.id}/versions/{first.id}"

    assert browser_a.submit(f"{path}/delete", f"{path}/delete").status_code == 303
    assert browser_a.get(path).status_code == 404
    assert browser_a.submit(f"/experiences/{item.id}/history", f"{path}/restore").status_code == 303
    assert browser_a.get(path).status_code == 200
    assert document(app, "profile").content["name"] == "第二版"


# --- export and isolation ------------------------------------------------------------


def test_the_profile_and_preferences_are_exported_with_every_field(app, browser_a):
    profile = {field.name: f"{field.label}{SENTINEL}" for field in PROFILE_FIELDS}
    profile["email"] = "a@example.com"
    profile["phone"] = "138 0000 0000"
    preferences = {field.name: f"{field.label}{SENTINEL}" for field in PREFERENCE_FIELDS}
    save_profile(browser_a, **profile)
    save_preferences(browser_a, **preferences)
    files = unzip(browser_a.get("/export/all.zip"))
    data = json.loads(by_suffix(files, "/data.json"))
    manifest = json.loads(by_suffix(files, "/manifest.json"))
    profile_md = by_suffix(files, "/profile.md").decode("utf-8")
    preferences_md = by_suffix(files, "/preferences.md").decode("utf-8")

    assert data["profile"]["content"] == profile
    assert data["preferences"]["content"] == preferences
    assert "private_note" not in data["profile"] and "state" not in data["profile"]
    assert manifest["counts"]["profile"] == 1 and manifest["counts"]["preferences"] == 1
    assert all(f"**{field.label}**\n\n{profile[field.name]}" in profile_md for field in PROFILE_FIELDS)
    assert all(preferences[field.name] in preferences_md for field in PREFERENCE_FIELDS)
    assert "不再受平台的权限保护" in profile_md
    assert "## 私有备注" not in profile_md


def test_a_deleted_profile_is_exported_apart_with_its_deadline(app, browser_a):
    save_profile(browser_a, name=f"回收站里的{SENTINEL}")
    delete(browser_a, document(app, "profile").id)
    files = unzip(browser_a.get("/export/all.zip"))
    data = json.loads(by_suffix(files, "/data.json"))

    assert data["profile"] is None
    assert [record["category"] for record in data["recycle_bin"]] == ["profile"]
    assert data["recycle_bin"][0]["restore_deadline"] == "2026-11-05T16:00:00Z"
    assert not [name for name in files if name.endswith("/profile.md")]


def test_another_user_never_sees_or_changes_my_profile(app, browser_a, browser_b):
    save_profile(browser_a, name=f"A 的名字{SENTINEL}")
    save_preferences(browser_a, cities_ideal=f"A 的城市{SENTINEL}")
    profile_id = document(app, "profile").id

    for path in ("/basic-profile", "/basic-profile/edit", "/job-preferences", "/job-preferences/edit"):
        assert SENTINEL not in browser_b.get(path).text, path

    for path in (f"/experiences/{profile_id}/history", f"/experiences/{profile_id}/export.md", f"/experiences/{profile_id}/delete"):
        assert browser_b.get(path).status_code == 404, path

    assert SENTINEL not in b"".join(unzip(browser_b.get("/export/all.zip")).values()).decode("utf-8")

    response = save_profile(browser_b, name="B 自己的名字")

    assert response.status_code == 303
    assert document(app, "profile").content["name"] == f"A 的名字{SENTINEL}"


def test_profile_text_is_shown_as_text(browser_a):
    save_profile(browser_a, name=HOSTILE, links=HOSTILE)
    save_preferences(browser_a, cities_ideal=HOSTILE)

    for path in ("/basic-profile", "/basic-profile/edit", "/job-preferences", "/job-preferences/edit"):
        page = browser_a.get(path).text
        assert "<script>alert" not in page and "<img src=x" not in page, path
        assert "&lt;script&gt;" in page, path


def test_saving_the_profile_from_the_leave_dialog_says_what_was_saved(app, browser_a):
    page = browser_a.get("/basic-profile/edit").text
    form = form_for(page, "/basic-profile")
    response = browser_a.post("/basic-profile", {**form.fields, "name": "离开前保存", "next": "/experiences"})

    assert response.headers["location"] == "/experiences?notice=profile_created_before_leaving"
    assert "已保存你刚才填写的基本资料。" in browser_a.get(response.headers["location"]).text


def test_saving_the_profile_then_logging_out(app, browser_a):
    form = form_for(browser_a.get("/basic-profile/edit").text, "/basic-profile")
    response = browser_a.post("/basic-profile", {**form.fields, "name": "退出前保存", "after_save": "logout"})

    assert response.headers["location"] == "/login?notice=document_saved_logged_out"
    assert browser_a.get("/basic-profile").status_code == 303
    assert document(app, "profile").content["name"] == "退出前保存"


# --- the version-2 data file -----------------------------------------------------------


def _file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_file_of_an_older_version_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "old.db"

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE library_meta (key VARCHAR(50) PRIMARY KEY, value VARCHAR(200))")
        connection.execute("INSERT INTO library_meta VALUES ('schema_version', '1')")

    before = _file_digest(path)
    engine = make_engine(path)

    with pytest.raises(ConfigError, match="结构版本是 1"):
        create_schema(engine)

    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert tables == {"library_meta"}
    assert _file_digest(path) == before


def test_a_new_file_gets_every_table_and_the_version_at_once(tmp_path):
    engine = make_engine(tmp_path / "new.db")
    create_schema(engine)
    tables = set(inspect(engine).get_table_names())

    with engine.connect() as connection:
        version = connection.exec_driver_sql("SELECT value FROM library_meta").scalar_one()

    engine.dispose()

    assert tables == set(LibraryBase.metadata.tables)
    assert version == SCHEMA_VERSION == "2"


def test_a_file_without_its_version_is_refused(tmp_path):
    path = tmp_path / "broken.db"

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE library_meta (key VARCHAR(50) PRIMARY KEY, value VARCHAR(200))")

    engine = make_engine(path)

    with pytest.raises(ConfigError, match="结构版本是 未知"):
        create_schema(engine)

    engine.dispose()


# --- the leave dialog shared by every form --------------------------------------


def test_a_late_close_event_never_forgets_a_new_leave(app):
    """The browser behaviour is checked in a real browser (see the U1-B report);
    this keeps the rule in place: only a dialog that is really closed forgets."""
    script = Browser(app).get("/static/leave_guard.js").text
    handler = script[script.index('dialog.addEventListener("close"'):]
    handler = handler[: handler.index("});") + 3]

    assert "if (!dialog.open) {" in handler
    assert handler.index("if (!dialog.open) {") < handler.index("pendingLeave = null;")


def test_processes_opening_one_new_file_together_all_start(tmp_path, monkeypatch):
    """Four starts at once on a slow disk: looking at the file and writing it
    must happen under one lock, or the later starts collide and fail."""
    import threading
    import time

    from app.library import models

    real_inspect = models.inspect

    class SlowInspector:
        def __init__(self, target):
            self.inner = real_inspect(target)

        def get_table_names(self):
            names = self.inner.get_table_names()
            time.sleep(0.2)

            return names

    monkeypatch.setattr(models, "inspect", SlowInspector)
    path = tmp_path / "shared.db"
    start = threading.Barrier(4)
    errors = []

    def open_library():
        engine = make_engine(path)

        try:
            start.wait()
            create_schema(engine)
        except Exception as error:  # noqa: BLE001 - any failure fails the test below
            errors.append(type(error).__name__)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=open_library) for _ in range(4)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    engine = make_engine(path)

    with engine.connect() as connection:
        rows = connection.exec_driver_sql("SELECT value FROM library_meta").all()

    engine.dispose()

    assert errors == []
    assert rows == [("2",)]


def test_a_file_that_is_not_sqlite_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "notes.db"
    path.write_text("这不是数据库", encoding="utf-8")
    before = _file_digest(path)
    engine = make_engine(path)

    with pytest.raises(ConfigError, match="不是可用的 SQLite 数据库"):
        create_schema(engine)

    engine.dispose()

    assert _file_digest(path) == before
