"""Tests for oxi_core.v3.routing.

Coverage targets from the roadmap spec (T2-39):
  - Known role → expected adapter and model.
  - Unknown role → RoleNotConfiguredError.
  - YAML missing → RoutingConfigError with the file path in the message.
  - YAML structurally invalid (not a mapping, missing required fields) →
    RoutingConfigError.
  - ModelChoice is frozen (immutable after construction).
  - reload_routing_table() clears the cache so a fresh file is read.
  - route_for accepts an optional task= kwarg without error.
  - fallback_chain is a tuple (not a list) with the correct values.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from oxi_core.v3.routing import (
    ModelChoice,
    RoleNotConfiguredError,
    RoutingConfigError,
    reload_routing_table,
    route_for,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Ensure each test starts with a clean routing-table cache."""
    reload_routing_table()
    yield
    reload_routing_table()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_routing_yaml(tmp_path: Path, content: str) -> Path:
    """Write *content* to a routing.yaml inside *tmp_path* and return it."""
    p = tmp_path / "routing.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests against the bundled routing.yaml
# ---------------------------------------------------------------------------


class TestBundledRoutingTable:
    """Tests that exercise the real defaults/routing.yaml shipped with oxi-core."""

    def test_worker_returns_expected_adapter(self):
        choice = route_for("worker")
        assert choice.adapter_name == "anthropic"

    def test_worker_returns_expected_model(self):
        choice = route_for("worker")
        assert choice.model_id == "claude-opus-4-7"

    def test_worker_fallback_chain_is_ordered(self):
        choice = route_for("worker")
        assert choice.fallback_chain == (
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        )

    def test_critic_returns_expected_model(self):
        choice = route_for("critic")
        assert choice.model_id == "claude-sonnet-4-6"

    def test_critic_has_one_fallback(self):
        choice = route_for("critic")
        assert "claude-haiku-4-5-20251001" in choice.fallback_chain

    def test_heartbeat_triage_uses_haiku(self):
        choice = route_for("heartbeat-triage")
        assert choice.model_id == "claude-haiku-4-5-20251001"

    def test_heartbeat_triage_has_empty_fallback(self):
        choice = route_for("heartbeat-triage")
        assert choice.fallback_chain == ()

    def test_prompt_injection_screen_uses_haiku(self):
        choice = route_for("prompt-injection-screen")
        assert choice.model_id == "claude-haiku-4-5-20251001"

    def test_prompt_injection_screen_has_empty_fallback(self):
        choice = route_for("prompt-injection-screen")
        assert choice.fallback_chain == ()

    def test_unknown_role_raises_role_not_configured_error(self):
        with pytest.raises(RoleNotConfiguredError) as exc_info:
            route_for("no-such-role")
        # Message must mention the role name.
        assert "no-such-role" in str(exc_info.value)

    def test_role_not_configured_error_is_a_key_error(self):
        """Callers that catch KeyError should still intercept this."""
        with pytest.raises(KeyError):
            route_for("definitely-not-a-role")

    def test_role_not_configured_error_exposes_role_attribute(self):
        try:
            route_for("ghost-role")
        except RoleNotConfiguredError as exc:
            assert exc.role == "ghost-role"
        else:
            pytest.fail("expected RoleNotConfiguredError")

    def test_route_for_accepts_task_kwarg(self):
        """task= is reserved for T2-40; must not raise today."""
        choice = route_for("worker", task={"id": 1, "title": "do thing"})
        assert choice.model_id == "claude-opus-4-7"

    def test_route_for_accepts_none_task(self):
        """Explicit task=None must not raise."""
        choice = route_for("critic", task=None)
        assert isinstance(choice, ModelChoice)

    def test_model_choice_is_frozen(self):
        """ModelChoice must be immutable — frozen=True on the dataclass."""
        choice = route_for("worker")
        with pytest.raises((AttributeError, TypeError)):
            choice.model_id = "hacked"  # type: ignore[misc]

    def test_fallback_chain_is_a_tuple(self):
        """Not a list — callers depend on hashability / immutability."""
        choice = route_for("worker")
        assert isinstance(choice.fallback_chain, tuple)

    def test_table_is_cached_across_calls(self):
        """Two calls to route_for() return equal ModelChoice objects."""
        a = route_for("critic")
        b = route_for("critic")
        assert a == b

    def test_all_four_default_roles_are_present(self):
        roles = ("worker", "critic", "heartbeat-triage", "prompt-injection-screen")
        for role in roles:
            choice = route_for(role)
            assert choice.adapter_name, f"adapter_name empty for {role}"
            assert choice.model_id, f"model_id empty for {role}"


# ---------------------------------------------------------------------------
# Tests with a synthetic YAML file (via monkeypatch)
# ---------------------------------------------------------------------------


class TestSyntheticYaml:
    """Tests that swap out the canonical YAML path for a tmp file."""

    def _patch_yaml_path(self, monkeypatch: pytest.MonkeyPatch, new_path: Path) -> None:
        """Point routing._ROUTING_YAML at *new_path* and clear the cache."""
        import oxi_core.v3.routing as routing_mod
        monkeypatch.setattr(routing_mod, "_ROUTING_YAML", new_path)
        reload_routing_table()

    def test_yaml_missing_raises_routing_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "nonexistent.yaml"
        self._patch_yaml_path(monkeypatch, missing)
        with pytest.raises(RoutingConfigError) as exc_info:
            route_for("worker")
        # Error message must reference the file path.
        assert str(missing) in str(exc_info.value)

    def test_yaml_not_a_mapping_raises_routing_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(tmp_path, "- just\n- a\n- list\n")
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoutingConfigError, match="mapping"):
            route_for("worker")

    def test_yaml_role_missing_adapter_name_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            worker:
              model_id: claude-opus-4-7
              fallback_chain: []
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoutingConfigError, match="adapter_name"):
            route_for("worker")

    def test_yaml_role_missing_model_id_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            worker:
              adapter_name: anthropic
              fallback_chain: []
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoutingConfigError, match="model_id"):
            route_for("worker")

    def test_yaml_role_not_a_mapping_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            worker: just-a-string
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoutingConfigError):
            route_for("worker")

    def test_custom_role_routes_correctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            custom-role:
              adapter_name: my-adapter
              model_id: my-model-v1
              fallback_chain:
                - my-model-v0
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        choice = route_for("custom-role")
        assert choice.adapter_name == "my-adapter"
        assert choice.model_id == "my-model-v1"
        assert choice.fallback_chain == ("my-model-v0",)

    def test_unknown_role_in_custom_yaml_raises_role_not_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            worker:
              adapter_name: anthropic
              model_id: claude-opus-4-7
              fallback_chain: []
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoleNotConfiguredError):
            route_for("critic")

    def test_empty_fallback_chain_in_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = _write_routing_yaml(
            tmp_path,
            """\
            triage:
              adapter_name: anthropic
              model_id: claude-haiku-4-5-20251001
              fallback_chain: []
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        choice = route_for("triage")
        assert choice.fallback_chain == ()

    def test_missing_fallback_chain_key_defaults_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """fallback_chain is optional in YAML; absence → empty tuple."""
        p = _write_routing_yaml(
            tmp_path,
            """\
            triage:
              adapter_name: anthropic
              model_id: claude-haiku-4-5-20251001
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        choice = route_for("triage")
        assert choice.fallback_chain == ()

    def test_reload_routing_table_picks_up_new_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """After reload, route_for reads the (updated) YAML, not the cache."""
        p = _write_routing_yaml(
            tmp_path,
            """\
            worker:
              adapter_name: anthropic
              model_id: claude-sonnet-4-6
              fallback_chain: []
            """,
        )
        self._patch_yaml_path(monkeypatch, p)
        first = route_for("worker")
        assert first.model_id == "claude-sonnet-4-6"

        # Update the file and reload.
        new_content = (
            "worker:\n"
            "  adapter_name: anthropic\n"
            "  model_id: claude-haiku-4-5-20251001\n"
            "  fallback_chain: []\n"
        )
        p.write_text(new_content, encoding="utf-8")
        reload_routing_table()
        second = route_for("worker")
        assert second.model_id == "claude-haiku-4-5-20251001"

    def test_invalid_yaml_syntax_raises_routing_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        p = tmp_path / "routing.yaml"
        p.write_text("key: [unclosed bracket\n", encoding="utf-8")
        self._patch_yaml_path(monkeypatch, p)
        with pytest.raises(RoutingConfigError, match="YAML parse error"):
            route_for("worker")
