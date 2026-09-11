from dataclasses import dataclass
from enum import Enum

from app.ai_provider import (
    AIProvider,
    AIProviderConnectionError,
    AIProviderEmptyResponseError,
    AIProviderRateLimitError,
    AIProviderRefusalError,
    AIProviderResponseError,
    AIProviderTimeoutError,
)
from app.jd_record import JobDescriptionRecord
from app.jd_service import (
    JobDescriptionEvidenceError,
    JobDescriptionInvalidJsonError,
    JobDescriptionSchemaError,
    parse_job_description,
)
from app.parser import extract_skills


class ParseOutcomeSource(str, Enum):
    LLM = "llm"
    RULE_FALLBACK = "rule_fallback"


class ParseFailureCategory(str, Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    PROVIDER_RESPONSE = "provider_response"
    REFUSAL = "refusal"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    SCHEMA = "schema"
    EVIDENCE = "evidence"


FAILURE_CATEGORIES = (
    (AIProviderTimeoutError, ParseFailureCategory.TIMEOUT),
    (AIProviderConnectionError, ParseFailureCategory.CONNECTION),
    (AIProviderRateLimitError, ParseFailureCategory.RATE_LIMIT),
    (AIProviderResponseError, ParseFailureCategory.PROVIDER_RESPONSE),
    (AIProviderRefusalError, ParseFailureCategory.REFUSAL),
    (AIProviderEmptyResponseError, ParseFailureCategory.EMPTY_RESPONSE),
    (JobDescriptionInvalidJsonError, ParseFailureCategory.INVALID_JSON),
    (JobDescriptionSchemaError, ParseFailureCategory.SCHEMA),
    (JobDescriptionEvidenceError, ParseFailureCategory.EVIDENCE),
)

FALLBACK_ERRORS = tuple(error_type for error_type, _ in FAILURE_CATEGORIES)


@dataclass(frozen=True)
class JobDescriptionOutcome:
    source: ParseOutcomeSource
    record: JobDescriptionRecord | None = None
    rule_skills: tuple[str, ...] = ()
    failure_category: ParseFailureCategory | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, ParseOutcomeSource):
            raise ValueError(
                "The outcome source must be a ParseOutcomeSource."
            )

        if self.source is ParseOutcomeSource.LLM:
            if self.record is None:
                raise ValueError(
                    "An llm outcome must carry a parsed record."
                )

            if self.failure_category is not None or self.rule_skills:
                raise ValueError(
                    "An llm outcome must not carry fallback results."
                )

            return

        if self.record is not None:
            raise ValueError(
                "A rule_fallback outcome must not carry a parsed record."
            )

        if self.failure_category is None:
            raise ValueError(
                "A rule_fallback outcome must name a failure category."
            )


def _failure_category(error: Exception) -> ParseFailureCategory:
    for error_type, category in FAILURE_CATEGORIES:
        if isinstance(error, error_type):
            return category

    raise error


def parse_job_description_with_fallback(
    provider: AIProvider,
    job_description_text: str,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> JobDescriptionOutcome:
    try:
        record = parse_job_description(
            provider,
            job_description_text,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
    except FALLBACK_ERRORS as error:
        return JobDescriptionOutcome(
            source=ParseOutcomeSource.RULE_FALLBACK,
            rule_skills=tuple(extract_skills(job_description_text)),
            failure_category=_failure_category(error),
        )

    return JobDescriptionOutcome(
        source=ParseOutcomeSource.LLM,
        record=record,
    )
