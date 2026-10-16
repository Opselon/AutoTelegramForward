"""GitHub Actions CI inspection and run status."""

from __future__ import annotations

import json

from . import _shared as sh

NAME = "ci"
HELP = "Inspect GitHub Actions CI runs, check annotations, and monitor workflows"

RUN_FIELDS = (
    "databaseId",
    "displayTitle",
    "workflowName",
    "status",
    "conclusion",
    "headBranch",
    "createdAt",
    "url",
)


def _gh_json(ctx, argv: list[str]) -> object | None:
    """Ask gh for machine-readable output; None when gh is missing or unhappy."""
    res = ctx.repo.run(["gh", *argv, "--json", ",".join(RUN_FIELDS)], check=False, quiet=True)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None


def _status(ctx, args) -> int:
    limit = str(args.limit or 5)
    argv = ["run", "list", "--limit", limit]
    if args.branch:
        argv.extend(["--branch", args.branch])

    runs = _gh_json(ctx, argv)
    if runs is None:
        # gh unavailable or not authenticated: fall back to its own rendering.
        ctx.out.step("Querying GitHub Actions runs...")
        return ctx.repo.run_streaming(["gh", *argv], check=False)

    if ctx.want_json():
        ctx.out.json({"runs": runs, "count": len(runs)})
        return 0

    ctx.out.title("CI runs")
    ctx.out.table(
        ("RUN", "WORKFLOW", "BRANCH", "STATUS", "CONCLUSION"),
        [
            [
                str(run.get("databaseId", "")),
                sh.truncate(str(run.get("workflowName", "")), 24),
                str(run.get("headBranch", "")),
                str(run.get("status", "")),
                str(run.get("conclusion") or "-"),
            ]
            for run in runs
        ],
    )
    return 0


def _view(ctx, args) -> int:
    run_id = args.run_id or ""
    base = ["run", "view", *([run_id] if run_id else [])]

    if args.log:
        if ctx.want_json():
            res = ctx.repo.run(["gh", *base, "--log"], check=False, quiet=True)
            ctx.out.json({"run_id": run_id, "log": res.stdout, "exit_code": res.returncode})
            return 0
        return ctx.repo.run_streaming(["gh", *base, "--log"], check=False)

    if ctx.want_json():
        payload = _gh_json(ctx, base)
        if payload is None:
            res = ctx.repo.run(["gh", *base], check=False, quiet=True)
            payload = {"run_id": run_id, "output": res.stdout, "exit_code": res.returncode}
        ctx.out.json(payload)
        return 0

    return ctx.repo.run_streaming(["gh", *base], check=False)


def _watch(ctx, args) -> int:
    """Stream a run to completion; --json reports the final conclusion instead."""
    if ctx.want_json():
        cmd = ["run", "watch", *([args.run_id] if args.run_id else []), "--exit-status"]
        res = ctx.repo.run(["gh", *cmd], check=False, quiet=True)
        ctx.out.json({"run_id": args.run_id, "exit_code": res.returncode, "ok": res.ok})
        return res.returncode
    ctx.out.step("Watching CI execution...")
    cmd = ["run", "watch", *([args.run_id] if args.run_id else [])]
    return ctx.repo.run_streaming(["gh", *cmd], check=False)


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("status", help="list recent CI runs", parents=parents)
    p.add_argument("-n", "--limit", type=int, default=5, help="number of runs")
    p.add_argument("-b", "--branch", default="", help="filter by git branch")
    p.set_defaults(func=_status)

    p = sub.add_parser("view", help="view detailed logs or findings for a run", parents=parents)
    p.add_argument("run_id", nargs="?", default="", help="run ID")
    p.add_argument("--log", action="store_true", help="show raw logs")
    p.set_defaults(func=_view)

    p = sub.add_parser("watch", help="wait for in-flight CI run to complete", parents=parents)
    p.add_argument("run_id", nargs="?", default="", help="run ID")
    p.set_defaults(func=_watch)
