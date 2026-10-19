"""Guided first-run setup: platform, questionnaire, secrets, build, verify.

One command for a machine that has never run ATF before:

    atf setup                # interactive wizard (asks, validates, then verifies)
    atf setup --check        # read-only readiness report
    atf setup --answers a.json
    atf setup --yes          # take every default, never prompt (CI / cron)

Design rules:

* Ask only what the operator actually knows, and refuse a wrong answer instead
  of storing it — every question has a validator (token shape, numeric ids,
  32-hex api hash, URL shape, domain, and a TLS email once a domain is set).
* Prove the stack works. "docker compose returned 0" is not evidence; after the
  build we inspect each of the four microservices and report state, health,
  reachability and endpoint, with a fix line for anything failing.
* Work on any host. Windows, macOS, WSL, a bare Debian box and a container all
  get machine-specific docker instructions, and two no-docker paths (source mode
  and the single-file portable build) are offered when docker is unusable.
* Never emit a secret. Generated credentials are reported as key + length, and
  operator-supplied ones as "set (n chars)" — values never reach stdout, a log
  line, or `--json`.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .. import platforms as plat
from .. import services_probe as probe
from .. import ui
from .. import validators
from ..errors import AtfError
from . import _shared as sh
from . import deploy, validate

NAME = "setup"
HELP = "One-command first-run setup: platform check, questionnaire, secrets, build, verify"

#: Crypto secrets the CLI can generate safely with no operator input.
GENERATED_KEYS: tuple[str, ...] = ("ATF_MASTER_KEY", "ATF_API_KEY")
#: Secrets only the operator can supply (from my.telegram.org / BotFather).
OPERATOR_KEYS: tuple[str, ...] = ("ATF_API_ID", "ATF_API_HASH", "ATF_BOT_TOKEN")
#: Answers that must never be echoed, logged, or put in `--json`.
MASKED_ANSWERS: tuple[str, ...] = ("bot_token", "api_hash")
SECRET_BYTES = 32
#: Local health endpoint tried when no --url is given.
LOCAL_HEALTH_URLS: tuple[str, ...] = ("http://127.0.0.1:8088/healthz", "http://127.0.0.1:8080/healthz")
#: The public Telegram Desktop api_id/api_hash pair. It is not a secret (it ships
#: in every Telegram Desktop build) and it is what most operators use for a
#: user-session bot, so it is offered as a default rather than as a puzzle.
TELEGRAM_PUBLIC_API_ID = "2040"
TELEGRAM_PUBLIC_API_HASH = "b18441a1ff607e10a989891a5462e627"
ANSWER_ENV: dict[str, str] = {
    "bot_token": "ATF_BOT_TOKEN",
    "api_id": "ATF_API_ID",
    "api_hash": "ATF_API_HASH",
    "admin_id": "ADMIN_USER_ID",
    "web_url": "ATF_WEB_URL",
    "tls_domain": "ATF_TLS_DOMAIN",
    "tls_email": "ATF_TLS_EMAIL",
}
#: Keys the wizard is allowed to write into `.env` (nothing else is touched).
WRITABLE_KEYS: tuple[str, ...] = tuple(ANSWER_ENV.values()) + ("ADMIN_IDS",)


@dataclass
class Step:
    name: str
    status: str  # ok | skip | warn | fail
    detail: str = ""


@dataclass
class Runbook:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> Step:
        step = Step(name, status, detail)
        self.steps.append(step)
        return step

    @property
    def failed(self) -> bool:
        return any(s.status == "fail" for s in self.steps)

    def to_json(self) -> list[dict[str, str]]:
        return [{"name": s.name, "status": s.status, "detail": s.detail} for s in self.steps]


# ------------------------------------------------------------------ env writing
def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_env(ctx) -> dict[str, str]:
    env_path = ctx.repo.path(".env")
    if env_path.exists():
        try:
            return _parse_env(env_path.read_text(encoding="utf-8"))
        except OSError:
            return {}
    return {}


def _put(text: str, key: str, value: str) -> str:
    """Replace `key` in place, or append it — comments and order preserved."""
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.partition("=")[0].strip() == key:
            lines[i] = f"{key}={value}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _needs_value(current: str) -> bool:
    value = (current or "").strip().strip('"').strip("'")
    if not value:
        return True
    return bool(validate.PLACEHOLDER.match(value))


def bootstrap_env(ctx, args, runbook: Runbook, *, force: bool = False) -> dict[str, str]:
    """Create/fill `.env`, generating local crypto secrets only."""
    env_path = ctx.repo.path(".env")
    example = ctx.repo.path(".env.example")
    created = False

    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
    elif example.exists():
        text = example.read_text(encoding="utf-8")
        created = True
    else:
        text = "# generated by atf setup\n"
        created = True

    current = _parse_env(text)
    generated: dict[str, str] = {}
    for key in GENERATED_KEYS:
        if force or _needs_value(current.get(key, "")):
            value = secrets.token_urlsafe(SECRET_BYTES)
            ctx.out.register_secret(value)
            text = _put(text, key, value)
            generated[key] = f"{len(value)} chars"

    admin_id = (getattr(args, "admin_id", "") or "").strip()
    if admin_id:
        text = _put(text, "ADMIN_USER_ID", admin_id)
        text = _put(text, "ADMIN_IDS", admin_id)

    if ctx.repo.dry_run:
        runbook.add("env", "skip", f"[dry-run] would write {env_path.name}")
        return generated

    if created or generated or admin_id:
        env_path.write_text(text, encoding="utf-8")
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: never group/world readable
        detail = f"{'created' if created else 'updated'} .env (mode 0600)"
        if generated:
            detail += " — generated " + ", ".join(f"{k} ({v})" for k, v in generated.items())
        runbook.add("env", "ok", detail)
    else:
        runbook.add("env", "skip", ".env already complete")

    values = read_env(ctx)
    missing = [k for k in OPERATOR_KEYS if _needs_value(values.get(k, ""))]
    if missing:
        runbook.add("env-credentials", "warn", f"operator must fill: {', '.join(missing)} in .env")
    else:
        runbook.add("env-credentials", "ok", "Telegram credentials present")
    return generated


def apply_answers(ctx, answers: dict[str, str], runbook: Runbook) -> list[str]:
    """Write questionnaire answers into `.env` (only `WRITABLE_KEYS`)."""
    pairs = [(ANSWER_ENV[key], str(answers[key]).strip()) for key in ANSWER_ENV if answers.get(key)]
    if not pairs:
        return []
    env_path = ctx.repo.path(".env")
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
    elif ctx.repo.path(".env.example").exists():
        text = ctx.repo.path(".env.example").read_text(encoding="utf-8")
    else:
        text = "# generated by atf setup\n"
    written: list[str] = []
    for key, value in pairs:
        if key not in WRITABLE_KEYS:
            continue
        if key in ("ATF_BOT_TOKEN", "ATF_API_HASH"):
            ctx.out.register_secret(value)
        text = _put(text, key, value)
        written.append(key)
        if key == "ADMIN_USER_ID":
            text = _put(text, "ADMIN_IDS", value)
    if ctx.repo.dry_run:
        runbook.add("answers", "skip", f"[dry-run] would set {', '.join(written)} in .env")
        return written
    env_path.write_text(text, encoding="utf-8")
    os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    runbook.add("answers", "ok", f"recorded {', '.join(written)} in .env (mode 0600)")
    return written


# ------------------------------------------------------------------- preflight
def preflight(ctx, args, runbook: Runbook) -> validate.Report:
    ctx.out.step("Preflight: validating checkout, config, catalogs, and secrets...")
    report = validate.run_checks(ctx, runtime=False)
    counts = validate._counts(report)
    status = "ok" if not report.failures else "fail"
    detail = f"{counts['ok']} ok · {counts['warn']} warn · {counts['fail']} fail"
    runbook.add("preflight", status, detail)
    if args.json:
        return report
    for finding in report.findings:
        if finding.status == "fail":
            ctx.out.fail(f"  • {finding.check}: {finding.detail}")
            if finding.fix:
                ctx.out.hint(f"    fix: {finding.fix}")
        elif finding.status == "warn":
            ctx.out.warn(f"  • {finding.check}: {finding.detail}")
    ctx.out.line(f"  {detail}")
    return report


# ---------------------------------------------------------------------- health
def _probe(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed local URL
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def wait_health(ctx, args, runbook: Runbook, *, seconds: float = 60.0) -> dict | None:
    candidates = [getattr(args, "url", "")] if getattr(args, "url", "") else []
    candidates = [u for u in candidates if u] + [u for u in LOCAL_HEALTH_URLS]
    deadline = time.time() + max(5.0, seconds)
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        for url in candidates:
            payload = _probe(url if url.endswith("healthz") else url.rstrip("/") + "/healthz")
            if payload is not None:
                version = payload.get("version") or ctx.repo.version
                runbook.add(
                    "health",
                    "ok",
                    f"{url} healthy (version {version}, upstream={payload.get('upstream_connected')})",
                )
                return payload
        time.sleep(2.0)
    runbook.add("health", "fail", f"no healthy /healthz after {int(seconds)}s ({attempt} probes)")
    return None


# ------------------------------------------------------------------- services
def verify_services(ctx, runbook: Runbook, *, repair: bool = True, render: bool = True) -> list[dict[str, Any]]:
    """Prove each microservice is up; optionally nudge dead containers once."""
    ctx.out.step("Verifying microservices (core, logger, api, web)...")
    if ctx.repo.dry_run:
        # Nothing was started, so there is nothing to inspect: an inspection run
        # against a stack we deliberately did not touch would only invent
        # failures ("container absent") and exit non-zero.
        runbook.add("services", "skip", "[dry-run] no containers were started")
        return []
    rows = probe.probe(ctx)
    broken = probe.failing(rows)
    if broken and repair and not ctx.repo.dry_run:
        for row in broken:
            if row["state"] != "running":
                ctx.repo.run(["docker", "start", row["container"]], check=False, quiet=True)
        time.sleep(2.0)
        rows = probe.probe(ctx)
    if render:
        probe.render(ctx, rows)
    healthy = sum(1 for row in rows if probe.status_of(row) == "ok")
    dead = [row["service"] for row in probe.failing(rows)]
    if dead:
        runbook.add("services", "fail", f"{healthy}/{len(rows)} healthy — not serving: {', '.join(dead)}")
    else:
        runbook.add("services", "ok", f"{healthy}/{len(rows)} healthy")
    return rows


# ------------------------------------------------------------------------ auth
def ensure_credential(ctx, args, runbook: Runbook) -> str:
    try:
        client = ctx.api
        cred = client.load_credential(allow_mint=True)
        if not getattr(args, "no_save", False):
            client.store.set(cred)
        runbook.add(
            "auth",
            "ok",
            f"token from {cred.source} for user {cred.user_id} (admin={cred.admin}); stored in {client.store.path}",
        )
        return cred.source
    except AtfError as exc:
        runbook.add("auth", "warn", f"could not establish a token: {exc}")
        return ""


# ------------------------------------------------------------------ boot service
def install_boot_service(ctx, runbook: Runbook) -> None:
    from . import system as system_cmd

    if ctx.repo.dry_run:
        runbook.add("boot-service", "skip", "[dry-run] would install atf-stack.service")
        return
    if system_cmd._service_install(ctx, SimpleNamespace(json=False)) == 0:
        runbook.add("boot-service", "ok", "atf-stack.service enabled — the stack starts at boot")
    else:
        runbook.add("boot-service", "warn", "could not install the boot service (re-run with sudo)")


# ------------------------------------------------------------------ questionnaire
@dataclass
class Question:
    key: str
    prompt: str
    hint: str = ""
    default: str = ""
    secret: bool = False
    required: bool = False
    choices: tuple[str, ...] = ()

    def validator(self):
        checks = {
            "bot_token": ui.valid_telegram_token,
            "api_id": validators.valid_api_id,
            "api_hash": validators.valid_api_hash,
            "admin_id": validators.valid_telegram_user_id,
            "web_url": validators.valid_url,
            "tls_domain": ui.valid_domain,
            "tls_email": validators.valid_email,
        }
        if self.choices:
            return ui.valid_choice(self.choices)
        return checks.get(self.key)


def is_api_hash(value: str) -> bool:
    return validators.valid_api_hash(value) is None


def valid_url(value: str) -> str | None:
    """Kept for callers of this module; the rule itself lives in `validators`."""
    return validators.valid_url(value)


def valid_email(value: str) -> str | None:
    return validators.valid_email(value)


def questions_for(env: dict[str, str], docker: plat.Docker) -> list[Question]:
    """The wizard's question set, pre-filled from the existing `.env`."""
    admin_default = env.get("ADMIN_USER_ID", "") or env.get("ADMIN_IDS", "")
    return [
        Question(
            "mode",
            "How should this machine run ATF?",
            hint=" · ".join(f"{name}: {desc}" for name, _, desc in plat.RUN_MODES),
            default="docker" if docker.usable else "source",
            choices=tuple(name for name, _, _ in plat.RUN_MODES),
        ),
        Question(
            "bot_token",
            "Telegram bot token (from @BotFather)",
            hint="not echoed while typing; stored in .env with mode 0600",
            default=env.get("ATF_BOT_TOKEN", "") if not _needs_value(env.get("ATF_BOT_TOKEN", "")) else "",
            secret=True,
            required=True,
        ),
        Question(
            "api_id",
            "Telegram api_id (my.telegram.org)",
            hint=f"the public Telegram Desktop id {TELEGRAM_PUBLIC_API_ID} is fine for a user-session bot",
            default=env.get("ATF_API_ID", "") or TELEGRAM_PUBLIC_API_ID,
            required=True,
        ),
        Question(
            "api_hash",
            "Telegram api_hash (my.telegram.org)",
            hint="32 hex chars; the public Telegram Desktop hash is offered as the default",
            default=env.get("ATF_API_HASH", "") if not _needs_value(env.get("ATF_API_HASH", "")) else TELEGRAM_PUBLIC_API_HASH,
            secret=True,
            required=True,
        ),
        Question(
            "admin_id",
            "Your Telegram user id (becomes the admin)",
            hint="send /id to @userinfobot if you are not sure",
            default=admin_default,
            required=True,
        ),
        Question(
            "web_url",
            "Public dashboard URL",
            hint="blank or a localhost URL means local-only",
            default=env.get("ATF_WEB_URL", "") or "http://127.0.0.1:8088",
        ),
        Question(
            "tls_domain",
            "Domain to serve with TLS (optional)",
            hint="blank = skip Let's Encrypt; the dashboard stays on http://localhost:8088",
            default="" if _needs_value(env.get("ATF_TLS_DOMAIN", "")) else env.get("ATF_TLS_DOMAIN", ""),
        ),
        Question(
            "tls_email",
            "Contact email for Let's Encrypt",
            hint="required once a TLS domain is set",
            default="" if _needs_value(env.get("ATF_TLS_EMAIL", "")) else env.get("ATF_TLS_EMAIL", ""),
        ),
    ]


def answers_template(env: dict[str, str]) -> dict[str, Any]:
    return {
        "mode": "docker",
        "bot_token": "<BotFather token>",
        "api_id": TELEGRAM_PUBLIC_API_ID,
        "api_hash": TELEGRAM_PUBLIC_API_HASH,
        "admin_id": env.get("ADMIN_USER_ID", "") or "123456789",
        "web_url": "http://127.0.0.1:8088",
        "tls_domain": "",
        "tls_email": "",
        "boot_service": True,
        "start_now": True,
        "mint_token": True,
        "_comment": "fill the values, then: atf setup --answers thisfile.json",
    }


def load_answers(args) -> dict[str, Any]:
    path = getattr(args, "answers", "") or ""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AtfError(f"could not read answers file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AtfError(f"answers file {path} must contain a JSON object")
    return {k: v for k, v in payload.items() if not str(k).startswith("_")}


def as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in ("y", "yes", "true", "1", "on", "enable"):
        return True
    if text in ("n", "no", "false", "0", "off", "disable"):
        return False
    return default


def public_answers(answers: dict[str, Any]) -> dict[str, Any]:
    """Answers safe to print — values of secrets never leave this function."""
    safe: dict[str, Any] = {}
    for key, value in answers.items():
        if key in MASKED_ANSWERS and value:
            safe[key] = f"set ({len(str(value))} chars)"
        else:
            safe[key] = value
    return safe


YES_NO: tuple[tuple[str, str], ...] = (
    ("start_now", "Build and start the whole stack now?"),
    ("mint_token", "Mint and store an admin API token for the CLI?"),
    ("boot_service", "Start the stack automatically at boot (systemd)?"),
)


def collect_answers(ctx, args, env: dict[str, str], docker: plat.Docker, host: plat.Host, runbook: Runbook) -> dict[str, Any]:
    """Run the questionnaire, honouring --yes / --answers / non-tty stdin."""
    answers: dict[str, Any] = dict(load_answers(args))
    flags: set[str] = set()
    for key in ("boot_service", "start_now", "mint_token"):
        if key in answers:
            flags.add(key)  # from the answers file: do not re-ask
        answers.setdefault(key, True)
    if getattr(args, "admin_id", ""):
        answers.setdefault("admin_id", args.admin_id)
        flags.add("admin_id")
    if args.no_boot_service:
        answers["boot_service"] = False
        flags.add("boot_service")
    if args.no_start:
        answers["start_now"] = False
        flags.add("start_now")
    if getattr(args, "source_wizard", False):
        answers["boot_service"] = False
        flags.add("boot_service")
    if args.no_auth:
        answers["mint_token"] = False
        flags.add("mint_token")

    # Prompts need a human at a terminal; `--yes`, `--json`, an answers file and a
    # piped stdin all mean "no one is watching", so defaults are used instead of
    # hanging forever. ATF_ASSUME_TTY forces the questionnaire (demos, tests).
    wants_prompts = ctx.out.is_tty or bool(os.environ.get("ATF_ASSUME_TTY"))
    interactive = not (getattr(args, "yes", False) or args.json or getattr(args, "answers", "") or not wants_prompts)
    preset = bool(getattr(args, "answers", ""))
    # `--quick` is the "I want it running, ask me only what you cannot know"
    # path: the token is always asked for, everything else keeps its default.
    quick = bool(getattr(args, "quick", False))
    skipped: list[str] = []
    for question in questions_for(env, docker):
        given = answers.get(question.key)
        if given not in (None, ""):
            continue
        if quick and question.key != "bot_token" and (not question.required or question.default):
            answers[question.key] = question.default
            skipped.append(question.key)
            continue
        if not interactive:
            answers[question.key] = question.default
            continue
        value = ui.ask(
            ctx.out,
            question.prompt,
            default=question.default,
            validate=question.validator(),
            hint=question.hint,
            secret=question.secret,
            required=question.required,
        )
        answers[question.key] = value
    for key, question in YES_NO:
        if key == "boot_service" and not host.systemd:
            answers["boot_service"] = False
            continue
        if interactive and not quick and key not in flags:
            answers[key] = ui.confirm(ctx.out, "  " + question, default=as_bool(answers.get(key), True))
        else:
            answers[key] = as_bool(answers.get(key), True)
    if quick and skipped and not ctx.out.json_mode:
        ctx.out.hint(f"--quick kept the defaults for: {', '.join(skipped)}")
    if not answers.get("tls_domain"):
        answers["tls_email"] = ""
    runbook.add(
        "answers",
        "ok" if preset else ("skip" if not interactive else "ok"),
        ("loaded from " + str(getattr(args, "answers", ""))) if preset else ("defaults (non-interactive)" if not interactive else "collected from this session"),
    )
    return answers


def review_answers(ctx, answers: dict[str, Any]) -> bool:
    """Echo the answers (secrets masked) and get an explicit confirmation."""
    if ctx.out.json_mode:
        return True
    rows = []
    for key in ANSWER_ENV:
        if key not in answers:
            continue
        value = answers[key]
        shown = f"set ({len(str(value))} chars)" if key in MASKED_ANSWERS and value else (value or "—")
        rows.append((key, str(shown)))
    rows.append(("mode", str(answers.get("mode", "docker"))))
    rows.append(("boot_service", "yes" if answers.get("boot_service") else "no"))
    rows.append(("start_now", "yes" if answers.get("start_now") else "no"))
    ui.box(ctx.out, rows, title="your answers")
    return ui.confirm(ctx.out, "Write these to .env and continue?", default=True)


# ---------------------------------------------------------------------- banner
def show_header(ctx, host: plat.Host, docker: plat.Docker, env: dict[str, str], runbook: Runbook) -> None:
    ui.banner(
        ctx.out,
        f"AutoTelegramForward · setup wizard · v{ctx.repo.version}",
        "Telegram forwarding platform: core · logger · api · dashboard",
    )
    ui.box(
        ctx.out,
        [
            ("host", host.label),
            ("python", f"{host.python}"),
            ("root", str(ctx.repo.root)),
            ("docker", (docker.server and f"server {docker.server}") or docker.detail or ("client only" if docker.present else "absent")),
            ("compose", docker.compose_version or ("missing" if docker.usable is False else "?")),
            ("env file", ".env present" if ctx.repo.path(".env").exists() else ".env will be created"),
        ],
        title="this machine",
    )
    if docker.usable:
        runbook.add("platform", "ok", f"{host.label} · docker {docker.server} · compose {docker.compose_version}")
    else:
        runbook.add("platform", "warn", f"{host.label} · {docker.detail or 'docker unavailable'}")


def portable_hint(ctx) -> None:
    ctx.out.line()
    ctx.out.line("  " + ctx.out.paint("run it without installing anything:", "bold"))
    ctx.out.line("    git clone https://github.com/Opselon/AutoTelegramForward && cd AutoTelegramForward")
    ctx.out.line("    ./scripts/setup.sh            " + ctx.out.paint("# creates the venv, puts atf on PATH, then runs this wizard", "grey"))
    ctx.out.line("    python3 atf.pyz setup         " + ctx.out.paint("# or the single-file build, no install at all", "grey"))
    ctx.out.line("    docker compose up -d          " + ctx.out.paint("# or plain compose, if you only want the stack", "grey"))


# --------------------------------------------------------------------- command
def _source_wizard(ctx, args) -> int:
    """Hand off to the legacy bare-metal wizard (`atf.py setup`) for source runs.

    ATF_LEGACY stops atf.py from bouncing straight back into this module.
    """
    ctx.out.title("ATF source-mode setup (bare metal, no docker)")
    if ctx.repo.dry_run:
        ctx.out.info("[dry-run] would delegate to: python atf.py setup (config.yaml + supervised processes)")
        return 0
    ctx.out.info("Delegating to the legacy source wizard — it writes config.yaml and supervises local processes.")
    return ctx.repo.run_streaming(
        [ctx.repo.python(), "atf.py", "setup", *(args.services or [])],
        check=False,
        env={"ATF_LEGACY": "1"},
    )


def _readiness(ctx, args, runbook: Runbook, host: plat.Host, docker: plat.Docker) -> int:
    """`--check`: report only, change nothing."""
    report = preflight(ctx, args, runbook)
    rows = [] if ctx.repo.dry_run else probe.probe(ctx)
    if rows:
        probe.render(ctx, rows)
        runbook.add("services", "ok" if not probe.failing(rows) else "fail", f"{sum(1 for r in rows if probe.status_of(r) == 'ok')}/{len(rows)} healthy")
    if not docker.usable:
        ctx.out.line()
        for what, command in plat.install_guidance(host):
            ctx.out.info(f"{what}: {command}")
    if args.json:
        return _finish(ctx, args, runbook, services=rows)
    ctx.out.line()
    ctx.out.table(("STEP", "STATUS", "DETAIL"), [[s.name, s.status, sh.truncate(s.detail, 74)] for s in runbook.steps])
    ctx.out.line()
    if report.failures:
        ctx.out.fail(f"{len(report.failures)} blocking problem(s) — fix them, then re-run `atf setup`")
        return 1
    ctx.out.ok("readiness check clean — run `atf setup` to build and start the stack")
    portable_hint(ctx)
    return 0


def _setup(ctx, args) -> int:
    if getattr(args, "source_wizard", False):
        return _source_wizard(ctx, args)

    runbook = Runbook()
    host = plat.detect_host()
    docker = plat.detect_docker(ctx)
    env = read_env(ctx)

    if getattr(args, "print_answers", False):
        payload = json.dumps(answers_template(env), indent=2)
        if args.json:
            ctx.out.json(answers_template(env))
        else:
            ctx.out.line(payload)
            ctx.out.hint("save that as answers.json, fill it in, then: atf setup --answers answers.json")
        return 0

    show_header(ctx, host, docker, env, runbook)
    if args.check:
        return _readiness(ctx, args, runbook, host, docker)

    # ---------------------------------------------------------------- questions
    if not docker.usable and not getattr(args, "answers", ""):
        plat.require_docker(ctx, docker)
    answers = collect_answers(ctx, args, env, docker, host, runbook)

    if answers.get("mode") == "source":
        runbook.add("mode", "ok", "source (bare metal)")
        runbook.add("docker", "skip", "not required in source mode")
        apply_answers(ctx, answers, runbook)
        return _source_wizard(ctx, args)
    if answers.get("mode") == "portable" and not docker.usable:
        runbook.add("mode", "warn", "portable build: run python3 atf.pyz setup on the target machine")
        portable_hint(ctx)
        return _finish(ctx, args, runbook)

    if not docker.usable:
        runbook.add("docker", "fail", docker.detail or "docker is not usable")
        return _finish(ctx, args, runbook)
    runbook.add("docker", "ok", f"server {docker.server} · compose {docker.compose_version}")

    if not review_answers(ctx, answers):
        ctx.out.warn("nothing written — re-run `atf setup` when you are ready")
        runbook.add("confirm", "skip", "operator declined")
        return _finish(ctx, args, runbook)

    # ----------------------------------------------------------------- the work
    report = preflight(ctx, args, runbook)
    if report.failures and not args.force:
        runbook.add("abort", "fail", "preflight found blocking problems (use --force to continue anyway)")
        return _finish(ctx, args, runbook)

    bootstrap_env(ctx, args, runbook, force=args.force)
    apply_answers(ctx, answers, runbook)

    services = list(args.services or [])
    if answers.get("start_now"):
        try:
            deploy.bring_up(ctx, services=services or None, build=not args.no_build, runbook=runbook)
            deploy.repair_networks(ctx, services=services or None)
            runbook.add("networks", "ok", "compose aliases and external bridges attached")
        except AtfError as exc:
            runbook.add("start", "fail", str(exc))
    else:
        runbook.add("start", "skip", "--no-start / answered no")

    rows = verify_services(ctx, runbook, repair=True)
    if not runbook.failed:
        wait_health(ctx, args, runbook, seconds=float(getattr(args, "wait", 60) or 60))
    if answers.get("mint_token") and not args.no_auth and not runbook.failed:
        ensure_credential(ctx, args, runbook)
    elif not answers.get("mint_token"):
        runbook.add("auth", "skip", "operator declined token minting")

    if answers.get("boot_service") and not args.no_boot_service:
        if host.systemd:
            install_boot_service(ctx, runbook)
        else:
            runbook.add("boot-service", "skip", f"systemd not available on {host.label} — use `docker compose up -d` at login")
    else:
        runbook.add("boot-service", "skip", "not requested")

    return _finish(ctx, args, runbook, services=rows, answers=answers, host=host, docker=docker)


NEXT_STEPS = (
    "atf status                  live board of services, rules and health",
    "atf validate --runtime      full health re-check of the live stack",
    "atf system services         microservice table with fix hints",
    "atf doctor --fix            repair anything the validator flags",
)


def _finish(
    ctx,
    args,
    runbook: Runbook,
    *,
    services: list[dict[str, Any]] | None = None,
    answers: dict[str, Any] | None = None,
    host: plat.Host | None = None,
    docker: plat.Docker | None = None,
) -> int:
    ok = not runbook.failed
    if args.json:
        payload: dict[str, Any] = {
            "ok": ok,
            "version": ctx.repo.version,
            "root": str(ctx.repo.root),
            "steps": runbook.to_json(),
            "next_steps": list(NEXT_STEPS),
        }
        if host is not None:
            payload["host"] = {"os": host.os, "distro": host.distro, "label": host.label, "systemd": host.systemd}
        if docker is not None:
            payload["docker"] = {"usable": docker.usable, "server": docker.server, "compose": docker.compose_version}
        if services is not None:
            payload["services"] = probe.to_json(services)
        if answers is not None:
            payload["answers"] = public_answers(answers)
        ctx.out.json(payload)
        return 0 if ok else 1

    ctx.out.line()
    ui.timeline(ctx.out, [(s.name, s.status) for s in runbook.steps])
    ctx.out.line()
    if answers is not None:
        rows = []
        if host is not None:
            rows.append(("host", host.label))
        if docker is not None:
            rows.append(("docker", docker.server or "—"))
        if services:
            rows.append(("services", f"{sum(1 for r in services if probe.status_of(r) == 'ok')}/{len(services)} healthy"))
        rows.append(("dashboard", answers.get("web_url") or "http://127.0.0.1:8088"))
        rows.append(("api", "http://127.0.0.1:8080/healthz"))
        if answers.get("tls_domain"):
            rows.append(("public", f"https://{answers['tls_domain']}"))
        rows.append(("credentials", "~/.atf/cli.json (mode 0600)"))
        rows.append(("boot service", "atf-stack.service" if answers.get("boot_service") else "not installed"))
        ui.box(ctx.out, rows, title="what you now have", ramp=ui.GOOD if ok else ui.WARN)
    if ok:
        ctx.out.ok(f"Setup complete — ATF v{ctx.repo.version} is ready at {ctx.repo.root}")
        ctx.out.line()
        for step in NEXT_STEPS:
            ctx.out.info(step)
    else:
        ctx.out.fail("Setup finished with problems — see the failing step(s) above")
        for step in runbook.steps:
            if step.status == "fail":
                ctx.out.hint(f"{step.name}: {step.detail}")
    return 0 if ok else 1


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.add_argument("services", nargs="*", help="subset of services to start (default: all)")
    parser.add_argument("--check", action="store_true", help="read-only readiness report, change nothing")
    parser.add_argument("--answers", default="", metavar="FILE", help="JSON file with the wizard's answers")
    parser.add_argument("--print-answers", action="store_true", help="print an answers template (JSON) and exit")
    parser.add_argument(
        "--source-wizard",
        action="store_true",
        help="run the legacy bare-metal wizard (config.yaml + supervised local processes) instead",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="ask only for the bot token; keep defaults for everything else",
    )
    parser.add_argument("--force", action="store_true", help="continue past failures and regenerate secrets")
    parser.add_argument("--no-build", action="store_true", help="skip the docker image build")
    parser.add_argument("--no-start", action="store_true", help="prepare only; do not touch the stack")
    parser.add_argument("--no-boot-service", action="store_true", help="do not install the systemd boot unit")
    parser.add_argument("--no-auth", action="store_true", help="skip token minting and storage")
    parser.add_argument("--no-save", action="store_true", help="do not persist the token into ~/.atf/cli.json")
    # default=None, not "": argparse runs `type=` over a *string* default too,
    # so an empty string would be rejected by the validator before the wizard
    # ever starts.
    parser.add_argument("--admin-id", type=validators.argparse_type("telegram_user_id"), default=None,
                        help="Telegram user id to register as admin")
    parser.add_argument("--wait", type=validators.argparse_type("int", minimum=0, maximum=3600), default=60,
                        help="seconds to wait for /healthz (default 60)")
    parser.set_defaults(func=_setup)
