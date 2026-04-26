"""CI smoke test — OpenRouter as InferenceGateway target (T2-42).

Acceptance criteria from the roadmap:

1. ``route_for("heartbeat-triage")`` with ``adapter_name="openrouter"`` and
   ``model_id="openrouter/qwen/qwen3-235b-a22b"`` — verified by patching
   the bundled routing table to the OpenRouter recipe and asserting the
   returned ModelChoice.

2. A ``heartbeat-triage`` call routed to an OpenRouter model via the
   InferenceGateway produces an ``InferenceResult`` whose ``cost_usd`` is
   extracted from ``usage.total_cost`` in the response body (the OpenRouter
   quirk — OpenRouter does not set the ``x-litellm-response-cost`` header).

3. When *both* the header and ``usage.total_cost`` are present, the header
   takes precedence (LiteLLM standard path is never bypassed for non-
   OpenRouter backends).

4. The InferenceGateway proxies the OpenRouter model string cleanly — the
   outbound request body carries the full ``"openrouter/..."`` model string
   and ``"stream": false``.

5. The ``inference_rates.yaml`` contains the four required OpenRouter model
   entries (Qwen 3.235B A22B, Llama 3.3 70B, DeepSeek V3, Mistral Large)
   with valid rate fields.

6. The ``inference.yaml`` provider block for ``openrouter`` has ``base_url``,
   ``litellm_provider``, and ``virtual_key_env`` set correctly.

7. The ``routing.yaml`` raw text documents the OpenRouter recipe candidates
   as comments, and the active heartbeat-triage role remains ``anthropic``.

8. Cost within 5 % of a recorded fixture: a canned response with
   ``usage.total_cost=0.000182`` is within 5 % of the expected value.

No real network calls are made.  The InferenceGateway is wired to a
capturing fake client (same pattern as test_grok_gateway_smoke.py).
The routing table is monkeypatched so the bundled routing.yaml is not
permanently altered.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from oxi_core.v3.inference import (
    InferenceGateway,
)
from oxi_core.v3.routing import (
    reload_routing_table,
    route_for,
)

# ---------------------------------------------------------------------------
# Paths to bundled defaults
# ---------------------------------------------------------------------------

_DEFAULTS_DIR = (
    Path(__file__).parent.parent / "src" / "oxi_core" / "defaults"
)
_INFERENCE_YAML = _DEFAULTS_DIR / "inference.yaml"
_INFERENCE_RATES_YAML = _DEFAULTS_DIR / "inference_rates.yaml"
_ROUTING_YAML = _DEFAULTS_DIR / "routing.yaml"

# ---------------------------------------------------------------------------
# OpenRouter model identifiers used across tests
# ---------------------------------------------------------------------------

_QWEN3 = "openrouter/qwen/qwen3-235b-a22b"
_LLAMA33 = "openrouter/meta-llama/llama-3.3-70b-instruct"
_DEEPSEEK_V3 = "openrouter/deepseek/deepseek-chat-v3-0324"
_MISTRAL_LARGE = "openrouter/mistralai/mistral-large-2411"

# ---------------------------------------------------------------------------
# Helpers — fake httpx client (mirrors test_grok_gateway_smoke.py)
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    body: dict[str, Any] | None = None,
    cost_header: str | None = None,   # OpenRouter does NOT set this
    body_cost: float | None = 0.000182,
    model: str = _QWEN3,
) -> httpx.Response:
    """Minimal fake httpx.Response for OpenRouter gateway tests.

    By default no ``x-litellm-response-cost`` header is set (matching real
    OpenRouter behaviour) and ``usage.total_cost`` is populated instead.
    """
    if body is None:
        usage: dict[str, Any] = {
            "prompt_tokens": 15,
            "completion_tokens": 10,
            "total_tokens": 25,
        }
        if body_cost is not None:
            usage["total_cost"] = body_cost

        body = {
            "id": "chatcmpl-openrouter-test",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "heartbeat-triage: all clear",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }
    raw_body = json.dumps(body).encode()
    headers: dict[str, str] = {"content-type": "application/json"}
    if cost_header is not None:
        headers["x-litellm-response-cost"] = cost_header
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=raw_body,
        request=httpx.Request(
            "POST", "http://litellm.tailnet.local:4000/chat/completions"
        ),
    )


class _CapturingClient:
    """Records ``post()`` calls; mirrors the one in test_grok_gateway_smoke.py."""

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
    *,
    model: str = _QWEN3,
    cost_header: str | None = None,
    body_cost: float | None = 0.000182,
) -> tuple[InferenceGateway, _CapturingClient]:
    response = _make_response(model=model, cost_header=cost_header, body_cost=body_cost)
    cap = _CapturingClient(response)
    gw = InferenceGateway(
        base_url="http://litellm.tailnet.local:4000",
        api_key="sk-openrouter-virtual-test",
        client=cap,  # type: ignore[arg-type]
    )
    return gw, cap


# ---------------------------------------------------------------------------
# Routing table fixture — patches the bundled yaml to the OpenRouter recipe
# ---------------------------------------------------------------------------


@pytest.fixture()
def openrouter_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch the routing table so heartbeat-triage resolves to OpenRouter."""
    or_yaml = textwrap.dedent(
        f"""\
        heartbeat-triage:
          adapter_name: openrouter
          model_id: {_QWEN3}
          fallback_chain:
            - {_LLAMA33}
        """
    )
    p = tmp_path / "routing_openrouter.yaml"
    p.write_text(or_yaml, encoding="utf-8")

    import oxi_core.v3.routing as routing_mod

    monkeypatch.setattr(routing_mod, "_ROUTING_YAML", p)
    reload_routing_table()
    yield
    reload_routing_table()


# ---------------------------------------------------------------------------
# 1. Routing table — OpenRouter recipe resolves correctly
# ---------------------------------------------------------------------------


def test_openrouter_route_returns_openrouter_adapter(openrouter_routing):
    """route_for('heartbeat-triage') → adapter_name == 'openrouter'."""
    choice = route_for("heartbeat-triage")
    assert choice.adapter_name == "openrouter"


def test_openrouter_route_returns_qwen3_model(openrouter_routing):
    """route_for('heartbeat-triage') → model_id == openrouter/qwen/qwen3-235b-a22b."""
    choice = route_for("heartbeat-triage")
    assert choice.model_id == _QWEN3


def test_openrouter_route_fallback_chain_contains_llama(openrouter_routing):
    """OpenRouter heartbeat-triage recipe falls back to Llama 3.3."""
    choice = route_for("heartbeat-triage")
    assert _LLAMA33 in choice.fallback_chain


def test_openrouter_route_model_choice_is_frozen(openrouter_routing):
    """ModelChoice from OpenRouter routing is still immutable."""
    choice = route_for("heartbeat-triage")
    with pytest.raises((AttributeError, TypeError)):
        choice.model_id = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Gateway reads cost from usage.total_cost (OpenRouter quirk)
# ---------------------------------------------------------------------------


async def test_openrouter_cost_from_body_when_header_absent():
    """cost_usd is read from usage.total_cost when x-litellm-response-cost absent."""
    gw, _ = _make_gateway(model=_QWEN3, cost_header=None, body_cost=0.000182)
    result = await gw.complete(
        messages=[{"role": "user", "content": "heartbeat triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert result.cost_usd == pytest.approx(0.000182)


async def test_openrouter_cost_within_5pct_of_fixture():
    """cost_usd from body is within 5 % of the expected fixture value.

    Fixture: a canned call to openrouter/qwen/qwen3-235b-a22b returning
    usage.total_cost=0.000182.  Acceptance criterion: |actual - expected|
    / expected <= 0.05.
    """
    expected = 0.000182
    gw, _ = _make_gateway(model=_QWEN3, cost_header=None, body_cost=expected)
    result = await gw.complete(
        messages=[{"role": "user", "content": "heartbeat triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert abs(result.cost_usd - expected) / expected <= 0.05


# ---------------------------------------------------------------------------
# 3. Header takes precedence over usage.total_cost
# ---------------------------------------------------------------------------


async def test_header_takes_precedence_over_body_cost():
    """When both header and usage.total_cost are present, header wins."""
    # Header says 0.001, body says 0.000182.
    gw, _ = _make_gateway(model=_QWEN3, cost_header="0.001", body_cost=0.000182)
    result = await gw.complete(
        messages=[{"role": "user", "content": "check"}],
        model=_QWEN3,
        max_tokens=16,
    )
    assert result.cost_usd == pytest.approx(0.001)


# ---------------------------------------------------------------------------
# 4. Gateway proxies OpenRouter — stream=False lock + model string forwarded
# ---------------------------------------------------------------------------


async def test_openrouter_gateway_stream_is_always_false():
    """InferenceGateway sends stream=False for OpenRouter models."""
    gw, cap = _make_gateway(model=_QWEN3)
    await gw.complete(
        messages=[{"role": "user", "content": "heartbeat triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert len(cap.requests) == 1
    assert cap.requests[0]["json"]["stream"] is False


async def test_openrouter_gateway_model_string_forwarded_verbatim():
    """The openrouter/... model string is forwarded as-is to the LiteLLM proxy."""
    gw, cap = _make_gateway(model=_QWEN3)
    await gw.complete(
        messages=[{"role": "user", "content": "triage"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert cap.requests[0]["json"]["model"] == _QWEN3


async def test_openrouter_gateway_result_has_text():
    """InferenceResult.text is populated from the OpenRouter completion."""
    gw, _ = _make_gateway(model=_QWEN3)
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert result.text == "heartbeat-triage: all clear"


async def test_openrouter_gateway_result_has_correct_model():
    """InferenceResult.model reflects the model echoed by the OpenRouter response."""
    gw, _ = _make_gateway(model=_QWEN3)
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert result.model == _QWEN3


async def test_openrouter_gateway_auth_header_present():
    """Virtual API key is forwarded in the Authorization header."""
    gw, cap = _make_gateway(model=_QWEN3)
    await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert (
        cap.requests[0]["headers"]["Authorization"]
        == "Bearer sk-openrouter-virtual-test"
    )


async def test_openrouter_gateway_url_is_chat_completions():
    """POST goes to <litellm-base-url>/chat/completions for OpenRouter too."""
    gw, cap = _make_gateway(model=_QWEN3)
    await gw.complete(
        messages=[{"role": "user", "content": "x"}],
        model=_QWEN3,
        max_tokens=8,
    )
    assert cap.requests[0]["url"].endswith("/chat/completions")


async def test_openrouter_gateway_caller_stream_true_overridden():
    """Caller cannot bypass the stream=False lock for OpenRouter models."""
    gw, cap = _make_gateway(model=_LLAMA33)
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model=_LLAMA33,
        max_tokens=16,
        stream=True,  # must be silently overridden
    )
    assert cap.requests[0]["json"]["stream"] is False


async def test_openrouter_gateway_tokens_parsed():
    """Token counts from the OpenRouter response body are surfaced correctly."""
    gw, _ = _make_gateway(model=_QWEN3)
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage"}],
        model=_QWEN3,
        max_tokens=32,
    )
    assert result.tokens_in == 15
    assert result.tokens_out == 10


async def test_openrouter_cost_zero_when_body_cost_absent(caplog):
    """When both header and usage.total_cost are absent, cost_usd=0.0 + warning."""
    import logging

    gw, _ = _make_gateway(model=_QWEN3, cost_header=None, body_cost=None)
    with caplog.at_level(logging.WARNING, logger="oxi_core.v3.inference"):
        result = await gw.complete(
            messages=[{"role": "user", "content": "check"}],
            model=_QWEN3,
            max_tokens=8,
        )
    assert result.cost_usd == pytest.approx(0.0)
    assert any("cost_header_missing" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. inference_rates.yaml — OpenRouter entries present and well-formed
# ---------------------------------------------------------------------------


class TestOpenRouterInferenceRatesYaml:
    """Asserts the OpenRouter model entries in defaults/inference_rates.yaml."""

    @pytest.fixture(autouse=True)
    def _load(self):
        assert _INFERENCE_RATES_YAML.is_file(), (
            f"inference_rates.yaml not found at {_INFERENCE_RATES_YAML}"
        )
        raw = yaml.safe_load(_INFERENCE_RATES_YAML.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "inference_rates.yaml must be a mapping"
        self.rates = raw

    def test_qwen3_is_present(self):
        assert _QWEN3 in self.rates

    def test_llama33_is_present(self):
        assert _LLAMA33 in self.rates

    def test_deepseek_v3_is_present(self):
        assert _DEEPSEEK_V3 in self.rates

    def test_mistral_large_is_present(self):
        assert _MISTRAL_LARGE in self.rates

    def test_qwen3_has_input_rate(self):
        entry = self.rates[_QWEN3]
        assert "input_usd_per_mtok" in entry
        assert isinstance(entry["input_usd_per_mtok"], (int, float))
        assert entry["input_usd_per_mtok"] > 0

    def test_qwen3_has_output_rate(self):
        entry = self.rates[_QWEN3]
        assert "output_usd_per_mtok" in entry
        assert isinstance(entry["output_usd_per_mtok"], (int, float))
        assert entry["output_usd_per_mtok"] > 0

    def test_all_openrouter_entries_have_provider_openrouter(self):
        for model_id in (_QWEN3, _LLAMA33, _DEEPSEEK_V3, _MISTRAL_LARGE):
            entry = self.rates[model_id]
            assert entry.get("provider") == "openrouter", (
                f"{model_id}: expected provider='openrouter', "
                f"got {entry.get('provider')!r}"
            )

    def test_all_openrouter_entries_have_notes(self):
        for model_id in (_QWEN3, _LLAMA33, _DEEPSEEK_V3, _MISTRAL_LARGE):
            entry = self.rates[model_id]
            assert "notes" in entry, f"{model_id} is missing 'notes' field"

    def test_anthropic_baselines_still_present(self):
        """Ensure the Anthropic reference entries weren't accidentally removed."""
        assert "claude-haiku-4-5-20251001" in self.rates
        assert "claude-sonnet-4-6" in self.rates
        assert "claude-opus-4-7" in self.rates

    def test_xai_baselines_still_present(self):
        """Ensure the xAI entries (T2-41) weren't accidentally removed."""
        assert "xai/grok-2" in self.rates
        assert "xai/grok-4-fast" in self.rates
        assert "xai/grok-code-fast-1" in self.rates


# ---------------------------------------------------------------------------
# 6. inference.yaml — openrouter provider block is well-formed
# ---------------------------------------------------------------------------


class TestOpenRouterInferenceYaml:
    """Asserts the openrouter provider block in defaults/inference.yaml."""

    @pytest.fixture(autouse=True)
    def _load(self):
        assert _INFERENCE_YAML.is_file(), (
            f"inference.yaml not found at {_INFERENCE_YAML}"
        )
        raw = yaml.safe_load(_INFERENCE_YAML.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "inference.yaml must be a mapping"
        self.providers = raw

    def test_openrouter_provider_present(self):
        assert "openrouter" in self.providers

    def test_openrouter_base_url_is_openrouter_v1(self):
        assert self.providers["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"

    def test_openrouter_litellm_provider_is_openrouter(self):
        assert self.providers["openrouter"]["litellm_provider"] == "openrouter"

    def test_openrouter_virtual_key_env_present(self):
        """Operators need to know which env var to set for the virtual key."""
        env_var = self.providers["openrouter"].get("virtual_key_env")
        assert env_var is not None
        assert isinstance(env_var, str)
        assert len(env_var) > 0

    def test_openrouter_virtual_key_env_name(self):
        """Virtual key env var follows the LITELLM_VIRTUAL_KEY_<PROVIDER> pattern."""
        env_var = self.providers["openrouter"]["virtual_key_env"]
        assert env_var == "LITELLM_VIRTUAL_KEY_OPENROUTER"

    def test_anthropic_provider_still_present(self):
        """inference.yaml must retain the anthropic block."""
        assert "anthropic" in self.providers

    def test_xai_provider_still_present(self):
        """inference.yaml must retain the xai block (T2-41)."""
        assert "xai" in self.providers


# ---------------------------------------------------------------------------
# 7. routing.yaml — OpenRouter recipe candidates documented, defaults intact
# ---------------------------------------------------------------------------


class TestRoutingYamlOpenRouterRecipes:
    """Check that routing.yaml documents the OpenRouter recipe candidates."""

    @pytest.fixture(autouse=True)
    def _load_raw(self):
        self.raw_text = _ROUTING_YAML.read_text(encoding="utf-8")

    def test_qwen3_mentioned_in_routing(self):
        """Qwen 3 model is documented as an OpenRouter recipe candidate."""
        assert "qwen3-235b-a22b" in self.raw_text

    def test_llama33_mentioned_in_routing(self):
        assert "llama-3.3-70b-instruct" in self.raw_text

    def test_deepseek_mentioned_in_routing(self):
        assert "deepseek-chat-v3-0324" in self.raw_text

    def test_mistral_large_mentioned_in_routing(self):
        assert "mistral-large-2411" in self.raw_text

    def test_openrouter_adapter_name_mentioned_in_routing(self):
        """The adapter_name: openrouter pattern is documented."""
        assert "adapter_name: openrouter" in self.raw_text

    def test_heartbeat_triage_default_remains_anthropic(self):
        """Active heartbeat-triage role must still default to anthropic (not openrouter)."""
        choice = route_for("heartbeat-triage")
        assert choice.adapter_name == "anthropic", (
            "heartbeat-triage default must remain 'anthropic'; "
            "the OpenRouter recipe should be commented out"
        )

    def test_routing_yaml_parses_cleanly_with_openrouter_comments(self):
        """routing.yaml with OpenRouter comments remains valid YAML."""
        import yaml as yaml_mod

        parsed = yaml_mod.safe_load(self.raw_text)
        assert isinstance(parsed, dict)
        for role in ("worker", "critic", "heartbeat-triage", "prompt-injection-screen"):
            assert role in parsed, f"role {role!r} missing from parsed routing.yaml"
