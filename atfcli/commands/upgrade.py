"""`atf upgrade` — check, apply, roll back, and review stack upgrades.

The work lives in `atfcli.upgrade`; this module is the argument surface. It is
deliberately thin so the same engine can be driven by `atf system upgrade` and
by tests without an argparse namespace in the way.
"""

from __future__ import annotations

from .. import upgrade as eng
from ..errors import AtfError
from .. import validators
from . import _shared as sh

NAME = "upgrade"
HELP = "Upgrade the stack: check for a newer revision, apply it, or roll back"

#: `atf upgrade` with no verb should feel like `git pull`: look, explain, ask,
#: then do it. The verbs below exist for scripting and for the unhappy paths.
DEFAULT_VERB = "apply"


def _check(ctx, args) -> int:
    chk = eng.check(ctx, branch_name=args.branch, remote=args.remote, do_fetch=not args.no_fetch)
    if args.json:
        ctx.out.json(chk.to_json())
    else:
        eng.render_check(ctx, chk)
    # Exit code is the contract: 0 = nothing to do, 10 = an upgrade is
    # available, 3 = this install cannot upgrade itself. A cron job must be able
    # to tell "up to date" from "cannot even look".
    if chk.blockers:
        return 3
    return 10 if (chk.behind or chk.newer_version) else 0


def _plan(ctx, args) -> int:
    chk = eng.check(ctx, branch_name=args.branch, remote=args.remote, do_fetch=not args.no_fetch)
    steps = eng.plan(ctx, chk, build=not args.no_build, backup=not args.no_backup)
    if args.json:
        ctx.out.json({"check": chk.to_json(), "plan": steps})
        return 0
    ctx.out.title("atf upgrade plan")
    ctx.out.kv([("installed", chk.current.label), ("available", chk.target.label), ("behind", str(chk.behind))])
    ctx.out.line()
    for index, step in enumerate(steps, 1):
        ctx.out.line(f"  {index}. {step}")
    for note in chk.notes:
        ctx.out.hint(note)
    for blocker in chk.blockers:
        ctx.out.fail(blocker)
    return 0 if chk.can_apply else 3


def _apply(ctx, args) -> int:
    chk = None
    if not args.ref:
        chk = eng.check(ctx, branch_name=args.branch, remote=args.remote, do_fetch=not args.no_fetch)
        if not args.json:
            eng.render_check(ctx, chk)
        if chk.up_to_date and not args.rebuild:
            if args.json:
                ctx.out.json({"status": "up-to-date", "current": chk.current.to_json(), "check": chk.to_json()})
            else:
                ctx.out.ok("nothing to upgrade")
            return 0
        if chk.blockers and not args.force:
            raise AtfError(
                "cannot upgrade in the current state",
                hint="; ".join(chk.blockers),
            )
    if not args.yes and not args.json:
        sh.confirm_or_fail(
            ctx,
            f"upgrade from {chk.current.label if chk else 'current revision'}"
            + (f" to {chk.target.label}" if chk else f" to {args.ref}"),
            default=False,
        )
    run = eng.apply(
        ctx,
        ref=args.ref,
        branch_name=args.branch,
        remote=args.remote,
        rebuild=not args.no_build,
        backup=not args.no_backup,
        verify=not args.no_verify,
        prune=args.prune,
        stash=args.stash,
        force=args.force,
        assume_yes=args.yes,
        fetch_remote=not args.no_fetch,
    )
    if args.json:
        ctx.out.json(run.to_json())
    else:
        eng.render_run(ctx, run)
    return 0 if run.ok else 1


def _rollback(ctx, args) -> int:
    previous = eng.previous_revision(ctx)
    if previous is None:
        raise AtfError(
            "no previous revision is recorded",
            hint=f"{eng.STATE_DIR}/{eng.STATE_FILE} is written on every upgrade; there is nothing to return to",
        )
    if not args.yes and not args.json:
        sh.confirm_or_fail(ctx, f"roll back to {previous.label}", default=False)
    run = eng.rollback(ctx, rebuild=not args.no_build, prune=args.prune, verify=not args.no_verify)
    if args.json:
        ctx.out.json(run.to_json())
    else:
        eng.render_run(ctx, run)
    return 0 if run.ok else 1


def _history(ctx, args) -> int:
    entries = eng.history(ctx, limit=args.limit)
    if args.json:
        ctx.out.json({"history": entries})
        return 0
    ctx.out.title("atf upgrade history")
    eng.render_history(ctx, entries)
    state = eng.read_state(ctx)
    previous = state.get("previous")
    if isinstance(previous, dict):
        ctx.out.line()
        ctx.out.hint(f"`atf upgrade rollback` returns to {previous.get('short') or previous.get('version')}")
    return 0


# ------------------------------------------------------------------- arguments


def _add_common(parser, parents) -> None:
    # `default=None`, not `default=""`: argparse applies `type=` to a *string*
    # default as well, so an empty default would be run through the git-ref
    # validator and reject the very invocation that omits the flag.
    parser.add_argument(
        "--branch",
        type=validators.argparse_type("git_ref"),
        default=None,
        help="branch to compare/upgrade from (default: the current branch)",
    )
    parser.add_argument(
        "--remote",
        type=validators.argparse_type("identifier", optional=True),
        default="",
        help="git remote to fetch from (default: the branch's upstream)",
    )


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog=(
            "Exit codes: 0 ok/up-to-date · 1 upgrade failed · 3 cannot upgrade in "
            "this state · 10 an upgrade is available (`atf upgrade check`).\n\n"
            "The revision left behind is recorded in .atf/upgrade-state.json "
            "before anything changes, so `atf upgrade rollback` always has a way back."
        ),
    )
    _add_common(parser, parents)
    parser.set_defaults(
        func=_apply,
        ref="",
        stash=False,
        prune=False,
        rebuild=False,
        force=False,
        no_fetch=False,
        no_build=False,
        no_backup=False,
        no_verify=False,
    )
    parser.add_argument(
        "--ref",
        type=validators.argparse_type("git_ref", optional=True),
        default="",
        help="upgrade to an exact ref (tag, branch, or commit)",
    )
    parser.add_argument("--no-build", action="store_true", help="skip `docker compose build`")
    parser.add_argument("--no-backup", action="store_true", help="skip the pre-upgrade database snapshot")
    parser.add_argument("--no-verify", action="store_true", help="skip the post-upgrade health checks")
    parser.add_argument("--stash", action="store_true", help="park uncommitted changes instead of refusing")
    parser.add_argument("--force", action="store_true", help="proceed despite a dirty tree or a diverged branch")
    parser.add_argument("--prune", action="store_true", help="remove dangling images afterwards")
    parser.add_argument("--rebuild", action="store_true", help="rebuild even when already up to date")

    sub = sh.add_group(parser)

    p = sub.add_parser("check", help="report the installed and the available revision", parents=parents)
    _add_common(p, parents)
    p.add_argument("--no-fetch", action="store_true", help="do not contact the remote")
    p.set_defaults(func=_check)

    p = sub.add_parser("plan", help="show what an upgrade would do, without doing it", parents=parents)
    _add_common(p, parents)
    p.add_argument("--no-fetch", action="store_true", help="do not contact the remote")
    p.set_defaults(func=_plan)

    p = sub.add_parser("apply", help="run the upgrade (default verb)", parents=parents)
    _add_common(p, parents)
    p.add_argument("--ref", type=validators.argparse_type("git_ref", optional=True), default="",
                   help="upgrade to an exact ref (tag, branch, or commit)")
    p.add_argument("--no-build", action="store_true", help="skip `docker compose build`")
    p.add_argument("--no-backup", action="store_true", help="skip the pre-upgrade database snapshot")
    p.add_argument("--no-verify", action="store_true", help="skip the post-upgrade health checks")
    p.add_argument("--stash", action="store_true", help="park uncommitted changes instead of refusing")
    p.add_argument("--force", action="store_true", help="proceed despite a dirty tree or a diverged branch")
    p.add_argument("--prune", action="store_true", help="remove dangling images afterwards")
    p.add_argument("--rebuild", action="store_true", help="rebuild even when already up to date")
    p.set_defaults(func=_apply)

    p = sub.add_parser("rollback", help="return to the revision recorded before the last upgrade", parents=parents)
    p.add_argument("--no-build", action="store_true", help="skip rebuilding (code-only rollback)")
    p.add_argument("--no-verify", action="store_true", help="skip post-rollback health checks")
    p.add_argument("--prune", action="store_true", help="remove dangling images afterwards")
    p.set_defaults(func=_rollback)

    p = sub.add_parser("history", help="list recorded upgrades and rollbacks", parents=parents)
    p.add_argument("--limit", type=validators.argparse_type("int", minimum=1, maximum=50), default=10)
    p.set_defaults(func=_history)
