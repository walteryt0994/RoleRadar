import pytest
from pydantic import ValidationError

from app.schemas import (
    Certification,
    Course,
    Education,
    Experience,
    Preferences,
    Project,
    StudentProfile,
    WorkAuthorization,
    WorkAuthorizationStatus,
    WorkMode,
)


def test_student_profile_defaults_when_empty():
    profile = StudentProfile()

    assert profile.education == []
    assert profile.experience == []
    assert profile.projects == []
    assert profile.courses == []
    assert profile.certifications == []
    assert profile.skills == []
    assert profile.preferences is None
    assert profile.work_authorization is None


def test_student_profile_accepts_full_payload():
    profile = StudentProfile(
        education=[
            Education(
                school="Fictional State University",
                degree="Bachelor of Science",
                major="Computer Science",
                start_date="2022-09",
                end_date="2026-05",
                gpa=3.8,
            )
        ],
        experience=[
            Experience(
                company="Fictional Robotics Inc",
                title="Backend Intern",
                start_date="2025-06",
                end_date="至今",
                description="Built internal tooling.",
            )
        ],
        projects=[
            Project(
                name="Job Analyzer",
                description="A tool that matches resumes to job descriptions.",
                technologies=["Python", "FastAPI"],
                link="https://example.com/job-analyzer",
                start_date="2025-01",
                end_date="2025-05",
            )
        ],
        courses=[
            Course(
                name="Data Structures",
                institution="Fictional State University",
                completion_date="2024-12",
            )
        ],
        certifications=[
            Certification(
                name="Fictional Cloud Practitioner",
                issuer="Fictional Cloud Corp",
                issue_date="2024-01",
                expiration_date="2027-01",
            )
        ],
        skills=["Python", "SQL"],
        preferences=Preferences(
            desired_roles=["Software Engineer Intern"],
            locations=["Remote"],
            work_mode=WorkMode.REMOTE,
        ),
        work_authorization=WorkAuthorization(
            status=WorkAuthorizationStatus.AUTHORIZED,
            requires_sponsorship=False,
        ),
    )

    assert profile.education[0].school == "Fictional State University"
    assert profile.experience[0].end_date == "至今"
    assert profile.projects[0].technologies == ["Python", "FastAPI"]
    assert profile.courses[0].completion_date == "2024-12"
    assert profile.certifications[0].issuer == "Fictional Cloud Corp"
    assert profile.preferences.work_mode == WorkMode.REMOTE
    assert profile.work_authorization.status == WorkAuthorizationStatus.AUTHORIZED


def test_student_profile_instances_do_not_share_list_state():
    profile_a = StudentProfile()
    profile_b = StudentProfile()

    profile_a.skills.append("Python")
    profile_a.education.append(Education(school="Fictional State University"))

    assert profile_a.skills == ["Python"]
    assert profile_b.skills == []
    assert profile_b.education == []


def test_student_profile_round_trip_serialization():
    original = StudentProfile(
        skills=["Python"],
        education=[Education(school="Fictional State University")],
        work_authorization=WorkAuthorization(status=WorkAuthorizationStatus.UNKNOWN),
    )

    dumped = original.model_dump()
    restored = StudentProfile.model_validate(dumped)

    assert restored == original



def test_work_authorization_rejects_invalid_status():
    with pytest.raises(ValidationError):
        WorkAuthorization(status="not_a_real_status")


def test_education_rejects_missing_required_school():
    with pytest.raises(ValidationError):
        Education(degree="Bachelor of Science")


def test_education_rejects_none_for_required_school():
    with pytest.raises(ValidationError):
        Education(school=None)


def test_education_accepts_empty_string_for_optional_field():
    education = Education(school="Fictional State University", degree="")

    assert education.degree == ""


def test_education_rejects_unexpected_extra_field():
    with pytest.raises(ValidationError):
        Education(school="Fictional State University", unexpected_field="value")


def test_student_profile_rejects_wrong_type_for_nested_field():
    with pytest.raises(ValidationError):
        StudentProfile(preferences="not a preferences object")
