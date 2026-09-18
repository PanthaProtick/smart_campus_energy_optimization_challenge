from __future__ import annotations

import json
import socket
import urllib.error
from unittest.mock import patch

import pytest

from app.services.llm_provider import (
    GeminiClient,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    _load_project_dotenv,
)


def test_missing_api_key_fails_with_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigurationError) as error:
        ProviderConfig.from_env()
    assert "GEMINI_API_KEY" in str(error.value)
    assert "secret" not in str(error.value).lower()


def test_configuration_uses_gemini_flash_and_enforces_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-secret-value")
    config = ProviderConfig.from_env()
    assert config.api_key == "test-secret-value"
    assert config.model == "gemini-3.5-flash"
    assert config.timeout_seconds == 8.0
    assert config.max_attempts == 2
    with pytest.raises(ProviderConfigurationError):
        ProviderConfig(api_key="test-key", timeout_seconds=30)
    with pytest.raises(ProviderConfigurationError):
        ProviderConfig(api_key="test-key", max_attempts=5)


def test_dotenv_loads_values_without_overriding_process_environment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'GEMINI_API_KEY="dotenv-key"\nGRIDWISE_LLM_MODEL=gemini-test\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "shell-key")
    monkeypatch.delenv("GRIDWISE_LLM_MODEL", raising=False)

    _load_project_dotenv(env_file)

    assert __import__("os").environ["GEMINI_API_KEY"] == "shell-key"
    assert __import__("os").environ["GRIDWISE_LLM_MODEL"] == "gemini-test"


def test_adapter_returns_plain_json_and_requests_json_output() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    config = ProviderConfig(api_key="test-key")
    with patch("urllib.request.urlopen", return_value=Response()) as open_url:
        result = GeminiClient(config).generate_content(
            prompt="interpret", response_schema={"type": "object"}
        )
    assert result == payload
    request = open_url.call_args.args[0]
    sent = json.loads(request.data)
    assert request.full_url.endswith("/models/gemini-3.5-flash:generateContent")
    assert sent["generationConfig"]["responseFormat"] == {
        "text": {
            "mimeType": "application/json",
            "schema": {"type": "object"},
        }
    }
    assert request.get_header("X-goog-api-key") == "test-key"


@pytest.mark.parametrize(
    "failure,expected",
    [
        (socket.timeout("secret"), ProviderTimeoutError),
        (urllib.error.URLError("secret"), ProviderUnavailableError),
        (
            urllib.error.HTTPError("https://provider.invalid", 429, "limited", {}, None),
            ProviderUnavailableError,
        ),
        (
            urllib.error.HTTPError("https://provider.invalid", 503, "offline", {}, None),
            ProviderUnavailableError,
        ),
    ],
)
def test_provider_timeout_rate_limit_and_outage_are_typed_without_leaking_details(
    failure: Exception, expected: type[Exception]
) -> None:
    client = GeminiClient(ProviderConfig(api_key="secret-api-key"))
    with patch("urllib.request.urlopen", side_effect=failure) as open_url:
        with pytest.raises(expected) as error:
            client.generate_content(prompt="secret prompt")
    assert open_url.call_count == 2
    assert "secret" not in str(error.value)


def test_malformed_provider_json_is_a_typed_response_failure() -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    client = GeminiClient(ProviderConfig(api_key="test-key"))
    with patch("urllib.request.urlopen", return_value=Response()):
        with pytest.raises(ProviderResponseError) as error:
            client.generate_content(prompt="interpret")
    assert "not-json" not in str(error.value)
