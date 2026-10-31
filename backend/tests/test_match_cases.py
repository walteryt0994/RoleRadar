"""Integrity checks for the frozen Day 28 worked example.

These tests do not exercise any matching code. They pin the fixture
so the hand-written answer cannot drift away from the data it was
derived from: if someone later gives AWS an evidence entry, or edits
a conclusion without editing the score, a test here fails.
"""

from decimal import Decimal

from app.jd_record import fingerprint_job_posting
from app.jd_service import PARSER_VERSION, PROMPT_VERSION, verify_evidence
from app.schemas import SCHEMA_VERSION
from tests.match_cases import (
    EXPECTED_OVERALL_SCORE,
    EXPECTED_OVERALL_SCORE_WITH_AWS_EVIDENCE,
    EXPECTED_OVERALL_SCORE_WITH_SECOND_PYTHON_EVIDENCE,
    EXPECTED_PREFERRED,
    EXPECTED_PREFERRED_SCORE,
    EXPECTED_REQUIRED,
    EXPECTED_REQUIRED_MATCHED_WITH_AWS_EVIDENCE,
    EXPECTED_REQUIRED_MATCHED_WITH_SECOND_PYTHON_EVIDENCE,
    EXPECTED_REQUIRED_SCORE,
    JOB_POSTING,
    MATCHED,
    MISSING,
    PREFERRED_WEIGHT,
    REQUIRED_WEIGHT,
    UNCERTAIN,
    build_profile,
    build_profile_with_aws_evidence,
    build_profile_with_second_python_evidence,
    build_record,
)


def _coverage(expectations) -> Decimal:
    matched = sum(
        1 for _, conclusion, _ in expectations if conclusion == MATCHED
    )

    return Decimal(matched) / Decimal(len(expectations)) * Decimal(100)


def _evidence_skills(profile) -> list[str]:
    return [item.skill for item in profile.profile_data.skill_evidence]


def test_every_requirement_quotes_the_job_posting():
    record = build_record()

    verify_evidence(JOB_POSTING, record.job_description)


def test_record_fingerprint_matches_the_job_posting():
    record = build_record()

    assert record.metadata.job_posting_sha256 == fingerprint_job_posting(
        JOB_POSTING
    )


def test_record_versions_match_the_current_contracts():
    metadata = build_record().metadata

    assert metadata.schema_version == SCHEMA_VERSION
    assert metadata.prompt_version == PROMPT_VERSION
    assert metadata.parser_version == PARSER_VERSION


def test_expectations_cover_exactly_the_posted_requirements():
    job_description = build_record().job_description

    required = [item.text for item in job_description.required_skills]
    preferred = [item.text for item in job_description.preferred_skills]

    assert required == [skill for skill, _, _ in EXPECTED_REQUIRED]
    assert preferred == [skill for skill, _, _ in EXPECTED_PREFERRED]


def test_expected_conclusions_use_only_the_three_labels():
    labels = {
        conclusion
        for _, conclusion, _ in EXPECTED_REQUIRED + EXPECTED_PREFERRED
    }

    assert labels <= {MATCHED, MISSING, UNCERTAIN}


def test_matched_expectations_point_at_a_real_evidence_entry():
    evidence = build_profile().profile_data.skill_evidence

    for skill, conclusion, index in EXPECTED_REQUIRED + EXPECTED_PREFERRED:
        if conclusion == MATCHED:
            assert index is not None
            assert evidence[index].skill == skill
        else:
            assert index is None


def test_profile_snapshot_is_confirmed():
    assert build_profile().is_confirmed is True


def test_base_evidence_has_no_source_id_or_item_confirmation():
    for item in build_profile().profile_data.skill_evidence:
        assert item.source_id is None
        assert item.user_confirmed is None


def test_aws_is_declared_without_supporting_evidence():
    profile = build_profile()

    assert "AWS" in profile.profile_data.skills
    assert "AWS" not in _evidence_skills(profile)


def test_docker_is_absent_from_the_whole_profile():
    profile = build_profile()
    profile_data = profile.profile_data

    assert "Docker" not in profile_data.skills
    assert "Docker" not in _evidence_skills(profile)

    for item in profile_data.skill_evidence:
        assert "Docker" not in item.evidence_text

    for project in profile_data.projects:
        assert "Docker" not in project.technologies


def test_expected_group_scores_follow_the_listed_conclusions():
    assert _coverage(EXPECTED_REQUIRED) == EXPECTED_REQUIRED_SCORE
    assert _coverage(EXPECTED_PREFERRED) == EXPECTED_PREFERRED_SCORE


def test_expected_overall_score_follows_the_frozen_weights():
    weighted = (
        EXPECTED_REQUIRED_SCORE * REQUIRED_WEIGHT
        + EXPECTED_PREFERRED_SCORE * PREFERRED_WEIGHT
    )

    assert weighted == EXPECTED_OVERALL_SCORE


def test_adding_aws_evidence_raises_only_the_required_group():
    profile = build_profile_with_aws_evidence()

    assert "AWS" in _evidence_skills(profile)

    required = (
        Decimal(EXPECTED_REQUIRED_MATCHED_WITH_AWS_EVIDENCE)
        / Decimal(len(EXPECTED_REQUIRED))
        * Decimal(100)
    )
    weighted = (
        required * REQUIRED_WEIGHT
        + EXPECTED_PREFERRED_SCORE * PREFERRED_WEIGHT
    )

    assert weighted == EXPECTED_OVERALL_SCORE_WITH_AWS_EVIDENCE
    assert weighted > EXPECTED_OVERALL_SCORE


def test_second_python_evidence_adds_no_new_distinct_skill():
    profile = build_profile_with_second_python_evidence()
    skills = _evidence_skills(profile)

    assert skills.count("Python") == 2
    assert set(skills) == {"Python", "SQL", "Git"}


def test_second_python_evidence_leaves_the_score_unchanged():
    required = (
        Decimal(EXPECTED_REQUIRED_MATCHED_WITH_SECOND_PYTHON_EVIDENCE)
        / Decimal(len(EXPECTED_REQUIRED))
        * Decimal(100)
    )
    weighted = (
        required * REQUIRED_WEIGHT
        + EXPECTED_PREFERRED_SCORE * PREFERRED_WEIGHT
    )

    assert weighted == EXPECTED_OVERALL_SCORE_WITH_SECOND_PYTHON_EVIDENCE
    assert weighted == EXPECTED_OVERALL_SCORE
