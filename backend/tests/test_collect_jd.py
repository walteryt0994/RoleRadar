import inspect
import json

import pytest

from app.jd_record import (
    JobDescriptionRecord,
    ParseMetadata,
    export_record,
    fingerprint_job_posting,
)
from app.schemas import StructuredJobDescription
from evaluation import collect_jd
from evaluation.batch_contract import (
    BATCH_APPROVED_BUDGET_USD,
    BATCH_APPROVED_REQUESTS,
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_REASONING_EFFORT,
    BATCH_REQUESTED_MODEL,
    RECORD_CONTRACT,
    estimated_cost,
)
from evaluation.collect_jd import (
    build_preview,
    known_usage,
    main,
    parse_arguments,
    pending_cases,
)
from evaluation.jd_cases import CASES

CASE_01 = CASES[0]

KNOWN_USAGE = (700, 150)

SENDING_NAMES = (
    "OpenAIProvider",
    "load_openai_config",
    "generate_text",
    "export_record",
    "api_key",
    "parse_job_description_with_fallback",
)


def _empty_answer():
    return json.dumps(
        {
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
    )


@pytest.fixture
def records_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr(collect_jd, "RECORDS_DIR", tmp_path)

    return tmp_path


def _saved_record(case, path, **metadata_overrides):
    values = {
        "generated_at": "2026-09-10T12:00:00Z",
        "returned_model": BATCH_REQUESTED_MODEL,
        "job_posting_sha256": fingerprint_job_posting(case["job_posting"]),
        "latency_seconds": 2.0,
        "input_tokens": KNOWN_USAGE[0],
        "output_tokens": KNOWN_USAGE[1],
        "total_tokens": None,
    }
    values.update(dict(RECORD_CONTRACT))
    values.update(metadata_overrides)
    record = JobDescriptionRecord(
        job_description=StructuredJobDescription.model_validate(
            json.loads(_empty_answer())
        ),
        metadata=ParseMetadata(**values),
    )

    return export_record(record, path)


def test_the_recorded_batch_settings_are_unchanged():
    assert BATCH_REQUESTED_MODEL == "gpt-5.6-luna"
    assert BATCH_MAX_OUTPUT_TOKENS == 900
    assert BATCH_REASONING_EFFORT == "none"
    assert BATCH_APPROVED_REQUESTS == 6
    assert BATCH_APPROVED_BUDGET_USD == 0.01


@pytest.mark.parametrize("name", SENDING_NAMES)
def test_the_module_source_cannot_send(name):
    assert name not in inspect.getsource(collect_jd)


@pytest.mark.parametrize("name", SENDING_NAMES)
def test_no_reachable_attribute_can_send(name):
    assert not hasattr(collect_jd, name)


def test_there_is_no_collection_entry_point():
    assert not hasattr(collect_jd, "collect")
    assert not hasattr(collect_jd, "preflight")


def test_sending_is_refused(records_dir):
    lines, status = build_preview(["--send"])

    assert status == 1
    assert any("refusing to run" in line for line in lines)
    assert any("cannot send a request" in line for line in lines)


def test_sending_is_refused_before_reading_records(records_dir, monkeypatch):
    def explode(directory):
        raise AssertionError("a refused run must not read records")

    monkeypatch.setattr(collect_jd, "load_records", explode)

    lines, status = build_preview(["--send"])

    assert status == 1


def test_sending_writes_nothing(records_dir):
    main(["collect_jd", "--send"])

    assert list(records_dir.glob("*.json")) == []


def test_a_preview_runs_without_any_openai_environment(records_dir):
    lines, status = build_preview([])

    assert status == 0
    assert any("is closed" in line for line in lines)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--only"],
        ["--onyl", "JD-01"],
        ["--only", "JD-99"],
        ["--extra"],
    ],
)
def test_invalid_arguments_exit_non_zero(arguments, records_dir):
    with pytest.raises(SystemExit) as error:
        main(["collect_jd"] + arguments)

    assert error.value.code != 0


def test_valid_arguments_are_parsed():
    options = parse_arguments(["--only", "JD-01", "--only", "JD-02"])

    assert options.send is False
    assert options.only == ["JD-01", "JD-02"]


def test_a_collected_case_is_reported_as_collected(records_dir):
    _saved_record(CASE_01, records_dir / "JD-01.json")
    loaded, _ = collect_jd.load_records(records_dir)
    lines = []

    remaining = pending_cases(loaded, lines)

    assert CASE_01 not in remaining
    assert any("collected JD-01" in line for line in lines)


def test_a_record_from_another_batch_does_not_count(records_dir):
    _saved_record(
        CASE_01,
        records_dir / "other.json",
        requested_model="another-model",
    )
    loaded, _ = collect_jd.load_records(records_dir)

    assert CASE_01 in pending_cases(loaded, [])


def test_known_usage_adds_up_this_batch_only(records_dir):
    _saved_record(CASE_01, records_dir / "JD-01.json")
    _saved_record(
        CASES[1],
        records_dir / "other.json",
        requested_model="another-model",
    )
    loaded, _ = collect_jd.load_records(records_dir)

    spent, unknown = known_usage(loaded, [])

    assert spent == pytest.approx(estimated_cost(*KNOWN_USAGE))
    assert unknown == 0


def test_a_record_without_usage_is_counted_as_unknown(records_dir):
    _saved_record(CASE_01, records_dir / "JD-01.json", output_tokens=None)
    loaded, _ = collect_jd.load_records(records_dir)
    lines = []

    spent, unknown = known_usage(loaded, lines)

    assert spent == 0.0
    assert unknown == 1
    assert any("no token counts" in line for line in lines)


def test_the_preview_never_reports_a_remaining_balance(records_dir):
    _saved_record(CASE_01, records_dir / "JD-01.json")

    lines, status = build_preview([])
    text = "\n".join(lines)

    assert "subtotal of the usage these records report" in text
    assert "no remaining balance is derived here" in text
    assert "budget remaining" not in text


def test_the_preview_names_the_uncollected_cases(records_dir):
    _saved_record(CASE_01, records_dir / "JD-01.json")

    lines, status = build_preview([])

    assert any("no record of this batch for JD-02" in line for line in lines)
    assert not any(
        "no record of this batch for JD-01" in line for line in lines
    )


def test_only_narrows_the_preview(records_dir):
    lines, status = build_preview(["--only", "JD-06"])

    named = [
        line for line in lines if line.startswith("no record of this batch")
    ]

    assert named == [
        f"no record of this batch for JD-06, "
        f"{len(CASES[5]['job_posting'])} characters of posting"
    ]


def test_an_unreadable_record_is_reported(records_dir):
    (records_dir / "broken.json").write_text("{not json", encoding="utf-8")

    lines, status = build_preview([])

    assert status == 0
    assert any("broken.json is unreadable" in line for line in lines)
