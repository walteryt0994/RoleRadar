import math
import os
import re
import time

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
    RateLimitError,
)


class SendState(StrEnum):
    """How far a request got, as far as its provider can tell."""

    # Stopped before it was handed to the SDK: nothing went out.
    NOT_SENT = "not_sent"
    # Handed to the SDK and no answer came back: it may have reached the service.
    HANDED = "handed"
    # The service answered, with an error or an unusable response included.
    ANSWERED = "answered"


@dataclass(frozen=True)
class Usage:
    """The token counts a service reported for one request and the tier it ran on; nothing it carried.

    input_tokens counts every input token, cache_read_tokens and
    cache_write_tokens among them (two separate parts of it); output_tokens
    counts every output token, reasoning_tokens among them; total_tokens is
    input plus output. cache_write_30m_tokens and cache_write_12h_tokens are
    the writes by how long they are kept, where the service says. A count the
    service did not report is None. malformed says that it reported
    something that is no count: such usage cannot be priced.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    service_tier: str | None = None
    malformed: bool = False
    cache_write_30m_tokens: int | None = None
    cache_write_12h_tokens: int | None = None


class AIProviderError(Exception):
    """A request that gave no usable text.

    sent says how far it got (by default: perhaps to the service); usage is
    what the service reported for it, if anything. Neither holds anything
    the request or the answer carried, and the message is a fixed text.
    """

    SENT = SendState.HANDED

    def __init__(self, *args, sent: SendState | None = None, usage: Usage | None = None):
        super().__init__(*args)
        self.sent = sent or self.SENT
        self.usage = usage


class AIProviderConfigError(AIProviderError):
    SENT = SendState.NOT_SENT


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


def went_out(sent: SendState, usage: Usage | None) -> bool:
    """Whether a request counts as sent: it went out, or may have.

    Only a request its provider stopped before handing it to the SDK, with
    nothing reported for it, did not; one that timed out or lost its
    connection may have reached the service. The ledger, the task and its
    pages all count by this one rule.
    """
    return sent is not SendState.NOT_SENT or usage is not None


def how_far(error: BaseException) -> tuple[SendState, Usage | None]:
    """How far a request that raised error got, and what was reported for it.

    An AIProviderError says; after any other error the request may have
    gone out, and nothing is known of what it used.
    """
    if isinstance(error, AIProviderError):
        return error.sent, error.usage

    return SendState.HANDED, None


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
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    service_tier: str | None = None
    usage_malformed: bool = False
    cache_write_30m_tokens: int | None = None
    cache_write_12h_tokens: int | None = None

    @property
    def usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
            total_tokens=self.total_tokens,
            service_tier=self.service_tier,
            malformed=self.usage_malformed,
            cache_write_30m_tokens=self.cache_write_30m_tokens,
            cache_write_12h_tokens=self.cache_write_12h_tokens,
        )


@dataclass(frozen=True)
class ProviderIdentity:
    """Where a provider's requests go, as it is configured: the service and
    the model a request asks for. That model may be an alias; the version a
    service answers with (GenerationResult.model) can be more specific.

    endpoint is the address requests go to when the configuration fixes it;
    None when it does not (the SDK then picks one, also from environment
    variables), so a task that names an endpoint refuses such a provider."""

    provider: str
    requested_model: str
    endpoint: str | None = None


SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SERVICE_TIER_PATTERN = re.compile(r"^[a-z_]{1,20}$")


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
        """Generate an answer. A provider behind the gate is also given
        timeout_seconds, the time its request may still take (app.live_gate)."""
        pass

    def count_input_tokens(
        self,
        prompt: str,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
        timeout_seconds: float | None = None,
    ) -> int:
        """How many input tokens the service counts for the request
        generate_text would send with these arguments; sending that request
        to the service is a request too. A provider that cannot count stops
        before anything is sent."""
        raise AIProviderConfigError("The provider cannot count input tokens.")

    def identity(self) -> ProviderIdentity | None:
        """The service and model the next request would go to, from the
        configuration alone: nothing is sent and no key is read. None when
        the provider cannot tell."""
        return None


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True)
class OpenAIProviderConfig:
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    # A fixed address for the requests; None leaves the choice to the SDK.
    base_url: str | None = None
    # False keeps proxy settings in the environment from rerouting requests.
    use_environment_proxies: bool = True
    # The most output tokens any one request may ask for; None leaves the
    # limit to each request.
    max_output_tokens: int | None = None
    # The service tier every request asks for; None leaves it to the service.
    service_tier: str | None = None

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

        if self.base_url is not None and (
            not isinstance(self.base_url, str)
            or not self.base_url.startswith("https://")
        ):
            raise AIProviderConfigError(
                "The provider address must be an https:// URL."
            )

        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens < 1
        ):
            raise AIProviderConfigError(
                "The output token limit must be an integer 1 or greater."
            )

        if self.service_tier is not None and (
            not isinstance(self.service_tier, str)
            or not SERVICE_TIER_PATTERN.match(self.service_tier)
        ):
            raise AIProviderConfigError(
                "The service tier must be a short lowercase name."
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


def is_count(value: object) -> bool:
    """Whether a reported token count is one: a whole number of 0 or more, never a bool."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _usage_of(response: object) -> Usage:
    """What a response says about its usage: token counts and the tier, nothing else.

    A count reported as anything but a whole number of 0 or more is left out
    and marks the usage malformed, so that it is never priced as though that
    count had not been reported.
    """
    usage = getattr(response, "usage", None)
    inputs = getattr(usage, "input_tokens_details", None)
    outputs = getattr(usage, "output_tokens_details", None)
    # The writes by how long they are kept. SDK 3.8.0 does not declare these and keeps them as a mapping.
    writes = getattr(inputs, "cache_write_tokens_details", None)
    reported = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_read_tokens": getattr(inputs, "cached_tokens", None),
        "cache_write_tokens": getattr(inputs, "cache_write_tokens", None),
        "reasoning_tokens": getattr(outputs, "reasoning_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
        "cache_write_30m_tokens": _detail(writes, "ttl_30m_tokens"),
        "cache_write_12h_tokens": _detail(writes, "ttl_12h_tokens"),
    }
    tier = getattr(response, "service_tier", None)

    return Usage(
        **{name: value if is_count(value) else None for name, value in reported.items()},
        service_tier=tier if isinstance(tier, str) else None,
        malformed=any(value is not None and not is_count(value) for value in reported.values()),
    )


def _detail(details: object, name: str) -> object:
    """One count of a usage detail, whether the SDK made the detail an object or kept it as a mapping."""
    if isinstance(details, dict):
        return details.get(name)

    return getattr(details, name, None)


def _has_refusal(response: object) -> bool:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "refusal":
            return True

        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return True

    return False


# What a count of the input takes of a generation request (POST /v1/responses/input_tokens, its
# reference checked 2026-11-03). The rest of the request, store, max_output_tokens and
# service_tier, is not among the count's parameters.
COUNTED = ("model", "input", "reasoning", "text")


class OpenAIProvider(AIProvider):
    PROVIDER_NAME = "openai"

    def __init__(self, config: OpenAIProviderConfig | None = None) -> None:
        if config is None:
            config = load_openai_config()

        self._config = config
        options = {}

        if config.base_url is not None:
            options["base_url"] = config.base_url

        if not config.use_environment_proxies:
            options["http_client"] = DefaultHttpxClient(
                timeout=config.timeout_seconds,
                trust_env=False,
            )

        self._client = OpenAI(
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
            **options,
        )

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider=self.PROVIDER_NAME,
            requested_model=self._config.model,
            endpoint=self._config.base_url,
        )

    def generate_text(
        self,
        prompt: str,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
        timeout_seconds: float | None = None,
    ) -> GenerationResult:
        request = self._request(
            prompt, max_output_tokens, reasoning_effort, json_schema_format
        )
        started_at = time.perf_counter()
        response = self._answer(
            self._client.responses.create, request, timeout_seconds
        )
        latency_seconds = time.perf_counter() - started_at
        # Answered: whatever comes of it, what it used is known as far as reported.
        usage = _usage_of(response)
        status = getattr(response, "status", None)

        if status != "completed":
            raise AIProviderResponseError(
                "The provider returned an unfinished response.",
                sent=SendState.ANSWERED,
                usage=usage,
            )

        if _has_refusal(response):
            raise AIProviderRefusalError(
                "The provider refused to generate text.",
                sent=SendState.ANSWERED,
                usage=usage,
            )

        text = (response.output_text or "").strip()

        if not text:
            raise AIProviderEmptyResponseError(
                "The provider returned no usable text.",
                sent=SendState.ANSWERED,
                usage=usage,
            )

        return GenerationResult(
            text=text,
            provider=self.PROVIDER_NAME,
            requested_model=request["model"],
            model=response.model,
            latency_seconds=latency_seconds,
            requested_max_output_tokens=request.get("max_output_tokens"),
            requested_reasoning_effort=reasoning_effort,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            service_tier=usage.service_tier,
            usage_malformed=usage.malformed,
            cache_write_30m_tokens=usage.cache_write_30m_tokens,
            cache_write_12h_tokens=usage.cache_write_12h_tokens,
        )

    def count_input_tokens(
        self,
        prompt: str,
        reasoning_effort: str | None = None,
        json_schema_format: JsonSchemaFormat | None = None,
        timeout_seconds: float | None = None,
    ) -> int:
        """The service's count of the input of the request generate_text
        would send: the same request, with only what the count takes
        (COUNTED). Errors and timeouts are told as generate_text tells them;
        a count that is not a whole number of 1 or more is refused."""
        request = self._request(
            prompt, None, reasoning_effort, json_schema_format
        )
        counted = {key: request[key] for key in COUNTED if key in request}
        response = self._answer(
            self._client.responses.input_tokens.count, counted, timeout_seconds
        )
        count = getattr(response, "input_tokens", None)

        if not is_count(count) or count < 1:
            raise AIProviderResponseError(
                "The provider returned no usable input token count.",
                sent=SendState.ANSWERED,
            )

        return count

    def _request(
        self,
        prompt: str,
        max_output_tokens: int | None,
        reasoning_effort: str | None,
        json_schema_format: JsonSchemaFormat | None,
    ) -> dict:
        """The one request both generate_text and count_input_tokens start from."""
        request = {
            "model": self._config.model,
            "input": prompt,
            "store": False,
        }

        # The caller's limit, never above the configured one.
        output_limit = self._config.max_output_tokens

        if max_output_tokens is not None:
            output_limit = (
                max_output_tokens
                if output_limit is None
                else min(max_output_tokens, output_limit)
            )

        if output_limit is not None:
            request["max_output_tokens"] = output_limit

        if reasoning_effort is not None:
            request["reasoning"] = {"effort": reasoning_effort}

        if self._config.service_tier is not None:
            request["service_tier"] = self._config.service_tier

        if json_schema_format is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": json_schema_format.name,
                    "schema": json_schema_format.schema,
                    "strict": True,
                }
            }

        return request

    def _answer(self, method, request: dict, timeout_seconds: float | None):
        """Hand one request to the SDK and tell its errors apart by how far it got.

        timeout_seconds, when given, is what this request may still take,
        never more than the configured timeout. Like that one, the SDK applies
        it to each part of the request (connecting, reading, …): it is no
        deadline on the whole."""
        options = {}

        if timeout_seconds is not None:
            options["timeout"] = min(
                timeout_seconds, self._config.timeout_seconds
            )

        try:
            return method(**request, **options)
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
                "The provider rate limit or quota was exceeded.",
                sent=SendState.ANSWERED,
            ) from None
        except APIStatusError as error:
            raise AIProviderResponseError(
                f"The provider returned an error response "
                f"(HTTP {error.status_code}).",
                sent=SendState.ANSWERED,
            ) from None
        except APIError:
            raise AIProviderResponseError(
                "The provider request failed."
            ) from None