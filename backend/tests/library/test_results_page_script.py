"""Owner redesign revision R2: the list-beside-one-item script itself, executed offline.

js/results_page.test.cjs runs the real app/library/static/results.js in Node's vm, against a small DOM double built
like the pages that load it (查找与分析记录, 我的岗位, a search's results): no browser, server, network or package. It
covers what Codex reproduced after R1 (a search record clicked on the records page, then reloaded, opened the analyses
tab): the address after a click, 返回列表, the browser's back and forward, or a copied link opened again, names the
tab and the item the page shows; the filters stay as they were.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parent / "js" / "results_page.test.cjs"
# The cases in the suite: all must run and pass, none skipped.
CASES = 7
# Where node usually is, for a run whose PATH was emptied (env -i).
NODE_PLACES = ("/usr/local/bin/node", "/opt/homebrew/bin/node")


def node() -> str | None:
    return shutil.which("node") or next((place for place in NODE_PLACES if os.access(place, os.X_OK)), None)


def test_the_results_script_keeps_the_tab_and_the_item_in_the_address_and_the_filters_as_they_were():
    binary = node()

    if binary is None:
        pytest.skip("这台电脑上没有 node：列表脚本的离线测试没有执行")

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
