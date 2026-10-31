"""Frozen worked example for the Day 28 evidence matching work.

This is a development fixture, not the Day 29 evaluation set. Every
value here is synthetic. The expected conclusions and the expected
scores were written from the job posting text and the frozen 80 / 20
weights before any matching code existed. Change them only with a
deliberate reason, and bump CASE_VERSION when you do.

The builders return fresh objects on every call so one test cannot
mutate the data another test depends on.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.jd_record import (
    JobDescriptionRecord,
    ParseMetadata,
    fingerprint_job_posting,
)
from app.jd_service import PARSER_VERSION, PROMPT_VERSION
from app.schemas import (
    SCHEMA_VERSION,
    EvidenceSourceType,
    JobRequirement,
    JobWorkMode,
    ProfileResponse,
    Project,
    SkillEvidence,
    StructuredJobDescription,
    StudentProfile,
)

CASE_VERSION = "1"

MATCHED = "matched"
MISSING = "missing"
UNCERTAIN = "uncertain"

REQUIRED_WEIGHT = Decimal("0.80")
PREFERRED_WEIGHT = Decimal("0.20")


JOB_POSTING = """Data Platform Intern
Aurora Grid - Boston, MA (hybrid)

Required qualifications:
- Strong SQL skills
- Proficiency in Python
- Experience with Docker
- Familiarity with AWS

Preferred qualifications:
- Git for version control
"""


def build_job_description() -> StructuredJobDescription:
    return StructuredJobDescription(
        job_title="Data Platform Intern",
        company="Aurora Grid",
        location="Boston, MA",
        work_mode=JobWorkMode.HYBRID,
        seniority=None,
        minimum_experience=None,
        education_requirement=None,
        work_authorization=None,
        responsibilities=[],
        required_skills=[
            JobRequirement(
                text="SQL",
                evidence_text="Strong SQL skills",
            ),
            JobRequirement(
                text="Python",
                evidence_text="Proficiency in Python",
            ),
            JobRequirement(
                text="Docker",
                evidence_text="Experience with Docker",
            ),
            JobRequirement(
                text="AWS",
                evidence_text="Familiarity with AWS",
            ),
        ],
        preferred_skills=[
            JobRequirement(
                text="Git",
                evidence_text="Git for version control",
            ),
        ],
        hard_constraints=[],
        uncertain_requirements=[],
    )


def build_metadata() -> ParseMetadata:
    return ParseMetadata(
        generated_at="2026-10-27T12:00:00Z",
        prompt_version=PROMPT_VERSION,
        parser_version=PARSER_VERSION,
        schema_version=SCHEMA_VERSION,
        provider="synthetic",
        requested_model="synthetic-test-model",
        returned_model="synthetic-test-model",
        requested_max_output_tokens=900,
        requested_reasoning_effort=None,
        job_posting_sha256=fingerprint_job_posting(JOB_POSTING),
        latency_seconds=1.5,
        input_tokens=700,
        output_tokens=150,
        total_tokens=850,
    )


def build_record() -> JobDescriptionRecord:
    return JobDescriptionRecord(
        job_description=build_job_description(),
        metadata=build_metadata(),
    )


PYTHON_EVIDENCE = 0
SQL_EVIDENCE = 1
GIT_EVIDENCE = 2


def build_skill_evidence() -> list[SkillEvidence]:
    return [
        SkillEvidence(
            skill="Python",
            evidence_text=(
                "Cleaned order data with Python for a course project."
            ),
            source_type=EvidenceSourceType.PROJECT,
        ),
        SkillEvidence(
            skill="SQL",
            evidence_text=(
                "Summarised sales figures with SQL in the same project."
            ),
            source_type=EvidenceSourceType.PROJECT,
        ),
        SkillEvidence(
            skill="Git",
            evidence_text=(
                "Managed project versions with Git across the term."
            ),
            source_type=EvidenceSourceType.PROJECT,
        ),
    ]


def build_aws_evidence() -> SkillEvidence:
    return SkillEvidence(
        skill="AWS",
        evidence_text=(
            "Deployed the dashboard to AWS for the final demo."
        ),
        source_type=EvidenceSourceType.PROJECT,
    )


def build_second_python_evidence() -> SkillEvidence:
    return SkillEvidence(
        skill="Python",
        evidence_text=(
            "Wrote a Python script to generate weekly reports."
        ),
        source_type=EvidenceSourceType.PROJECT,
    )


def build_profile_data(
    skill_evidence: list[SkillEvidence] | None = None,
) -> StudentProfile:
    if skill_evidence is None:
        skill_evidence = build_skill_evidence()

    return StudentProfile(
        projects=[
            Project(
                name="Campus Orders Dashboard",
                description=(
                    "A course project that cleaned and summarised "
                    "order data."
                ),
            ),
        ],
        skills=["Python", "SQL", "AWS", "Git"],
        skill_evidence=skill_evidence,
    )


def build_profile(
    skill_evidence: list[SkillEvidence] | None = None,
    is_confirmed: bool = True,
) -> ProfileResponse:
    moment = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

    return ProfileResponse(
        id=1,
        profile_data=build_profile_data(skill_evidence),
        is_confirmed=is_confirmed,
        created_at=moment,
        updated_at=moment,
    )


def build_profile_with_aws_evidence() -> ProfileResponse:
    return build_profile(
        skill_evidence=build_skill_evidence() + [build_aws_evidence()],
    )


def build_profile_with_second_python_evidence() -> ProfileResponse:
    return build_profile(
        skill_evidence=(
            build_skill_evidence() + [build_second_python_evidence()]
        ),
    )


EXPECTED_REQUIRED = (
    ("SQL", MATCHED, SQL_EVIDENCE),
    ("Python", MATCHED, PYTHON_EVIDENCE),
    ("Docker", MISSING, None),
    ("AWS", UNCERTAIN, None),
)

EXPECTED_PREFERRED = (
    ("Git", MATCHED, GIT_EVIDENCE),
)

EXPECTED_REQUIRED_SCORE = Decimal("50.00")
EXPECTED_PREFERRED_SCORE = Decimal("100.00")
EXPECTED_OVERALL_SCORE = Decimal("60.00")

EXPECTED_REQUIRED_MATCHED_WITH_AWS_EVIDENCE = 3
EXPECTED_OVERALL_SCORE_WITH_AWS_EVIDENCE = Decimal("80.00")

EXPECTED_REQUIRED_MATCHED_WITH_SECOND_PYTHON_EVIDENCE = 2
EXPECTED_OVERALL_SCORE_WITH_SECOND_PYTHON_EVIDENCE = Decimal("60.00")
