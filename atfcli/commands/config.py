"""Configuration and environment (.env) management.

`.env` is the one file every service reads and the one file an operator routinely
hand-edits, so this group is deliberately conservative:

* a value is validated against `atfcli.domain.envschema` **before** anything is
  written, and a refusal names every offending key at once;
* `set` writes through the `EnvFileStore` port — timestamped backup, atomic
  replace, read-back verification, automatic rollback, mode 0600 — never with a
  bare `write_text`, which could leave a torn file behind;
* `diff`/`--dry-run` show the exact change without touching the disk;
* `validate` audits the file as it stands (placeholder credentials, invalid
  ports, missing required keys, duplicate assignments, world-readable mode) and
  exits non-zero on an error, so a cron job can gate on it;
* `backups`/`restore` make a mistyped value one command away from gone.

Secrets are masked by *name* (`atfcli.domain.secrets`), not by a per-command
list, so `--json` cannot become the path a bot token leaks through.
"""

from __future__ import annotations

from ..domain import envschema
from ..domain.envschema import is_masked
from ..errors import NotFoundError, UsageError
from . import _shared as sh

NAME = "config"
HELP = "Manage environment variables and configuration files (.env)"


def _values(ctx) -> dict[str, str]:
    """The file's current values, read through the use case (not a cache).

    `ctx.env_uc`, not `ctx.uc.env`: editing `.env` is host work and must not need
    an API credential. A server that is down is exactly when an operator reaches
    for `atf config`.
    """
    return dict(ctx.env_uc.values())


def _list(ctx, args) -> None:
    values = _values(ctx)
    masked = {key: ("[REDACTED]" if is_masked(key) else value) for key, value in values.items()}
    if sh.wants_json(ctx, args):
        ctx.out.json(masked)
        return
    ctx.out.title("Environment Configuration (.env)")
    ctx.out.kv(sorted(masked.items()), indent=1)
    undocumented = sorted(key for key in values if envschema.spec_for(key) is None)
    if undocumented:
        ctx.out.line()
        ctx.out.hint(f"{len(undocumented)} undocumented key(s): {', '.join(undocumented[:6])}")
    ctx.out.line()
    ctx.out.hint("`atf config schema` explains every documented key · `atf config validate` audits the file")


def _get(ctx, args) -> None:
    key = args.key
    problem = envschema.validate_key(key)
    if problem:
        raise UsageError(problem)
    values = _values(ctx)
    if key not in values:
        hint = f"`atf config set {key}=<value>` writes it"
        if envschema.describe(key):
            hint += f" · {envschema.describe(key)}"
        raise NotFoundError(f"{key} is not set", hint=hint)
    value = values[key]
    sensitive = is_masked(key)
    if sensitive and args.reveal:
        # An explicit, deliberate escape hatch: the console is the redaction
        # barrier, so a deliberate reveal has to bypass it rather than pretend
        # to be possible through it.
        ctx.out.warn(f"{key} is a credential — revealing on stdout")
        sh.write_stdout(value)
        return
    if sensitive:
        ctx.out.emit({"key": key, "value": "[REDACTED]"}, f"{key}=[REDACTED]  (pass --reveal to print it)")
        return
    ctx.out.emit({"key": key, "value": value}, f"{key}={value}")


def _plan(ctx, args, *, dry_run: bool) -> int:
    pairs = sh.settings_from_args(args)
    if not pairs:
        raise UsageError(
            "pass one or more KEY=VALUE pairs",
            hint="e.g. `atf config set ATF_WEB_PORT=8090`",
        )
    plan = ctx.env_uc.plan(pairs, allow_unknown=not getattr(args, "strict", False))
    effective_dry_run = dry_run or bool(ctx.repo.dry_run)
    if sh.wants_json(ctx, args):
        if effective_dry_run:
            ctx.out.json({"dry_run": True, **plan.to_json()})
        else:
            outcome = ctx.env_uc.apply(plan)
            ctx.out.json({**outcome.to_json(), "changed": list(plan.changed_keys)})
        return 0
    for warning in plan.warnings:
        ctx.out.warn(warning)
    if plan.empty:
        ctx.out.ok("no changes — the file already holds those values")
        return 0
    ctx.out.title("Proposed .env change")
    for line in plan.diff_lines():
        ctx.out.line(line)
    if effective_dry_run:
        ctx.out.line()
        ctx.out.hint("dry run: nothing was written")
        return 0
    outcome = ctx.env_uc.apply(plan)
    ctx.out.ok(outcome.detail)
    return 0


def _set(ctx, args) -> int:
    return _plan(ctx, args, dry_run=False)


def _diff(ctx, args) -> int:
    return _plan(ctx, args, dry_run=True)


def _validate(ctx, args) -> int:
    report = ctx.env_uc.validate()
    if sh.wants_json(ctx, args):
        ctx.out.json(report.to_json())
    else:
        ctx.out.title("Configuration audit")
        counts = {"error": 0, "warning": 0, "info": 0}
        for finding in report.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        ctx.out.kv(
            [
                ("file", report.path),
                ("keys", str(report.keys)),
                ("mode", oct(report.mode) if report.mode else "—"),
                ("problems", f"{counts['error']} error(s), {counts['warning']} warning(s)"),
            ]
        )
        for finding in report.findings:
            if finding.severity == "info" and not getattr(args, "verbose", False):
                continue
            line = f"  [{finding.severity}] {finding.title}"
            if finding.severity == "error":
                ctx.out.fail(line)
            elif finding.severity == "warning":
                ctx.out.warn(line)
            else:
                ctx.out.line(line)
            if finding.detail:
                ctx.out.line(f"        {finding.detail}")
            if finding.remediation:
                ctx.out.hint(f"        fix: {finding.remediation}")
        ctx.out.line()
        if report.ok:
            ctx.out.ok("configuration is valid")
        else:
            ctx.out.fail("configuration has errors — the stack may not start")
    return 0 if report.ok else 1


def _schema(ctx, args) -> int:
    # `dest="env_group"`: naming it `group` would overwrite the top-level dest
    # dispatch reads, and argparse would send the invocation down the wrong branch.
    wanted = getattr(args, "env_group", "") or ""
    specs = ctx.env_uc.schema(group=wanted)
    if sh.wants_json(ctx, args):
        ctx.out.json(
            {
                "count": len(specs),
                "groups": list(ctx.env_uc.groups()),
                "settings": [spec.to_json() for spec in specs],
            }
        )
        return 0
    ctx.out.title("Documented .env settings")
    for group in [wanted] if wanted else list(ctx.env_uc.groups()):
        rows = [spec for spec in specs if spec.group == group]
        if not rows:
            continue
        ctx.out.line()
        ctx.out.line("  " + ctx.out.paint(group.upper(), "bold"))
        for spec in rows:
            flag = "*" if spec.required else " "
            name = spec.name + (" (secret)" if spec.masked else "")
            default = "" if spec.masked or not spec.default else f"  [{spec.default}]"
            ctx.out.line(f"   {flag} {name}")
            detail = envschema.describe(spec.name) or spec.description
            if detail:
                ctx.out.line(f"       {detail}{default}")
    ctx.out.line()
    ctx.out.hint("(* required · secrets are never printed, not even their defaults)")
    return 0


def _backups(ctx, args) -> int:
    entries = ctx.env_uc.backups()
    if sh.wants_json(ctx, args):
        ctx.out.json({"count": len(entries), "backups": [entry.to_json() for entry in entries]})
        return 0
    rows = [[entry.name, entry.created_at, str(entry.keys), f"{entry.size} B"] for entry in entries]
    ctx.out.table(
        ("BACKUP", "CREATED", "KEYS", "SIZE"),
        rows,
        empty="no backups yet (one is written before every change)",
    )
    if entries:
        ctx.out.line()
        ctx.out.hint(f"`atf config restore {entries[0].name}` rolls the file back")
    return 0


def _restore(ctx, args) -> int:
    sh.confirm_or_fail(ctx, f"restore .env from backup {args.name}?")
    outcome = ctx.env_uc.restore(args.name)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=sh.help_handler(parser))
    sub = sh.add_group(parser)

    p = sub.add_parser("list", aliases=["ls"], help="list configuration variables", parents=parents)
    p.set_defaults(func=_list)

    p = sub.add_parser("get", help="get single configuration variable", parents=parents)
    p.add_argument("key", help="configuration key name")
    p.add_argument("--reveal", action="store_true", help="reveal secret value if authorized")
    p.set_defaults(func=_get)

    p = sub.add_parser(
        "set",
        help="set configuration variables (validated, backed up, atomic)",
        parents=parents,
        epilog=(
            "A value is validated before anything is written; the previous file is copied to\n"
            ".env.bak.<timestamp> and the write is verified after the replace, rolling back\n"
            "automatically if the bytes on disk are not the bytes that were sent."
        ),
    )
    p.add_argument("set", nargs="+", metavar="KEY=VALUE", help="key=value pairs to set")
    p.add_argument("--strict", action="store_true", help="refuse keys that are not documented settings")
    p.set_defaults(func=_set)

    p = sub.add_parser("diff", help="show what a change would do, without writing", parents=parents)
    p.add_argument("set", nargs="+", metavar="KEY=VALUE", help="key=value pairs to preview")
    p.add_argument("--strict", action="store_true", help="refuse keys that are not documented settings")
    p.set_defaults(func=_diff)

    p = sub.add_parser(
        "validate",
        aliases=["check"],
        help="audit .env against the schema (exit 1 on errors)",
        parents=parents,
    )
    p.set_defaults(func=_validate)

    p = sub.add_parser("schema", help="list documented keys, their groups and accepted values", parents=parents)
    p.add_argument(
        "--group",
        dest="env_group",
        default="",
        help="only one group (telegram, admin, security, web, redis, runtime, logging)",
    )
    p.set_defaults(func=_schema)

    p = sub.add_parser("backups", help="list rollback points written before each change", parents=parents)
    p.set_defaults(func=_backups)

    p = sub.add_parser("restore", help="restore a previous .env from its backup", parents=parents)
    p.add_argument("name", help="backup name as shown by `atf config backups`")
    p.set_defaults(func=_restore)
