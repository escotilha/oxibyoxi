"""Three-layer deduplication for external proposals — T1-B7.

Every candidate that survives the ranking + judge stages passes through
three dedup layers before it can be emitted as a proposal.  A candidate
that fails any layer is dropped and a
``EXTERNAL_PROPOSAL_DEDUP_SKIPPED`` event is written to the ledger.

Layers (applied in order)
--------------------------
1. **Identifier dedup** (:func:`dedup_identifier`) — assigns a stable
   monotonic proposal counter by querying
   ``EXTERNAL_PROPOSAL_EMITTED`` events already in the ledger.  This
   layer never drops a candidate; it assigns the next available integer
   counter that will be used as the ``T<tier>-A<n>`` identifier suffix.

2. **Semantic dedup** (:func:`dedup_semantic`) — computes cosine
   similarity between the candidate's ``title`` + ``subtitle`` text and
   the text of every open ``front`` row and every ``task`` row updated in
   the last 30 days.  If the maximum similarity exceeds
   ``SEMANTIC_THRESHOLD`` (0.85), the candidate is dropped.  The
   similarity is computed with a pure-Python dot-product over TF-IDF-like
   bag-of-word vectors (no external model dependency) so the layer works
   on any platform without optional dependencies.

3. **Temporal dedup** (:func:`dedup_temporal`) — drops a candidate whose
   ``(signal_kind, target_identifier)`` pair already appears in an
   ``EXTERNAL_PROPOSAL_EMITTED`` event within the last
   ``TEMPORAL_WINDOW_DAYS`` (14) days.  This prevents the same repo or
   signal from being re-proposed within a 14-day cooldown window.

Pipeline integration
--------------------
The top-level :func:`run_dedup` function applies all three layers in
order and returns the list of surviving candidates.  It is called by the
``_run_dedup`` stub in :mod:`oxi_core.v3.auto_external.scan` and
replaces that stub.

Candidate shape
---------------
Each candidate is a ``dict`` with at minimum:

.. code-block:: python

    {
        "signal_kind":        str,   # e.g. "github_release"
        "target_identifier":  str,   # e.g. "anthropic/anthropic-sdk-python"
        "title":              str,   # proposed roadmap item title
        "subtitle":           str,   # proposed roadmap item subtitle (may be empty)
    }

Additional keys (from the ranking / judge stages) are preserved
unchanged.

Emitted events
--------------
``EXTERNAL_PROPOSAL_DEDUP_SKIPPED`` — one per dropped candidate.

Payload schema::

    {
        "signal_kind":       str,
        "target_identifier": str,
        "layer":             "identifier" | "semantic" | "temporal",
        "reason":            str,        # human-readable explanation
        "similarity":        float | None,  # semantic layer only
    }

Design notes
------------
- **No LLM calls** — all three layers use only DB queries + local maths.
- **No new DB tables** — reads from existing ``event``, ``front``, and
  ``task`` tables; writes only ``event`` rows.
- **Synchronous** — mirrors the discipline of ``auto_observe``,
  ``auto_recover``, etc.
- **Injectable now** — ``now`` parameter allows deterministic tests.
- **Pure-Python similarity** — avoids ``sentence-transformers`` or
  ``sqlite-vec`` at this layer; the ranking stage (B5) handles heavy
  embeddings.  Here we use a normalised bag-of-words cosine that is
  "good enough" for a dedup gate.
- **LedgerEvent constants** — all event kinds use
  :class:`oxi_core.v3.ledger_events.LedgerEvent` rather than bare
  strings.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta

from ..ledger_events import LedgerEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Cosine-similarity threshold for semantic dedup.  Candidates with
#: similarity ≥ this value against any existing front / task are dropped.
SEMANTIC_THRESHOLD: float = 0.85

#: Rolling window in days for temporal dedup.
TEMPORAL_WINDOW_DAYS: int = 14

#: Rolling window in days for the task "recently updated" query in semantic dedup.
SEMANTIC_TASK_WINDOW_DAYS: int = 30

# ---------------------------------------------------------------------------
# Layer 1: Identifier dedup
# ---------------------------------------------------------------------------


def dedup_identifier(
    conn: sqlite3.Connection,
) -> int:
    """Return the next monotonic proposal counter from the ledger.

    Queries all ``EXTERNAL_PROPOSAL_EMITTED`` events and extracts the
    ``counter`` field from each payload.  Returns ``max(counters) + 1``,
    or ``1`` if no events exist yet.

    This function never drops a candidate — it is a counter-assignment
    helper called once per :func:`run_dedup` invocation.

    Arguments:
        conn: SQLite connection.

    Returns:
        int — the next monotonic counter value (≥ 1).

    Examples:
        .. code-block:: python

            counter = dedup_identifier(conn)
            # counter == 1 on a fresh DB
    """
    rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ?",
        (LedgerEvent.EXTERNAL_PROPOSAL_EMITTED,),
    ).fetchall()

    if not rows:
        return 1

    max_counter = 0
    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
            c = payload.get("counter")
            if isinstance(c, int) and c > max_counter:
                max_counter = c
        except (TypeError, ValueError):
            pass

    return max_counter + 1


# ---------------------------------------------------------------------------
# Layer 2: Semantic dedup
# ---------------------------------------------------------------------------


def _tokenise(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace.

    Returns a list of word tokens suitable for bag-of-words comparison.
    """
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return [tok for tok in text.split() if tok]


def _bow_vector(tokens: list[str]) -> dict[str, float]:
    """Return a length-normalised bag-of-words vector.

    Each dimension is a word; its value is the term frequency divided by
    the L2 norm of the raw count vector.  Returns an empty dict for empty
    input (representing the zero vector).
    """
    if not tokens:
        return {}
    counts = Counter(tokens)
    norm = math.sqrt(sum(v * v for v in counts.values()))
    if norm == 0.0:
        return {}
    return {term: count / norm for term, count in counts.items()}


def _cosine(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Cosine similarity between two normalised bag-of-words vectors.

    Both vectors must already be L2-normalised (i.e. produced by
    :func:`_bow_vector`).  Returns a value in [0.0, 1.0].
    """
    if not vec_a or not vec_b:
        return 0.0
    dot = 0.0
    # Iterate over the smaller vector for efficiency.
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    for term, val in vec_a.items():
        if term in vec_b:
            dot += val * vec_b[term]
    return dot


def _candidate_vector(candidate: dict) -> dict[str, float]:
    """Build a normalised BOW vector from a candidate's title + subtitle."""
    text = (candidate.get("title", "") + " " + candidate.get("subtitle", "")).strip()
    return _bow_vector(_tokenise(text))


def _corpus_vectors(
    conn: sqlite3.Connection,
    *,
    task_window_days: int = SEMANTIC_TASK_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, float]]]:
    """Return (source_label, bow_vector) pairs for the semantic corpus.

    Corpus consists of:
    - All active ``front`` rows (``active = 1``), using ``title`` +
      ``subtitle`` or ``description`` as the text.
    - All ``task`` rows whose ``updated_at`` is within the last
      ``task_window_days`` days.

    Arguments:
        conn: SQLite connection.
        task_window_days: how far back to look for recently-updated tasks.
        now: wall-clock override for tests.

    Returns:
        List of ``(label, vector)`` pairs where ``label`` is a human-readable
        description for logging and ``vector`` is the normalised BOW dict.
    """
    now_dt = now if now is not None else datetime.now(tz=UTC)
    cutoff = (now_dt - timedelta(days=task_window_days)).strftime("%Y-%m-%d %H:%M:%S")

    corpus: list[tuple[str, dict[str, float]]] = []

    # --- open front rows -----------------------------------------------
    try:
        front_rows = conn.execute(
            "SELECT COALESCE(title, ''), COALESCE(subtitle, ''), "
            "COALESCE(description, ''), COALESCE(identifier, name, '') "
            "FROM front WHERE active = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        # Front table may lack 'title'/'subtitle' columns on older schemas.
        front_rows = conn.execute(
            "SELECT '', '', COALESCE(description, ''), COALESCE(name, '') "
            "FROM front WHERE active = 1"
        ).fetchall()

    for title, subtitle, description, label in front_rows:
        text = (title + " " + subtitle + " " + description).strip()
        vec = _bow_vector(_tokenise(text))
        if vec:
            corpus.append((f"front:{label}", vec))

    # --- recently-updated task rows ------------------------------------
    try:
        task_rows = conn.execute(
            "SELECT COALESCE(title, ''), COALESCE(subtitle, ''), "
            "COALESCE(identifier, '') "
            "FROM task WHERE updated_at >= ?",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        task_rows = []

    for title, subtitle, label in task_rows:
        text = (title + " " + subtitle).strip()
        vec = _bow_vector(_tokenise(text))
        if vec:
            corpus.append((f"task:{label}", vec))

    return corpus


def dedup_semantic(
    candidate: dict,
    conn: sqlite3.Connection,
    *,
    threshold: float = SEMANTIC_THRESHOLD,
    task_window_days: int = SEMANTIC_TASK_WINDOW_DAYS,
    now: datetime | None = None,
) -> tuple[bool, float]:
    """Check whether a candidate is semantically too close to existing items.

    Computes cosine similarity between the candidate's title+subtitle and
    every item in the semantic corpus (open fronts + recently-updated
    tasks).  If the maximum similarity ≥ ``threshold``, the candidate is
    considered a duplicate.

    Arguments:
        candidate: candidate dict with at least ``"title"`` and
            ``"subtitle"`` keys.
        conn: SQLite connection.
        threshold: cosine-similarity cutoff (default 0.85).
        task_window_days: how far back to look for recently-updated tasks.
        now: wall-clock override for tests.

    Returns:
        ``(is_duplicate, max_similarity)`` where ``is_duplicate`` is True
        if the candidate should be dropped.
    """
    candidate_vec = _candidate_vector(candidate)
    if not candidate_vec:
        # Empty candidate text — can't be a semantic duplicate.
        return False, 0.0

    corpus = _corpus_vectors(conn, task_window_days=task_window_days, now=now)
    if not corpus:
        return False, 0.0

    max_sim = 0.0
    for label, corpus_vec in corpus:
        sim = _cosine(candidate_vec, corpus_vec)
        if sim > max_sim:
            max_sim = sim
            if max_sim >= threshold:
                logger.debug(
                    "dedup_semantic: candidate %r matches %r (sim=%.3f ≥ %.2f)",
                    candidate.get("target_identifier", "?"),
                    label,
                    max_sim,
                    threshold,
                )
                # Early exit — no need to check further.
                break

    return max_sim >= threshold, max_sim


# ---------------------------------------------------------------------------
# Layer 3: Temporal dedup
# ---------------------------------------------------------------------------


def dedup_temporal(
    candidate: dict,
    conn: sqlite3.Connection,
    *,
    window_days: int = TEMPORAL_WINDOW_DAYS,
    now: datetime | None = None,
) -> bool:
    """Return True if the candidate's (signal_kind, target_identifier) was
    recently emitted.

    Queries ``EXTERNAL_PROPOSAL_EMITTED`` events within the last
    ``window_days`` days.  If any event's payload matches both
    ``signal_kind`` and ``target_identifier``, the candidate is a temporal
    duplicate.

    Arguments:
        candidate: candidate dict with ``"signal_kind"`` and
            ``"target_identifier"`` keys.
        conn: SQLite connection.
        window_days: rolling window in days (default 14).
        now: wall-clock override for tests.

    Returns:
        True if the candidate should be dropped (temporal duplicate).
    """
    now_dt = now if now is not None else datetime.now(tz=UTC)
    cutoff = (now_dt - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M:%S")

    signal_kind = candidate.get("signal_kind", "")
    target_identifier = candidate.get("target_identifier", "")

    if not signal_kind and not target_identifier:
        # Neither field present — can't determine temporal duplicate.
        return False

    rows = conn.execute(
        "SELECT payload FROM event WHERE kind = ? AND created_at >= ?",
        (LedgerEvent.EXTERNAL_PROPOSAL_EMITTED, cutoff),
    ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            continue

        if (
            payload.get("signal_kind") == signal_kind
            and payload.get("target_identifier") == target_identifier
        ):
            logger.debug(
                "dedup_temporal: candidate (signal_kind=%r, target=%r) "
                "already emitted within %d days",
                signal_kind,
                target_identifier,
                window_days,
            )
            return True

    return False


# ---------------------------------------------------------------------------
# Ledger event emission
# ---------------------------------------------------------------------------


def _emit_dedup_skipped(
    conn: sqlite3.Connection,
    candidate: dict,
    layer: str,
    reason: str,
    *,
    similarity: float | None = None,
) -> None:
    """Write an ``EXTERNAL_PROPOSAL_DEDUP_SKIPPED`` event to the ledger.

    Arguments:
        conn: SQLite connection.
        candidate: the dropped candidate dict.
        layer: one of ``"identifier"``, ``"semantic"``, ``"temporal"``.
        reason: human-readable explanation of why the candidate was dropped.
        similarity: cosine similarity (semantic layer only; None otherwise).
    """
    payload: dict = {
        "signal_kind": candidate.get("signal_kind", ""),
        "target_identifier": candidate.get("target_identifier", ""),
        "layer": layer,
        "reason": reason,
    }
    if similarity is not None:
        payload["similarity"] = round(similarity, 4)

    with conn:
        conn.execute(
            "INSERT INTO event (task_id, kind, payload) VALUES (NULL, ?, ?)",
            (LedgerEvent.EXTERNAL_PROPOSAL_DEDUP_SKIPPED, json.dumps(payload)),
        )


# ---------------------------------------------------------------------------
# Public pipeline entry point
# ---------------------------------------------------------------------------


def run_dedup(
    candidates: list[dict],
    conn: sqlite3.Connection,
    *,
    semantic_threshold: float = SEMANTIC_THRESHOLD,
    temporal_window_days: int = TEMPORAL_WINDOW_DAYS,
    task_window_days: int = SEMANTIC_TASK_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """Apply the three-layer dedup pipeline to a list of candidates.

    Applies layers in order:

    1. ``dedup_identifier`` — assigns a monotonic counter.
    2. ``dedup_semantic`` — drops semantically-similar candidates.
    3. ``dedup_temporal`` — drops recently-emitted ``(signal_kind, target)`` pairs.

    Each surviving candidate is annotated with ``"_proposal_counter"``
    (from layer 1) for use by the emit stage (B8).

    Dropped candidates cause one ``EXTERNAL_PROPOSAL_DEDUP_SKIPPED``
    event to be written to the ledger.

    Arguments:
        candidates: list of candidate dicts from the judge stage.
        conn: SQLite connection with foreign keys enabled.
        semantic_threshold: cosine cutoff (default 0.85).
        temporal_window_days: temporal dedup window (default 14 days).
        task_window_days: task corpus window for semantic dedup (default 30 days).
        now: wall-clock override for tests.

    Returns:
        List of surviving candidates (order preserved), each augmented
        with ``"_proposal_counter": int``.
    """
    if not candidates:
        return []

    # --- Layer 1: assign next counter ------------------------------------
    next_counter = dedup_identifier(conn)
    logger.debug("dedup: next_counter=%d, candidates=%d", next_counter, len(candidates))

    surviving: list[dict] = []

    for candidate in candidates:
        signal_kind = candidate.get("signal_kind", "")
        target = candidate.get("target_identifier", "")

        # --- Layer 2: semantic -------------------------------------------
        is_semantic_dup, max_sim = dedup_semantic(
            candidate,
            conn,
            threshold=semantic_threshold,
            task_window_days=task_window_days,
            now=now,
        )
        if is_semantic_dup:
            reason = (
                f"cosine similarity {max_sim:.3f} ≥ threshold {semantic_threshold:.2f} "
                f"against existing front/task"
            )
            logger.info(
                "dedup: SKIP semantic — signal_kind=%r target=%r sim=%.3f",
                signal_kind,
                target,
                max_sim,
            )
            _emit_dedup_skipped(
                conn, candidate, "semantic", reason, similarity=max_sim
            )
            continue

        # --- Layer 3: temporal -------------------------------------------
        is_temporal_dup = dedup_temporal(
            candidate,
            conn,
            window_days=temporal_window_days,
            now=now,
        )
        if is_temporal_dup:
            reason = (
                f"(signal_kind={signal_kind!r}, target_identifier={target!r}) "
                f"already emitted within {temporal_window_days} days"
            )
            logger.info(
                "dedup: SKIP temporal — signal_kind=%r target=%r",
                signal_kind,
                target,
            )
            _emit_dedup_skipped(conn, candidate, "temporal", reason)
            continue

        # --- Survived all layers — assign counter -----------------------
        annotated = dict(candidate)
        annotated["_proposal_counter"] = next_counter
        next_counter += 1
        surviving.append(annotated)

    logger.info(
        "dedup: %d/%d candidates survived (dropped %d)",
        len(surviving),
        len(candidates),
        len(candidates) - len(surviving),
    )
    return surviving


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "SEMANTIC_THRESHOLD",
    "TEMPORAL_WINDOW_DAYS",
    "SEMANTIC_TASK_WINDOW_DAYS",
    "dedup_identifier",
    "dedup_semantic",
    "dedup_temporal",
    "run_dedup",
]
