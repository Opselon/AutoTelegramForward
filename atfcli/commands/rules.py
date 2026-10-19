"""`atf rules` — forwarding tasks: lifecycle, settings, sources, history, simulation.

The group an operator lives in. Two things changed when this moved onto the
application layer, and both were real bugs before:

* **simulation** posts ``sample_text``/``sample_sender_id``. It used to post
  ``text``/``sender``, which the API ignores — so every simulation ran against an
  empty message and reported "no match" for a rule that would have matched.
* **lifecycle** compares the state the server reports with the state the verb
  promised, because the only mutation the API offers is ``toggle``: `enable` on an
  already-enabled task must not flip it *off*.

Identifiers accept a full id, a unique id prefix, or the task name, and that
resolution happens once, in the use case, so every verb accepts the same forms.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.errors import NotFoundError, UsageError
from ..domain.host import SERVICES
from ..domain.logs import LogQuery
from ..domain.models import RoutePath, Task, TaskDraft
from . import _shared as sh

NAME = "rules"
HELP = "Manage forwarding tasks: list, show, edit, pause, clone, settings, sources"


def _truncate(value: Any, width: int = 200) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sh.truncate(str(value), width)


def _confirm(ctx, args, question: str) -> None:
    if getattr(args, "dry_run", False):
        return
    sh.confirm_or_fail(ctx, question)


def _dry(ctx, payload: dict[str, Any]) -> int:
    ctx.out.json({"ok": True, "dry_run": True, **payload})
    return 0


# --------------------------------------------------------------------- queries
def _list(ctx, args) -> int:
    tasks = ctx.uc.tasks.list(
        state=getattr(args, "state", "") or "",
        source=getattr(args, "source", "") or "",
        enabled_only=bool(getattr(args, "enabled_only", False)),
    )
    if ctx.want_json():
        ctx.out.json({"count": len(tasks), "rules": [task.to_json() for task in tasks]})
        return 0
    ctx.out.table(sh.RULE_HEADERS, [sh.rule_row(task.to_json()) for task in tasks], empty="no tasks")
    ctx.out.line()
    ctx.out.line(f"  {len(tasks)} task(s)")
    return 0


def _show(ctx, args) -> int:
    task = ctx.uc.tasks.find(args.ref)
    if ctx.want_json():
        ctx.out.json(task.to_json())
        return 0
    sh.print_rule(ctx, task.to_json(), deep=True)
    return 0


def _history(ctx, args) -> int:
    """A task's own log history — the first place to look when it "does nothing"."""
    query = LogQuery(
        level=str(getattr(args, "level", "") or ""),
        search=str(getattr(args, "search", "") or ""),
        hours=int(getattr(args, "hours", 0) or 0),
        limit=int(getattr(args, "limit", 50) or 50),
        offset=int(getattr(args, "offset", 0) or 0),
    )
    page = ctx.uc.tasks.logs(args.ref, query)
    if ctx.want_json():
        ctx.out.json(page.to_json())
        return 0
    import time

    rows = [
        [
            time.strftime("%m-%d %H:%M:%S", time.localtime(entry.timestamp)) if entry.timestamp else "—",
            entry.level or "INFO",
            entry.service or "—",
            sh.truncate(entry.message or "—", 70),
        ]
        for entry in page.entries
    ]
    ctx.out.table(("TIME", "LEVEL", "SERVICE", "MESSAGE"), rows, empty="no history for this task")
    ctx.out.line()
    ctx.out.line(f"  {len(page.entries)} of {page.total} record(s)")
    return 0


# ------------------------------------------------------------------- mutations
def _create(ctx, args) -> int:
    draft = TaskDraft(
        source=args.source,
        target=args.target,
        name=getattr(args, "name", "") or "",
        session_id=getattr(args, "session", "") or "",
        source_title=getattr(args, "source_title", "") or "",
        target_title=getattr(args, "target_title", "") or "",
        enabled=not bool(getattr(args, "no_enable", False)),
        extra=sh.settings_from_args(args),
    )
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"would_create": draft.to_payload()})
    outcome = ctx.uc.tasks.create(draft)
    if ctx.want_json():
        ctx.out.json(outcome.to_json())
        return 0
    ctx.out.ok(outcome.detail)
    source = outcome.follow_up.get("source") or ""
    target = outcome.follow_up.get("target") or ""
    if source or target:
        ctx.out.kv([("source", source or "—"), ("target", target or "—")])
    return 0


def _update(ctx, args) -> int:
    changes: dict[str, Any] = dict(sh.settings_from_args(args))
    if getattr(args, "name", ""):
        changes["name"] = args.name
    if getattr(args, "source", ""):
        changes["source_chat_id"] = args.source
    if getattr(args, "target", ""):
        changes["target_chat_id"] = args.target
    if not changes:
        raise UsageError("nothing to update", hint="pass --set key=value, --name, --source or --target")
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"task": args.ref, "patch": changes})
    outcome = ctx.uc.tasks.update(args.ref, changes)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _delete(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        task = ctx.uc.tasks.find(args.ref)
        return _dry(ctx, {"would_delete": task.id, "name": task.name})
    task = ctx.uc.tasks.find(args.ref)
    _confirm(ctx, args, f"delete task {task.label}?")
    outcome = ctx.uc.tasks.delete(task.id)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _lifecycle(verb: str):
    def handler(ctx, args) -> int:
        if getattr(args, "dry_run", False):
            task = ctx.uc.tasks.find(args.ref)
            return _dry(ctx, {"task": task.id, "action": verb})
        outcome = ctx.uc.tasks.set_state(args.ref, verb)
        ctx.out.emit(outcome.to_json(), outcome.detail)
        return 0

    return handler


def _duplicate(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        task = ctx.uc.tasks.find(args.ref)
        return _dry(ctx, {"would_duplicate": task.id})
    outcome = ctx.uc.tasks.duplicate(args.ref)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _path(ctx, args) -> int:
    path = RoutePath(kind=args.path, intermediate=getattr(args, "intermediate", "") or "")
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"task": args.ref, "route_path": path.to_payload()})
    outcome = ctx.uc.tasks.set_route_path(args.ref, path)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _simulate(ctx, args) -> int:
    text = args.text
    if getattr(args, "file", ""):
        text = ctx.repo.path(args.file).read_text(encoding="utf-8")
    if not text:
        raise UsageError("provide --text or --file", hint="e.g. --text 'hello world'")
    result = ctx.uc.tasks.simulate(args.ref, text, sender=getattr(args, "sender", "") or "")
    if ctx.want_json():
        ctx.out.json(result.to_json())
        return 0
    ctx.out.title("simulation")
    verdict = "would forward" if result.would_forward else "would not forward"
    ctx.out.kv(
        [
            ("matched", "yes" if result.matched else "no"),
            ("action", result.action or "—"),
            ("verdict", verdict),
        ]
    )
    if getattr(args, "show", False) and result.transformed_text:
        ctx.out.line()
        ctx.out.line(result.transformed_text)
    for note in result.notes:
        ctx.out.hint(note)
    return 0


# -------------------------------------------------------------------- settings
def _settings_schema(ctx, args) -> int:
    payload = ctx.uc.tasks.settings_schema(group=getattr(args, "settings_group", "") or "")
    if ctx.want_json():
        ctx.out.json(payload["payload"])
        return 0
    specs = payload["settings"]
    rows = []
    for spec in specs:
        default = spec.get("default")
        if isinstance(default, (dict, list)):
            default = json.dumps(default, ensure_ascii=False)
        rows.append(
            [
                str(spec.get("key", "")),
                str(spec.get("kind", "")),
                sh.truncate(str(default), 22),
                str(spec.get("group", "")),
                sh.truncate(str(spec.get("label", "")), 30),
            ]
        )
    ctx.out.table(("KEY", "KIND", "DEFAULT", "GROUP", "LABEL"), rows)
    ctx.out.line()
    registered = payload["payload"].get("count", len(payload["settings"]))
    ctx.out.line(f"  {len(rows)} setting(s) — {registered} registered")
    groups = payload["payload"].get("groups") or []
    if groups:
        ctx.out.line("  groups: " + ", ".join(str(group) for group in groups))
    return 0


def _settings_get(ctx, args) -> int:
    if getattr(args, "effective", False):
        task, stored, effective = ctx.uc.tasks.effective_settings(args.ref)
        if ctx.want_json():
            ctx.out.json({"task": task.id, "settings": stored, "effective": effective})
            return 0
        ctx.out.title(f"effective settings · {task.label}")
        rows = [[key, sh.truncate(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value), 60)] for key, value in sorted(effective.items())]
        ctx.out.table(("SETTING", "EFFECTIVE VALUE"), rows, empty="nothing to show")
        ctx.out.line()
        ctx.out.line(f"  {len(stored)} override(s) on this task; the rest are inherited")
        return 0
    if getattr(args, "key", ""):
        task, value = ctx.uc.tasks.setting(args.ref, args.key)
        human = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ctx.out.emit({"task": task.id, "key": args.key, "value": value}, f"{args.key} = {human}")
        return 0
    task, settings = ctx.uc.tasks.settings(args.ref)
    if ctx.want_json():
        ctx.out.json({"task": task.id, "settings": settings})
        return 0
    ctx.out.title(f"settings · {task.label}")
    if not settings:
        ctx.out.line("  (no per-task overrides — defaults apply)")
        return 0
    rows = [
        [key, sh.truncate(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value), 60)]
        for key, value in sorted(settings.items())
    ]
    ctx.out.table(("SETTING", "VALUE"), rows)
    return 0


def _settings_set(ctx, args) -> int:
    values = sh.settings_from_args(args)
    if not values:
        raise UsageError("nothing to set", hint="pass key=value pairs, e.g. clean_buttons=true")
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"task": args.ref, "settings": values, "replace_all": bool(args.replace)})
    outcome = ctx.uc.tasks.set_settings(args.ref, values, replace_all=bool(getattr(args, "replace", False)))
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _settings_reset(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"task": args.ref, "reset": list(args.keys)})
    outcome = ctx.uc.tasks.reset_settings(args.ref, list(args.keys))
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


# --------------------------------------------------------------------- sources
def _sources_list(ctx, args) -> int:
    task, sources, names = ctx.uc.tasks.sources(args.ref)
    if ctx.want_json():
        ctx.out.json({"task": task.id, "sources": sources, "names": names})
        return 0
    rows = [[str(index), chat, names[index] if index < len(names) else ""] for index, chat in enumerate(sources)]
    ctx.out.table(("INDEX", "CHAT ID", "TITLE"), rows, empty="no sources")
    return 0


def _sources_add(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        task, sources, _ = ctx.uc.tasks.sources(args.ref)
        return _dry(ctx, {"task": task.id, "sources": [*sources, args.chat]})
    outcome = ctx.uc.tasks.add_source(args.ref, args.chat, title=getattr(args, "title", "") or "")
    for warning in outcome.warnings:
        ctx.out.warn(warning.message)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _sources_remove(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        task, sources, _ = ctx.uc.tasks.sources(args.ref)
        return _dry(ctx, {"task": task.id, "sources": [chat for chat in sources if chat != args.chat]})
    outcome = ctx.uc.tasks.remove_source(args.ref, args.chat)
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


def _sources_set(ctx, args) -> int:
    if getattr(args, "dry_run", False):
        return _dry(ctx, {"task": args.ref, "sources": list(args.chats)})
    outcome = ctx.uc.tasks.set_sources(args.ref, list(args.chats))
    ctx.out.emit(outcome.to_json(), outcome.detail)
    return 0


# ---------------------------------------------------------------------- wiring
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

    p = sub.add_parser("logs", aliases=["history"], help="this task's own log history", parents=parents)
    p.add_argument("ref", help="task id, id prefix or name")
    p.add_argument("-n", "--limit", type=int, default=50, help="max records")
    p.add_argument("--offset", type=int, default=0, help="skip this many records")
    p.add_argument("--level", default="", help="filter by severity")
    p.add_argument("-s", "--search", default="", help="search the message text")
    p.add_argument("--hours", type=int, default=0, help="only the last N hours")
    p.set_defaults(func=_history)

    p = sub.add_parser("create", help="create a task", parents=parents)
    p.add_argument("--source", required=True, help="source chat id or @username")
    p.add_argument("--target", required=True, help="target chat id or @username")
    p.add_argument("--name", default="", help="human label for the task")
    p.add_argument("--source-title", default="", help="display title for the source")
    p.add_argument("--target-title", default="", help="display title for the target")
    p.add_argument("--session", default="", help="telegram session id to forward with")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="extra field (repeatable)")
    p.add_argument("--no-enable", action="store_true", help="create it disabled")
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
        ("enable", "enable a task (a state, not a flip)"),
        ("disable", "disable a task (a state, not a flip)"),
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
    sp.add_argument("--group", dest="settings_group", default="", help="only this group (e.g. content_cleanup)")
    sp.set_defaults(func=_settings_schema)

    sp = settings_sub.add_parser("get", help="show a task's settings", parents=parents)
    sp.add_argument("ref", help="task id, id prefix or name")
    sp.add_argument("key", nargs="?", default="", help="one setting key")
    sp.add_argument("--effective", action="store_true", help="also show inherited values and their source")
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
