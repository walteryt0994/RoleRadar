"""Independent synthetic-only UI acceptance probes. Do not edit product files."""
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

sys.path.insert(0, '/Users/hug0_/roleradar/backend/tests/library')
from conftest import *  # noqa
from app import data_management
from job_helpers import InlineExecutor, confirmed, text_of
from library_helpers import forms_in
from test_ui_data_management import build, token_of
from test_ui_job_connections import analysed, save, kept_analyses, POSTING_TEXT
from test_job_supplied_pages import paste, keep, record_of, token_of as supply_token
from test_discovery_pages import PREFERENCES, PROJECT, confirm, review, search_of


def delete_item(browser, item):
    form = browser.form(f'/experiences/{item}/delete', f'/experiences/{item}/delete')
    assert browser.post(form.action, form.fields).status_code == 303


def test_export_delete_between_last_check_and_take_cannot_download_old_package(app, browser_a, monkeypatch):
    item = confirmed(browser_a, title='DELETE WINDOW SYNTHETIC', body='Synthetic private data')
    token = token_of(build(browser_a))
    arrived, release = Event(), Event()
    original = app.state.export_holds.take

    def held_take(*args, **kwargs):
        arrived.set()
        assert release.wait(15), 'review did not release the download'
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.export_holds, 'take', held_take)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser_a.get, f'/data/export/download/{token}')
        try:
            assert arrived.wait(15), 'download did not reach the intended window'
            delete_item(browser_a, item)
        finally:
            release.set()
        response = pending.result(15)
    assert response.status_code in (404, 409), f'old package was downloadable after delete: {response.status_code}'


def test_export_result_is_invalid_after_delete(app, browser_a):
    item = confirmed(browser_a, title='RESULT INVALIDATION SYNTHETIC', body='Synthetic text')
    location = build(browser_a)
    delete_item(browser_a, item)
    assert browser_a.get(location).status_code in (404, 409), 'deleted content still has a valid export result'


def test_export_finishing_after_logout_does_not_refill_hold(app, browser_a, monkeypatch):
    confirmed(browser_a, title='LOGOUT WINDOW SYNTHETIC', body='Synthetic text')
    original = data_management.write_export
    arrived, release = Event(), Event()

    def hold_after_build(*args, **kwargs):
        package = original(*args, **kwargs)
        arrived.set()
        assert release.wait(15), 'review did not release the build'
        return package

    monkeypatch.setattr(data_management, 'write_export', hold_after_build)
    form = browser_a.form('/data/export', '/data/export/build')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(browser_a.post, form.action, form.fields)
        try:
            assert arrived.wait(15), 'build did not reach the intended window'
            logout = browser_a.form('/data/export', '/logout')
            assert browser_a.post(logout.action, logout.fields).status_code == 303
        finally:
            release.set()
        response = pending.result(15)
    assert len(app.state.export_holds) == 0, f'late export refilled ended session; response {response.status_code}'


def test_analyse_before_keep_links_analysis_when_same_supplied_posting_is_kept(app, browser_a):
    app.state.job_analysis.executor = InlineExecutor()
    item = confirmed(browser_a, title='Synthetic sales report', body='I used SQL and Python to build weekly sales reports.')
    token = supply_token(paste(browser_a, text=POSTING_TEXT))
    task = analysed(browser_a, [item], origin=f'supplied:{token}')
    assert save(browser_a, task).status_code == 303
    analysis = kept_analyses(app)[0]
    record = record_of(keep(browser_a, token))
    page = browser_a.get(f'/job-records/postings/{record}').text
    assert f'/job-records/analyses/{analysis.id}' in page, 'analysis made before keeping is orphaned from the same supplied posting'


def test_english_system_failure_reason_not_left_as_chinese(app, browser_a):
    app.state.discovery.executor = InlineExecutor()
    assert browser_a.submit('/job-preferences/edit', '/job-preferences', **PREFERENCES).status_code == 303
    item = confirmed(browser_a, **PROJECT)
    assert browser_a.post('/language', {'lang': 'en', 'next': '/workspace'}).status_code == 303
    task = search_of(review(browser_a, sources=[item], use_preferences=True, result_language='en'))
    assert confirm(browser_a, task).status_code == 303
    text = text_of(browser_a.get(task).text)
    assert '分析次数已用完' not in text, 'system failure reason remains Chinese (not user/source text)'
