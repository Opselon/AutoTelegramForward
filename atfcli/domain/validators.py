"""Input validation for every value a human can type at this CLI.

Two rules shape this module:

1. **A refusal must be usable.** Every validator answers with the reason the
   value is wrong *and*, wherever the value came from a fixed vocabulary, what
   the operator probably meant (`suggest`). `atf doctor --only doctro` telling
   you "invalid choice: 'doctro'" wastes a round trip; naming `doctor` ends the
   task.
2. **One definition, three consumers.** The wizard's prompts (`ui.ask`), the
   command line (`argparse` `type=` via [argparse_type]) and the API-facing
   commands all use the functions below, so a rule can never hold on one path
   and not another — that drift is how a bad value ends up stored as
   configuration.

Every validator has the same shape: it takes the raw string the user typed and
returns a problem message, or `None` when the value is acceptable:

    >>> valid_port("8080")
    >>> valid_port("99999")
    'port must be <= 65535'

That matches `ui.ask(validate=...)` and keeps the call sites free of
try/except, while [argparse_type] adapts the same function to argparse, which
wants an exception.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: Tanner of a Telegram user/bot id. Telegram allocates up to 52 bits and
#: user ids already exceed 32 bits, so `int32` assumptions silently truncate.
TELEGRAM_ID_MAX = 2**52
#: `@username` rules: 5-32 chars, letters/digits/underscore, must start with a
#: letter (Telegram's own rule).
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
#: BotFather hands out `<bot id>:<35 char secret>`; older bots have shorter
#: secrets, so 30 is the floor rather than the exact length.
TOKEN_RE = re.compile(r"^\d{5,12}:[A-Za-z0-9_-]{30,}$")
#: my.telegram.org apps: `api_id` is numeric, `api_hash` is 32 hex chars.
API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
DURATION_RE = re.compile(r"^(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days|w|wk|weeks?)?$", re.IGNORECASE)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
#: A git ref that cannot be confused for a flag or an option-looking string.
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
#: `--set key=value` keys: dotted paths into nested settings documents.
KV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$")
#: Settings/document identifiers (rule names, extension ids, catalog keys).
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: Control characters break terminals, JSON documents and Telegram payloads
#: alike, so nothing user-supplied may carry them.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_ALIASES = {"WARN": "WARNING", "ERR": "ERROR", "FATAL": "CRITICAL", "TRACE": "DEBUG"}
#: The ten locales the product ships; the CLI never invents an eleventh.
LOCALES: tuple[str, ...] = ("en", "fa", "ru", "zh", "pt", "uz", "id", "es", "fr", "it")
_LOCALE_ALIASES = {
    "fa-ir": "fa", "fa-af": "fa", "en-us": "en", "en-gb": "en", "ru-ru": "ru",
    "zh-cn": "zh", "zh-tw": "zh", "zh-hans": "zh", "pt-br": "pt", "pt-pt": "pt",
    "uz-uz": "uz", "id-id": "id", "es-es": "es", "es-mx": "es", "fr-fr": "fr",
    "it-it": "it", "persian": "fa", "farsi": "fa",
}
RUN_MODES: tuple[str, ...] = ("docker", "source", "portable")
LOG_FORMATS: tuple[str, ...] = ("json", "csv")
USER_STATUSES: tuple[str, ...] = ("active", "suspended", "blocked")
RULE_STATES: tuple[str, ...] = ("on", "off", "paused")


def suggest(value: str, options: Iterable[str], *, cutoff: float = 0.6) -> str:
    """A "did you mean" tail for a rejection, or an empty string.

    Included in the message rather than offered as a separate prompt: the user
    is looking at a stack trace of a failed command, not at a REPL.
    """
    matches = get_close_matches((value or "").strip().lower(), [o.lower() for o in options], n=3, cutoff=cutoff)
    if not matches:
        return ""
    return " — did you mean " + ", ".join(f"'{m}'" for m in matches) + "?"


def _digits(text: str) -> bool:
    return bool(text) and text.isdigit()


# ------------------------------------------------------------------- telegram
def valid_telegram_token(value: str) -> str | None:
    """BotFather token: `<5-12 digits>:<30+ chars>` (never echoed back)."""
    if not TOKEN_RE.match((value or "").strip()):
        return "that does not look like a BotFather token (expected <digits>:<35+ chars>)"
    return None


def valid_api_id(value: str) -> str | None:
    """`api_id` from my.telegram.org: a positive integer (32-bit apps exist)."""
    text = (value or "").strip()
    if not _digits(text):
        return "api_id must be numeric (from my.telegram.org)"
    if not 1 <= int(text) <= 10**12:
        return "api_id must be between 1 and 10^12"
    return None


def valid_api_hash(value: str) -> str | None:
    if not API_HASH_RE.match((value or "").strip()):
        return "api_hash must be exactly 32 hex characters (from my.telegram.org)"
    return None


def valid_telegram_user_id(value: str) -> str | None:
    """A Telegram user id. Digits only: `@name` is not an id and never a key."""
    text = (value or "").strip()
    if not _digits(text):
        return "a Telegram user id is numeric — send /id to @userinfobot to get yours"
    if not 1 <= int(text) < TELEGRAM_ID_MAX:
        return f"user id must be between 1 and {TELEGRAM_ID_MAX - 1}"
    return None


def valid_chat_ref(value: str, *, allow_username: bool = True) -> str | None:
    """A chat the forwarder can address: numeric id (channels are negative) or @name.

    Supergroups and channels are addressed by negative ids (`-100…`), and
    refusing them would make the most common target impossible to configure.
    """
    text = (value or "").strip()
    if not text:
        return "a chat id or @username is required"
    if text.startswith("@"):
        if not allow_username:
            return "this field takes a numeric chat id, not a username"
        if not USERNAME_RE.match(text[1:]):
            return "@username must be 5-32 characters (letters, digits, underscore)"
        return None
    if text.startswith("-") and text[1:].isdigit():
        number = int(text)
        if not -(TELEGRAM_ID_MAX) < number < 0:
            return "a negative chat id outside the Telegram range"
        return None
    if text.isdigit():
        if not 1 <= int(text) < TELEGRAM_ID_MAX:
            return f"chat id must be between 1 and {TELEGRAM_ID_MAX - 1}"
        return None
    return "expected a numeric chat id or @username"


def valid_phone(value: str) -> str | None:
    """E.164-ish phone number for the session login flow."""
    text = (value or "").strip().replace(" ", "").replace("-", "")
    if not PHONE_RE.match(text):
        return "expected an international phone number, e.g. +15551234567"
    return None


def valid_username(value: str) -> str | None:
    text = (value or "").strip().lstrip("@")
    if not USERNAME_RE.match(text):
        return "username must be 5-32 characters, starting with a letter"
    return None


# --------------------------------------------------------------------- numbers
def valid_int(value: str, *, minimum: int | None = None, maximum: int | None = None, label: str = "number") -> str | None:
    text = (value or "").strip()
    body = text[1:] if text.startswith(("-", "+")) else text
    if not body.isdigit():
        return f"{label} must be numeric, got {text!r}"
    number = int(text)
    if minimum is not None and number < minimum:
        return f"{label} must be >= {minimum}"
    if maximum is not None and number > maximum:
        return f"{label} must be <= {maximum}"
    return None


def valid_port(value: str) -> str | None:
    """A TCP port number. 1-65535; ports below 1024 need privileges."""
    problem = valid_int(value, minimum=1, maximum=65535, label="port")
    if problem:
        return problem
    if int(value) < 1024:
        return f"port {int(value)} is privileged (below 1024) — pick one above 1023 unless you run as root"
    return None


def _port_bindable(port: int, host: str = "127.0.0.1") -> bool:
    """Can this process bind the port? Loopback, not the wildcard.

    A listener on `0.0.0.0:<port>` refuses a loopback bind with EADDRINUSE, so
    this detects the conflicts that occur in practice while never opening a
    socket on every interface (see `ports.free_port`).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def valid_free_port(value: str, *, host: str = "127.0.0.1") -> str | None:
    """A port number that is also free *right now*.

    The bind test is what turns "port 8088 is already in use" from a build-time
    surprise into a refusal at the prompt. SO_REUSEADDR means a socket in
    TIME_WAIT does not count as taken.
    """
    problem = valid_port(value)
    if problem:
        return problem
    port = int(value)
    if not _port_bindable(port, host):
        return f"port {port} is already in use on {host or 'this host'} — pick another"
    return None


def valid_duration(value: str) -> str | None:
    """`45s`, `30m`, `12h`, `7d`, `2w` — the units the CLI's --older-than takes."""
    if not DURATION_RE.match((value or "").strip()):
        return "expected a duration like 45s, 30m, 12h, 7d or 2w"
    return None


def duration_seconds(value: str) -> int:
    """`30d` -> 2592000. Caller must have validated first (or pass a literal)."""
    match = DURATION_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"not a duration: {value!r}")
    amount = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    if unit.startswith("s"):
        return amount
    if unit.startswith("m"):
        return amount * 60
    if unit.startswith("h"):
        return amount * 3600
    if unit.startswith("d"):
        return amount * 86400
    return amount * 604800


# ----------------------------------------------------------------------- host
def valid_domain(value: str) -> str | None:
    """A DNS name. Blank is allowed: the wizard treats it as "no TLS"."""
    text = (value or "").strip().lower()
    if not text:
        return None
    if not DOMAIN_RE.match(text):
        return "that is not a valid domain name (e.g. api.example.com)"
    return None


def valid_hostname(value: str) -> str | None:
    """A domain, an IPv4 address or a container/service name (no spaces)."""
    text = (value or "").strip()
    if not text:
        return "a host is required"
    if valid_ipv4(text) is None:
        return None
    if DOMAIN_RE.match(text.lower()):
        return None
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$", text):
        return None  # a bare service name such as `core`, `api`, `9router`
    return "expected a hostname, IP address or service name"


def valid_ipv4(value: str) -> str | None:
    text = (value or "").strip()
    match = IPV4_RE.match(text)
    if not match or any(int(part) > 255 or (len(part) > 1 and part[0] == "0") for part in match.groups()):
        return "expected a dotted-quad IPv4 address, e.g. 127.0.0.1"
    return None


def valid_cidr(value: str) -> str | None:
    text = (value or "").strip()
    host, _, prefix = text.partition("/")
    problem = valid_ipv4(host)
    if problem:
        return problem
    if not prefix.isdigit() or not 0 <= int(prefix) <= 32:
        return "expected CIDR notation with a prefix of 0-32 bits, e.g. 10.0.0.0/8"
    return None


def valid_email(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not EMAIL_RE.match(text):
        return "that is not a valid email address"
    return None


def valid_url(value: str, *, schemes: Sequence[str] = ("http", "https"), allow_blank: bool = True) -> str | None:
    """An absolute URL with a host — `ftp://x` is not a dashboard URL."""
    text = (value or "").strip()
    if not text:
        return None if allow_blank else "a URL is required"
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://([^/\s?#]+)(.*)$", text)
    if not match:
        return "expected an absolute URL, e.g. http://127.0.0.1:8088"
    scheme, authority, _rest = match.groups()
    if scheme.lower() not in {s.lower() for s in schemes}:
        return f"{scheme}:// is not supported here — use " + " or ".join(f"{s}://" for s in schemes)
    host, port = _split_authority(authority)
    if port and not port.isdigit():
        return f"{port!r} is not a port number in that URL"
    if port and not 1 <= int(port) <= 65535:
        return f"port {port} is out of range in that URL"
    if valid_hostname(host):
        return f"{authority!r} is not a usable host in that URL"
    return None


def _split_authority(authority: str) -> tuple[str, str]:
    """`host:port` / `[v6]:port` / `host` -> (host, port)."""
    if authority.startswith("["):
        host, _, tail = authority[1:].partition("]")
        return host, tail.lstrip(":")
    host, separator, port = authority.rpartition(":")
    return (host, port) if separator else (authority, "")


def valid_hostport(value: str) -> str | None:
    """`host:port`, both parts checked."""
    text = (value or "").strip()
    if ":" not in text:
        return "expected host:port"
    host, port = _split_authority(text)
    if valid_hostname(host):
        return f"{host!r} is not a usable host"
    return valid_port(port)


# ------------------------------------------------------------------ vocabulary
def valid_choice(options: Iterable[str], *, label: str = "value") -> Callable[[str], str | None]:
    """A closed vocabulary with a did-you-mean tail."""
    allowed = tuple(options)

    def check(value: str) -> str | None:
        text = (value or "").strip()
        if text.lower() in {a.lower() for a in allowed}:
            return None
        return f"{label} must be one of: {', '.join(allowed)}" + suggest(text, allowed)

    return check


def normalise_choice(value: str, options: Iterable[str]) -> str:
    """`WARN` -> the option that matches it case-insensitively."""
    text = (value or "").strip().lower()
    for option in options:
        if option.lower() == text:
            return option
    return value.strip()


def valid_level(value: str) -> str | None:
    text = (value or "").strip().upper()
    if not text:
        return None
    if text in LEVELS:
        return None
    if text in _LEVEL_ALIASES:
        return None
    return f"log level must be one of: {', '.join(LEVELS)}" + suggest(text, LEVELS)


def normalise_level(value: str) -> str:
    text = (value or "").strip().upper()
    return _LEVEL_ALIASES.get(text, text)


def valid_language(value: str) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if text in LOCALES or text in _LOCALE_ALIASES:
        return None
    return f"language code must be one of: {', '.join(LOCALES)}" + suggest(text, LOCALES)


def normalise_language(value: str) -> str:
    text = (value or "").strip().lower().replace("_", "-")
    if text in LOCALES:
        return text
    return _LOCALE_ALIASES.get(text, text)


def valid_service(value: str, *, services: Sequence[str] = (), containers: Sequence[str] = ()) -> str | None:
    """A service or container name from this stack (with suggestions)."""
    text = (value or "").strip()
    options = tuple(dict.fromkeys([*services, *containers]))
    if not options:
        return None if text else "a service is required"
    if text in options:
        return None
    return f"unknown service {text!r}" + suggest(text, options)


def valid_service_name(value: str) -> str | None:
    """`valid_service` bound to this stack's real names, for argparse and logs."""
    from .host import CONTAINERS, SERVICES

    return valid_service(value, services=SERVICES, containers=tuple(CONTAINERS.values()))


def service_argparse_type(*, optional: bool = False) -> Callable[[str], str]:
    """argparse `type=` for a compose service, also accepting its container name.

    `atf docker logs atf-core` and `atf docker logs core` both work because the
    container name is an alias, not a second vocabulary.
    """
    from .host import CONTAINERS, SERVICES

    alias = {container: service for service, container in CONTAINERS.items()}
    return choice_argparse_type(SERVICES, label="service", alias=alias, optional=optional)


def valid_protocol(value: str) -> str | None:
    return valid_choice(("tcp", "udp"), label="protocol")(value)


# --------------------------------------------------------------------- tooling
def valid_git_ref(value: str) -> str | None:
    """A branch/tag/commit the CLI may hand to git.

    Deliberately narrow: the value reaches `git checkout`, so anything that
    could be read as an option (`-` prefixed) or as a revision range (`..`,
    `@{`) is refused rather than escaped.
    """
    text = (value or "").strip()
    if not text:
        return "a git ref is required"
    if text.startswith("-"):
        return "a git ref cannot start with '-'"
    if ".." in text or "@{" in text or "\\" in text or " " in text:
        return "that is not a plain branch, tag or commit"
    if not GIT_REF_RE.match(text):
        return "a git ref may contain letters, digits, '.', '_', '-' and '/'"
    return None


def valid_semver(value: str) -> str | None:
    if not SEMVER_RE.match((value or "").strip()):
        return "expected a semantic version such as 2.1.0"
    return None


def valid_rule_ref(value: str) -> str | None:
    """A task reference: full id, id prefix, or name (`resolve_rule` accepts all)."""
    text = (value or "").strip()
    if not text:
        return "a task id, id prefix or name is required"
    if text.startswith("-"):
        return "a task reference cannot start with '-'"
    if CONTROL_RE.search(text) or len(text) > 128:
        return "that does not look like a task id, prefix or name"
    return None


def valid_identifier(value: str) -> str | None:
    if not IDENTIFIER_RE.match((value or "").strip()):
        return "expected an identifier of letters, digits, '.', '_', '-' or ':' (max 128)"
    return None


def valid_text(value: str, *, maximum: int = 4096, label: str = "text") -> str | None:
    text = value or ""
    if not text.strip():
        return f"{label} cannot be empty"
    if len(text) > maximum:
        return f"{label} is limited to {maximum} characters (got {len(text)})"
    if CONTROL_RE.search(text):
        return f"{label} contains control characters"
    return None


def valid_key_value(value: str) -> str | None:
    """One `--set key=value` pair, where key may be a dotted path."""
    text = (value or "").strip()
    if "=" not in text:
        return "expected key=value"
    key, _, _raw = text.partition("=")
    if not KV_KEY_RE.match(key.strip()):
        return f"{key.strip()!r} is not a usable setting key (letters, digits, '_', '-' and '.')"
    return None


def valid_json(value: str) -> str | None:
    text = (value or "").strip()
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return "expected valid JSON"
    return None


def valid_regex(value: str) -> str | None:
    if not value:
        return "a pattern is required"
    try:
        re.compile(value)
    except re.error as exc:
        return f"invalid regular expression: {exc}"
    return None


def valid_path(value: str, *, root: Path | str | None = None, must_exist: bool = False) -> str | None:
    """A filesystem path that stays inside `root` when one is given.

    Path traversal is refused rather than normalised away: a backup restored to
    `../../etc` is a different action from the one the operator asked for.
    """
    text = (value or "").strip()
    if not text:
        return "a path is required"
    if CONTROL_RE.search(text):
        return "that path contains control characters"
    path = Path(text).expanduser()
    if root is not None:
        base = Path(root).expanduser().resolve()
        resolved = (base / path).resolve() if not path.is_absolute() else path.resolve()
        if resolved != base and base not in resolved.parents:
            return f"path must stay inside {base}"
        path = resolved
    if must_exist and not path.exists():
        return f"{path} does not exist"
    return None


# ------------------------------------------------------------------ the registry
#: Name -> validator, so a caller that only knows the *kind* of value (the
#: wizard's answers file, a generic `--set` loop) can still validate it.
VALIDATORS: dict[str, Callable[..., str | None]] = {
    "telegram_token": valid_telegram_token,
    "api_id": valid_api_id,
    "api_hash": valid_api_hash,
    "telegram_user_id": valid_telegram_user_id,
    "chat_ref": valid_chat_ref,
    "phone": valid_phone,
    "username": valid_username,
    "int": valid_int,
    "port": valid_port,
    "free_port": valid_free_port,
    "duration": valid_duration,
    "domain": valid_domain,
    "hostname": valid_hostname,
    "ipv4": valid_ipv4,
    "cidr": valid_cidr,
    "email": valid_email,
    "url": valid_url,
    "hostport": valid_hostport,
    "level": valid_level,
    "language": valid_language,
    "service": valid_service_name,
    "git_ref": valid_git_ref,
    "semver": valid_semver,
    "rule_ref": valid_rule_ref,
    "identifier": valid_identifier,
    "text": valid_text,
    "key_value": valid_key_value,
    "json": valid_json,
    "regex": valid_regex,
    "path": valid_path,
    "protocol": valid_protocol,
}


#: Kinds whose value is genuinely a number: `type=` must hand the command an
#: `int`, not the string the validator inspected. Anything else stays text —
#: telegram ids and tokens are compared, never counted.
_NUMERIC_KINDS: dict[str, Callable[[str], int]] = {"int": int, "port": int, "free_port": int}


def check(kind: str, value: str, **options) -> str | None:
    """Validate `value` as `kind`. Unknown kinds are a programming error."""
    validator = VALIDATORS.get(kind)
    if validator is None:
        raise KeyError(f"unknown validator {kind!r}")
    return validator(value, **options)


def validate(kind: str, value: str, **options) -> str:
    """Return the value, or raise [ValueError] with the reason it is wrong."""
    problem = check(kind, value, **options)
    if problem:
        raise ValueError(problem)
    return value


def argparse_type(kind: str, *, optional: bool = False, **options) -> Callable[[str], Any]:
    """Adapt a validator to argparse's `type=` callable.

    argparse reports a failing `type` as "invalid <name> value" plus the
    exception text and exits 2 — the same exit code as any other usage error,
    which is what a wrapper script expects for "you typed it wrong".

    `optional=True` lets an empty string through unvalidated, for flags whose
    default is `""` and where empty means "no filter" (`--level`, `--service`).
    Without it, `--level=""` would be refused by a validator that is only meant
    to police actual values.

    A numeric kind returns an `int`: `type=` is where argparse converts, and a
    validated-but-still-string number reaches slicing and arithmetic
    (`entries[-limit:]` raised "bad operand type for unary -: 'str'"). Kinds
    that name things (telegram ids, paths, tokens) stay strings — the callers
    treat those as text.
    """

    def parse(raw: str) -> Any:
        if optional and raw == "":
            return ""
        problem = check(kind, raw, **options)
        if problem:
            raise argparse.ArgumentTypeError(problem)
        coerce = _NUMERIC_KINDS.get(kind)
        return coerce(raw) if coerce is not None else raw

    parse.__name__ = f"valid_{kind}"
    return parse


def describe_choices(options: Iterable[str]) -> str:
    """The options as a help string, for `metavar`/`help` text."""
    return ", ".join(options)


#: The `--only` vocabulary of `atf doctor`, kept here so the doctor command and
#: the CLI reference cannot disagree about the check names.
def choice_argparse_type(
    options: Sequence[str],
    *,
    label: str = "value",
    alias: dict[str, str] | None = None,
    optional: bool = False,
) -> Callable[[str], str]:
    """argparse `type=` for a closed vocabulary that also normalises aliases.

    `optional=True` accepts `""` as "not specified"; the vocabulary is still
    policed for every other value.
    """
    allowed = tuple(options)
    aliases = dict(alias or {})

    def parse(raw: str) -> str:
        text = (raw or "").strip()
        if optional and text == "":
            return ""
        lowered = text.lower()
        for option in allowed:
            if option.lower() == lowered:
                return option
        if lowered in aliases:
            return aliases[lowered]
        raise argparse.ArgumentTypeError(
            f"{label} must be one of: {describe_choices(allowed)}" + suggest(text, allowed)
        )

    parse.__name__ = f"choice_{label}"
    return parse


# --------------------------------------------------- vocabulary for the CLI surfaces
#: Audit outcomes the server records. The query route takes the name verbatim, and
#: an unknown one is silently ignored, so it is validated here.
OUTCOMES: tuple[str, ...] = ("SUCCEEDED", "FAILED", "DENIED", "UNKNOWN")
#: Audit severities, matching the proto.
SEVERITIES: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
#: How a task reaches its target.
ROUTE_PATHS: tuple[str, ...] = ("direct", "intermediate")


def valid_outcome(value: str) -> str | None:
    wanted = value.strip().upper()
    if wanted in OUTCOMES:
        return None
    return f"outcome must be one of: {', '.join(OUTCOMES)}" + suggest(wanted, OUTCOMES)


def valid_severity(value: str) -> str | None:
    wanted = value.strip().upper()
    if wanted in SEVERITIES:
        return None
    return f"severity must be one of: {', '.join(SEVERITIES)}" + suggest(wanted, SEVERITIES)


def valid_route_path(value: str) -> str | None:
    wanted = value.strip().lower()
    if wanted in ROUTE_PATHS:
        return None
    return f"route path must be one of: {', '.join(ROUTE_PATHS)}" + suggest(wanted, ROUTE_PATHS)


def coerce(kind: str, text: str) -> Any:
    """The type the command should receive for a validated string.

    `argparse_type` already converts numbers; this is the same rule for callers
    that validate first and convert themselves (the wizard, `--set` values).
    """
    converter = _NUMERIC_KINDS.get(kind)
    return converter(text) if converter else text.strip()


VALIDATORS.update(
    {
        "outcome": valid_outcome,
        "severity": valid_severity,
        "route_path": valid_route_path,
    }
)
