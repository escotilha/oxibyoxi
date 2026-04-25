r"""CLI entrypoint — user-facing surface for the engine.

Commands:

    oxi                         print a banner
    oxi status [--json]         task + event summary (plan tier shown first)
    oxi v3 tick [--times N]     run N dispatch/heartbeat/pr_watcher/auto_merge cycles
    oxi v3 status [--json]      alias for `status`
    oxi v3 plan --dry-run       parse the roadmap and print what was found (no DB write)
    oxi v3 kill [--reason R] [-y]   create the killswitch file (prompts unless -y)
    oxi v3 unkill [-y]              remove the killswitch file (prompts unless -y)
    oxi v3 heal                     reset engine-unhealthy state after consecutive failures
    oxi v3 observe                  list pending auto-observation proposals
    oxi v3 observe accept <id>      queue a proposal for dispatch (closes self-improvement loop)
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
import json
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

    handle = connect()
    try:
        conn = handle.connection

        # --json: emit stable machine-readable payload and exit.
        if getattr(args, "json", False):
            from .v3.status_json import build as build_status_json
            payload = build_status_json(conn, adapter)
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            return 0

        # Human-readable output.
        from .v3 import kill as kill_mod
        from .v3._glyphs import FAIL, PAUSED

        ks_active = kill_mod.is_set()
        ks_path = kill_mod.path()

        print(f"oxi {__version__}")
        print(f"  instance:  {adapter.naming().instance_name}")
        print(f"  plan tier: {adapter.plan_tier()}")
        print(f"  repo:      {adapter.github_repo()}")
        if ks_active:
            ks_reason = ks_path.read_text().strip()
            reason_suffix = f" — {ks_reason}" if ks_reason else ""
            # Killswitched engine → PAUSED glyph from the standard
            # vocabulary (matches dashboard + brief).
            print(f"  killswitch: {PAUSED} ACTIVE{reason_suffix}")
            print(f"              {ks_path}")
        else:
            print("  killswitch: off")
        print()

        # Engine health — show at the very top if unhealthy so operators see
        # it immediately.  Healthy engines show nothing extra here.
        from .v3.engine_health import is_engine_unhealthy_from_db
        if is_engine_unhealthy_from_db(conn):
            print(
                f"{FAIL} ENGINE UNHEALTHY — dispatch paused after "
                "consecutive failures. Run `oxi v3 heal` to resume."
            )
            print()

        # Budget first — operators need to see hard-stop before anything else.
        # WARN keeps ⚠ (a warning marker, distinct from task-state glyphs);
        # HARD_STOP uses the FAIL glyph from the standard vocabulary.
        from .v3 import budget as budget_mod
        status = budget_mod.check(conn)
        marker = {
            budget_mod.Verdict.OK: "  ",
            budget_mod.Verdict.WARN: "⚠ ",
            budget_mod.Verdict.HARD_STOP: f"{FAIL} ",
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


def _confirm(prompt: str, *, yes: bool) -> bool:
    """Return True if the operator confirms the action.

    If ``yes`` is True (``-y`` / ``--yes`` flag), skip the prompt and
    return True immediately — suitable for non-interactive scripts.

    On a non-TTY stdin the prompt is shown but the read will succeed or
    raise ``EOFError``; we treat EOF as "no" to fail-safe.
    """
    if yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def cmd_kill(args: argparse.Namespace) -> int:
    _require_adapter()
    from .v3 import kill as kill_mod

    ks_path = kill_mod.path()
    already_active = kill_mod.is_set(ks_path)

    if already_active:
        existing_reason = ks_path.read_text().strip()
        reason_suffix = f" (reason: {existing_reason})" if existing_reason else ""
        print(f"oxi: killswitch is already active at {ks_path}{reason_suffix}")
    else:
        print(f"oxi: this will halt the engine by creating {ks_path}")

    reason_str = args.reason or ""
    if reason_str:
        print(f"     reason: {reason_str}")

    if not _confirm("Set killswitch?", yes=getattr(args, "yes", False)):
        print("oxi: aborted.")
        return 1

    p = kill_mod.create(reason=reason_str)
    print(f"oxi: killswitch set at {p}")
    return 0


def cmd_unkill(args: argparse.Namespace) -> int:
    _require_adapter()
    from .v3 import kill as kill_mod

    ks_path = kill_mod.path()

    if not kill_mod.is_set(ks_path):
        print(f"oxi: killswitch not present at {ks_path}")
        return 0

    existing_reason = ks_path.read_text().strip()
    reason_suffix = f" (reason: {existing_reason})" if existing_reason else ""
    print(f"oxi: this will resume the engine by removing {ks_path}{reason_suffix}")

    if not _confirm("Clear killswitch?", yes=getattr(args, "yes", False)):
        print("oxi: aborted.")
        return 1

    kill_mod.remove(ks_path)
    print(f"oxi: killswitch cleared at {ks_path}")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    """List or accept auto-observation proposals.

    The auto_observe subsystem watches the ledger for repeated patterns
    (dispatch failures, critic rejections, worktree drift) and emits
    proposed roadmap items when a pattern crosses threshold. Operators
    review proposals here.

    Subcommands:
      oxi v3 observe                  list all pending proposals
      oxi v3 observe accept <id>      accept a proposal — injects a
                                      front row so the next tick's
                                      seed_from_roadmap promotes it to
                                      a planned task. The engine then
                                      dispatches against it on the
                                      following --real-claude tick.
                                      This closes the auto-improvement
                                      loop: engine observes → operator
                                      approves → engine fixes itself.
    """
    _require_adapter()
    from .v3 import auto_observe

    handle = connect()
    try:
        conn = handle.connection
        if args.observe_action == "accept":
            ok = auto_observe.accept(conn, args.proposal_id)
            if ok:
                print(
                    f"oxi: accepted proposal #{args.proposal_id}. "
                    "Next reconciliation tick will seed it as a planned task."
                )
                return 0
            print(
                f"oxi: proposal #{args.proposal_id} not found "
                "or already accepted."
            )
            return 1

        # Default action: list.
        proposals = auto_observe.list_proposals(conn)
        if not proposals:
            print("oxi: no pending observation proposals.")
            return 0
        print(f"oxi: {len(proposals)} pending proposal(s):")
        print()
        for p in proposals:
            ts = p.created_at.split(".")[0] if "." in p.created_at else p.created_at
            print(f"  #{p.event_id}  [{p.signal_kind}]  observed×{p.count}")
            print(f"    target  : {p.target_identifier}")
            print(f"    title   : {p.title}")
            if p.subtitle:
                print(f"    subtitle: {p.subtitle}")
            print(f"    seen at : {ts}")
            print()
        print("Accept one with: oxi v3 observe accept <id>")
        return 0
    finally:
        handle.connection.close()


def cmd_heal(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Reset the engine-unhealthy state written by the auto-healing subsystem.

    When N consecutive dispatches fail, the engine transitions to
    UNHEALTHY and pauses dispatch to prevent burning the daily budget
    during unattended runs.  ``oxi v3 heal`` clears that state so the
    engine will accept new dispatches again on the next tick.

    This writes an ``engine_healed`` event to the DB so the dashboard
    and the brief both reflect that a human cleared the condition.
    """
    _require_adapter()
    from .v3.engine_health import (
        EngineHealth,
        HealthStatus,
        is_engine_unhealthy_from_db,
    )

    handle = connect()
    try:
        conn = handle.connection
        was_unhealthy = is_engine_unhealthy_from_db(conn)

        if was_unhealthy:
            # Write the engine_healed DB event.  We set the in-memory
            # status to UNHEALTHY first so heal() detects a real
            # transition and emits the event.
            health = EngineHealth()
            health._status = HealthStatus.UNHEALTHY  # noqa: SLF001
            health.heal(conn, operator="oxi v3 heal")
            print(
                "oxi: engine-unhealthy state cleared. "
                "Dispatch will resume on next tick."
            )
        else:
            print("oxi: engine is already healthy — nothing to do.")
    finally:
        handle.connection.close()
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    _require_adapter()
    adapter = get_active_adapter()
    from .v3 import kill as kill_mod
    from .v3._color import green, red, yellow
    from .v3.engine_health import EngineHealth
    from .v3.engine_state import EngineState

    state = EngineState(
        plan_tier=adapter.plan_tier(),
        max_concurrent=max(
            (h.max_concurrent for h in adapter.dispatch_hosts()), default=1
        ),
    )
    health = EngineHealth()

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

    from .v3 import auto_recover, heartbeat

    handle = connect()
    try:
        total_abandoned = 0
        total_recovered = 0
        for _ in range(args.times):
            if state.is_stopping():
                break
            if health.is_unhealthy():
                print(
                    "  health: engine UNHEALTHY — dispatch paused. "
                    "Run `oxi v3 heal` to resume."
                )
                break
            report = heartbeat.reap(handle.connection, state)
            total_abandoned += report.abandoned
            if report.abandoned or report.considered:
                heartbeat_line = (
                    f"  heartbeat: considered={report.considered} "
                    f"abandoned={report.abandoned} "
                    f"protected_by_pr={report.protected_by_pr} "
                    f"fresh={report.skipped_fresh}"
                )
                # Yellow when tasks were abandoned (stuck); plain otherwise.
                if report.abandoned:
                    heartbeat_line = yellow(heartbeat_line)
                print(heartbeat_line)

            rec_report = auto_recover.run(handle.connection, state)
            total_recovered += rec_report.recovered
            if rec_report.recovered or rec_report.exhausted:
                recover_line = (
                    f"  auto_recover: recovered={rec_report.recovered} "
                    f"exhausted={rec_report.exhausted} "
                    f"skipped_cooldown={rec_report.skipped_cooldown}"
                )
                # Red when all retries are exhausted; green when recovered.
                if rec_report.exhausted:
                    recover_line = red(recover_line)
                else:
                    recover_line = green(recover_line)
                print(recover_line)

            if args.real_claude:
                _run_real_claude_tick(handle.connection, state, health, adapter)

        summary = (
            f"oxi: tick done. abandoned={total_abandoned} "
            f"auto_recovered={total_recovered}"
        )
        # Color the summary line: red if anything was abandoned, green if
        # recoveries happened with no abandonments, plain otherwise.
        if total_abandoned:
            summary = yellow(summary)
        elif total_recovered:
            summary = green(summary)
        print(summary)
    finally:
        handle.connection.close()
    return 0


def _run_real_claude_tick(conn, state, health, adapter) -> None:
    """One iteration of dispatch → pr_watcher → auto_merge with real claude.

    Split out of ``cmd_tick`` so tests can exercise the reconciliation
    path without importing the async dispatch machinery. Only called
    when ``--real-claude`` is passed.
    """
    import asyncio

    from .v3 import auto_merge, dispatch, pr_watcher
    from .v3._color import green, red, yellow
    from .v3.critic import ClaudeCriticBackend
    from .v3.dispatch_invoke import Classification

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
            engine_health=health,
            repo_root=repo_root,
            anthropic_api_key=anthropic_api_key,
        )
    )
    if result is not None:
        dispatch_line = (
            f"  dispatch: classification={result.classification.value} "
            f"cost=${result.cost_usd:.4f}"
        )
        # Green on success, red on failure/timeout, yellow on transient.
        if result.classification == Classification.SUCCESS:
            dispatch_line = green(dispatch_line)
        elif result.classification == Classification.RETRYABLE_TRANSIENT:
            dispatch_line = yellow(dispatch_line)
        else:
            # FAILED or TIMEOUT
            dispatch_line = red(dispatch_line)
        print(dispatch_line)
        if health.is_unhealthy():
            print(
                "  health: engine UNHEALTHY after consecutive failures — "
                "dispatch paused. Run `oxi v3 heal` to resume."
            )

    watch_report = pr_watcher.watch(conn, state)
    if (watch_report.pr_numbers_stamped
            or watch_report.tasks_transitioned_merged
            or watch_report.tasks_transitioned_failed):
        watch_line = (
            f"  pr_watcher: stamped={watch_report.pr_numbers_stamped} "
            f"merged={watch_report.tasks_transitioned_merged} "
            f"failed={watch_report.tasks_transitioned_failed}"
        )
        # Red when PRs failed; green when merged; yellow otherwise (stamped open).
        if watch_report.tasks_transitioned_failed:
            watch_line = red(watch_line)
        elif watch_report.tasks_transitioned_merged:
            watch_line = green(watch_line)
        else:
            watch_line = yellow(watch_line)
        print(watch_line)

    critic = ClaudeCriticBackend(cwd=repo_root)
    merge_report = auto_merge.run(conn, state, critic)
    if merge_report.considered:
        merge_line = (
            f"  auto_merge: considered={merge_report.considered} "
            f"merged={merge_report.merged} "
            f"rejected={merge_report.critic_rejected}"
        )
        # Red when critic rejected; green when merged; yellow if only considered.
        if merge_report.critic_rejected:
            merge_line = red(merge_line)
        elif merge_report.merged:
            merge_line = green(merge_line)
        else:
            merge_line = yellow(merge_line)
        print(merge_line)


def cmd_plan(args: argparse.Namespace) -> int:
    """Parse the roadmap and report what was found.  Does NOT touch the DB.

    Designed for ``oxi v3 plan --dry-run``.  Operators writing their first
    roadmap often produce natural markdown (e.g. ``## T0-1 — title``) that
    the planner silently ignores.  This command surfaces the format pickiness
    before any seeding happens.

    Exit codes:
        0 — parse succeeded and at least one item was found
        1 — parse error or zero items found
        2 — missing adapter or missing roadmap file
    """
    _require_adapter()
    adapter = get_active_adapter()

    from .v3.plan_dry_run import dry_run, format_report

    paths = adapter.paths()
    repo_root = Path(paths.repo_root) if paths.repo_root else Path.cwd()
    roadmap_path = repo_root / adapter.roadmap_location()

    try:
        report = dry_run(roadmap_path)
    except FileNotFoundError as exc:
        _print_err(
            f"oxi: roadmap not found at {roadmap_path}\n"
            "  Check adapter.roadmap_location() and adapter.paths().repo_root."
        )
        raise SystemExit(2) from exc

    sys.stdout.write(format_report(report, roadmap_path) + "\n")

    if not report.ok or not report.items:
        return 1
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a new adapter. Does NOT require an active adapter."""
    from .wizard import run as wizard_run

    # If the operator didn't provide a destination, derive it from the
    # adapter slug they're about to type.  Two-pass flow: collect first
    # so we know the slug, then hand both to wizard_run which renders
    # the review screen and writes.
    yes = bool(getattr(args, "yes", False))
    if args.destination is None:
        from .wizard import collect_answers

        answers = collect_answers()
        dest = Path.cwd() / f"oxi-adapter-{answers.adapter_slug}"
        # Re-enter the wizard at the scaffold step using the answers
        # we already collected.  We can't pass them through cleanly
        # because run() owns the prompt loop — instead, call scaffold
        # directly and skip the review screen if --yes; otherwise show
        # the review and require confirmation here.
        from .wizard import _prompt_bool, _render_review, default_template_root, scaffold

        print(_render_review(answers, dest))
        if not yes:
            confirmed = _prompt_bool(
                "Proceed and write these files?", default=True,
            )
            if not confirmed:
                print("oxi init: aborted — no files written.")
                return 0
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

    result = wizard_run(args.destination, force=args.force, yes=yes)
    # wizard_run returns None if the operator declined the review.
    return 0 if result is not None else 0


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
    p_init.add_argument(
        "--yes", "-y", action="store_true",
        help="skip the review-and-confirm prompt (non-interactive scaffolding)",
    )
    p_init.set_defaults(func=cmd_init)

    # `oxi status` (top-level alias for v3 status)
    p_status = sub.add_parser("status", help="print task + event summary")
    p_status.add_argument(
        "--json", action="store_true",
        help="emit a stable JSON payload instead of human-readable output "
             "(schema documented in docs/status-json-schema.md)",
    )
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
    p_v3_status.add_argument(
        "--json", action="store_true",
        help="emit a stable JSON payload instead of human-readable output "
             "(schema documented in docs/status-json-schema.md)",
    )
    p_v3_status.set_defaults(func=cmd_status)

    p_v3_tick = p_v3_sub.add_parser("tick", help="run one or more engine cycles")
    p_v3_tick.add_argument("--times", type=int, default=1, help="iterations")
    p_v3_tick.add_argument(
        "--real-claude", action="store_true",
        help="invoke real claude for dispatch + critic (spends budget). "
             "Off by default.",
    )
    p_v3_tick.set_defaults(func=cmd_tick)

    p_v3_plan = p_v3_sub.add_parser(
        "plan",
        help="parse the roadmap and report what was found (no DB write)",
    )
    p_v3_plan.add_argument(
        "--dry-run", action="store_true", required=True,
        help="required flag — parse roadmap, print items, do not touch the DB",
    )
    p_v3_plan.set_defaults(func=cmd_plan)

    p_v3_kill = p_v3_sub.add_parser(
        "kill",
        help="create the killswitch file (halts the engine)",
    )
    p_v3_kill.add_argument("--reason", default="", help="reason string stored in the file")
    p_v3_kill.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip the confirmation prompt (for non-interactive scripts)",
    )
    p_v3_kill.set_defaults(func=cmd_kill)

    p_v3_unkill = p_v3_sub.add_parser(
        "unkill",
        help="remove the killswitch file (resumes the engine)",
    )
    p_v3_unkill.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip the confirmation prompt (for non-interactive scripts)",
    )
    p_v3_unkill.set_defaults(func=cmd_unkill)

    p_v3_heal = p_v3_sub.add_parser(
        "heal",
        help=(
            "reset engine-unhealthy state — resumes dispatch after "
            "consecutive failure pause"
        ),
    )
    p_v3_heal.set_defaults(func=cmd_heal)

    # `oxi v3 observe` — list/accept auto-observation proposals
    p_v3_observe = p_v3_sub.add_parser(
        "observe",
        help=(
            "list pending auto-observation proposals; accept one to "
            "queue it for dispatch"
        ),
    )
    p_v3_observe_sub = p_v3_observe.add_subparsers(
        dest="observe_action",
    )
    p_v3_observe_accept = p_v3_observe_sub.add_parser(
        "accept",
        help="accept a proposal by event id (closes the auto-improvement loop)",
    )
    p_v3_observe_accept.add_argument(
        "proposal_id",
        type=int,
        help="event id of the observe_proposal row to accept",
    )
    p_v3_observe.set_defaults(func=cmd_observe, observe_action=None)
    p_v3_observe_accept.set_defaults(func=cmd_observe)

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
    from oxi_core.v3.logging_setup import configure_logging

    configure_logging()
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
