"""FakeHTTPFetcher — deterministic HTTP stub for newsletter source tests.

Lives under ``tests/fixtures/`` so it's test-only infrastructure.  The
fake lets tests pre-configure URL → HTML body mappings and verify that
:class:`oxi_core.v3.auto_external.sources.newsletter.NewsletterSource`
calls the correct URLs and returns correctly-parsed items — all without
any real network activity.

Usage::

    from tests.fixtures.fake_http import FakeHTTPFetcher

    fake = FakeHTTPFetcher()
    fake.add("https://example.com/archive", "<html>...</html>")

    source = NewsletterSource(fake, config=NewsletterConfig())
    items = source.fetch(datetime.now(tz=UTC))

Error simulation::

    fake.raise_on("https://bad.example.com/archive", ConnectionError("timeout"))

After the call::

    assert fake.call_count("https://example.com/archive") == 1
    assert fake.all_urls_called == ["https://example.com/archive"]
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Canned HTML fixture helper
# ---------------------------------------------------------------------------

#: Path to the ``tests/fixtures/data/newsletters/`` directory, resolved
#: relative to this file so imports work regardless of cwd.
_FIXTURES_DATA_DIR = Path(__file__).parent / "data" / "newsletters"


def load_fixture(name: str) -> str:
    """Return the content of a canned HTML fixture file.

    Parameters
    ----------
    name:
        Fixture filename (e.g. ``"alphasignal.html"``).  Must exist in
        ``tests/fixtures/data/newsletters/``.

    Raises
    ------
    FileNotFoundError
        If the fixture file doesn't exist.
    """
    path = _FIXTURES_DATA_DIR / name
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# FakeHTTPFetcher
# ---------------------------------------------------------------------------


class FakeHTTPFetcher:
    """In-memory :class:`~oxi_core.v3.auto_external.sources.newsletter.HTTPFetcher`.

    Pre-load responses with :meth:`add` (or :func:`load_fixture`).
    Optionally configure errors with :meth:`raise_on`.  Tracks which URLs
    were called and how many times via :meth:`call_count` /
    :attr:`all_urls_called`.

    Parameters
    ----------
    responses:
        Optional initial mapping of URL → HTML string.  Equivalent to
        calling :meth:`add` for each entry after construction.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self._responses: dict[str, str] = dict(responses or {})
        self._errors: dict[str, Exception] = {}
        self._calls: list[str] = []

    # ---- Test setup helpers --------------------------------------------------

    def add(self, url: str, html: str) -> None:
        """Register a successful HTML response for *url*."""
        self._responses[url] = html

    def add_fixture(self, url: str, fixture_name: str) -> None:
        """Register the content of a fixture file as the response for *url*.

        Parameters
        ----------
        url:
            URL to associate with the fixture.
        fixture_name:
            Filename in ``tests/fixtures/data/newsletters/``
            (e.g. ``"alphasignal.html"``).
        """
        self._responses[url] = load_fixture(fixture_name)

    def raise_on(self, url: str, exc: Exception) -> None:
        """Configure *url* to raise *exc* when fetched.

        This simulates network errors, HTTP 4xx/5xx, timeouts, etc.
        """
        self._errors[url] = exc

    # ---- Call tracking -------------------------------------------------------

    @property
    def all_urls_called(self) -> list[str]:
        """Ordered list of all URLs that :meth:`get` was called with."""
        return list(self._calls)

    def call_count(self, url: str) -> int:
        """Return how many times *url* was fetched."""
        return self._calls.count(url)

    def reset(self) -> None:
        """Clear call history (but not responses or error config)."""
        self._calls.clear()

    # ---- HTTPFetcher protocol ------------------------------------------------

    def get(self, url: str) -> str:
        """Return the pre-configured HTML body for *url*.

        Raises
        ------
        Exception
            If configured via :meth:`raise_on`.
        KeyError
            If no response or error is configured for *url* — this
            surfaces misconfigured tests early rather than silently
            returning empty HTML.
        """
        self._calls.append(url)

        if url in self._errors:
            raise self._errors[url]

        if url in self._responses:
            return self._responses[url]

        raise KeyError(
            f"FakeHTTPFetcher: no response configured for {url!r}. "
            f"Call fake.add(url, html) or fake.add_fixture(url, filename) in your test."
        )
