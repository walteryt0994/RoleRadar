"""Fixtures for the experience library tests.

These tests run only inside the isolated launcher (run_isolated.py), which
points every database variable into a temporary root before pytest starts.
Every app built here also gets an explicit config whose database file is
under tmp_path, and the path guard refuses anything outside the test root.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.library.config import TEST_ROOT_ENV, LibraryConfig
from app.library.db import Database, make_engine
from app.library.identity import dev_users, ensure_dev_users
from app.library.models import create_schema
from app.web_main import create_app
from library_helpers import LOCAL_BASE_URL, Browser, ManualClock

START = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def _require_isolated_launcher():
    if not os.environ.get(TEST_ROOT_ENV) or os.environ.get("LIBRARY_AUDIT_ACTIVE") != "1":
        pytest.fail(
            "经历库测试必须通过 tests/library/run_isolated.py 启动："
            "它会在 pytest 启动前隔离新旧数据库并加载审计插件。",
            pytrace=False,
        )


@pytest.fixture
def test_root() -> Path:
    return Path(os.environ[TEST_ROOT_ENV])


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def make_config(tmp_path, test_root):
    def build(**overrides) -> LibraryConfig:
        values = {
            "db_path": tmp_path / "library.db",
            "files_dir": tmp_path / "files",
            "dev_login": True,
            "test_root": test_root,
            "purge_interval_seconds": None,
        }
        values.update(overrides)

        return LibraryConfig(**values)

    return build


@pytest.fixture
def app(make_config, clock):
    application = create_app(make_config(), clock=clock)

    # This client only keeps the app's lifespan running; Browser objects send
    # the requests, each with its own cookies.
    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        yield application


@pytest.fixture
def browser_a(app) -> Browser:
    return Browser.signed_in(app, "合成用户 A")


@pytest.fixture
def browser_b(app) -> Browser:
    return Browser.signed_in(app, "合成用户 B")


@pytest.fixture
def database(tmp_path, clock):
    engine = make_engine(tmp_path / "service.db")
    create_schema(engine)
    db = Database(engine)

    with db.transaction() as session:
        ensure_dev_users(session, clock.now())

    yield db
    engine.dispose()


@pytest.fixture
def owners(database) -> tuple[str, str]:
    with database.transaction() as session:
        first, second = dev_users(session)

    return first.id, second.id
