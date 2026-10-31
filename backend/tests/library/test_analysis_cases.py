"""The frozen semantic cases (analysis_cases.py), run through the real
analysis with the simulated service. Each case states why its expectation
holds; a failure names the case and what it got instead."""
import pytest

from app.analysis_contracts import AnalysisStatus
from app.experience_analysis import analyze_selected_experiences
from analysis_cases import CASES, UNWRITTEN
from analysis_helpers import ScriptedProvider, approved, confirmed, request_for


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_rules_reach_the_expected_conclusions(database, owners, clock, case):
    owner, _ = owners
    chosen = [confirmed(database, owner, clock, category, **fields) for category, fields in case.experiences]
    request = request_for(owner, chosen, posting=case.posting)
    provider = ScriptedProvider(case.parsed(), case.scripted_answer())
    result = analyze_selected_experiences(database, request, approved(request), provider)
    got = {finding.requirement_id: finding.conclusion for finding in result.findings}

    assert result.status is AnalysisStatus.COMPLETE, (case.name, result.status, result.problem)
    assert got == case.expected, (case.name, case.why)
    assert case.expected_overall is not UNWRITTEN, case.name
    assert result.coverage.overall_score == case.expected_overall, case.name
    assert tuple(finding.requirement_id for finding in result.condition_conflicts) == case.expected_conflicts, case.name
