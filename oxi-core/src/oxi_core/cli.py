r"""CLI entrypoint — user-facing surface for the engine.

Commands:

    oxi                         print a banner
    oxi status                  task + event summary (plan tier shown first)
    oxi v3 tick [--times N]     run N dispatch/heartbeat/pr_watcher/auto_merge cycles
    oxi v3 status               alias for `status`
    oxi v3 kill [--reason R]    create the killswitch file
    oxi v3 unkill               remove the killswitch file
    oxi brief [--hours N]       print the markdown brief to stdout (or --write)
    oxi dashboard [--port]      start the localhost HTML dashboard

The CLI uses argparse — no external click/typer dependency.

Every command that reads engine state goes through the adapter and
the DB. Missing adapter → print a clear error and exit 2; don't
try to recover.

Real Claude dispatch is NOT wired here by default — \`oxi v3 tick\`
without a critic backend uses \`AlwaysRejectBackend\` as a safety
default (no PRs will be auto-merged by accident). Operators opt in
by configuring their adapter and passing a real backend via
scripted entry — the CLI does not yet expose that.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from . import __version__
from .adapter import (
    AdapterLoadError,
    AdapterNotRegisteredError,
    MultipleAdaptersError,
    get_active_adapter,
    load_adapter,
)
from .db import connect


def _print_err(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")


def _require_adapter() -> None:
    """Ensure an adapter is active, auto-loading one if possible.

    Resolution order:
    1. Already registered (explicit ``register_adapter()`` call).
    2. ``OXI_ADAPTER=module:ClassName`` env var.
    3. Installed ``oxi.adapters`` entry-points (exactly one must be present).

    Prints a clear error and exits 2 if no adapter can be found or if
    loading fails.
    """
    try:
        load_adapter()
    except MultipleAdaptersError as exc:
        _print_err(
            f"oxi: multiple adapters installed — pin one with "
            f"OXI_ADAPTER=module:ClassName.\n  {exc}"
        )
        raise SystemExit(2) from exc
    except AdapterLoadError as exc:
        _print_err(f"oxi: adapter load failed.\n  {exc}")
        raise SystemExit(2) from exc

    try:
        get_active_adapter()
    except AdapterNotRegisteredError as exc:
        _print_err(
            "oxi: no adapter registered. Install an adapter package that "
            "declares [project.entry-points.\"oxi.adapters\"] in its "
            "pyproject.toml, or set OXI_ADAPTER=module:ClassName.\n"
            "  See adapters/_reference for an example.\n  " + str(exc)
        )
        raise SystemExit(2) from exc


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    _require_adapter()
    adapter = get_active_adapter()

    print(f"oxi {__version__}")
    print(f"  instance:  {adapter.naming().instance_name}")
    print(f"  plan tier: {adapter.plan_tier()}")
    print(f"  repo:      {adapter.github_repo()}")
    print()

    handle = connect()
    try:
        conn = handle.connection

        # Budget first — operators need to see hard-stop before anything else.
        from .v3 import budget as budget_mod
        status = budget_mod.check(conn)
        marker = {
            budget_mod.Verdict.OK: "  ",
            budget_mod.Verdict.WARN: "⚠ ",
            budget_mod.Verdict.HARD_STOP: "✗ ",
        }[status.verdict]
        print(
            f"{marker}budget (today): "
            f"${status.today_spend_usd:.2f} spent / "
            f"${status.daily_soft_warn_usd:.2f} warn / "
            f"${status.daily_hard_cap_usd:.2f} hard — {status.verdict.value}"
        )
        print()

        # Status histogram.
        counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM task GROUP BY status"
            )
        }
        print("task counts by status:")
        if counts:
            for status_name in sorted(counts):
                print(f"  {status_name:<12} {counts[status_name]}")
        else:
            print("  (no tasks)")

        # Recent events (last 10).
        print()
        print("recent events:")
        events = list(
            conn.execute(
                "SELECT created_at, kind, task_id FROM event "
                "ORDER BY id DESC LIMIT 10"
            )
        )
        if not events:
            print("  (none)")
        else:
            for e in events:
                print(f"  {e['created_at']}  {e['kind']:<25} task#{e['task_id']}")
    finally:
        handle.connection.close()
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    _require_adapter()
    from .v3.brief import generate

    handle = connect()
    try:
        brief = generate(handle.connection, window_hours=args.hours)
    finally:
        handle.connection.close()

    text = brief.render()
    if args.write:
        adapter = get_active_adapter()
        brief_path = adapter.paths().brief_path
        if brief_path is None:
            # Fallback: under oxi_dir.
            repo_root = adapter.paths().repo_root or "."
            brief_path = str(Path(repo_root) / adapter.paths().oxi_dir / "brief.md")
        Path(brief_path).parent.mkdir(parents=True, exist_ok=True)
        Path(brief_path).write_text(text)
        print(f"oxi: brief written to {brief_path}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    _require_adapter()
    from .v3.kill import create

    p = create(reason=args.reason or "")
    print(f"oxi: killswitch set at {p}")
    return 0


def cmd_unkill(args: argparse.Namespace) -> int:  # noqa: ARG001
    _require_adapter()
    from .v3.kill import path, remove

    removed = remove()
    p = path()
    if removed:
        print(f"oxi: killswitch cleared at {p}")
    else:
        print(f"oxi: killswitch not present at {p}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    _require_adapter()
    adapter = get_active_adapter()
    from .v3 import kill as kill_mod
    from .v3.engine_state import EngineState

    state = EngineState(
        plan_tier=adapter.plan_tier(),
        max_concurrent=max(
            (h.max_concurrent for h in adapter.dispatch_hosts()), default=1
        ),
    )
    if kill_mod.is_set():
        state.request_stop()
        print("oxi: killswitch is set; tick is a no-op")
        return 0

    # Two modes:
    # - Default (reconciliation-only): runs heartbeat.reap, no Claude.
    # - --real-claude: additionally runs dispatch + auto_merge with a
    #   ClaudeCriticBackend. Spends real budget. Off by default.
    if args.real_claude:
        print(
            f"oxi: tick --times {args.times} (REAL CLAUDE — "
            "dispatches + critic-gated auto_merge)"
        )
    else:
        print(f"oxi: tick --times {args.times} (reconciliation-only)")

    from .v3 import heartbeat

    handle = connect()
    try:
        total_abandoned = 0
        for _ in range(args.times):
            if state.is_stopping():
                break
            report = heartbeat.reap(handle.connection, state)
            total_abandoned += report.abandoned
            if report.abandoned or report.considered:
                print(
                    f"  heartbeat: considered={report.considered} "
                    f"abandoned={report.abandoned} "
                    f"protected_by_pr={report.protected_by_pr} "
                    f"fresh={report.skipped_fresh}"
                )
            if args.real_claude:
                _run_real_claude_tick(handle.connection, state, adapter)
        print(f"oxi: tick done. abandoned={total_abandoned}")
    finally:
        handle.connection.close()
    return 0


def _run_real_claude_tick(conn, state, adapter) -> None:
    """One iteration of dispatch → pr_watcher → auto_merge with real claude.

    Split out of ``cmd_tick`` so tests can exercise the reconciliation
    path without importing the async dispatch machinery. Only called
    when ``--real-claude`` is passed.
    """
    import asyncio

    from .v3 import auto_merge, dispatch, pr_watcher
    from .v3.critic import ClaudeCriticBackend

    repo_root_str = adapter.paths().repo_root or "."
    repo_root = Path(repo_root_str)

    # Read ANTHROPIC_API_KEY from the supervisor's env and hand it
    # explicitly to dispatch. The dispatch_invoke env whitelist would
    # otherwise strip it, leaving workers with no credentials. This is
    # the one secret we intentionally allow across the boundary.
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

    result = asyncio.run(
        dispatch.dispatch_one(
            conn=conn,
            adapter=adapter,
            engine_state=state,
            repo_root=repo_root,
            anthropic_api_key=anthropic_api_key,
        )
    )
    if result is not None:
        print(
            f"  dispatch: classification={result.classification.value} "
            f"cost=${result.cost_usd:.4f}"
        )

    watch_report = pr_watcher.watch(conn, state)
    if (watch_report.pr_numbers_stamped
            or watch_report.tasks_transitioned_merged
            or watch_report.tasks_transitioned_failed):
        print(
            f"  pr_watcher: stamped={watch_report.pr_numbers_stamped} "
            f"merged={watch_report.tasks_transitioned_merged} "
            f"failed={watch_report.tasks_transitioned_failed}"
        )

    critic = ClaudeCriticBackend(cwd=repo_root)
    merge_report = auto_merge.run(conn, state, critic)
    if merge_report.considered:
        print(
            f"  auto_merge: considered={merge_report.considered} "
            f"merged={merge_report.merged} "
            f"rejected={merge_report.critic_rejected}"
        )


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a new adapter. Does NOT require an active adapter."""
    from .wizard import run as wizard_run

    # If the operator didn't provide a destination, ask the wizard for
    # answers first so we can use the slug in the default directory.
    if args.destination is None:
        from .wizard import collect_answers, default_template_root, scaffold

        answers = collect_answers()
        dest = Path.cwd() / f"oxi-adapter-{answers.adapter_slug}"
        scaffold(
            answers, dest,
            template_root=default_template_root(),
            force=args.force,
        )
        print()
        print(f"oxi init: scaffolded into {dest}")
        print()
        print("Next steps:")
        print(f"  1. cd {dest}")
        print("  2. pip install -e .")
        print("  3. oxi status   # confirms the adapter loads")
        return 0

    wizard_run(args.destination, force=args.force)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    _require_adapter()
    adapter = get_active_adapter()
    from .v3.dashboard import DashboardConfig, serve

    db_path = adapter.paths().db_path
    if db_path is None:
        from .db import default_db_path
        db_path = str(default_db_path())

    config = DashboardConfig(host=args.host, port=args.port, window_hours=args.hours)
    url = f"http://{args.host}:{args.port}/"
    print(f"oxi: dashboard at {url} (Ctrl-C to stop)")
    try:
        serve(db_path=db_path, config=config)
    except KeyboardInterrupt:
        print("\noxi: dashboard stopped")
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oxi",
        description="Forkable autonomous coding orchestrator.",
    )
    parser.add_argument(
        "--version", action="version", version=f"oxi {__version__}"
    )
    sub = parser.add_subparsers(dest="command")

    # `oxi init` — scaffold a new adapter.
    p_init = sub.add_parser(
        "init",
        help="scaffold a new oxi adapter for your project",
    )
    p_init.add_argument(
        "destination", type=Path, nargs="?", default=None,
        help="where to write the adapter package "
             "(default: ./oxi-adapter-<slug>)",
    )
    p_init.add_argument(
        "--force", action="store_true",
        help="overwrite existing files in destination",
    )
    p_init.set_defaults(func=cmd_init)

    # `oxi status` (top-level alias for v3 status)
    p_status = sub.add_parser("status", help="print task + event summary")
    p_status.set_defaults(func=cmd_status)

    # `oxi brief`
    p_brief = sub.add_parser("brief", help="print the markdown brief")
    p_brief.add_argument("--hours", type=int, default=24, help="window in hours")
    p_brief.add_argument(
        "--write", action="store_true",
        help="write to adapter.paths().brief_path instead of stdout",
    )
    p_brief.set_defaults(func=cmd_brief)

    # `oxi v3 ...`
    p_v3 = sub.add_parser("v3", help="engine commands")
    p_v3_sub = p_v3.add_subparsers(dest="v3_command")

    p_v3_status = p_v3_sub.add_parser("status", help="alias for `oxi status`")
    p_v3_status.set_defaults(func=cmd_status)

    p_v3_tick = p_v3_sub.add_parser("tick", help="run one or more engine cycles")
    p_v3_tick.add_argument("--times", type=int, default=1, help="iterations")
    p_v3_tick.add_argument(
        "--real-claude", action="store_true",
        help="invoke real claude for dispatch + critic (spends budget). "
             "Off by default.",
    )
    p_v3_tick.set_defaults(func=cmd_tick)

    p_v3_kill = p_v3_sub.add_parser("kill", help="create the killswitch file")
    p_v3_kill.add_argument("--reason", default="", help="reason string")
    p_v3_kill.set_defaults(func=cmd_kill)

    p_v3_unkill = p_v3_sub.add_parser("unkill", help="remove the killswitch file")
    p_v3_unkill.set_defaults(func=cmd_unkill)

    # `oxi dashboard`
    p_dashboard = sub.add_parser("dashboard", help="start the localhost HTML dashboard")
    p_dashboard.add_argument("--host", default="127.0.0.1")
    p_dashboard.add_argument("--port", type=int, default=8765)
    p_dashboard.add_argument("--hours", type=int, default=24)
    p_dashboard.set_defaults(func=cmd_dashboard)

    return parser


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = _build_parser()

    if not argv:
        print(f"oxi {__version__}")
        print("Run `oxi --help` for the command list.")
        return 0

    # `oxi v3` without a subcommand: show v3 help.
    if argv == ["v3"]:
        parser.parse_args(["v3", "--help"])
        return 0

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2

    # Some commands are async — none yet, but the branch is here.
    result = func(args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
