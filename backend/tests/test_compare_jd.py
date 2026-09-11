import pytest

from app.jd_record import (
    JobDescriptionRecord,
    ParseMetadata,
    export_record,
    fingerprint_job_posting,
)
from app.schemas import StructuredJobDescription
from evaluation.compare_jd import (
    build_report,
    check_case,
    graded_check_count,
    load_records,
    mismatch_reason,
    select_records,
    _usage,
)
from evaluation.jd_cases import (
    CASES,
    EXPECTED_PARSER_VERSION,
    EXPECTED_PROMPT_VERSION,
    EXPECTED_SCHEMA_VERSION,
)

CASE_01, CASE_02, CASE_03, CASE_04, CASE_05, CASE_06 = CASES

FROZEN_GRADED_TOTAL = 104


def _answer(**overrides):
    payload = {
        "job_title": None,
        "company": None,
        "location": None,
        "work_mode": None,
        "seniority": None,
        "minimum_experience": None,
        "education_requirement": None,
        "work_authorization": None,
        "responsibilities": [],
        "required_skills": [],
        "preferred_skills": [],
        "hard_constraints": [],
        "uncertain_requirements": [],
    }
    payload.update(overrides)

    return StructuredJobDescription.model_validate(payload)


def _requirement(text):
    return {"text": text, "evidence_text": text}


def _answer_01(**overrides):
    payload = {
        "job_title": "Data Analyst Intern",
        "company": "Northwind Labs",
        "location": "Boston, MA",
        "work_mode": "hybrid",
        "required_skills": [_requirement("SQL"), _requirement("Python")],
        "preferred_skills": [_requirement("Tableau"), _requirement("AWS")],
    }
    payload.update(overrides)

    return _answer(**payload)


def _answer_04(**overrides):
    payload = {
        "job_title": "Junior Data Engineer",
        "company": "Fernwood Analytics",
        "work_mode": "remote",
        "seniority": "junior-level role",
        "required_skills": [_requirement("Python"), _requirement("SQL")],
        "preferred_skills": [_requirement("Docker")],
    }
    payload.update(overrides)

    return _answer(**payload)


def _answer_06(**overrides):
    payload = {
        "job_title": "Platform Engineer",
        "company": "Vireo Robotics",
        "location": "Denver, CO",
        "work_mode": "onsite",
        "responsibilities": ["mentor junior engineers"],
        "required_skills": [_requirement("Rust"), _requirement("Terraform")],
    }
    payload.update(overrides)

    return _answer(**payload)


def _metadata(posting, **overrides):
    values = {
        "generated_at": "2026-09-10T12:00:00Z",
        "prompt_version": EXPECTED_PROMPT_VERSION,
        "parser_version": EXPECTED_PARSER_VERSION,
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "provider": "fake-provider",
        "requested_model": "test-model",
        "returned_model": "test-model-2026-01-01",
        "requested_max_output_tokens": 1200,
        "requested_reasoning_effort": None,
        "job_posting_sha256": fingerprint_job_posting(posting),
        "latency_seconds": 2.5,
        "input_tokens": 420,
        "output_tokens": None,
        "total_tokens": None,
    }
    values.update(overrides)

    return ParseMetadata(**values)


def _record(case, job_description, **metadata_overrides):
    return JobDescriptionRecord(
        job_description=job_description,
        metadata=_metadata(case["job_posting"], **metadata_overrides),
    )


def test_a_correct_answer_reports_no_failure():
    assert check_case(CASE_01, _answer_01()) == []


def test_the_frozen_case_set_has_a_stable_check_count():
    total = sum(graded_check_count(case) for case in CASES)

    assert total == FROZEN_GRADED_TOTAL


@pytest.mark.parametrize(
    (
        "overrides",
        "expected_fragment",
    ),
    [
        ({"work_mode": "remote"}, "work_mode"),
        ({"location": "Denver, CO"}, "Boston"),
        ({"education_requirement": "Bachelor"}, "should stay null"),
        ({"responsibilities": ["something"]}, "should be empty"),
        ({"required_skills": [_requirement("Python")]}, "missing 'SQL'"),
        (
            {
                "required_skills": [
                    _requirement("SQL"),
                    _requirement("Python"),
                    _requirement("AWS"),
                ]
            },
            "must not contain 'AWS'",
        ),
    ],
)
def test_each_defect_is_reported_once(overrides, expected_fragment):
    failures = check_case(CASE_01, _answer_01(**overrides))

    assert len(failures) == 1
    assert expected_fragment in failures[0]


def test_a_negated_skill_is_reported_even_in_an_ungraded_list():
    answer = _answer_04(
        hard_constraints=[_requirement("Java is not needed")],
    )

    failures = check_case(CASE_04, answer)

    assert len(failures) == 1
    assert "'Java' must not appear in any requirement list" in failures[0]
    assert "hard_constraints" in failures[0]


def test_an_empty_required_list_is_reported():
    failures = check_case(CASE_06, _answer_06(required_skills=[]))

    assert "required_skills should not be empty" in failures


def test_a_missing_scalar_is_not_treated_as_a_match():
    failures = check_case(CASE_01, _answer_01(location=None))

    assert len(failures) == 1
    assert "Boston" in failures[0]


def test_a_record_for_another_posting_is_a_different_posting():
    record = _record(CASE_02, _answer_01())

    assert mismatch_reason(record, CASE_01) == "different posting"


def test_a_record_with_a_stale_prompt_version_is_rejected():
    record = _record(CASE_01, _answer_01(), prompt_version="0")

    reason = mismatch_reason(record, CASE_01)

    assert reason is not None
    assert "prompt_version" in reason


def test_a_record_with_a_stale_schema_version_is_rejected():
    record = _record(CASE_01, _answer_01(), schema_version="0")

    reason = mismatch_reason(record, CASE_01)

    assert reason is not None
    assert "schema_version" in reason


def test_a_matching_record_has_no_mismatch_reason():
    record = _record(CASE_01, _answer_01())

    assert mismatch_reason(record, CASE_01) is None


def test_only_same_version_records_are_selected(tmp_path):
    loaded = [
        (tmp_path / "stale.json", _record(CASE_01, _answer_01(),
                                          prompt_version="0")),
        (tmp_path / "other.json", _record(CASE_02, _answer_01())),
        (tmp_path / "good.json", _record(CASE_01, _answer_01())),
    ]

    matched, rejected = select_records(CASE_01, loaded)

    assert [path.name for path, _ in matched] == ["good.json"]
    assert [path.name for path, _ in rejected] == ["stale.json"]


def test_a_missing_records_directory_is_not_an_error(tmp_path):
    loaded, unreadable = load_records(tmp_path / "nothing-here")

    assert loaded == []
    assert unreadable == []


def test_a_corrupt_record_is_listed_as_unreadable(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    loaded, unreadable = load_records(tmp_path)

    assert loaded == []
    assert [path.name for path, _ in unreadable] == ["broken.json"]


def test_a_saved_record_is_read_back(tmp_path):
    export_record(_record(CASE_01, _answer_01()), tmp_path / "jd01.json")

    loaded, unreadable = load_records(tmp_path)

    assert unreadable == []
    assert len(loaded) == 1
    assert mismatch_reason(loaded[0][1], CASE_01) is None


def test_missing_usage_is_reported_as_unknown():
    assert _usage(None) == "unknown"


def test_zero_usage_is_not_reported_as_unknown():
    assert _usage(0) == "0"


def test_a_report_without_records_measures_nothing(tmp_path):
    lines = build_report(tmp_path)

    assert "  cases measured: 0 of 6" in lines
    assert sum("NOT MEASURED" in line for line in lines) == len(CASES)


def test_a_report_counts_a_correct_record(tmp_path):
    export_record(_record(CASE_01, _answer_01()), tmp_path / "jd01.json")

    lines = build_report(tmp_path)

    assert "  cases measured: 1 of 6" in lines
    assert "  graded checks on measured cases: 17 passed, 0 failed" in lines


def test_a_report_counts_a_wrong_record(tmp_path):
    answer = _answer_01(required_skills=[_requirement("Python")])
    export_record(_record(CASE_01, answer), tmp_path / "jd01.json")

    lines = build_report(tmp_path)

    assert "  graded checks on measured cases: 16 passed, 1 failed" in lines


def test_two_matching_records_are_not_measured(tmp_path):
    export_record(_record(CASE_01, _answer_01()), tmp_path / "a.json")
    export_record(_record(CASE_01, _answer_01()), tmp_path / "b.json")

    lines = build_report(tmp_path)

    assert any("AMBIGUOUS" in line for line in lines)
    assert "  cases measured: 0 of 6" in lines


def test_a_stale_record_is_named_in_the_report(tmp_path):
    export_record(
        _record(CASE_01, _answer_01(), prompt_version="0"),
        tmp_path / "stale.json",
    )

    lines = build_report(tmp_path)

    assert any("skipped stale.json" in line for line in lines)
    assert "  cases measured: 0 of 6" in lines


def test_the_report_never_claims_an_accuracy_rate(tmp_path):
    export_record(_record(CASE_01, _answer_01()), tmp_path / "jd01.json")

    text = "\n".join(build_report(tmp_path)).lower()

    assert "precision" not in text
    assert "recall" not in text
    assert "accuracy rate" in text


def test_the_report_marks_unknown_usage(tmp_path):
    export_record(_record(CASE_01, _answer_01()), tmp_path / "jd01.json")

    call_lines = [
        line for line in build_report(tmp_path) if line.startswith("  call")
    ]

    assert len(call_lines) == 1
    assert "out unknown" in call_lines[0]


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[case["case_id"] for case in CASES],
)
def test_every_case_lists_its_skipped_fields(case, tmp_path):
    lines = build_report(tmp_path)

    for field in case["not_graded"]:
        assert any(
            line.startswith(f"  skip  : {field},") for line in lines
        )
