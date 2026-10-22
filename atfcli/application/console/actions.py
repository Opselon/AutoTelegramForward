"""The console's action registry: one row per capability, one responsibility each.

Every action here is a thin adapter over a use case — it asks for what the use
case needs, calls it, and renders the outcome. Nothing validates business rules
of its own: prompts carry `domain.validators` / `domain.envschema` rules, and the
use cases re-check what they are given. That is what keeps "the console refused
it" and "the CLI refused it" the same behaviour instead of two dialects.

Ordering matters: the registry is the menu, and an operator scanning it should
see the read-only state of the world first, the destructive verbs last.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ...domain import envschema, validators
from ...domain.secrets import is_secret_name
from .contracts import (
    DESTRUCTIVE,
    READ,
    WRITE,
    Action,
    ActionContext,
    Confirmation,
    Prompt,
)
from .values import coerce

#: Menu order, and the only groups the console renders.
GROUP_ORDER: tuple[str, ...] = (
    "Overview",
    "Users",
    "Administrators",
    "Platform settings",
    "Extensions",
    "Configuration (.env)",
    "Broadcast",
)


# ------------------------------------------------------------------ rendering
def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={_render(item)}" for key, item in value.items()) or "—"
    return str(value) if value not in (None, "") else "—"


def _scalar(value: Any) -> str:
    """A default only for values a prompt can show back; JSON is summarised."""
    if isinstance(value, (Mapping, list, tuple)):
        return ""
    return _render(value)


def _mask(document: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [
        (str(key), "[REDACTED]" if is_secret_name(str(key)) else _render(value))
        for key, value in sorted(document.items())
    ]


# ------------------------------------------------------------------- overview
def _overview(ctx: ActionContext) -> None:
    data = ctx.uc.admin.overview()
    ctx.io.title("System overview")
    scalars = [(str(key), _render(value)) for key, value in data.items() if not isinstance(value, (Mapping, list))]
    ctx.io.table(("METRIC", "VALUE"), scalars, empty="the server reported no metrics")
    for key, value in data.items():
        if not isinstance(value, (Mapping, list)) or not value:
            continue
        ctx.io.line()
        ctx.io.line("  " + key.replace("_", " ").upper())
        if isinstance(value, Mapping):
            ctx.io.table(("NAME", "VALUE"), _mask(value))
        else:
            ctx.io.line("    " + _render(value))


def _health(ctx: ActionContext) -> None:
    services = ctx.uc.ops.service_health()
    ctx.io.title("Service health")
    rows = [
        [
            service.name,
            "online" if service.online else "offline",
            service.detail or "—",
            f"{service.latency_ms} ms" if service.latency_ms else "—",
        ]
        for service in services
    ]
    ctx.io.table(("SERVICE", "STATE", "DETAIL", "LATENCY"), rows, empty="no services reported")
    readiness = ctx.uc.ops.readiness()
    ctx.io.line()
    if readiness.ready:
        ctx.io.ok(f"ready — {readiness.deployment or 'this deployment'} reports every dependency")
    else:
        missing = ", ".join(readiness.missing_required) or "a dependency is unreachable"
        ctx.io.fail(f"not ready — {missing}")
        for step in readiness.next_steps[:5]:
            ctx.io.hint(f"  next: {step}")


# ---------------------------------------------------------------------- users
def _user_id_prompt(label: str = "user id") -> Prompt:
    return Prompt(
        label=label,
        hint="the numeric Telegram id (e.g. 5094837833)",
        validate=validators.valid_telegram_user_id,
    )


def _users_list(ctx: ActionContext) -> None:
    limit = ctx.io.ask(
        Prompt(
            "how many users to show",
            default="20",
            required=False,
            validate=lambda raw: validators.valid_int(raw, minimum=1, maximum=200, label="limit"),
        )
    )
    users = ctx.uc.admin.users(limit=int(limit or 20))
    ctx.io.title(f"Registered users ({len(users)})")
    ctx.io.table(
        ("USER ID", "USERNAME", "ROLE", "STATUS", "TASKS"),
        [
            [user.id, user.username or "—", user.role or "user", "active" if user.active else "suspended", str(user.tasks)]
            for user in users
        ],
        empty="no users registered",
    )


def _user_status(ctx: ActionContext, status: str) -> None:
    user_id = ctx.io.ask(_user_id_prompt())
    if not ctx.confirm(f"set {user_id} to {status}?", default=False):
        ctx.io.warn("left unchanged")
        return
    outcome = ctx.uc.admin.set_user_status(user_id, status)
    ctx.io.ok(outcome.detail)


def _user_suspend(ctx: ActionContext) -> None:
    _user_status(ctx, "suspended")


def _user_activate(ctx: ActionContext) -> None:
    _user_status(ctx, "active")


# -------------------------------------------------------------- administrators
def _admins_list(ctx: ActionContext) -> None:
    admins = ctx.uc.admin.admins()
    ctx.io.title(f"Administrators ({len(admins)})")
    ctx.io.table(
        ("USER ID", "USERNAME", "ROLE"),
        [[admin.id, admin.username or "—", admin.role or "admin"] for admin in admins],
        empty="no administrators registered",
    )


def _admin_promote(ctx: ActionContext) -> None:
    user_id = ctx.io.ask(_user_id_prompt())
    username = ctx.io.ask(Prompt("username (optional)", required=False))
    outcome = ctx.uc.admin.add_admin(user_id, username=username)
    ctx.io.ok(outcome.detail)


def _admin_revoke(ctx: ActionContext) -> None:
    user_id = ctx.io.ask(_user_id_prompt())
    confirmation = Confirmation(
        question=f"revoke administrative access from {user_id}?",
        token=user_id,
        hint=f"type {user_id} exactly to confirm",
    )
    if not ctx.io.require(confirmation):
        ctx.io.warn("left unchanged")
        return
    outcome = ctx.uc.admin.remove_admin(user_id, confirm=True)
    ctx.io.ok(outcome.detail)


# --------------------------------------------------------------- settings
def _settings_show(ctx: ActionContext) -> None:
    settings = ctx.uc.admin.settings()
    ctx.io.title("Platform settings")
    ctx.io.table(("SETTING", "VALUE"), _mask(settings), empty="the server reports no settings")


def _setting_key(value: str) -> str | None:
    return validators.valid_identifier(value)


def _settings_set(ctx: ActionContext) -> None:
    key = ctx.io.ask(Prompt("setting name", hint="e.g. maintenance_mode", validate=_setting_key))
    current = ctx.uc.admin.settings().get(key, "")
    default = "" if is_secret_name(key) else _scalar(current)
    value = ctx.io.ask(
        Prompt(
            f"value for {key}",
            default=default,
            hint="true / false / a number / text / JSON",
            validate=lambda raw: validators.valid_text(raw, maximum=4096, label="value"),
        )
    )
    outcome = ctx.uc.admin.save_settings({key: coerce(value)})
    ctx.io.ok(outcome.detail)


# --------------------------------------------------------------- extensions
def _extensions_list(ctx: ActionContext) -> None:
    rows = ctx.uc.admin.extensions()
    ctx.io.title(f"Extensions ({len(rows)})")
    ctx.io.table(
        ("EXTENSION", "STATUS", "VERSION"),
        [
            [
                str(row.get("id", "—")),
                "enabled" if row.get("enabled") else "disabled",
                str(row.get("version") or "—"),
            ]
            for row in rows
        ],
        empty="no extensions installed",
    )


def _extension_toggle(ctx: ActionContext) -> None:
    ref = ctx.io.ask(
        Prompt(
            "extension id",
            hint="an id or a unique prefix (see `extensions`)",
            validate=lambda raw: validators.valid_identifier(raw),
        )
    )
    outcome = ctx.uc.admin.toggle_extension(ref)
    ctx.io.ok(outcome.detail)


# ------------------------------------------------------------ configuration
def _config_audit(ctx: ActionContext) -> None:
    report = ctx.uc.env.validate()
    ctx.io.title("Configuration audit")
    errors = [finding for finding in report.findings if finding.severity == "error"]
    warnings = [finding for finding in report.findings if finding.severity == "warning"]
    ctx.io.table(
        ("FILE", "KEYS", "ERRORS", "WARNINGS"),
        [[report.path, str(report.keys), str(len(errors)), str(len(warnings))]],
    )
    for finding in errors + warnings:
        render = ctx.io.fail if finding.severity == "error" else ctx.io.warn
        render(f"  {finding.title}")
        if finding.remediation:
            ctx.io.hint(f"        fix: {finding.remediation}")
    ctx.io.line()
    if report.ok:
        ctx.io.ok("configuration is valid")
    else:
        ctx.io.fail("configuration has errors — the stack may not start")


def _config_key(value: str) -> str | None:
    """Only documented keys, so a guided edit cannot invent a setting."""
    problem = envschema.validate_key(value)
    if problem:
        return problem
    if envschema.spec_for(value) is None:
        return (
            f"{value} is not a documented setting"
            + validators.suggest(value, tuple(envschema.BY_NAME))
        )
    return None


def _config_show(ctx: ActionContext) -> None:
    key = ctx.io.ask(Prompt("setting name", hint="e.g. ATF_WEB_PORT", validate=_config_key))
    spec = envschema.spec_for(key)
    values = ctx.uc.env.values()
    shown = "[REDACTED]" if (spec and spec.masked) or is_secret_name(key) else _render(values.get(key, ""))
    ctx.io.table(("SETTING", "VALUE", "GROUP"), [[key, shown or "(unset)", spec.group if spec else "—"]])
    if spec:
        ctx.io.hint(envschema.describe(key))


def _config_set(ctx: ActionContext) -> None:
    key = ctx.io.ask(Prompt("setting name", hint="e.g. ATF_WEB_PORT", validate=_config_key))
    spec = envschema.spec_for(key)
    value = ctx.io.ask(
        Prompt(
            f"value for {key}",
            hint=envschema.describe(key) or "the new value",
            secret=bool(spec and spec.masked),
            validate=(lambda raw: spec.validate(raw)) if spec else None,
        )
    )
    plan = ctx.uc.env.plan({key: value})
    for line in plan.diff_lines():
        ctx.io.line(line)
    outcome = ctx.uc.env.apply(plan)
    ctx.io.ok(outcome.detail)


def _config_backups(ctx: ActionContext) -> None:
    entries = ctx.uc.env.backups()
    ctx.io.title(f"Backups ({len(entries)})")
    ctx.io.table(
        ("BACKUP", "CREATED", "KEYS", "SIZE"),
        [[entry.name, entry.created_at, str(entry.keys), f"{entry.size} B"] for entry in entries],
        empty="no backups yet (one is written before every change)",
    )


def _config_restore(ctx: ActionContext) -> None:
    entries = ctx.uc.env.backups()
    if not entries:
        ctx.io.warn("no backups to restore")
        return
    _config_backups(ctx)
    ctx.io.line()
    ctx.io.hint(f"latest: {entries[0].name}")
    name = ctx.io.ask(
        Prompt(
            "backup to restore",
            default=entries[0].name,
            validate=lambda raw: None if any(entry.name == raw for entry in entries) else "no such backup",
        )
    )
    confirmation = Confirmation(
        question=f"replace .env with backup {name}?",
        token=name,
        hint=f"type {name} exactly to confirm",
    )
    if not ctx.io.require(confirmation):
        ctx.io.warn("left unchanged")
        return
    outcome = ctx.uc.env.restore(name)
    ctx.io.ok(outcome.detail)


# ------------------------------------------------------------------ broadcast
def _broadcast(ctx: ActionContext) -> None:
    message = ctx.io.ask(
        Prompt(
            "message",
            hint="sent to every user of the platform",
            validate=lambda raw: validators.valid_text(raw, maximum=2000, label="message"),
        )
    )
    confirmation = Confirmation(
        question=f"send this to every user?\n\n    {message[:400]}",
        token="BROADCAST",
        hint="type BROADCAST to confirm",
    )
    if not ctx.io.require(confirmation):
        ctx.io.warn("cancelled — nothing was sent")
        return
    outcome = ctx.uc.admin.broadcast(message, confirm=True)
    ctx.io.ok(outcome.detail)


# ------------------------------------------------------------------ registry
def actions() -> tuple[Action, ...]:
    """The registry, in menu order. The single place a capability is declared."""
    return (
        Action("overview", "system overview and counts", "Overview", _overview, keywords=("status",)),
        Action("health", "service health and readiness", "Overview", _health, keywords=("doctor",)),
        Action("users", "list users", "Users", _users_list, keywords=("ls",)),
        Action("user-suspend", "suspend a user", "Users", _user_suspend, danger=WRITE),
        Action("user-activate", "activate a user", "Users", _user_activate, danger=WRITE),
        Action("admins", "list administrators", "Administrators", _admins_list),
        Action("admin-promote", "promote a user to administrator", "Administrators", _admin_promote, danger=WRITE),
        Action(
            "admin-revoke",
            "revoke administrative access",
            "Administrators",
            _admin_revoke,
            danger=DESTRUCTIVE,
        ),
        Action("settings-show", "show platform settings", "Platform settings", _settings_show, keywords=("settings",)),
        Action("settings-set", "change a platform setting", "Platform settings", _settings_set, danger=WRITE),
        Action("extensions", "list extensions", "Extensions", _extensions_list),
        Action("extension-toggle", "enable or disable an extension", "Extensions", _extension_toggle, danger=WRITE),
        Action("config-audit", "audit .env against the schema", "Configuration (.env)", _config_audit, keywords=("validate",)),
        Action("config-show", "show one .env setting", "Configuration (.env)", _config_show),
        Action("config-set", "change one .env setting (validated, backed up)", "Configuration (.env)", _config_set, danger=WRITE),
        Action("config-backups", "list .env rollback points", "Configuration (.env)", _config_backups),
        Action(
            "config-restore",
            "restore a .env backup",
            "Configuration (.env)",
            _config_restore,
            danger=DESTRUCTIVE,
        ),
        Action(
            "broadcast",
            "message every user",
            "Broadcast",
            _broadcast,
            danger=DESTRUCTIVE,
        ),
    )


def by_id(actions_: Sequence[Action], ref: str) -> tuple[Action | None, str]:
    """Resolve `ref` to one action; the second value explains a refusal."""
    matches = [action for action in actions_ if action.matches(ref)]
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"no action matches {ref!r}"
    return None, f"{ref!r} matches {len(matches)} actions: " + ", ".join(a.id for a in matches)


__all__ = ["GROUP_ORDER", "actions", "by_id"]
