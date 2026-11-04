import traceback
import types
from unittest.mock import MagicMock, patch

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from app.ai_provider import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    AIProvider,
    AIProviderConfigError,
    AIProviderConnectionError,
    AIProviderEmptyResponseError,
    AIProviderError,
    AIProviderRateLimitError,
    AIProviderRefusalError,
    AIProviderResponseError,
    AIProviderTimeoutError,
    CacheWriteSplit,
    JsonSchemaFormat,
    OpenAIProvider,
    OpenAIProviderConfig,
    ProviderIdentity,
    SendState,
    Usage,
    load_openai_config,
)

TEST_CONFIG = OpenAIProviderConfig(
    api_key="test-key-not-real",
    model="test-model",
    timeout_seconds=30.0,
    max_retries=2,
)


@pytest.fixture
def provider():
    with patch("app.ai_provider.OpenAI"):
        yield OpenAIProvider(config=TEST_CONFIG)


def _fake_response(
    output_text="Python, SQL",
    status="completed",
    incomplete_reason=None,
    output=None,
):
    response = MagicMock()
    response.output_text = output_text
    response.model = "test-model-2026-01-01"
    response.status = status
    response.output = output if output is not None else []
    response.usage = types.SimpleNamespace(
        input_tokens=42,
        output_tokens=7,
        total_tokens=49,
    )

    if incomplete_reason is None:
        response.incomplete_details = None
    else:
        response.incomplete_details = types.SimpleNamespace(
            reason=incomplete_reason,
        )

    return response


def _fake_truncated_response():
    return _fake_response(
        output_text="Python, SQL, Doc",
        status="incomplete",
        incomplete_reason="max_output_tokens",
    )


def _fake_failed_response():
    return _fake_response(
        output_text="",
        status="failed",
    )


def test_generate_text_returns_text_model_and_usage(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")

    assert result.text == "Python, SQL"
    assert result.model == "test-model-2026-01-01"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.total_tokens == 49


def test_generate_text_strips_surrounding_whitespace(provider):
    provider._client.responses.create.return_value = _fake_response(
        output_text="  Python, SQL  \n"
    )

    result = provider.generate_text("Parse this job description.")

    assert result.text == "Python, SQL"


def test_generate_text_measures_latency(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")

    assert result.latency_seconds > 0


def test_generate_text_sends_configured_model_and_prompt(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text("Parse this job description.")

    request = provider._client.responses.create.call_args.kwargs

    assert request["model"] == "test-model"
    assert request["input"] == "Parse this job description."


def test_generate_text_disables_response_storage(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text("Parse this job description.")

    request = provider._client.responses.create.call_args.kwargs

    assert request["store"] is False


def test_generate_text_omits_max_output_tokens_by_default(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text("Parse this job description.")

    request = provider._client.responses.create.call_args.kwargs

    assert "max_output_tokens" not in request


def test_generate_text_sends_max_output_tokens_when_given(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        max_output_tokens=300,
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["max_output_tokens"] == 300


def test_generate_text_returns_none_tokens_when_usage_missing(provider):
    response = _fake_response()
    del response.usage
    provider._client.responses.create.return_value = response

    result = provider.generate_text("Parse this job description.")

    assert result.text == "Python, SQL"
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None


def _fake_request() -> httpx2.Request:
    return httpx2.Request(
        "POST",
        "https://api.openai.com/v1/responses",
    )


def _fake_http_response(status_code: int) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        request=_fake_request(),
        json={"error": {"message": "boom"}},
    )


def test_generate_text_translates_timeout_error(provider):
    provider._client.responses.create.side_effect = APITimeoutError(
        _fake_request()
    )

    with pytest.raises(AIProviderTimeoutError):
        provider.generate_text("Parse this job description.")


def test_generate_text_translates_connection_error(provider):
    provider._client.responses.create.side_effect = APIConnectionError(
        request=_fake_request()
    )

    with pytest.raises(AIProviderConnectionError):
        provider.generate_text("Parse this job description.")


def test_generate_text_translates_rate_limit_error(provider):
    provider._client.responses.create.side_effect = RateLimitError(
        "rate limited",
        response=_fake_http_response(429),
        body=None,
    )

    with pytest.raises(AIProviderRateLimitError):
        provider.generate_text("Parse this job description.")


def test_generate_text_translates_api_status_error(provider):
    provider._client.responses.create.side_effect = APIStatusError(
        "server error",
        response=_fake_http_response(500),
        body=None,
    )

    with pytest.raises(AIProviderResponseError) as error_info:
        provider.generate_text("Parse this job description.")

    assert "500" in str(error_info.value)


def test_timeout_error_is_not_reported_as_connection_error(provider):
    provider._client.responses.create.side_effect = APITimeoutError(
        _fake_request()
    )

    with pytest.raises(AIProviderTimeoutError) as error_info:
        provider.generate_text("Parse this job description.")

    assert not isinstance(error_info.value, AIProviderConnectionError)


@pytest.mark.parametrize("output_text", ["", "   \n  ", None])
def test_generate_text_rejects_empty_output(provider, output_text):
    provider._client.responses.create.return_value = _fake_response(
        output_text=output_text
    )

    with pytest.raises(AIProviderEmptyResponseError):
        provider.generate_text("Parse this job description.")


LEAK_CANARY = "canary-sk-do-not-log-this-value"

CANARY_CONFIG = OpenAIProviderConfig(
    api_key=LEAK_CANARY,
    model="test-model",
    timeout_seconds=30.0,
    max_retries=2,
)


@pytest.fixture
def canary_provider():
    with patch("app.ai_provider.OpenAI"):
        yield OpenAIProvider(config=CANARY_CONFIG)


def _canary_http_response(status_code: int) -> httpx2.Response:
    return httpx2.Response(
        status_code,
        request=httpx2.Request(
            "POST",
            "https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer " + LEAK_CANARY},
        ),
        json={"error": {"message": "invalid key " + LEAK_CANARY}},
    )


def _formatted_traceback(error: BaseException) -> str:
    return "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )


@pytest.mark.parametrize(
    "sdk_error_factory",
    [
        lambda: APITimeoutError(_fake_request()),
        lambda: APIConnectionError(request=_fake_request()),
        lambda: RateLimitError(
            "rate limited " + LEAK_CANARY,
            response=_canary_http_response(429),
            body=None,
        ),
        lambda: APIStatusError(
            "unauthorized " + LEAK_CANARY,
            response=_canary_http_response(401),
            body=None,
        ),
    ],
)
def test_provider_errors_do_not_leak_the_api_key(
    canary_provider, sdk_error_factory
):
    create = canary_provider._client.responses.create
    create.side_effect = sdk_error_factory()

    with pytest.raises(AIProviderError) as error_info:
        canary_provider.generate_text("Parse this job description.")

    assert LEAK_CANARY not in str(error_info.value)
    assert LEAK_CANARY not in _formatted_traceback(error_info.value)


def test_provider_config_repr_hides_the_api_key():
    assert LEAK_CANARY not in repr(CANARY_CONFIG)
    assert LEAK_CANARY not in str(CANARY_CONFIG)
    assert LEAK_CANARY not in f"{CANARY_CONFIG}"


def test_timeout_error_message_promises_no_time_bound(provider):
    provider._client.responses.create.side_effect = APITimeoutError(
        _fake_request()
    )

    with pytest.raises(AIProviderTimeoutError) as error_info:
        provider.generate_text("Parse this job description.")

    message = str(error_info.value)

    assert not any(character.isdigit() for character in message)


def _clear_openai_env(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_RETRIES",
    ):
        monkeypatch.delenv(name, raising=False)


def test_load_openai_config_applies_defaults(monkeypatch):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    config = load_openai_config()

    assert config.api_key == "test-key-not-real"
    assert config.model == "test-model"
    assert config.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    assert config.max_retries == DEFAULT_MAX_RETRIES


def test_load_openai_config_reads_all_settings(monkeypatch):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "0")

    config = load_openai_config()

    assert config.timeout_seconds == 12.5
    assert config.max_retries == 0


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_load_openai_config_rejects_missing_api_key(monkeypatch, api_key):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "test-model")

    if api_key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", api_key)

    with pytest.raises(AIProviderConfigError) as error_info:
        load_openai_config()

    assert "OPENAI_API_KEY" in str(error_info.value)


@pytest.mark.parametrize("model", [None, "", "   "])
def test_load_openai_config_rejects_missing_model(monkeypatch, model):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")

    if model is not None:
        monkeypatch.setenv("OPENAI_MODEL", model)

    with pytest.raises(AIProviderConfigError) as error_info:
        load_openai_config()

    assert "OPENAI_MODEL" in str(error_info.value)


@pytest.mark.parametrize(
    "timeout_seconds",
    ["abc", "0", "-1", "nan", "inf"],
)
def test_load_openai_config_rejects_invalid_timeout(
    monkeypatch, timeout_seconds
):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", timeout_seconds)

    with pytest.raises(AIProviderConfigError) as error_info:
        load_openai_config()

    assert "OPENAI_TIMEOUT_SECONDS" in str(error_info.value)


@pytest.mark.parametrize("max_retries", ["abc", "2.5", "-1"])
def test_load_openai_config_rejects_invalid_retries(
    monkeypatch, max_retries
):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", max_retries)

    with pytest.raises(AIProviderConfigError) as error_info:
        load_openai_config()

    assert "OPENAI_MAX_RETRIES" in str(error_info.value)


def test_config_errors_do_not_leak_the_api_key_value(monkeypatch):
    _clear_openai_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "leak-canary-value")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "abc")

    with pytest.raises(AIProviderConfigError) as error_info:
        load_openai_config()

    assert "leak-canary-value" not in str(error_info.value)


UNFINISHED_MESSAGE = "The provider returned an unfinished response."


def test_generate_text_rejects_truncated_response(provider):
    create = provider._client.responses.create
    create.return_value = _fake_truncated_response()

    with pytest.raises(AIProviderResponseError) as error_info:
        provider.generate_text("Parse this job description.")

    assert str(error_info.value) == UNFINISHED_MESSAGE
    assert create.call_count == 1


def test_generate_text_rejects_failed_response(provider):
    create = provider._client.responses.create
    create.return_value = _fake_failed_response()

    with pytest.raises(AIProviderError) as error_info:
        provider.generate_text("Parse this job description.")

    assert isinstance(error_info.value, AIProviderResponseError)
    assert str(error_info.value) == UNFINISHED_MESSAGE
    assert create.call_count == 1


def test_generate_text_rejects_response_without_status(provider):
    response = _fake_response()
    del response.status
    create = provider._client.responses.create
    create.return_value = response

    with pytest.raises(AIProviderResponseError) as error_info:
        provider.generate_text("Parse this job description.")

    assert str(error_info.value) == UNFINISHED_MESSAGE
    assert create.call_count == 1


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: _fake_response(status=LEAK_CANARY),
        lambda: _fake_response(
            status="incomplete",
            incomplete_reason=LEAK_CANARY,
        ),
        lambda: _fake_response(
            status=LEAK_CANARY,
            incomplete_reason=LEAK_CANARY,
        ),
    ],
)
def test_unfinished_response_errors_do_not_leak_server_values(
    provider, response_factory
):
    provider._client.responses.create.return_value = response_factory()

    with pytest.raises(AIProviderResponseError) as error_info:
        provider.generate_text("Parse this job description.")

    assert LEAK_CANARY not in str(error_info.value)
    assert LEAK_CANARY not in _formatted_traceback(error_info.value)


def _config_values(**overrides):
    values = {
        "api_key": "test-key-not-real",
        "model": "test-model",
        "timeout_seconds": 30.0,
        "max_retries": 2,
    }
    values.update(overrides)

    return values


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"model": ""},
        {"model": "   "},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1.0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": "abc"},
        {"max_retries": -1},
        {"max_retries": 2.5},
        {"max_retries": "abc"},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": 2.5},
        {"max_output_tokens": "1500"},
    ],
)
def test_explicit_config_rejects_invalid_values(overrides):
    with pytest.raises(AIProviderConfigError):
        OpenAIProviderConfig(**_config_values(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": True},
        {"max_retries": True},
        {"max_output_tokens": True},
    ],
)
def test_explicit_config_rejects_boolean_numbers(overrides):
    with pytest.raises(AIProviderConfigError):
        OpenAIProviderConfig(**_config_values(**overrides))


def test_explicit_config_accepts_zero_retries():
    config = OpenAIProviderConfig(**_config_values(max_retries=0))

    assert config.max_retries == 0


def test_invalid_config_never_constructs_the_sdk_client():
    with patch("app.ai_provider.OpenAI") as fake_openai:
        with pytest.raises(AIProviderConfigError):
            OpenAIProvider(
                config=OpenAIProviderConfig(**_config_values(model="   "))
            )

        assert fake_openai.call_count == 0


def test_generate_text_omits_reasoning_by_default(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text("Parse this job description.")

    request = provider._client.responses.create.call_args.kwargs

    assert "reasoning" not in request


def test_generate_text_sends_reasoning_effort_none(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        reasoning_effort="none",
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["reasoning"] == {"effort": "none"}


def test_generate_text_does_not_hardcode_reasoning_effort(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        reasoning_effort="low",
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["reasoning"] == {"effort": "low"}


def test_generate_text_sends_live_call_parameters(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        max_output_tokens=300,
        reasoning_effort="none",
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["model"] == "test-model"
    assert request["store"] is False
    assert request["max_output_tokens"] == 300
    assert request["reasoning"] == {"effort": "none"}


TEST_SCHEMA = {
    "type": "object",
    "properties": {"job_title": {"type": "string"}},
    "required": ["job_title"],
    "additionalProperties": False,
}

TEST_SCHEMA_FORMAT = JsonSchemaFormat(
    name="test_job_schema",
    schema=TEST_SCHEMA,
)


def _refusal_response(refusal_text="I cannot help with that."):
    refusal_part = types.SimpleNamespace(
        type="refusal",
        refusal=refusal_text,
    )
    message_item = types.SimpleNamespace(
        type="message",
        content=[refusal_part],
    )

    return _fake_response(output_text="", output=[message_item])


def test_generate_text_omits_text_format_by_default(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text("Parse this job description.")

    request = provider._client.responses.create.call_args.kwargs

    assert "text" not in request


def test_generate_text_sends_strict_json_schema_format(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        json_schema_format=TEST_SCHEMA_FORMAT,
    )

    request = provider._client.responses.create.call_args.kwargs
    text_format = request["text"]["format"]

    assert text_format["type"] == "json_schema"
    assert text_format["name"] == "test_job_schema"
    assert text_format["strict"] is True


def test_generate_text_sends_the_schema_unchanged(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        json_schema_format=TEST_SCHEMA_FORMAT,
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["text"]["format"]["schema"] == TEST_SCHEMA


def test_generate_text_combines_schema_with_other_options(provider):
    provider._client.responses.create.return_value = _fake_response()

    provider.generate_text(
        "Parse this job description.",
        max_output_tokens=1500,
        reasoning_effort="none",
        json_schema_format=TEST_SCHEMA_FORMAT,
    )

    request = provider._client.responses.create.call_args.kwargs

    assert request["max_output_tokens"] == 1500
    assert request["reasoning"] == {"effort": "none"}
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True


def test_generate_text_rejects_a_refusal_output_item(provider):
    refusal_item = types.SimpleNamespace(type="refusal", content=None)
    provider._client.responses.create.return_value = _fake_response(
        output_text="",
        output=[refusal_item],
    )

    with pytest.raises(AIProviderRefusalError):
        provider.generate_text("Parse this job description.")


def test_generate_text_rejects_a_refusal_content_part(provider):
    provider._client.responses.create.return_value = _refusal_response()

    with pytest.raises(AIProviderRefusalError):
        provider.generate_text("Parse this job description.")


def test_refusal_error_does_not_leak_provider_text(provider):
    provider._client.responses.create.return_value = _refusal_response(
        refusal_text="Refused because of " + LEAK_CANARY,
    )

    with pytest.raises(AIProviderRefusalError) as error_info:
        provider.generate_text("Parse this job description.")

    assert LEAK_CANARY not in str(error_info.value)
    assert LEAK_CANARY not in _formatted_traceback(error_info.value)


def test_empty_output_without_a_refusal_is_not_a_refusal(provider):
    provider._client.responses.create.return_value = _fake_response(
        output_text="",
        output=[],
    )

    with pytest.raises(AIProviderError) as error_info:
        provider.generate_text("Parse this job description.")

    assert isinstance(error_info.value, AIProviderEmptyResponseError)


@pytest.mark.parametrize(
    "name",
    ["", "   ", "has space", "has.dot", "a" * 65],
)
def test_json_schema_format_rejects_invalid_names(name):
    with pytest.raises(AIProviderConfigError):
        JsonSchemaFormat(name=name, schema=TEST_SCHEMA)


@pytest.mark.parametrize("schema", [{}, None, "not a dict", [], 5])
def test_json_schema_format_rejects_invalid_schemas(schema):
    with pytest.raises(AIProviderConfigError):
        JsonSchemaFormat(name="test_job_schema", schema=schema)


def test_json_schema_format_accepts_a_64_character_name():
    schema_format = JsonSchemaFormat(name="a" * 64, schema=TEST_SCHEMA)

    assert len(schema_format.name) == 64


def test_generate_text_reports_the_declared_provider_identity(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")

    assert result.provider == OpenAIProvider.PROVIDER_NAME


def test_generate_text_does_not_hardcode_the_provider_identity():
    class RenamedProvider(OpenAIProvider):
        PROVIDER_NAME = "renamed-provider"

    with patch("app.ai_provider.OpenAI"):
        renamed = RenamedProvider(config=TEST_CONFIG)

    renamed._client.responses.create.return_value = _fake_response()

    result = renamed.generate_text("Parse this job description.")

    assert result.provider == "renamed-provider"


def test_generate_text_reports_the_model_it_actually_requested(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")
    sent = provider._client.responses.create.call_args.kwargs

    assert result.requested_model == sent["model"]
    assert result.requested_model == TEST_CONFIG.model


def test_generate_text_separates_requested_and_returned_model(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")

    assert result.requested_model == "test-model"
    assert result.model == "test-model-2026-01-01"
    assert result.requested_model != result.model


def test_identity_names_the_configured_service_and_model_without_a_request(
    provider,
):
    assert provider.identity() == ProviderIdentity(
        provider="openai",
        requested_model="test-model",
    )
    provider._client.responses.create.assert_not_called()


def test_identity_is_what_a_request_then_asks_for(provider):
    provider._client.responses.create.return_value = _fake_response()

    identity = provider.identity()
    result = provider.generate_text("Parse this job description.")
    sent = provider._client.responses.create.call_args.kwargs

    assert identity.requested_model == sent["model"]
    assert (result.provider, result.requested_model) == (
        identity.provider,
        identity.requested_model,
    )
    # The version the service answers with is not part of the identity.
    assert result.model != identity.requested_model


def test_identity_does_not_hardcode_the_provider_name():
    class RenamedProvider(OpenAIProvider):
        PROVIDER_NAME = "renamed-provider"

    with patch("app.ai_provider.OpenAI"):
        renamed = RenamedProvider(config=TEST_CONFIG)

    assert renamed.identity().provider == "renamed-provider"


def test_identity_names_a_fixed_endpoint_and_none_otherwise(provider):
    assert provider.identity().endpoint is None

    with patch("app.ai_provider.OpenAI") as client:
        fixed = OpenAIProvider(
            config=OpenAIProviderConfig(
                api_key="test-key-not-real",
                model="test-model",
                base_url="https://api.example.invalid/v1",
            )
        )

    assert fixed.identity().endpoint == "https://api.example.invalid/v1"
    assert client.call_args.kwargs["base_url"] == "https://api.example.invalid/v1"


def test_proxies_from_the_environment_are_ignored_when_asked():
    with patch("app.ai_provider.OpenAI") as client, patch(
        "app.ai_provider.DefaultHttpxClient"
    ) as http:
        OpenAIProvider(
            config=OpenAIProviderConfig(
                api_key="test-key-not-real",
                model="test-model",
                use_environment_proxies=False,
            )
        )

    assert http.call_args.kwargs["trust_env"] is False
    assert client.call_args.kwargs["http_client"] is http.return_value

    with patch("app.ai_provider.OpenAI") as client:
        OpenAIProvider(config=TEST_CONFIG)

    assert "http_client" not in client.call_args.kwargs


@pytest.mark.parametrize(
    "base_url", ["http://api.example.invalid/v1", "api.example.invalid", 5]
)
def test_config_rejects_an_endpoint_that_is_not_https(base_url):
    with pytest.raises(AIProviderConfigError):
        OpenAIProviderConfig(
            api_key="test-key-not-real", model="test-model", base_url=base_url
        )


def test_a_provider_that_cannot_tell_has_no_identity():
    class UnnamedProvider(AIProvider):
        def generate_text(self, prompt, **options):
            raise AssertionError("nothing is sent")

    assert UnnamedProvider().identity() is None


def test_generate_text_reports_the_options_it_actually_sent(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text(
        "Parse this job description.",
        max_output_tokens=1500,
        reasoning_effort="none",
    )
    sent = provider._client.responses.create.call_args.kwargs

    assert result.requested_max_output_tokens == sent["max_output_tokens"]
    assert result.requested_reasoning_effort == sent["reasoning"]["effort"]


def test_generate_text_reports_omitted_options_as_unknown(provider):
    provider._client.responses.create.return_value = _fake_response()

    result = provider.generate_text("Parse this job description.")
    sent = provider._client.responses.create.call_args.kwargs

    assert "max_output_tokens" not in sent
    assert "reasoning" not in sent
    assert result.requested_max_output_tokens is None


@pytest.mark.parametrize(
    "asked, sent", [(None, 1500), (300, 300), (1500, 1500), (4000, 1500)]
)
def test_a_configured_output_limit_caps_every_request(asked, sent):
    with patch("app.ai_provider.OpenAI"):
        capped = OpenAIProvider(
            config=OpenAIProviderConfig(
                **_config_values(max_output_tokens=1500)
            )
        )

    capped._client.responses.create.return_value = _fake_response()

    result = capped.generate_text(
        "Parse this job description.", max_output_tokens=asked
    )
    request = capped._client.responses.create.call_args.kwargs

    assert request["max_output_tokens"] == sent
    assert result.requested_max_output_tokens == sent
    assert result.requested_reasoning_effort is None


# --- what a request used and how far it got (U2-C1) -----------------------------------------------------

SECRET_PROMPT = "SYNTHETIC-PROMPT-WORDS"


def _response_with_usage(**changes):
    response = _fake_response(**changes)
    response.service_tier = "default"
    response.usage = types.SimpleNamespace(
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        input_tokens_details=types.SimpleNamespace(cached_tokens=40, cache_write_tokens=10),
        output_tokens_details=types.SimpleNamespace(reasoning_tokens=12),
    )

    return response


FULL_USAGE = Usage(
    input_tokens=120,
    output_tokens=30,
    cache_read_tokens=40,
    cache_write_tokens=10,
    reasoning_tokens=12,
    total_tokens=150,
    service_tier="default",
    # The response says nothing of how long its writes are kept (C4 R1).
    cache_write_split=CacheWriteSplit.ABSENT,
)


def test_a_result_reports_every_usage_count_and_the_tier_it_ran_on(provider):
    provider._client.responses.create.return_value = _response_with_usage()

    assert provider.generate_text(SECRET_PROMPT).usage == FULL_USAGE


def test_counts_left_out_are_unknown_and_nothing_more(provider):
    response = _response_with_usage()
    response.usage.input_tokens_details = None
    response.service_tier = None
    provider._client.responses.create.return_value = response
    usage = provider.generate_text(SECRET_PROMPT).usage

    assert (usage.input_tokens, usage.cache_read_tokens, usage.service_tier, usage.malformed) == (120, None, None, False)


@pytest.mark.parametrize("field, value", [("output_tokens", True), ("input_tokens", -1), ("total_tokens", 1.5), ("input_tokens", "120")])
def test_a_count_reported_as_no_count_marks_the_usage_malformed(provider, field, value):
    response = _response_with_usage()
    setattr(response.usage, field, value)
    provider._client.responses.create.return_value = response
    usage = provider.generate_text(SECRET_PROMPT).usage

    # Left out, and never mistaken for a count that was simply not reported.
    assert getattr(usage, field) is None and usage.malformed is True


def test_an_error_carries_the_malformed_mark_too(provider):
    response = _response_with_usage(status="incomplete", incomplete_reason="max_output_tokens")
    response.usage.input_tokens_details.cached_tokens = -40
    provider._client.responses.create.return_value = response

    with pytest.raises(AIProviderResponseError) as caught:
        provider.generate_text(SECRET_PROMPT)

    assert caught.value.usage.malformed is True and caught.value.usage.cache_read_tokens is None


@pytest.mark.parametrize(
    "changes, error_type",
    [
        ({"output_text": "Python, SQL, Doc", "status": "incomplete", "incomplete_reason": "max_output_tokens"}, AIProviderResponseError),
        ({"output_text": "   "}, AIProviderEmptyResponseError),
        ({"output_text": "", "output": [types.SimpleNamespace(type="refusal", content=None)]}, AIProviderRefusalError),
    ],
)
def test_an_unusable_answer_says_it_was_answered_and_what_it_used(provider, changes, error_type):
    provider._client.responses.create.return_value = _response_with_usage(**changes)

    with pytest.raises(error_type) as caught:
        provider.generate_text(SECRET_PROMPT)

    assert caught.value.sent is SendState.ANSWERED and caught.value.usage == FULL_USAGE
    # Counts only: nothing the request or the answer carried.
    shown = str(caught.value) + repr(caught.value) + repr(caught.value.usage)
    assert SECRET_PROMPT not in shown and "Python" not in shown


@pytest.mark.parametrize(
    "error, sent",
    [
        (lambda: APITimeoutError(_fake_request()), SendState.HANDED),
        (lambda: APIConnectionError(request=_fake_request()), SendState.HANDED),
        (lambda: RateLimitError("rate limited", response=_fake_http_response(429), body=None), SendState.ANSWERED),
        (lambda: APIStatusError("server error", response=_fake_http_response(500), body=None), SendState.ANSWERED),
    ],
)
def test_a_failed_call_says_how_far_it_got_and_reports_no_usage(provider, error, sent):
    provider._client.responses.create.side_effect = error()

    with pytest.raises(AIProviderError) as caught:
        provider.generate_text(SECRET_PROMPT)

    assert caught.value.sent is sent and caught.value.usage is None
    assert SECRET_PROMPT not in str(caught.value)


def test_errors_raised_without_saying_count_as_perhaps_sent_and_config_errors_as_not_sent():
    assert AIProviderTimeoutError("x").sent is SendState.HANDED and AIProviderTimeoutError("x").usage is None
    assert AIProviderConfigError("x").sent is SendState.NOT_SENT


def test_a_configured_service_tier_is_asked_for_in_every_request():
    with patch("app.ai_provider.OpenAI"):
        tiered = OpenAIProvider(config=OpenAIProviderConfig(**_config_values(service_tier="flex")))

    tiered._client.responses.create.return_value = _fake_response()
    tiered.generate_text("Parse this job description.")

    assert tiered._client.responses.create.call_args.kwargs["service_tier"] == "flex"


def test_no_service_tier_is_sent_unless_configured(provider):
    provider._client.responses.create.return_value = _fake_response()
    provider.generate_text("Parse this job description.")

    assert "service_tier" not in provider._client.responses.create.call_args.kwargs


@pytest.mark.parametrize("tier", ["", "Default", "flex tier", 3, "x" * 21])
def test_config_rejects_a_service_tier_that_is_not_a_short_name(tier):
    with pytest.raises(AIProviderConfigError):
        OpenAIProviderConfig(**_config_values(service_tier=tier))


# --- counting a request's input (U2-C4 prerequisites) -------------------------------------------------


COUNTING_CONFIG = OpenAIProviderConfig(
    api_key="test-key-not-real",
    model="test-model",
    timeout_seconds=30.0,
    max_retries=0,
    max_output_tokens=500,
    service_tier="default",
)
SCHEMA = JsonSchemaFormat("synthetic_format", {"type": "object"})


@pytest.fixture
def counting():
    with patch("app.ai_provider.OpenAI"):
        provider = OpenAIProvider(config=COUNTING_CONFIG)

    provider._client.responses.create.return_value = _fake_response()
    provider._client.responses.input_tokens.count.return_value = types.SimpleNamespace(
        object="response.input_tokens", input_tokens=321
    )

    return provider


def test_a_count_takes_the_request_a_generation_would_send_less_what_a_count_does_not_take(counting):
    assert counting.count_input_tokens("Parse this.", reasoning_effort="low", json_schema_format=SCHEMA) == 321

    counting.generate_text("Parse this.", reasoning_effort="low", json_schema_format=SCHEMA)
    counted = counting._client.responses.input_tokens.count.call_args.kwargs
    created = counting._client.responses.create.call_args.kwargs

    # The prompt (input) and the format (text) are counted as they are sent; only these three are not.
    assert set(counted) == {"model", "input", "reasoning", "text"}
    assert counted == {key: created[key] for key in counted}
    assert set(created) - set(counted) == {"store", "max_output_tokens", "service_tier"}


def test_a_request_is_given_the_time_left_and_never_more_than_the_configured_timeout(counting):
    counting.count_input_tokens("Parse this.", timeout_seconds=12.5)
    assert counting._client.responses.input_tokens.count.call_args.kwargs["timeout"] == 12.5

    counting.count_input_tokens("Parse this.", timeout_seconds=99)
    assert counting._client.responses.input_tokens.count.call_args.kwargs["timeout"] == 30.0

    counting.generate_text("Parse this.", timeout_seconds=7)
    assert counting._client.responses.create.call_args.kwargs["timeout"] == 7

    counting.generate_text("Parse this.")
    assert "timeout" not in counting._client.responses.create.call_args.kwargs


@pytest.mark.parametrize(
    "raised, expected, sent",
    [
        (lambda: APITimeoutError(_fake_request()), AIProviderTimeoutError, SendState.HANDED),
        (lambda: APIConnectionError(request=_fake_request()), AIProviderConnectionError, SendState.HANDED),
        (lambda: RateLimitError("rate limited", response=_fake_http_response(429), body=None), AIProviderRateLimitError, SendState.ANSWERED),
        (lambda: APIStatusError("bad request", response=_fake_http_response(400), body=None), AIProviderResponseError, SendState.ANSWERED),
    ],
    ids=["timeout", "connection", "rate-limit", "status"],
)
def test_a_count_that_fails_says_how_far_it_got(counting, raised, expected, sent):
    counting._client.responses.input_tokens.count.side_effect = raised()

    with pytest.raises(expected) as failed:
        counting.count_input_tokens("Parse this.")

    assert failed.value.sent is sent and failed.value.usage is None


@pytest.mark.parametrize("count", [None, 0, -1, True, "7", 1.5], ids=["none", "zero", "negative", "bool", "text", "fraction"])
def test_a_count_that_is_no_whole_number_of_one_or_more_is_refused_as_answered(counting, count):
    counting._client.responses.input_tokens.count.return_value = types.SimpleNamespace(input_tokens=count)

    with pytest.raises(AIProviderResponseError) as refused:
        counting.count_input_tokens("Parse this.")

    assert refused.value.sent is SendState.ANSWERED


def test_a_provider_that_cannot_count_stops_before_anything_is_sent():
    class Generating(AIProvider):
        def generate_text(self, prompt, **options):
            raise AssertionError("never asked")

    with pytest.raises(AIProviderConfigError) as refused:
        Generating().count_input_tokens("Parse this.")

    assert refused.value.sent is SendState.NOT_SENT


@pytest.mark.parametrize(
    "details, kept",
    [
        # SDK 3.8.0 does not declare the split and keeps it as a mapping.
        ({"ttl_30m_tokens": 3, "ttl_12h_tokens": 4}, (3, 4, False)),
        (types.SimpleNamespace(ttl_30m_tokens=7, ttl_12h_tokens=0), (7, 0, False)),
        (None, (None, None, False)),
        ({"ttl_30m_tokens": "3", "ttl_12h_tokens": 4}, (None, 4, True)),
    ],
    ids=["mapping", "object", "not-said", "not-a-count"],
)
def test_cache_writes_by_how_long_they_are_kept_are_read_as_the_sdk_keeps_them(provider, details, kept):
    response = _fake_response()
    inputs = types.SimpleNamespace(cached_tokens=0, cache_write_tokens=7)

    if details is not None:
        inputs.cache_write_tokens_details = details

    response.usage = types.SimpleNamespace(input_tokens=42, output_tokens=7, total_tokens=49, input_tokens_details=inputs)
    provider._client.responses.create.return_value = response
    result = provider.generate_text("Parse this job description.")

    assert (result.cache_write_30m_tokens, result.cache_write_12h_tokens, result.usage_malformed) == kept
    assert result.usage.cache_write_30m_tokens == kept[0]


# --- the body a request goes out with, measured before it is sent (U2-C cost control) -------------------


def test_a_request_body_is_measured_by_one_fixed_rule():
    from app.ai_provider import request_bytes

    # Keys sorted, no spaces, Chinese as UTF-8 (3 bytes each), a quote and a line break escaped (2 bytes each).
    assert request_bytes({"b": 1, "a": "中\"\n"}) == len('{"a":"中\\"\\n","b":1}'.encode("utf-8")) == 21


def test_the_body_a_provider_tells_is_the_body_it_sends(counting):
    body = counting.request_body("Parse this.", max_output_tokens=300, reasoning_effort="low", json_schema_format=SCHEMA)
    counting.generate_text("Parse this.", max_output_tokens=300, reasoning_effort="low", json_schema_format=SCHEMA, timeout_seconds=9)
    sent = dict(counting._client.responses.create.call_args.kwargs)

    assert sent.pop("timeout") == 9 and body == sent
    # Telling it sends nothing, and counts nothing.
    assert counting._client.responses.input_tokens.count.call_count == 0


def test_a_provider_that_cannot_tell_its_body_stops_before_anything_is_sent():
    class Generating(AIProvider):
        def generate_text(self, prompt, **options):
            raise AssertionError("never asked")

    with pytest.raises(AIProviderConfigError) as refused:
        Generating().request_body("Parse this.")

    assert refused.value.sent is SendState.NOT_SENT
