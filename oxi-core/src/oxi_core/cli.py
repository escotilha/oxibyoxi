"""CLI entrypoint. Phase 1 will fill this in with subcommands.

For Phase 0, this exists only so `pip install -e oxi-core/` succeeds
and the `oxi` console script is discoverable.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("oxi 0.0.0 — scaffold only. See docs/PLAN.md for roadmap.")
        return 0
    print(f"oxi: command not implemented yet: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
