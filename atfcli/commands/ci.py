"""GitHub Actions CI inspection and run status."""

from __future__ import annotations

from . import _shared as sh

NAME = "ci"
HELP = "Inspect GitHub Actions CI runs, check annotations, and monitor workflows"


def _status(ctx, args) -> int:
    ctx.out.step("Querying GitHub Actions runs...")
    cmd = ["gh", "run", "list", "--limit", str(args.limit or 5)]
    if args.branch:
        cmd.extend(["--branch", args.branch])
    return ctx.repo.run_streaming(cmd, check=False)


def _view(ctx, args) -> int:
    run_id = args.run_id or ""
    cmd = ["gh", "run", "view"]
    if run_id:
        cmd.append(run_id)
    if args.log:
        cmd.append("--log")
    return ctx.repo.run_streaming(cmd, check=False)


def _watch(ctx, args) -> int:
    ctx.out.step("Watching CI execution...")
    cmd = ["gh", "run", "watch"]
    if args.run_id:
        cmd.append(args.run_id)
    return ctx.repo.run_streaming(cmd, check=False)


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
