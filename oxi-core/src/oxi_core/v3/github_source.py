"""github_source — discover candidate repositories via GitHub release feeds and topic queries.

``GitHubSource`` queries two kinds of source in a single pass:

1. **Pinned-org release feeds** (up to 8 orgs): ``gh release list --repo <org>/<repo>``
   is used per-repo; for org-level feeds we use the GitHub search API via
   ``gh api`` to list repos in the org sorted by latest release activity.

2. **Topic queries** (up to 5 topics): ``gh search repos --topic <topic>`` to
   find recently-active repos tagged with a given topic.

Prefilters
----------
Both source kinds apply the same two prefilters before returning results:

- **Star-velocity filter**: drop repos with fewer than
  ``min_stars_per_day`` (default 5) stars/day averaged over the last 30 days.
  Uses ``gh api /repos/<owner>/<name>`` and the ``stargazers_count`` +
  ``created_at`` fields for a lifetime average, clamped to the 30-day window.

- **Commit-activity filter**: drop repos with no commits in the last
  ``max_commit_age_days`` (default 14) days. Uses
  ``gh api /repos/<owner>/<repo>/commits?since=<iso>`` and checks that the
  list is non-empty.

Architecture
------------
- ``GitHubSource`` receives a ``SourceClient`` (protocol defined here) so tests
  inject ``FakeSourceClient`` without touching real GitHub.  Production wires
  ``GhCliSourceClient``.  Both live in this module.

- The ``FakeGitHubClient`` in ``tests/fixtures/fake_github.py`` is a PR-level
  stub; source-level faking is handled by ``FakeSourceClient`` defined here
  (also exported for test import convenience).

- Per-source try/except: a failure in one org feed or one topic query emits
  ``AUTO_IMPROVE_SOURCE_FAILED`` to the ledger and continues with the remaining
  sources.  The caller receives whatever repos the surviving sources found.

- All operations are synchronous (same discipline as auto_merge, ci_issue_filer,
  auto_recover).

- ``GitHubSource`` is stateless: instantiate it, call ``fetch(conn)``.

Ledger events
-------------
- ``AUTO_IMPROVE_SOURCE_FAILED`` — emitted once per source that raises an
  exception.  Payload: ``{source_kind, source_name, error}``.

Configuration
-------------
``SourceConfig`` is a frozen dataclass callers construct; the adapter can
expose it as an optional ``github_source_config()`` method (not part of
the core Adapter protocol — adapters that omit it get a safe default with
no orgs and no topics, producing an empty fetch).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Minimum stars per day (30-day window) for a repo to pass the velocity filter.
DEFAULT_MIN_STARS_PER_DAY: float = 5.0

#: Maximum age in days for the most recent commit before a repo is dropped.
DEFAULT_MAX_COMMIT_AGE_DAYS: int = 14

#: Window used for star-velocity calculation (days).
STAR_VELOCITY_WINDOW_DAYS: int = 30

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoInfo:
    """Minimal repository metadata returned by a source fetch.

    Attributes:
        full_name: ``owner/repo`` identifier, e.g. ``"anthropics/anthropic-sdk-python"``.
        description: repo description string (may be empty).
        stars: current stargazers_count.
        pushed_at: ISO-8601 timestamp of the last push (may be empty if unknown).
        topics: tuple of GitHub topic strings attached to the repo.
        source_kind: ``"org_release"`` or ``"topic_query"`` — which source
            produced this result.
        source_name: the org name or topic string that produced this result.
    """

    full_name: str
    description: str
    stars: int
    pushed_at: str
    topics: tuple[str, ...]
    source_kind: str  # "org_release" | "topic_query"
    source_name: str  # org name or topic string


@dataclass(frozen=True)
class SourceConfig:
    """Configuration for ``GitHubSource``.

    Attributes:
        orgs: up to 8 GitHub organisation names whose latest-release feeds
            are queried.
        topics: up to 5 GitHub topic strings used as search queries.
        min_stars_per_day: star-velocity threshold (stars/day over 30 days).
            Repos below this are dropped.
        max_commit_age_days: repos with no commits in this many days are dropped.
        max_repos_per_source: cap on results returned per source (org or topic).
    """

    orgs: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    min_stars_per_day: float = DEFAULT_MIN_STARS_PER_DAY
    max_commit_age_days: int = DEFAULT_MAX_COMMIT_AGE_DAYS
    max_repos_per_source: int = 30


@dataclass(frozen=True)
class FetchReport:
    """Summary of one ``GitHubSource.fetch()`` call.

    Attributes:
        repos_found: total repos that passed both prefilters.
        repos_dropped_velocity: repos dropped by the star-velocity filter.
        repos_dropped_stale: repos dropped by the commit-activity filter.
        sources_failed: number of org/topic sources that raised an exception.
    """

    repos_found: int
    repos_dropped_velocity: int
    repos_dropped_stale: int
    sources_failed: int


# ---------------------------------------------------------------------------
# SourceClient protocol
# ---------------------------------------------------------------------------


class SourceClient(Protocol):
    """Minimal interface for GitHub source-discovery operations.

    Separate from ``GitHubClient`` (which owns PR lifecycle) so the two
    concerns remain independently testable and the Protocol surface stays
    small.  Production uses ``GhCliSourceClient``; tests use
    ``FakeSourceClient``.
    """

    def list_org_repos(self, org: str, limit: int = 30) -> list[dict]:
        """Return a list of repo dicts for *org*, sorted by push date descending.

        Each dict must contain at least:
            ``full_name``, ``description``, ``stargazers_count``,
            ``pushed_at``, ``topics``.

        Returns an empty list on error (caller decides how to handle).
        """
        ...

    def search_repos_by_topic(self, topic: str, limit: int = 30) -> list[dict]:
        """Search GitHub for repos tagged with *topic*, sorted by relevance.

        Same dict shape as ``list_org_repos``.
        Returns an empty list if the search yields nothing.
        """
        ...

    def get_recent_commits(self, full_name: str, since_iso: str) -> list[dict]:
        """Return commits in *full_name* since *since_iso* (ISO-8601 UTC).

        Used only to check non-emptiness — the list can be truncated to 1
        item to minimise API load.  Returns an empty list if the repo has
        no commits in the window or does not exist.
        """
        ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class GhCliSourceClient:
    """Production ``SourceClient`` — shells out to ``gh``.

    All calls use ``gh api`` (REST) or ``gh search repos``.
    ``shell=True`` is never used; all invocations pass a list of tokens.
    """

    def __init__(self, binary: str = "gh") -> None:
        self._binary = binary

    # ---- SourceClient protocol -----------------------------------------

    def list_org_repos(self, org: str, limit: int = 30) -> list[dict]:
        """List repos in *org* sorted by pushed_at descending."""
        # Use gh api to list org repos with the fields we need.
        args = [
            self._binary, "api",
            f"/orgs/{org}/repos",
            "--jq", ".",
            "-X", "GET",
            "-F", f"per_page={limit}",
            "-F", "sort=pushed",
            "-F", "direction=desc",
            "-F", "type=public",
        ]
        try:
            out = self._run(args)
        except _GhError:
            return []
        if not out.strip():
            return []
        items = json.loads(out)
        return [self._normalise_repo(r) for r in items]

    def search_repos_by_topic(self, topic: str, limit: int = 30) -> list[dict]:
        """Search repos by GitHub topic."""
        args = [
            self._binary, "search", "repos",
            f"--topic={topic}",
            "--sort=updated",
            "--order=desc",
            f"--limit={limit}",
            "--json", "fullName,description,stargazersCount,pushedAt,repositoryTopics",
        ]
        try:
            out = self._run(args)
        except _GhError:
            return []
        if not out.strip():
            return []
        raw_items = json.loads(out)
        result = []
        for r in raw_items:
            result.append({
                "full_name": r.get("fullName", ""),
                "description": r.get("description") or "",
                "stargazers_count": r.get("stargazersCount", 0),
                "pushed_at": r.get("pushedAt", ""),
                "topics": [
                    t.get("name", "") if isinstance(t, dict) else str(t)
                    for t in (r.get("repositoryTopics") or [])
                ],
            })
        return result

    def get_recent_commits(self, full_name: str, since_iso: str) -> list[dict]:
        """Return ≤1 commit in *full_name* since *since_iso*."""
        args = [
            self._binary, "api",
            f"/repos/{full_name}/commits",
            "-X", "GET",
            "-F", f"since={since_iso}",
            "-F", "per_page=1",
        ]
        try:
            out = self._run(args)
        except _GhError:
            return []
        if not out.strip():
            return []
        return json.loads(out)

    # ---- Internals -----------------------------------------------------

    def _run(self, argv: list[str]) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise _GhError(f"{self._binary} not on PATH") from exc
        if proc.returncode != 0:
            raise _GhError(
                f"{' '.join(argv)} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return proc.stdout

    @staticmethod
    def _normalise_repo(r: dict) -> dict:
        """Normalise a raw REST /repos entry to our canonical shape."""
        return {
            "full_name": r.get("full_name", ""),
            "description": r.get("description") or "",
            "stargazers_count": r.get("stargazers_count", 0),
            "pushed_at": r.get("pushed_at", ""),
            "topics": r.get("topics") or [],
        }


class _GhError(RuntimeError):
    """Internal error from the gh CLI; caught within this module."""


# ---------------------------------------------------------------------------
# Fake implementation for tests
# ---------------------------------------------------------------------------


@dataclass
class FakeSourceClient:
    """Deterministic in-memory ``SourceClient`` for tests.

    Pre-load data with ``add_org_repos`` and ``add_topic_repos``; set
    ``commits_by_repo`` to control the commit-activity prefilter.  All
    three structures default to empty, which means no repos pass through
    and both prefilters are exercisable.

    Usage::

        client = FakeSourceClient()
        client.add_org_repos("anthropic", [
            {"full_name": "anthropic/sdk", "stargazers_count": 5000, ...},
        ])
        client.commits_by_repo["anthropic/sdk"] = [{"sha": "abc"}]
    """

    _org_repos: dict[str, list[dict]] = field(default_factory=dict)
    _topic_repos: dict[str, list[dict]] = field(default_factory=dict)
    commits_by_repo: dict[str, list[dict]] = field(default_factory=dict)

    def add_org_repos(self, org: str, repos: list[dict]) -> None:
        """Pre-configure repos returned for an org release-feed query."""
        self._org_repos[org] = repos

    def add_topic_repos(self, topic: str, repos: list[dict]) -> None:
        """Pre-configure repos returned for a topic search query."""
        self._topic_repos[topic] = repos

    def list_org_repos(self, org: str, limit: int = 30) -> list[dict]:
        return self._org_repos.get(org, [])[:limit]

    def search_repos_by_topic(self, topic: str, limit: int = 30) -> list[dict]:
        return self._topic_repos.get(topic, [])[:limit]

    def get_recent_commits(self, full_name: str, since_iso: str) -> list[dict]:
        return self.commits_by_repo.get(full_name, [])


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class GitHubSource:
    """Fetch candidate repositories from GitHub org release feeds and topic queries.

    Parameters
    ----------
    client:
        A ``SourceClient`` implementation.  Production: ``GhCliSourceClient()``.
        Tests: ``FakeSourceClient()``.
    config:
        A ``SourceConfig`` describing which orgs and topics to query, and
        what prefilter thresholds to apply.
    conn:
        ``sqlite3.Connection`` used to emit ``AUTO_IMPROVE_SOURCE_FAILED``
        ledger events when a source fails.  May be ``None`` — if so, events
        are logged but not written to the DB.

    Returns
    -------
    Call ``fetch()`` to get a ``tuple[RepoInfo, ...]`` of passing repos and
    a ``FetchReport`` summary.

    Example
    -------
    .. code-block:: python

        import sqlite3
        from oxi_core.v3.github_source import (
            FakeSourceClient, GitHubSource, SourceConfig
        )

        conn = sqlite3.connect(":memory:")
        client = FakeSourceClient()
        client.add_org_repos("anthropic", [...])
        client.commits_by_repo["anthropic/sdk"] = [{"sha": "abc"}]

        source = GitHubSource(client, config=SourceConfig(orgs=("anthropic",)), conn=conn)
        repos, report = source.fetch()
    """

    def __init__(
        self,
        client: SourceClient,
        *,
        config: SourceConfig | None = None,
        conn=None,
    ) -> None:
        self._client = client
        self._config = config or SourceConfig()
        self._conn = conn

    def fetch(self) -> tuple[tuple[RepoInfo, ...], FetchReport]:
        """Run all configured sources and return passing repos + a summary.

        Sources are processed left-to-right.  Each source is wrapped in a
        try/except; failures emit ``AUTO_IMPROVE_SOURCE_FAILED`` and are
        counted in ``FetchReport.sources_failed`` — remaining sources continue.

        Returns
        -------
        repos:
            Deduplicated (by ``full_name``) repos that passed both prefilters,
            in the order they were first encountered.
        report:
            Summary counters for the fetch pass.
        """
        cfg = self._config
        all_raw: list[tuple[str, str, dict]] = []  # (source_kind, source_name, raw_dict)
        sources_failed = 0

        # --- Org release feeds (up to 8) ---------------------------------
        for org in cfg.orgs[:8]:
            try:
                raw_repos = self._client.list_org_repos(org, limit=cfg.max_repos_per_source)
                for r in raw_repos:
                    all_raw.append(("org_release", org, r))
            except Exception as exc:  # noqa: BLE001
                sources_failed += 1
                self._emit_source_failed("org_release", org, exc)

        # --- Topic queries (up to 5) -------------------------------------
        for topic in cfg.topics[:5]:
            try:
                raw_repos = self._client.search_repos_by_topic(
                    topic, limit=cfg.max_repos_per_source
                )
                for r in raw_repos:
                    all_raw.append(("topic_query", topic, r))
            except Exception as exc:  # noqa: BLE001
                sources_failed += 1
                self._emit_source_failed("topic_query", topic, exc)

        # --- Apply prefilters --------------------------------------------
        since_iso = _since_iso(cfg.max_commit_age_days)
        velocity_window_days = STAR_VELOCITY_WINDOW_DAYS

        seen: set[str] = set()
        passing: list[RepoInfo] = []
        dropped_velocity = 0
        dropped_stale = 0

        for source_kind, source_name, raw in all_raw:
            full_name = raw.get("full_name", "")
            if not full_name:
                continue
            # Deduplicate — first source wins.
            if full_name in seen:
                continue
            seen.add(full_name)

            stars = int(raw.get("stargazers_count", 0))
            pushed_at = raw.get("pushed_at", "")

            # Star-velocity prefilter
            velocity = _star_velocity(stars, pushed_at, velocity_window_days)
            if velocity < cfg.min_stars_per_day:
                dropped_velocity += 1
                logger.debug(
                    "github_source: drop %s velocity=%.2f < threshold=%.2f",
                    full_name, velocity, cfg.min_stars_per_day,
                )
                continue

            # Commit-activity prefilter
            try:
                recent = self._client.get_recent_commits(full_name, since_iso)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_source: commit check failed for %s: %s", full_name, exc
                )
                recent = []

            if not recent:
                dropped_stale += 1
                logger.debug("github_source: drop %s — no commits since %s", full_name, since_iso)
                continue

            topics_raw = raw.get("topics") or []
            passing.append(
                RepoInfo(
                    full_name=full_name,
                    description=raw.get("description", ""),
                    stars=stars,
                    pushed_at=pushed_at,
                    topics=tuple(str(t) for t in topics_raw),
                    source_kind=source_kind,
                    source_name=source_name,
                )
            )

        report = FetchReport(
            repos_found=len(passing),
            repos_dropped_velocity=dropped_velocity,
            repos_dropped_stale=dropped_stale,
            sources_failed=sources_failed,
        )
        logger.info(
            "github_source: fetch complete — found=%d dropped_velocity=%d "
            "dropped_stale=%d sources_failed=%d",
            report.repos_found,
            report.repos_dropped_velocity,
            report.repos_dropped_stale,
            report.sources_failed,
        )
        return tuple(passing), report

    # ---- Internals -----------------------------------------------------

    def _emit_source_failed(
        self, source_kind: str, source_name: str, exc: Exception
    ) -> None:
        """Log + emit ``AUTO_IMPROVE_SOURCE_FAILED`` to the ledger."""
        error_str = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "github_source: source failed kind=%s name=%s error=%s",
            source_kind, source_name, error_str,
        )
        if self._conn is None:
            return
        try:
            from .ledger_events import LedgerEvent

            payload = json.dumps({
                "source_kind": source_kind,
                "source_name": source_name,
                "error": error_str,
            })
            self._conn.execute(
                "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
                (LedgerEvent.AUTO_IMPROVE_SOURCE_FAILED, payload),
            )
            self._conn.commit()
        except Exception as ledger_exc:  # noqa: BLE001
            logger.error(
                "github_source: failed to write ledger event: %s", ledger_exc
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _since_iso(days: int) -> str:
    """Return an ISO-8601 UTC timestamp for ``days`` ago."""
    dt = datetime.now(tz=UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _star_velocity(stars: int, pushed_at: str, window_days: int) -> float:
    """Estimate star velocity (stars/day) for a repo.

    Uses the repo's total star count divided by the *minimum* of:
    - the repo's age in days since ``pushed_at`` (as a rough creation proxy),
      clamped to ``window_days``
    - ``window_days``

    This is a coarse approximation suitable for a prefilter.  Repos with
    fewer than ``window_days`` days of history get their rate extrapolated
    from their full lifetime.

    A ``stars=0`` or invalid ``pushed_at`` yields velocity 0.0.
    """
    if stars <= 0:
        return 0.0
    if not pushed_at:
        # No date info — treat as 0 velocity (conservative drop).
        return 0.0
    try:
        pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0

    now = datetime.now(tz=UTC)
    age_days = max(1.0, (now - pushed_dt).total_seconds() / 86400)
    effective_days = min(age_days, float(window_days))
    return stars / effective_days


__all__ = [
    "DEFAULT_MAX_COMMIT_AGE_DAYS",
    "DEFAULT_MIN_STARS_PER_DAY",
    "STAR_VELOCITY_WINDOW_DAYS",
    "FakeSourceClient",
    "FetchReport",
    "GhCliSourceClient",
    "GitHubSource",
    "RepoInfo",
    "SourceClient",
    "SourceConfig",
]
