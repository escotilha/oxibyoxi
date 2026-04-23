"""Tests for the Adapter protocol, defaults, and registration."""

from __future__ import annotations

import pytest

from oxi_core import adapter as adapter_mod
from oxi_core import defaults
from oxi_core.adapter import (
    Adapter,
    AdapterNotRegisteredError,
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    InvalidAdapterError,
    NamingConfig,
    PathsConfig,
    PromoteRecipe,
    RoadmapItem,
    clear_adapter,
    get_active_adapter,
    register_adapter,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _MinimalAdapter:
    """Adapter returning sensible defaults for every method."""

    def naming(self) -> NamingConfig:
        return NamingConfig()

    def paths(self) -> PathsConfig:
        return PathsConfig()

    def budget(self) -> BudgetCaps:
        return BudgetCaps()

    def github_repo(self) -> str:
        return "owner/repo"

    def roadmap_location(self) -> str:
        return "roadmap.md"

    def branch_prefixes(self) -> tuple[str, ...]:
        return defaults.BRANCH_PREFIXES

    def dispatch_hosts(self) -> tuple[DispatchHost, ...]:
        return (DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),)

    def promote_recipe(self) -> None:
        return None

    def plan_tier(self) -> str:
        return "standard"

    def policy(self) -> DispatchPolicy:
        return DispatchPolicy()


class _AdapterMissingPlanTier(_MinimalAdapter):
    def plan_tier(self) -> str:
        return ""


class _NotAnAdapter:
    """Object that does not satisfy the protocol."""

    def some_other_method(self) -> int:
        return 42


@pytest.fixture(autouse=True)
def _reset_adapter_state():
    """Ensure each test starts with no registered adapter."""
    clear_adapter()
    yield
    clear_adapter()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_instance_name_default_is_project_neutral():
    assert defaults.INSTANCE_NAME == "oxi"


def test_branch_prefixes_default_is_a_tuple():
    assert isinstance(defaults.BRANCH_PREFIXES, tuple)
    assert all(p.endswith("/") for p in defaults.BRANCH_PREFIXES)


def test_budget_defaults_are_conservative():
    assert defaults.PER_TASK_OPUS_USD <= defaults.DAILY_HARD_CAP_USD
    assert defaults.DAILY_SOFT_WARN_USD < defaults.DAILY_HARD_CAP_USD


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


def test_naming_config_defaults():
    cfg = NamingConfig()
    assert cfg.instance_name == defaults.INSTANCE_NAME
    assert cfg.session_code_regex == defaults.SESSION_CODE_REGEX


def test_budget_caps_defaults():
    cfg = BudgetCaps()
    assert cfg.daily_hard_cap == defaults.DAILY_HARD_CAP_USD


def test_paths_config_defaults_allow_none():
    cfg = PathsConfig()
    assert cfg.repo_root is None
    assert cfg.oxi_dir == defaults.OXI_DIR


def test_dispatch_host_is_frozen():
    from dataclasses import FrozenInstanceError

    h = DispatchHost(name="x", ssh_alias=None, max_concurrent=1, worktree_root="/tmp")
    with pytest.raises(FrozenInstanceError):
        h.name = "y"  # type: ignore[misc]


def test_roadmap_item_defaults():
    item = RoadmapItem(identifier="T0-1", tier=0, title="do a thing")
    assert item.subtitle == ""
    assert item.files_touched == ()


def test_promote_recipe_requires_all_fields():
    # No defaults — promote is all-or-nothing.
    with pytest.raises(TypeError):
        PromoteRecipe()  # type: ignore[call-arg]


def test_dispatch_policy_defaults():
    p = DispatchPolicy()
    assert p.auto_merge is False
    assert p.tier_zero_absorb is True
    assert p.skill_weights == {}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_minimal_adapter_conforms_to_protocol():
    # runtime_checkable lets us use isinstance against the Protocol.
    assert isinstance(_MinimalAdapter(), Adapter)


def test_non_adapter_does_not_conform():
    assert not isinstance(_NotAnAdapter(), Adapter)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_and_get_adapter():
    a = _MinimalAdapter()
    register_adapter(a)
    assert get_active_adapter() is a


def test_get_without_register_raises():
    with pytest.raises(AdapterNotRegisteredError):
        get_active_adapter()


def test_clear_removes_active_adapter():
    register_adapter(_MinimalAdapter())
    clear_adapter()
    with pytest.raises(AdapterNotRegisteredError):
        get_active_adapter()


def test_register_rejects_non_conforming_object():
    with pytest.raises(InvalidAdapterError):
        register_adapter(_NotAnAdapter())  # type: ignore[arg-type]


def test_register_rejects_empty_plan_tier():
    """Anti-pattern #3: plan tier must be explicit."""
    with pytest.raises(InvalidAdapterError) as exc:
        register_adapter(_AdapterMissingPlanTier())
    assert "plan tier" in str(exc.value).lower()


def test_register_replaces_previous_adapter():
    a = _MinimalAdapter()
    b = _MinimalAdapter()
    register_adapter(a)
    register_adapter(b)
    assert get_active_adapter() is b


# ---------------------------------------------------------------------------
# Internal state isolation
# ---------------------------------------------------------------------------


def test_module_state_is_process_wide():
    """Confirm the registration dance uses a module-level variable.

    This is intentional — tests that forget to clear will leak state
    into subsequent tests. The autouse fixture above guards against
    that; documenting the property here so a future refactor doesn't
    introduce a thread-local and break test isolation assumptions.
    """
    assert adapter_mod._active_adapter is None
    register_adapter(_MinimalAdapter())
    assert adapter_mod._active_adapter is not None
    clear_adapter()
    assert adapter_mod._active_adapter is None
