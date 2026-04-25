"""auto_external — daily external-signal scanner for the auto-improve subsystem.

This package is the T1-B series implementation.  The public entry point is
:func:`scan`, which the Routine entry point ``scripts/auto_improve_routine.py``
calls once per day.

Pipeline (phased delivery — each phase ships as its own PR):

    B2  GitHub source fetcher
    B3  Newsletter source fetcher
    B4  X source fetcher (via skill)
    B5  Ranking pipeline (BM25 + sqlite-vec + RRF k=60)
    B6  LLM judge (Haiku 4.5, rubric, fabricated-module filter)
    B7  Three-layer dedup (identifier / semantic / temporal)
    B8  Emit step (ledger events + Markdown digest)
    B9  Health monitor (acceptance-ratio auto-pause)

B10 (this PR) ships the entry-point scaffolding: :func:`scan` with the
idempotency guard, the Routine config, and the CI smoke test.  The full
pipeline stubs are imported but return empty results until later PRs
replace them.

Hard guardrails (lint-enforced):
  - This package MUST NOT import ``oxi_core.v3.dispatch``,
    ``oxi_core.v3.dispatch_invoke``, ``oxi_core.v3.dispatch_pool``, or
    ``oxi_core.v3.auto_merge``.
  - ``engine_state.is_stopping()`` is checked at entry and between stages.
  - ``budget.check()`` is called before any LLM invocation.
"""

from __future__ import annotations

from .scan import (
    AutoImproveAlreadyRanError,
    ScanReport,
    scan,
    scan_already_ran_today,
)

__all__ = [
    "AutoImproveAlreadyRanError",
    "ScanReport",
    "scan",
    "scan_already_ran_today",
]
