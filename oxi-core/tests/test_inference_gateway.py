"""Tests for oxi_core.v3.inference — InferenceGateway + FakeInferenceGateway.

Coverage targets from the T2-37 acceptance plan:

- ``test_stream_false_locked*`` — every outbound request body has
  ``stream=False`` regardless of kwargs passed by the caller.
- ``test_cost_extracted_from_header`` — cost header parsed into
  ``InferenceResult.cost_usd``.
- ``test_cost_header_missing_*`` — absent or malformed header → 0.0 with
  a warning log; no exception.
- ``test_4xx_raises_typed_error`` — 401 → ``InferenceAuthError``;
  429 → ``InferenceRateLimitError``; 5xx → ``InferenceServiceError``.
- ``test_fake_*`` — ``FakeInferenceGateway`` behaves correctly.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oxi_core.v3.inference import (
    FakeInferenceGateway,
    InferenceAuthError,
    InferenceGateway,
    InferenceRateLimitError,
    InferenceResult,
    InferenceServiceError,
    _prompt_hash,
)

# ---------------------------------------------------------------------------
# Helpers — build fake httpx responses + a capturing client
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    body: dict[str, Any] | None = None,
    cost_header: str | None = "0.0025",
) -> httpx.Response:
    """Build a minimal ``httpx.Response`` for monkeypatching."""
    if body is None:
        body = {
            "id": "chatcmpl-test",
            "model": "claude-haiku-4-5",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello, world!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
    raw_body = json.dumps(body).encode()
    headers: dict[str, str] = {"content-type": "application/json"}
    if cost_header is not None:
        headers["x-litellm-response-cost"] = cost_header
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=raw_body,
        request=httpx.Request("POST", "http://litellm.test:4000/chat/completions"),
    )


class _CapturingClient:
    """Records every call to ``post()`` so tests can assert on the request body.

    Behaves like ``httpx.AsyncClient`` for the methods ``InferenceGateway`` uses.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.requests: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self._response


def _make_gateway(
    response: httpx.Response | None = None,
    *,
    status_code: int = 200,
    cost_header: str | None = "0.0025",
) -> tuple[InferenceGateway, _CapturingClient]:
    """Build a gateway wired to a _CapturingClient."""
    if response is None:
        response = _make_response(status_code=status_code, cost_header=cost_header)
    cap = _CapturingClient(response)
    gw = InferenceGateway(
        base_url="http://litellm.test:4000",
        api_key="sk-test",
        client=cap,  # type: ignore[arg-type]
    )
    return gw, cap


# ---------------------------------------------------------------------------
# stream=False lock — the core correctness invariant
# ---------------------------------------------------------------------------


async def test_stream_false_locked_default_call():
    """Default call path sets stream=False in the request body."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-haiku-4-5",
        max_tokens=64,
    )
    assert len(cap.requests) == 1
    assert cap.requests[0]["json"]["stream"] is False


async def test_stream_false_locked_when_caller_passes_stream_true():
    """Caller explicitly passing stream=True is silently overridden to False."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-haiku-4-5",
        max_tokens=64,
        stream=True,  # caller tries to override — must be suppressed
    )
    assert len(cap.requests) == 1
    assert cap.requests[0]["json"]["stream"] is False


async def test_stream_false_locked_with_extra_kwargs():
    """Extra kwargs like temperature are forwarded but stream is always False."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hello"}],
        model="claude-haiku-4-5",
        max_tokens=128,
        temperature=0.7,
        top_p=0.9,
        stream=True,  # still must be overridden
    )
    assert len(cap.requests) == 1
    body = cap.requests[0]["json"]
    assert body["stream"] is False
    # Extra kwargs were forwarded
    assert body["temperature"] == pytest.approx(0.7)
    assert body["top_p"] == pytest.approx(0.9)


async def test_stream_false_locked_with_system_prompt():
    """System-prompt call path also locks stream=False."""
    gw, cap = _make_gateway()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "ping"},
    ]
    await gw.complete(messages=messages, model="claude-haiku-4-5", max_tokens=16)
    assert cap.requests[0]["json"]["stream"] is False


# ---------------------------------------------------------------------------
# Cost header parsing
# ---------------------------------------------------------------------------


async def test_cost_extracted_from_header():
    """cost_usd is read from x-litellm-response-cost header."""
    gw, _ = _make_gateway(cost_header="0.00357")
    result = await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=8,
    )
    assert result.cost_usd == pytest.approx(0.00357)


async def test_cost_header_missing_returns_zero(caplog):
    """Absent header → cost_usd=0.0 and a warning is logged."""
    import logging

    gw, _ = _make_gateway(cost_header=None)
    with caplog.at_level(logging.WARNING, logger="oxi_core.v3.inference"):
        result = await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert result.cost_usd == pytest.approx(0.0)
    assert any("cost_header_missing" in r.message for r in caplog.records)


async def test_cost_header_non_numeric_returns_zero(caplog):
    """Malformed header value → cost_usd=0.0 and a warning is logged."""
    import logging

    gw, _ = _make_gateway(cost_header="N/A")
    with caplog.at_level(logging.WARNING, logger="oxi_core.v3.inference"):
        result = await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert result.cost_usd == pytest.approx(0.0)
    assert any("cost_header_invalid" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Response body parsing
# ---------------------------------------------------------------------------


async def test_result_fields_populated_from_response():
    """InferenceResult carries text, token counts, and model from the body."""
    gw, _ = _make_gateway()
    result = await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=32,
    )
    assert result.text == "Hello, world!"
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    assert result.model == "claude-haiku-4-5"
    assert result.latency_ms >= 0.0


async def test_response_model_overrides_requested_model():
    """When the gateway echoes a different model name, InferenceResult uses it."""
    body = {
        "id": "chatcmpl-fallback",
        "model": "claude-haiku-4-5-fallback",  # gateway applied a fallback
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    gw, _ = _make_gateway(response=_make_response(body=body))
    result = await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=8,
    )
    assert result.model == "claude-haiku-4-5-fallback"


# ---------------------------------------------------------------------------
# Error path — 4xx / 5xx
# ---------------------------------------------------------------------------


async def test_401_raises_inference_auth_error():
    """HTTP 401 → InferenceAuthError with status_code attribute."""
    gw, _ = _make_gateway(status_code=401, cost_header=None)
    with pytest.raises(InferenceAuthError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 401


async def test_403_raises_inference_auth_error():
    """HTTP 403 → InferenceAuthError."""
    gw, _ = _make_gateway(status_code=403, cost_header=None)
    with pytest.raises(InferenceAuthError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 403


async def test_429_raises_inference_rate_limit_error():
    """HTTP 429 → InferenceRateLimitError."""
    gw, _ = _make_gateway(status_code=429, cost_header=None)
    with pytest.raises(InferenceRateLimitError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 429


async def test_500_raises_inference_service_error():
    """HTTP 500 → InferenceServiceError."""
    gw, _ = _make_gateway(status_code=500, cost_header=None)
    with pytest.raises(InferenceServiceError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 500


async def test_503_raises_inference_service_error():
    """HTTP 503 → InferenceServiceError."""
    gw, _ = _make_gateway(status_code=503, cost_header=None)
    with pytest.raises(InferenceServiceError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 503


async def test_422_raises_inference_service_error():
    """HTTP 422 (non-auth 4xx) → InferenceServiceError."""
    gw, _ = _make_gateway(status_code=422, cost_header=None)
    with pytest.raises(InferenceServiceError) as exc_info:
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )
    assert exc_info.value.status_code == 422


async def test_transport_error_raises_inference_service_error(monkeypatch):
    """Network-level failure → InferenceServiceError (not a raw httpx error)."""

    async def _failing_post(url: str, *, json: Any, headers: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    cap = _CapturingClient(_make_response())
    monkeypatch.setattr(cap, "post", _failing_post)

    gw = InferenceGateway(
        base_url="http://litellm.test:4000",
        api_key="sk-test",
        client=cap,  # type: ignore[arg-type]
    )
    with pytest.raises(InferenceServiceError, match="transport error"):
        await gw.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5",
            max_tokens=8,
        )


# ---------------------------------------------------------------------------
# Request structure — URL and auth header
# ---------------------------------------------------------------------------


async def test_request_posted_to_chat_completions_endpoint():
    """POST goes to <base_url>/chat/completions."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=8,
    )
    assert cap.requests[0]["url"] == "http://litellm.test:4000/chat/completions"


async def test_auth_header_forwarded():
    """Authorization: Bearer <api_key> is present in the request headers."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=8,
    )
    assert cap.requests[0]["headers"]["Authorization"] == "Bearer sk-test"


async def test_no_auth_header_when_api_key_none():
    """When api_key=None, no Authorization header is sent."""
    cap = _CapturingClient(_make_response())
    gw = InferenceGateway(
        base_url="http://litellm.test:4000",
        api_key=None,
        client=cap,  # type: ignore[arg-type]
    )
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5",
        max_tokens=8,
    )
    assert "Authorization" not in cap.requests[0]["headers"]


async def test_model_and_max_tokens_in_body():
    """model and max_tokens appear in the outbound request body."""
    gw, cap = _make_gateway()
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-5",
        max_tokens=256,
    )
    body = cap.requests[0]["json"]
    assert body["model"] == "claude-sonnet-4-5"
    assert body["max_tokens"] == 256


# ---------------------------------------------------------------------------
# FakeInferenceGateway
# ---------------------------------------------------------------------------


async def test_fake_returns_canned_response_by_prompt_hash():
    """FakeInferenceGateway returns the pre-seeded result for a matching key."""
    fake = FakeInferenceGateway()
    messages = [{"role": "user", "content": "ping"}]
    canned = InferenceResult(
        text="pong",
        cost_usd=0.001,
        tokens_in=1,
        tokens_out=1,
        model="claude-haiku-4-5",
        latency_ms=42.0,
    )
    fake.register(model="claude-haiku-4-5", messages=messages, result=canned)

    result = await fake.complete(
        messages=messages, model="claude-haiku-4-5", max_tokens=16
    )
    assert result.text == "pong"
    assert result.cost_usd == pytest.approx(0.001)


async def test_fake_returns_zero_cost_default_for_unknown_key():
    """Unknown (model, messages) key → zero-cost empty default; no exception."""
    fake = FakeInferenceGateway()
    result = await fake.complete(
        messages=[{"role": "user", "content": "unknown query"}],
        model="some-model",
        max_tokens=8,
    )
    assert result.text == ""
    assert result.cost_usd == pytest.approx(0.0)
    assert result.tokens_in == 0
    assert result.tokens_out == 0


async def test_fake_records_all_calls():
    """FakeInferenceGateway.calls records every invocation."""
    fake = FakeInferenceGateway()
    await fake.complete(
        messages=[{"role": "user", "content": "a"}],
        model="m1",
        max_tokens=4,
    )
    await fake.complete(
        messages=[{"role": "user", "content": "b"}],
        model="m2",
        max_tokens=4,
    )
    assert fake.call_count() == 2
    assert fake.calls[0]["model"] == "m1"
    assert fake.calls[1]["model"] == "m2"


async def test_fake_different_models_same_messages_are_separate_keys():
    """Same messages but different model → different entries."""
    fake = FakeInferenceGateway()
    messages = [{"role": "user", "content": "hello"}]

    canned_a = InferenceResult(
        text="from model A", cost_usd=0.01, tokens_in=2, tokens_out=3,
        model="model-a", latency_ms=1.0,
    )
    canned_b = InferenceResult(
        text="from model B", cost_usd=0.02, tokens_in=2, tokens_out=4,
        model="model-b", latency_ms=2.0,
    )
    fake.register(model="model-a", messages=messages, result=canned_a)
    fake.register(model="model-b", messages=messages, result=canned_b)

    result_a = await fake.complete(messages=messages, model="model-a", max_tokens=8)
    result_b = await fake.complete(messages=messages, model="model-b", max_tokens=8)

    assert result_a.text == "from model A"
    assert result_b.text == "from model B"


async def test_fake_same_model_different_messages_are_separate_keys():
    """Same model but different messages → different entries."""
    fake = FakeInferenceGateway()
    model = "claude-haiku-4-5"

    messages_a = [{"role": "user", "content": "query A"}]
    messages_b = [{"role": "user", "content": "query B"}]

    canned_a = InferenceResult(
        text="answer A", cost_usd=0.001, tokens_in=1, tokens_out=2,
        model=model, latency_ms=1.0,
    )
    canned_b = InferenceResult(
        text="answer B", cost_usd=0.002, tokens_in=1, tokens_out=3,
        model=model, latency_ms=2.0,
    )
    fake.register(model=model, messages=messages_a, result=canned_a)
    fake.register(model=model, messages=messages_b, result=canned_b)

    result_a = await fake.complete(messages=messages_a, model=model, max_tokens=8)
    result_b = await fake.complete(messages=messages_b, model=model, max_tokens=8)

    assert result_a.text == "answer A"
    assert result_b.text == "answer B"


async def test_fake_calls_returns_copy_not_reference():
    """calls property returns a copy; mutations don't affect internal state."""
    fake = FakeInferenceGateway()
    await fake.complete(
        messages=[{"role": "user", "content": "x"}], model="m", max_tokens=4
    )
    snapshot = fake.calls
    snapshot.clear()
    assert fake.call_count() == 1  # internal state unaffected


# ---------------------------------------------------------------------------
# _prompt_hash helper
# ---------------------------------------------------------------------------


def test_prompt_hash_is_deterministic():
    """Same messages always produce the same hash."""
    msgs = [{"role": "user", "content": "hello"}]
    assert _prompt_hash(msgs) == _prompt_hash(msgs)


def test_prompt_hash_differs_for_different_content():
    """Different message content → different hash."""
    a = [{"role": "user", "content": "hello"}]
    b = [{"role": "user", "content": "world"}]
    assert _prompt_hash(a) != _prompt_hash(b)


def test_prompt_hash_is_16_chars():
    """Hash is truncated to 16 hex chars."""
    assert len(_prompt_hash([{"role": "user", "content": "x"}])) == 16


def test_prompt_hash_order_insensitive_to_dict_key_order():
    """Key order inside each message dict doesn't affect the hash."""
    a = [{"role": "user", "content": "hi"}]
    b = [{"content": "hi", "role": "user"}]
    assert _prompt_hash(a) == _prompt_hash(b)
