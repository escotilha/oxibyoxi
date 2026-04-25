"""CI smoke test — xAI Grok as InferenceGateway target (T2-41).

Acceptance criteria from the roadmap:

1. ``route_for("heartbeat-triage")`` with ``adapter_name="xai"`` and
   ``model_id="xai/grok-code-fast-1"`` — verified by patching the bundled
   routing table to the Grok recipe and asserting the returned ModelChoice.

2. A ``heartbeat-triage`` call routed to Grok via the InferenceGateway
   produces an ``InferenceResult`` whose ``cost_usd`` comes from the
   ``x-litellm-response-cost`` header (the same non-streaming cost-header
   lock that T2-37 established).

3. The InferenceGateway proxies the Grok model string cleanly — the
   outbound request body carries ``"model": "xai/grok-code-fast-1"`` and
   ``"stream": false``.

4. Inference rates yaml is parseable and contains the three Grok model
   entries (xai/grok-2, xai/grok-4-fast, xai/grok-code-fast-1) with the
   expected fields.

5. The ``inference.yaml`` provider block for ``xai`` has ``base_url``,
   ``litellm_provider``, and ``virtual_key_env`` set correctly.

No real network calls are made.  The InferenceGateway is wired to a
capturing fake client (same pattern as test_inference_gateway.py).
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
# Helpers — fake httpx client (mirrors test_inference_gateway.py)
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    body: dict[str, Any] | None = None,
    cost_header: str | None = "0.00042",
    model: str = "xai/grok-code-fast-1",
) -> httpx.Response:
    """Minimal fake httpx.Response for gateway tests."""
    if body is None:
        body = {
            "id": "chatcmpl-grok-test",
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
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
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
        request=httpx.Request(
            "POST", "http://litellm.tailnet.local:4000/chat/completions"
        ),
    )


class _CapturingClient:
    """Records ``post()`` calls; mirrors the one in test_inference_gateway.py."""

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
    model: str = "xai/grok-code-fast-1",
    cost_header: str | None = "0.00042",
) -> tuple[InferenceGateway, _CapturingClient]:
    response = _make_response(model=model, cost_header=cost_header)
    cap = _CapturingClient(response)
    gw = InferenceGateway(
        base_url="http://litellm.tailnet.local:4000",
        api_key="sk-xai-virtual-test",
        client=cap,  # type: ignore[arg-type]
    )
    return gw, cap


# ---------------------------------------------------------------------------
# Routing table fixture — patches the bundled yaml to the Grok recipe
# ---------------------------------------------------------------------------


@pytest.fixture()
def grok_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch the routing table so heartbeat-triage resolves to Grok."""
    grok_yaml = textwrap.dedent(
        """\
        heartbeat-triage:
          adapter_name: xai
          model_id: xai/grok-code-fast-1
          fallback_chain: []
        """
    )
    p = tmp_path / "routing_grok.yaml"
    p.write_text(grok_yaml, encoding="utf-8")

    import oxi_core.v3.routing as routing_mod

    monkeypatch.setattr(routing_mod, "_ROUTING_YAML", p)
    reload_routing_table()
    yield
    reload_routing_table()


# ---------------------------------------------------------------------------
# 1. Routing table — Grok recipe resolves correctly
# ---------------------------------------------------------------------------


def test_grok_route_returns_xai_adapter(grok_routing):
    """route_for('heartbeat-triage') → adapter_name == 'xai'."""
    choice = route_for("heartbeat-triage")
    assert choice.adapter_name == "xai"


def test_grok_route_returns_grok_code_fast_model(grok_routing):
    """route_for('heartbeat-triage') → model_id == 'xai/grok-code-fast-1'."""
    choice = route_for("heartbeat-triage")
    assert choice.model_id == "xai/grok-code-fast-1"


def test_grok_route_fallback_chain_is_empty(grok_routing):
    """Grok heartbeat-triage recipe has an empty fallback chain."""
    choice = route_for("heartbeat-triage")
    assert choice.fallback_chain == ()


def test_grok_route_model_choice_is_frozen(grok_routing):
    """ModelChoice from Grok routing is still immutable."""
    choice = route_for("heartbeat-triage")
    with pytest.raises((AttributeError, TypeError)):
        choice.model_id = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2 & 3. Gateway proxies Grok — stream=False lock + cost header
# ---------------------------------------------------------------------------


async def test_grok_gateway_stream_is_always_false():
    """InferenceGateway sends stream=False even when model is Grok."""
    gw, cap = _make_gateway(model="xai/grok-code-fast-1")
    await gw.complete(
        messages=[{"role": "user", "content": "heartbeat triage check"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert len(cap.requests) == 1
    assert cap.requests[0]["json"]["stream"] is False


async def test_grok_gateway_model_string_forwarded_verbatim():
    """The xai/... model string is forwarded as-is to the LiteLLM proxy."""
    gw, cap = _make_gateway(model="xai/grok-code-fast-1")
    await gw.complete(
        messages=[{"role": "user", "content": "triage"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert cap.requests[0]["json"]["model"] == "xai/grok-code-fast-1"


async def test_grok_gateway_cost_from_header():
    """cost_usd is extracted from x-litellm-response-cost for Grok calls."""
    gw, _ = _make_gateway(model="xai/grok-code-fast-1", cost_header="0.00042")
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert result.cost_usd == pytest.approx(0.00042)


async def test_grok_gateway_result_has_correct_model():
    """InferenceResult.model reflects the Grok model echoed by the response."""
    gw, _ = _make_gateway(model="xai/grok-code-fast-1")
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert result.model == "xai/grok-code-fast-1"


async def test_grok_gateway_result_has_text():
    """InferenceResult.text is populated from the Grok completion."""
    gw, _ = _make_gateway(model="xai/grok-code-fast-1")
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert result.text == "heartbeat-triage: all clear"


async def test_grok_gateway_auth_header_present():
    """Virtual API key is forwarded in the Authorization header."""
    gw, cap = _make_gateway(model="xai/grok-code-fast-1")
    await gw.complete(
        messages=[{"role": "user", "content": "triage check"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert cap.requests[0]["headers"]["Authorization"] == "Bearer sk-xai-virtual-test"


async def test_grok_gateway_url_is_chat_completions():
    """POST goes to <litellm-base-url>/chat/completions for Grok too."""
    gw, cap = _make_gateway(model="xai/grok-code-fast-1")
    await gw.complete(
        messages=[{"role": "user", "content": "x"}],
        model="xai/grok-code-fast-1",
        max_tokens=8,
    )
    assert cap.requests[0]["url"].endswith("/chat/completions")


async def test_grok_gateway_caller_stream_true_overridden():
    """Caller cannot bypass the stream=False lock even for Grok models."""
    gw, cap = _make_gateway(model="xai/grok-2")
    await gw.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="xai/grok-2",
        max_tokens=16,
        stream=True,  # must be silently overridden
    )
    assert cap.requests[0]["json"]["stream"] is False


async def test_grok_gateway_tokens_parsed():
    """token counts from the Grok response body are surfaced in InferenceResult."""
    gw, _ = _make_gateway(model="xai/grok-code-fast-1")
    result = await gw.complete(
        messages=[{"role": "user", "content": "triage"}],
        model="xai/grok-code-fast-1",
        max_tokens=32,
    )
    assert result.tokens_in == 12
    assert result.tokens_out == 8


# ---------------------------------------------------------------------------
# 4. inference_rates.yaml — Grok entries present and well-formed
# ---------------------------------------------------------------------------


class TestInferenceRatesYaml:
    """Asserts the Grok model entries in defaults/inference_rates.yaml."""

    @pytest.fixture(autouse=True)
    def _load(self):
        assert _INFERENCE_RATES_YAML.is_file(), (
            f"inference_rates.yaml not found at {_INFERENCE_RATES_YAML}"
        )
        raw = yaml.safe_load(_INFERENCE_RATES_YAML.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "inference_rates.yaml must be a mapping"
        self.rates = raw

    def test_grok_2_is_present(self):
        assert "xai/grok-2" in self.rates

    def test_grok_4_fast_is_present(self):
        assert "xai/grok-4-fast" in self.rates

    def test_grok_code_fast_1_is_present(self):
        assert "xai/grok-code-fast-1" in self.rates

    def test_grok_2_has_input_rate(self):
        entry = self.rates["xai/grok-2"]
        assert "input_usd_per_mtok" in entry
        assert isinstance(entry["input_usd_per_mtok"], (int, float))
        assert entry["input_usd_per_mtok"] > 0

    def test_grok_2_has_output_rate(self):
        entry = self.rates["xai/grok-2"]
        assert "output_usd_per_mtok" in entry
        assert isinstance(entry["output_usd_per_mtok"], (int, float))
        assert entry["output_usd_per_mtok"] > 0

    def test_grok_4_fast_output_rate_greater_than_grok_2(self):
        """Grok 4 Fast is a higher-tier model — its output price should be >= Grok 2."""
        g2_out = self.rates["xai/grok-2"]["output_usd_per_mtok"]
        g4_out = self.rates["xai/grok-4-fast"]["output_usd_per_mtok"]
        assert g4_out >= g2_out

    def test_grok_code_fast_has_lower_price_than_grok_2(self):
        """Grok Code Fast is the cheapest Grok variant."""
        g2_in = self.rates["xai/grok-2"]["input_usd_per_mtok"]
        gcf_in = self.rates["xai/grok-code-fast-1"]["input_usd_per_mtok"]
        assert gcf_in <= g2_in

    def test_all_grok_entries_have_provider_xai(self):
        for model_id in ("xai/grok-2", "xai/grok-4-fast", "xai/grok-code-fast-1"):
            entry = self.rates[model_id]
            assert entry.get("provider") == "xai", (
                f"{model_id}: expected provider='xai', got {entry.get('provider')!r}"
            )

    def test_anthropic_baselines_also_present(self):
        """Ensure the Anthropic reference entries weren't accidentally removed."""
        assert "claude-haiku-4-5-20251001" in self.rates
        assert "claude-sonnet-4-6" in self.rates
        assert "claude-opus-4-7" in self.rates


# ---------------------------------------------------------------------------
# 5. inference.yaml — xai provider block is well-formed
# ---------------------------------------------------------------------------


class TestInferenceYaml:
    """Asserts the xai provider block in defaults/inference.yaml."""

    @pytest.fixture(autouse=True)
    def _load(self):
        assert _INFERENCE_YAML.is_file(), (
            f"inference.yaml not found at {_INFERENCE_YAML}"
        )
        raw = yaml.safe_load(_INFERENCE_YAML.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "inference.yaml must be a mapping"
        self.providers = raw

    def test_xai_provider_present(self):
        assert "xai" in self.providers

    def test_xai_base_url_is_xai_v1(self):
        assert self.providers["xai"]["base_url"] == "https://api.x.ai/v1"

    def test_xai_litellm_provider_is_xai(self):
        assert self.providers["xai"]["litellm_provider"] == "xai"

    def test_xai_virtual_key_env_present(self):
        """Operators need to know which env var to set for the virtual key."""
        env_var = self.providers["xai"].get("virtual_key_env")
        assert env_var is not None
        assert isinstance(env_var, str)
        assert len(env_var) > 0

    def test_anthropic_provider_also_present(self):
        """inference.yaml must retain the anthropic block."""
        assert "anthropic" in self.providers

    def test_anthropic_has_base_url(self):
        assert "base_url" in self.providers["anthropic"]


# ---------------------------------------------------------------------------
# 6. routing.yaml — Grok recipe candidates are present as comments
# ---------------------------------------------------------------------------


class TestRoutingYamlGrokRecipes:
    """Check that routing.yaml documents the Grok recipe candidates."""

    @pytest.fixture(autouse=True)
    def _load_raw(self):
        self.raw_text = _ROUTING_YAML.read_text(encoding="utf-8")

    def test_grok_code_fast_mentioned_in_routing(self):
        """grok-code-fast-1 model is documented as a recipe candidate."""
        assert "grok-code-fast-1" in self.raw_text

    def test_grok_2_mentioned_in_routing(self):
        assert "grok-2" in self.raw_text

    def test_grok_4_fast_mentioned_in_routing(self):
        assert "grok-4-fast" in self.raw_text

    def test_xai_adapter_name_mentioned_in_routing(self):
        """The adapter_name: xai pattern is documented."""
        assert "adapter_name: xai" in self.raw_text

    def test_heartbeat_triage_grok_recipe_commented_out(self):
        """Active heartbeat-triage role must still default to anthropic (not xai)."""
        choice = route_for("heartbeat-triage")
        assert choice.adapter_name == "anthropic", (
            "heartbeat-triage default must remain 'anthropic'; "
            "the Grok recipe should be commented out"
        )

    def test_routing_yaml_still_parses_cleanly_with_comments(self):
        """YAML with the Grok comments remains valid YAML (no parse errors)."""
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(self.raw_text)
        assert isinstance(parsed, dict)
        # The four active roles must be present.
        for role in ("worker", "critic", "heartbeat-triage", "prompt-injection-screen"):
            assert role in parsed, f"role {role!r} missing from parsed routing.yaml"
