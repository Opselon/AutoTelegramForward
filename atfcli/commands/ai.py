"""`atf ai` — AI provider configurations, plus the provider catalogue and key probe.

New surface: `ai-configs` existed in the API and the web dashboard but not in the
CLI, so the one thing a headless deployment needs (point the bot at a different
model) required SQL. The CRUD verbs are the shared collection verbs; the two
extra verbs are the ones that only make sense for AI:

* ``providers`` — what the server will actually accept for ``--provider``;
  asking it beats guessing, because an unknown provider is stored happily and
  then fails at send time.
* ``check-key`` — probe a key before saving it. The API already masks the stored
  key, so a wrong key that was saved is invisible until a message fails.

Keys are never echoed: the console masker covers ``apiKey`` because
:mod:`atfcli.domain.secrets` lists it, and it is the same list used to skip the
echo check on server-masked fields (the server sends the key back redacted).
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.resources import AI_CONFIGS
from . import _resource
from . import _shared as sh

NAME = "ai"
HELP = "AI provider configurations: list, create, check a key, switch the active model"


def _providers(ctx, args) -> int:
    providers = ctx.uc.ops.ai_providers()
    if ctx.want_json():
        ctx.out.json({"count": len(providers), "providers": providers})
        return 0
    rows = []
    for entry in providers:
        models = entry.get("models") or entry.get("model") or []
        if isinstance(models, (list, tuple)):
            models = ", ".join(str(model) for model in models)
        rows.append(
            [
                str(entry.get("id") or entry.get("name") or "—"),
                str(entry.get("label") or entry.get("name") or "—"),
                str(entry.get("default_base_url") or entry.get("default_model") or "—"),
                sh.truncate(str(models), 60) if models else "—",
            ]
        )
    ctx.out.table(("PROVIDER", "LABEL", "DEFAULT BASE URL", "MODELS"), rows, empty="no providers reported")
    return 0


def _check_key(ctx, args) -> int:
    """Probe a key; the answer is `success`/`healthy`, not the HTTP status."""
    if not args.key and not getattr(args, "from_config", ""):
        raise sh.UsageError("nothing to check", hint="pass --key, or --from-config <ref> to probe a saved config")
    values: dict[str, Any] = {"provider": args.provider}
    if args.key:
        values["api_key"] = args.key
    if getattr(args, "base_url", ""):
        values["base_url"] = args.base_url
    if getattr(args, "model", ""):
        values["model"] = args.model
    if getattr(args, "from_config", ""):
        saved = ctx.uc.ai.find(args.from_config)
        # The stored key comes back masked, so only the shape of the request can
        # be taken from a saved config; `--key` supplies the secret itself.
        for field, target in (("provider", "provider"), ("baseUrl", "base_url"), ("model", "model")):
            if saved.raw.get(field) and not values.get(target):
                values[target] = saved.raw[field]
        values["config_id"] = saved.id
    answer = ctx.uc.ops.check_ai_key(**values)
    ok = bool(answer.get("ok"))
    if ctx.want_json():
        ctx.out.json({key: value for key, value in answer.items() if key != "raw"})
        return 0 if ok else 1
    if ok:
        ctx.out.ok(answer.get("detail") or "the provider accepted this key")
        latency = answer.get("latency_ms")
        if latency:
            ctx.out.kv([("latency", f"{float(latency):.0f} ms")])
        if answer.get("model"):
            ctx.out.kv([("model", answer["model"])])
    else:
        ctx.out.fail(answer.get("detail") or answer.get("error_code") or "the provider rejected this key")
    return 0 if ok else 1


def _toggle(enabled: bool):
    def handler(ctx, args) -> int:
        if getattr(args, "dry_run", False):
            row = ctx.uc.ai.find(args.ref)
            ctx.out.json({"ok": True, "dry_run": True, "id": row.id, "isEnabled": enabled})
            return 0
        outcome = ctx.uc.ai.update(args.ref, {"isEnabled": enabled})
        for warning in outcome.warnings:
            ctx.out.warn(warning.message)
        ctx.out.emit(outcome.to_json(), outcome.detail)
        return 0

    return handler


def _show(ctx, args) -> int:
    row = ctx.uc.ai.find(args.ref)
    document = dict(row.raw)
    if ctx.want_json():
        ctx.out.json(document)
        return 0
    ctx.out.title(f"AI config {row.id[:8]}")
    rows = []
    for key, value in sorted(document.items()):
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        else:
            rendered = str(value) if value not in (None, "") else "—"
        rows.append((key, sh.truncate(rendered, 80)))
    ctx.out.kv(rows)
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog="`atf ai providers` lists what `--provider` accepts; `atf ai check-key` probes one.",
    )
    sub = sh.add_group(parser)

    verbs = _resource.register(
        sub,
        parents,
        AI_CONFIGS,
        aliases={"list": ["ls"], "create": ["add"], "update": ["edit"], "delete": ["rm"]},
    )
    # `show` prints the masked document rather than the generic key/value dump, so
    # the secret-bearing field reads the same way the dashboard shows it.
    sub.choices["show"].set_defaults(func=_show)
    verbs["show"] = _show

    p = sub.add_parser("providers", aliases=["kinds"], help="list the AI providers the server supports", parents=parents)
    p.set_defaults(func=_providers)

    p = sub.add_parser("check-key", help="probe an API key before saving it", parents=parents)
    p.add_argument("--provider", required=True, help="provider id (see `atf ai providers`)")
    p.add_argument("--key", default="", help="API key to probe (never stored)")
    p.add_argument("--model", default="", help="model to probe with")
    p.add_argument("--base-url", default="", help="override the provider base URL")
    p.add_argument("--from-config", default="", help="take provider/model/base-url from a saved config")
    p.set_defaults(func=_check_key)

    for name, enabled, help_text in (
        ("enable", True, "enable an AI config"),
        ("disable", False, "disable an AI config"),
    ):
        p = sub.add_parser(name, help=help_text, parents=parents)
        p.add_argument("ref", help="config id, id prefix or name")
        p.set_defaults(func=_toggle(enabled))
