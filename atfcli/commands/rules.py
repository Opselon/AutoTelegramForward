"""Forwarding tasks: lifecycle, per-task settings, sources, simulation.

This is the group an operator lives in, so the verbs mirror the dashboard
exactly and accept the same identifiers a person has to hand — full id, id
prefix, or task name.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..errors import AtfError, UsageError
from . import _shared as sh

NAME = "rules"
HELP = "Manage forwarding tasks: list, show, edit, pause, clone, settings, sources"


# --------------------------------------------------------------------- verbs
def _list(ctx, args) -> None:
    payload = ctx.api.get("/api/rules")
    rules = sh.list_of(payload, "rules")
    if args.enabled_only:
        rules = [r for r in rules if r.get("is_enabled")]
    if args.state:
        want = args.state.lower()
        rules = [
            r
            for r in rules
            if (want == "on" and r.get("is_enabled"))
            or (want == "off" and not r.get("is_enabled"))
            or (want == "paused" and r.get("is_paused"))
        ]
    if args.source:
        needle = args.source.lower()
        rules = [
            r
            for r in rules
            if needle in str(r.get("source_chat_id", "")).lower()
            or needle in str(r.get("source_chat_title", "")).lower()
            or any(needle in str(s).lower() for s in (r.get("source_chat_ids") or []))
        ]
    if args.json:
        ctx.out.json({"count": len(rules), "rules": rules})
        return
    ctx.out.table(sh.RULE_HEADERS, [sh.rule_row(r) for r in rules], empty="no tasks")
    ctx.out.line()
    ctx.out.line(f"  {len(rules)} task(s)")


def _show(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    if args.json:
        ctx.out.json(rule)
        return
    sh.print_rule(ctx, rule, deep=True)


def _create(ctx, args) -> None:
    body: dict[str, Any] = {
        "source_chat_id": args.source,
        "target_chat_id": args.target,
    }
    if args.name:
        body["name"] = args.name
    if args.session:
        body["session_id"] = args.session
    if args.source_title:
        body["source_chat_title"] = args.source_title
    if args.target_title:
        body["target_chat_title"] = args.target_title
    if getattr(args, "no_enable", False):
        body["is_enabled"] = False
    if args.set:
        body.update(sh.settings_from_args(args))
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "would_create": body})
        return
    created = ctx.api.post("/api/rules", payload=body)
    if args.json:
        ctx.out.json(created)
        return
    rule = created.get("rule", created) if isinstance(created, dict) else {}
    ctx.out.ok(f"created task {rule.get('id', '(id in response)')}")
    if isinstance(rule, dict) and rule.get("id"):
        ctx.out.kv(
            [
                ("source", rule.get("source_chat_id", "")),
                ("target", rule.get("target_chat_id", "")),
            ]
        )


def _update(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    patch = sh.settings_from_args(args)
    if args.name:
        patch["name"] = args.name
    if getattr(args, "source", None):
        patch["source_chat_id"] = args.source
    if getattr(args, "target", None):
        patch["target_chat_id"] = args.target
    if not patch:
        raise UsageError("nothing to update", hint="pass --set key=value, --name, --source or --target")
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "task": rule.get("id"), "patch": patch})
        return
    result = ctx.api.put(f"/api/rules/{rule['id']}", payload=patch)
    if args.json:
        ctx.out.json(result)
        return
    ctx.out.ok(f"updated task {str(rule['id'])[:8]} ({', '.join(sorted(patch))})")


def _delete(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    sh.confirm_or_fail(ctx, f"delete task {rule.get('name') or str(rule['id'])[:8]}?")
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "would_delete": rule.get("id")})
        return
    ctx.api.delete(f"/api/rules/{rule['id']}")
    ctx.out.emit({"ok": True, "deleted": rule.get("id")}, f"deleted task {str(rule['id'])[:8]}")


def _lifecycle(verb: str):
    """Build the enable/disable/pause/resume/stop/restart/toggle handlers."""

    def handler(ctx, args) -> None:
        rule = sh.resolve_rule(ctx.api, args.ref)
        path = {
            "enable": "toggle",
            "disable": "toggle",
            "toggle": "toggle",
            "pause": "pause",
            "resume": "resume",
            "stop": "stop",
            "restart": "restart",
        }[verb]
        if args.dry_run:
            ctx.out.json({"ok": True, "dry_run": True, "task": rule.get("id"), "action": verb})
            return
        result = ctx.api.post(f"/api/rules/{rule['id']}/{path}")
        # toggle flips state, so "enable" on an enabled task is a no-op the
        # operator should be told about rather than a silent flip back off.
        if verb in ("enable", "disable"):
            was = bool(rule.get("is_enabled"))
            state = result.get("is_enabled", result.get("rule", {}).get("is_enabled"))
            if verb == "enable" and was and state is not None and not state:
                result = ctx.api.post(f"/api/rules/{rule['id']}/toggle")
            elif verb == "disable" and not was and state is not None and state:
                result = ctx.api.post(f"/api/rules/{rule['id']}/toggle")
        if args.json:
            ctx.out.json(result)
            return
        current = result.get("is_enabled") if isinstance(result, dict) else None
        ctx.out.ok(
            f"{verb} task {str(rule['id'])[:8]}"
            + (f" → {'enabled' if current else 'disabled'}" if current is not None else "")
        )

    return handler


def _duplicate(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "would_duplicate": rule.get("id")})
        return
    result = ctx.api.post(f"/api/rules/{rule['id']}/duplicate")
    if args.json:
        ctx.out.json(result)
        return
    clone = result.get("rule", result) if isinstance(result, dict) else {}
    ctx.out.ok(f"duplicated {str(rule['id'])[:8]} → {str(clone.get('id', ''))[:8]}")


def _path(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    body = {"route_path": args.path}
    if args.intermediate:
        body["intermediate_chat_id"] = args.intermediate
    result = ctx.api.post(f"/api/rules/{rule['id']}/route-path", payload=body)
    ctx.out.emit(result, f"route path for {str(rule['id'])[:8]} set to {args.path}")


def _simulate(ctx, args) -> None:
    """Dry-run a message through the live rule engine."""
    rule = sh.resolve_rule(ctx.api, args.ref)
    text = args.text
    if args.file:
        text = ctx.repo.path(args.file).read_text(encoding="utf-8")
    if not text:
        raise UsageError("provide --text or --file")
    payload = {"rule_id": rule["id"], "text": text}
    if args.sender:
        payload["sender"] = args.sender
    result = ctx.api.post("/api/simulate", payload=payload)
    if args.json:
        ctx.out.json(result)
        return
    ctx.out.title("simulation")
    if isinstance(result, dict):
        for key in ("matched", "action", "reason", "would_forward", "transformed_text", "notes"):
            if key in result:
                value = result[key]
                ctx.out.kv([(key, sh.truncate(str(value), 200) if not isinstance(value, (int, float, bool)) else value)])
        if args.show and "transformed_text" in result:
            ctx.out.line()
            ctx.out.line(str(result["transformed_text"]))
    else:
        ctx.out.line(str(result))


# ------------------------------------------------------------------ settings
def _settings_schema(ctx, args) -> None:
    payload = ctx.api.get("/api/rules/settings/schema")
    if args.json:
        ctx.out.json(payload)
        return
    settings = payload.get("settings", []) if isinstance(payload, dict) else []
    group = getattr(args, "settings_group", "")
    rows = []
    for spec in settings:
        if group and spec.get("group") != group:
            continue
        default = spec.get("default")
        if isinstance(default, (dict, list)):
            default = json.dumps(default, ensure_ascii=False)
        rows.append(
            [
                spec.get("key", ""),
                spec.get("kind", ""),
                sh.truncate(str(default), 22),
                spec.get("group", ""),
                sh.truncate(spec.get("label", ""), 30),
            ]
        )
    ctx.out.table(("KEY", "KIND", "DEFAULT", "GROUP", "LABEL"), rows)
    ctx.out.line()
    ctx.out.line(f"  {len(rows)} setting(s) — {payload.get('count', len(settings))} registered")
    groups = payload.get("groups") or []
    if groups:
        ctx.out.line("  groups: " + ", ".join(groups))


def _settings_get(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    payload = ctx.api.get(f"/api/rules/{rule['id']}/settings")
    settings = payload.get("settings", {}) if isinstance(payload, dict) else {}
    if args.key:
        if args.key not in settings:
            from ..errors import NotFoundError

            raise NotFoundError(f"setting {args.key!r} is not set on this task")
        value = settings[args.key]
        human = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ctx.out.emit({"key": args.key, "value": value}, f"{args.key} = {human}")
        return
    if args.json:
        ctx.out.json(payload)
        return
    ctx.out.title(f"settings · {rule.get('name') or str(rule['id'])[:8]}")
    if not settings:
        ctx.out.line("  (no per-task overrides — defaults apply)")
        return
    rows = []
    for key in sorted(settings):
        value = settings[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        rows.append([key, sh.truncate(str(value), 60)])
    ctx.out.table(("SETTING", "VALUE"), rows)


def _settings_set(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    values = sh.settings_from_args(args)
    if not values:
        raise UsageError("nothing to set", hint="pass key=value pairs, e.g. clean_buttons=true")
    # Unknown keys are rejected server-side; catching them here would require
    # duplicating the registry, so send and surface the server's message.
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "task": rule.get("id"), "settings": values})
        return
    result = ctx.api.patch(
        f"/api/rules/{rule['id']}/settings",
        payload={"settings": values, "replace_all": bool(args.replace)},
    )
    if args.json:
        ctx.out.json(result)
        return
    if result.get("success") is False:
        raise AtfError(
            str(result.get("message", "the server rejected these settings")),
            hint=str(result.get("error_code", "")),
        )
    ctx.out.ok(
        f"applied {len(values)} setting(s) to {str(rule['id'])[:8]}: "
        + ", ".join(sorted(values))
    )


def _settings_reset(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    # `reset` takes bare keys (there is no value to type), so the shared
    # key=value collector does not apply here.
    cleared = {key: None for key in (args.keys or [])}
    if not cleared:
        raise UsageError("nothing to clear", hint="name the settings to remove, e.g. clean_buttons")
    result = ctx.api.patch(
        f"/api/rules/{rule['id']}/settings",
        payload={"settings": cleared, "replace_all": False},
    )
    ctx.out.emit(result, f"cleared {len(cleared)} override(s) on {str(rule['id'])[:8]}")


# ------------------------------------------------------------------- sources
def _sources_list(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    ids = [s for s in (rule.get("source_chat_ids") or []) if s]
    if not ids:
        ids = [rule.get("source_chat_id", "")]
    names = rule.get("source_chat_names") or []
    rows = [[str(i), ids[i], names[i] if i < len(names) else ""] for i in range(len(ids))]
    if args.json:
        ctx.out.json({"task": rule.get("id"), "sources": ids, "names": names})
        return
    ctx.out.table(("INDEX", "CHAT ID", "TITLE"), rows, empty="no sources")


def _sources_add(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    ids = [s for s in (rule.get("source_chat_ids") or []) if s] or [rule.get("source_chat_id", "")]
    if args.chat in ids:
        ctx.out.warn(f"{args.chat} is already a source of this task")
        return
    ids.append(args.chat)
    patch: dict[str, Any] = {"source_chat_ids": ids}
    names = list(rule.get("source_chat_names") or [])
    if args.title:
        names = names + [args.title] if names else [args.title]
        patch["source_chat_names"] = names
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "patch": patch})
        return
    ctx.api.put(f"/api/rules/{rule['id']}", payload=patch)
    ctx.out.emit(
        {"ok": True, "sources": ids},
        f"added {args.chat} — task now reads {len(ids)} source(s)",
    )


def _sources_remove(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    ids = [s for s in (rule.get("source_chat_ids") or []) if s] or [rule.get("source_chat_id", "")]
    if args.chat not in ids:
        from ..errors import NotFoundError

        raise NotFoundError(f"{args.chat} is not a source of this task")
    if len(ids) == 1:
        raise AtfError(
            "a task must keep at least one source",
            hint="add another source first, or delete the task",
        )
    ids = [s for s in ids if s != args.chat]
    names = [n for n in (rule.get("source_chat_names") or []) if n != args.chat]
    patch: dict[str, Any] = {"source_chat_ids": ids}
    if names:
        patch["source_chat_names"] = names
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "patch": patch})
        return
    ctx.api.put(f"/api/rules/{rule['id']}", payload=patch)
    ctx.out.emit(
        {"ok": True, "sources": ids},
        f"removed {args.chat} — task now reads {len(ids)} source(s)",
    )


def _sources_set(ctx, args) -> None:
    rule = sh.resolve_rule(ctx.api, args.ref)
    if not args.chats:
        raise UsageError("pass one or more chat ids")
    patch = {"source_chat_ids": list(args.chats), "source_chat_id": args.chats[0]}
    if args.dry_run:
        ctx.out.json({"ok": True, "dry_run": True, "patch": patch})
        return
    ctx.api.put(f"/api/rules/{rule['id']}", payload=patch)
    ctx.out.emit({"ok": True, "sources": list(args.chats)}, f"set {len(args.chats)} source(s)")


# ------------------------------------------------------------------- wiring
def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(
        NAME,
        help=HELP,
        description=HELP,
        parents=parents,
        epilog="Identifiers accept a full task id, a unique id prefix, or the task name.",
    )
    sub = sh.add_group(parser)

    p = sub.add_parser("list", aliases=["ls"], help="list tasks", parents=parents)
    p.add_argument("--source", default="", help="filter by source chat id or title")
    p.add_argument("--state", choices=["on", "off", "paused"], help="filter by state")
    p.add_argument("--enabled-only", action="store_true", help="hide disabled tasks")
    p.set_defaults(func=_list)

    p = sub.add_parser("show", help="show one task in detail", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.set_defaults(func=_show)

    p = sub.add_parser("create", help="create a task", parents=parents)
    p.add_argument("--source", required=True, help="source chat id or @username")
    p.add_argument("--target", required=True, help="target chat id or @username")
    p.add_argument("--name", default="", help="human label for the task")
    p.add_argument("--source-title", default="", help="display title for the source")
    p.add_argument("--target-title", default="", help="display title for the target")
    p.add_argument("--session", default="", help="telegram session id to forward with")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="extra field (repeatable)")
    p.add_argument("--no-enable", action="store_true", help="create it paused/disabled")
    p.set_defaults(func=_create)

    p = sub.add_parser("update", aliases=["edit"], help="update task fields", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.add_argument("--name", default="", help="new label")
    p.add_argument("--source", default="", help="new primary source")
    p.add_argument("--target", default="", help="new target")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="field=value (repeatable)")
    p.set_defaults(func=_update)

    p = sub.add_parser("delete", aliases=["rm"], help="delete a task", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.set_defaults(func=_delete)

    for verb, help_text in (
        ("enable", "enable a task"),
        ("disable", "disable a task"),
        ("toggle", "flip a task's enabled state"),
        ("pause", "pause processing without disabling"),
        ("resume", "resume a paused task"),
        ("stop", "stop forwarding for this task"),
        ("restart", "restart forwarding for this task"),
    ):
        p = sub.add_parser(verb, help=help_text, parents=parents)
        p.add_argument("ref", help="task id, id prefix or name")
        p.set_defaults(func=_lifecycle(verb))

    p = sub.add_parser("duplicate", aliases=["clone"], help="clone a task", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.set_defaults(func=_duplicate)

    p = sub.add_parser("path", help="set the routing path (direct / via intermediate)", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.add_argument("--path", required=True, choices=["direct", "intermediate"], help="route path")
    p.add_argument("--intermediate", default="", help="intermediate chat id when --path intermediate")
    p.set_defaults(func=_path)

    p = sub.add_parser("simulate", aliases=["test-message"], help="dry-run a message through a task", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.add_argument("--text", default="", help="message text to simulate")
    p.add_argument("--file", default="", help="read the message text from a file")
    p.add_argument("--sender", default="", help="sender username or id")
    p.add_argument("--show", action="store_true", help="print the transformed text")
    p.set_defaults(func=_simulate)

    # ------------------------------------------------------------- settings
    p = sub.add_parser("settings", help="per-task settings", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    settings_sub = sh.add_group(p)

    sp = settings_sub.add_parser("schema", help="the full settings registry", parents=parents)
    # NOTE: `--group` must NOT use the default dest ("group") — that dest belongs
    # to the top-level subparsers action, and argparse copies a subparser's
    # namespace over the parent's, silently blanking `args.group` and sending
    # dispatch down the wrong branch.
    sp.add_argument(
        "--group",
        dest="settings_group",
        default="",
        help="only this group (e.g. content_cleanup)",
    )
    sp.set_defaults(func=_settings_schema)

    sp = settings_sub.add_parser("get", help="show a task's settings", parents=parents)
    sp.add_argument("ref", help="task id, id prefix or name")
    sp.add_argument("key", nargs="?", default="", help="one setting key")
    sp.set_defaults(func=_settings_get)

    sp = settings_sub.add_parser("set", help="apply settings", parents=parents)
    sp.add_argument("ref", help="task id, id prefix or name")
    sp.add_argument("set", nargs="+", metavar="KEY=VALUE", help="settings to apply")
    sp.add_argument("--replace", action="store_true", help="replace the whole settings object")
    sp.set_defaults(func=_settings_set)

    sp = settings_sub.add_parser("reset", help="remove overrides (back to defaults)", parents=parents)
    sp.add_argument("ref", help="task id, id prefix or name")
    sp.add_argument("keys", nargs="+", metavar="KEY")
    sp.set_defaults(func=_settings_reset)

    # -------------------------------------------------------------- sources
    p = sub.add_parser("sources", help="multi-source input management", parents=parents)
    p.set_defaults(func=sh.help_handler(p))
    sources_sub = sh.add_group(p)

    sp = sources_sub.add_parser("list", aliases=["ls"], help="list a task's sources", parents=parents)
    sp.add_argument("ref")
    sp.set_defaults(func=_sources_list)

    sp = sources_sub.add_parser("add", help="add a source channel", parents=parents)
    sp.add_argument("ref")
    sp.add_argument("chat", help="chat id or @username")
    sp.add_argument("--title", default="", help="display title for the new source")
    sp.set_defaults(func=_sources_add)

    sp = sources_sub.add_parser("remove", aliases=["rm"], help="remove a source channel", parents=parents)
    sp.add_argument("ref")
    sp.add_argument("chat", help="chat id or @username")
    sp.set_defaults(func=_sources_remove)

    sp = sources_sub.add_parser("set", help="replace the whole source list", parents=parents)
    sp.add_argument("ref")
    sp.add_argument("chats", nargs="+", help="chat ids, in order")
    sp.set_defaults(func=_sources_set)
