"""Independent R1 boundaries. Synthetic data only, no sleeps or product edits."""
import sys
from datetime import timedelta

sys.path.insert(0, '/Users/hug0_/roleradar/backend/tests/library')
from conftest import *  # noqa
from sqlalchemy import select
from app.library.models import LoginSession
from job_helpers import InlineExecutor, confirmed
from test_ui_data_management import build, token_of
from test_ui_job_connections import analysed, save, kept_analyses, task_in, POSTING_TEXT
from test_job_supplied_pages import paste, token_of as supplied_token


def test_expired_export_is_released_by_normal_cleanup_without_another_export_request(app, browser_a):
    confirmed(browser_a, title='Synthetic expiry probe', body='Synthetic data')
    holds = app.state.export_holds
    now = [1000.0]
    holds._clock = lambda: now[0]
    token = token_of(build(browser_a))
    package = holds._held[token].package
    now[0] += 601
    # Exercise the existing scheduled-cleanup entry points, not holds.sweep() directly:
    # merely proving an unwired helper works would not show runtime resource cleanup.
    app.state.library.run_cleanup()
    app.state.job_analysis.sweep()
    app.state.discovery.sweep()
    app.state.directions.sweep()
    assert package.handle.closed and len(holds) == 0, 'expired export survives every normal cleanup path until another export request or shutdown'


def test_naturally_expired_session_releases_export_before_package_ttl(app, browser_a, clock):
    confirmed(browser_a, title='Synthetic session expiry probe', body='Synthetic data')
    holds = app.state.export_holds
    now = [1000.0]
    holds._clock = lambda: now[0]
    token = token_of(build(browser_a))
    held = holds._held[token]
    with app.state.library.database.transaction() as db:
        row = db.scalar(select(LoginSession).where(LoginSession.token_hash == held.session))
        row.expires_at = clock.now() + timedelta(seconds=30)
    clock.advance(seconds=31)
    now[0] += 31
    app.state.library.run_cleanup()
    with app.state.library.database.transaction() as db:
        assert db.scalar(select(LoginSession).where(LoginSession.token_hash == held.session)) is None
    assert held.package.handle.closed and len(holds) == 0, 'expired login removed from DB but its export package remains open'


def test_same_text_supplied_twice_never_shares_an_analysis_identity(app, browser_a):
    app.state.job_analysis.executor = InlineExecutor()
    item = confirmed(browser_a, title='Synthetic identity control', body='Python and SQL project')
    first = supplied_token(paste(browser_a, text=POSTING_TEXT))
    second = supplied_token(paste(browser_a, text=POSTING_TEXT))
    a = analysed(browser_a, [item], origin=f'supplied:{first}')
    b = analysed(browser_a, [item], origin=f'supplied:{second}')
    assert task_in(app, a).origin.posting_id != task_in(app, b).origin.posting_id
    assert save(browser_a, a).status_code == 303
    assert save(browser_a, b).status_code == 303
    rows = kept_analyses(app)
    assert len(rows) == 2 and len({row.posting_id for row in rows}) == 2
