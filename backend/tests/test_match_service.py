import pytest
from pydantic import ValidationError

from app.jd_outcome import (
    JobDescriptionOutcome,
    ParseFailureCategory,
    ParseOutcomeSource,
)
from app.jd_record import JobDescriptionRecord
from app.match_service import (
    MATCH_VERSION,
    SCORING_VERSION,
    MatchInputError,
    coverage_score,
    match_profile_to_job,
)
from app.schemas import JobRequirement, MatchConclusion
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
    MATCHED,
    MISSING,
    PYTHON_EVIDENCE,
    UNCERTAIN,
    build_profile,
    build_profile_data,
    build_profile_with_aws_evidence,
    build_profile_with_second_python_evidence,
    build_record,
)


def test_match_conclusion_labels_agree_with_the_frozen_case():
    assert MatchConclusion.MATCHED.value == MATCHED
    assert MatchConclusion.MISSING.value == MISSING
    assert MatchConclusion.UNCERTAIN.value == UNCERTAIN


def test_result_carries_the_current_versions():
    result = match_profile_to_job(build_profile(), build_record())

    assert result.match_version == MATCH_VERSION
    assert result.scoring_version == SCORING_VERSION


def test_result_points_back_at_both_input_snapshots():
    profile = build_profile()
    record = build_record()

    result = match_profile_to_job(profile, record)
    metadata = record.metadata

    assert result.inputs.profile_id == profile.id
    assert result.inputs.profile_updated_at == profile.updated_at
    assert result.inputs.job_posting_sha256 == metadata.job_posting_sha256
    assert result.inputs.prompt_version == metadata.prompt_version
    assert result.inputs.parser_version == metadata.parser_version
    assert result.inputs.schema_version == metadata.schema_version


def test_unconfirmed_profile_is_rejected():
    profile = build_profile(is_confirmed=False)

    with pytest.raises(MatchInputError):
        match_profile_to_job(profile, build_record())


def test_bare_student_profile_is_rejected():
    with pytest.raises(MatchInputError):
        match_profile_to_job(build_profile_data(), build_record())


def test_job_description_without_its_record_is_rejected():
    record = build_record()

    with pytest.raises(MatchInputError):
        match_profile_to_job(build_profile(), record.job_description)


def test_rule_fallback_outcome_is_rejected():
    outcome = JobDescriptionOutcome(
        source=ParseOutcomeSource.RULE_FALLBACK,
        rule_skills=("Python", "SQL"),
        failure_category=ParseFailureCategory.TIMEOUT,
    )

    with pytest.raises(MatchInputError):
        match_profile_to_job(build_profile(), outcome)


def test_matching_does_not_mutate_its_inputs():
    profile = build_profile()
    record = build_record()
    profile_before = profile.model_dump()
    job_description_before = record.job_description.model_dump()

    match_profile_to_job(profile, record)

    assert profile.model_dump() == profile_before
    assert record.job_description.model_dump() == job_description_before


def test_result_cannot_be_edited_after_it_is_built():
    result = match_profile_to_job(build_profile(), build_record())

    with pytest.raises(ValidationError):
        result.match_version = "999"


# --- the frozen worked example, now reached by the implementation ------------


def _actual(matches):
    return tuple(
        (match.requirement.text, match.conclusion.value, match.evidence_index)
        for match in matches
    )


def test_the_implementation_reaches_the_frozen_conclusions():
    result = match_profile_to_job(build_profile(), build_record())

    assert _actual(result.required) == EXPECTED_REQUIRED
    assert _actual(result.preferred) == EXPECTED_PREFERRED


def test_the_implementation_reaches_the_frozen_scores():
    score = match_profile_to_job(build_profile(), build_record()).score

    assert score.required_score == EXPECTED_REQUIRED_SCORE
    assert score.preferred_score == EXPECTED_PREFERRED_SCORE
    assert score.overall_score == EXPECTED_OVERALL_SCORE
    # The denominator and the undecided one are said, not only the share.
    assert (score.required_total, score.required_matched, score.required_uncertain) == (4, 2, 1)


def test_aws_evidence_raises_the_score_to_the_frozen_value():
    score = match_profile_to_job(build_profile_with_aws_evidence(), build_record()).score

    assert score.required_matched == EXPECTED_REQUIRED_MATCHED_WITH_AWS_EVIDENCE
    assert score.overall_score == EXPECTED_OVERALL_SCORE_WITH_AWS_EVIDENCE


def test_more_evidence_of_the_same_skill_leaves_the_frozen_score():
    result = match_profile_to_job(build_profile_with_second_python_evidence(), build_record())
    python = next(match for match in result.required if match.requirement.text == "Python")

    assert result.score.required_matched == EXPECTED_REQUIRED_MATCHED_WITH_SECOND_PYTHON_EVIDENCE
    assert result.score.overall_score == EXPECTED_OVERALL_SCORE_WITH_SECOND_PYTHON_EVIDENCE
    assert python.evidence_index == PYTHON_EVIDENCE


def test_an_llm_outcome_is_matched_through_the_record_it_carries():
    outcome = JobDescriptionOutcome(source=ParseOutcomeSource.LLM, record=build_record())

    assert match_profile_to_job(build_profile(), outcome) == match_profile_to_job(build_profile(), build_record())


def test_a_record_of_another_schema_version_is_rejected():
    record = build_record()
    other = JobDescriptionRecord(
        job_description=record.job_description,
        metadata=record.metadata.model_copy(update={"schema_version": "999"}),
    )

    with pytest.raises(MatchInputError):
        match_profile_to_job(build_profile(), other)


def test_a_requirement_named_twice_is_scored_once():
    record = build_record()
    twice = record.job_description.model_copy(
        update={
            "required_skills": record.job_description.required_skills
            + [JobRequirement(text="sql", evidence_text="Strong SQL skills")]
        }
    )
    result = match_profile_to_job(
        build_profile(), JobDescriptionRecord(job_description=twice, metadata=record.metadata)
    )

    assert [match.requirement.text for match in result.required] == ["SQL", "Python", "Docker", "AWS"]
    assert result.score.overall_score == EXPECTED_OVERALL_SCORE


def test_scoring_is_not_applicable_without_requirements_and_uses_the_group_there_is():
    nothing = coverage_score([], [])
    only_required = coverage_score([MatchConclusion.MATCHED, MatchConclusion.MISSING], [])
    only_preferred = coverage_score([], [MatchConclusion.MATCHED])

    assert (nothing.required_score, nothing.preferred_score, nothing.overall_score) == (None, None, None)
    assert (only_required.required_score, only_required.overall_score) == (50, 50)
    assert only_required.preferred_score is None
    assert (only_preferred.preferred_score, only_preferred.overall_score) == (100, 100)
