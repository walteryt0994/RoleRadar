"""Isolation audit for the RoleRadar test launcher (observation only).

Loaded with -p by run_isolated.py, so the audit hook is installed when pytest
starts, before tests are collected or application modules are imported. It
blocks network connections and DNS lookups, and records SQLite connection
targets, file opens with write intent, reads of protected paths, file-system
changes and process launches. A JSON summary is written to
$LIBRARY_AUDIT_LOG when the session ends.

Only Python audit events of this process are seen. File access done directly
inside C libraries (for example SQLite's own journal files) and events this
hook does not listen to are not recorded.
"""
import json
import os
import socket
import sys
from pathlib import Path

LOG_PATH = os.environ["LIBRARY_AUDIT_LOG"]
REPO = str(Path(__file__).resolve().parents[3])
PROTECTED = (
    REPO + "/backend/records",
    REPO + "/.claude",
    str(Path.home() / "Documents"),
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
FS_EVENTS = (
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.mkdir",
    "os.truncate",
    "shutil.rmtree",
    "shutil.move",
)
PROCESS_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork")

events = []
_busy = False


def _path(value):
    if isinstance(value, int):
        return None

    try:
        return os.path.abspath(os.fsdecode(value))
    except (TypeError, ValueError):
        return repr(value)


def _protected(path):
    name = os.path.basename(path)

    return (
        name == ".env"
        or name.startswith(".env.local")
        or name.startswith("roleradar.db")
        or any(path == root or path.startswith(root + "/") for root in PROTECTED)
    )


def _hook(event, args):
    global _busy

    if _busy:
        return

    _busy = True

    try:
        if event == "socket.connect":
            sock, address = args

            if getattr(sock, "family", None) == socket.AF_UNIX:
                events.append(["socket.connect.unix", repr(address)])
                return

            events.append(["socket.connect.BLOCKED", repr(address)])
            raise ConnectionRefusedError("network blocked by the isolation audit")

        if event == "socket.getaddrinfo":
            events.append(["socket.getaddrinfo.BLOCKED", repr(args[0])])
            raise OSError("DNS lookup blocked by the isolation audit")

        if event == "sqlite3.connect":
            target = args[0]
            events.append(["sqlite3.connect", _path(target) if target != ":memory:" else ":memory:"])
        elif event == "open":
            path = _path(args[0])

            if path is None:
                return

            mode, flags = args[1], args[2]
            writing = bool(flags & WRITE_FLAGS) if isinstance(flags, int) else any(
                char in str(mode or "") for char in "wax+"
            )

            if writing:
                events.append(["open.write", path])
            elif _protected(path):
                events.append(["open.read.sensitive", path])
        elif event in FS_EVENTS:
            path = _path(args[0])

            if path:
                events.append([event, path])
        elif event in PROCESS_EVENTS:
            events.append([event, repr(args[0])[:200]])
    finally:
        _busy = False


sys.addaudithook(_hook)
os.environ["LIBRARY_AUDIT_ACTIVE"] = "1"


def pytest_sessionfinish(session, exitstatus):
    details = {}

    for kind, target in events:
        bucket = details.setdefault(kind, {})
        bucket[target] = bucket.get(target, 0) + 1

    report = {
        "exitstatus": int(exitstatus),
        "event_counts": {kind: sum(bucket.values()) for kind, bucket in details.items()},
        "details": details,
    }

    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True, ensure_ascii=False)
