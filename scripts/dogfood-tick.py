#!/usr/bin/env python3
"""Register the self-adapter and run one engine tick against the oxi repo.

This is the dogfood entry point. Equivalent to `oxi v3 tick` after the
self-adapter is registered, but wraps both in one invocation so operators
don't have to remember the registration step.

Usage:
    scripts/dogfood-tick.py                 # reconciliation-only
    scripts/dogfood-tick.py --real-claude   # spend budget, dispatch for real
    scripts/dogfood-tick.py --times 3       # three engine ticks

Kept out of the package on purpose — this is an internal operator tool,
not part of the public CLI surface forks inherit.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from oxi_adapter_self import SelfAdapter
from oxi_core import db, planner
from oxi_core.adapter import register_adapter
from oxi_core.cli import cmd_tick


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--times", type=int, default=1)
    p.add_argument("--real-claude", action="store_true", dest="real_claude")
    p.add_argument(
        "--skip-plan", action="store_true",
        help="skip the roadmap → task seeder (useful for re-runs)",
    )
    args = p.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    adapter = SelfAdapter(repo_root=repo_root)
    register_adapter(adapter)

    # Make sure .oxi/ exists so the DB, worktree root, etc. have a home.
    (repo_root / ".oxi").mkdir(exist_ok=True)
    (repo_root / ".oxi" / "worktrees").mkdir(exist_ok=True)

    if not args.skip_plan:
        handle = db.connect()
        try:
            added = planner.plan(repo_root, handle.connection)
            print(f"dogfood-tick: planner seeded {added} new task(s)")
        finally:
            handle.connection.close()

    return cmd_tick(args)


if __name__ == "__main__":
    sys.exit(main())
