"""newsletter — NewsletterSource fetching AI newsletter archives via HTTP scraping.

Implements :class:`NewsletterSource` against three public newsletter archives:

- **AlphaSignal** (``alphasignal.ai/archives``)
- **Latent Space** (``latent.space`` via Substack feed)
- **Ben's Bites** (``bensbites.beehiiv.com/archive``)

Architecture
------------
Each newsletter has its own **dedicated parser function** so a layout change
to one doesn't take down all three.  The parser functions are:

- :func:`_parse_alphasignal` — selects ``article.issue-card`` elements.
- :func:`_parse_latentspace` — selects ``div.post-preview`` elements.
- :func:`_parse_bensbites` — selects ``div.archive-item`` elements.

All three call :func:`_parse_generic_anchors` as a fallback when the
primary selector yields nothing.

HTTP fetching
~~~~~~~~~~~~~
:class:`NewsletterSource` accepts an :class:`HTTPFetcher` (a simple
Protocol with ``get(url) -> str``).  Production uses
:class:`HttpxFetcher`; tests inject :class:`FakeHTTPFetcher`
(in ``tests/fixtures/fake_http.py``).

Per-source isolation
~~~~~~~~~~~~~~~~~~~~
Each newsletter URL is fetched and parsed inside its own ``try/except``.
If one fails (network error, parse error, empty result), the failure is
logged and the other newsletters continue.  The ``sources_failed`` counter
in :class:`NewsletterFetchReport` records how many failed.

Ledger events
~~~~~~~~~~~~~
This module does **not** write ledger events directly — it returns a
:class:`NewsletterFetchReport` whose ``errors`` list the caller (scan.py)
uses to emit ``AUTO_IMPROVE_SOURCE_FAILED`` events if desired.  This keeps
the source implementation free of the DB dependency.

Configuration
~~~~~~~~~~~~~
:class:`NewsletterConfig` is a frozen dataclass callers construct.  The
defaults target the three known-good archive URLs.

Usage::

    from oxi_core.v3.auto_external.sources.newsletter import (
        NewsletterSource, NewsletterConfig, HttpxFetcher,
    )
    from datetime import UTC, datetime

    source = NewsletterSource(HttpxFetcher(), config=NewsletterConfig())
    items = source.fetch(datetime.now(tz=UTC))
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urljoin

from ..sources import make_raw_item

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default URLs
# ---------------------------------------------------------------------------

#: AlphaSignal public archive.
ALPHASIGNAL_URL: str = "https://alphasignal.ai/archive"

#: Latent Space Substack archive.
LATENTSPACE_URL: str = "https://www.latent.space/archive"

#: Ben's Bites Beehiiv archive.
BENSBITES_URL: str = "https://bensbites.beehiiv.com/archive"

#: All three default archive URLs in canonical order.
DEFAULT_NEWSLETTER_URLS: tuple[str, ...] = (
    ALPHASIGNAL_URL,
    LATENTSPACE_URL,
    BENSBITES_URL,
)

# ---------------------------------------------------------------------------
# Source identifiers (used in RawItem["source"])
# ---------------------------------------------------------------------------

_SOURCE_ALPHASIGNAL = "newsletter:alphasignal"
_SOURCE_LATENTSPACE = "newsletter:latentspace"
_SOURCE_BENSBITES = "newsletter:bensbites"
_SOURCE_UNKNOWN = "newsletter:unknown"


def _source_id_for_url(url: str) -> str:
    """Return a stable source identifier string for the given archive URL."""
    if "alphasignal" in url:
        return _SOURCE_ALPHASIGNAL
    if "latent.space" in url:
        return _SOURCE_LATENTSPACE
    if "bensbites" in url:
        return _SOURCE_BENSBITES
    return _SOURCE_UNKNOWN


# ---------------------------------------------------------------------------
# HTTPFetcher Protocol
# ---------------------------------------------------------------------------


class HTTPFetcher(Protocol):
    """Minimal HTTP client interface used by :class:`NewsletterSource`.

    The interface is intentionally thin so tests can inject a fake without
    any HTTP stack.  Production uses :class:`HttpxFetcher`.
    """

    def get(self, url: str) -> str:
        """Fetch *url* and return the response body as a string.

        Raises :exc:`Exception` on HTTP error or network failure.
        The caller (NewsletterSource) catches and logs these exceptions.
        """
        ...


# ---------------------------------------------------------------------------
# Production HTTPFetcher
# ---------------------------------------------------------------------------


class HttpxFetcher:
    """Production :class:`HTTPFetcher` using :mod:`httpx`.

    A minimal synchronous HTTP client that follows redirects and sets a
    realistic ``User-Agent`` header so archive pages don't 403.

    Parameters
    ----------
    timeout:
        Request timeout in seconds (default 20).
    """

    _USER_AGENT = (
        "Mozilla/5.0 (compatible; oxi-core/1 newsletter-fetcher; "
        "+https://github.com/escotilha/oxi)"
    )

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    def get(self, url: str) -> str:
        """Fetch *url* synchronously and return the body text."""
        import httpx  # local import — keeps the module importable without httpx in CI

        headers = {"User-Agent": self._USER_AGENT, "Accept": "text/html"}
        with httpx.Client(follow_redirects=True, timeout=self._timeout) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsletterConfig:
    """Configuration for :class:`NewsletterSource`.

    Attributes
    ----------
    urls:
        Archive URLs to fetch.  Defaults to the three canonical newsletters.
    max_items_per_source:
        Maximum :class:`RawItem` objects returned per newsletter URL.
    """

    urls: tuple[str, ...] = DEFAULT_NEWSLETTER_URLS
    max_items_per_source: int = 20


# ---------------------------------------------------------------------------
# Fetch report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewsletterFetchReport:
    """Summary of one :meth:`NewsletterSource.fetch` call.

    Attributes
    ----------
    items_found:
        Total items collected across all newsletters that succeeded.
    sources_succeeded:
        Number of newsletter URLs that produced at least one item.
    sources_failed:
        Number of newsletter URLs that raised an exception or yielded
        zero items after parsing.
    errors:
        Per-source error messages; one entry per failed source.
    """

    items_found: int
    sources_succeeded: int
    sources_failed: int
    errors: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Per-newsletter parsers
# ---------------------------------------------------------------------------


def _parse_alphasignal(html: str, base_url: str, now: datetime) -> list[dict]:
    """Parse an AlphaSignal archive page.

    Selects ``article.issue-card`` elements.  Each card contains:
    - ``a.issue-link`` → URL (may be relative) + title from ``h2.issue-title``
    - ``p.issue-summary`` → description
    - ``p.issue-date`` → publication date string
    - ``li.tag`` → tags

    Falls back to :func:`_parse_generic_anchors` if no cards are found.
    """
    from selectolax.parser import HTMLParser  # deferred — keeps module importable

    tree = HTMLParser(html)
    items: list[dict] = []

    for card in tree.css("article.issue-card"):
        try:
            link_node = card.css_first("a.issue-link")
            if link_node is None:
                continue
            url = _resolve_url(link_node.attributes.get("href", ""), base_url)
            if not url:
                continue

            title_node = card.css_first("h2.issue-title")
            title = _clean_text(title_node.text() if title_node else link_node.text())
            if not title:
                continue

            summary_node = card.css_first("p.issue-summary")
            summary = _clean_text(summary_node.text() if summary_node else "")

            date_node = card.css_first("p.issue-date")
            published_at = _clean_text(date_node.text() if date_node else "")

            tags = [
                _clean_text(t.text())
                for t in card.css("li.tag")
                if _clean_text(t.text())
            ]

            items.append(make_raw_item(
                source=_SOURCE_ALPHASIGNAL,
                title=title,
                url=url,
                summary=summary,
                published_at=published_at,
                tags=tags,
                now=now,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("alphasignal: skipping malformed card: %s", exc)

    if not items:
        logger.debug("alphasignal: primary selector found nothing, trying generic fallback")
        items = _parse_generic_anchors(html, base_url, _SOURCE_ALPHASIGNAL, now)

    return items


def _parse_latentspace(html: str, base_url: str, now: datetime) -> list[dict]:
    """Parse a Latent Space (Substack) archive page.

    Selects ``div.post-preview`` elements.  Each preview contains:
    - ``a.post-preview-title`` → URL + title text
    - ``div.post-preview-subtitle`` → description
    - ``time.post-preview-date[datetime]`` → ISO publication date

    Falls back to :func:`_parse_generic_anchors` if no previews are found.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    items: list[dict] = []

    for preview in tree.css("div.post-preview"):
        try:
            link_node = preview.css_first("a.post-preview-title")
            if link_node is None:
                continue
            url = _resolve_url(link_node.attributes.get("href", ""), base_url)
            if not url:
                continue

            title = _clean_text(link_node.text())
            if not title:
                continue

            subtitle_node = preview.css_first("div.post-preview-subtitle")
            summary = _clean_text(subtitle_node.text() if subtitle_node else "")

            time_node = preview.css_first("time.post-preview-date")
            published_at = ""
            if time_node is not None:
                # Prefer the datetime= attribute; fall back to visible text.
                published_at = (
                    time_node.attributes.get("datetime", "")
                    or _clean_text(time_node.text())
                )

            items.append(make_raw_item(
                source=_SOURCE_LATENTSPACE,
                title=title,
                url=url,
                summary=summary,
                published_at=published_at,
                now=now,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("latentspace: skipping malformed preview: %s", exc)

    if not items:
        logger.debug("latentspace: primary selector found nothing, trying generic fallback")
        items = _parse_generic_anchors(html, base_url, _SOURCE_LATENTSPACE, now)

    return items


def _parse_bensbites(html: str, base_url: str, now: datetime) -> list[dict]:
    """Parse a Ben's Bites (Beehiiv) archive page.

    Selects ``div.archive-item`` elements.  Each item contains:
    - ``h3.archive-item-title > a`` → URL + title text
    - ``p.archive-item-description`` → description
    - ``span.archive-item-date`` → publication date string

    Falls back to :func:`_parse_generic_anchors` if no items are found.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    items: list[dict] = []

    for item in tree.css("div.archive-item"):
        try:
            link_node = item.css_first("h3.archive-item-title a")
            if link_node is None:
                link_node = item.css_first("a")
            if link_node is None:
                continue
            url = _resolve_url(link_node.attributes.get("href", ""), base_url)
            if not url:
                continue

            title = _clean_text(link_node.text())
            if not title:
                continue

            desc_node = item.css_first("p.archive-item-description")
            summary = _clean_text(desc_node.text() if desc_node else "")

            date_node = item.css_first("span.archive-item-date")
            published_at = _clean_text(date_node.text() if date_node else "")

            items.append(make_raw_item(
                source=_SOURCE_BENSBITES,
                title=title,
                url=url,
                summary=summary,
                published_at=published_at,
                now=now,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("bensbites: skipping malformed item: %s", exc)

    if not items:
        logger.debug("bensbites: primary selector found nothing, trying generic fallback")
        items = _parse_generic_anchors(html, base_url, _SOURCE_BENSBITES, now)

    return items


# ---------------------------------------------------------------------------
# Generic fallback parser
# ---------------------------------------------------------------------------


#: Patterns that suggest a link points to an archive issue rather than
#: a navigation / social link.  At least one pattern must match.
_ISSUE_PATH_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"/issues?/"),
    re.compile(r"/p/"),   # Substack post path
    re.compile(r"/posts?/"),
    re.compile(r"/archive/"),
    re.compile(r"/newsletters?/"),
    re.compile(r"/editions?/"),
]

#: Link text patterns to skip (navigation, social, etc.).
_SKIP_LINK_RE = re.compile(
    r"^(subscribe|home|about|twitter|x\.com|linkedin|rss|sign\s*in|log\s*in|"
    r"privacy|terms|contact|follow|unsubscribe)$",
    re.IGNORECASE,
)


def _parse_generic_anchors(
    html: str, base_url: str, source_id: str, now: datetime
) -> list[dict]:
    """Generic fallback parser: extract anchors that look like archive links.

    Used when the primary parser finds nothing (layout changed, empty page,
    etc.).  Picks ``<a>`` tags whose ``href`` matches
    :data:`_ISSUE_PATH_PATTERNS` and whose visible text looks like an
    article title (non-empty, not a nav-link keyword).

    Parameters
    ----------
    html:
        Raw HTML string.
    base_url:
        Base URL for resolving relative hrefs.
    source_id:
        Source identifier to embed in each :class:`RawItem`.
    now:
        Wall-clock override for ``fetched_at``.
    """
    from selectolax.parser import HTMLParser

    tree = HTMLParser(html)
    items: list[dict] = []
    seen_urls: set[str] = set()

    for node in tree.css("a[href]"):
        try:
            href = node.attributes.get("href", "") or ""
            url = _resolve_url(href, base_url)
            if not url or url in seen_urls:
                continue

            # Must look like an issue path
            if not any(p.search(url) for p in _ISSUE_PATH_PATTERNS):
                continue

            text = _clean_text(node.text())
            if not text or _SKIP_LINK_RE.match(text):
                continue

            seen_urls.add(url)
            items.append(make_raw_item(
                source=source_id,
                title=text,
                url=url,
                now=now,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("generic_fallback: skipping malformed anchor: %s", exc)

    return items


# ---------------------------------------------------------------------------
# Dispatcher — picks the right per-newsletter parser
# ---------------------------------------------------------------------------

# (url_substring, parser_function) — tried in order; first match wins.
_PARSERS: list[tuple[str, object]] = [
    ("alphasignal", _parse_alphasignal),
    ("latent.space", _parse_latentspace),
    ("bensbites", _parse_bensbites),
]


def _dispatch_parser(url: str, html: str, now: datetime) -> list[dict]:
    """Select the right parser for *url* and run it.

    Falls back to :func:`_parse_generic_anchors` when no dedicated parser
    matches — this keeps the class usable against arbitrary newsletter URLs
    added later via ``NewsletterConfig.urls``.
    """
    for fragment, parser in _PARSERS:
        if fragment in url:
            return parser(html, url, now)  # type: ignore[operator]

    logger.debug("newsletter: no dedicated parser for %s, using generic fallback", url)
    return _parse_generic_anchors(html, url, _source_id_for_url(url), now)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class NewsletterSource:
    """Fetch newsletter archive items from configured URLs.

    Parameters
    ----------
    fetcher:
        :class:`HTTPFetcher` implementation.  Production: :class:`HttpxFetcher`.
        Tests: :class:`~tests.fixtures.fake_http.FakeHTTPFetcher`.
    config:
        :class:`NewsletterConfig` describing which URLs to fetch and
        how many items to return per source.
    """

    def __init__(
        self,
        fetcher: HTTPFetcher,
        *,
        config: NewsletterConfig | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._config = config or NewsletterConfig()

    def fetch(self, now: datetime) -> list[dict]:
        """Fetch and parse all configured newsletter archives.

        Each URL is fetched and parsed in its own ``try/except``.  A failure
        in one newsletter does not prevent the others from running.

        Parameters
        ----------
        now:
            Wall-clock reference time.  Embedded in each returned
            :class:`RawItem` as ``fetched_at``.

        Returns
        -------
        list[dict]
            Deduplicated (by URL) :class:`RawItem` dicts from all
            newsletters that succeeded.  May be empty.
        """
        cfg = self._config
        all_items: list[dict] = []
        seen_urls: set[str] = set()

        for url in cfg.urls:
            try:
                html = self._fetcher.get(url)
                raw = _dispatch_parser(url, html, now)
                capped = raw[: cfg.max_items_per_source]
                for item in capped:
                    item_url = item.get("url", "")
                    if item_url and item_url not in seen_urls:
                        seen_urls.add(item_url)
                        all_items.append(item)
                logger.info(
                    "newsletter: %s → %d items (cap=%d)",
                    url,
                    len(capped),
                    cfg.max_items_per_source,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("newsletter: fetch/parse failed for %s: %s", url, exc)

        return all_items

    def fetch_with_report(self, now: datetime) -> tuple[list[dict], NewsletterFetchReport]:
        """Like :meth:`fetch` but also returns a :class:`NewsletterFetchReport`.

        Useful for callers (e.g. scan.py) that want to emit ledger events
        for failed sources without reimplementing the error tracking.

        Returns
        -------
        items:
            Same as :meth:`fetch`.
        report:
            Summary of successes and failures.
        """
        cfg = self._config
        all_items: list[dict] = []
        seen_urls: set[str] = set()
        succeeded = 0
        failed = 0
        errors: list[str] = []

        for url in cfg.urls:
            try:
                html = self._fetcher.get(url)
                raw = _dispatch_parser(url, html, now)
                capped = raw[: cfg.max_items_per_source]
                new_items = 0
                for item in capped:
                    item_url = item.get("url", "")
                    if item_url and item_url not in seen_urls:
                        seen_urls.add(item_url)
                        all_items.append(item)
                        new_items += 1
                if new_items > 0:
                    succeeded += 1
                else:
                    failed += 1
                    errors.append(f"{url}: parsed 0 items")
                logger.info(
                    "newsletter: %s → %d items (cap=%d)",
                    url,
                    new_items,
                    cfg.max_items_per_source,
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                error_str = f"{url}: {type(exc).__name__}: {exc}"
                errors.append(error_str)
                logger.warning("newsletter: fetch/parse failed for %s: %s", url, exc)

        report = NewsletterFetchReport(
            items_found=len(all_items),
            sources_succeeded=succeeded,
            sources_failed=failed,
            errors=tuple(errors),
        )
        return all_items, report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_text(text: str | None) -> str:
    """Collapse whitespace and strip a text node."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _resolve_url(href: str, base_url: str) -> str:
    """Return an absolute URL from *href* and *base_url*.

    Returns an empty string if the result doesn't look like a valid HTTP URL.
    """
    if not href:
        return ""
    url = urljoin(base_url, href)
    if not url.startswith(("http://", "https://")):
        return ""
    return url


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "ALPHASIGNAL_URL",
    "BENSBITES_URL",
    "DEFAULT_NEWSLETTER_URLS",
    "LATENTSPACE_URL",
    "HTTPFetcher",
    "HttpxFetcher",
    "NewsletterConfig",
    "NewsletterFetchReport",
    "NewsletterSource",
]
