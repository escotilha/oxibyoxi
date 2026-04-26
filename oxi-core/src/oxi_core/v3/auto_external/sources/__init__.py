"""auto_external.sources — Source Protocol and RawItem shared data type.

Each source module (``github``, ``newsletter``, ``x``) implements the
:class:`Source` Protocol: a single callable ``fetch(now)`` that returns a
list of :class:`RawItem` dicts.

RawItem shape
-------------
A ``RawItem`` is a plain ``dict`` with a well-known key set.  Callers
(ranking, judge, emit) treat it as a typed record.  Mandatory keys:

    ``source``   (str) — source identifier, e.g. ``"newsletter:alphasignal"``.
    ``title``    (str) — article / post title.
    ``url``      (str) — canonical URL.
    ``summary``  (str) — one-paragraph description (may be empty).
    ``fetched_at`` (str) — ISO-8601 UTC timestamp of the fetch.

Optional keys that sources may include:

    ``published_at`` (str)  — ISO-8601 publication date if known.
    ``tags``         (list[str]) — free-form tags.

The dict is intentionally untyped (plain ``dict``) rather than a typed
dataclass so downstream stages can pass it through JSON serialisation
without a custom encoder, and so new keys don't require a schema change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# RawItem — factory helper
# ---------------------------------------------------------------------------


def make_raw_item(
    *,
    source: str,
    title: str,
    url: str,
    summary: str = "",
    published_at: str = "",
    tags: list[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Build a canonical :class:`RawItem` dict.

    Parameters
    ----------
    source:
        Source identifier, e.g. ``"newsletter:alphasignal"``.
    title:
        Article or post title.
    url:
        Canonical URL.
    summary:
        One-paragraph description (empty string if unknown).
    published_at:
        ISO-8601 publication timestamp, empty string if unknown.
    tags:
        Optional list of free-form tag strings.
    now:
        Override for ``fetched_at``; production callers omit this
        (defaults to ``datetime.now(tz=UTC).isoformat()``).

    Returns
    -------
    dict
        A ``RawItem`` dict ready for the ranking / judge / emit stages.
    """
    fetched_at = (now if now is not None else datetime.now(tz=UTC)).isoformat()
    return {
        "source": source,
        "title": title,
        "url": url,
        "summary": summary,
        "published_at": published_at,
        "tags": tags or [],
        "fetched_at": fetched_at,
    }


# ---------------------------------------------------------------------------
# Source Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Source(Protocol):
    """Minimal interface every auto-external source must implement.

    Each concrete source (GitHub, Newsletter, X) implements this protocol.
    ``fetch`` is the only required method; sources are stateless — the
    caller constructs the source, calls ``fetch(now)``, and discards it.

    Per-source try/except
    ~~~~~~~~~~~~~~~~~~~~~
    Each source is responsible for catching its own exceptions and returning
    a partial result (possibly an empty list) rather than propagating.  The
    :func:`oxi_core.v3.auto_external.scan._run_sources` orchestrator wraps
    *each source call* in an additional outer try/except that emits
    ``AUTO_IMPROVE_SOURCE_FAILED`` to the ledger — so even a source that
    forgets to catch an exception doesn't take down the whole pipeline.
    """

    def fetch(self, now: datetime) -> list[dict]:
        """Return a list of :class:`RawItem` dicts.

        Parameters
        ----------
        now:
            Wall-clock override.  Production callers pass
            ``datetime.now(tz=UTC)``; tests pass a fixed datetime.

        Returns
        -------
        list[dict]
            Zero or more :class:`RawItem` dicts.  Empty list is always a
            valid return — callers must not raise on empty results.
        """
        ...


__all__ = [
    "Source",
    "make_raw_item",
]
