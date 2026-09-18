"""Small Google Gemini API boundary for the GridWise interpreter.

Uses the Gemini REST API directly, keeping provider response details out of
interpreter code. Credentials come only from the process environment and are
never included in returned errors or logs.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    """Safe base error for provider configuration and request failures."""

    category = "provider_error"


class ProviderConfigurationError(ProviderError):
    category = "configuration_error"


class ProviderTimeoutError(ProviderError):
    category = "timeout"


class ProviderRateLimitError(ProviderError):
    category = "rate_limited"


class ProviderRejectedError(ProviderError):
    """A permanent HTTP 4xx rejection, with the safe status code exposed."""

    category = "request_rejected"

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(
            f"Gemini rejected the request (HTTP {status_code}); check key access, model ID, and request format."
        )


class ProviderUnavailableError(ProviderError):
    category = "provider_unavailable"


class ProviderResponseError(ProviderError):
    category = "invalid_provider_response"


def _load_project_dotenv(path: Path | None = None) -> None:
    """Load simple KEY=VALUE lines without overriding existing environment.

    This intentionally supports only the small dotenv subset needed here; it
    does not expand variables or execute shell syntax.
    """
    env_path = path or Path(__file__).resolve().parents[2] / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not name.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str
    model: str = "gemini-3.1-flash-lite"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    timeout_seconds: float = 8.0
    max_attempts: int = 2

    def __post_init__(self) -> None:
        # Keep every configured call inside a conservative share of the API's
        # 30 second total budget, even when instantiated directly in code.
        if not self.api_key.strip():
            raise ProviderConfigurationError("Gemini API key is missing.")
        if not self.model.strip() or not self.endpoint.startswith("https://"):
            raise ProviderConfigurationError("Gemini provider configuration is invalid.")
        if not 0 < self.timeout_seconds <= 10 or not 1 <= self.max_attempts <= 2:
            raise ProviderConfigurationError("Gemini timeout or retry configuration is invalid.")

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        _load_project_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise ProviderConfigurationError(
                "Interpreter provider is not configured (GEMINI_API_KEY is missing)."
            )
        model = os.environ.get("GRIDWISE_LLM_MODEL", "gemini-3.1-flash-lite").strip()
        endpoint = os.environ.get(
            "GRIDWISE_LLM_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta"
        ).strip().rstrip("/")
        return cls(api_key=api_key, model=model, endpoint=endpoint)


class GeminiClient:
    """Gemini REST adapter with bounded transient retries and plain JSON output."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig.from_env()

    @property
    def model(self) -> str:
        return self._config.model

    def generate_content(
        self, *, prompt: str, response_schema: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return decoded GenerateContent JSON; no SDK response types escape."""
        generation_config: dict[str, Any] = {"temperature": 0}
        if response_schema is not None:
            generation_config["responseFormat"] = {
                "text": {
                    "mimeType": "APPLICATION_JSON",
                    "schema": response_schema,
                }
            }
        body = json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.endpoint}/models/{self._config.model}:generateContent",
            data=body,
            headers={
                "x-goog-api-key": self._config.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(self._config.max_attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self._config.timeout_seconds
                ) as response:
                    data = json.loads(response.read())
                if not isinstance(data, dict):
                    raise ProviderResponseError("Gemini returned an invalid response.")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code in (408, 429) or 500 <= exc.code <= 599:
                    if attempt + 1 < self._config.max_attempts:
                        time.sleep(0.2)
                        continue
                    if exc.code == 429:
                        raise ProviderRateLimitError(
                            "Gemini rate limit or quota was reached."
                        ) from None
                    if exc.code == 408:
                        raise ProviderTimeoutError("Gemini request timed out.") from None
                    raise ProviderUnavailableError("Gemini service is temporarily unavailable.") from None
                raise ProviderRejectedError(exc.code) from None
            except (TimeoutError, socket.timeout):
                if attempt + 1 < self._config.max_attempts:
                    time.sleep(0.2)
                    continue
                raise ProviderTimeoutError("Gemini request timed out.") from None
            except urllib.error.URLError:
                if attempt + 1 < self._config.max_attempts:
                    time.sleep(0.2)
                    continue
                raise ProviderUnavailableError("Gemini is unavailable.") from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ProviderResponseError("Gemini returned malformed JSON.") from None
        raise ProviderUnavailableError("Gemini is unavailable.")
