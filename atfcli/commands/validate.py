"""Deep validation of configuration, contracts, catalogs, and data.

`atf validate` is the *static* counterpart of `atf doctor`: it never mutates the
host (no builds, no container churn, no repairs), so it is safe to run in CI,
in a pre-deploy gate, or on a fresh checkout before anything is started. Add
`--runtime` to fold in the live stack checks that `doctor` owns.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import re
import sqlite3
import stat
from pathlib import Path

from ..errors import AtfError
from . import _shared as sh
from .doctor import (
    Report,
    _port_open,
    check_aliases,
    check_api,
    check_certificate,
    check_containers,
    check_services,
)

NAME = "validate"
HELP = "Validate config, secrets, catalogs, contracts, and data (CI-safe, read-only)"

#: Every locale the product ships must carry an identical key set.
LOCALES: tuple[str, ...] = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
CATALOG = Path("core/infrastructure/telegram/locales/translations.json")

#: Keys the stack cannot run without.
REQUIRED_ENV: tuple[str, ...] = (
    "ATF_API_ID",
    "ATF_API_HASH",
    "ATF_BOT_TOKEN",
    "ADMIN_USER_ID",
    "ATF_MASTER_KEY",
)
#: Optional keys that must not ship with their example value: compose wires
#: them in, so a placeholder silently disables them instead of failing loudly.
RECOMMENDED_ENV: tuple[str, ...] = ("ATF_API_KEY", "ATF_WEB_URL")
#: Subset that must be high-entropy rather than a human-chosen word.
SECRET_ENV: tuple[str, ...] = ("ATF_MASTER_KEY", "ATF_API_KEY", "ATF_BOT_TOKEN")
#: Subset the operator must supply from Telegram — never invented locally.
OPERATOR_ENV: tuple[str, ...] = ("ATF_API_ID", "ATF_API_HASH", "ATF_BOT_TOKEN")
MIN_SECRET_LEN = 24

REQUIRED_PATHS: tuple[str, ...] = (
    "docker-compose.yml",
    "VERSION",
    "atfcli/cli.py",
    "core",
    "api",
    "web",
    "Dockerfile.core",
    "Dockerfile.api",
    "Dockerfile.web",
    "Dockerfile.logger-rust",
)

PLACEHOLDER = re.compile(
    r"^(changeme|placeholder|your[_-]|xxx*|todo|fixme|<[^>]*>|example|genera|option|test|dummy|password|secret)",
    re.IGNORECASE,
)

#: Shapes that look like a live credential if they appear in tracked source.
SECRET_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9]{24,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)
#: Paths where a token-shaped literal is expected (fixtures, docs, templates).
SCAN_SKIP = re.compile(r"(^|/)(tests?|fixtures?)/|\.example$|\.md$|\.lock$|package-lock\.json")
#: The directory `scripts/gen_proto.py` reads its sources from. A `.proto`
#: outside it (the Rust logger crate, the mobile app) is compiled by its own
#: toolchain, so comparing it against `core/proto/*_pb2.py` reports staleness
#: that cannot exist — the check would fail because a file from another
#: language tree was checked out later.
PROTO_SOURCE_DIR = "proto"


# --------------------------------------------------------------------- helpers
def _env_values(ctx) -> dict[str, str]:
    return dict(ctx.repo.env_file_values)


def _placeholder(value: str) -> bool:
    stripped = (value or "").strip()
    return not stripped or bool(PLACEHOLDER.match(stripped))


def _catalog(ctx) -> dict[str, dict[str, str]]:
    path = ctx.repo.path(CATALOG)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("locales"), dict):
        data = data["locales"]
    if not isinstance(data, dict):
        raise ValueError("catalog is not a locale mapping")
    return {str(k): v for k, v in data.items()}


def _placeholders(text: object) -> list[str]:
    return re.findall(r"\{[a-zA-Z0-9_]+\}", str(text))


def _counts(report: Report) -> dict[str, int]:
    return {
        "ok": len(report.findings) - len(report.failures) - len(report.warnings),
        "warn": len(report.warnings),
        "fail": len(report.failures),
    }


# ---------------------------------------------------------------------- checks
def check_layout(ctx, report: Report, **_) -> None:
    missing = [p for p in REQUIRED_PATHS if not ctx.repo.exists(p)]
    if missing:
        report.add(
            "layout",
            "fail",
            f"missing {', '.join(missing)}",
            fix="run inside a complete AutoTelegramForward checkout",
        )
    else:
        report.add("layout", "ok", f"complete checkout at {ctx.repo.root}")


def check_version(ctx, report: Report, **_) -> None:
    raw = ctx.repo.version
    if re.fullmatch(r"\d+\.\d+\.\d+", raw or ""):
        report.add("version", "ok", f"VERSION = {raw}")
    else:
        report.add("version", "warn", f"VERSION looks unusual: {raw!r}", fix="write a semver into VERSION")


def check_env_present(ctx, report: Report, **_) -> None:
    if not ctx.repo.path(".env").exists():
        report.add(
            "env-file",
            "fail",
            ".env is missing",
            fix="run `atf setup` to generate it from .env.example",
        )
        return
    report.add("env-file", "ok", ".env present")


def check_env_required(ctx, report: Report, **_) -> None:
    values = _env_values(ctx)
    if not values:
        report.add("env-keys", "warn", "no .env values to inspect")
        return
    missing = [k for k in REQUIRED_ENV if _placeholder(values.get(k, ""))]
    if missing:
        operator = [k for k in missing if k in OPERATOR_ENV]
        report.add(
            "env-keys",
            "fail",
            f"unset or placeholder: {', '.join(missing)}",
            fix=(
                "set the Telegram credentials in .env"
                if operator
                else "run `atf setup --force` to generate the crypto secrets"
            ),
        )
        return
    report.add("env-keys", "ok", f"{len(REQUIRED_ENV)} required keys populated")


def check_env_recommended(ctx, report: Report, **_) -> None:
    values = _env_values(ctx)
    if not values:
        return
    placeholders = [k for k in RECOMMENDED_ENV if k in values and _placeholder(values.get(k, ""))]
    if placeholders:
        report.add(
            "env-optional",
            "warn",
            f"still carrying the example value: {', '.join(placeholders)}",
            fix="run `atf setup` to generate/refresh these, or set them in .env",
        )
    else:
        report.add("env-optional", "ok", "optional keys set to real values")


def check_env_secret_strength(ctx, report: Report, **_) -> None:
    values = _env_values(ctx)
    weak = [k for k in SECRET_ENV if k in values and 0 < len(values.get(k, "")) < MIN_SECRET_LEN]
    if weak:
        report.add(
            "env-secrets",
            "warn",
            f"short secret value(s): {', '.join(weak)}",
            fix=f"use at least {MIN_SECRET_LEN} random characters (`atf setup --force`)",
        )
    else:
        report.add("env-secrets", "ok", "secret values meet the length floor")


def check_env_perms(ctx, report: Report, **_) -> None:
    path = ctx.repo.path(".env")
    if not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        report.add(
            "env-perms",
            "warn",
            f".env mode {mode:04o} is group/world readable",
            fix="chmod 600 .env",
        )
    else:
        report.add("env-perms", "ok", f".env mode {mode:04o}")


def check_env_unknown_keys(ctx, report: Report, **_) -> None:
    example = ctx.repo.path(".env.example")
    if not example.exists() or not _env_values(ctx):
        return
    known = set()
    for raw in example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            known.add(line.partition("=")[0].strip())
    unknown = sorted(set(_env_values(ctx)) - known)
    if unknown:
        report.add("env-typos", "warn", f"keys absent from .env.example: {', '.join(unknown)}")
    else:
        report.add("env-typos", "ok", "no stray env keys")


def check_compose(ctx, report: Report, **_) -> None:
    info = ctx.repo.docker_info(refresh=True)
    if not info.available:
        report.add("compose", "warn", "docker unavailable — compose file not parsed")
        return
    res = ctx.repo.run(["docker", "compose", "config", "-q"], check=False, quiet=True)
    if res.ok:
        report.add("compose", "ok", "docker-compose.yml parses cleanly")
    else:
        report.add(
            "compose",
            "fail",
            f"compose config rejected: {res.text.strip().splitlines()[-1] if res.text.strip() else res.returncode}",
        )


def check_catalog_parity(ctx, report: Report, **_) -> None:
    if not ctx.repo.exists(CATALOG):
        report.add("catalog", "fail", f"{CATALOG} not found")
        return
    catalog = _catalog(ctx)
    unexpected = [loc for loc in catalog if loc not in LOCALES]
    missing = [loc for loc in LOCALES if loc not in catalog]
    if missing or unexpected:
        report.add(
            "catalog",
            "fail",
            f"locales missing={missing or 'none'} unexpected={unexpected or 'none'}",
        )
        return
    base = set(catalog["en"])
    problems = []
    for loc in LOCALES:
        keys = set(catalog[loc])
        if keys != base:
            problems.append(f"{loc}: -{len(base - keys)}/+{len(keys - base)}")
    if problems:
        report.add("catalog", "fail", "key parity broken — " + "; ".join(problems))
    else:
        report.add("catalog", "ok", f"{len(base)} keys identical across {len(LOCALES)} locales")


def check_catalog_placeholders(ctx, report: Report, **_) -> None:
    if not ctx.repo.exists(CATALOG):
        return
    try:
        catalog = _catalog(ctx)
    except (OSError, ValueError):
        return
    base = catalog.get("en", {})
    mismatches: list[str] = []
    for loc in LOCALES:
        if loc == "en":
            continue
        for key, value in catalog.get(loc, {}).items():
            if key not in base:
                continue
            if sorted(_placeholders(value)) != sorted(_placeholders(base[key])):
                mismatches.append(f"{loc}.{key}")
    if mismatches:
        report.add(
            "catalog-placeholders",
            "fail",
            f"{len(mismatches)} placeholder mismatch(es): {', '.join(mismatches[:5])}"
            + (" …" if len(mismatches) > 5 else ""),
        )
    else:
        report.add("catalog-placeholders", "ok", "placeholder sets match across locales")


def check_catalog_rtl(ctx, report: Report, **_) -> None:
    """English values must never carry Persian/Arabic script (fallback is en)."""
    if not ctx.repo.exists(CATALOG):
        return
    try:
        catalog = _catalog(ctx)
    except (OSError, ValueError):
        return
    rtl = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
    offenders = [k for k, v in catalog.get("en", {}).items() if rtl.search(str(v))]
    if offenders:
        report.add(
            "catalog-rtl",
            "fail",
            f"{len(offenders)} English value(s) contain RTL script: {', '.join(offenders[:5])}",
        )
    else:
        report.add("catalog-rtl", "ok", "no RTL script in English catalog values")


def check_secrets_uncommitted(ctx, report: Report, **_) -> None:
    res = ctx.repo.run(["git", "ls-files"], check=False, quiet=True)
    if not res.ok:
        report.add("secrets-scan", "warn", "git ls-files unavailable — skipped")
        return
    hits: list[str] = []
    scanned = 0
    for rel in res.stdout.splitlines():
        rel = rel.strip()
        if not rel or SCAN_SKIP.search(rel):
            continue
        path = ctx.repo.path(rel)
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, pattern in SECRET_SHAPES:
            if pattern.search(text):
                hits.append(f"{rel} ({label})")
                break
    if hits:
        report.add(
            "secrets-scan",
            "fail",
            f"credential-shaped literal in tracked file(s): {', '.join(hits[:5])}",
            fix="remove the literal and hold the value in .env (never commit secrets)",
        )
    else:
        report.add("secrets-scan", "ok", f"{scanned} tracked file(s) clean of credential shapes")


def check_env_ignored(ctx, report: Report, **_) -> None:
    res = ctx.repo.run(["git", "check-ignore", "-q", ".env"], check=False, quiet=True)
    if res.returncode == 0:
        report.add("env-ignored", "ok", ".env is git-ignored")
    else:
        report.add("env-ignored", "fail", ".env is NOT git-ignored", fix="add .env to .gitignore")


def check_database_integrity(ctx, report: Report, **_) -> None:
    db = ctx.repo.path("data", "atf.db")
    if not db.exists():
        report.add("database", "warn", "data/atf.db not created yet (first boot will make it)")
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        try:
            verdict = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows = ""
            for name in ("rules", "sessions", "users"):
                if name in tables:
                    count = con.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    rows += f"{name}={count} "
        finally:
            con.close()
    except sqlite3.Error as exc:
        report.add("database", "fail", f"sqlite error: {exc}")
        return
    if verdict == "ok":
        report.add("database", "ok", f"integrity ok ({rows.strip() or 'no known tables'})")
    else:
        report.add("database", "fail", f"integrity_check: {verdict}")


def _repo_glob(ctx, pattern: str) -> list[Path]:
    """Every match under the repo, minus dependency and VCS trees."""
    skip = {".git", ".venv", "node_modules", "target", "build", "dist", "__pycache__"}
    found: list[Path] = []
    for path in ctx.repo.root.rglob(pattern):
        if skip & set(path.parts):
            continue
        found.append(path)
    return found


def check_proto_freshness(ctx, report: Report, **_) -> None:
    source_dir = ctx.repo.path(PROTO_SOURCE_DIR)
    protos = sorted(source_dir.glob("*.proto")) if source_dir.is_dir() else []
    if not protos:
        report.add("proto", "warn", f"no .proto sources in {PROTO_SOURCE_DIR}/")
        return
    generated = _repo_glob(ctx, "*_pb2.py")
    if not generated:
        report.add("proto", "warn", "no generated *_pb2.py — run `atf proto gen`")
        return
    newest_proto = max(protos, key=lambda p: p.stat().st_mtime)
    newest_gen = max(generated, key=lambda p: p.stat().st_mtime)
    if newest_proto.stat().st_mtime > newest_gen.stat().st_mtime:
        report.add(
            "proto",
            "warn",
            f"{newest_proto.relative_to(ctx.repo.root)} is newer than "
            f"{newest_gen.relative_to(ctx.repo.root)} — run `atf proto gen`",
            fix="atf proto",
        )
    else:
        report.add("proto", "ok", f"{len(protos)} proto source(s) generated")


def check_ports_ownership(ctx, report: Report, **_) -> None:
    info = ctx.repo.docker_info()
    if not info.available:
        return
    published: set[int] = set()
    res = ctx.repo.run(
        ["docker", "ps", "--format", "{{.Names}}={{.Ports}}"], check=False, quiet=True
    )
    for line in res.stdout.splitlines():
        for port in re.findall(r":(\d{2,5})->", line):
            published.add(int(port))
    ours = {name.split("=")[0] for name in res.stdout.splitlines()}
    foreign = []
    for port in (8080, 8088, 50051, 50052):
        if port in published:
            continue
        if _port_open("127.0.0.1", port):
            foreign.append(port)
    if foreign:
        report.add(
            "ports",
            "warn",
            f"port(s) {foreign} are held by a process outside this stack",
            fix="stop the foreign listener or publish on another port",
        )
    else:
        report.add("ports", "ok", f"{len(ours)} container(s), no foreign listeners on ATF ports")


#: Where the API can be reached when no --url is given. 8080 is the gateway,
#: 8088 the web container (which proxies /api), both on the loopback interface.
LOCAL_API_BASES: tuple[str, ...] = ("http://127.0.0.1:8080", "http://127.0.0.1:8088")


def _api_bases(ctx) -> list[str]:
    args = getattr(ctx, "args", None)
    configured = (getattr(args, "url", "") or os.environ.get("ATF_CLI_URL", "") or "").strip()
    bases = [configured.rstrip("/")] if configured else []
    return bases + [base for base in LOCAL_API_BASES if base not in bases]


def check_container_env_contract(ctx, report: Report, **_) -> None:
    """Ask the running API which configuration it actually received.

    The host `.env` and the container's environment can disagree — that is the
    classic "I set ATF_BOT_TOKEN and the bot still says unconfigured" bug, and a
    check that only reads the file on disk cannot see it. The endpoint is behind
    the same auth as the rest of /api, so this needs a credential; without one it
    reports a warning instead of a false failure.
    """
    client = ctx.api_or_none
    if client is None:
        report.add(
            "container-env",
            "warn",
            "no credential — cannot ask the container which configuration it received",
            fix="atf auth login  (or export ATF_TOKEN)",
        )
        return

    host_env = _env_values(ctx)
    try:
        payload = client.get("/api/system/env")
    except AtfError as exc:
        report.add(
            "container-env",
            "warn",
            f"{client.url}: /api/system/env unavailable ({exc.message})",
            fix="atf system restart  # rebuild the api image to pick up this endpoint",
        )
        return
    if not isinstance(payload, dict):
        report.add("container-env", "warn", f"{client.url}: unexpected response from /api/system/env")
        return

    checks = payload.get("checks") or []
    missing_container = [str(key) for key in (payload.get("missing_required") or [])]
    missing_recommended = [str(key) for key in (payload.get("missing_recommended") or [])]
    # A key the host file sets but the container never received means the
    # container predates the change: restarting is the fix, not editing .env.
    stale = [key for key in missing_container if key in host_env]
    if stale:
        report.add(
            "container-env",
            "fail",
            f"{client.url}: container is missing {', '.join(stale)} although .env sets it "
            "(the container predates the change)",
            fix="atf system restart  # recreates containers with the current .env",
        )
        return
    unset_anywhere = [key for key in missing_container if key not in host_env]
    if unset_anywhere:
        report.add(
            "container-env",
            "warn",
            f"{client.url}: not configured yet: {', '.join(unset_anywhere)}",
            fix="atf setup --check  (then set the keys it reports)",
        )
        return
    if missing_recommended:
        report.add(
            "container-env",
            "warn",
            f"{client.url}: not configured yet (optional but recommended): {', '.join(missing_recommended)}",
            fix="atf setup --check  (it offers to generate these)",
        )
        return
    deployment = payload.get("deployment") or "unknown"
    report.add(
        "container-env",
        "ok",
        f"{client.url}: container received every required and recommended input ({len(checks)} checks, {deployment})",
    )


def _fetch_json(url: str, timeout: float = 4.0) -> dict | None:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed local URL
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def check_runtime(ctx, report: Report, **_) -> None:
    for check in (check_containers, check_aliases, check_api, check_services, check_certificate, check_container_env_contract):
        try:
            check(ctx, report, fix=False)
        except AtfError as exc:
            report.add(check.__name__, "warn", f"runtime check unavailable: {exc}")


STATIC_CHECKS = (
    check_layout,
    check_version,
    check_env_present,
    check_env_required,
    check_env_recommended,
    check_env_secret_strength,
    check_env_perms,
    check_env_unknown_keys,
    check_env_ignored,
    check_compose,
    check_catalog_parity,
    check_catalog_placeholders,
    check_catalog_rtl,
    check_secrets_uncommitted,
    check_database_integrity,
    check_proto_freshness,
    check_ports_ownership,
)


def run_checks(ctx, *, runtime: bool = False, deep: bool = False) -> Report:
    """Every check, each isolated so one crash cannot mask the rest."""
    report = Report()
    for check in STATIC_CHECKS:
        try:
            check(ctx, report)
        except AtfError as exc:
            report.add(check.__name__, "warn", f"check failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - a broken check must not abort the run
            report.add(check.__name__, "warn", f"check crashed: {type(exc).__name__}: {exc}")
    if runtime:
        check_runtime(ctx, report)
    if deep:
        _audits(ctx, report)
    return report


def _audits(ctx, report: Report, **_) -> None:
    """Shell out to the repo's own i18n audits (source-level checks)."""
    for script in ("scripts/audit_cross_language.py", "scripts/audit_telegram_i18n.py"):
        if not ctx.repo.exists(script):
            continue
        res = ctx.repo.run([ctx.repo.python(), script], check=False, quiet=True, timeout=300)
        label = Path(script).stem.replace("audit_", "")
        if res.ok:
            report.add(f"audit-{label}", "ok", "passed")
        else:
            tail = (res.stdout + res.stderr).strip().splitlines()
            report.add(f"audit-{label}", "fail", tail[-1] if tail else "failed")


# -------------------------------------------------------------------- command
def _validate(ctx, args) -> int:
    runtime = bool(getattr(args, "runtime", False))
    deep = bool(getattr(args, "deep", False))
    strict = bool(getattr(args, "strict", False))

    report = run_checks(ctx, runtime=runtime, deep=deep)
    counts = _counts(report)
    failed = bool(report.failures) or (strict and bool(report.warnings))

    if args.json:
        ctx.out.json(
            {
                "ok": not failed,
                "strict": strict,
                "score": report.score(),
                "counts": counts,
                "findings": [
                    {
                        "check": f.check,
                        "status": f.status,
                        "detail": f.detail,
                        "fix": f.fix,
                    }
                    for f in report.findings
                ],
            }
        )
        return 1 if failed else 0

    ctx.out.title("ATF validation")
    ctx.out.table(
        ("CHECK", "STATUS", "DETAIL"),
        [[f.check, f.status, sh.truncate(f.detail, 66)] for f in report.findings],
    )
    if report.failures:
        ctx.out.line()
        ctx.out.fail(f"{counts['fail']} failure(s):")
        for f in report.failures:
            ctx.out.line(f"  • {f.check}: {f.detail}")
            if f.fix:
                ctx.out.hint(f"    fix: {f.fix}")
    if report.warnings:
        ctx.out.line()
        ctx.out.warn(f"{counts['warn']} warning(s):")
        for f in report.warnings:
            ctx.out.line(f"  • {f.check}: {f.detail}")
            if f.fix:
                ctx.out.hint(f"    fix: {f.fix}")
    ctx.out.line()
    ctx.out.line(
        f"  {counts['ok']} ok · {counts['warn']} warn · {counts['fail']} fail · score {report.score()}/100"
    )
    if failed:
        ctx.out.fail("validation FAILED")
        return 1
    ctx.out.ok("validation PASSED")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.add_argument("--runtime", action="store_true", help="also check the live stack")
    parser.add_argument("--deep", action="store_true", help="also run the repo i18n audits")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures (CI gate)")
    parser.set_defaults(func=_validate)
