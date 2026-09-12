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
from evaluation import batch_contract, collect_jd
from evaluation.batch_contract import (
    BATCH_BUDGET_USD,
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_MAX_REQUESTS,
    BATCH_REASONING_EFFORT,
    BATCH_REQUESTED_MODEL,
    RECORD_CONTRACT,
    estimated_cost,
    request_cost_upper_bound,
)
from evaluation.collect_jd import (
    collect,
    main,
    parse_arguments,
    pending_cases,
    preflight,
    spent_so_far,
)
from evaluation.jd_cases import CASES

CASE_01 = CASES[0]

CHEAP_USAGE = (700, 150)


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
    def __init__(self, fail_on=None, usage=CHEAP_USAGE):
        self.calls = []
        self.fail_on = fail_on
        self.usage = usage

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

        input_tokens, output_tokens = self.usage

        return GenerationResult(
            text=_empty_answer(),
            provider="openai",
            requested_model=BATCH_REQUESTED_MODEL,
            model=BATCH_REQUESTED_MODEL,
            latency_seconds=2.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=None,
        )


class ExplodingProvider:
    def __init__(self, config):
        raise AssertionError("no provider may be built on this path")


@pytest.fixture
def batch_env(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", BATCH_REQUESTED_MODEL)
    monkeypatch.setattr(collect_jd, "RECORDS_DIR", tmp_path)

    return tmp_path


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: fake)

    return fake


def _saved_record(case, path, **metadata_overrides):
    values = {
        "generated_at": "2026-09-10T12:00:00Z",
        "returned_model": BATCH_REQUESTED_MODEL,
        "job_posting_sha256": fingerprint_job_posting(case["job_posting"]),
        "latency_seconds": 2.0,
        "input_tokens": CHEAP_USAGE[0],
        "output_tokens": CHEAP_USAGE[1],
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


def test_the_batch_contract_is_unchanged():
    assert BATCH_REQUESTED_MODEL == "gpt-5.6-luna"
    assert BATCH_MAX_OUTPUT_TOKENS == 900
    assert BATCH_REASONING_EFFORT == "none"
    assert BATCH_MAX_REQUESTS == 6
    assert BATCH_BUDGET_USD == 0.01


@pytest.mark.parametrize(
    "arguments",
    [
        ["--send", "--only"],
        ["--send", "--onyl", "JD-01"],
        ["--send", "--only", "JD-99"],
        ["--send", "--extra"],
    ],
)
def test_invalid_arguments_exit_before_any_request(
    arguments,
    batch_env,
    monkeypatch,
):
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)

    with pytest.raises(SystemExit) as error:
        main(["collect_jd"] + arguments)

    assert error.value.code != 0
    assert list(batch_env.glob("*.json")) == []


def test_valid_arguments_are_parsed():
    options = parse_arguments(["--send", "--only", "JD-01", "--only", "JD-02"])

    assert options.send is True
    assert options.only == ["JD-01", "JD-02"]


def test_only_narrows_the_batch(batch_env, provider):
    main(["collect_jd", "--only", "JD-06", "--send"])

    assert len(provider.calls) == 1
    assert (batch_env / "JD-06.json").exists()


def test_a_dry_run_never_builds_a_provider(batch_env, monkeypatch):
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)

    assert main(["collect_jd"]) == 0
    assert list(batch_env.glob("*.json")) == []


def test_an_unapproved_model_refuses_to_run(batch_env, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "another-model")
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)
    lines = []

    config, plan = preflight(["--send"], lines)

    assert config is None
    assert any("refusing to run" in line for line in lines)


def test_changed_running_versions_refuse_to_run(batch_env, monkeypatch):
    monkeypatch.setattr(
        batch_contract,
        "RUNNING_CODE_VERSIONS",
        (("prompt_version", "2", "1"),),
    )
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)
    lines = []

    config, plan = preflight(["--send"], lines)

    assert config is None
    assert any("prompt_version" in line for line in lines)


def test_a_collected_case_is_skipped(batch_env):
    _saved_record(CASE_01, batch_env / "JD-01.json")
    loaded, _ = collect_jd.load_records(batch_env)

    remaining = pending_cases(loaded, [])

    assert CASE_01 not in remaining
    assert len(remaining) == len(CASES) - 1


def test_a_record_from_another_batch_is_still_pending(batch_env):
    _saved_record(
        CASE_01,
        batch_env / "other.json",
        requested_model="another-model",
    )
    loaded, _ = collect_jd.load_records(batch_env)

    assert CASE_01 in pending_cases(loaded, [])


def test_spent_so_far_adds_up_this_batch_only(batch_env):
    _saved_record(CASE_01, batch_env / "JD-01.json")
    _saved_record(
        CASES[1],
        batch_env / "other.json",
        requested_model="another-model",
    )
    loaded, _ = collect_jd.load_records(batch_env)

    spent = spent_so_far(loaded, [])

    assert spent == pytest.approx(estimated_cost(*CHEAP_USAGE))


def test_unknown_usage_is_not_counted_as_spent(batch_env):
    _saved_record(CASE_01, batch_env / "JD-01.json", output_tokens=None)
    loaded, _ = collect_jd.load_records(batch_env)
    lines = []

    spent = spent_so_far(loaded, lines)

    assert spent == 0.0
    assert any("no token counts" in line for line in lines)


def test_sending_forces_no_sdk_retry(batch_env, monkeypatch):
    captured = {}
    fake = FakeProvider()

    def build(config):
        captured["config"] = config

        return fake

    monkeypatch.setattr(collect_jd, "OpenAIProvider", build)

    main(["collect_jd", "--only", "JD-01", "--send"])

    assert captured["config"].max_retries == 0
    assert captured["config"].model == BATCH_REQUESTED_MODEL


def test_sending_uses_the_batch_call_options(batch_env, provider):
    main(["collect_jd", "--only", "JD-01", "--send"])

    assert provider.calls[0]["max_output_tokens"] == BATCH_MAX_OUTPUT_TOKENS
    assert provider.calls[0]["reasoning_effort"] == BATCH_REASONING_EFFORT


def test_a_zero_budget_sends_nothing(batch_env, monkeypatch):
    monkeypatch.setattr(collect_jd, "BATCH_BUDGET_USD", 0.0)
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)

    assert main(["collect_jd", "--send"]) == 1
    assert list(batch_env.glob("*.json")) == []


def test_a_budget_below_one_request_sends_nothing(batch_env, provider):
    cheapest = min(
        request_cost_upper_bound(case["job_posting"]) for case in CASES
    )
    collect_jd.BATCH_BUDGET_USD = cheapest / 2

    try:
        lines = []
        config, plan = preflight(["--send"], lines)
        sent = collect(config, plan, lines)
    finally:
        collect_jd.BATCH_BUDGET_USD = BATCH_BUDGET_USD

    assert sent == 0
    assert provider.calls == []
    assert any("does not cover its worst case" in line for line in lines)


def test_the_batch_stops_when_the_budget_runs_out(batch_env, provider):
    collect_jd.BATCH_BUDGET_USD = 0.0020

    try:
        lines = []
        config, plan = preflight(["--send"], lines)
        sent = collect(config, plan, lines)
    finally:
        collect_jd.BATCH_BUDGET_USD = BATCH_BUDGET_USD

    assert 0 < sent < len(CASES)
    assert len(provider.calls) == sent
    assert any("stopping before" in line for line in lines)


def test_a_full_budget_collects_every_case(batch_env, provider):
    main(["collect_jd", "--send"])

    assert len(provider.calls) == len(CASES)
    assert len(list(batch_env.glob("*.json"))) == len(CASES)


def test_unknown_usage_stops_the_batch(batch_env, monkeypatch):
    fake = FakeProvider(usage=(700, None))
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: fake)
    lines = []

    config, plan = preflight(["--send"], lines)
    sent = collect(config, plan, lines)

    assert sent == 1
    assert any("usage is unknown" in line for line in lines)


def test_a_failure_stops_the_remaining_cases(batch_env, monkeypatch):
    fake = FakeProvider(fail_on=2)
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: fake)
    lines = []

    config, plan = preflight(["--send"], lines)
    sent = collect(config, plan, lines)

    assert sent == 2
    assert len(fake.calls) == 2
    assert any("usage unknown" in line for line in lines)
    assert any("charged against the budget" in line for line in lines)


def test_a_failure_charges_its_worst_case_to_the_budget(
    batch_env,
    monkeypatch,
):
    fake = FakeProvider(fail_on=2)
    monkeypatch.setattr(collect_jd, "OpenAIProvider", lambda config: fake)
    lines = []

    config, plan = preflight(["--send"], lines)
    collect(config, plan, lines)

    expected = (
        BATCH_BUDGET_USD
        - estimated_cost(*CHEAP_USAGE)
        - request_cost_upper_bound(CASES[1]["job_posting"])
    )
    reported = [
        line for line in lines if line.startswith("budget remaining: ")
    ]

    assert reported == [f"budget remaining: ${expected:.6f}"]


def test_a_leftover_file_refuses_to_run(batch_env, monkeypatch):
    (batch_env / "JD-01.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)
    lines = []

    config, plan = preflight(["--send"], lines)

    assert config is None
    assert any("move it aside" in line for line in lines)


def test_more_cases_than_the_limit_refuse_to_run(batch_env, monkeypatch):
    monkeypatch.setattr(collect_jd, "BATCH_MAX_REQUESTS", 2)
    monkeypatch.setattr(collect_jd, "OpenAIProvider", ExplodingProvider)
    lines = []

    config, plan = preflight(["--send"], lines)

    assert config is None
    assert any("more cases than the batch limit" in line for line in lines)


def test_collect_stops_at_the_request_limit(batch_env, provider):
    lines = []
    config, plan = preflight(["--send"], lines)
    collect_jd.BATCH_MAX_REQUESTS = 2

    try:
        sent = collect(config, plan, lines)
    finally:
        collect_jd.BATCH_MAX_REQUESTS = BATCH_MAX_REQUESTS

    assert sent == 2
    assert any("reached the batch request limit" in line for line in lines)
