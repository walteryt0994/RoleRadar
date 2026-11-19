"""岗位要求依据覆盖 (owner redesign, plan 7; app.evidence_coverage, evidence_coverage_v1).

A descriptive count of the REQUIRED and PREFERRED requirements an analysis supports, shown with both numbers; never
the weighted skill coverage of app.match_service (required 80%, preferred 20%), which keeps its own meaning. These
tests cover every case plan 7.3 names, the rounding of 7.2, and how the pages show it."""
from decimal import Decimal

import pytest

from app import i18n
from app.analysis_contracts import (
    AnalysisResult,
    AnalysisStatus,
    ConditionConclusion,
    EvidenceConclusion,
    RequirementFinding,
    RequirementGroup,
)
from app.discovery.records import analysis_body, shown_analysis
from app.evidence_coverage import VERSION, EvidenceCoverage, NoCoverage, coverage_record, evidence_coverage
from app.match_service import coverage_score
from app.schemas import MatchConclusion

R, P, H, U = RequirementGroup.REQUIRED, RequirementGroup.PREFERRED, RequirementGroup.HARD_CONSTRAINT, RequirementGroup.UNCERTAIN
YES, ASK, NONE = EvidenceConclusion.SUPPORTED, EvidenceConclusion.NEEDS_CONFIRMATION, EvidenceConclusion.NO_EVIDENCE


def finding(requirement_id, group, conclusion, citations=0, listed=()):
    return {
        "requirement_id": requirement_id,
        "group": group,
        "conclusion": conclusion,
        "citations": [{"quote": f"引文 {n}"} for n in range(citations)],
        "listed": list(listed),
    }


def complete(*findings):
    return {"status": AnalysisStatus.COMPLETE, "findings": list(findings)}


def test_two_of_four_is_fifty_percent_with_both_numbers():
    coverage = evidence_coverage(complete(finding("r1", R, YES), finding("r2", R, NONE), finding("p1", P, YES), finding("p2", P, ASK)))

    assert coverage == EvidenceCoverage(supported=2, total=4, needs_confirmation=1, no_evidence=1)
    assert coverage.percent == "50" and coverage.version == VERSION == "evidence_coverage_v1"


def test_none_of_four_is_zero_percent_not_hidden():
    coverage = evidence_coverage(complete(*(finding(f"r{n}", R, NONE) for n in range(4))))

    assert (coverage.supported, coverage.total, coverage.percent) == (0, 4, "0")


def test_no_countable_requirement_shows_no_percentage():
    assert evidence_coverage(complete()) is NoCoverage.NO_SKILLS
    # Only hard conditions and vague requirements: nothing to count, never "0%".
    assert evidence_coverage(complete(finding("h1", H, ConditionConclusion.MET), finding("u1", U, YES))) is NoCoverage.NO_SKILLS


def test_needs_confirmation_and_no_evidence_stay_in_the_denominator_only():
    coverage = evidence_coverage(complete(finding("r1", R, ASK), finding("r2", R, ASK), finding("p1", P, NONE)))

    assert (coverage.supported, coverage.total, coverage.needs_confirmation, coverage.no_evidence) == (0, 3, 2, 1)


def test_hard_constraints_vague_requirements_and_preferences_are_not_counted():
    coverage = evidence_coverage(
        complete(finding("r1", R, YES), finding("h1", H, ConditionConclusion.CONFLICT), finding("u1", U, NONE))
    )

    assert (coverage.supported, coverage.total, coverage.percent) == (1, 1, "100")


def test_several_citations_of_one_requirement_count_once_and_a_listed_requirement_is_not_split():
    coverage = evidence_coverage(
        complete(finding("r1", R, YES, citations=3), finding("r2", R, NONE, listed=("Python", "SQL", "Tableau")))
    )

    assert (coverage.supported, coverage.total) == (1, 2)


def test_a_requirement_given_twice_refuses_the_count_instead_of_counting_it_once():
    assert evidence_coverage(complete(finding("r1", R, YES), finding("r1", R, YES))) is NoCoverage.INCONSISTENT
    assert evidence_coverage(complete(finding("", R, YES))) is NoCoverage.INCONSISTENT
    assert evidence_coverage(complete(finding("r1", R, "maybe"))) is NoCoverage.INCONSISTENT
    assert evidence_coverage({"status": AnalysisStatus.COMPLETE}) is NoCoverage.INCONSISTENT


@pytest.mark.parametrize(
    "status",
    [AnalysisStatus.FAILED, AnalysisStatus.DEGRADED, AnalysisStatus.REJECTED, AnalysisStatus.CANCELLED, AnalysisStatus.STALE],
)
def test_an_analysis_that_is_not_complete_shows_no_percentage(status):
    assert evidence_coverage({"status": status, "findings": [finding("r1", R, YES)]}) is NoCoverage.NOT_COMPLETE


def test_required_one_of_one_preferred_none_of_three_weighted_80_but_described_25():
    # The difference plan 7.3 asks to keep visible: two counts with different meanings, never one metric.
    findings = [finding("r1", R, YES), finding("p1", P, NONE), finding("p2", P, NONE), finding("p3", P, ASK)]
    weighted = coverage_score(
        [MatchConclusion.MATCHED], [MatchConclusion.MISSING, MatchConclusion.MISSING, MatchConclusion.UNCERTAIN]
    )
    described = evidence_coverage(complete(*findings))

    assert weighted.overall_score == Decimal("80")
    assert (described.supported, described.total, described.percent) == (1, 4, "25")


@pytest.mark.parametrize(
    ("supported", "total", "shown"),
    [
        (1, 3, "33.3"),
        (2, 3, "66.7"),
        (1, 8, "12.5"),
        (999, 1000, "99.9"),
        # 99.95 rounds to 100.0: below 100% it never reads as full coverage.
        (1999, 2000, "＞99.9"),
        (9999, 10000, "＞99.9"),
        # 99.945 rounds down: an ordinary share.
        (19989, 20000, "99.9"),
        # Exactly 0.1%: the threshold itself is an ordinary share.
        (1, 1000, "0.1"),
        # 0.05% (1 of 2,000) and 0.0999…% are below 0.1%, judged before rounding (plan 7.2): never "0.1%".
        (1, 2000, "＜0.1"),
        (1, 1001, "＜0.1"),
        # 0.033…: above 0, never read as no evidence.
        (1, 3000, "＜0.1"),
        (3, 3, "100"),
    ],
)
def test_rounding_keeps_at_most_one_decimal_and_never_reads_full_or_empty_wrongly(supported, total, shown):
    assert EvidenceCoverage(supported=supported, total=total, needs_confirmation=0, no_evidence=total - supported).percent == shown


def kept_content(*rows, metrics=None):
    content = {
        "findings": [
            {
                "requirement_id": requirement_id,
                "group": group.value,
                "text": "合成要求",
                "job_posting_quote": "合成原文",
                "conclusion": conclusion.value,
                "citations": [],
                "reasons": [],
            }
            for requirement_id, group, conclusion in rows
        ],
        "preferences": "not_analyzed",
        "preference_findings": [],
    }

    if metrics is not None:
        content["display_metrics"] = {"evidence_coverage": metrics}

    return content


FOUR = (("r1", R, YES), ("r2", R, NONE), ("p1", P, YES), ("p2", P, ASK))


def result(*rows, status=AnalysisStatus.COMPLETE):
    return AnalysisResult(
        status=status,
        task_fingerprint="合成指纹",
        mode="simulation",
        requests_sent=1,
        findings=tuple(
            RequirementFinding(requirement_id, group, "合成要求", "合成原文", conclusion.value, (), ())
            for requirement_id, group, conclusion in rows
        ),
    )


def test_a_kept_analysis_shows_the_count_it_was_kept_with():
    # Kept through analysis_body (what records.keep_analysis stores): the version and the four counts go with it, and
    # the page shows them again from the kept findings, never from the experiences as they are now.
    body = analysis_body(result(*FOUR))

    assert body["display_metrics"] == {
        "evidence_coverage": {"version": VERSION, "supported": 2, "total": 4, "needs_confirmation": 1, "no_evidence": 1}
    }

    coverage = evidence_coverage(shown_analysis({**body, "findings": kept_content(*FOUR)["findings"]}))

    assert (coverage.supported, coverage.total, coverage.percent) == (2, 4, "50")


def test_a_kept_analysis_without_the_count_never_gets_a_new_percentage():
    # Kept before this count existed (no display_metrics) or with another version of it: nothing is made up.
    assert evidence_coverage(shown_analysis(kept_content(*FOUR))) is NoCoverage.NOT_RECORDED
    assert evidence_coverage(shown_analysis(kept_content(*FOUR, metrics={"version": "evidence_coverage_v0", "supported": 2, "total": 4, "needs_confirmation": 1, "no_evidence": 1}))) is NoCoverage.NOT_RECORDED
    # A bare object with findings and neither a status nor a kept count: no percentage either (Codex review case).
    old = {"findings": [{"requirement_id": "r1", "group": "required", "conclusion": "supported"}]}
    assert not isinstance(evidence_coverage(old), EvidenceCoverage)


def test_a_kept_count_that_its_kept_findings_no_longer_give_is_not_shown():
    wrong = {"version": VERSION, "supported": 3, "total": 4, "needs_confirmation": 0, "no_evidence": 1}
    text = {"version": VERSION, "supported": "2", "total": 4, "needs_confirmation": 1, "no_evidence": 1}

    assert evidence_coverage(shown_analysis(kept_content(*FOUR, metrics=wrong))) is NoCoverage.INCONSISTENT
    assert evidence_coverage(shown_analysis(kept_content(*FOUR, metrics=text))) is NoCoverage.INCONSISTENT


def test_a_kept_analysis_with_no_countable_requirement_keeps_its_reason():
    body = analysis_body(result(("h1", H, ConditionConclusion.MET)))

    assert body["display_metrics"]["evidence_coverage"] == {"version": VERSION, "none": "no_skills"}
    assert evidence_coverage(shown_analysis({**body, "findings": []})) is NoCoverage.NO_SKILLS
    # Saved as having none, but its findings now count something: inconsistent, not a percentage.
    assert evidence_coverage(shown_analysis(kept_content(*FOUR, metrics={"version": VERSION, "none": "no_skills"}))) is NoCoverage.INCONSISTENT


def test_only_a_complete_result_is_counted_when_kept_or_shown():
    for status in (AnalysisStatus.DEGRADED, AnalysisStatus.FAILED, AnalysisStatus.STALE):
        assert evidence_coverage(result(*FOUR, status=status)) is NoCoverage.NOT_COMPLETE
        assert coverage_record(result(*FOUR, status=status)) == {"version": VERSION, "none": "not_complete"}


def test_the_kept_count_reads_the_same_in_either_language():
    content = {**analysis_body(result(*FOUR)), "findings": kept_content(*FOUR)["findings"]}

    with i18n.using("zh-CN"):
        chinese = evidence_coverage(shown_analysis(content))
    with i18n.using("en"):
        english = evidence_coverage(shown_analysis(content))

    assert chinese == english == EvidenceCoverage(2, 4, 1, 1)


def render_card(app, analysis, conflicts=()):
    module = app.state.library.templates.env.get_template("_findings.html").make_module(
        {"titles": {}, "conclusion_label": str, "requirement_words": lambda finding: finding["requirement_id"]}
    )

    return str(module.coverage_card(analysis, conflicts))


def test_the_card_names_the_count_and_shows_both_numbers_in_either_language(app):
    analysis = complete(finding("r1", R, YES), finding("r2", R, NONE), finding("p1", P, YES), finding("p2", P, ASK))

    with i18n.using("zh-CN"):
        chinese = render_card(app, analysis)
    with i18n.using("en"):
        english = render_card(app, analysis)

    assert "岗位要求依据覆盖" in chinese and "技能要求" in chinese and ">50%<" in chinese and "2／4 项有依据" in chinese
    assert "Requirement evidence coverage" in english and ">50%<" in english and "2 of 4 with evidence" in english
    # Never the weighted score's name or its weights on this card.
    for card in (chinese, english):
        assert "80%" not in card and "加权" not in card and "weighted" not in card.lower()


def test_the_card_says_why_there_is_no_percentage_and_never_writes_zero(app):
    with i18n.using("zh-CN"):
        none = render_card(app, complete())
        failed = render_card(app, {"status": AnalysisStatus.FAILED, "findings": []})

    assert "岗位原文里没有可计数的技能要求" in none and "%" not in none.split("</h2>", 1)[1]
    assert "分析没有完整完成" in failed and "0%" not in failed

    with i18n.using("zh-CN"):
        old = render_card(app, shown_analysis(kept_content(*FOUR)))

    assert "保存时没有记录依据覆盖" in old and "%" not in old.split("</h2>", 1)[1]


def test_a_hard_condition_in_conflict_stands_beside_a_full_coverage(app):
    conflict = finding("h1", H, ConditionConclusion.CONFLICT)
    analysis = complete(finding("r1", R, YES), conflict)

    with i18n.using("zh-CN"):
        card = render_card(app, analysis, [conflict])

    assert ">100%<" in card and "另有硬性条件冲突" in card
