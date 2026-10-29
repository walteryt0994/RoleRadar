"""The library is kept apart from the legacy app and from real data.

Nothing here opens, reads or hashes the real database, the records directory
or .env. Unsafe paths are built inside the temporary test root, and the guard
is shown to refuse them before anything is created.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.library.config import (
    BACKEND_DIR,
    LibraryConfig,
    check_storage_path,
    legacy_database_paths,
    load_config_from_env,
)
from app.library.errors import ConfigError, UnsafeStoragePath
from app.web_main import check_bind_host, create_app, main
from library_helpers import LOCAL_BASE_URL, Browser, all_routes

LEGACY_MODULES = (
    "app.database",
    "app.main",
    "app.models",
    "app.routers",
    "app.services",
    "app.schemas",
)


def test_launcher_points_every_database_variable_into_the_test_root(test_root):
    assert os.environ["DATABASE_URL"] == f"sqlite:///{test_root / 'legacy_default.db'}"
    assert Path(os.environ["ROLERADAR_LIBRARY_DB_PATH"]).parent == test_root
    assert Path(tempfile.gettempdir()).resolve().is_relative_to(test_root.resolve())
    assert not any(name.startswith("OPENAI_") for name in os.environ)


def test_dns_lookups_are_blocked_inside_the_isolated_run():
    with pytest.raises(OSError, match="blocked by the isolation audit"):
        socket.getaddrinfo("isolation-probe.invalid", 443)


def test_importing_and_building_the_app_never_loads_legacy_modules(tmp_path, test_root):
    script = "\n".join(
        [
            "import json, sys",
            "from pathlib import Path",
            "import app.web_main",
            f"legacy = {LEGACY_MODULES!r}",
            "after_import = [name for name in legacy if name in sys.modules]",
            "from app.library.config import LibraryConfig",
            f"config = LibraryConfig(db_path=Path({str(tmp_path / 'child.db')!r}), dev_login=True,",
            f"    test_root=Path({str(test_root)!r}), purge_interval_seconds=None)",
            "app.web_main.create_app(config)",
            "after_create = [name for name in legacy if name in sys.modules]",
            "print(json.dumps([after_import, after_create]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=BACKEND_DIR,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [[], []]
    assert (tmp_path / "child.db").exists()


@pytest.fixture
def fake_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    (repo / "backend" / "records").mkdir(parents=True)

    return repo


def guard(path, repo: Path, *, legacy=(), records=None, test_root=None) -> Path:
    return check_storage_path(
        path,
        test_root=test_root,
        repo_root=repo,
        records_dir=records or repo / "backend" / "records",
        legacy_paths=list(legacy),
    )


def test_guard_accepts_a_new_file_outside_the_repository(tmp_path, fake_repo):
    assert guard(tmp_path / "new.db", fake_repo) == (tmp_path / "new.db").resolve()


def test_guard_rejects_relative_paths(fake_repo):
    with pytest.raises(UnsafeStoragePath, match="绝对路径"):
        guard("library.db", fake_repo)


def test_guard_rejects_paths_inside_the_repository(fake_repo):
    with pytest.raises(UnsafeStoragePath, match="代码仓库"):
        guard(fake_repo / "backend" / "library.db", fake_repo)


def test_guard_rejects_the_records_directory(tmp_path, fake_repo):
    records = tmp_path / "records"
    records.mkdir()

    with pytest.raises(UnsafeStoragePath, match="records"):
        guard(records / "library.db", fake_repo, records=records)


def test_guard_rejects_the_legacy_database_file(tmp_path, fake_repo):
    legacy = tmp_path / "legacy.db"

    with pytest.raises(UnsafeStoragePath, match="同一个文件"):
        guard(legacy, fake_repo, legacy=[legacy])


def test_guard_follows_symlinks_to_the_legacy_database(tmp_path, fake_repo):
    legacy = tmp_path / "legacy.db"
    legacy.write_bytes(b"")
    link = tmp_path / "innocent-looking.db"
    link.symlink_to(legacy)

    with pytest.raises(UnsafeStoragePath, match="同一个文件"):
        guard(link, fake_repo, legacy=[legacy])


def test_guard_detects_hard_links_to_the_legacy_database(tmp_path, fake_repo):
    legacy = tmp_path / "legacy.db"
    legacy.write_bytes(b"")
    hard_link = tmp_path / "copy.db"
    os.link(legacy, hard_link)

    with pytest.raises(UnsafeStoragePath, match="同一个文件"):
        guard(hard_link, fake_repo, legacy=[legacy])


def test_guard_rejects_directories(tmp_path, fake_repo):
    with pytest.raises(UnsafeStoragePath, match="目录"):
        guard(tmp_path, fake_repo)


def test_guard_rejects_paths_outside_the_test_root(tmp_path, fake_repo):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(UnsafeStoragePath, match="测试临时目录"):
        guard(tmp_path / "elsewhere.db", fake_repo, test_root=root)


def test_known_legacy_locations_are_derived_without_touching_files(tmp_path):
    paths = legacy_database_paths(
        environ={"DATABASE_URL": "sqlite:///./data/old.db"},
        cwd=tmp_path,
    )

    assert BACKEND_DIR / "roleradar.db" in paths
    assert BACKEND_DIR.parent / "roleradar.db" in paths
    assert tmp_path / "roleradar.db" in paths
    assert tmp_path / "data" / "old.db" in paths


def test_an_unreadable_legacy_url_fails_closed(tmp_path):
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        legacy_database_paths(environ={"DATABASE_URL": "not a database url"}, cwd=tmp_path)


def test_create_app_refuses_the_legacy_database_and_creates_nothing(tmp_path, test_root, monkeypatch):
    legacy = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{legacy}")
    config = LibraryConfig(
        db_path=legacy,
        dev_login=True,
        test_root=test_root,
        purge_interval_seconds=None,
    )

    with pytest.raises(UnsafeStoragePath):
        create_app(config)

    assert not legacy.exists()


def test_the_library_has_no_default_database_path():
    with pytest.raises(ConfigError, match="明确指定"):
        load_config_from_env(environ={})


def test_a_database_that_belongs_to_another_program_is_refused(tmp_path, make_config):
    foreign = tmp_path / "foreign.db"

    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY)")

    with pytest.raises(ConfigError, match="其他程序"):
        create_app(make_config(db_path=foreign))


def test_the_controlled_launcher_only_binds_to_loopback():
    check_bind_host("127.0.0.1")

    for host in ("0.0.0.0", "::", "192.168.1.20", "localhost", "::1"):
        with pytest.raises(ConfigError):
            check_bind_host(host)


def test_the_launcher_refuses_a_public_address_before_creating_anything(tmp_path, capsys):
    target = tmp_path / "never-created.db"

    with pytest.raises(SystemExit) as exit_info:
        main(["--db", str(target), "--dev-login", "--host", "0.0.0.0"])

    assert exit_info.value.code == 2
    assert "127.0.0.1" in capsys.readouterr().err
    assert not target.exists()


def test_the_dev_clock_offset_needs_dev_login(tmp_path):
    with pytest.raises(ConfigError, match="开发登录"):
        LibraryConfig(db_path=tmp_path / "x.db", dev_clock_offset=timedelta(days=8))


def test_the_library_app_mounts_no_legacy_routes(app):
    paths = {getattr(route, "path", None) for route in all_routes(app)}
    legacy_paths = (
        "/profile",
        "/profile/confirmation",
        "/applications",
        "/applications/{application_id}",
        "/resumes/text",
        "/resumes/pdf",
        "/parse-jd",
        "/analyze-job",
        "/docs",
        "/redoc",
        "/openapi.json",
    )

    assert not paths.intersection(legacy_paths)

    visitor = Browser(app)

    for path in ("/profile", "/applications", "/docs", "/openapi.json"):
        assert visitor.get(path).status_code == 404


def test_requests_from_other_machines_are_refused(app):
    outsider = Browser(app, client_host="192.168.1.20")

    assert outsider.get("/login").status_code == 403
    assert outsider.get("/health").status_code == 403


def test_forwarding_headers_cannot_make_a_remote_client_look_local(app):
    outsider = Browser(app, client_host="203.0.113.9")
    response = outsider.get(
        "/login",
        headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1"},
    )

    assert response.status_code == 403


def test_unexpected_host_names_are_refused(app):
    response = Browser(app).get("/login", headers={"Host": "attacker.example"})

    assert response.status_code == 400


def test_dev_login_is_off_unless_explicitly_enabled(make_config, clock):
    application = create_app(make_config(dev_login=False), clock=clock)

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)) as client:
        page = client.get("/login")
        attempt = client.post("/login/dev", data={"user_id": "anyone"})

    assert "真实登录还没有建设" in page.text
    assert "/login/dev" not in page.text
    assert attempt.status_code == 404
