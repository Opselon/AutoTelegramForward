"""The generic CRUD command set, driven by a :class:`ResourceSpec`.

AI configurations, prompt templates and filter rules are the same shape of
problem: a per-owner collection behind ``/api/<name>`` with an id and a name.
Rather than three near-identical command modules (which is how the surfaces
drift apart — one gets `--json` right, another forgets the id prefix), the verbs
are registered here once and each module adds only what is genuinely its own.

Every verb goes through :class:`atfcli.application.use_cases.resources.Resources`,
so the local refusals (a missing required field, an unknown `--set` key) and the
echo warning (a field the API silently discarded) are shared by all three.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.resources import ResourceSpec
from . import _shared as sh


def use_case(ctx, spec: ResourceSpec):
    """The Resources use case bundle for a spec (the container owns the wiring)."""
    return ctx.uc.resources_for(spec)


def _render(value: Any, width: int = 60) -> str:
    if isinstance(value, (dict, list)):
        return sh.truncate(json.dumps(value, ensure_ascii=False), width)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return sh.truncate(str(value), width) if value not in (None, "") else "—"


def _collection_verbs(spec: ResourceSpec):
    """The verbs every collection gets: list, show, create, update, delete."""

    def do_list(ctx, args) -> int:
        rows = use_case(ctx, spec).list()
        keys = _columns(spec)
        if ctx.want_json():
            ctx.out.json(
                {
                    "count": len(rows),
                    spec.plural: [{key: row.raw.get(key) for key in keys} | {"id": row.id} for row in rows],
                }
            )
            return 0
        table = [[str(row.raw.get(key) or "—") if key != "id" else row.id[:8] for key in keys] for row in rows]
        ctx.out.table(tuple(key.upper() for key in keys), table, empty=f"no {spec.plural} found")
        ctx.out.line()
        ctx.out.line(f"  {len(rows)} {spec.plural}")
        return 0

    def do_show(ctx, args) -> int:
        row = use_case(ctx, spec).find(args.ref)
        if ctx.want_json():
            ctx.out.json(row.to_json())
            return 0
        ctx.out.title(f"{spec.name} {row.id[:8]}")
        ctx.out.kv([(key, _render(value, 120)) for key, value in sorted(row.raw.items())])
        return 0

    def do_create(ctx, args) -> int:
        values = _values_from_args(args, spec)
        resources = use_case(ctx, spec)
        _warn_unknown(ctx, spec, values)
        resources.require_fields(values)
        if getattr(args, "dry_run", False):
            ctx.out.json({"ok": True, "dry_run": True, "would_create": values})
            return 0
        outcome = resources.create(values, expected_version=int(getattr(args, "version", 0) or 0))
        _emit(ctx, outcome)
        return 0

    def do_update(ctx, args) -> int:
        values = _values_from_args(args, spec)
        if not values:
            raise sh.UsageError(
                "nothing to update",
                hint="pass --set key=value, or one of the documented flags",
            )
        resources = use_case(ctx, spec)
        _warn_unknown(ctx, spec, values)
        if getattr(args, "dry_run", False):
            row = resources.find(args.ref)
            ctx.out.json({"ok": True, "dry_run": True, "id": row.id, "patch": values})
            return 0
        outcome = resources.update(args.ref, values, expected_version=int(getattr(args, "version", 0) or 0))
        _emit(ctx, outcome)
        return 0

    def do_delete(ctx, args) -> int:
        resources = use_case(ctx, spec)
        _warn_unknown(ctx, spec, values)
        if getattr(args, "dry_run", False):
            row = resources.find(args.ref)
            ctx.out.json({"ok": True, "dry_run": True, "would_delete": row.id})
            return 0
        row = resources.find(args.ref)
        sh.confirm_or_fail(ctx, f"delete {spec.name} {row.label or row.id[:8]}?")
        outcome = resources.delete(row.id)
        _emit(ctx, outcome)
        return 0

    return {"list": do_list, "show": do_show, "create": do_create, "update": do_update, "delete": do_delete}


def _emit(ctx, outcome) -> None:
    for warning in outcome.warnings:
        ctx.out.warn(warning.message)
        if warning.hint:
            ctx.out.hint(warning.hint)
    ctx.out.emit(outcome.to_json(), outcome.detail)


def _columns(spec: ResourceSpec) -> list[str]:
    columns = ["id", *spec.label_keys[:2]]
    return [column for index, column in enumerate(columns) if column not in columns[:index]]


def _values_from_args(args, spec: ResourceSpec | None = None) -> dict[str, Any]:
    """Collect the editable fields from flags, then overlay `--set` pairs."""
    values: dict[str, Any] = {}
    for dest, field in (getattr(args, "field_map", None) or {}).items():
        value = getattr(args, dest, None)
        if value not in (None, "", []):
            values[field] = value
    values.update(sh.settings_from_args(args))
    return values


def _warn_unknown(ctx, spec: ResourceSpec, values: dict[str, Any]) -> None:
    """Name keys this collection does not document, *before* the write.

    The API discards unknown fields silently (protojson `DiscardUnknown`), so a
    typo like `apiKeY` is accepted and ignored. The post-write echo check catches
    it too, but a dry run never writes — and a dry run is exactly when someone is
    checking their spelling.
    """
    known = set(spec.editable) | set(spec.id_keys) | {"version"}
    for key in sorted(values):
        if key not in known:
            ctx.out.warn(
                f"{key!r} is not a documented {spec.name} field — "
                f"known: {', '.join(sorted(spec.editable))}"
            )


def register(
    subparsers,
    parents,
    spec: ResourceSpec,
    *,
    aliases: dict[str, list[str]] | None = None,
    extra_help: str = "",
) -> dict[str, Any]:
    """Register the CRUD verbs for one collection and return the handlers.

    `aliases` maps a verb to its alternative spellings (``{"list": ["ls"]}``);
    the returned dict lets a module wrap or extend a handler (prompts add a
    `test` verb, AI configs add `providers`/`check-key`).
    """
    verbs = _collection_verbs(spec)
    aliases = aliases or {}
    parsers: dict[str, Any] = {}

    p = subparsers.add_parser(
        "list", aliases=aliases.get("list", ["ls"]), help=f"list {spec.plural}", parents=parents
    )
    p.set_defaults(func=verbs["list"])
    parsers["list"] = p

    p = subparsers.add_parser("show", help=f"show one {spec.name} in full", parents=parents)
    p.add_argument("ref", help=f"{spec.name} id, unique id prefix or name")
    p.set_defaults(func=verbs["show"])
    parsers["show"] = p

    p = subparsers.add_parser("create", help=f"create a {spec.name}", parents=parents)
    _add_field_flags(p, spec, creating=True)
    p.set_defaults(func=verbs["create"])
    parsers["create"] = p

    p = subparsers.add_parser(
        "update", aliases=aliases.get("update", ["edit"]), help=f"update a {spec.name}", parents=parents
    )
    p.add_argument("ref", help=f"{spec.name} id, unique id prefix or name")
    _add_field_flags(p, spec, creating=False)
    p.set_defaults(func=verbs["update"])
    parsers["update"] = p

    p = subparsers.add_parser("delete", aliases=aliases.get("delete", ["rm"]), help=f"delete a {spec.name}", parents=parents)
    p.add_argument("ref", help=f"{spec.name} id, unique id prefix or name")
    p.set_defaults(func=verbs["delete"])
    parsers["delete"] = p

    if extra_help:
        for parser in parsers.values():
            parser.epilog = extra_help
    return verbs


def _add_field_flags(parser, spec: ResourceSpec, *, creating: bool) -> None:
    """One flag per editable field, plus the always-available `--set` escape hatch."""
    required = set(spec.required)
    for field in spec.editable:
        # The flag is the human spelling (`--api-key`); `dest` is underscored so
        # `args.api_key` is valid Python; the wire keeps camelCase (`apiKey`).
        label = spec.flag_of(field).replace("-", " ")
        suffix = " (required)" if creating and field in required else ""
        parser.add_argument(
            f"--{spec.flag_of(field)}",
            dest=spec.flag_of(field).replace("-", "_"),
            default=None,
            metavar="VALUE",
            help=label + suffix,
        )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="any other field (repeatable); unknown fields are reported after the write",
    )
    parser.add_argument("--version", type=int, default=0, help="expected version, for optimistic concurrency")
    # set_defaults, not setattr: the parser object is not what dispatch sees —
    # only values copied into the namespace by set_defaults reach the handler.
    parser.set_defaults(field_map=spec.flag_map())
