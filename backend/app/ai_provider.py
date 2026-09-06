import math
import os
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


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    latency_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class AIProvider(ABC):
    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
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


class OpenAIProvider(AIProvider):
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

        text = (response.output_text or "").strip()

        if not text:
            raise AIProviderEmptyResponseError(
                "The provider returned no usable text."
            )

        usage = getattr(response, "usage", None)

        return GenerationResult(
            text=text,
            model=response.model,
            latency_seconds=latency_seconds,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
        )