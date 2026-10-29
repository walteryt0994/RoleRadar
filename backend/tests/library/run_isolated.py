"""Run pytest in an isolated child process (RoleRadar test launcher, U1-A).

Isolation is set up *before* pytest starts, so it is already in force while
pytest collects tests and while any application module is first imported:

- a new, empty temporary root outside the repository;
- DATABASE_URL (legacy app) and ROLERADAR_LIBRARY_DB_PATH (library) both
  point into that root, and so do TMPDIR and pytest's --basetemp;
- OPENAI_* and proxy variables are removed; plugin auto-loading is off (so
  pytest-cov cannot rewrite backend/.coverage); no .pyc files or
  .pytest_cache are written;
- an audit plugin, loaded at pytest start-up, blocks network connections and
  DNS lookups and records SQLite connections and file writes. After the run
  this launcher checks that every recorded connection and write stayed
  inside the root, and fails the run if one did not.

The audit only sees Python audit events of the pytest process itself. It is
evidence about this run, not a proof about the whole machine. The temporary
root is kept after the run so its contents can be inspected.

Usage, from backend/:
    ../.venv/bin/python tests/library/run_isolated.py [--root-parent DIR] [pytest arguments]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parents[1]
REPO_ROOT = BACKEND_DIR.parent
REMOVED_PREFIXES = ("OPENAI_", "AZURE_OPENAI_")
REMOVED_NAMES = {
    "DATABASE_URL",
    "ROLERADAR_LIBRARY_DB_PATH",
    "ROLERADAR_LIBRARY_DEV_LOGIN",
    "ROLERADAR_TEST_ROOT",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTHONPATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
}
# Host name the isolation test uses to prove that DNS lookups are blocked.
PROBE_HOST = "isolation-probe.invalid"
SAFE_DEVICES = {"/dev/null"}
WRITE_KINDS = {
    "open.write",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.truncate",
    "shutil.rmtree",
    "shutil.move",
}


def isolated_environment(root: Path) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(REMOVED_PREFIXES) and name not in REMOVED_NAMES
    }
    temp_dir = root / "tmp"
    temp_dir.mkdir()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{root / 'legacy_default.db'}",
            "ROLERADAR_LIBRARY_DB_PATH": str(root / "library_default.db"),
            "ROLERADAR_TEST_ROOT": str(root),
            "TMPDIR": str(temp_dir),
            "PYTHONPATH": str(HERE),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "LIBRARY_AUDIT_ROOT": str(root),
            "LIBRARY_AUDIT_LOG": str(root / "audit.json"),
        }
    )

    return env


def audit_problems(root: Path) -> tuple[list[str], dict]:
    log = root / "audit.json"

    if not log.exists():
        return ["审计日志没有生成：审计插件可能没有加载，本次运行不能算作隔离运行。"], {}

    report = json.loads(log.read_text(encoding="utf-8"))
    problems = []

    for kind, details in report["details"].items():
        for target, count in details.items():
            if kind == "socket.getaddrinfo.BLOCKED" and target == repr(PROBE_HOST):
                continue

            if kind.endswith(".BLOCKED"):
                problems.append(f"网络访问被拦截 {count} 次：{kind} {target}")
            elif kind == "open.read.sensitive":
                problems.append(f"读取了受保护的路径 {count} 次：{target}")
            elif kind == "sqlite3.connect":
                if target != ":memory:" and not _inside(target, root):
                    problems.append(f"连接了临时目录以外的 SQLite 文件 {count} 次：{target}")
            elif kind in WRITE_KINDS:
                if target not in SAFE_DEVICES and not _inside(target, root):
                    problems.append(f"在临时目录以外写入 {count} 次（{kind}）：{target}")

    return problems, report


def _inside(target: str, root: Path) -> bool:
    try:
        path = Path(target).resolve()
    except (OSError, ValueError):
        return False

    return path == root or root in path.parents


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--root-parent")
    options, pytest_args = parser.parse_known_args(argv)

    parent = Path(options.root_parent).resolve() if options.root_parent else None
    root = Path(tempfile.mkdtemp(prefix="roleradar-isolated-", dir=parent)).resolve()

    if root == REPO_ROOT or REPO_ROOT in root.parents:
        print(f"[isolated] 临时目录不能放在仓库里：{root}", file=sys.stderr)
        return 2

    env = isolated_environment(root)
    command = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        "library_audit_plugin",
        f"--basetemp={root / 'basetemp'}",
        *pytest_args,
    ]
    print(f"[isolated] temporary root: {root}", flush=True)
    result = subprocess.run(command, cwd=BACKEND_DIR, env=env)
    problems, report = audit_problems(root)
    counts = report.get("event_counts", {})
    print(f"[isolated] audit event counts: {json.dumps(counts, sort_keys=True)}")

    if problems:
        print("[isolated] ISOLATION PROBLEMS:")

        for problem in problems:
            print(f"[isolated]   - {problem}")

        return result.returncode or 3

    print("[isolated] audit: every recorded SQLite connection and file write stayed inside the temporary root")

    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
