"""CI smoke test — Mac Mini LiteLLM gateway health check (T2-36).

Acceptance criteria from the roadmap:

1. ``SelfAdapter.inference_gateway_url()`` returns an
   ``InferenceGatewayConfig`` with ``base_url`` populated from
   ``OXI_GATEWAY_BASE_URL`` and per-role ``virtual_keys`` populated
   from the corresponding ``OXI_GATEWAY_KEY_*`` env vars.

2. When ``OXI_GATEWAY_BASE_URL`` is unset, ``base_url`` is an empty
   string and ``virtual_keys`` is empty — no crash, no KeyError.

3. ``inference.yaml`` contains a ``mac-mini-gateway`` block with the
   expected ``virtual_keys`` and ``virtual_key_envs`` fields.

4. A live ``/health`` probe against the configured gateway URL
   succeeds — **skipped** when ``OXI_INFERENCE_OFFLINE=1`` (offline
   mode, CI without a Tailscale peer, planned maintenance).

No real network calls are made in tests 1–3.  Test 4 makes a live
HTTP call only when the gateway env var is set and offline mode is
not requested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths to bundled defaults
# ---------------------------------------------------------------------------

_DEFAULTS_DIR = (
    Path(__file__).parent.parent / "src" / "oxi_core" / "defaults"
)
_INFERENCE_YAML = _DEFAULTS_DIR / "inference.yaml"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_offline() -> bool:
    """Return True when OXI_INFERENCE_OFFLINE=1 (skip live probes)."""
    return os.environ.get("OXI_INFERENCE_OFFLINE", "").strip() == "1"


# ---------------------------------------------------------------------------
# 1 & 2. SelfAdapter.inference_gateway_url() — env var resolution
# ---------------------------------------------------------------------------


class TestInferenceGatewayUrl:
    """Unit tests for SelfAdapter.inference_gateway_url() (no network)."""

    def test_base_url_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """OXI_GATEWAY_BASE_URL is surfaced in base_url."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.setenv("OXI_GATEWAY_BASE_URL", "http://mac-mini.tail1234.ts.net:4000")
        monkeypatch.delenv("OXI_GATEWAY_KEY_HEARTBEAT", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_CLASSIFIER", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_SUMMARY", raising=False)

        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert cfg.base_url == "http://mac-mini.tail1234.ts.net:4000"

    def test_base_url_trailing_slash_stripped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Trailing slashes are stripped from the base URL."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.setenv("OXI_GATEWAY_BASE_URL", "http://mac-mini.tail1234.ts.net:4000/")
        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert not cfg.base_url.endswith("/")

    def test_base_url_empty_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When OXI_GATEWAY_BASE_URL is absent, base_url is empty string."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.delenv("OXI_GATEWAY_BASE_URL", raising=False)
        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert cfg.base_url == ""

    def test_virtual_keys_all_provisioned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """All three role keys appear in virtual_keys when env vars are set."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.setenv("OXI_GATEWAY_BASE_URL", "http://localhost:4000")
        monkeypatch.setenv("OXI_GATEWAY_KEY_HEARTBEAT", "sk-hb-test")
        monkeypatch.setenv("OXI_GATEWAY_KEY_CLASSIFIER", "sk-cl-test")
        monkeypatch.setenv("OXI_GATEWAY_KEY_SUMMARY", "sk-su-test")

        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert cfg.virtual_keys == {
            "heartbeat": "sk-hb-test",
            "classifier": "sk-cl-test",
            "summary": "sk-su-test",
        }

    def test_virtual_keys_partial_provisioning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Only env vars that are set appear in virtual_keys."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.setenv("OXI_GATEWAY_BASE_URL", "http://localhost:4000")
        monkeypatch.setenv("OXI_GATEWAY_KEY_HEARTBEAT", "sk-hb-test")
        monkeypatch.delenv("OXI_GATEWAY_KEY_CLASSIFIER", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_SUMMARY", raising=False)

        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert "heartbeat" in cfg.virtual_keys
        assert "classifier" not in cfg.virtual_keys
        assert "summary" not in cfg.virtual_keys

    def test_virtual_keys_empty_when_all_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No virtual_keys when no key env vars are set."""
        from oxi_adapter_self import SelfAdapter

        monkeypatch.delenv("OXI_GATEWAY_BASE_URL", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_HEARTBEAT", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_CLASSIFIER", raising=False)
        monkeypatch.delenv("OXI_GATEWAY_KEY_SUMMARY", raising=False)

        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert cfg.virtual_keys == {}

    def test_return_type_is_inference_gateway_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """inference_gateway_url() always returns an InferenceGatewayConfig."""
        from oxi_adapter_self import InferenceGatewayConfig, SelfAdapter

        monkeypatch.delenv("OXI_GATEWAY_BASE_URL", raising=False)
        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        assert isinstance(cfg, InferenceGatewayConfig)

    def test_config_is_frozen(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """InferenceGatewayConfig is a frozen dataclass (immutable)."""
        from dataclasses import FrozenInstanceError

        from oxi_adapter_self import SelfAdapter

        monkeypatch.setenv("OXI_GATEWAY_BASE_URL", "http://localhost:4000")
        cfg = SelfAdapter(repo_root=tmp_path).inference_gateway_url()
        with pytest.raises(FrozenInstanceError):
            cfg.base_url = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. inference.yaml — mac-mini-gateway block is well-formed
# ---------------------------------------------------------------------------


class TestInferenceYamlMacMiniGateway:
    """Asserts the mac-mini-gateway provider block in defaults/inference.yaml."""

    @pytest.fixture(autouse=True)
    def _load(self):
        assert _INFERENCE_YAML.is_file(), (
            f"inference.yaml not found at {_INFERENCE_YAML}"
        )
        raw = yaml.safe_load(_INFERENCE_YAML.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), "inference.yaml must be a mapping"
        self.providers = raw

    def test_mac_mini_gateway_block_present(self):
        """inference.yaml must have a mac-mini-gateway provider block."""
        assert "mac-mini-gateway" in self.providers, (
            "mac-mini-gateway block missing from inference.yaml"
        )

    def test_mac_mini_gateway_has_base_url(self):
        block = self.providers["mac-mini-gateway"]
        assert "base_url" in block, "mac-mini-gateway must declare base_url"
        assert isinstance(block["base_url"], str)

    def test_mac_mini_gateway_virtual_keys_block_present(self):
        block = self.providers["mac-mini-gateway"]
        assert "virtual_keys" in block, (
            "mac-mini-gateway must have a virtual_keys map"
        )
        assert isinstance(block["virtual_keys"], dict)

    def test_mac_mini_gateway_virtual_keys_has_three_roles(self):
        vk = self.providers["mac-mini-gateway"]["virtual_keys"]
        for role in ("heartbeat", "classifier", "summary"):
            assert role in vk, f"virtual_keys missing role: {role!r}"

    def test_mac_mini_gateway_virtual_key_names(self):
        vk = self.providers["mac-mini-gateway"]["virtual_keys"]
        assert vk["heartbeat"] == "oxi-heartbeat"
        assert vk["classifier"] == "oxi-classifier"
        assert vk["summary"] == "oxi-summary"

    def test_mac_mini_gateway_virtual_key_envs_block_present(self):
        block = self.providers["mac-mini-gateway"]
        assert "virtual_key_envs" in block, (
            "mac-mini-gateway must have a virtual_key_envs map"
        )
        assert isinstance(block["virtual_key_envs"], dict)

    def test_mac_mini_gateway_virtual_key_envs_correct(self):
        envs = self.providers["mac-mini-gateway"]["virtual_key_envs"]
        assert envs["heartbeat"] == "OXI_GATEWAY_KEY_HEARTBEAT"
        assert envs["classifier"] == "OXI_GATEWAY_KEY_CLASSIFIER"
        assert envs["summary"] == "OXI_GATEWAY_KEY_SUMMARY"

    def test_existing_provider_blocks_still_present(self):
        """anthropic and xai blocks must not be accidentally removed."""
        assert "anthropic" in self.providers
        assert "xai" in self.providers

    def test_inference_yaml_is_valid_yaml(self):
        """The file parses cleanly with PyYAML."""
        import yaml as yaml_mod
        parsed = yaml_mod.safe_load(_INFERENCE_YAML.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# 4. Live /health probe — skipped when OXI_INFERENCE_OFFLINE=1
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _is_offline(),
    reason="OXI_INFERENCE_OFFLINE=1 — skipping live gateway health probe",
)
@pytest.mark.skipif(
    not os.environ.get("OXI_GATEWAY_BASE_URL"),
    reason="OXI_GATEWAY_BASE_URL not set — skipping live gateway health probe",
)
def test_gateway_health_reachable():
    """Hit the configured gateway's /health endpoint and assert it is healthy.

    This test is skipped in two cases:
      - ``OXI_INFERENCE_OFFLINE=1`` is set (offline mode / CI without
        Tailscale peer / planned gateway maintenance).
      - ``OXI_GATEWAY_BASE_URL`` is not set (gateway not configured).

    When both conditions are absent (i.e., the operator has a live gateway
    and has not signalled offline mode), this test acts as a smoke check
    confirming the Tailscale route to the Mac Mini is working.
    """
    import httpx

    base_url = os.environ["OXI_GATEWAY_BASE_URL"].rstrip("/")
    health_url = f"{base_url}/health"

    try:
        response = httpx.get(health_url, timeout=10.0)
    except httpx.TransportError as exc:
        pytest.fail(
            f"Gateway at {health_url!r} is unreachable: {exc}\n\n"
            "If the Mac Mini gateway is intentionally down, set "
            "OXI_INFERENCE_OFFLINE=1 to skip this check."
        )

    assert response.status_code == 200, (
        f"Gateway /health returned HTTP {response.status_code}: {response.text[:200]}"
    )

    body = response.json()
    # LiteLLM returns {"status": "healthy"} or {"status": "ok"} depending
    # on version.  Accept either.
    status = body.get("status", "")
    assert status in ("healthy", "ok", "connected"), (
        f"Unexpected gateway health status: {status!r} (full body: {body})"
    )
