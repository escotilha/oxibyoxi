"""Tests for scripts/roadmap-prune.py.

Covers:
  - Parses standard roadmap item headers (** T-ID · title **)
  - Substantive-commit gating: pure docs/release-notes commits don't ship items
  - Freshness window: recent commits don't trip the prune
  - Format preservation: kept items round-trip verbatim
  - --apply writes the file; default dry-run leaves it alone
  - --json emits a machine-readable summary
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "roadmap-prune.py"
).resolve()


def _load_module():
    """Load roadmap-prune.py as a module so we can call its functions.

    Hyphenated script name + @dataclass — register in sys.modules
    before exec so dataclasses can resolve forward references.
    """
    spec = importlib.util.spec_from_file_location("roadmap_prune", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["roadmap_prune"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rp():
    return _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Empty initial commit so HEAD exists.
    (repo / ".gitkeep").write_text("")
    _git(repo, "add", ".gitkeep")
    _git(repo, "commit", "-m", "init")
    return repo


def _seed_roadmap(repo: Path, items: list[tuple[str, str]]) -> Path:
    """Write a minimal roadmap with the given (id, title) items under Tier 1."""
    body = ["# roadmap\n", "\n", "Preamble.\n", "\n", "## Tier 1\n", "\n"]
    for ident, title in items:
        body.append(f"**{ident} · {title}**\n")
        body.append(f"_subtitle for {ident}._\n")
        body.append("\n")
    body.append("## Done\n")
    body.append("\n")
    body.append("Trailing notes.\n")
    path = repo / "docs" / "roadmap.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(body))
    return path


def _commit(repo: Path, files: dict[str, str], subject: str, *, days_ago: int = 5) -> None:
    """Create a commit touching the given files with the given subject.

    days_ago lets us simulate "old" commits that escape the 48h freshness window.
    """
    import os
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", *files.keys())
    # Pin both author and committer date via env vars. Free-text
    # ("3 days ago") is rejected when set as an env var; use ISO 8601.
    from datetime import UTC, datetime, timedelta
    target = datetime.now(UTC) - timedelta(days=days_ago)
    date_str = target.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": date_str,
        "GIT_COMMITTER_DATE": date_str,
    }
    subprocess.check_output(
        ["git", "-C", str(repo), "commit", "-m", subject],
        env=env,
        text=True,
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_items_picks_up_canonical_header(rp):
    body = [
        "**T0-1 · install runbook**\n",
        "_one-line subtitle._\n",
        "\n",
        "**T1-3 · status JSON flag**\n",
        "_emit stable JSON._\n",
    ]
    items = rp._parse_items(body)
    assert [i.identifier for i in items] == ["T0-1", "T1-3"]
    assert items[0].title == "install runbook"
    assert items[1].title == "status JSON flag"


def test_parse_items_preserves_full_block(rp):
    body = [
        "**T0-1 · install runbook**\n",
        "_subtitle._\n",
        "\n",
        "**T1-3 · next**\n",
        "_more._\n",
    ]
    items = rp._parse_items(body)
    # First item must own its header + subtitle + blank line.
    assert items[0].lines == [
        "**T0-1 · install runbook**\n",
        "_subtitle._\n",
        "\n",
    ]


# ---------------------------------------------------------------------------
# Substantive-commit gating
# ---------------------------------------------------------------------------


def test_pure_release_notes_commit_does_not_ship_item(rp, tmp_path):
    """A commit that only touches docs/release-notes must not prune an item."""
    repo = _init_repo(tmp_path)
    _seed_roadmap(repo, [("T0-1", "install runbook")])
    _commit(
        repo,
        {"docs/release-notes/v1.md": "T0-1 mentioned but not shipped\n"},
        "docs(release-notes): mention T0-1 (no code)",
        days_ago=10,
    )
    text, report = rp.prune(
        repo / "docs" / "roadmap.md",
        repo_root=repo,
        freshness_hours=0,
    )
    assert [i.identifier for i in report.kept] == ["T0-1"]
    assert report.pruned == []


def test_substantive_code_commit_prunes_item(rp, tmp_path):
    """A commit that touches oxi-core/src AND names the T-ID prunes."""
    repo = _init_repo(tmp_path)
    _seed_roadmap(repo, [("T0-1", "install runbook"), ("T0-2", "rollback")])
    _commit(
        repo,
        {"oxi-core/src/feature.py": "x = 1\n"},
        "feat(install): T0-1 install runbook implemented",
        days_ago=10,
    )
    text, report = rp.prune(
        repo / "docs" / "roadmap.md",
        repo_root=repo,
        freshness_hours=0,
    )
    pruned_ids = [i.identifier for i in report.pruned]
    kept_ids = [i.identifier for i in report.kept]
    assert pruned_ids == ["T0-1"]
    assert kept_ids == ["T0-2"]


def test_substantive_paths_include_scripts_and_workflows(rp, tmp_path):
    repo = _init_repo(tmp_path)
    _seed_roadmap(repo, [("T1-16", "bandit + codeql")])
    _commit(
        repo,
        {".github/workflows/ci.yml": "jobs: {}\n"},
        "ci: T1-16 add bandit",
        days_ago=10,
    )
    _, report = rp.prune(
        repo / "docs" / "roadmap.md",
        repo_root=repo,
        freshness_hours=0,
    )
    assert [i.identifier for i in report.pruned] == ["T1-16"]


# ---------------------------------------------------------------------------
# Freshness window
# ---------------------------------------------------------------------------


def test_recent_commit_does_not_trip_prune(rp, tmp_path):
    """A commit younger than the freshness window stays out of the shipped set."""
    repo = _init_repo(tmp_path)
    _seed_roadmap(repo, [("T0-1", "install runbook")])
    # Commit 1 hour ago — younger than the default 48h window.
    _commit(
        repo,
        {"oxi-core/src/feature.py": "x = 1\n"},
        "feat: T0-1 done",
        days_ago=0,
    )
    _, report = rp.prune(
        repo / "docs" / "roadmap.md",
        repo_root=repo,
        freshness_hours=48,
    )
    assert [i.identifier for i in report.kept] == ["T0-1"]


# ---------------------------------------------------------------------------
# Format preservation
# ---------------------------------------------------------------------------


def test_kept_items_round_trip_verbatim(rp, tmp_path):
    repo = _init_repo(tmp_path)
    path = _seed_roadmap(
        repo, [("T0-1", "shipped"), ("T2-99", "still pending")]
    )
    original = path.read_text()
    _commit(
        repo,
        {"oxi-core/src/feature.py": "x = 1\n"},
        "feat: T0-1 done",
        days_ago=10,
    )
    new_text, _ = rp.prune(
        path, repo_root=repo, freshness_hours=0
    )
    # T2-99 line still there, T0-1 line gone.
    assert "**T2-99 · still pending**" in new_text
    assert "**T0-1 · shipped**" not in new_text
    # Preamble and trailer untouched.
    assert "Preamble." in new_text
    assert "Trailing notes." in new_text
    # Original file is unchanged (dry-run).
    assert path.read_text() == original


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_dry_run_does_not_modify_file(tmp_path):
    repo = _init_repo(tmp_path)
    path = _seed_roadmap(repo, [("T0-1", "shipped")])
    original = path.read_text()
    _commit(
        repo,
        {"oxi-core/src/x.py": "x=1\n"},
        "feat: T0-1 done",
        days_ago=10,
    )
    out = subprocess.check_output(
        [
            sys.executable, str(SCRIPT_PATH),
            "--roadmap", str(path),
            "--freshness-hours", "0",
        ],
        cwd=repo,
        text=True,
    )
    assert "PRUNE  T0-1" in out
    assert path.read_text() == original


def test_cli_apply_writes_file(tmp_path):
    repo = _init_repo(tmp_path)
    path = _seed_roadmap(repo, [("T0-1", "shipped")])
    _commit(
        repo,
        {"oxi-core/src/x.py": "x=1\n"},
        "feat: T0-1 done",
        days_ago=10,
    )
    subprocess.check_output(
        [
            sys.executable, str(SCRIPT_PATH),
            "--roadmap", str(path),
            "--freshness-hours", "0",
            "--apply",
        ],
        cwd=repo,
        text=True,
    )
    assert "T0-1" not in path.read_text()


def test_cli_json_emits_parseable_summary(tmp_path):
    repo = _init_repo(tmp_path)
    path = _seed_roadmap(
        repo, [("T0-1", "shipped"), ("T2-99", "pending")]
    )
    _commit(
        repo,
        {"oxi-core/src/x.py": "x=1\n"},
        "feat: T0-1 done",
        days_ago=10,
    )
    out = subprocess.check_output(
        [
            sys.executable, str(SCRIPT_PATH),
            "--roadmap", str(path),
            "--freshness-hours", "0",
            "--json",
        ],
        cwd=repo,
        text=True,
    )
    data = json.loads(out)
    assert [x["id"] for x in data["pruned"]] == ["T0-1"]
    assert [x["id"] for x in data["kept"]] == ["T2-99"]
