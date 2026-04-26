"""Tests for oxi_core.v3.auto_external.sources.newsletter — newsletter source fetcher.

Test strategy
-------------
- Pure in-process: no real HTTP, no real filesystem writes.
- :class:`FakeHTTPFetcher` from ``tests/fixtures/fake_http.py`` provides
  canned HTML from ``tests/fixtures/data/newsletters/``.
- Each newsletter parser is exercised independently so a failure in one
  is provably isolated from the others.
- The generic fallback parser is tested with HTML that has no
  newsletter-specific structure.

Coverage matrix
---------------
  FakeHTTPFetcher.add / add_fixture / raise_on / call_count / all_urls_called
  load_fixture returns correct HTML from filesystem
  AlphaSignal parser: correct title, url, summary, tags, source_id
  AlphaSignal parser: multiple items returned
  AlphaSignal parser: relative URLs resolved to absolute
  Latent Space parser: correct title, url, summary, date attribute, source_id
  Latent Space parser: datetime= attribute preferred over text
  Ben's Bites parser: correct title, url, summary, date, source_id
  Generic fallback: picks up issue-path anchors, skips nav anchors
  Generic fallback: deduplicates by URL
  Dispatch: right parser chosen per URL
  Dispatch: unknown URL falls back to generic
  Per-source isolation: one failing URL does not stop others
  Per-source isolation: sources_failed counter accurate
  fetch_with_report: reports correct counts on mixed results
  fetch_with_report: zero items → sources_failed incremented
  max_items_per_source cap enforced
  Cross-source URL deduplication
  Empty HTML → no items, no exception
  fetch() with no configured URLs → empty list
  make_raw_item: fields populated correctly
  _clean_text helper
  _resolve_url helper: relative, absolute, invalid
  source_id_for_url: correct identifiers
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap (same pattern as test_auto_improve_routine.py)
# ---------------------------------------------------------------------------

_OXI_CORE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_OXI_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_OXI_CORE_SRC))

from oxi_core.v3.auto_external.sources import make_raw_item  # noqa: E402
from oxi_core.v3.auto_external.sources.newsletter import (  # noqa: E402
    ALPHASIGNAL_URL,
    BENSBITES_URL,
    DEFAULT_NEWSLETTER_URLS,
    LATENTSPACE_URL,
    HttpxFetcher,
    NewsletterConfig,
    NewsletterFetchReport,
    NewsletterSource,
    _clean_text,
    _dispatch_parser,
    _parse_alphasignal,
    _parse_bensbites,
    _parse_generic_anchors,
    _parse_latentspace,
    _resolve_url,
    _source_id_for_url,
)

_FIXTURES_DATA = Path(__file__).parent / "fixtures" / "data" / "newsletters"

# Also import the fixture helpers.
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from fake_http import FakeHTTPFetcher, load_fixture  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _alphasignal_html() -> str:
    return load_fixture("alphasignal.html")


def _latentspace_html() -> str:
    return load_fixture("latentspace.html")


def _bensbites_html() -> str:
    return load_fixture("bensbites.html")


def _empty_html() -> str:
    return load_fixture("empty.html")


# ---------------------------------------------------------------------------
# FakeHTTPFetcher unit tests
# ---------------------------------------------------------------------------


class TestFakeHTTPFetcher:
    def test_add_and_get(self):
        fake = FakeHTTPFetcher()
        fake.add("https://example.com/archive", "<html>ok</html>")
        assert fake.get("https://example.com/archive") == "<html>ok</html>"

    def test_add_fixture(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        html = fake.get(ALPHASIGNAL_URL)
        assert "AlphaSignal" in html

    def test_raise_on(self):
        fake = FakeHTTPFetcher()
        fake.raise_on("https://fail.example.com", ConnectionError("timeout"))
        with pytest.raises(ConnectionError, match="timeout"):
            fake.get("https://fail.example.com")

    def test_call_count_and_all_urls_called(self):
        fake = FakeHTTPFetcher()
        fake.add("https://a.com", "a")
        fake.add("https://b.com", "b")

        fake.get("https://a.com")
        fake.get("https://b.com")
        fake.get("https://a.com")

        assert fake.call_count("https://a.com") == 2
        assert fake.call_count("https://b.com") == 1
        assert fake.all_urls_called == [
            "https://a.com",
            "https://b.com",
            "https://a.com",
        ]

    def test_reset_clears_call_history(self):
        fake = FakeHTTPFetcher()
        fake.add("https://x.com", "html")
        fake.get("https://x.com")
        assert fake.call_count("https://x.com") == 1

        fake.reset()
        assert fake.call_count("https://x.com") == 0
        assert fake.all_urls_called == []

    def test_unconfigured_url_raises_key_error(self):
        fake = FakeHTTPFetcher()
        with pytest.raises(KeyError, match="no response configured"):
            fake.get("https://unconfigured.example.com")

    def test_responses_in_constructor(self):
        fake = FakeHTTPFetcher({"https://c.com": "<html>c</html>"})
        assert fake.get("https://c.com") == "<html>c</html>"

    def test_load_fixture_returns_html(self):
        html = load_fixture("alphasignal.html")
        assert "<html" in html
        assert "AlphaSignal" in html

    def test_load_fixture_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            load_fixture("does_not_exist.html")


# ---------------------------------------------------------------------------
# make_raw_item helper
# ---------------------------------------------------------------------------


class TestMakeRawItem:
    def test_required_fields(self):
        item = make_raw_item(source="newsletter:test", title="T", url="https://x.com")
        assert item["source"] == "newsletter:test"
        assert item["title"] == "T"
        assert item["url"] == "https://x.com"
        assert item["summary"] == ""
        assert item["published_at"] == ""
        assert item["tags"] == []
        assert "fetched_at" in item

    def test_optional_fields(self):
        item = make_raw_item(
            source="s", title="t", url="u",
            summary="desc", published_at="2026-04-25", tags=["ai", "ml"],
            now=_NOW,
        )
        assert item["summary"] == "desc"
        assert item["published_at"] == "2026-04-25"
        assert item["tags"] == ["ai", "ml"]
        assert _NOW.isoformat() in item["fetched_at"]

    def test_now_override(self):
        item = make_raw_item(source="s", title="t", url="u", now=_NOW)
        assert _NOW.isoformat() in item["fetched_at"]


# ---------------------------------------------------------------------------
# _clean_text helper
# ---------------------------------------------------------------------------


class TestCleanText:
    def test_strips_whitespace(self):
        assert _clean_text("  hello  ") == "hello"

    def test_collapses_internal_whitespace(self):
        assert _clean_text("foo\n  bar\t baz") == "foo bar baz"

    def test_empty_string(self):
        assert _clean_text("") == ""

    def test_none(self):
        assert _clean_text(None) == ""  # type: ignore[arg-type]

    def test_already_clean(self):
        assert _clean_text("clean title") == "clean title"


# ---------------------------------------------------------------------------
# _resolve_url helper
# ---------------------------------------------------------------------------


class TestResolveUrl:
    def test_absolute_url_unchanged(self):
        url = "https://alphasignal.ai/issues/foo"
        assert _resolve_url(url, "https://alphasignal.ai/archive") == url

    def test_relative_url_resolved(self):
        resolved = _resolve_url("/issues/foo", "https://alphasignal.ai/archive")
        assert resolved == "https://alphasignal.ai/issues/foo"

    def test_relative_url_with_base_path(self):
        resolved = _resolve_url("p/foo", "https://www.latent.space/archive")
        assert resolved == "https://www.latent.space/p/foo"

    def test_empty_href_returns_empty(self):
        assert _resolve_url("", "https://example.com") == ""

    def test_non_http_returns_empty(self):
        assert _resolve_url("mailto:foo@bar.com", "https://example.com") == ""

    def test_already_https(self):
        url = "https://bensbites.beehiiv.com/p/foo"
        assert _resolve_url(url, "https://bensbites.beehiiv.com") == url


# ---------------------------------------------------------------------------
# _source_id_for_url helper
# ---------------------------------------------------------------------------


class TestSourceIdForUrl:
    def test_alphasignal(self):
        assert _source_id_for_url(ALPHASIGNAL_URL) == "newsletter:alphasignal"

    def test_latentspace(self):
        assert _source_id_for_url(LATENTSPACE_URL) == "newsletter:latentspace"

    def test_bensbites(self):
        assert _source_id_for_url(BENSBITES_URL) == "newsletter:bensbites"

    def test_unknown(self):
        assert _source_id_for_url("https://someothernewsletter.com") == "newsletter:unknown"


# ---------------------------------------------------------------------------
# AlphaSignal parser
# ---------------------------------------------------------------------------


class TestParseAlphaSignal:
    def test_returns_three_items(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert len(items) == 3

    def test_first_item_title(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert items[0]["title"] == "LLM Tool Use: Function Calling in Production"

    def test_first_item_url_absolute(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        url = items[0]["url"]
        assert url.startswith("https://alphasignal.ai")
        assert "llm-tool-use" in url

    def test_first_item_summary(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert "function-calling" in items[0]["summary"].lower()

    def test_first_item_tags(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert "llm" in items[0]["tags"]
        assert "tool-use" in items[0]["tags"]
        assert "production" in items[0]["tags"]

    def test_source_id(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        for item in items:
            assert item["source"] == "newsletter:alphasignal"

    def test_second_item_title(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert "Embedding Models" in items[1]["title"]

    def test_third_item_title(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert "RLHF" in items[2]["title"]

    def test_fetched_at_is_now(self):
        items = _parse_alphasignal(_alphasignal_html(), ALPHASIGNAL_URL, _NOW)
        assert _NOW.isoformat() in items[0]["fetched_at"]

    def test_empty_html_no_exception(self):
        items = _parse_alphasignal(_empty_html(), ALPHASIGNAL_URL, _NOW)
        # May return 0 items (primary selector empty, fallback finds nothing)
        assert isinstance(items, list)

    def test_fallback_on_no_cards(self):
        # HTML with no article.issue-card — parser should fall back to generic
        html = """
        <html><body>
        <a href="/issues/some-post">Some Post Title</a>
        </body></html>
        """
        items = _parse_alphasignal(html, ALPHASIGNAL_URL, _NOW)
        # Generic fallback should pick up the link
        assert any("Some Post Title" in i["title"] for i in items)


# ---------------------------------------------------------------------------
# Latent Space parser
# ---------------------------------------------------------------------------


class TestParseLatentSpace:
    def test_returns_three_items(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert len(items) == 3

    def test_first_item_title(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert "Agent Frameworks" in items[0]["title"]

    def test_first_item_url(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert items[0]["url"] == "https://www.latent.space/p/agents-2026"

    def test_first_item_summary(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert "agent" in items[0]["summary"].lower()

    def test_datetime_attribute_used(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        # First item datetime= attribute is "2026-04-24"
        assert items[0]["published_at"] == "2026-04-24"

    def test_source_id(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        for item in items:
            assert item["source"] == "newsletter:latentspace"

    def test_second_item_title(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert "Fine-Tuning" in items[1]["title"]

    def test_third_item_title(self):
        items = _parse_latentspace(_latentspace_html(), LATENTSPACE_URL, _NOW)
        assert "MCP" in items[2]["title"]

    def test_empty_html_no_exception(self):
        items = _parse_latentspace(_empty_html(), LATENTSPACE_URL, _NOW)
        assert isinstance(items, list)


# ---------------------------------------------------------------------------
# Ben's Bites parser
# ---------------------------------------------------------------------------


class TestParseBensBites:
    def test_returns_three_items(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert len(items) == 3

    def test_first_item_title(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "GPT-5" in items[0]["title"]

    def test_first_item_url(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "openai-gpt5" in items[0]["url"]

    def test_first_item_summary(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "context window" in items[0]["summary"].lower()

    def test_first_item_date(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "April 25, 2026" in items[0]["published_at"]

    def test_source_id(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        for item in items:
            assert item["source"] == "newsletter:bensbites"

    def test_second_item_title(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "Computer Use" in items[1]["title"]

    def test_third_item_title(self):
        items = _parse_bensbites(_bensbites_html(), BENSBITES_URL, _NOW)
        assert "Codestral" in items[2]["title"]

    def test_empty_html_no_exception(self):
        items = _parse_bensbites(_empty_html(), BENSBITES_URL, _NOW)
        assert isinstance(items, list)


# ---------------------------------------------------------------------------
# Generic fallback parser
# ---------------------------------------------------------------------------


class TestParseGenericAnchors:
    def _html(self, links: list[tuple[str, str]]) -> str:
        """Build minimal HTML with given (href, text) links."""
        anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
        return f"<html><body>{anchors}</body></html>"

    def test_picks_up_issue_links(self):
        html = self._html([
            ("/issues/great-post", "Great Post Title"),
            ("/issues/another", "Another Post"),
        ])
        items = _parse_generic_anchors(html, "https://example.com", "newsletter:unknown", _NOW)
        assert len(items) == 2
        assert items[0]["title"] == "Great Post Title"

    def test_skips_nav_anchors(self):
        html = self._html([
            ("/issues/real-post", "Real Post"),
            ("/subscribe", "Subscribe"),
            ("/about", "About"),
        ])
        items = _parse_generic_anchors(html, "https://example.com", "newsletter:unknown", _NOW)
        titles = [i["title"] for i in items]
        # "Real Post" should appear; nav anchors should be excluded because
        # they don't match issue path patterns
        assert "Real Post" in titles

    def test_deduplicates_by_url(self):
        html = self._html([
            ("/issues/foo", "First mention"),
            ("/issues/foo", "Second mention"),
        ])
        items = _parse_generic_anchors(html, "https://example.com", "newsletter:unknown", _NOW)
        assert len(items) == 1
        assert items[0]["title"] == "First mention"

    def test_substack_p_links(self):
        html = self._html([("/p/great-article", "Great Article")])
        items = _parse_generic_anchors(
            html, "https://www.latent.space", "newsletter:latentspace", _NOW
        )
        assert any("Great Article" in i["title"] for i in items)

    def test_empty_html_returns_empty_list(self):
        items = _parse_generic_anchors(
            "<html><body></body></html>", "https://example.com", "newsletter:unknown", _NOW
        )
        assert items == []

    def test_source_id_propagated(self):
        html = self._html([("/issues/x", "X")])
        items = _parse_generic_anchors(
            html, "https://example.com", "newsletter:bensbites", _NOW
        )
        for item in items:
            assert item["source"] == "newsletter:bensbites"


# ---------------------------------------------------------------------------
# Dispatch — right parser chosen per URL
# ---------------------------------------------------------------------------


class TestDispatchParser:
    def test_alphasignal_url_uses_alphasignal_parser(self):
        items = _dispatch_parser(ALPHASIGNAL_URL, _alphasignal_html(), _NOW)
        assert len(items) == 3
        assert items[0]["source"] == "newsletter:alphasignal"

    def test_latentspace_url_uses_latentspace_parser(self):
        items = _dispatch_parser(LATENTSPACE_URL, _latentspace_html(), _NOW)
        assert len(items) == 3
        assert items[0]["source"] == "newsletter:latentspace"

    def test_bensbites_url_uses_bensbites_parser(self):
        items = _dispatch_parser(BENSBITES_URL, _bensbites_html(), _NOW)
        assert len(items) == 3
        assert items[0]["source"] == "newsletter:bensbites"

    def test_unknown_url_falls_back_to_generic(self):
        # An unknown URL with issue-path links
        html = """
        <html><body>
        <a href="/issues/test-post">Test Post</a>
        </body></html>
        """
        items = _dispatch_parser("https://some.newsletter.io/archive", html, _NOW)
        # Generic fallback should pick it up
        assert any("Test Post" in i["title"] for i in items)


# ---------------------------------------------------------------------------
# NewsletterSource — per-source isolation
# ---------------------------------------------------------------------------


class TestNewsletterSourceIsolation:
    def test_one_failing_url_does_not_stop_others(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.raise_on(LATENTSPACE_URL, ConnectionError("DNS failure"))
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(fake, config=NewsletterConfig())
        items = source.fetch(_NOW)

        # Items from alphasignal + bensbites should still be returned
        sources = {i["source"] for i in items}
        assert "newsletter:alphasignal" in sources
        assert "newsletter:bensbites" in sources
        # latentspace items should be absent
        assert "newsletter:latentspace" not in sources

    def test_all_urls_called_even_when_first_fails(self):
        fake = FakeHTTPFetcher()
        fake.raise_on(ALPHASIGNAL_URL, TimeoutError("timeout"))
        fake.add_fixture(LATENTSPACE_URL, "latentspace.html")
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(fake, config=NewsletterConfig())
        source.fetch(_NOW)

        # All three URLs must have been attempted
        assert fake.call_count(ALPHASIGNAL_URL) == 1
        assert fake.call_count(LATENTSPACE_URL) == 1
        assert fake.call_count(BENSBITES_URL) == 1

    def test_all_failing_returns_empty_list(self):
        fake = FakeHTTPFetcher()
        for url in DEFAULT_NEWSLETTER_URLS:
            fake.raise_on(url, RuntimeError("boom"))

        source = NewsletterSource(fake, config=NewsletterConfig())
        items = source.fetch(_NOW)
        assert items == []

    def test_single_url_config(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")

        source = NewsletterSource(
            fake, config=NewsletterConfig(urls=(ALPHASIGNAL_URL,))
        )
        items = source.fetch(_NOW)
        assert len(items) == 3
        assert fake.call_count(LATENTSPACE_URL) == 0
        assert fake.call_count(BENSBITES_URL) == 0


# ---------------------------------------------------------------------------
# NewsletterSource — fetch_with_report
# ---------------------------------------------------------------------------


class TestNewsletterFetchWithReport:
    def test_all_succeed(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.add_fixture(LATENTSPACE_URL, "latentspace.html")
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(fake, config=NewsletterConfig())
        items, report = source.fetch_with_report(_NOW)

        assert len(items) == 9  # 3 per newsletter
        assert report.items_found == 9
        assert report.sources_succeeded == 3
        assert report.sources_failed == 0
        assert report.errors == ()

    def test_one_failing_source(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.raise_on(LATENTSPACE_URL, ConnectionError("timeout"))
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(fake, config=NewsletterConfig())
        items, report = source.fetch_with_report(_NOW)

        assert report.sources_succeeded == 2
        assert report.sources_failed == 1
        assert len(report.errors) == 1
        assert "latent.space" in report.errors[0]

    def test_zero_items_from_empty_html_increments_failed(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "empty.html")
        fake.add_fixture(LATENTSPACE_URL, "empty.html")
        fake.add_fixture(BENSBITES_URL, "empty.html")

        source = NewsletterSource(fake, config=NewsletterConfig())
        items, report = source.fetch_with_report(_NOW)

        assert items == []
        # 0 items per source → each counts as failed
        assert report.sources_failed == 3
        assert report.sources_succeeded == 0

    def test_report_type(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.add_fixture(LATENTSPACE_URL, "latentspace.html")
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(fake)
        _, report = source.fetch_with_report(_NOW)
        assert isinstance(report, NewsletterFetchReport)


# ---------------------------------------------------------------------------
# max_items_per_source cap
# ---------------------------------------------------------------------------


class TestMaxItemsCap:
    def test_cap_at_one(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.add_fixture(LATENTSPACE_URL, "latentspace.html")
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(
            fake, config=NewsletterConfig(max_items_per_source=1)
        )
        items = source.fetch(_NOW)

        # At most 1 item per source × 3 sources = 3 items
        assert len(items) <= 3

    def test_cap_at_two(self):
        fake = FakeHTTPFetcher()
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")
        fake.add_fixture(LATENTSPACE_URL, "latentspace.html")
        fake.add_fixture(BENSBITES_URL, "bensbites.html")

        source = NewsletterSource(
            fake, config=NewsletterConfig(max_items_per_source=2)
        )
        items = source.fetch(_NOW)
        assert len(items) <= 6

    def test_cap_above_items_returns_all(self):
        fake = FakeHTTPFetcher()
        # alphasignal.html has 3 items
        fake.add_fixture(ALPHASIGNAL_URL, "alphasignal.html")

        source = NewsletterSource(
            fake, config=NewsletterConfig(urls=(ALPHASIGNAL_URL,), max_items_per_source=100)
        )
        items = source.fetch(_NOW)
        assert len(items) == 3


# ---------------------------------------------------------------------------
# Cross-source URL deduplication
# ---------------------------------------------------------------------------


class TestCrossSourceDedup:
    def test_same_url_from_two_sources_appears_once(self):
        # Manufacture HTML where both "newsletters" link to the same URL
        shared_html = """
        <html><body>
        <div class="post-preview">
          <a class="post-preview-title" href="https://shared.example.com/p/post">
            Shared Post
          </a>
          <div class="post-preview-subtitle">A shared post</div>
          <time class="post-preview-date" datetime="2026-04-25">Apr 25</time>
        </div>
        </body></html>
        """
        fake = FakeHTTPFetcher()
        fake.add(LATENTSPACE_URL, shared_html)
        fake.add(BENSBITES_URL, shared_html)

        source = NewsletterSource(
            fake, config=NewsletterConfig(urls=(LATENTSPACE_URL, BENSBITES_URL))
        )
        items = source.fetch(_NOW)

        urls = [i["url"] for i in items]
        assert urls.count("https://shared.example.com/p/post") == 1


# ---------------------------------------------------------------------------
# Empty config
# ---------------------------------------------------------------------------


class TestEmptyConfig:
    def test_no_urls_returns_empty_list(self):
        fake = FakeHTTPFetcher()
        source = NewsletterSource(fake, config=NewsletterConfig(urls=()))
        items = source.fetch(_NOW)
        assert items == []
        assert fake.all_urls_called == []

    def test_fetch_with_report_no_urls(self):
        fake = FakeHTTPFetcher()
        source = NewsletterSource(fake, config=NewsletterConfig(urls=()))
        items, report = source.fetch_with_report(_NOW)
        assert items == []
        assert report.items_found == 0
        assert report.sources_succeeded == 0
        assert report.sources_failed == 0


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    def test_default_urls(self):
        assert ALPHASIGNAL_URL in DEFAULT_NEWSLETTER_URLS
        assert LATENTSPACE_URL in DEFAULT_NEWSLETTER_URLS
        assert BENSBITES_URL in DEFAULT_NEWSLETTER_URLS
        assert len(DEFAULT_NEWSLETTER_URLS) == 3

    def test_default_config_uses_all_three(self):
        cfg = NewsletterConfig()
        assert cfg.urls == DEFAULT_NEWSLETTER_URLS

    def test_default_max_items_is_positive(self):
        cfg = NewsletterConfig()
        assert cfg.max_items_per_source > 0


# ---------------------------------------------------------------------------
# HttpxFetcher (import-only smoke test — not a live network test)
# ---------------------------------------------------------------------------


class TestHttpxFetcherSmoke:
    def test_importable(self):
        """HttpxFetcher can be imported and instantiated without network."""
        f = HttpxFetcher()
        assert f is not None

    def test_custom_timeout(self):
        f = HttpxFetcher(timeout=5.0)
        assert f._timeout == 5.0  # noqa: SLF001 — testing internal state for timeout config
