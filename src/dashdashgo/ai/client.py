"""A minimal client for OpenAI-compatible ``/chat/completions`` endpoints.

Only what the assistant needs: a system + user prompt, optional images, and a
JSON object back. Models are tried in order - free tiers are often "overloaded"
(503) or rate-limited (429) for a while, so each model is retried with backoff
and then the next one is used.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from dashdashgo.errors import AIError

log = logging.getLogger(__name__)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_NEXT_MODEL_STATUS = frozenset({404})  # model retired or unknown to this provider
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class Completion:
    data: dict[str, Any]
    model: str


class AIClient(Protocol):
    def complete_json(
        self, system: str, user: str, *, images: Sequence[bytes] = ()
    ) -> Completion: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model reply (tolerates a Markdown code fence)."""
    if match := _FENCE.match(text):
        text = match.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIError(f"the model did not return valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AIError("the model returned JSON that is not an object")
    return value


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        models: Sequence[str],
        timeout_s: float = 60,
        attempts_per_model: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not models:
            raise AIError("no AI model configured (AI_MODEL)")
        self.models = list(models)
        self._attempts = attempts_per_model
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    def complete_json(self, system: str, user: str, *, images: Sequence[bytes] = ()) -> Completion:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images:
            encoded = base64.b64encode(image).decode()
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        problems: list[str] = []
        for model in self.models:
            try:
                return Completion(self._complete(model, messages), model)
            except _TryNextModelError as exc:
                log.warning("AI model %s unavailable (%s); trying the next one", model, exc)
                problems.append(f"{model}: {exc}")
        raise AIError("no AI model could answer: " + "; ".join(problems), retryable=True)

    def _complete(self, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        last = ""
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._http.post("chat/completions", json=body)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return self._content(response)
                last = f"HTTP {response.status_code} {_error_text(response)}"
                if response.status_code in _NEXT_MODEL_STATUS:
                    raise _TryNextModelError(last)
                if response.status_code not in _RETRY_STATUS:
                    # Bad key, bad request: another model or attempt will not help.
                    raise AIError(f"AI provider refused the request: {last}")
            if attempt < self._attempts:
                self._sleep(min(2.0**attempt, 20.0))
        raise _TryNextModelError(last)

    @staticmethod
    def _content(response: httpx.Response) -> dict[str, Any]:
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIError(f"unexpected AI response shape: {exc}") from exc
        if not isinstance(text, str):
            raise AIError("the AI response has no text content")
        return parse_json_object(text)

    def close(self) -> None:
        self._http.close()


class _TryNextModelError(Exception):
    pass


def _error_text(response: httpx.Response) -> str:
    try:
        detail = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(detail, list) and detail:
        detail = detail[0]
    if isinstance(detail, dict):
        error = detail.get("error", detail)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    return str(detail)[:300]


def build_client(
    *, api_key: str, base_url: str, model: str, timeout_s: float
) -> OpenAICompatibleClient | None:
    """The configured client, or None when no API key is set (assistant off)."""
    if not api_key:
        return None
    models = [m.strip() for m in model.split(",") if m.strip()]
    return OpenAICompatibleClient(
        api_key=api_key, base_url=base_url, models=models, timeout_s=timeout_s
    )
