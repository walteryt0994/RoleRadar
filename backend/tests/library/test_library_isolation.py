"""The library is kept apart from the legacy app and from real data.

Nothing here opens, reads or hashes the real database, the records directory
or .env. Unsafe paths are built inside the temporary test root, and the guard
is shown to refuse them before anything is created.
"""
import ast
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
)
# Since U2-A2 the app also mounts the job analysis, which uses the shared job
# description contracts in app.schemas (data models only, no database) and
# the provider code. The library package itself still loads none of them.
SHARED_CONTRACTS = ("app.schemas", "app.experience_analysis", "app.job_analysis", "openai")
# The search tools (U3-A1) start no task and enter no gate of their own, have no page and reach no
# network: they load none of these modules, and their own code imports no network client. The Lever adapter
# (U3-D2, offline) is one of them: its boards' transport sends nothing unless another is passed in, and the one
# that does (app.discovery.lever_http) is loaded by none of them.
SEARCH_TOOLS = ("contracts", "rules", "sources", "synthetic", "tools", "lever")
NOT_FOR_SEARCH_TOOLS = LEGACY_MODULES + (
    # Kept directions reach a search through its pages only (U3-C3b): the tools know nothing of them.
    "app.directions",
    "app.job_analysis.runner",
    "app.job_analysis.tasks",
    "app.job_analysis.service",
    "app.job_analysis.live",
    "app.job_analysis.web",
    "app.web_main",
    "app.discovery.lever_http",
    # What the user keeps from a search (U3-D3a) is never read by a search: a skip changes no later one.
    "app.discovery.records",
)
# The one transport that reaches Lever (U3-D2): only a separately approved one-off validation may load it.
SENDING_TRANSPORT = "app.discovery.lever_http"
NETWORK_CLIENTS = {"socket", "ssl", "http", "urllib", "requests", "httpx", "httpx2", "aiohttp", "openai"}
# The search agent (U3-A2): its loop, the fixed flow and its task store start no analysis task and have no page;
# only its runner gets a service, through app.job_analysis.service. None of its own code imports a network client.
AGENT_MODULES = ("agent", "baseline", "tasks")
NOT_FOR_THE_AGENT = LEGACY_MODULES + (
    "app.directions",
    "app.job_analysis.runner",
    "app.job_analysis.tasks",
    "app.job_analysis.web",
    "app.web_main",
    "app.discovery.lever_http",
    "app.discovery.records",
)
# What the user keeps from a finished search (U3-D3a): records over the library and the search's contracts. They load
# no search tool, agent, task store, runner, page, gate or service, none of their own code imports a network client,
# and the library package itself stays free of the search (the choices and the export live in it, the writes do not).
JOB_RECORD_MODULES = ("app.discovery.records", "app.library.job_choices", "app.library.job_export")
NOT_FOR_JOB_RECORDS = LEGACY_MODULES + (
    "app.discovery.tools",
    "app.discovery.agent",
    "app.discovery.baseline",
    "app.discovery.tasks",
    "app.discovery.runner",
    "app.discovery.web",
    "app.discovery.lever_http",
    "app.discovery.lever_live",
    "app.discovery.model_live",
    "app.job_analysis",
    "app.live_gate",
    "app.web_main",
    "app.directions",
)
# The direction records (U3-C1) are contracts and internal entries over the library: no analysis, search, gate,
# service client or page, and none of their own code imports a network client. The hand-off to a search (U3-C3b)
# only reads them: it loads no search, page or gate either.
DIRECTION_MODULES = ("contracts", "records", "handoff")
NOT_FOR_DIRECTIONS = LEGACY_MODULES + (
    "app.job_analysis",
    "app.discovery",
    "app.live_gate",
    "app.ai_provider",
    "app.experience_analysis",
    "app.web_main",
    "openai",
    "app.directions.explore",
    "app.directions.simulation",
    "app.directions.live_cases",
    "app.directions.live_check",
)
# The direction workflow (U3-C2) and its simulated answers: one request to a provider it is handed, never a gate,
# a search, a task store, a page or a saving entry of its own. It reuses U2's per-field plans and quote reading,
# so the provider types and the SDK they import come along; none of its own code imports a network client.
DIRECTION_WORKFLOW = ("explore", "simulation", "attribution")
NOT_FOR_THE_DIRECTION_WORKFLOW = LEGACY_MODULES + (
    "app.live_gate",
    "app.discovery",
    "app.job_analysis.runner",
    "app.job_analysis.tasks",
    "app.job_analysis.service",
    "app.job_analysis.live",
    "app.job_analysis.web",
    "app.web_main",
    "app.directions.records",
    "app.directions.live_cases",
    "app.directions.live_check",
)
# Behind the direction pages (U3-C3a): the task store, its runner, the checked writes and the words. They build no
# service and enter no gate: the pages hand the runner its provider (app.directions.web: the local simulation, or the
# app's one real service behind its gate, what the gate says turned into the runner's own exceptions). They load no
# search, no other task store or page, and none of their own code imports a network client.
DIRECTION_PAGE_PARTS = ("tasks", "runner", "actions", "presentation")
NOT_FOR_THE_DIRECTION_PAGE_PARTS = LEGACY_MODULES + (
    "app.live_gate",
    "app.discovery",
    "app.job_analysis.runner",
    "app.job_analysis.tasks",
    "app.job_analysis.service",
    "app.job_analysis.live",
    "app.job_analysis.web",
    "app.web_main",
    "app.directions.web",
    "app.directions.live_cases",
    "app.directions.live_check",
)
# The validation command of the direction workflow (U3-C4-0) and its frozen cases: the workflow, the gate and the
# approval's parser, never a page, a search or the pages' task store; none of their own code imports a network
# client, and nothing of the app imports them.
DIRECTION_CHECK = ("live_cases", "live_check")
NOT_FOR_THE_DIRECTION_CHECK = LEGACY_MODULES + (
    "app.discovery",
    "app.web_main",
    "app.directions.web",
    "app.directions.tasks",
    "app.directions.runner",
    "app.directions.actions",
    "app.job_analysis.web",
    "app.job_analysis.runner",
    "app.job_analysis.tasks",
)


def test_launcher_points_every_database_variable_into_the_test_root(test_root):
    assert os.environ["DATABASE_URL"] == f"sqlite:///{test_root / 'legacy_default.db'}"
    assert Path(os.environ["ROLERADAR_LIBRARY_DB_PATH"]).parent == test_root
    assert Path(tempfile.gettempdir()).resolve().is_relative_to(test_root.resolve())
    assert not any(name.startswith(("OPENAI_", "ROLERADAR_SOURCE_", "ROLERADAR_BATCH_")) for name in os.environ)
    assert "ROLERADAR_DISCOVERY_DEMO" not in os.environ
    # U3-D2-E3: nor the choice of validation B's real model.
    assert "ROLERADAR_DISCOVERY_MODEL" not in os.environ


def test_a_parent_started_for_a_d6_launch_hands_none_of_its_batch_to_an_isolated_run(tmp_path, monkeypatch):
    """U3-D6-0 R1: the launch, its run directory, run date, admission, approvals, board and save permission stay with
    the parent: an app built in the isolated run never loads the parent's batch."""
    from app.discovery import batch_live
    from run_isolated import isolated_environment

    settings = {
        batch_live.LAUNCH_VARIABLE: batch_live.Launch.DISCOVERY.value,
        batch_live.SAVE_VARIABLE: "facts",
        **{name: "made-up" for name in (*batch_live.COMMON_VARIABLES.values(), *batch_live.SOURCE_VARIABLES.values())},
    }

    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    root = tmp_path / "child"
    root.mkdir()
    isolated = isolated_environment(root)

    assert all(name.startswith("ROLERADAR_BATCH_") for name in settings)
    assert not set(settings) & set(isolated)


def test_a_parent_choosing_validation_b_hands_the_choice_to_no_isolated_run(tmp_path, monkeypatch):
    from app.discovery import model_live
    from run_isolated import isolated_environment

    monkeypatch.setenv(model_live.VARIABLE, model_live.LIVE)
    root = tmp_path / "child"
    root.mkdir()

    assert model_live.VARIABLE not in isolated_environment(root)


def _snapshot(folder: Path) -> list:
    return sorted((str(path.relative_to(folder)), path.read_bytes() if path.is_file() else None) for path in folder.rglob("*"))


def test_a_parent_started_for_the_real_source_hands_none_of_it_to_an_isolated_run(tmp_path, monkeypatch):
    """U3-D2-E1: the settings that start the app for the real source stay with the parent. An app built in the
    isolated run starts on the synthetic set, and the parent's approvals (synthetic, in folders outside the run's
    root) are left exactly as they were."""
    from app.discovery import lever_live
    from app.discovery.web import DEMO_VARIABLE
    from run_isolated import isolated_environment

    today = lever_live.today_here()
    parent = tmp_path / "parent"
    run_dir = parent / "run"
    used = parent / "home" / ".roleradar" / "source-approvals" / f"u3d2e-src-{today}-z.claim"
    run_dir.mkdir(parents=True)
    used.parent.mkdir(parents=True)
    used.write_text(json.dumps({"approval_id": used.stem, "task_id": "an-earlier-search"}), encoding="utf-8")
    settings = {
        "HOME": str(parent / "home"),
        DEMO_VARIABLE: lever_live.KEY,
        "ROLERADAR_SOURCE_ADMISSION_ID": f"u3d2e-admit-{today}-a",
        "ROLERADAR_SOURCE_ADMISSION_UNTIL": str(today + timedelta(days=29)),
        "ROLERADAR_SOURCE_APPROVAL_ID": f"u3d2e-src-{today}-a",
        "ROLERADAR_SOURCE_RUN_DATE": str(today),
        "ROLERADAR_SOURCE_BOARD": "palantir",
        "ROLERADAR_SOURCE_MAX_REQUESTS": "12",
        "ROLERADAR_SOURCE_WINDOW_SECONDS": "600",
        "ROLERADAR_SOURCE_RUN_DIR": str(run_dir),
    }
    # Every setting the real source needs, and right: the parent itself would start for it.
    assert set(settings) == {"HOME", DEMO_VARIABLE, *lever_live.VARIABLES.values()}
    assert lever_live.load_terms(settings, today=today).approval_id == f"u3d2e-src-{today}-a"

    for name, value in settings.items():
        monkeypatch.setenv(name, value)

    before = _snapshot(parent)
    root = tmp_path / "child"
    root.mkdir()
    isolated = isolated_environment(root)
    script = "\n".join(
        [
            "import json",
            "from pathlib import Path",
            "from app.library.config import LibraryConfig",
            "from app.web_main import create_app",
            f"root = Path({str(root)!r})",
            "config = LibraryConfig(db_path=root / 'child.db', dev_login=True, files_dir=root / 'child-files',",
            "    test_root=root, purge_interval_seconds=None)",
            "print(json.dumps(create_app(config).state.discovery.sources.key))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=isolated, capture_output=True, text=True, timeout=60
    )

    assert not ({DEMO_VARIABLE, *lever_live.VARIABLES.values()} & isolated.keys())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == "synthetic"
    assert _snapshot(parent) == before


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
            f"    files_dir=Path({str(tmp_path / 'child-files')!r}),",
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


def test_the_library_package_itself_loads_no_analysis_or_legacy_code():
    script = "\n".join(
        [
            "import importlib, json, pkgutil, sys",
            "import app.library",
            "for module in pkgutil.iter_modules(app.library.__path__):",
            "    importlib.import_module(f'app.library.{module.name}')",
            f"names = {LEGACY_MODULES + SHARED_CONTRACTS!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_the_search_tools_load_no_task_runner_gate_entry_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {SEARCH_TOOLS!r}:",
            "    importlib.import_module(f'app.discovery.{name}')",
            f"names = {NOT_FOR_SEARCH_TOOLS!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in SEARCH_TOOLS:
        tree = ast.parse((BACKEND_DIR / "app" / "discovery" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


def test_the_kept_records_load_no_search_tool_agent_page_gate_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {JOB_RECORD_MODULES!r}:",
            "    importlib.import_module(name)",
            f"names = {NOT_FOR_JOB_RECORDS!r}",
            "print(json.dumps([name for name in names if any(module == name or module.startswith(name + '.') for module in sys.modules)]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in JOB_RECORD_MODULES:
        tree = ast.parse((BACKEND_DIR / Path(*name.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


# Checks of a kept posting from its record (U3-D4a): they read public postings through a source's adapter and compare
# public facts. They load no analysis or experience workflow, no gate, search tool, agent, task store, runner, page or
# keeping entry, and none of their own code imports a network client or a model's provider. (The search's contracts,
# shared with the kept records, load the provider's definitions through app.analysis_contracts; nothing here calls it.)
UPDATE_MODULES = ("app.discovery.updates",)
NOT_FOR_UPDATES = NOT_FOR_JOB_RECORDS + (
    "app.experience_analysis",
    "app.job_analysis",
    "app.discovery.records",
    "app.discovery.synthetic",
    "app.discovery.lever",
)
NOT_IMPORTED_BY_UPDATES = {"app.ai_provider", "app.analysis_contracts", "app.experience_analysis", "app.live_gate", "openai"}


def test_the_checks_of_a_kept_posting_load_no_model_analysis_gate_search_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {UPDATE_MODULES!r}:",
            "    importlib.import_module(name)",
            f"names = {NOT_FOR_UPDATES!r}",
            "print(json.dumps([name for name in names if any(module == name or module.startswith(name + '.') for module in sys.modules)]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in UPDATE_MODULES:
        tree = ast.parse((BACKEND_DIR / Path(*name.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not {name.split(".")[0] for name in imported} & NETWORK_CLIENTS
    assert not {name for name in imported if any(name == banned or name.startswith(banned + ".") for banned in NOT_IMPORTED_BY_UPDATES)}
    assert not {name for name in imported if name.startswith("app.job_analysis") or name.startswith("app.directions")}


# 每日查新 (U3-D4c): a fixed flow over a kept summary's searches. Like the checks of a kept posting it loads no model,
# analysis or experience workflow, gate, search tool, agent, runner, page or keeping entry, and none of its own code
# imports a network client, a model's provider, the search tools or the records (which read the preferences); the
# shared checks of a search's words (app.discovery.conditions) are held to the same.
WATCH_MODULES = ("app.discovery.watches", "app.discovery.conditions", "app.library.job_watches")
NOT_FOR_WATCHES = NOT_FOR_UPDATES
NOT_IMPORTED_BY_WATCHES = NOT_IMPORTED_BY_UPDATES | {
    "app.discovery.tools",
    "app.discovery.records",
    "app.discovery.agent",
    "app.discovery.runner",
    "app.library.projection",
}


def test_daily_new_postings_load_no_model_analysis_gate_search_tool_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {WATCH_MODULES!r}:",
            "    importlib.import_module(name)",
            f"names = {NOT_FOR_WATCHES!r}",
            "print(json.dumps([name for name in names if any(module == name or module.startswith(name + '.') for module in sys.modules)]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in WATCH_MODULES:
        tree = ast.parse((BACKEND_DIR / Path(*name.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not {name.split(".")[0] for name in imported} & NETWORK_CLIENTS
    assert not {name for name in imported if any(name == banned or name.startswith(banned + ".") for banned in NOT_IMPORTED_BY_WATCHES)}
    assert not {name for name in imported if name.startswith("app.job_analysis") or name.startswith("app.directions")}



# 补充岗位 (U3-D4d): matching a link, reading it as D4a does, and holding what was read or pasted. It loads no model,
# analysis or experience workflow, gate, search tool, agent, runner, page or keeping entry, and none of its own code
# imports a model's provider, the search tools or the records. Of urllib it imports the parsing of an address alone
# (urllib.parse), never anything that opens one: the address the user gives is only ever looked at as text.
SUPPLY_MODULES = ("app.discovery.supplied", "app.library.job_texts")
NOT_FOR_SUPPLY = NOT_FOR_JOB_RECORDS + ("app.experience_analysis", "app.job_analysis", "app.discovery.records")
NOT_IMPORTED_BY_SUPPLY = NOT_IMPORTED_BY_WATCHES


def test_supplied_postings_load_no_model_analysis_gate_search_tool_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {SUPPLY_MODULES!r}:",
            "    importlib.import_module(name)",
            f"names = {NOT_FOR_SUPPLY!r}",
            "print(json.dumps([name for name in names if any(module == name or module.startswith(name + '.') for module in sys.modules)]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in SUPPLY_MODULES:
        tree = ast.parse((BACKEND_DIR / Path(*name.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert {name for name in imported if name.split(".")[0] in NETWORK_CLIENTS} == {"urllib.parse"}
    assert not {name for name in imported if any(name == banned or name.startswith(banned + ".") for banned in NOT_IMPORTED_BY_SUPPLY)}
    assert not {name for name in imported if name.startswith("app.job_analysis") or name.startswith("app.directions")}


def test_the_search_agent_starts_no_analysis_task_and_has_no_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {AGENT_MODULES!r}:",
            "    importlib.import_module(f'app.discovery.{name}')",
            f"names = {NOT_FOR_THE_AGENT!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in (*AGENT_MODULES, "runner"):
        tree = ast.parse((BACKEND_DIR / "app" / "discovery" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


def test_nothing_in_the_product_loads_the_transport_that_reaches_lever():
    """U3-D2: the app, as it starts, does not load app.discovery.lever_http, and no module of the app imports it, but
    the controlled real source (D2-E1), which may only inside a function, when a request is about to leave."""
    script = "\n".join(
        [
            "import json, sys",
            "import app.web_main",
            "import app.discovery.lever_live",
            f"print(json.dumps({SENDING_TRANSPORT!r} in sys.modules))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is False

    importing = []

    for path in sorted((BACKEND_DIR / "app").rglob("*.py")):
        if path.name == "lever_http.py":
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Imports inside a function of the controlled real source run only when called: those, and only those, may.
        inside = {
            id(node)
            for function in ast.walk(tree)
            if path.name == "lever_live.py" and isinstance(function, ast.FunctionDef)
            for node in ast.walk(function)
        }

        for node in ast.walk(tree):
            if id(node) in inside:
                continue

            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            else:
                continue

            if SENDING_TRANSPORT in names:
                importing.append(str(path.relative_to(BACKEND_DIR)))

    assert importing == []


def test_the_direction_records_load_no_analysis_search_gate_page_or_network_client():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {DIRECTION_MODULES!r}:",
            "    importlib.import_module(f'app.directions.{name}')",
            f"names = {NOT_FOR_DIRECTIONS!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in DIRECTION_MODULES:
        tree = ast.parse((BACKEND_DIR / "app" / "directions" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


def test_the_direction_workflow_loads_no_gate_search_task_page_or_saving_entry():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {DIRECTION_WORKFLOW!r}:",
            "    importlib.import_module(f'app.directions.{name}')",
            f"names = {NOT_FOR_THE_DIRECTION_WORKFLOW!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in DIRECTION_WORKFLOW:
        tree = ast.parse((BACKEND_DIR / "app" / "directions" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


def test_behind_the_direction_pages_nothing_builds_a_service_or_enters_a_gate():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {DIRECTION_PAGE_PARTS!r}:",
            "    importlib.import_module(f'app.directions.{name}')",
            f"names = {NOT_FOR_THE_DIRECTION_PAGE_PARTS!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in DIRECTION_PAGE_PARTS + ("web",):
        tree = ast.parse((BACKEND_DIR / "app" / "directions" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS


def test_the_direction_check_loads_no_page_search_or_task_store_and_nothing_of_the_app_loads_it():
    script = "\n".join(
        [
            "import importlib, json, sys",
            f"for name in {DIRECTION_CHECK!r}:",
            "    importlib.import_module(f'app.directions.{name}')",
            f"names = {NOT_FOR_THE_DIRECTION_CHECK!r}",
            "print(json.dumps([name for name in names if name in sys.modules]))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []

    imported = set()

    for name in DIRECTION_CHECK:
        tree = ast.parse((BACKEND_DIR / "app" / "directions" / f"{name}.py").read_text(encoding="utf-8"))
        imported |= {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not imported & NETWORK_CLIENTS

    # The pages, the app and the workflow never import the command or its cases (U3-C pre-check 22.3).
    for path in ("directions/web.py", "directions/explore.py", "directions/runner.py", "directions/tasks.py", "web_main.py"):
        tree = ast.parse((BACKEND_DIR / "app" / path).read_text(encoding="utf-8"))
        modules = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        modules |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        modules |= {f"{node.module}.{alias.name}" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module for alias in node.names}

        assert not {module for module in modules if "live_check" in module or "live_cases" in module}, path


def test_the_simulated_service_loads_the_direction_rules_only_when_asked_for_them():
    """The job analysis's simulation answers direction explorations too, importing their rules lazily."""
    script = "\n".join(
        [
            "import json, sys",
            "import app.job_analysis.simulation",
            "print(json.dumps(sorted(name for name in sys.modules if name.startswith('app.directions'))))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_building_the_app_builds_no_service_client(tmp_path, test_root):
    script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "import openai",
            "def refuse(*args, **kwargs):",
            "    raise AssertionError('a service client was built')",
            "openai.OpenAI.__init__ = refuse",
            "import app.web_main",
            "from app.library.config import LibraryConfig",
            f"config = LibraryConfig(db_path=Path({str(tmp_path / 'child.db')!r}), dev_login=True,",
            f"    files_dir=Path({str(tmp_path / 'child-files')!r}),",
            f"    test_root=Path({str(test_root)!r}), purge_interval_seconds=None)",
            "built = app.web_main.create_app(config)",
            "print('app.job_analysis.web' in sys.modules, built.state.job_analysis is not None)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=BACKEND_DIR, env=os.environ.copy(), capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "True"]


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
