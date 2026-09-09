import math
import os
import re
import time

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)


class AIProviderError(Exception):
    pass


class AIProviderConfigError(AIProviderError):
    pass


class AIProviderTimeoutError(AIProviderError):
    pass


class AIProviderConnectionError(AIProviderError):
    pass


class AIProviderRateLimitError(AIProviderError):
    pass


class AIProviderResponseError(AIProviderError):
    pass


class AIProviderEmptyResponseError(AIProviderError):
    pass


class AIProviderRefusalError(AIProviderError):
    pass


@dataclass(frozen=True)
class GenerationResult:
    text: str
    provider: str
    requested_model: str
    model: str
    latency_seconds: float
    requested_max_output_tokens: int | None = None
    requested_reasoning_effort: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class JsonSchemaFormat:
    name: str
    schema: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not SCHEMA_NAME_PATTERN.match(
            self.name
        ):
            raise AIProviderConfigError(
                "Schema name must be 1-64 characters of letters, digits, "
                "underscores, or hyphens."
            )

        if not isinstance(self.schema, dict) or not self.schema:
            raise AIProviderConfigError(
                "Schema must be a non-empty JSON Schema object."
            )


class AIProvider(ABC):
    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
    ) -> GenerationResult:
        pass


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True)
class OpenAIProviderConfig:
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise AIProviderConfigError("OPENAI_API_KEY is not set.")

        if not isinstance(self.model, str) or not self.model.strip():
            raise AIProviderConfigError("OPENAI_MODEL is not set.")

        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise AIProviderConfigError(
                "OPENAI_TIMEOUT_SECONDS must be a finite number "
                "greater than 0."
            )

        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise AIProviderConfigError(
                "OPENAI_MAX_RETRIES must be an integer 0 or greater."
            )


def _read_float_setting(name: str, default: float) -> float:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        return float(raw)
    except ValueError:
        raise AIProviderConfigError(f"{name} must be a number.") from None


def _read_int_setting(name: str, default: int) -> int:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        return int(raw)
    except ValueError:
        raise AIProviderConfigError(f"{name} must be an integer.") from None


def load_openai_config() -> OpenAIProviderConfig:
    return OpenAIProviderConfig(
        api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        model=os.getenv("OPENAI_MODEL", "").strip(),
        timeout_seconds=_read_float_setting(
            "OPENAI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        ),
        max_retries=_read_int_setting(
            "OPENAI_MAX_RETRIES", DEFAULT_MAX_RETRIES
        ),
    )


def _has_refusal(response: object) -> bool:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "refusal":
            return True

        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return True

    return False


class OpenAIProvider(AIProvider):
    PROVIDER_NAME = "openai"

    def __init__(self, config: OpenAIProviderConfig | None = None) -> None:
        if config is None:
            config = load_openai_config()

        self._config = config
        self._client = OpenAI(
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    def generate_text(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
    ) -> GenerationResult:
        request = {
            "model": self._config.model,
            "input": prompt,
            "store": False,
        }

        if max_output_tokens is not None:
            request["max_output_tokens"] = max_output_tokens

        if reasoning_effort is not None:
            request["reasoning"] = {"effort": reasoning_effort}

        if json_schema_format is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema_format.name,
                    "schema": json_schema_format.schema,
                    "strict": True,
                }
            }

        started_at = time.perf_counter()
        try:
            response = self._client.responses.create(**request)
        except APITimeoutError:
            raise AIProviderTimeoutError(
                "The provider request timed out."
            ) from None
        except APIConnectionError:
            raise AIProviderConnectionError(
                "Could not reach the provider."
            ) from None
        except RateLimitError:
            raise AIProviderRateLimitError(
                "The provider rate limit or quota was exceeded."
            ) from None
        except APIStatusError as error:
            raise AIProviderResponseError(
                f"The provider returned an error response "
                f"(HTTP {error.status_code})."
            ) from None
        except APIError:
            raise AIProviderResponseError(
                "The provider request failed."
            ) from None
        latency_seconds = time.perf_counter() - started_at

        status = getattr(response, "status", None)

        if status != "completed":
            raise AIProviderResponseError(
                "The provider returned an unfinished response."
            )

        if _has_refusal(response):
            raise AIProviderRefusalError(
                "The provider refused to generate text."
            )

        text = (response.output_text or "").strip()

        if not text:
            raise AIProviderEmptyResponseError(
                "The provider returned no usable text."
            )

        usage = getattr(response, "usage", None)

        return GenerationResult(
            text=text,
            provider=self.PROVIDER_NAME,
            requested_model=request["model"],
            model=response.model,
            latency_seconds=latency_seconds,
            requested_max_output_tokens=request.get("max_output_tokens"),
            requested_reasoning_effort=reasoning_effort,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )