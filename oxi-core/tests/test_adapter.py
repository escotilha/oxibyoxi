"""Tests for the Adapter protocol, defaults, and registration."""

from __future__ import annotations

import pytest

from oxi_core import adapter as adapter_mod
from oxi_core import defaults
from oxi_core.adapter import (
    Adapter,
    AdapterLoadError,
    AdapterNotRegisteredError,
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    InvalidAdapterError,
    MultipleAdaptersError,
    NamingConfig,
    PathsConfig,
    PromoteRecipe,
    RoadmapItem,
    clear_adapter,
    get_active_adapter,
    load_adapter,
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


# ---------------------------------------------------------------------------
# load_adapter — auto-discovery
# ---------------------------------------------------------------------------


class _FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, value: str, cls):
        self.name = name
        self.value = value
        self._cls = cls

    def load(self):
        return self._cls


def test_load_adapter_is_noop_when_already_registered():
    """If an adapter is already registered, load_adapter does not replace it."""
    a = _MinimalAdapter()
    register_adapter(a)
    load_adapter()  # should not raise or change the active adapter
    assert get_active_adapter() is a


def test_load_adapter_uses_env_var(monkeypatch):
    """OXI_ADAPTER=module:ClassName registers the named class."""
    # Register _MinimalAdapter into a synthetic module so importlib can find it.
    import sys
    import types
    fake = types.ModuleType("_oxi_test_minimal_mod")
    fake.MinimalAdapter = _MinimalAdapter  # type: ignore[attr-defined]
    sys.modules["_oxi_test_minimal_mod"] = fake
    try:
        monkeypatch.setenv("OXI_ADAPTER", "_oxi_test_minimal_mod:MinimalAdapter")
        load_adapter()
        adapter = get_active_adapter()
        assert isinstance(adapter, _MinimalAdapter)
    finally:
        del sys.modules["_oxi_test_minimal_mod"]


def test_load_adapter_env_var_wins_over_entry_points(monkeypatch):
    """OXI_ADAPTER takes priority; entry-points are never consulted."""
    import sys
    import types
    fake = types.ModuleType("_oxi_test_env_wins")
    fake.MinimalAdapter = _MinimalAdapter  # type: ignore[attr-defined]
    sys.modules["_oxi_test_env_wins"] = fake
    try:
        monkeypatch.setenv("OXI_ADAPTER", "_oxi_test_env_wins:MinimalAdapter")
        # Patch entry_points to return something that would conflict if used.
        ep = _FakeEntryPoint("would-conflict", "_oxi_test_env_wins:MinimalAdapter", _MinimalAdapter)
        monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [ep, ep])

        load_adapter()  # must not raise MultipleAdaptersError
        assert get_active_adapter() is not None
    finally:
        del sys.modules["_oxi_test_env_wins"]


def test_load_adapter_env_var_bad_spec_raises(monkeypatch):
    """OXI_ADAPTER without a colon raises AdapterLoadError."""
    monkeypatch.setenv("OXI_ADAPTER", "oxi_adapter_missing_colon")
    with pytest.raises(AdapterLoadError, match="not a valid"):
        load_adapter()


def test_load_adapter_env_var_bad_module_raises(monkeypatch):
    """OXI_ADAPTER pointing at a non-existent module raises AdapterLoadError."""
    monkeypatch.setenv("OXI_ADAPTER", "oxi_no_such_module_xyz:Foo")
    with pytest.raises(AdapterLoadError, match="cannot import module"):
        load_adapter()


def test_load_adapter_env_var_bad_class_raises(monkeypatch):
    """OXI_ADAPTER with a bad class name raises AdapterLoadError."""
    import sys
    import types
    fake = types.ModuleType("_oxi_test_bad_class")
    sys.modules["_oxi_test_bad_class"] = fake
    try:
        monkeypatch.setenv("OXI_ADAPTER", "_oxi_test_bad_class:NoSuchClass")
        with pytest.raises(AdapterLoadError, match="no attribute"):
            load_adapter()
    finally:
        del sys.modules["_oxi_test_bad_class"]


def test_load_adapter_env_var_constructor_error_raises(monkeypatch):
    """OXI_ADAPTER pointing at a class that needs args raises AdapterLoadError."""

    class _NeedsArgs:
        def __init__(self, required_arg):
            pass

    # Patch the module so importlib.import_module finds it.
    import sys
    import types
    fake_mod = types.ModuleType("_fake_needs_args")
    fake_mod.NeedsArgs = _NeedsArgs  # type: ignore[attr-defined]
    sys.modules["_fake_needs_args"] = fake_mod
    try:
        monkeypatch.setenv("OXI_ADAPTER", "_fake_needs_args:NeedsArgs")
        with pytest.raises(AdapterLoadError, match="TypeError"):
            load_adapter()
    finally:
        del sys.modules["_fake_needs_args"]


def test_load_adapter_single_entry_point(monkeypatch):
    """With one installed entry-point, load_adapter registers it."""
    ep = _FakeEntryPoint("test-ep", "oxi_core.tests.test_adapter:_MinimalAdapter", _MinimalAdapter)
    monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [ep])
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    load_adapter()
    assert isinstance(get_active_adapter(), _MinimalAdapter)


def test_load_adapter_multiple_entry_points_raises(monkeypatch):
    """With more than one entry-point and no env var, raise MultipleAdaptersError."""
    ep1 = _FakeEntryPoint("adapter-a", "...:A", _MinimalAdapter)
    ep2 = _FakeEntryPoint("adapter-b", "...:B", _MinimalAdapter)
    monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [ep1, ep2])
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    with pytest.raises(MultipleAdaptersError, match="adapter-a"):
        load_adapter()


def test_load_adapter_no_entry_points_no_env_is_noop(monkeypatch):
    """No entry-points + no env var → load_adapter returns without registering."""
    monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [])
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    load_adapter()  # must not raise
    with pytest.raises(AdapterNotRegisteredError):
        get_active_adapter()


def test_load_adapter_entry_point_load_failure_raises(monkeypatch):
    """If ep.load() raises, AdapterLoadError is surfaced."""

    class _BadEP:
        name = "bad-ep"
        value = "broken:Adapter"

        def load(self):
            raise ImportError("simulated load failure")

    monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [_BadEP()])
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    with pytest.raises(AdapterLoadError, match="simulated load failure"):
        load_adapter()


def test_load_adapter_entry_point_group_name_is_correct(monkeypatch):
    """load_adapter queries exactly the 'oxi.adapters' group."""
    seen_groups: list[str] = []

    def _spy(group):
        seen_groups.append(group)
        return []

    monkeypatch.setattr(adapter_mod, "entry_points", _spy)
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    load_adapter()
    assert seen_groups == ["oxi.adapters"]


def test_load_adapter_invalid_adapter_from_entry_point_raises(monkeypatch):
    """If the entry-point class doesn't satisfy the protocol, InvalidAdapterError."""
    ep = _FakeEntryPoint("broken", "...:_NotAnAdapter", _NotAnAdapter)
    monkeypatch.setattr(adapter_mod, "entry_points", lambda group: [ep])
    monkeypatch.delenv("OXI_ADAPTER", raising=False)

    with pytest.raises(InvalidAdapterError):
        load_adapter()
