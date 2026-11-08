"""Independent offline acceptance probes; never change product files or use a real provider."""
import copy
import json

import pytest

from app.discovery import compare
from app.discovery.agent import ScriptedDecider, make_action
from app.discovery.compare_tasks import TASKS
from app.discovery.contracts import EndReason, Tool
from app.discovery.synthetic import CAREERS


@pytest.fixture
def good(tmp_path):
    folder = tmp_path / "good"
    compare.run_batch(folder)
    return json.loads((folder / "runs/T1-agent.json").read_text())


@pytest.mark.parametrize("field,value", [
    ("source_id", "unregistered-source"),
    ("evidence", "lead"),
    ("url", "https://not-the-source.invalid/invented"),
    ("content_sha256", "0" * 64),
])
def test_reject_fabricated_provenance(good, field, value):
    altered = copy.deepcopy(good)
    altered["results"][0][field] = value
    assert compare.hard_checks(TASKS[0], altered), f"forged {field} was accepted"


def test_no_results_requires_found_postings_to_be_concluded(tmp_path):
    script = [
        make_action(Tool.SEARCH, source_id=CAREERS, keywords="数据分析", city="上海", job_type="实习", direction="数据分析"),
        make_action(Tool.FINISH, claim=EndReason.ENOUGH),
    ]
    folder = tmp_path / "pending"
    compare.run_batch(folder, tasks=(TASKS[1],), decider_for=lambda: ScriptedDecider(script))
    run = json.loads((folder / "runs/T2-agent.json").read_text())
    assert run["results"] == [] and run["unsearched"] == [] and run["leads"]
    assert run["end"] == "enough"
    run["end"] = "no_results"
    assert compare.hard_checks(TASKS[1], run), "unfinished postings were accepted as no suitable results"


def test_unexpected_run_error_still_produces_report_and_stops(tmp_path):
    class Broken:
        kind = "scripted"

        def decide(self, observation):
            raise RuntimeError("synthetic internal failure")

    folder = tmp_path / "failed"
    summary = compare.run_batch(folder, decider_for=Broken)
    assert summary["stopped"] and summary["not_run"] == ["T2", "T3", "T4"]
    assert (folder / "report.md").is_file()
