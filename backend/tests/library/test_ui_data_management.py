"""UI bilingual batch: 数据管理 (plan 2) and the export built before it is downloaded (5.6).

The export is built once; its result page says what the package holds and which files it could not put in, from that
very build; the package waits for one download under a token of this user and session, for ten minutes at most. Logging
out, the time running out, a deletion or purge since the build, cancelling or downloading frees it. The direct
/export/all.zip stays. Synthetic data only; nothing leaves the process.
"""
import io
import re
import zipfile

import pytest

from app.library import export as export_module
from job_helpers import confirmed, text_of
from library_helpers import Browser, forms_in


@pytest.fixture
def item(browser_a):
    return confirmed(browser_a, title="导出测试项目（合成）", actions="整理了每周数据。")


def build(browser):
    form = next(form for form in forms_in(browser.get("/data/export").text) if form.action == "/data/export/build")
    response = browser.post(form.action, form.fields)
    assert response.status_code == 303, response.text

    return response.headers["location"]


def token_of(location: str) -> str:
    return location.rsplit("/", 1)[1]


def names_in(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.namelist()


def test_the_overview_counts_and_leads_to_the_trash_and_the_export(browser_a, item):
    page = browser_a.get("/data").text

    assert 'href="/data/trash"' in page and 'href="/data/export"' in page
    trash = browser_a.get("/data/trash")
    assert trash.status_code == 200 and 'aria-current="page"' in trash.text


def test_an_export_is_built_shown_and_downloaded_once(app, browser_a, item):
    location = build(browser_a)
    result = browser_a.get(location).text
    assert "这次构建完整" in text_of(result)

    token = token_of(location)
    download = browser_a.get(f"/data/export/download/{token}")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert any(name.endswith("manifest.json") for name in names_in(download.content))

    # One download: the token is used up, and the package freed.
    assert browser_a.get(f"/data/export/download/{token}").status_code == 404
    assert len(app.state.export_holds) == 0


def test_a_package_is_its_builders_session_alone(app, browser_a, browser_b, item):
    token = token_of(build(browser_a))

    assert browser_b.get(f"/data/export/result/{token}").status_code == 404
    assert browser_b.get(f"/data/export/download/{token}").status_code == 404
    other_session = Browser.signed_in(app, "合成用户 A")
    assert other_session.get(f"/data/export/download/{token}").status_code == 404
    assert browser_a.get(f"/data/export/result/{token}").status_code == 200


def test_logging_out_frees_the_package(app, browser_a, item):
    token = token_of(build(browser_a))
    logout = browser_a.form("/data/export", "/logout")
    browser_a.post(logout.action, logout.fields)

    assert len(app.state.export_holds) == 0


def test_a_package_lapses_after_ten_minutes(app, browser_a, item):
    holds = app.state.export_holds
    now = [1000.0]
    holds._clock = lambda: now[0]
    token = token_of(build(browser_a))
    now[0] += 10 * 60

    assert browser_a.get(f"/data/export/download/{token}").status_code == 404
    assert len(holds) == 0


def test_a_deletion_after_the_build_spoils_the_package(app, browser_a, item):
    token = token_of(build(browser_a))
    form = next(form for form in forms_in(browser_a.get(f"/experiences/{item}/delete").text) if form.action == f"/experiences/{item}/delete")
    assert browser_a.post(form.action, form.fields).status_code == 303

    response = browser_a.get(f"/data/export/download/{token}")
    assert response.status_code == 409 and "重新构建" in text_of(response.text)
    assert len(app.state.export_holds) == 0


def test_files_left_out_are_listed_from_the_same_build_and_what_is_there_can_be_downloaded(app, browser_a, item, monkeypatch):
    browser_a.upload(item)
    monkeypatch.setattr(export_module, "_copy_attachment", lambda *args, **kwargs: export_module.MISSING)
    location = build(browser_a)
    page = browser_a.get(location).text

    assert "导出不完整" in text_of(page)
    assert f'href="/experiences/{item}"' in page
    download = browser_a.get(f"/data/export/download/{token_of(location)}")
    assert download.status_code == 200
    assert any("incomplete" in name or name.endswith("README.md") for name in names_in(download.content))


def test_cancelling_frees_the_package(app, browser_a, item):
    location = build(browser_a)
    form = next(form for form in forms_in(browser_a.get(location).text) if form.action.startswith("/data/export/cancel/"))
    assert browser_a.post(form.action, form.fields).status_code == 303
    assert len(app.state.export_holds) == 0


def test_the_direct_export_link_still_works(browser_a, item):
    response = browser_a.get("/export/all.zip")

    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"


def test_an_export_built_in_english_explains_itself_in_english_and_keeps_the_users_words(browser_a, item):
    assert browser_a.post("/language", {"lang": "en", "next": "/data/export"}).status_code == 303
    location = build(browser_a)
    data = browser_a.get(f"/data/export/download/{token_of(location)}").content

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        readme = next(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith("README.md"))
        item_file = next(archive.read(name).decode("utf-8") for name in archive.namelist() if "/experiences/" in name and name.endswith(".md"))

    assert not re.search(r"[一-鿿]", readme.split("\n", 3)[0])
    assert "导出测试项目（合成）" in item_file and "整理了每周数据。" in item_file


# --- a deletion spoils a package at the moment it is written (Codex review F1) ---------------------------------------


def delete_item(browser, item):
    form = browser.form(f"/experiences/{item}/delete", f"/experiences/{item}/delete")
    assert browser.post(form.action, form.fields).status_code == 303


def test_a_deletion_drops_the_held_package_at_once_and_its_pages_say_why(app, browser_a, item):
    location = build(browser_a)
    delete_item(browser_a, item)

    # Dropped (and closed) as the deletion was written, before any page of the export was opened again.
    assert len(app.state.export_holds) == 0
    result = browser_a.get(location)
    download = browser_a.get(f"/data/export/download/{token_of(location)}")
    assert result.status_code == 409 and download.status_code == 409
    assert "重新构建" in text_of(result.text)


def test_a_deletion_while_the_package_is_built_makes_the_build_refused_and_closed(app, browser_a, item, monkeypatch):
    from app import data_management

    original = data_management.write_export
    closed = []

    def build_then_delete(*args, **kwargs):
        package = original(*args, **kwargs)
        handle_close = package.handle.close
        package.handle.close = lambda: (closed.append(True), handle_close())[1]
        # The deletion lands after everything was read and written, before the package is held.
        delete_item(browser_a, item)

        return package

    monkeypatch.setattr(data_management, "write_export", build_then_delete)
    form = browser_a.form("/data/export", "/data/export/build")
    response = browser_a.post(form.action, form.fields)

    assert response.status_code == 409 and "没有保留" in text_of(response.text)
    assert len(app.state.export_holds) == 0 and closed == [True]


def test_a_deletion_that_rolls_back_still_spoils_to_be_safe(app, browser_a, item):
    from app.library.models import AuditEvent, ExperienceItem

    token = token_of(build(browser_a))
    rt = app.state.library

    # Spoiling happens in the flush, before the commit: a deletion rolled back afterwards has spoiled it all the same.
    with pytest.raises(RuntimeError):
        with rt.database.transaction() as db:
            owner = db.get(ExperienceItem, item).owner_id
            db.add(AuditEvent(owner_id=owner, item_id=item, object_type="experience", object_id=item, action="deleted", at=rt.now()))
            db.flush()
            raise RuntimeError("synthetic failure after the flush")

    assert browser_a.get(f"/data/export/download/{token}").status_code == 409


def test_a_deletion_written_by_another_writer_is_found_by_the_pages_own_check(app, browser_a, item):
    """The flush hook sees what this app writes; a deletion another writer made (another process on the same file, here
    a second engine with no hook) is found by the result and download pages, which count deletions again before they
    show or hand anything: the package is spoiled and released, never handed."""
    from pathlib import Path

    from app.library.db import Database, make_engine
    from app.library.models import AuditEvent, ExperienceItem

    location = build(browser_a)
    rt = app.state.library
    other = Database(make_engine(Path(rt.database.engine.url.database)))

    with other.transaction() as db:
        owner = db.get(ExperienceItem, item).owner_id
        db.add(AuditEvent(owner_id=owner, item_id=item, object_type="experience", object_id=item, action="deleted", at=rt.now()))

    # Still held: nothing of this app saw it written.
    assert len(app.state.export_holds) == 1

    result = browser_a.get(location)

    assert result.status_code == 409 and len(app.state.export_holds) == 0
    assert browser_a.get(f"/data/export/download/{token_of(location)}").status_code == 409
    other.engine.dispose()


def test_another_users_deletion_spoils_nothing_of_mine(app, browser_a, browser_b, item):
    location = build(browser_a)
    theirs = confirmed(browser_b, title="B 的合成经历", actions="合成内容")
    delete_item(browser_b, theirs)

    assert browser_a.get(location).status_code == 200
    assert browser_a.get(f"/data/export/download/{token_of(location)}").status_code == 200


def test_a_build_begun_in_a_session_that_ended_is_never_held(app, browser_a, item):
    holds = app.state.export_holds
    ticket = holds.begin("someone", "a-session")
    holds.end_session("a-session")

    class Package:
        class handle:
            closed = False

            @classmethod
            def close(cls):
                cls.closed = True

    assert holds.put(ticket, Package, 0) is None and Package.handle.closed
    assert holds.begin("someone", "a-session") is None and len(holds) == 0


# --- freed by the app itself (Codex R1 review): past their time, and when a login runs out ---------------------------


def held_package(app, location):
    return app.state.export_holds._held[token_of(location)].package


def closing_seen(package):
    """An event set when the package's file is closed, by whoever closes it."""
    from threading import Event

    closed = Event()
    close = package.handle.close
    package.handle.close = lambda: (closed.set(), close())[1]

    return closed


def expire_login(app, browser, clock):
    """The browser's login, its time run out by the app's clock (nothing else of it changes)."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.library.identity import token_hash
    from app.library.models import LoginSession

    session = token_hash(browser.client.cookies.get("rr_session"))

    with app.state.library.database.transaction() as db:
        row = db.scalar(select(LoginSession).where(LoginSession.token_hash == session))
        row.expires_at = clock.now() + timedelta(seconds=30)

    clock.advance(seconds=31)

    return session


def test_a_package_past_its_time_is_freed_by_the_apps_clean_up_pass_without_another_request(app, browser_a, item):
    holds = app.state.export_holds
    now = [1000.0]
    holds._clock = lambda: now[0]
    location = build(browser_a)
    package = held_package(app, location)
    now[0] += 10 * 60 + 1

    # The pass the app runs on its own schedule (web_main._cleanup_loop), not a page and not holds.sweep().
    app.state.library.run_cleanup()

    assert package.handle.closed and len(holds) == 0
    assert browser_a.get(location).status_code == 404


def test_while_the_app_runs_it_frees_packages_past_their_time_on_its_own(make_config, clock):
    """The sweep the app's lifespan starts (web_main._sweep_loop over the export holds): an event, not a sleep, says
    the package was closed; nobody asks for an export after the build."""
    from fastapi.testclient import TestClient

    from app.web_main import create_app
    from library_helpers import LOCAL_BASE_URL

    app = create_app(make_config(), clock=clock)
    holds = app.state.export_holds
    now = [1000.0]
    holds._clock = lambda: now[0]
    holds.sweep_seconds = 0.01

    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        browser = Browser.signed_in(app, "合成用户 A")
        confirmed(browser, title="导出测试项目（合成）", actions="整理了每周数据。")
        location = build(browser)
        closed = closing_seen(held_package(app, location))
        now[0] += 10 * 60 + 1

        assert closed.wait(10), "the app's own sweep did not free the package"
        assert len(holds) == 0

    assert holds.sweeper is not None and holds.sweeper.done()


def test_a_login_that_runs_out_frees_its_package_through_the_clean_up_loop(make_config, clock):
    """The clean-up loop the app runs (purge_interval_seconds) removes the expired login and tells the export holds, as
    signing out does: the package is closed before its own ten minutes are up."""
    from fastapi.testclient import TestClient

    from app.web_main import create_app
    from library_helpers import LOCAL_BASE_URL

    app = create_app(make_config(purge_interval_seconds=0.01), clock=clock)
    holds = app.state.export_holds
    holds._clock = lambda: 1000.0

    with TestClient(app, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        browser = Browser.signed_in(app, "合成用户 A")
        confirmed(browser, title="导出测试项目（合成）", actions="整理了每周数据。")
        location = build(browser)
        closed = closing_seen(held_package(app, location))
        session = expire_login(app, browser, clock)

        assert closed.wait(10), "the expired login's package stayed open"
        assert len(holds) == 0 and holds.begin("anyone", session) is None


def test_a_build_finishing_after_its_login_ran_out_is_never_held(app, browser_a, item, clock, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app import data_management

    holds = app.state.export_holds
    original = data_management.write_export
    arrived, release, closed = Event(), Event(), []

    def build_then_wait(*args, **kwargs):
        package = original(*args, **kwargs)
        handle_close = package.handle.close
        package.handle.close = lambda: (closed.append(True), handle_close())[1]
        arrived.set()
        assert release.wait(10), "the test did not release the build"

        return package

    monkeypatch.setattr(data_management, "write_export", build_then_wait)
    form = browser_a.form("/data/export", "/data/export/build")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser_a.post, form.action, form.fields)

        try:
            assert arrived.wait(10), "the build did not reach the window"
            # The login runs out and the app's clean-up pass removes it while the build is still writing.
            expire_login(app, browser_a, clock)
            app.state.library.run_cleanup()
        finally:
            release.set()

        response = pending.result(10)

    assert response.status_code == 409 and len(holds) == 0 and closed == [True]


def test_a_login_running_out_frees_that_login_alone_and_a_new_login_builds_again(app, browser_a, browser_b, item, clock):
    mine = build(browser_a)
    confirmed(browser_b, title="B 的合成经历", actions="合成内容")
    theirs = build(browser_b)
    package = held_package(app, mine)

    expire_login(app, browser_a, clock)
    app.state.library.run_cleanup()

    # Mine is closed and gone; theirs, of another login that has not run out, is untouched.
    assert package.handle.closed and token_of(mine) not in app.state.export_holds._held
    assert browser_b.get(theirs).status_code == 200

    # A new login of the same user builds and downloads as before.
    again = Browser.signed_in(app, "合成用户 A")
    location = build(again)

    assert again.get(f"/data/export/download/{token_of(location)}").status_code == 200
