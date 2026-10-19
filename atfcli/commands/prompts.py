"""`atf prompts` — reusable prompt templates, and testing one before it ships.

Prompt templates were reachable from the dashboard but not from the CLI; a
prompt that renders badly is the most expensive kind of mistake here, because it
is only caught when a real message goes out. ``atf prompts test`` runs a template
against a live AI config and prints what it would have produced, with no
forwarding and no writes.
"""

from __future__ import annotations

import json

from ..domain.resources import PROMPTS
from . import _resource
from . import _shared as sh

NAME = "prompts"
HELP = "Prompt templates: list, create, test against a live AI config"


def _test(ctx, args) -> int:
    sample = args.text
    if getattr(args, "file", ""):
        sample = ctx.repo.path(args.file).read_text(encoding="utf-8")
    if not sample:
        raise sh.UsageError("nothing to test", hint="pass --text 'a sample message' or --file sample.txt")
    config = ctx.uc.ai.find(args.config)
    outcome = ctx.uc.prompts.test_prompt(
        args.ref, config.id, sample, version=int(getattr(args, "version", 0) or 0)
    )
    output = outcome.follow_up.get("output", "")
    if ctx.want_json():
        ctx.out.json({**outcome.to_json(), "ai_config": config.id})
        return 0
    ctx.out.ok(outcome.detail)
    ctx.out.kv([("ai config", f"{config.label or config.id[:8]}"), ("model", str(config.raw.get('model') or '—'))])
    if output:
        ctx.out.line()
        ctx.out.title("rendered output")
        ctx.out.line(output)
    else:
        ctx.out.warn("the server returned no rendered text; see --json for the raw answer")
    return 0


def _columns(ctx, args) -> int:
    """Which templates use which language — handy when a translation goes missing."""
    rows = ctx.uc.prompts.list()
    counts: dict[str, int] = {}
    for row in rows:
        language = str(row.raw.get("target_language") or "unset")
        counts[language] = counts.get(language, 0) + 1
    if ctx.want_json():
        ctx.out.json({"total": len(rows), "by_language": counts})
        return 0
    ctx.out.table(("TARGET LANGUAGE", "TEMPLATES"), [[key, str(value)] for key, value in sorted(counts.items())])
    return 0


def _show(ctx, args) -> int:
    row = ctx.uc.prompts.find(args.ref)
    if ctx.want_json():
        ctx.out.json(row.to_json())
        return 0
    ctx.out.title(f"prompt {row.id[:8]} · {row.label or 'untitled'}")
    scalar = [(key, value) for key, value in sorted(row.raw.items()) if not isinstance(value, (dict, list))]
    ctx.out.kv([(key, str(value) if value not in (None, "") else "—") for key, value in scalar])
    for key in ("system_prompt", "user_prompt_template", "description"):
        body = row.raw.get(key)
        if body:
            ctx.out.line()
            ctx.out.title(key.replace("_", " "))
            ctx.out.line(str(body))
    other = {key: value for key, value in row.raw.items() if isinstance(value, (dict, list)) and value}
    if other:
        ctx.out.line()
        ctx.out.kv([(key, sh.truncate(json.dumps(value, ensure_ascii=False), 100)) for key, value in sorted(other.items())])
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog="`atf prompts test <template> --config <ai-config> --text 'hi'` renders without sending.",
    )
    sub = sh.add_group(parser)
    _resource.register(
        sub,
        parents,
        PROMPTS,
        aliases={"list": ["ls"], "create": ["add"], "update": ["edit"], "delete": ["rm"]},
        extra_help="Body fields use --system-prompt / --user-prompt-template (the wire names are snake_case here).",
    )
    sub.choices["show"].set_defaults(func=_show)

    p = sub.add_parser("test", aliases=["preview"], help="render a template against a live AI config", parents=parents)
    p.add_argument("ref", help="template id, unique prefix or name")
    p.add_argument("--config", required=True, help="AI config to render with (id, prefix or name)")
    p.add_argument("--text", default="", help="sample message to render")
    p.add_argument("--file", default="", help="read the sample message from a file")
    p.add_argument("--version", type=int, default=0, help="template version to test")
    p.set_defaults(func=_test)

    p = sub.add_parser("languages", aliases=["coverage"], help="template counts per target language", parents=parents)
    p.set_defaults(func=_columns)
