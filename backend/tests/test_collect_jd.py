import json

import pytest

from app.ai_provider import (
    AIProvider,
    AIProviderTimeoutError,
    GenerationResult,
)
from app.jd_record import (
    JobDescriptionRecord,
    ParseMetadata,
    export_record,
    fingerprint_job_posting,
)
from app.schemas import StructuredJobDescription
from evaluation import collect_jd
from evaluation.collect_jd import (
    APPROVED_MAX_OUTPUT_TOKENS,
    APPROVED_MAX_REQUESTS,
    APPROVED_MODEL,
    APPROVED_REASONING_EFFORT,
    collect,
    main,
    pending_cases,
    preflight,
)
from evaluation.jd_cases import (
    CASES,
    EXPECTED_PARSER_VERSION,
    EXPECTED_PROMPT_VERSION,
    EXPECTED_SCHEMA_VERSION,
)

CASE_01 = CASES[0]


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


class FakeProvider(AIProvider):
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def generate_text(
        self,
        prompt,
        max_output_tokens=None,
        reasoning_effort=None,
        json_schema_format=None,
    ):
        self.calls.append(
            {
                "max_output_tokens": max_output_tokens,
                "reasoning_effort": reasoning_effort,
            }
        )

        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise AIProviderTimeoutError("too slow")

        return GenerationResult(
            text=_empty_answer(),
            provider="openai",
            requested_model=APPROVED_MODEL,
            model=APPROVED_MODEL,
            latency_seconds=2.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
            input_tokens=700,
            output_tokens=150,
            total_tokens=850,
        )


@pytest.fixture
def approved_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", APPROVED_MODEL)
    monkeypatch.setattr(collect_jd, "RECORDS_DIR", tmp_path)

    return tmp_path


def _saved_record(case, path):
    metadata = ParseMetadata(
        generated_at="2026-09-10T12:00:00Z",
        prompt_version=EXPECTED_PROMPT_VERSION,
        parser_version=EXPECTED_PARSER_VERSION,
        schema_version=EXPECTED_SCHEMA_VERSION,
        provider="openai",
        requested_model=APPROVED_MODEL,
        returned_model=APPROVED_MODEL,
        requested_max_output_tokens=APPROVED_MAX_OUTPUT_TOKENS,
        requested_reasoning_effort=APPROVED_REASONING_EFFORT,
        job_posting_sha256=fingerprint_job_posting(case["job_posting"]),
        latency_seconds=2.0,
        input_tokens=700,
        output_tokens=150,
        total_tokens=850,
    )
    record = JobDescriptionRecord(
        job_description=StructuredJobDescription.model_validate(
            json.loads(_empty_answer())
        ),
        metadata=metadata,
    )

    return export_record(record, path)


def test_the_approved_limits_are_unchanged():
    assert APPROVED_MODEL == "gpt-5.6-luna"
    assert APPROVED_MAX_OUTPUT_TOKENS == 900
    assert APPROVED_REASONING_EFFORT == "none"
    assert APPROVED_MAX_REQUESTS == 6


def test_a_collected_case_is_skipped(approved_env):
    _saved_record(CASE_01, approved_env / "JD-01.json")
    loaded, _ = collect_jd.load_records(approved_env)

    remaining = pending_cases(loaded, [])

    assert CASE_01 not in remaining
    assert len(remaining) == len(CASES) - 1


def test_every_case_is_pending_without_records(approved_env):
    assert pending_cases([], []) == list(CASES)


def test_an_unapproved_model_refuses_to_run(approved_env, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "some-other-model")
    lines = []

    config, cases = preflight(["collect_jd"], lines)

    assert config is None
    assert cases is None
    assert any("refusing to run" in line for line in lines)


def test_the_approved_model_passes_preflight(approved_env):
    config, cases = preflight(["collect_jd"], [])

    assert config.model == APPROVED_MODEL
    assert len(cases) == len(CASES)


def test_only_narrows_the_batch(approved_env):
    lines = []

    _, cases = preflight(["collect_jd", "--only", "JD-06"], lines)

    assert [case["case_id"] for case in cases] == ["JD-06"]
    assert any("limited to JD-06" in line for line in lines)


def test_an_unknown_case_id_refuses_to_run(approved_env):
    lines = []

    config, cases = preflight(["collect_jd", "--only", "JD-99"], lines)

    assert config is None
    assert any("unknown case id" in line for line in lines)


def test_a_leftover_file_refuses_to_run(approved_env):
    (approved_env / "JD-01.json").write_text("{}", encoding="utf-8")
    lines = []

    config, cases = preflight(["collect_jd"], lines)

    assert config is None
    assert any("move it aside" in line for line in lines)


def test_a_dry_run_never_builds_a_provider(approved_env, monkeypatch):
    def explode(config):
        raise AssertionError("a dry run must not build a provider")

    monkeypatch.setattr(collect_jd, "OpenAIProvider", explode)

    assert main(["collect_jd"]) == 0
    assert list(approved_env.glob("*.json")) == []


def test_sending_forces_no_sdk_retry(approved_env, monkeypatch):
    captured = {}
    provider = FakeProvider()

    def build(config):
        captured["config"] = config

        return provider

    monkeypatch.setattr(collect_jd, "OpenAIProvider", build)

    main(["collect_jd", "--only", "JD-01", "--send"])

    assert captured["config"].max_retries == 0
    assert captured["config"].model == APPROVED_MODEL


def test_sending_uses_the_approved_call_options(approved_env, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: provider)

    main(["collect_jd", "--only", "JD-01", "--send"])

    assert len(provider.calls) == 1
    assert provider.calls[0]["max_output_tokens"] == APPROVED_MAX_OUTPUT_TOKENS
    assert provider.calls[0]["reasoning_effort"] == APPROVED_REASONING_EFFORT
    assert (approved_env / "JD-01.json").exists()


def test_a_failure_stops_the_remaining_cases(approved_env, monkeypatch):
    provider = FakeProvider(fail_on=2)
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: provider)
    lines = []

    config, cases = preflight(["collect_jd"], lines)
    sent = collect(config, cases, lines)

    assert sent == 2
    assert len(provider.calls) == 2
    assert sorted(p.name for p in approved_env.glob("*.json")) == [
        "JD-01.json"
    ]
    assert any("usage unknown" in line for line in lines)


def test_more_cases_than_the_limit_refuse_to_run(
    approved_env,
    monkeypatch,
):
    def explode(config):
        raise AssertionError("preflight must refuse before this")

    monkeypatch.setattr(collect_jd, "OpenAIProvider", explode)
    monkeypatch.setattr(collect_jd, "APPROVED_MAX_REQUESTS", 2)
    lines = []

    config, cases = preflight(["collect_jd"], lines)

    assert config is None
    assert any("more cases than the approved limit" in l for l in lines)


def test_collect_stops_at_the_request_limit(approved_env, monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: provider)
    lines = []
    config, cases = preflight(["collect_jd"], lines)

    monkeypatch.setattr(collect_jd, "APPROVED_MAX_REQUESTS", 2)
    sent = collect(config, cases, lines)

    assert sent == 2
    assert len(provider.calls) == 2
    assert any("reached the approved request limit" in l for l in lines)
