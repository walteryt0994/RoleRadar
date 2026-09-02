import pytest
from pydantic import ValidationError
from app.schemas import EvidenceSourceType, SkillEvidence, StudentProfile


def test_student_profile_default_skill_evidence_is_empty():
    profile = StudentProfile()

    assert profile.skill_evidence == []


def test_skill_evidence_accepts_full_payload():
    evidence = SkillEvidence(
        skill="Python",
        evidence_text="Built a job analysis API using FastAPI.",
        source_type=EvidenceSourceType.PROJECT,
        source_id="project-1",
        confidence=0.9,
        user_confirmed=True,
    )

    assert evidence.skill == "Python"
    assert evidence.source_type == EvidenceSourceType.PROJECT
    assert evidence.confidence == 0.9
    assert evidence.user_confirmed is True


def test_skill_evidence_supports_multiple_entries_for_same_skill():
    profile = StudentProfile(
        skill_evidence=[
            SkillEvidence(
                skill="Python",
                evidence_text="Completed a Data Structures course using Python.",
                source_type=EvidenceSourceType.COURSE,
                confidence=0.8,
            ),
            SkillEvidence(
                skill="Python",
                evidence_text="Built a job analysis API using FastAPI.",
                source_type=EvidenceSourceType.PROJECT,
                confidence=0.9,
                user_confirmed=True,
            ),
        ]
    )

    assert len(profile.skill_evidence) == 2
    assert all(item.skill == "Python" for item in profile.skill_evidence)
    assert {item.source_type for item in profile.skill_evidence} == {
        EvidenceSourceType.COURSE,
        EvidenceSourceType.PROJECT,
    }


def test_skill_evidence_round_trip_serialization():
    original = SkillEvidence(
        skill="SQL",
        evidence_text="Used SQL in a course project.",
        source_type=EvidenceSourceType.COURSE,
        confidence=0.7,
    )

    dumped = original.model_dump()
    restored = SkillEvidence.model_validate(dumped)

    assert restored == original


def test_skill_evidence_rejects_invalid_source_type():
    with pytest.raises(ValidationError):
        SkillEvidence(
            skill="Python",
            evidence_text="Some evidence text.",
            source_type="not_a_real_source",
        )


def test_skill_evidence_confidence_boundaries_are_valid():
    low = SkillEvidence(
        skill="Python",
        evidence_text="Some evidence text.",
        source_type=EvidenceSourceType.COURSE,
        confidence=0.0,
    )
    high = SkillEvidence(
        skill="Python",
        evidence_text="Some evidence text.",
        source_type=EvidenceSourceType.COURSE,
        confidence=1.0,
    )
    unset = SkillEvidence(
        skill="Python",
        evidence_text="Some evidence text.",
        source_type=EvidenceSourceType.COURSE,
    )

    assert low.confidence == 0.0
    assert high.confidence == 1.0
    assert unset.confidence is None


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_skill_evidence_rejects_confidence_out_of_range(confidence):
    with pytest.raises(ValidationError):
        SkillEvidence(
            skill="Python",
            evidence_text="Some evidence text.",
            source_type=EvidenceSourceType.COURSE,
            confidence=confidence,
        )


@pytest.mark.parametrize("user_confirmed", [None, True, False])
def test_skill_evidence_user_confirmed_keeps_original_value(user_confirmed):
    evidence = SkillEvidence(
        skill="Python",
        evidence_text="Some evidence text.",
        source_type=EvidenceSourceType.COURSE,
        user_confirmed=user_confirmed,
    )

    assert evidence.user_confirmed is user_confirmed


def test_skill_evidence_rejects_missing_required_skill():
    with pytest.raises(ValidationError):
        SkillEvidence(
            evidence_text="Some evidence text.",
            source_type=EvidenceSourceType.COURSE,
        )


def test_skill_evidence_rejects_none_for_required_skill():
    with pytest.raises(ValidationError):
        SkillEvidence(
            skill=None,
            evidence_text="Some evidence text.",
            source_type=EvidenceSourceType.COURSE,
        )


def test_skill_evidence_rejects_unexpected_extra_field():
    with pytest.raises(ValidationError):
        SkillEvidence(
            skill="Python",
            evidence_text="Some evidence text.",
            source_type=EvidenceSourceType.COURSE,
            unexpected_field="value",
        )


def test_student_profile_instances_do_not_share_skill_evidence_list():
    profile_a = StudentProfile()
    profile_b = StudentProfile()

    profile_a.skill_evidence.append(
        SkillEvidence(
            skill="Python",
            evidence_text="Some evidence text.",
            source_type=EvidenceSourceType.COURSE,
        )
    )

    assert len(profile_a.skill_evidence) == 1
    assert profile_b.skill_evidence == []