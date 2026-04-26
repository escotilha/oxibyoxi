"""Ranking pipeline — keyword + vector hybrid search (no LLM).

This module implements a four-stage retrieval pipeline used to rank
corpus documents (roadmap items, changelog entries, merged PR titles)
against a free-text query.  The design is deliberately LLM-free so it
runs cheaply on every tick without API spend.

Pipeline stages
---------------
1. **prefilter** — SQLite FTS5 full-text search narrows the corpus to
   keyword-relevant candidates.  Returns at most ``top_n`` rows
   (default 50) with their raw BM25 scores.

2. **bm25_score** — Runs SQLite FTS5's built-in ``bm25()`` function on
   the full corpus against the query.  The scores are negated so higher
   is better (FTS5 returns negative scores natively).

3. **vector_rerank** — Embeds the query and all candidate documents
   with ``all-MiniLM-L6-v2`` (384-dim, fast, already a project dep).
   Returns cosine-similarity scores.

4. **rrf_combine** — Reciprocal Rank Fusion (k=60, matching the project
   memory rule for hybrid retrieval).  Merges the BM25 rank list and
   the vector rank list into a single final ranking.

Corpus building
---------------
``build_corpus`` assembles the full document set from three sources:

- ``roadmap.md`` — each ``**T<tier>-<n> · <title>**`` item becomes a
  document (title + optional subtitle).
- ``CHANGELOG.md`` — each release section becomes a document.
- Last-90-day merged PRs — queried via
  ``GitHubClient.list_merged_prs(since=now-90d)``.  The PR title is
  the document body.

The FTS5 virtual table is built in-memory (``sqlite3.connect(':memory:')``)
on demand and discarded after the ranking call.  No on-disk state.

Usage
-----
.. code-block:: python

    from oxi_core.v3.ranking import build_corpus, rank

    corpus = build_corpus(
        roadmap_text=roadmap_md,
        changelog_text=changelog_md,
        merged_prs=[("feat: add greet", 42), ...],
    )
    results = rank("ranking pipeline bm25 hybrid", corpus, top_n=15)
    for doc_id, score in results:
        print(doc_id, score)

Design notes
------------
- ``prefilter`` and ``bm25_score`` share the same in-memory FTS5
  connection to avoid double-parsing.  Callers can pass an existing
  connection via the ``_conn`` keyword argument (used by tests for
  isolation).
- ``vector_rerank`` loads the model lazily the first time it's called
  and caches it in a module-level singleton so repeated calls within
  one process are cheap.
- ``rrf_combine`` is a pure function — no I/O, no state.  Tests can
  call it directly with synthetic ranked lists.
- The sqlite-vec extension is *not* required — we compute cosine
  similarity in NumPy.  sqlite-vec is an optional extension that
  requires extension-loading support compiled into the sqlite3 binary;
  the standard macOS Python does not have it.  Pure-NumPy cosine is
  equivalent and more portable.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from sentence_transformers import SentenceTransformer

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sentence-transformers model name (already a dep; 384-dim).
_MODEL_NAME = "all-MiniLM-L6-v2"

#: RRF smoothing constant.  k=60 is the standard value recommended in
#: the Cormack & Clarke 2009 paper and matches the project memory rule.
RRF_K: int = 60

#: FTS5 tokenizer — porter stemmer + ASCII folding.
_FTS5_TOKENIZER = "porter ascii"

# ---------------------------------------------------------------------------
# Module-level model singleton
# ---------------------------------------------------------------------------

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Return the cached model, loading it on first call."""
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    """A single corpus document.

    Attributes
    ----------
    doc_id:
        Stable string identifier (e.g. ``"roadmap:T1-B5"``,
        ``"changelog:v0.1.0b1"``, ``"pr:42"``).
    title:
        Short label used as the primary FTS and embedding target.
    body:
        Longer text (subtitle, description, PR body snippet).  Used to
        enrich the embedding.  May be empty.
    source:
        One of ``"roadmap"``, ``"changelog"``, ``"pr"``.
    """

    doc_id: str
    title: str
    body: str = ""
    source: str = "unknown"


@dataclass
class RankedDoc:
    """A document with its combined RRF score."""

    doc: Document
    score: float
    bm25_rank: int | None = field(default=None)
    vector_rank: int | None = field(default=None)


# ---------------------------------------------------------------------------
# FTS5 connection helper
# ---------------------------------------------------------------------------


def _build_fts_conn(docs: list[Document]) -> sqlite3.Connection:
    """Build an in-memory FTS5 table from *docs* and return the connection.

    The returned connection must be closed by the caller.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        f"CREATE VIRTUAL TABLE docs USING fts5("
        f"doc_id UNINDEXED, title, body, "
        f"tokenize='{_FTS5_TOKENIZER}')"
    )
    conn.executemany(
        "INSERT INTO docs VALUES (?, ?, ?)",
        [(d.doc_id, d.title, d.body) for d in docs],
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Stage 1 — prefilter
# ---------------------------------------------------------------------------


def prefilter(
    query: str,
    docs: list[Document],
    *,
    top_n: int = 50,
    _conn: sqlite3.Connection | None = None,
) -> list[tuple[str, float]]:
    """FTS5 keyword pre-filter: return up to *top_n* (doc_id, bm25) pairs.

    The query is sanitised to remove FTS5 special characters before
    passing to ``MATCH`` so callers don't need to escape manually.

    Parameters
    ----------
    query:
        Free-text search string.
    docs:
        Full corpus as returned by ``build_corpus``.
    top_n:
        Maximum number of results to return (default 50).
    _conn:
        Optional pre-built FTS5 connection (used by tests and by
        ``bm25_score`` to share a single in-memory DB).  When ``None``,
        a new connection is created and closed before returning.

    Returns
    -------
    list[tuple[str, float]]
        List of ``(doc_id, bm25_score)`` pairs, sorted by descending
        BM25 score (i.e. best matches first; BM25 nativley returns
        negative values so we negate them here).
    """
    safe_query = _sanitise_fts_query(query)
    if not safe_query:
        # No usable tokens — return all docs with uniform score 0.
        return [(d.doc_id, 0.0) for d in docs[:top_n]]

    own_conn = _conn is None
    conn = _build_fts_conn(docs) if own_conn else _conn

    try:
        rows = conn.execute(
            "SELECT doc_id, bm25(docs) AS score "
            "FROM docs WHERE docs MATCH ? "
            "ORDER BY bm25(docs) "  # ascending = most negative = best match
            "LIMIT ?",
            (safe_query, top_n),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 can still fail on pathological inputs (e.g. bare wildcards).
        rows = []
    finally:
        if own_conn:
            conn.close()

    # Negate scores so higher is better.
    return [(row[0], -row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Stage 2 — bm25_score
# ---------------------------------------------------------------------------


def bm25_score(
    query: str,
    docs: list[Document],
    *,
    _conn: sqlite3.Connection | None = None,
) -> dict[str, float]:
    """Return a BM25 score for *every* doc in the corpus.

    Docs that don't match the query receive score 0.  The returned
    mapping is keyed on ``doc_id`` and has a value for every document
    (not just matches) so ``rrf_combine`` can rank non-matching docs at
    the tail of the BM25 rank list.

    Parameters
    ----------
    query:
        Free-text search string.
    docs:
        Full corpus.
    _conn:
        Optional pre-built FTS5 connection.

    Returns
    -------
    dict[str, float]
        ``{doc_id: bm25_score}`` for all docs.  Higher is better.
        Non-matching docs have score 0.
    """
    # Initialise all scores to 0.
    scores: dict[str, float] = {d.doc_id: 0.0 for d in docs}

    safe_query = _sanitise_fts_query(query)
    if not safe_query:
        return scores

    own_conn = _conn is None
    conn = _build_fts_conn(docs) if own_conn else _conn

    try:
        rows = conn.execute(
            "SELECT doc_id, bm25(docs) AS score "
            "FROM docs WHERE docs MATCH ?",
            (safe_query,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        if own_conn:
            conn.close()

    for doc_id, raw_score in rows:
        # FTS5 BM25 is negative; negate so higher is better.
        scores[doc_id] = -raw_score

    return scores


# ---------------------------------------------------------------------------
# Stage 3 — vector_rerank
# ---------------------------------------------------------------------------


def vector_rerank(
    query: str,
    docs: list[Document],
    *,
    model: SentenceTransformer | None = None,
) -> dict[str, float]:
    """Embed query + docs with all-MiniLM-L6-v2; return cosine similarity.

    Parameters
    ----------
    query:
        Free-text search string.
    docs:
        Full corpus.
    model:
        Optional pre-loaded SentenceTransformer (used by tests to
        inject a stub and avoid network/disk I/O).  When ``None``,
        the module-level singleton is used.

    Returns
    -------
    dict[str, float]
        ``{doc_id: cosine_similarity}`` for all docs.  Range: [-1, 1].
        Higher is better.
    """
    if not docs:
        return {}

    _model = model if model is not None else _get_model()

    # Build the texts to embed: title + body.
    texts = [f"{d.title} {d.body}".strip() for d in docs]

    # Encode all at once (batched internally by sentence-transformers).
    all_texts = [query] + texts
    embeddings = _model.encode(all_texts, convert_to_numpy=True, show_progress_bar=False)

    query_emb = embeddings[0]
    doc_embs = embeddings[1:]

    # Cosine similarity = dot(q, d) / (|q| * |d|).
    q_norm = np.linalg.norm(query_emb)
    if q_norm == 0.0:
        return {d.doc_id: 0.0 for d in docs}

    similarities: dict[str, float] = {}
    for doc, emb in zip(docs, doc_embs, strict=True):
        d_norm = np.linalg.norm(emb)
        if d_norm == 0.0:
            similarities[doc.doc_id] = 0.0
        else:
            sim = float(np.dot(query_emb, emb) / (q_norm * d_norm))
            similarities[doc.doc_id] = sim

    return similarities


# ---------------------------------------------------------------------------
# Stage 4 — rrf_combine
# ---------------------------------------------------------------------------


def rrf_combine(
    bm25_scores: dict[str, float],
    vector_scores: dict[str, float],
    *,
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """Merge BM25 and vector rankings with Reciprocal Rank Fusion.

    RRF formula: ``score(d) = Σ 1 / (k + rank(d))`` where the sum is
    over the two rankers and ``rank`` is 1-based.

    Documents that appear in only one ranker are assigned the tail rank
    in the other ranker (``len(all_docs) + 1``).  This is the standard
    behaviour for RRF with partial coverage.

    Parameters
    ----------
    bm25_scores:
        ``{doc_id: bm25_score}`` from ``bm25_score()``.  Higher is better.
    vector_scores:
        ``{doc_id: cosine_similarity}`` from ``vector_rerank()``.
        Higher is better.
    k:
        RRF smoothing constant.  Default 60 (project memory rule).

    Returns
    -------
    list[tuple[str, float]]
        ``[(doc_id, rrf_score)]`` sorted by descending RRF score.
    """
    # Gather all document IDs from both rankers.
    all_ids = set(bm25_scores) | set(vector_scores)
    tail_rank = len(all_ids) + 1

    # Rank each list (descending score → ascending rank, 1-based).
    bm25_rank = _score_to_rank(bm25_scores, all_ids, tail_rank)
    vec_rank = _score_to_rank(vector_scores, all_ids, tail_rank)

    # Combine via RRF.
    rrf: dict[str, float] = {}
    for doc_id in all_ids:
        rrf[doc_id] = 1.0 / (k + bm25_rank[doc_id]) + 1.0 / (k + vec_rank[doc_id])

    return sorted(rrf.items(), key=lambda x: x[1], reverse=True)


def _score_to_rank(
    scores: dict[str, float],
    all_ids: set[str],
    tail_rank: int,
) -> dict[str, int]:
    """Convert score dict to 1-based rank dict over *all_ids*.

    Docs present in *scores* are ranked by descending score.
    Docs absent from *scores* get *tail_rank*.
    Ties are broken by doc_id string for determinism.
    """
    # Sort present docs: higher score = better rank = lower number.
    present = sorted(
        [(doc_id, score) for doc_id, score in scores.items() if doc_id in all_ids],
        key=lambda x: (-x[1], x[0]),
    )
    rank: dict[str, int] = {}
    for i, (doc_id, _) in enumerate(present):
        rank[doc_id] = i + 1
    # Absent docs get tail rank.
    for doc_id in all_ids:
        if doc_id not in rank:
            rank[doc_id] = tail_rank
    return rank


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------


def rank(
    query: str,
    docs: list[Document],
    *,
    top_n: int = 15,
    model: SentenceTransformer | None = None,
) -> list[RankedDoc]:
    """Run the full ranking pipeline and return the top-*top_n* results.

    Pipeline: prefilter → bm25_score → vector_rerank → rrf_combine.

    The prefilter stage narrows to at most ``top_n * 3`` candidates
    (or all docs if fewer) before computing embeddings, keeping the
    vector stage cheap even for large corpora.

    Parameters
    ----------
    query:
        Free-text search string.
    docs:
        Full corpus as returned by ``build_corpus``.
    top_n:
        Number of results to return (default 15).
    model:
        Optional pre-loaded SentenceTransformer (for testing).

    Returns
    -------
    list[RankedDoc]
        Top-*top_n* documents with their combined RRF score and
        individual BM25/vector ranks.  Sorted by descending RRF score.
    """
    if not docs:
        return []

    # Shared FTS5 connection for prefilter + bm25_score.
    conn = _build_fts_conn(docs)

    try:
        # Stage 1: prefilter — narrow candidates.
        prefilter_top_n = min(max(top_n * 3, 50), len(docs))
        prefilter_results = prefilter(query, docs, top_n=prefilter_top_n, _conn=conn)
        candidate_ids = {doc_id for doc_id, _ in prefilter_results}

        # If prefilter returns nothing (no keyword match), use all docs.
        if not candidate_ids:
            candidates = docs
        else:
            candidates = [d for d in docs if d.doc_id in candidate_ids]

        # Stage 2: BM25 scores for candidates.
        bm25 = bm25_score(query, candidates, _conn=conn)

    finally:
        conn.close()

    # Stage 3: vector similarity for candidates.
    vec = vector_rerank(query, candidates, model=model)

    # Stage 4: RRF combine.
    combined = rrf_combine(bm25, vec)

    # Build result list.
    doc_index = {d.doc_id: d for d in candidates}

    # Reconstruct ranks for metadata.
    bm25_order = sorted(bm25, key=lambda x: -bm25[x])
    vec_order = sorted(vec, key=lambda x: -vec[x])
    bm25_ranks = {doc_id: i + 1 for i, doc_id in enumerate(bm25_order)}
    vec_ranks = {doc_id: i + 1 for i, doc_id in enumerate(vec_order)}

    results: list[RankedDoc] = []
    for doc_id, rrf_score in combined[:top_n]:
        if doc_id in doc_index:
            results.append(
                RankedDoc(
                    doc=doc_index[doc_id],
                    score=rrf_score,
                    bm25_rank=bm25_ranks.get(doc_id),
                    vector_rank=vec_ranks.get(doc_id),
                )
            )

    return results


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------


# Pattern: **T1-5 · title text**
_ROADMAP_ITEM = re.compile(
    r"^\*\*(T\d+-\d+)\s*[··•]\s*(.+?)\*\*\s*$",
    re.MULTILINE,
)

# Pattern: _subtitle text_  (immediately follows a roadmap item)
_ROADMAP_SUBTITLE = re.compile(r"^_(.+?)_\s*$")

# Pattern: ## [version] — date  or  ## [Unreleased]
_CHANGELOG_SECTION = re.compile(
    r"^##\s+\[([^\]]+)\]",
    re.MULTILINE,
)


def _parse_roadmap(text: str) -> list[Document]:
    """Extract Document objects from roadmap.md text."""
    docs: list[Document] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _ROADMAP_ITEM.match(line.strip())
        if m:
            identifier = m.group(1)
            title = m.group(2).strip()
            # Look ahead for a subtitle line.
            subtitle = ""
            if i + 1 < len(lines):
                sub_m = _ROADMAP_SUBTITLE.match(lines[i + 1].strip())
                if sub_m:
                    subtitle = sub_m.group(1).strip()
                    i += 1
            docs.append(
                Document(
                    doc_id=f"roadmap:{identifier}",
                    title=f"{identifier} {title}",
                    body=subtitle,
                    source="roadmap",
                )
            )
        i += 1
    return docs


def _parse_changelog(text: str) -> list[Document]:
    """Extract one Document per release section from CHANGELOG.md text."""
    docs: list[Document] = []
    lines = text.splitlines()
    sections: list[tuple[str, int]] = []  # (version, line_index)

    for i, line in enumerate(lines):
        m = _CHANGELOG_SECTION.match(line)
        if m:
            sections.append((m.group(1), i))

    for j, (version, start) in enumerate(sections):
        end = sections[j + 1][1] if j + 1 < len(sections) else len(lines)
        body_lines = [ln for ln in lines[start + 1 : end] if ln.strip()]
        body = " ".join(body_lines[:5])  # first 5 non-blank lines as body
        docs.append(
            Document(
                doc_id=f"changelog:{version}",
                title=f"release {version}",
                body=body,
                source="changelog",
            )
        )

    return docs


def build_corpus(
    *,
    roadmap_text: str = "",
    changelog_text: str = "",
    merged_prs: list[tuple[str, int]] | None = None,
) -> list[Document]:
    """Build the ranking corpus from the three standard sources.

    Parameters
    ----------
    roadmap_text:
        Content of ``roadmap.md``.
    changelog_text:
        Content of ``CHANGELOG.md``.
    merged_prs:
        List of ``(title, pr_number)`` tuples for recently merged PRs.
        Typically the last-90-days output from
        ``GitHubClient.list_merged_prs(since=now-90d)``.

    Returns
    -------
    list[Document]
        Deduplicated corpus ready for ``rank()``.
    """
    docs: list[Document] = []

    if roadmap_text:
        docs.extend(_parse_roadmap(roadmap_text))

    if changelog_text:
        docs.extend(_parse_changelog(changelog_text))

    if merged_prs:
        for title, pr_number in merged_prs:
            docs.append(
                Document(
                    doc_id=f"pr:{pr_number}",
                    title=title,
                    body="",
                    source="pr",
                )
            )

    return docs


# ---------------------------------------------------------------------------
# FTS query sanitiser
# ---------------------------------------------------------------------------

# Characters that have special meaning in FTS5 queries and can cause
# OperationalError if passed raw.
_FTS5_SPECIAL = re.compile(r'["\(\)\^\*\-\+\:]')


def _sanitise_fts_query(query: str) -> str:
    """Strip FTS5 special characters and return a safe MATCH string.

    Splits on whitespace, strips each token, drops empties. Returns
    an empty string if no usable tokens remain (callers must handle
    the empty case).
    """
    # Remove special chars.
    cleaned = _FTS5_SPECIAL.sub(" ", query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    # Join with OR so partial-query matches work (more permissive than AND).
    return " OR ".join(tokens)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "Document",
    "RankedDoc",
    "RRF_K",
    "bm25_score",
    "build_corpus",
    "prefilter",
    "rank",
    "rrf_combine",
    "vector_rerank",
]
