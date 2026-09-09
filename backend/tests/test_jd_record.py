import json
import re
import traceback

import pytest
from pydantic import ValidationError

from app.ai_provider import GenerationResult
from app.jd_record import (
    METADATA_FIELDS,
    RECORD_VERSION,
    JobDescriptionRecord,
    JobDescriptionRecordExistsError,
    JobDescriptionRecordFormatError,
    JobDescriptionRecordVersionError,
    ParseMetadata,
    export_record,
    fingerprint_job_posting,
    load_record,
    record_to_payload,
    utc_timestamp,
)
from app.schemas import SCHEMA_VERSION, StructuredJobDescription

RAW_TEXT_CANARY = "canary-raw-response-do-not-save"

JOB_POSTING = """Backend Engineer Intern
Northwind Robotics - Boston, MA (hybrid)

Requirements:
- Must be proficient in Python
"""


def _job_description():
    return StructuredJobDescription(
        job_title="Backend Engineer Intern",
        company="Northwind Robotics",
        location="Boston, MA",
        work_mode="hybrid",
        seniority="intern",
        minimum_experience=None,
        education_requirement=None,
        work_authorization=None,
        responsibilities=["Help build internal data services"],
        required_skills=[
            {
                "text": "Python",
                "evidence_text": "Must be proficient in Python",
            },
        ],
        preferred_skills=[],
        hard_constraints=[],
        uncertain_requirements=[],
    )


def _metadata(**overrides):
    values = {
        "generated_at": "2026-09-09T21:00:00Z",
        "prompt_version": "1",
        "parser_version": "1",
        "schema_version": SCHEMA_VERSION,
        "provider": "fake-provider",
        "requested_model": "test-model",
        "returned_model": "test-model-2026-01-01",
        "requested_max_output_tokens": 1500,
        "requested_reasoning_effort": "none",
        "job_posting_sha256": fingerprint_job_posting(JOB_POSTING),
        "latency_seconds": 1.5,
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
    }
    values.update(overrides)

    return ParseMetadata(**values)


def _record(**overrides):
    return JobDescriptionRecord(
        job_description=_job_description(),
        metadata=_metadata(),
        generation=GenerationResult(
            text='{"leaked": "' + RAW_TEXT_CANARY + '"}',
            provider="fake-provider",
            requested_model="test-model",
            model="test-model-2026-01-01",
            latency_seconds=1.5,
        ),
        **overrides,
    )


def test_fingerprint_is_stable_for_the_same_posting():
    assert fingerprint_job_posting(JOB_POSTING) == fingerprint_job_posting(
        JOB_POSTING
    )


def test_fingerprint_ignores_surrounding_whitespace():
    padded = "\n\n  " + JOB_POSTING + "  \n\n"

    assert fingerprint_job_posting(padded) == fingerprint_job_posting(
        JOB_POSTING
    )


def test_fingerprint_changes_when_the_posting_changes():
    edited = JOB_POSTING.replace("Python", "Kubernetes")

    assert fingerprint_job_posting(edited) != fingerprint_job_posting(
        JOB_POSTING
    )


def test_fingerprint_changes_when_inner_whitespace_changes():
    edited = JOB_POSTING.replace("Requirements:", "Requirements:   ")

    assert fingerprint_job_posting(edited) != fingerprint_job_posting(
        JOB_POSTING
    )


def test_fingerprint_is_a_sha256_hex_digest():
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint_job_posting(JOB_POSTING))


def test_utc_timestamp_uses_the_expected_utc_format():
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        utc_timestamp(),
    )


def test_metadata_fields_match_the_metadata_model():
    assert set(METADATA_FIELDS) == set(ParseMetadata.model_fields)


def test_payload_has_only_the_expected_top_level_keys():
    payload = record_to_payload(_record())

    assert sorted(payload) == [
        "job_description",
        "metadata",
        "record_version",
    ]
    assert payload["record_version"] == RECORD_VERSION


def test_payload_metadata_matches_the_allow_list():
    payload = record_to_payload(_record())

    assert set(payload["metadata"]) == set(METADATA_FIELDS)


def test_payload_never_contains_the_raw_provider_response():
    dumped = json.dumps(record_to_payload(_record()), ensure_ascii=False)

    assert RAW_TEXT_CANARY not in dumped
    assert "generation" not in dumped


def test_payload_is_json_serialisable_with_enum_values():
    payload = record_to_payload(_record())

    assert payload["job_description"]["work_mode"] == "hybrid"
    assert isinstance(json.dumps(payload), str)


def test_payload_ignores_a_missing_generation():
    record = JobDescriptionRecord(
        job_description=_job_description(),
        metadata=_metadata(),
    )

    assert record.generation is None
    assert record_to_payload(record) == record_to_payload(_record())


def test_export_record_writes_a_readable_json_file(tmp_path):
    target = tmp_path / "records" / "parse-001.json"

    returned = export_record(_record(), target)

    assert returned == target
    assert target.exists()

    raw = target.read_text(encoding="utf-8")

    assert raw.endswith("\n")
    assert json.loads(raw) == record_to_payload(_record())


def test_export_record_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "parse-001.json"

    export_record(_record(), target)

    assert target.exists()


def test_export_record_accepts_a_string_path(tmp_path):
    target = tmp_path / "parse-001.json"

    export_record(_record(), str(target))

    assert target.exists()


def test_export_record_refuses_to_overwrite(tmp_path):
    target = tmp_path / "parse-001.json"
    export_record(_record(), target)
    before = target.read_text(encoding="utf-8")
    different = JobDescriptionRecord(
        job_description=_job_description(),
        metadata=_metadata(prompt_version="99"),
    )

    with pytest.raises(JobDescriptionRecordExistsError):
        export_record(different, target)

    assert target.read_text(encoding="utf-8") == before
    assert "99" not in target.read_text(encoding="utf-8")


def test_load_record_round_trips_the_record(tmp_path):
    target = tmp_path / "parse-001.json"
    original = _record()
    export_record(original, target)

    loaded = load_record(target)

    assert loaded.job_description == original.job_description
    assert loaded.metadata == original.metadata
    assert loaded.generation is None


def test_load_record_rejects_a_missing_file(tmp_path):
    with pytest.raises(JobDescriptionRecordFormatError):
        load_record(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "content",
    ["not json at all", '["a list"]', "12", "null"],
)
def test_load_record_rejects_unusable_content(tmp_path, content):
    target = tmp_path / "parse-001.json"
    target.write_text(content, encoding="utf-8")

    with pytest.raises(JobDescriptionRecordFormatError):
        load_record(target)


def _write_mutated_record(tmp_path, mutate):
    payload = record_to_payload(_record())
    mutate(payload)
    target = tmp_path / "parse-001.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    return target


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("record_version", "99"),
        lambda payload: payload.pop("record_version"),
    ],
)
def test_load_record_rejects_an_unsupported_record_version(
    tmp_path, mutate
):
    target = _write_mutated_record(tmp_path, mutate)

    with pytest.raises(JobDescriptionRecordVersionError):
        load_record(target)


def test_load_record_rejects_an_unsupported_schema_version(tmp_path):
    target = _write_mutated_record(
        tmp_path,
        lambda payload: payload["metadata"].__setitem__(
            "schema_version", "99"
        ),
    )

    with pytest.raises(JobDescriptionRecordVersionError):
        load_record(target)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("metadata"),
        lambda payload: payload.pop("job_description"),
        lambda payload: payload["metadata"].pop("provider"),
        lambda payload: payload["metadata"].__setitem__(
            "experiment_id", "extra"
        ),
        lambda payload: payload["job_description"].__setitem__(
            "bogus_field", "extra"
        ),
        lambda payload: payload["job_description"].pop("required_skills"),
    ],
)
def test_load_record_rejects_a_broken_record(tmp_path, mutate):
    target = _write_mutated_record(tmp_path, mutate)

    with pytest.raises(JobDescriptionRecordFormatError):
        load_record(target)


def test_record_errors_do_not_leak_file_contents(tmp_path):
    target = tmp_path / "parse-001.json"
    target.write_text("not json " + RAW_TEXT_CANARY, encoding="utf-8")

    with pytest.raises(JobDescriptionRecordFormatError) as error_info:
        load_record(target)

    message = str(error_info.value)
    trace = "".join(
        traceback.format_exception(
            type(error_info.value),
            error_info.value,
            error_info.value.__traceback__,
        )
    )

    assert RAW_TEXT_CANARY not in message
    assert RAW_TEXT_CANARY not in trace


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": ""},
        {"provider": "   "},
        {"provider": None},
        {"provider": {"not": "a provider"}},
        {"requested_model": {"not": "a model"}},
        {"returned_model": None},
        {"prompt_version": None},
        {"parser_version": ""},
        {"schema_version": None},
        {"generated_at": "not-a-date"},
        {"generated_at": "2026-09-09 21:00:00"},
        {"generated_at": "2026-09-09T21:00:00+02:00"},
        {"generated_at": None},
        {"job_posting_sha256": "invalid"},
        {"job_posting_sha256": "A" * 64},
        {"job_posting_sha256": "a" * 63},
        {"input_tokens": -10},
        {"input_tokens": True},
        {"input_tokens": 1.5},
        {"output_tokens": "100"},
        {"total_tokens": -1},
        {"requested_max_output_tokens": 0},
        {"requested_max_output_tokens": -5},
        {"requested_max_output_tokens": True},
        {"requested_reasoning_effort": ""},
        {"requested_reasoning_effort": 3},
        {"latency_seconds": float("nan")},
        {"latency_seconds": float("inf")},
        {"latency_seconds": -1.0},
        {"latency_seconds": True},
        {"latency_seconds": "1.5"},
    ],
)
def test_metadata_rejects_invalid_values(overrides):
    with pytest.raises(ValidationError):
        _metadata(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        {
            "requested_max_output_tokens": None,
            "requested_reasoning_effort": None,
        },
        {"latency_seconds": 0.0},
        {"latency_seconds": 3},
        {"prompt_version": "0.9"},
        {"parser_version": "2026-08-01"},
        {"requested_reasoning_effort": "medium"},
        {"generated_at": utc_timestamp()},
    ],
)
def test_metadata_accepts_valid_values(overrides):
    assert _metadata(**overrides) is not None


def test_metadata_rejects_unexpected_fields():
    with pytest.raises(ValidationError):
        _metadata(experiment_id="extra")


@pytest.mark.parametrize(
    "overrides",
    [
        {"latency_seconds": float("nan")},
        {"job_posting_sha256": "invalid"},
        {"input_tokens": -10},
    ],
)
def test_load_record_rejects_invalid_metadata_values(tmp_path, overrides):
    payload = record_to_payload(_record())
    payload["metadata"].update(overrides)
    target = tmp_path / "parse-001.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobDescriptionRecordFormatError):
        load_record(target)


def test_load_record_rejects_unexpected_top_level_keys(tmp_path):
    payload = record_to_payload(_record())
    payload["experiment_id"] = "extra"
    target = tmp_path / "parse-001.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobDescriptionRecordFormatError):
        load_record(target)


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt_version": "0.9"},
        {"parser_version": "2026-08-01"},
    ],
)
def test_load_record_keeps_historical_behaviour_versions(
    tmp_path, overrides
):
    payload = record_to_payload(_record())
    payload["metadata"].update(overrides)
    target = tmp_path / "parse-001.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_record(target)

    for name, value in overrides.items():
        assert getattr(loaded.metadata, name) == value


def test_export_record_rejects_invalid_metadata_before_writing(tmp_path):
    broken = ParseMetadata.model_construct(
        **{
            **_metadata().model_dump(),
            "latency_seconds": float("nan"),
        }
    )
    record = JobDescriptionRecord(
        job_description=_job_description(),
        metadata=broken,
    )
    target = tmp_path / "parse-001.json"

    with pytest.raises(JobDescriptionRecordFormatError):
        export_record(record, target)

    assert not target.exists()


def test_metadata_errors_do_not_leak_the_broken_value(tmp_path):
    payload = record_to_payload(_record())
    payload["metadata"]["provider"] = RAW_TEXT_CANARY + "-broken"
    payload["metadata"]["job_posting_sha256"] = "invalid"
    target = tmp_path / "parse-001.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JobDescriptionRecordFormatError) as error_info:
        load_record(target)

    trace = "".join(
        traceback.format_exception(
            type(error_info.value),
            error_info.value,
            error_info.value.__traceback__,
        )
    )

    assert RAW_TEXT_CANARY not in str(error_info.value)
    assert RAW_TEXT_CANARY not in trace