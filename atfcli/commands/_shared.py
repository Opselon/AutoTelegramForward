"""Helpers shared by the command modules.

Commands are deliberately thin: resolve the argument, call the API or a shell
command, render the result. Anything that needs to happen in more than one
command lives here so the behaviour cannot drift between groups.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..console import truncate
from ..context import collect_settings, parse_jsonish
from ..errors import AtfError, NotFoundError, UsageError


class SubParsers:
    """Small façade over argparse so command modules stay readable."""

    def __init__(self, subparsers, parents: Sequence[argparse.ArgumentParser]) -> None:
        self._sub = subparsers
        self._parents = list(parents)

    def command(
        self,
        name: str,
        help_text: str,
        *,
        aliases: Sequence[str] = (),
        epilog: str = "",
    ) -> argparse.ArgumentParser:
        parser = self._sub.add_parser(
            name,
            help=help_text,
            description=help_text,
            aliases=list(aliases),
            parents=self._parents,
            epilog=epilog or None,
        )
        return parser


def add_group(parser: argparse.ArgumentParser, name: str = "cmd") -> argparse._SubParsersAction:
    """Attach a verb subparser to a group parser.

    Not `required=True`: bare `atf rules` should print the group's help (the
    dispatcher handles a missing `func`), not exit 2 with a usage error.
    """
    return parser.add_subparsers(dest=name, metavar="<command>")


def help_handler(parser: argparse.ArgumentParser):
    """`func` for a group parser so `atf rules settings` prints its own help."""

    def handler(ctx, args) -> int:
        parser.print_help()
        return 0

    return handler


def wants_json(ctx, args=None) -> bool:
    """`--json` from the arguments, or from the console the context carries.

    Commands are driven directly by tests with a stub context that has an `out`
    (sometimes a real Console in JSON mode) but no `want_json`; reading the flag
    from both places keeps them drivable without a special case per command.
    """
    if args is not None and getattr(args, "json", False):
        return True
    checker = getattr(ctx, "want_json", None)
    return bool(checker()) if callable(checker) else False


def want_json(ctx) -> bool:
    return bool(getattr(ctx.args, "json", False))


def out_json(ctx, payload: Any) -> None:
    ctx.out.json(payload)


# --------------------------------------------------------------------- ids
def matches_id(candidate: str, wanted: str, *, min_prefix: int = 4) -> bool:
    if not candidate or not wanted:
        return False
    if candidate == wanted:
        return True
    return len(wanted) >= min_prefix and candidate.startswith(wanted)


def resolve_rule(api, ref: str) -> dict:
    """Accept a full id, a unique id prefix, or a rule name.

    Operators copy ids out of logs and messages, so requiring the exact 36-char
    uuid (or knowing which of name/id to pass) would make every command harder
    than the task it performs.
    """
    if not ref:
        raise UsageError("a task id, id prefix or name is required")
    rules = api.get("/api/rules")
    items = _as_list(rules, "rules")
    exact = [r for r in items if r.get("id") == ref]
    if exact:
        return exact[0]
    prefix = [r for r in items if matches_id(str(r.get("id", "")), ref)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise UsageError(
            f"{ref!r} matches {len(prefix)} tasks; use a longer prefix",
            hint=", ".join(str(r.get("id", ""))[:12] for r in prefix[:5]),
        )
    by_name = [
        r
        for r in items
        if str(r.get("name", "")).strip().lower() == ref.strip().lower()
    ]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise UsageError(f"{ref!r} names {len(by_name)} tasks; use the id instead")
    raise NotFoundError(
        f"no task matches {ref!r}",
        hint="`atf rules list` shows the ids and names",
    )


def _as_list(payload: Any, key: str) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        raw = payload.get(key)
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        for fallback in ("items", "data", "results"):
            raw = payload.get(fallback)
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict)]
        if "success" not in payload:
            return [payload]
    return []


def list_of(payload: Any, key: str) -> list[dict]:
    return _as_list(payload, key)


def select(obj: dict, fields: Iterable[str] | None) -> dict:
    if not fields:
        return obj
    wanted = [f.strip() for f in fields if f.strip()]
    return {k: v for k, v in obj.items() if k in wanted}


# ------------------------------------------------------------------ values
def settings_from_args(args) -> dict:
    """Collect `--set key=value` pairs into a nested dict.

    Accepts either the parsed namespace or the raw list of pairs, because both
    call styles exist and `getattr(list, "set", [])` is `[]` — passing the list
    to a namespace-only helper silently returned no settings at all, so
    `--set clean_buttons=true` was accepted and ignored.
    """
    pairs = args if isinstance(args, (list, tuple)) else getattr(args, "set", [])
    return collect_settings(pairs or [])


def parse_bool(text: str) -> bool:
    value = parse_jsonish(text)
    if isinstance(value, bool):
        return value
    raise UsageError(f"expected true/false, got {text!r}")


def require(text: str | None, what: str) -> str:
    if not text:
        raise UsageError(f"{what} is required")
    return text


# --------------------------------------------------------------- rendering
def enabled_of(rule: dict) -> bool:
    """The rule DTO's enabled flag is `is_active`; `is_enabled` is a legacy alias.

    Every renderer goes through here so a task cannot read "on" in one column and
    "off" in another.
    """
    for key in ("is_active", "is_enabled"):
        value = rule.get(key)
        if isinstance(value, bool):
            return value
    return False


def chat_name_of(rule: dict, side: str) -> str:
    """Titles are `source_chat_name`/`target_chat_name` in the DTO."""
    return str(rule.get(f"{side}_chat_name") or rule.get(f"{side}_chat_title") or "")


def rule_row(rule: dict) -> list[str]:
    sources = rule.get("source_chat_ids") or []
    if not isinstance(sources, list) or not sources:
        sources = [rule.get("source_chat_id", "")]
    extra = max(0, len([s for s in sources if s]) - 1)
    state = "on" if enabled_of(rule) else "off"
    if rule.get("is_paused"):
        state = "paused"
    return [
        str(rule.get("id", ""))[:8],
        truncate(rule.get("name") or chat_name_of(rule, "source") or "—", 24),
        state,
        truncate(chat_name_of(rule, "source") or rule.get("source_chat_id") or "—", 22)
        + (f" +{extra}" if extra else ""),
        truncate(chat_name_of(rule, "target") or rule.get("target_chat_id") or "—", 22),
    ]


RULE_HEADERS = ("ID", "NAME", "STATE", "SOURCE", "TARGET")


def print_rule(ctx, rule: dict, *, deep: bool = False) -> None:
    ctx.out.kv(
        [
            ("id", rule.get("id", "")),
            ("name", rule.get("name") or "—"),
            ("enabled", "yes" if enabled_of(rule) else "no"),
            ("paused", "yes" if rule.get("is_paused") else "no"),
            ("source", chat_name_of(rule, "source") or rule.get("source_chat_id") or "—"),
            ("target", chat_name_of(rule, "target") or rule.get("target_chat_id") or "—"),
        ]
    )
    sources = rule.get("source_chat_ids") or []
    if isinstance(sources, list) and len([s for s in sources if s]) > 1:
        ctx.out.line()
        ctx.out.kv([(f"source[{i}]", s) for i, s in enumerate(sources) if s])
    if deep:
        for key in (
            "session_id",
            "delay_seconds",
            "auto_delete_seconds",
            "clean_buttons",
            "ai_rewrite_enabled",
            "created_at",
            "updated_at",
        ):
            if key in rule and rule[key] not in (None, ""):
                ctx.out.kv([(key, rule[key])])
        keywords = rule.get("keywords") or rule.get("detection_criteria")
        if keywords:
            ctx.out.line()
            ctx.out.kv([("criteria", truncate(json.dumps(keywords, ensure_ascii=False), 160))])


def confirm_or_fail(ctx, question: str, *, default: bool = False) -> None:
    if not ctx.repo.confirm(question, default=default):
        raise AtfError("aborted", hint="pass --yes to skip the confirmation")


def write_stdout(text: str) -> None:
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def write_file(path: Path, text: str, *, overwrite: bool = True) -> Path:
    if path.exists() and not overwrite:
        raise AtfError(f"{path} already exists", hint="pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
