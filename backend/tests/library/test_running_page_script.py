"""U3-A3 R1 (F1): the running page's script itself, executed offline.

js/running_page.test.cjs runs the real app/library/static/job_analysis.js in Node's vm, against small event and DOM
doubles: no browser, server, network or package. It covers both status formats the script reads, the job analysis
one (step and sends) and the search one (status and steps), and what must stop once the user cancels or leaves, or
the task ended: timers already set, and answers and failures that come back late.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent / "js" / "running_page.test.cjs"
# The cases in the suite: all must run and pass, none skipped.
CASES = 20
# Where node usually is, for a run whose PATH was emptied (env -i).
NODE_PLACES = ("/usr/local/bin/node", "/opt/homebrew/bin/node")


def node() -> str | None:
    return shutil.which("node") or next((place for place in NODE_PLACES if os.access(place, os.X_OK)), None)


def test_the_running_page_script_stops_asking_once_the_page_is_left_or_the_task_ended():
    binary = node()

    if binary is None:
        pytest.skip("这台电脑上没有 node：运行页脚本的离线测试没有执行")

    result = subprocess.run(
        [binary, "--test", "--test-reporter=tap", SUITE.name],
        cwd=SUITE.parent,
        env={"PATH": "/usr/bin:/bin", "TMPDIR": os.environ.get("TMPDIR", "/tmp")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    counts = dict(re.findall(r"^# (tests|pass|fail|skipped|todo|cancelled) (\d+)$", result.stdout, re.M))

    assert result.returncode == 0, result.stdout + result.stderr
    assert counts == {"tests": str(CASES), "pass": str(CASES), "fail": "0", "skipped": "0", "todo": "0", "cancelled": "0"}, result.stdout
