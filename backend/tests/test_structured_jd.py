import pytest
from pydantic import ValidationError

from app.schemas import (
    JobRequirement,
    JobWorkMode,
    StructuredJobDescription,
)

VALID_JD = {
    "job_title": "Backend Engineer Intern",
    "company": "Northwind Robotics",
    "location": "Boston, MA",
    "work_mode": "hybrid",
    "seniority": "intern",
    "minimum_experience": None,
    "education_requirement": (
        "Currently pursuing a BS or MS in Computer Science"
    ),
    "work_authorization": (
        "Must be authorized to work in the US without sponsorship"
    ),
    "responsibilities": ["Build internal data services"],
    "required_skills": [
        {
            "text": "Python",
            "evidence_text": "Must be proficient in Python",
        },
    ],
    "preferred_skills": [
        {
            "text": "Docker",
            "evidence_text": "Experience with Docker",
        },
    ],
    "hard_constraints": [
        {
            "text": "US work authorization without sponsorship",
            "evidence_text": (
                "Must be authorized to work in the US without sponsorship"
            ),
        },
    ],
    "uncertain_requirements": [
        {
            "text": "Cloud platform exposure",
            "evidence_text": (
                "Some exposure to cloud platforms is a plus"
            ),
        },
    ],
}

NULLABLE_FIELDS = [
    "job_title",
    "company",
    "location",
    "work_mode",
    "seniority",
    "minimum_experience",
    "education_requirement",
    "work_authorization",
]

LIST_FIELDS = [
    "responsibilities",
    "required_skills",
    "preferred_skills",
    "hard_constraints",
    "uncertain_requirements",
]


def _jd_payload(**overrides):
    payload = dict(VALID_JD)
    payload.update(overrides)

    return payload


def test_structured_jd_accepts_full_payload():
    jd = StructuredJobDescription(**VALID_JD)

    assert jd.job_title == "Backend Engineer Intern"
    assert jd.company == "Northwind Robotics"
    assert jd.work_mode is JobWorkMode.HYBRID
    assert jd.seniority == "intern"


def test_structured_jd_keeps_required_and_preferred_separate():
    jd = StructuredJobDescription(**VALID_JD)

    assert [item.text for item in jd.required_skills] == ["Python"]
    assert [item.text for item in jd.preferred_skills] == ["Docker"]


def test_structured_jd_binds_evidence_to_each_requirement():
    jd = StructuredJobDescription(**VALID_JD)

    assert jd.required_skills[0].evidence_text == (
        "Must be proficient in Python"
    )
    assert jd.uncertain_requirements[0].evidence_text == (
        "Some exposure to cloud platforms is a plus"
    )


def test_structured_jd_represents_unknown_facts_as_none():
    payload = _jd_payload(
        **{name: None for name in NULLABLE_FIELDS}
    )

    jd = StructuredJobDescription(**payload)

    for name in NULLABLE_FIELDS:
        assert getattr(jd, name) is None


def test_structured_jd_represents_nothing_extracted_as_empty_list():
    payload = _jd_payload(
        **{name: [] for name in LIST_FIELDS}
    )

    jd = StructuredJobDescription(**payload)

    for name in LIST_FIELDS:
        assert getattr(jd, name) == []


@pytest.mark.parametrize("missing_field", sorted(VALID_JD))
def test_structured_jd_rejects_missing_fields(missing_field):
    payload = dict(VALID_JD)
    del payload[missing_field]

    with pytest.raises(ValidationError):
        StructuredJobDescription(**payload)


def test_structured_jd_rejects_extra_fields():
    payload = _jd_payload(salary_range="$30/hr")

    with pytest.raises(ValidationError):
        StructuredJobDescription(**payload)


@pytest.mark.parametrize("work_mode", ["onsite", "hybrid", "remote"])
def test_structured_jd_accepts_supported_work_modes(work_mode):
    jd = StructuredJobDescription(**_jd_payload(work_mode=work_mode))

    assert jd.work_mode.value == work_mode


@pytest.mark.parametrize(
    "work_mode",
    ["no_preference", "flexible", "Hybrid", ""],
)
def test_structured_jd_rejects_unsupported_work_modes(work_mode):
    with pytest.raises(ValidationError):
        StructuredJobDescription(**_jd_payload(work_mode=work_mode))


@pytest.mark.parametrize(
    "requirement",
    [
        {"text": "Python"},
        {"evidence_text": "Must be proficient in Python"},
        {"text": "   ", "evidence_text": "Must be proficient in Python"},
        {"text": "Python", "evidence_text": "   "},
        {
            "text": "Python",
            "evidence_text": "Must be proficient in Python",
            "source": "requirements",
        },
    ],
)
def test_job_requirement_rejects_invalid_entries(requirement):
    with pytest.raises(ValidationError):
        JobRequirement(**requirement)


def test_structured_jd_round_trips_through_json():
    jd = StructuredJobDescription(**VALID_JD)

    restored = StructuredJobDescription.model_validate_json(
        jd.model_dump_json()
    )

    assert restored == jd


def test_structured_jd_schema_stays_strict_output_compatible():
    schema = StructuredJobDescription.model_json_schema()
    definitions = [schema] + list(schema.get("$defs", {}).values())
    checked = 0

    for definition in definitions:
        if "properties" not in definition:
            continue

        assert definition["additionalProperties"] is False
        assert set(definition["properties"]) == set(definition["required"])
        checked += 1

    assert checked == 2