import json

import pytest

from app.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    AIProviderConnectionError,
    AIProviderEmptyResponseError,
    AIProviderRateLimitError,
    AIProviderRefusalError,
    AIProviderResponseError,
    AIProviderTimeoutError,
    GenerationResult,
)
from app.jd_outcome import (
    FAILURE_CATEGORIES,
    FALLBACK_ERRORS,
    JobDescriptionOutcome,
    ParseFailureCategory,
    ParseOutcomeSource,
    parse_job_description_with_fallback,
)
from app.jd_service import JobDescriptionParseError
from app.parser import extract_skills
from evaluation.jd_cases import CASES

JOB_POSTING = CASES[0]["job_posting"]


def _model_output(**overrides):
    payload = {
        "job_title": "Data Analyst Intern",
        "company": "Northwind Labs",
        "location": "Boston, MA",
        "work_mode": "hybrid",
        "seniority": None,
        "minimum_experience": None,
        "education_requirement": None,
        "work_authorization": None,
        "responsibilities": [],
        "required_skills": [
            {
                "text": "SQL",
                "evidence_text": "Strong SQL skills",
            },
            {
                "text": "Python",
                "evidence_text": "Proficiency in Python",
            },
        ],
        "preferred_skills": [
            {
                "text": "Tableau",
                "evidence_text": "Experience with Tableau",
            },
            {
                "text": "AWS",
                "evidence_text": "Exposure to AWS",
            },
        ],
        "hard_constraints": [],
        "uncertain_requirements": [],
    }
    payload.update(overrides)

    return json.dumps(payload)


class FakeProvider(AIProvider):
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    def generate_text(
        self,
        prompt,
        max_output_tokens=None,
        reasoning_effort=None,
        json_schema_format=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "max_output_tokens": max_output_tokens,
                "reasoning_effort": reasoning_effort,
                "json_schema_format": json_schema_format,
            }
        )

        if self.error is not None:
            raise self.error

        return GenerationResult(
            text=self.text,
            provider="fake-provider",
            requested_model="test-model",
            model="test-model-2026-01-01",
            latency_seconds=1.5,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )


PROVIDER_FAILURES = (
    (AIProviderTimeoutError, ParseFailureCategory.TIMEOUT),
    (AIProviderConnectionError, ParseFailureCategory.CONNECTION),
    (AIProviderRateLimitError, ParseFailureCategory.RATE_LIMIT),
    (AIProviderResponseError, ParseFailureCategory.PROVIDER_RESPONSE),
    (AIProviderRefusalError, ParseFailureCategory.REFUSAL),
    (AIProviderEmptyResponseError, ParseFailureCategory.EMPTY_RESPONSE),
)

OUTPUT_FAILURES = (
    ("not json at all", ParseFailureCategory.INVALID_JSON),
    ('{"job_title": "only one field"}', ParseFailureCategory.SCHEMA),
    (
        _model_output(
            required_skills=[
                {
                    "text": "SQL",
                    "evidence_text": "a phrase the posting never uses",
                },
            ]
        ),
        ParseFailureCategory.EVIDENCE,
    ),
)


@pytest.fixture
def provider():
    return FakeProvider(text=_model_output())


@pytest.fixture
def rule_calls(monkeypatch):
    calls = []

    def counting_extract_skills(text):
        calls.append(text)

        return extract_skills(text)

    monkeypatch.setattr(
        "app.jd_outcome.extract_skills",
        counting_extract_skills,
    )

    return calls


@pytest.fixture
def record(provider):
    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    return outcome.record


def test_successful_parse_reports_the_llm_source(provider):
    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.source is ParseOutcomeSource.LLM


def test_successful_parse_keeps_the_structured_record(provider):
    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.record is not None
    assert outcome.record.job_description.job_title == "Data Analyst Intern"


def test_successful_parse_leaves_the_fallback_fields_empty(provider):
    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.rule_skills == ()
    assert outcome.failure_category is None


def test_successful_parse_never_calls_the_rule_parser(provider, rule_calls):
    parse_job_description_with_fallback(provider, JOB_POSTING)

    assert rule_calls == []


def test_successful_parse_sends_exactly_one_request(provider):
    parse_job_description_with_fallback(provider, JOB_POSTING)

    assert len(provider.calls) == 1


def test_call_options_reach_the_provider(provider):
    parse_job_description_with_fallback(
        provider,
        JOB_POSTING,
        max_output_tokens=300,
        reasoning_effort="none",
    )

    assert provider.calls[0]["max_output_tokens"] == 300
    assert provider.calls[0]["reasoning_effort"] == "none"


@pytest.mark.parametrize(
    (
        "error_type",
        "expected_category",
    ),
    PROVIDER_FAILURES,
)
def test_provider_failure_reports_its_category(error_type, expected_category):
    provider = FakeProvider(error=error_type("provider is unhappy"))

    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.source is ParseOutcomeSource.RULE_FALLBACK
    assert outcome.failure_category is expected_category


@pytest.mark.parametrize(
    (
        "error_type",
        "expected_category",
    ),
    PROVIDER_FAILURES,
)
def test_provider_failure_uses_the_rule_parser_once(
    error_type,
    expected_category,
    rule_calls,
):
    provider = FakeProvider(error=error_type("provider is unhappy"))

    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.record is None
    assert len(rule_calls) == 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    (
        "model_text",
        "expected_category",
    ),
    OUTPUT_FAILURES,
)
def test_unusable_output_reports_its_category(model_text, expected_category):
    provider = FakeProvider(text=model_text)

    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.source is ParseOutcomeSource.RULE_FALLBACK
    assert outcome.failure_category is expected_category


@pytest.mark.parametrize(
    (
        "model_text",
        "expected_category",
    ),
    OUTPUT_FAILURES,
)
def test_unusable_output_uses_the_rule_parser_once(
    model_text,
    expected_category,
    rule_calls,
):
    provider = FakeProvider(text=model_text)

    outcome = parse_job_description_with_fallback(provider, JOB_POSTING)

    assert outcome.record is None
    assert len(rule_calls) == 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[case["case_id"] for case in CASES],
)
def test_fallback_returns_the_frozen_rule_skills(case):
    provider = FakeProvider(error=AIProviderTimeoutError("too slow"))

    outcome = parse_job_description_with_fallback(
        provider,
        case["job_posting"],
    )

    assert outcome.rule_skills == case["rule_expected_skills"]


def test_an_empty_rule_list_is_still_a_fallback():
    provider = FakeProvider(error=AIProviderTimeoutError("too slow"))

    outcome = parse_job_description_with_fallback(
        provider,
        CASES[5]["job_posting"],
    )

    assert outcome.rule_skills == ()
    assert outcome.source is ParseOutcomeSource.RULE_FALLBACK
    assert outcome.failure_category is ParseFailureCategory.TIMEOUT


@pytest.mark.parametrize(
    "posting",
    [
        "",
        "   \n  ",
    ],
)
def test_an_empty_posting_is_not_a_fallback(posting, provider, rule_calls):
    with pytest.raises(JobDescriptionParseError) as error:
        parse_job_description_with_fallback(provider, posting)

    assert type(error.value) is JobDescriptionParseError
    assert provider.calls == []
    assert rule_calls == []


def test_a_configuration_error_is_not_a_fallback(rule_calls):
    provider = FakeProvider(error=AIProviderConfigError("no key"))

    with pytest.raises(AIProviderConfigError):
        parse_job_description_with_fallback(provider, JOB_POSTING)

    assert rule_calls == []


def test_an_unexpected_error_is_not_a_fallback(rule_calls):
    provider = FakeProvider(error=RuntimeError("a real bug"))

    with pytest.raises(RuntimeError):
        parse_job_description_with_fallback(provider, JOB_POSTING)

    assert rule_calls == []


def test_an_llm_outcome_needs_a_record():
    with pytest.raises(ValueError):
        JobDescriptionOutcome(source=ParseOutcomeSource.LLM)


def test_an_llm_outcome_rejects_rule_skills(record):
    with pytest.raises(ValueError):
        JobDescriptionOutcome(
            source=ParseOutcomeSource.LLM,
            record=record,
            rule_skills=("Python",),
        )


def test_an_llm_outcome_rejects_a_failure_category(record):
    with pytest.raises(ValueError):
        JobDescriptionOutcome(
            source=ParseOutcomeSource.LLM,
            record=record,
            failure_category=ParseFailureCategory.TIMEOUT,
        )


def test_a_fallback_outcome_needs_a_failure_category():
    with pytest.raises(ValueError):
        JobDescriptionOutcome(source=ParseOutcomeSource.RULE_FALLBACK)


def test_a_fallback_outcome_rejects_a_record(record):
    with pytest.raises(ValueError):
        JobDescriptionOutcome(
            source=ParseOutcomeSource.RULE_FALLBACK,
            record=record,
            failure_category=ParseFailureCategory.TIMEOUT,
        )


def test_the_outcome_source_must_be_an_enum_member(record):
    with pytest.raises(ValueError):
        JobDescriptionOutcome(source="llm", record=record)


def test_the_failure_table_maps_each_error_type_once():
    error_types = [error_type for error_type, _ in FAILURE_CATEGORIES]

    assert len(error_types) == len(set(error_types))


def test_the_failure_table_maps_each_category_once():
    categories = [category for _, category in FAILURE_CATEGORIES]

    assert len(categories) == len(set(categories))


def test_the_caught_errors_come_from_the_failure_table():
    assert FALLBACK_ERRORS == tuple(
        error_type for error_type, _ in FAILURE_CATEGORIES
    )
