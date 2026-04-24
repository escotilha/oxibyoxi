"""Tests for oxi_core.v3.status_json and the ``oxi status --json`` CLI flag."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from oxi_core import db
from oxi_core.adapter import (
    BudgetCaps,
    DispatchHost,
    DispatchPolicy,
    NamingConfig,
    PathsConfig,
    clear_adapter,
    register_adapter,
)
from oxi_core.cli import main
from oxi_core.v3.status_json import SCHEMA_VERSION, build


@dataclass
class _Adapter:
    db_path_value: str
    ks_path_value: str

    def naming(self): return NamingConfig(instance_name="oxi-test-json")
    def paths(self):
        return PathsConfig(
            db_path=self.db_path_value,
            release_lock_path=self.ks_path_value,
        )
    def budget(self): return BudgetCaps(daily_soft_warn=1.0, daily_hard_cap=5.0)
    def github_repo(self): return "owner/repo"
    def roadmap_location(self): return "roadmap.md"
    def branch_prefixes(self): return ("feat/",)
    def dispatch_hosts(self):
        return (
            DispatchHost(name="local", ssh_alias=None, max_concurrent=1, worktree_root="/tmp"),
        )
    def promote_recipe(self): return None
    def plan_tier(self): return "standard"
    def policy(self): return DispatchPolicy()


@pytest.fixture(autouse=True)
def _reset():
    clear_adapter()
    yield
    clear_adapter()


@pytest.fixture
def _env(tmp_path: Path):
    adapter = _Adapter(
        db_path_value=str(tmp_path / "oxi.db"),
        ks_path_value=str(tmp_path / "KS"),
    )
    register_adapter(adapter)
    handle = db.connect()
    yield {
        "conn": handle.connection,
        "tmp": tmp_path,
        "ks_path": tmp_path / "KS",
        "adapter": adapter,
    }
    handle.connection.close()


def _seed(conn, identifier: str, status: str, *, title: str = "t",
          pr_number: int | None = None, cost_usd: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO task (identifier, tier, title, status, pr_number, cost_usd) "
        "VALUES (?, 0, ?, ?, ?, ?)",
        (identifier, title, status, pr_number, cost_usd),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# status_json.build — top-level shape
# ---------------------------------------------------------------------------


def test_schema_version_is_int(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["schema_version"] == SCHEMA_VERSION
    assert isinstance(payload["schema_version"], int)


def test_generated_at_is_iso8601(_env):
    payload = build(_env["conn"], _env["adapter"])
    ts = payload["generated_at"]
    # "YYYY-MM-DDTHH:MM:SSZ" — 20 chars, ends with Z
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    assert len(ts) == 20


def test_instance_matches_adapter(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["instance"] == "oxi-test-json"


def test_plan_tier_matches_adapter(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["plan_tier"] == "standard"


def test_repo_matches_adapter(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["repo"] == "owner/repo"


def test_required_top_level_keys_present(_env):
    payload = build(_env["conn"], _env["adapter"])
    required = {
        "schema_version", "generated_at", "instance", "plan_tier",
        "repo", "task_counts", "tasks", "budget", "heartbeat", "killswitch",
    }
    assert required <= set(payload.keys())


# ---------------------------------------------------------------------------
# task_counts
# ---------------------------------------------------------------------------


def test_empty_db_yields_empty_counts(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["task_counts"] == {}


def test_single_task_counted(_env):
    _seed(_env["conn"], "T0-1", "dispatched")
    payload = build(_env["conn"], _env["adapter"])
    assert payload["task_counts"] == {"dispatched": 1}


def test_multiple_statuses_counted(_env):
    _seed(_env["conn"], "T0-1", "dispatched")
    _seed(_env["conn"], "T0-2", "merged")
    _seed(_env["conn"], "T0-3", "merged")
    payload = build(_env["conn"], _env["adapter"])
    assert payload["task_counts"]["dispatched"] == 1
    assert payload["task_counts"]["merged"] == 2


# ---------------------------------------------------------------------------
# tasks list
# ---------------------------------------------------------------------------


def test_empty_db_yields_empty_task_list(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["tasks"] == []


def test_task_fields_present(_env):
    _seed(_env["conn"], "T0-1", "dispatched", title="Do something", pr_number=42)
    payload = build(_env["conn"], _env["adapter"])
    assert len(payload["tasks"]) == 1
    task = payload["tasks"][0]
    required_fields = {
        "identifier", "tier", "title", "status", "pr_number",
        "cost_usd", "last_progress_at", "dispatched_at", "merged_at",
        "failed_at", "failure_reason", "created_at", "updated_at",
    }
    assert required_fields <= set(task.keys())


def test_task_values_correct(_env):
    _seed(_env["conn"], "T0-1", "dispatched", title="Do something", pr_number=42)
    payload = build(_env["conn"], _env["adapter"])
    task = payload["tasks"][0]
    assert task["identifier"] == "T0-1"
    assert task["title"] == "Do something"
    assert task["status"] == "dispatched"
    assert task["pr_number"] == 42


def test_tasks_limit_respected(_env):
    for i in range(5):
        _seed(_env["conn"], f"T0-{i}", "planned", title=f"t{i}")
    payload = build(_env["conn"], _env["adapter"], tasks_limit=3)
    assert len(payload["tasks"]) == 3


def test_tasks_limit_zero_yields_empty_list(_env):
    _seed(_env["conn"], "T0-1", "planned")
    payload = build(_env["conn"], _env["adapter"], tasks_limit=0)
    assert payload["tasks"] == []


# ---------------------------------------------------------------------------
# budget section
# ---------------------------------------------------------------------------


def test_budget_keys_present(_env):
    payload = build(_env["conn"], _env["adapter"])
    budget = payload["budget"]
    required = {
        "verdict", "today_spend_usd", "daily_soft_warn_usd",
        "daily_hard_cap_usd", "window_start",
    }
    assert required <= set(budget.keys())


def test_budget_verdict_ok_when_no_spend(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["budget"]["verdict"] == "ok"


def test_budget_spend_reflects_tasks(_env):
    _seed(_env["conn"], "T0-1", "merged", cost_usd=0.42)
    payload = build(_env["conn"], _env["adapter"])
    assert payload["budget"]["today_spend_usd"] == pytest.approx(0.42, abs=1e-6)


def test_budget_caps_match_adapter(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["budget"]["daily_soft_warn_usd"] == pytest.approx(1.0)
    assert payload["budget"]["daily_hard_cap_usd"] == pytest.approx(5.0)


def test_budget_window_start_is_iso8601(_env):
    payload = build(_env["conn"], _env["adapter"])
    ws = payload["budget"]["window_start"]
    assert isinstance(ws, str)
    assert ws.endswith("Z")


# ---------------------------------------------------------------------------
# heartbeat section
# ---------------------------------------------------------------------------


def test_heartbeat_keys_present(_env):
    payload = build(_env["conn"], _env["adapter"])
    hb = payload["heartbeat"]
    required = {
        "dispatched_total", "dispatched_without_pr",
        "dispatched_with_pr", "abandoned_last_24h",
    }
    assert required <= set(hb.keys())


def test_heartbeat_empty_db_all_zeros(_env):
    payload = build(_env["conn"], _env["adapter"])
    hb = payload["heartbeat"]
    assert hb["dispatched_total"] == 0
    assert hb["dispatched_without_pr"] == 0
    assert hb["dispatched_with_pr"] == 0
    assert hb["abandoned_last_24h"] == 0


def test_heartbeat_dispatched_without_pr(_env):
    _seed(_env["conn"], "T0-1", "dispatched", pr_number=None)
    payload = build(_env["conn"], _env["adapter"])
    hb = payload["heartbeat"]
    assert hb["dispatched_total"] == 1
    assert hb["dispatched_without_pr"] == 1
    assert hb["dispatched_with_pr"] == 0


def test_heartbeat_dispatched_with_pr(_env):
    _seed(_env["conn"], "T0-1", "dispatched", pr_number=7)
    payload = build(_env["conn"], _env["adapter"])
    hb = payload["heartbeat"]
    assert hb["dispatched_total"] == 1
    assert hb["dispatched_without_pr"] == 0
    assert hb["dispatched_with_pr"] == 1


def test_heartbeat_abandoned_last_24h(_env):
    _seed(_env["conn"], "T0-1", "abandoned")
    payload = build(_env["conn"], _env["adapter"])
    assert payload["heartbeat"]["abandoned_last_24h"] == 1


# ---------------------------------------------------------------------------
# killswitch section
# ---------------------------------------------------------------------------


def test_killswitch_keys_present(_env):
    payload = build(_env["conn"], _env["adapter"])
    ks = payload["killswitch"]
    assert {"active", "reason", "path"} <= set(ks.keys())


def test_killswitch_inactive_by_default(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["killswitch"]["active"] is False
    assert payload["killswitch"]["reason"] is None


def test_killswitch_active_when_file_exists(_env):
    _env["ks_path"].write_text("operator halt\n")
    payload = build(_env["conn"], _env["adapter"])
    assert payload["killswitch"]["active"] is True
    assert payload["killswitch"]["reason"] == "operator halt"


def test_killswitch_reason_none_when_file_empty(_env):
    _env["ks_path"].write_text("")
    payload = build(_env["conn"], _env["adapter"])
    assert payload["killswitch"]["active"] is True
    assert payload["killswitch"]["reason"] is None


def test_killswitch_path_in_payload(_env):
    payload = build(_env["conn"], _env["adapter"])
    assert payload["killswitch"]["path"] == str(_env["ks_path"])


# ---------------------------------------------------------------------------
# JSON serializability
# ---------------------------------------------------------------------------


def test_payload_is_json_serializable(_env):
    """The entire payload must round-trip through json.dumps without error."""
    _seed(_env["conn"], "T0-1", "dispatched", pr_number=99, cost_usd=0.10)
    payload = build(_env["conn"], _env["adapter"])
    dumped = json.dumps(payload)
    reloaded = json.loads(dumped)
    assert reloaded["schema_version"] == SCHEMA_VERSION
    assert reloaded["instance"] == "oxi-test-json"


# ---------------------------------------------------------------------------
# CLI integration: oxi status --json
# ---------------------------------------------------------------------------


def test_oxi_status_json_exits_zero(_env, capsys):
    rc = main(["status", "--json"])
    assert rc == 0


def test_oxi_status_json_output_is_valid_json(_env, capsys):
    main(["status", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["schema_version"] == SCHEMA_VERSION


def test_oxi_v3_status_json_is_alias(_env, capsys):
    rc = main(["v3", "status", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "schema_version" in data


def test_oxi_status_json_contains_instance(_env, capsys):
    main(["status", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["instance"] == "oxi-test-json"


def test_oxi_status_json_contains_task_counts(_env, capsys):
    _seed(_env["conn"], "T0-1", "dispatched")
    _seed(_env["conn"], "T0-2", "merged")
    main(["status", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["task_counts"]["dispatched"] == 1
    assert data["task_counts"]["merged"] == 1


def test_oxi_status_json_no_human_text_leaks(_env, capsys):
    """--json must emit only the JSON object, no banner or table text."""
    main(["status", "--json"])
    out = capsys.readouterr().out
    # Must be valid JSON — raises if not
    json.loads(out)
    # Must NOT contain human-facing prose strings
    assert "plan tier" not in out
    assert "task counts" not in out
    assert "recent events" not in out


def test_oxi_status_without_json_still_works(_env, capsys):
    """Default (no --json) must still produce human-readable text."""
    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "plan tier" in out
    assert "oxi-test-json" in out


def test_oxi_status_json_output_ends_with_newline(_env, capsys):
    """JSON output must end with a newline for shell-pipeline friendliness."""
    main(["status", "--json"])
    out = capsys.readouterr().out
    assert out.endswith("\n")
