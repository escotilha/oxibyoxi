"""Inference gateway — async HTTP client for non-agentic LLM calls.

Design constraints (from ``docs/plans/multi-model-orchestration.md``):

- **Non-streaming only.** LiteLLM bug #12689: the ``x-litellm-response-cost``
  header is silently dropped on ``stream=True`` responses, causing cost
  under-counting. ``stream=False`` is hard-coded in every outbound request
  body. Tests assert this lock is never bypassed regardless of caller kwargs.

- **Cost from header.** ``InferenceResult.cost_usd`` is read from the
  ``x-litellm-response-cost`` response header. Falls back to ``0.0`` and
  logs a warning when the header is absent (e.g. a non-LiteLLM backend).

- **Typed errors.** 4xx/5xx responses raise ``InferenceAuthError``,
  ``InferenceRateLimitError``, or ``InferenceServiceError`` so callers can
  handle them precisely without parsing HTTP status codes themselves.

- **``FakeInferenceGateway``** — a test double that stores canned
  ``InferenceResult`` responses keyed by ``(model, prompt_hash)`` tuple.
  Returns a zero-cost result for unknown keys so tests can call it without
  pre-seeding every possible combination.

No call sites wired in this PR. The gateway is ready to use; T2-38 wires
heartbeat reasoning, T2-39 wires the routing function, etc.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceResult:
    """Response from a single non-streaming LLM completion.

    Attributes:
        text: the assistant's reply (first choice, message content).
        cost_usd: monetary cost extracted from the ``x-litellm-response-cost``
            response header.  Zero when the header is absent.
        tokens_in: prompt token count from the usage block.
        tokens_out: completion token count from the usage block.
        model: the model name echoed by the response (may differ from the
            requested model when the gateway applies a fallback).
        latency_ms: wall-clock milliseconds from request start to first
            byte of the response body.
    """

    text: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    model: str
    latency_ms: float


# ---------------------------------------------------------------------------
# Typed error hierarchy
# ---------------------------------------------------------------------------


class InferenceError(RuntimeError):
    """Base class for all inference gateway errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InferenceAuthError(InferenceError):
    """Raised on HTTP 401 / 403 — bad or missing API key."""


class InferenceRateLimitError(InferenceError):
    """Raised on HTTP 429 — rate-limit or quota exhausted."""


class InferenceServiceError(InferenceError):
    """Raised on HTTP 5xx or unexpected transport errors."""


# ---------------------------------------------------------------------------
# Message type alias
# ---------------------------------------------------------------------------

#: A single chat message dict, e.g. ``{"role": "user", "content": "hello"}``.
Message = dict[str, Any]


# ---------------------------------------------------------------------------
# Production gateway
# ---------------------------------------------------------------------------


class InferenceGateway:
    """Async HTTP client against a LiteLLM-compatible ``/chat/completions`` endpoint.

    Parameters:
        base_url: root URL of the LiteLLM proxy, e.g.
            ``"http://litellm.tailnet.local:4000"``.  No trailing slash required.
        api_key: virtual API key forwarded in the ``Authorization`` header.
            May be ``None`` when the proxy is configured to accept unauthenticated
            requests (e.g. local dev with no budget cap).
        timeout: per-request timeout in seconds.  Defaults to 60 s — long
            enough for a Sonnet response under load, short enough to not stall
            the heartbeat loop.
        client: optional pre-built ``httpx.AsyncClient``.  Inject in tests to
            capture outbound requests without making real network calls.

    Usage::

        gw = InferenceGateway(base_url="http://litellm.local:4000", api_key="sk-...")
        result = await gw.complete(
            messages=[{"role": "user", "content": "What is 2+2?"}],
            model="claude-haiku-4-5",
            max_tokens=64,
        )
        print(result.text, result.cost_usd)
    """

    _COST_HEADER = "x-litellm-response-cost"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int,
        **kwargs: Any,
    ) -> InferenceResult:
        """Send a non-streaming chat completion request.

        ``stream=False`` is **always** injected into the request body,
        overriding any value the caller passes via ``kwargs``.  This is
        the hard lock against LiteLLM bug #12689.

        Args:
            messages: list of ``{"role": ..., "content": ...}`` dicts.
            model: model identifier as understood by the LiteLLM proxy
                (e.g. ``"claude-haiku-4-5"``).
            max_tokens: maximum tokens in the completion.
            **kwargs: extra parameters forwarded to the request body
                (``temperature``, ``top_p``, etc.).  ``stream`` is always
                stripped and replaced with ``False``.

        Returns:
            ``InferenceResult`` populated from the response body and headers.

        Raises:
            InferenceAuthError: HTTP 401 or 403.
            InferenceRateLimitError: HTTP 429.
            InferenceServiceError: HTTP 5xx or transport failure.
        """
        # Hard-code stream=False — never allow the caller to override.
        # This is the non-streaming lock that preserves the cost header.
        kwargs.pop("stream", None)

        payload: dict[str, Any] = {
            **kwargs,
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,  # hard-coded; see module docstring
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"
        t_start = time.monotonic()

        if self._client is not None:
            return await self._do_request(
                self._client, url, payload, headers, t_start, model
            )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._do_request(
                client, url, payload, headers, t_start, model
            )

    # ---- Internal helpers ------------------------------------------------

    async def _do_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        t_start: float,
        requested_model: str,
    ) -> InferenceResult:
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.TransportError as exc:
            raise InferenceServiceError(
                f"transport error calling {url}: {exc}"
            ) from exc

        latency_ms = (time.monotonic() - t_start) * 1000.0

        self._raise_for_status(response)

        body: dict[str, Any] = response.json()

        # Cost from response header — the whole reason we hard-code stream=False.
        raw_cost = response.headers.get(self._COST_HEADER)
        if raw_cost is None:
            logger.warning(
                "inference_gateway.cost_header_missing",
                extra={"url": url, "model": requested_model},
            )
            cost_usd = 0.0
        else:
            try:
                cost_usd = float(raw_cost)
            except ValueError:
                logger.warning(
                    "inference_gateway.cost_header_invalid",
                    extra={"url": url, "raw": raw_cost},
                )
                cost_usd = 0.0

        # Parse the response body (OpenAI-compatible format).
        choices = body.get("choices") or []
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            text = message.get("content") or ""

        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        response_model = body.get("model") or requested_model

        return InferenceResult(
            text=text,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=response_model,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        code = response.status_code
        if code < 400:
            return
        body_snippet = response.text[:200]
        if code in (401, 403):
            raise InferenceAuthError(
                f"authentication failed ({code}): {body_snippet}",
                status_code=code,
            )
        if code == 429:
            raise InferenceRateLimitError(
                f"rate limit exceeded ({code}): {body_snippet}",
                status_code=code,
            )
        # 4xx (other) and 5xx
        raise InferenceServiceError(
            f"inference request failed ({code}): {body_snippet}",
            status_code=code,
        )


# ---------------------------------------------------------------------------
# Prompt hash helper (shared by gateway + fake)
# ---------------------------------------------------------------------------


def _prompt_hash(messages: list[Message]) -> str:
    """Stable hash of a messages list for fake-response keying."""
    # Serialize deterministically: sort-keys, no whitespace variation.
    import json

    canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeInferenceGateway:
    """Deterministic test double for ``InferenceGateway``.

    Stores canned ``InferenceResult`` responses keyed by
    ``(model, prompt_hash)`` where ``prompt_hash`` is a 16-character
    SHA-256 prefix of the JSON-serialized messages list.

    For unknown keys, returns a zero-cost, empty-text result so callers
    can invoke the gateway without pre-seeding every combination.

    Usage in tests::

        fake = FakeInferenceGateway()
        fake.register(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "ping"}],
            result=InferenceResult(
                text="pong",
                cost_usd=0.001,
                tokens_in=1,
                tokens_out=1,
                model="claude-haiku-4-5",
                latency_ms=42.0,
            ),
        )
        result = await fake.complete(
            messages=[{"role": "user", "content": "ping"}],
            model="claude-haiku-4-5",
            max_tokens=16,
        )
        assert result.text == "pong"

    The fake also records all ``complete`` calls so tests can assert
    invocation count, models used, etc.
    """

    _responses: dict[tuple[str, str], InferenceResult] = field(
        default_factory=dict
    )
    _calls: list[dict[str, Any]] = field(default_factory=list)

    def register(
        self,
        *,
        model: str,
        messages: list[Message],
        result: InferenceResult,
    ) -> None:
        """Pre-load a canned response for a ``(model, messages)`` pair."""
        key = (model, _prompt_hash(messages))
        self._responses[key] = result

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Read-only list of recorded ``complete`` invocations.

        Each entry is a dict with keys ``model``, ``messages``,
        ``max_tokens``, and ``kwargs``.
        """
        return list(self._calls)

    def call_count(self) -> int:
        """Number of times ``complete`` was called."""
        return len(self._calls)

    async def complete(
        self,
        messages: list[Message],
        model: str,
        max_tokens: int,
        **kwargs: Any,
    ) -> InferenceResult:
        """Return a pre-seeded result or a zero-cost default."""
        self._calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "kwargs": kwargs,
            }
        )
        key = (model, _prompt_hash(messages))
        if key in self._responses:
            return self._responses[key]
        # Unknown key — return a safe zero-cost default so tests that
        # don't need specific responses can still exercise the code path.
        return InferenceResult(
            text="",
            cost_usd=0.0,
            tokens_in=0,
            tokens_out=0,
            model=model,
            latency_ms=0.0,
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "FakeInferenceGateway",
    "InferenceAuthError",
    "InferenceError",
    "InferenceGateway",
    "InferenceRateLimitError",
    "InferenceResult",
    "InferenceServiceError",
    "Message",
]
