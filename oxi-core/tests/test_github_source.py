"""Tests for oxi_core.v3.github_source — GitHub repo discovery with prefilters.

Test strategy:
- Pure in-process: no subprocess, no real GitHub, no real DB schema.
- ``FakeSourceClient`` supplies deterministic repo + commit data.
- Every observable effect checked: returned repos, FetchReport counters,
  ledger event emission for source failures.

Coverage matrix:
  happy path org feed — repo passes both filters → appears in results
  happy path topic query — same filter path via topic source
  star-velocity filter drops slow repos
  commit-activity filter drops stale repos
  both filters can drop same repo (velocity checked first)
  deduplication — same repo from two sources appears once
  per-source isolation — one org raising drops only that org
  per-source isolation — one topic raising drops only that topic
  sources_failed counter incremented correctly
  AUTO_IMPROVE_SOURCE_FAILED event written to ledger DB
  no conn — failure logged but no DB write attempted
  orgs capped at 8
  topics capped at 5
  empty config — fetch returns no repos
  repo with stars=0 dropped by velocity filter
  repo with missing pushed_at dropped by velocity filter
  FetchReport counters accurate across mixed results
  _star_velocity helper: correct calculation
  _since_iso helper: returns a well-formed ISO timestamp in the past
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from oxi_core.v3.github_source import (
    DEFAULT_MIN_STARS_PER_DAY,
    STAR_VELOCITY_WINDOW_DAYS,
    FakeSourceClient,
    FetchReport,
    GitHubSource,
    SourceConfig,
    _since_iso,
    _star_velocity,
)
from oxi_core.v3.ledger_events import LedgerEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_30_DAYS_AGO = (_NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_3_DAYS_AGO = (_NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
_13_DAYS_AGO = (_NOW - timedelta(days=13)).strftime("%Y-%m-%dT%H:%M:%SZ")
_15_DAYS_AGO = (_NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_repo(
    full_name: str,
    *,
    stars: int = 1000,
    pushed_at: str | None = None,
    description: str = "",
    topics: list[str] | None = None,
) -> dict:
    """Build a minimal repo dict for FakeSourceClient."""
    return {
        "full_name": full_name,
        "description": description,
        "stargazers_count": stars,
        "pushed_at": pushed_at if pushed_at is not None else _3_DAYS_AGO,
        "topics": topics or [],
    }


def _make_db() -> sqlite3.Connection:
    """Create an in-memory DB with the minimal event table schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE event (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id  INTEGER,
            kind     TEXT NOT NULL,
            payload  TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def _events(conn: sqlite3.Connection, kind: str) -> list[dict]:
    """Return all events of a given kind as parsed payload dicts."""
    rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ?", (kind,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _make_source(
    client: FakeSourceClient,
    *,
    config: SourceConfig | None = None,
    conn: sqlite3.Connection | None = None,
) -> GitHubSource:
    return GitHubSource(client, config=config, conn=conn)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


class TestStarVelocity:
    def test_high_star_repo_passes(self):
        # 10 000 stars, repo pushed 30 days ago → 10 000/30 ≈ 333 stars/day
        vel = _star_velocity(10_000, _30_DAYS_AGO, STAR_VELOCITY_WINDOW_DAYS)
        assert vel > DEFAULT_MIN_STARS_PER_DAY

    def test_low_star_repo_fails(self):
        # 10 stars over 30 days → 0.33 stars/day < 5
        vel = _star_velocity(10, _30_DAYS_AGO, STAR_VELOCITY_WINDOW_DAYS)
        assert vel < DEFAULT_MIN_STARS_PER_DAY

    def test_zero_stars(self):
        assert _star_velocity(0, _30_DAYS_AGO, STAR_VELOCITY_WINDOW_DAYS) == 0.0

    def test_empty_pushed_at(self):
        assert _star_velocity(9999, "", STAR_VELOCITY_WINDOW_DAYS) == 0.0

    def test_invalid_pushed_at(self):
        assert _star_velocity(9999, "not-a-date", STAR_VELOCITY_WINDOW_DAYS) == 0.0

    def test_very_new_repo(self):
        # 500 stars, pushed 1 day ago → velocity = 500/1 = 500 > threshold
        vel = _star_velocity(500, _3_DAYS_AGO, STAR_VELOCITY_WINDOW_DAYS)
        assert vel > DEFAULT_MIN_STARS_PER_DAY

    def test_window_clamped(self):
        # Repo that is 100 days old — effective_days clamped to window=30
        _100_days_ago = (_NOW - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        vel = _star_velocity(300, _100_days_ago, 30)
        # 300/30 = 10 > 5
        assert vel == pytest.approx(10.0, rel=0.05)


class TestSinceIso:
    def test_returns_past_timestamp(self):
        iso = _since_iso(14)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        assert dt < datetime.now(tz=UTC)

    def test_roughly_correct_offset(self):
        iso = _since_iso(14)
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(tz=UTC) - dt
        assert 13 < delta.total_seconds() / 86400 < 15

    def test_format_ends_with_z(self):
        assert _since_iso(7).endswith("Z")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_org_feed_single_repo(self):
        """Repo that passes both prefilters is returned."""
        client = FakeSourceClient()
        repo = _make_repo("acme/widget", stars=9000)
        client.add_org_repos("acme", [repo])
        client.commits_by_repo["acme/widget"] = [{"sha": "abc"}]

        source = _make_source(client, config=SourceConfig(orgs=("acme",)))
        repos, report = source.fetch()

        assert len(repos) == 1
        assert repos[0].full_name == "acme/widget"
        assert repos[0].source_kind == "org_release"
        assert repos[0].source_name == "acme"
        assert report.repos_found == 1
        assert report.repos_dropped_velocity == 0
        assert report.repos_dropped_stale == 0
        assert report.sources_failed == 0

    def test_topic_query_single_repo(self):
        """Repo discovered via topic search is returned."""
        client = FakeSourceClient()
        repo = _make_repo("corp/llm-lib", stars=5000, topics=["llm", "ai"])
        client.add_topic_repos("llm", [repo])
        client.commits_by_repo["corp/llm-lib"] = [{"sha": "xyz"}]

        source = _make_source(client, config=SourceConfig(topics=("llm",)))
        repos, report = source.fetch()

        assert len(repos) == 1
        assert repos[0].full_name == "corp/llm-lib"
        assert repos[0].source_kind == "topic_query"
        assert repos[0].source_name == "llm"
        assert repos[0].topics == ("llm", "ai")
        assert report.repos_found == 1

    def test_both_sources_in_one_fetch(self):
        """Org feed + topic query both contribute repos."""
        client = FakeSourceClient()
        client.add_org_repos("anthropic", [_make_repo("anthropic/sdk", stars=8000)])
        client.add_topic_repos("mcp", [_make_repo("modelcontextprotocol/spec", stars=6000)])
        client.commits_by_repo["anthropic/sdk"] = [{"sha": "1"}]
        client.commits_by_repo["modelcontextprotocol/spec"] = [{"sha": "2"}]

        config = SourceConfig(orgs=("anthropic",), topics=("mcp",))
        repos, report = _make_source(client, config=config).fetch()

        full_names = {r.full_name for r in repos}
        assert "anthropic/sdk" in full_names
        assert "modelcontextprotocol/spec" in full_names
        assert report.repos_found == 2


# ---------------------------------------------------------------------------
# Prefilter tests
# ---------------------------------------------------------------------------


class TestStarVelocityFilter:
    def test_slow_repo_dropped(self):
        """Repo with < 5 stars/day is dropped."""
        client = FakeSourceClient()
        # 10 stars, pushed 30 days ago → ~0.33 stars/day
        repo = _make_repo("slow/pkg", stars=10, pushed_at=_30_DAYS_AGO)
        client.add_org_repos("slow-org", [repo])

        source = _make_source(client, config=SourceConfig(orgs=("slow-org",)))
        repos, report = source.fetch()

        assert len(repos) == 0
        assert report.repos_dropped_velocity == 1
        assert report.repos_dropped_stale == 0

    def test_zero_stars_dropped(self):
        client = FakeSourceClient()
        client.add_org_repos("x", [_make_repo("x/y", stars=0)])

        _, report = _make_source(client, config=SourceConfig(orgs=("x",))).fetch()
        assert report.repos_dropped_velocity == 1

    def test_missing_pushed_at_dropped(self):
        client = FakeSourceClient()
        client.add_org_repos("x", [_make_repo("x/y", stars=9999, pushed_at="")])

        _, report = _make_source(client, config=SourceConfig(orgs=("x",))).fetch()
        assert report.repos_dropped_velocity == 1

    def test_custom_threshold_respected(self):
        """Higher min_stars_per_day drops repos that default threshold passes."""
        client = FakeSourceClient()
        # ~6.67 stars/day — passes default (5) but not custom (10)
        repo = _make_repo("x/y", stars=200, pushed_at=_30_DAYS_AGO)
        client.add_org_repos("x", [repo])
        client.commits_by_repo["x/y"] = [{"sha": "1"}]

        # With default threshold (5): passes
        repos_default, _ = _make_source(
            client, config=SourceConfig(orgs=("x",), min_stars_per_day=5.0)
        ).fetch()
        assert len(repos_default) == 1

        # With higher threshold (10): dropped
        repos_high, report = _make_source(
            client, config=SourceConfig(orgs=("x",), min_stars_per_day=10.0)
        ).fetch()
        assert len(repos_high) == 0
        assert report.repos_dropped_velocity == 1


class TestCommitActivityFilter:
    def test_stale_repo_dropped(self):
        """Repo with no recent commits is dropped."""
        client = FakeSourceClient()
        repo = _make_repo("stale/pkg", stars=9000)
        client.add_org_repos("stale-org", [repo])
        # No entry in commits_by_repo → get_recent_commits returns []

        source = _make_source(client, config=SourceConfig(orgs=("stale-org",)))
        repos, report = source.fetch()

        assert len(repos) == 0
        assert report.repos_dropped_stale == 1
        assert report.repos_dropped_velocity == 0

    def test_recent_commit_passes(self):
        """Repo with a commit in the window passes."""
        client = FakeSourceClient()
        repo = _make_repo("active/pkg", stars=9000)
        client.add_org_repos("active-org", [repo])
        client.commits_by_repo["active/pkg"] = [{"sha": "deadbeef"}]

        repos, report = _make_source(
            client, config=SourceConfig(orgs=("active-org",))
        ).fetch()

        assert len(repos) == 1
        assert report.repos_dropped_stale == 0

    def test_custom_commit_age_days(self):
        """Stricter max_commit_age_days can drop more repos."""
        client = FakeSourceClient()
        repo = _make_repo("borderline/pkg", stars=9000)
        client.add_org_repos("x", [repo])
        # One commit exists — but FakeSourceClient ignores since_iso
        # and just checks whether the repo key exists in commits_by_repo.
        client.commits_by_repo["borderline/pkg"] = [{"sha": "abc"}]

        repos, report = _make_source(
            client, config=SourceConfig(orgs=("x",), max_commit_age_days=14)
        ).fetch()
        assert len(repos) == 1
        assert report.repos_dropped_stale == 0


class TestBothFilters:
    def test_velocity_checked_before_commit(self):
        """If velocity filter drops a repo, commit filter is not invoked."""
        client = FakeSourceClient()
        # Slow repo — velocity filter should drop it
        repo = _make_repo("slow/stale", stars=5, pushed_at=_30_DAYS_AGO)
        client.add_org_repos("org", [repo])
        # No commits loaded — would also drop it, but velocity wins

        _, report = _make_source(client, config=SourceConfig(orgs=("org",))).fetch()
        assert report.repos_dropped_velocity == 1
        assert report.repos_dropped_stale == 0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_same_repo_from_two_sources(self):
        """A repo discovered by both an org feed and a topic query appears once."""
        client = FakeSourceClient()
        repo = _make_repo("shared/lib", stars=8000)
        client.add_org_repos("shared-org", [repo])
        client.add_topic_repos("ai", [repo])
        client.commits_by_repo["shared/lib"] = [{"sha": "abc"}]

        config = SourceConfig(orgs=("shared-org",), topics=("ai",))
        repos, report = _make_source(client, config=config).fetch()

        assert len(repos) == 1
        assert report.repos_found == 1
        # Source kind is from the first encounter (org_release)
        assert repos[0].source_kind == "org_release"

    def test_same_repo_from_two_topics(self):
        """A repo matching two topic queries appears once."""
        client = FakeSourceClient()
        repo = _make_repo("ml/framework", stars=9000)
        client.add_topic_repos("ml", [repo])
        client.add_topic_repos("ai", [repo])
        client.commits_by_repo["ml/framework"] = [{"sha": "abc"}]

        config = SourceConfig(topics=("ml", "ai"))
        repos, _ = _make_source(client, config=config).fetch()

        assert len(repos) == 1


# ---------------------------------------------------------------------------
# Per-source failure isolation
# ---------------------------------------------------------------------------


class _RaisingSource:
    """A SourceClient that always raises for org/topic queries."""

    def list_org_repos(self, org: str, limit: int = 30) -> list[dict]:
        raise RuntimeError(f"boom from {org}")

    def search_repos_by_topic(self, topic: str, limit: int = 30) -> list[dict]:
        raise RuntimeError(f"boom from topic {topic}")

    def get_recent_commits(self, full_name: str, since_iso: str) -> list[dict]:
        return []


class TestPerSourceIsolation:
    def test_failing_org_does_not_stop_other_orgs(self):
        """An org that raises only increments sources_failed; other orgs continue."""
        client = FakeSourceClient()
        good_repo = _make_repo("good-org/pkg", stars=9000)
        client.add_org_repos("good-org", [good_repo])
        client.commits_by_repo["good-org/pkg"] = [{"sha": "abc"}]

        # Mix a good FakeSourceClient with one raising org by pre-registering
        # no repos for "bad-org" and then monkey-patching list_org_repos.
        original_list = client.list_org_repos

        def patched_list(org: str, limit: int = 30) -> list[dict]:
            if org == "bad-org":
                raise RuntimeError("network failure")
            return original_list(org, limit)

        client.list_org_repos = patched_list  # type: ignore[method-assign]

        config = SourceConfig(orgs=("bad-org", "good-org"))
        repos, report = _make_source(client, config=config).fetch()

        assert report.sources_failed == 1
        assert report.repos_found == 1
        assert repos[0].full_name == "good-org/pkg"

    def test_failing_topic_does_not_stop_other_topics(self):
        """A topic that raises only increments sources_failed; other topics continue."""
        client = FakeSourceClient()
        good_repo = _make_repo("corp/lib", stars=9000)
        client.add_topic_repos("good-topic", [good_repo])
        client.commits_by_repo["corp/lib"] = [{"sha": "abc"}]

        original_search = client.search_repos_by_topic

        def patched_search(topic: str, limit: int = 30) -> list[dict]:
            if topic == "bad-topic":
                raise ValueError("topic API error")
            return original_search(topic, limit)

        client.search_repos_by_topic = patched_search  # type: ignore[method-assign]

        config = SourceConfig(topics=("bad-topic", "good-topic"))
        repos, report = _make_source(client, config=config).fetch()

        assert report.sources_failed == 1
        assert report.repos_found == 1

    def test_all_sources_fail(self):
        """If every source fails, repos=() and sources_failed=total sources."""
        config = SourceConfig(orgs=("a", "b"), topics=("x",))
        source = _make_source(_RaisingSource(), config=config)
        repos, report = source.fetch()

        assert repos == ()
        assert report.repos_found == 0
        assert report.sources_failed == 3  # 2 orgs + 1 topic


# ---------------------------------------------------------------------------
# Ledger event emission
# ---------------------------------------------------------------------------


class TestLedgerEvents:
    def test_source_failed_event_written(self):
        """AUTO_IMPROVE_SOURCE_FAILED is written to DB when a source fails."""
        conn = _make_db()
        config = SourceConfig(orgs=("failing-org",))
        client = FakeSourceClient()

        def raiser(org: str, limit: int = 30) -> list[dict]:
            raise RuntimeError("API timeout")

        client.list_org_repos = raiser  # type: ignore[method-assign]

        source = _make_source(client, config=config, conn=conn)
        _, report = source.fetch()

        assert report.sources_failed == 1
        events = _events(conn, LedgerEvent.AUTO_IMPROVE_SOURCE_FAILED)
        assert len(events) == 1
        assert events[0]["source_kind"] == "org_release"
        assert events[0]["source_name"] == "failing-org"
        assert "RuntimeError" in events[0]["error"]

    def test_multiple_failures_each_write_event(self):
        """Two source failures → two ledger events."""
        conn = _make_db()
        config = SourceConfig(orgs=("org1", "org2"))
        source = _make_source(_RaisingSource(), config=config, conn=conn)
        _, report = source.fetch()

        assert report.sources_failed == 2
        events = _events(conn, LedgerEvent.AUTO_IMPROVE_SOURCE_FAILED)
        assert len(events) == 2
        names = {e["source_name"] for e in events}
        assert names == {"org1", "org2"}

    def test_no_conn_does_not_crash(self):
        """Without a DB conn, failure is logged but no exception raised."""
        config = SourceConfig(orgs=("org",))
        client = FakeSourceClient()

        def raiser(org: str, limit: int = 30) -> list[dict]:
            raise RuntimeError("boom")

        client.list_org_repos = raiser  # type: ignore[method-assign]

        source = _make_source(client, config=config, conn=None)
        repos, report = source.fetch()  # must not raise

        assert report.sources_failed == 1
        assert repos == ()

    def test_topic_failure_event_written(self):
        """AUTO_IMPROVE_SOURCE_FAILED emitted for failing topic query."""
        conn = _make_db()
        config = SourceConfig(topics=("bad-topic",))
        client = FakeSourceClient()

        def raiser(topic: str, limit: int = 30) -> list[dict]:
            raise ConnectionError("timeout")

        client.search_repos_by_topic = raiser  # type: ignore[method-assign]

        source = _make_source(client, config=config, conn=conn)
        _, report = source.fetch()

        assert report.sources_failed == 1
        events = _events(conn, LedgerEvent.AUTO_IMPROVE_SOURCE_FAILED)
        assert len(events) == 1
        assert events[0]["source_kind"] == "topic_query"


# ---------------------------------------------------------------------------
# Source cap enforcement
# ---------------------------------------------------------------------------


class TestSourceCaps:
    def test_orgs_capped_at_8(self):
        """Only the first 8 orgs are queried even if more are configured."""
        client = FakeSourceClient()
        # Provide 10 orgs, each with one repo
        orgs = [f"org{i}" for i in range(10)]
        for org in orgs:
            repo = _make_repo(f"{org}/pkg", stars=9000)
            client.add_org_repos(org, [repo])
            client.commits_by_repo[f"{org}/pkg"] = [{"sha": "abc"}]

        config = SourceConfig(orgs=tuple(orgs))
        repos, _ = _make_source(client, config=config).fetch()

        queried_orgs = {r.source_name for r in repos}
        # Only first 8 orgs should appear
        assert queried_orgs == set(orgs[:8])
        assert len(repos) == 8

    def test_topics_capped_at_5(self):
        """Only the first 5 topics are queried even if more are configured."""
        client = FakeSourceClient()
        topics = [f"topic{i}" for i in range(7)]
        for topic in topics:
            repo = _make_repo(f"corp/{topic}-lib", stars=9000)
            client.add_topic_repos(topic, [repo])
            client.commits_by_repo[f"corp/{topic}-lib"] = [{"sha": "abc"}]

        config = SourceConfig(topics=tuple(topics))
        repos, _ = _make_source(client, config=config).fetch()

        queried_topics = {r.source_name for r in repos}
        assert queried_topics == set(topics[:5])
        assert len(repos) == 5


# ---------------------------------------------------------------------------
# Empty config
# ---------------------------------------------------------------------------


class TestEmptyConfig:
    def test_no_orgs_no_topics(self):
        """Empty config → no sources queried → empty results."""
        client = FakeSourceClient()
        repos, report = _make_source(client, config=SourceConfig()).fetch()

        assert repos == ()
        assert report == FetchReport(
            repos_found=0,
            repos_dropped_velocity=0,
            repos_dropped_stale=0,
            sources_failed=0,
        )


# ---------------------------------------------------------------------------
# RepoInfo field accuracy
# ---------------------------------------------------------------------------


class TestRepoInfoFields:
    def test_fields_populated_correctly(self):
        """RepoInfo is populated from the raw dict with correct field mapping."""
        client = FakeSourceClient()
        repo = _make_repo(
            "owner/name",
            stars=42_000,
            description="A great library",
            topics=["python", "ml"],
        )
        client.add_org_repos("owner", [repo])
        client.commits_by_repo["owner/name"] = [{"sha": "deadbeef"}]

        repos, _ = _make_source(client, config=SourceConfig(orgs=("owner",))).fetch()

        assert len(repos) == 1
        r = repos[0]
        assert r.full_name == "owner/name"
        assert r.description == "A great library"
        assert r.stars == 42_000
        assert r.topics == ("python", "ml")
        assert r.source_kind == "org_release"
        assert r.source_name == "owner"

    def test_topic_query_source_kind(self):
        client = FakeSourceClient()
        repo = _make_repo("org/project", stars=9000)
        client.add_topic_repos("ai", [repo])
        client.commits_by_repo["org/project"] = [{"sha": "abc"}]

        repos, _ = _make_source(client, config=SourceConfig(topics=("ai",))).fetch()
        assert repos[0].source_kind == "topic_query"
        assert repos[0].source_name == "ai"


# ---------------------------------------------------------------------------
# FetchReport accuracy
# ---------------------------------------------------------------------------


class TestFetchReport:
    def test_mixed_results(self):
        """Verify counters when some repos pass, some fail velocity, some fail commit."""
        client = FakeSourceClient()

        # passes both filters
        good = _make_repo("org/good", stars=9000)
        # fails velocity
        slow = _make_repo("org/slow", stars=10, pushed_at=_30_DAYS_AGO)
        # passes velocity, fails commit activity
        stale = _make_repo("org/stale", stars=9000)

        client.add_org_repos("org", [good, slow, stale])
        client.commits_by_repo["org/good"] = [{"sha": "abc"}]
        # org/slow: no need (dropped before commit check)
        # org/stale: no commit entry → stale

        repos, report = _make_source(client, config=SourceConfig(orgs=("org",))).fetch()

        assert report.repos_found == 1
        assert report.repos_dropped_velocity == 1
        assert report.repos_dropped_stale == 1
        assert report.sources_failed == 0
        assert repos[0].full_name == "org/good"
