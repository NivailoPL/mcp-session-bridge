"""Jev on OpenRouter: one choice question per chapter.

Jev is a decisions model, not a chat model: it is called through the alpha
decisions API with a ``state`` object and a ``choice`` question whose criteria
are the topic descriptions. It answers with a choice, a confidence and the
probability of every option; only input is billed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.topics import JEV_INSTRUCTIONS

JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
JEV_TIMEOUT_SECONDS = 60
JEV_RETRIES = 4
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class JevError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class JevClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=JEV_TIMEOUT_SECONDS,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "MCP Session Bridge topics",
            },
        )

    def request_body(self, state: dict[str, Any], criteria: dict[str, str]) -> dict[str, Any]:
        return {
            "model": self.model,
            "state": state,
            "questions": {"topic": {"type": "choice", "instructions": JEV_INSTRUCTIONS, "criteria": criteria}},
        }

    async def classify(self, state: dict[str, Any], criteria: dict[str, str]) -> dict[str, Any]:
        """Return the parsed answer plus the raw response; raise ``JevError`` when there is none."""
        body = self.request_body(state, criteria)
        for attempt in range(JEV_RETRIES + 1):
            try:
                response = await self._client.post(JEV_ENDPOINT, json=body)
            except httpx.HTTPError as exc:
                if attempt < JEV_RETRIES:
                    await self._sleep(2**attempt)
                    continue
                raise JevError(f"OpenRouter is unreachable: {exc.__class__.__name__}.", code="jev_unavailable") from None
            if response.status_code in _RETRY_STATUSES and attempt < JEV_RETRIES:
                await self._sleep(2**attempt)
                continue
            if response.status_code in (401, 403):
                raise JevError("OpenRouter refused the API key.", code="jev_auth")
            if response.status_code in _RETRY_STATUSES:
                raise JevError(f"OpenRouter kept failing (HTTP {response.status_code}).", code="jev_unavailable")
            if response.status_code >= 400:
                raise JevError(
                    f"OpenRouter rejected the request (HTTP {response.status_code}): {response.text[:300]}",
                    code="jev_rejected",
                )
            try:
                raw = response.json()
            except ValueError:
                raise JevError("OpenRouter returned invalid JSON.", code="jev_invalid") from None
            return parse_answer(raw, criteria)
        raise JevError("OpenRouter kept failing.", code="jev_unavailable")

    async def close(self) -> None:
        await self._client.aclose()


def parse_answer(raw: Any, criteria: dict[str, str]) -> dict[str, Any]:
    answer = (raw.get("answers") or {}).get("topic") if isinstance(raw, dict) else None
    if not isinstance(answer, dict) or answer.get("choice") not in criteria:
        raise JevError("Jev returned no usable topic.", code="jev_invalid")
    probabilities = answer.get("probabilities") or {}
    if not isinstance(probabilities, dict):
        probabilities = {}
    confidence = answer.get("confidence")
    usage = raw.get("usage") or {}
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return {
        "choice": answer["choice"],
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
        "probabilities": {
            key: float(value) for key, value in probabilities.items() if isinstance(value, (int, float))
        },
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "raw": raw,
    }
