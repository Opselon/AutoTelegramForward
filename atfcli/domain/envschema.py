"""The `.env` document as a value object, plus the schema of the keys ATF reads.

Why a document type instead of a dict
-------------------------------------
`.env` is not a key/value store the operator only machine-writes: it is the file
they hand-edit, with comment blocks that explain *why* a value is what it is. A
"read into a dict, write a dict back" round trip silently deletes every comment
the first time the CLI touches the file. So the document keeps its lines in
order, keeps comments and blank lines and even lines it could not parse, and a
patch rewrites only the lines whose value actually changed.

Why a schema
------------
"A wrong value in `.env`" is the most expensive class of mistake on this stack: a
bot token with a stray space, a port outside the range, `ATF_RATELIMIT_ALGO=tb
` with a trailing tab, an api_id that is not a number. Every one of them starts
as a value the operator typed and ends as a container in a restart loop. The
schema states, once, what each key means, so the same rule is used by:

* `atf config set` (refuse before writing),
* `atf config validate` (find what is already wrong),
* the admin console (ask again instead of storing a wrong answer),
* the wizard's answers file.

Unknown-but-real keys are *not* refused: the codebase reads more keys than this
table documents (queue tuning, diagnostics), and refusing them would break a
working deployment on upgrade. They are allowed and reported as unknown, so
`--strict` can still fail a review.

Nothing here performs I/O: parsing, rendering and validation are pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Sequence

from . import validators
from .secrets import is_secret_name

#: Keys compose and the core read: upper case, digits, underscore.
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: A value that would end the line (`\\n`), start a comment (`#` at line start),
#: or look like a second assignment (`=`) has no business in a single-line
#: dotenv value. Refusing these is what makes "write a key" unable to become
#: "write several keys" — the injection this file exists to prevent.
FORBIDDEN_VALUE_CHARS = ("\n", "\r", "\x00")

#: Placeholders shipped in `.env.example`. A deployment still carrying one is not
#: configured, even though every value is syntactically valid.
PLACEHOLDERS: Mapping[str, str] = {
    "ATF_BOT_TOKEN": "123456:ABC-your-bot-token",
    "ATF_MASTER_KEY": "generate-a-long-random-string-here",
    "ATF_API_KEY": "optional-rest-api-key",
}


@dataclass(frozen=True)
class EnvVarSpec:
    """One documented key: what it is, what it may hold, who may see it."""

    name: str
    kind: str = "text"
    group: str = "runtime"
    description: str = ""
    secret: bool = False
    #: Explicit "this documented key is not a credential" (e.g. `ATF_API_ID`):
    #: it silences the *name pattern only*. A key the schema knows nothing about
    #: is always judged by its name, on the fail-closed side.
    public: bool = False
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    min_length: int = 0
    default: str = ""

    @property
    def masked(self) -> bool:
        """True when the value must never be printed.

        The union of every signal, so a missed `secret=True` cannot turn into a
        leak: the explicit flag, or the name pattern unless the spec is
        explicitly `public`. `ATF_API_ID` is the only public key — it is not a
        credential and operators need it to diagnose auth.
        """
        if self.secret:
            return True
        return is_secret_name(self.name) and not self.public

    def validate(self, value: str) -> str | None:
        """A problem string for `value`, or None. Pure."""
        text = "" if value is None else str(value)
        if any(char in text for char in FORBIDDEN_VALUE_CHARS):
            return "value must be a single line without control characters"
        if not text.strip():
            return "value is empty" if self.required else None
        # Before the shape rule: the placeholder from .env.example is often
        # *almost* a valid value (a token-shaped string), and "still the
        # placeholder" is the sentence that tells the operator what to do.
        if PLACEHOLDERS.get(self.name) and text.strip() == PLACEHOLDERS[self.name]:
            return "still the placeholder from .env.example — replace it with the real value"
        if self.choices:
            problem = validators.valid_choice(self.choices)(text)
            if problem:
                return problem
        elif self.kind == "user_id_list":
            problem = _validate_id_list(text)
            if problem:
                return problem
        elif self.kind != "text":
            options: dict[str, object] = {}
            if self.kind == "int":
                if self.minimum is not None:
                    options["minimum"] = self.minimum
                if self.maximum is not None:
                    options["maximum"] = self.maximum
            problem = validators.check(self.kind, text, **options)
            if problem:
                return problem
        if self.min_length and len(text.strip()) < self.min_length:
            return f"must be at least {self.min_length} characters"
        return None

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "group": self.group,
            "kind": self.kind,
            "secret": self.masked,
            "required": self.required,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default": "[REDACTED]" if self.masked else self.default,
            "description": self.description,
        }


# --------------------------------------------------------------------- schema
#: The operator-facing `.env`. Order is the order `atf config schema` prints,
#: grouped by the area of the stack the key configures.
SCHEMA: tuple[EnvVarSpec, ...] = (
    # -- telegram -----------------------------------------------------------
    EnvVarSpec(
        "ATF_API_ID",
        kind="api_id",
        public=True,  # not a credential: printed so operators can diagnose auth
        group="telegram",
        required=True,
        description="api_id from my.telegram.org (a number, not a secret)",
    ),
    EnvVarSpec(
        "ATF_API_HASH",
        kind="api_hash",
        secret=True,
        group="telegram",
        required=True,
        description="api_hash from my.telegram.org (32 hex characters)",
    ),
    EnvVarSpec(
        "ATF_BOT_TOKEN",
        kind="telegram_token",
        secret=True,
        group="telegram",
        required=True,
        description="BotFather token, `<digits>:<35+ chars>`",
    ),
    EnvVarSpec(
        "ADMIN_USER_ID",
        kind="telegram_user_id",
        group="admin",
        required=True,
        description="the Telegram account that owns the first admin session",
    ),
    EnvVarSpec(
        "ADMIN_IDS",
        kind="user_id_list",
        group="admin",
        description="comma-separated additional administrator ids",
    ),
    # -- security -----------------------------------------------------------
    EnvVarSpec(
        "ATF_MASTER_KEY",
        group="security",
        secret=True,
        required=True,
        min_length=16,
        description="key material for stored credentials; changing it orphans them",
    ),
    EnvVarSpec(
        "ATF_JWT_SECRET",
        group="security",
        secret=True,
        min_length=32,
        description="optional shared session-signing key (32+ chars); unset is the safe default",
    ),
    EnvVarSpec(
        "ATF_API_KEY",
        group="security",
        secret=True,
        description="optional static API key for the REST surface",
    ),
    EnvVarSpec(
        "ATF_CORE_RPC_TOKEN",
        group="security",
        secret=True,
        description="token the API uses to authenticate to the core gRPC server",
    ),
    # -- web ----------------------------------------------------------------
    EnvVarSpec(
        "ATF_WEB_URL",
        kind="url",
        group="web",
        description="public URL this deployment advertises (dashboard + health links)",
    ),
    EnvVarSpec(
        "ATF_WEB_PORT",
        kind="port",
        group="web",
        default="8088",
        description="host port mapped to the web container",
    ),
    EnvVarSpec(
        "ATF_TLS_DOMAIN",
        kind="domain",
        group="web",
        description="domain Caddy issues a certificate for (unset = plain HTTP)",
    ),
    EnvVarSpec(
        "ATF_TLS_EMAIL",
        kind="email",
        group="web",
        description="ACME account email",
    ),
    # -- redis --------------------------------------------------------------
    EnvVarSpec(
        "ATF_REDIS_ADDR",
        kind="hostport",
        group="redis",
        default="redis:6379",
        description="Redis address used by the API rate limiter and the core lease coordinator",
    ),
    EnvVarSpec(
        "ATF_REDIS_PASSWORD",
        group="redis",
        secret=True,
        description="Redis AUTH password, when the server requires one",
    ),
    EnvVarSpec(
        "ATF_REDIS_MAXMEMORY",
        group="redis",
        default="256mb",
        description="Redis maxmemory (Redis size syntax: 256mb)",
    ),
    EnvVarSpec(
        "ATF_REDIS_MAXMEMORY_POLICY",
        group="redis",
        choices=(
            "noeviction",
            "allkeys-lru",
            "volatile-lru",
            "allkeys-random",
            "volatile-random",
            "volatile-ttl",
        ),
        default="noeviction",
        description="Redis eviction policy; noeviction keeps lease keys alive",
    ),
    EnvVarSpec(
        "ATF_RATELIMIT_ALGO",
        group="redis",
        # tb/sw/swe are the three algorithms `ratelimit.Algo.Valid` accepts.
        choices=("tb", "sw", "swe"),
        description="rate-limit algorithm: token bucket, sliding window, exact window",
    ),
    # -- runtime ------------------------------------------------------------
    EnvVarSpec(
        "ATF_TASK_RETENTION_SECONDS",
        kind="int",
        group="runtime",
        minimum=0,
        description="how long finished-task bookkeeping is kept (0 = forever)",
    ),
    EnvVarSpec(
        "ATF_CHANNEL_POLL_INTERVAL",
        kind="int",
        group="runtime",
        minimum=1,
        description="seconds between channel polls when the event feed is unavailable",
    ),
    EnvVarSpec(
        "ATF_QUEUE_NUM_WORKERS",
        kind="int",
        group="runtime",
        minimum=1,
        maximum=64,
        description="delivery workers per core process",
    ),
    EnvVarSpec(
        "ATF_QUEUE_MAX_SIZE",
        kind="int",
        group="runtime",
        minimum=1,
        description="queued deliveries allowed before back-pressure",
    ),
    EnvVarSpec(
        "ATF_QUEUE_MAX_PER_MINUTE",
        kind="int",
        group="runtime",
        minimum=1,
        description="outbound sends per minute across all rules",
    ),
    EnvVarSpec(
        "ATF_QUEUE_LEASE_SECONDS",
        kind="int",
        group="runtime",
        minimum=1,
        description="how long a worker's lease on a delivery lasts",
    ),
    EnvVarSpec(
        "ATF_QUEUE_BATCH_SIZE",
        kind="int",
        group="runtime",
        minimum=1,
        maximum=1000,
        description="deliveries claimed per lease round",
    ),
    EnvVarSpec(
        "ATF_DISABLE_BOT",
        group="runtime",
        choices=("0", "1", "true", "false", "yes", "no"),
        description="run the stack without the bot (dashboard/API only)",
    ),
    EnvVarSpec(
        "ATF_RAW_API_TRACE",
        group="runtime",
        choices=("0", "1", "true", "false"),
        description="log raw Telegram API frames (diagnostics; never leave on)",
    ),
    EnvVarSpec(
        "ATF_ALLOW_PRIVATE_AI_ENDPOINTS",
        group="runtime",
        choices=("0", "1"),
        description="allow AI endpoints on private peers (loopback/metadata stay refused)",
    ),
    # -- logging ------------------------------------------------------------
    EnvVarSpec(
        "ATF_LOG_LEVELS",
        group="logging",
        description="per-service log level overrides, e.g. core=DEBUG,api=INFO",
    ),
    EnvVarSpec(
        "ATF_TRUSTED_PROXIES",
        group="logging",
        description="comma-separated CIDRs whose X-Forwarded-For is believed",
    ),
)

#: Name -> spec, for the hot path (validation of a submitted change).
BY_NAME: Mapping[str, EnvVarSpec] = {spec.name: spec for spec in SCHEMA}

#: Groups in the order they are documented, so `atf config schema` reads like the
#: stack rather than like an alphabetised dump.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(spec.group for spec in SCHEMA))


def spec_for(name: str) -> EnvVarSpec | None:
    return BY_NAME.get(str(name or "").strip())


def is_masked(name: str) -> bool:
    """Whether `name`'s value must be masked in output.

    Documented keys answer from the schema; anything else answers from the
    credential-name pattern, so a key the CLI has never heard of is masked rather
    than printed.
    """
    spec = spec_for(name)
    return is_secret_name(name) if spec is None else spec.masked


def specs_in(group: str) -> list[EnvVarSpec]:
    return [spec for spec in SCHEMA if spec.group == group]


def required_keys() -> tuple[str, ...]:
    return tuple(spec.name for spec in SCHEMA if spec.required)


# ------------------------------------------------------------------ the rules
def _validate_id_list(value: str) -> str | None:
    """`ADMIN_IDS`: a comma-separated list of Telegram user ids."""
    parts = [part.strip() for part in str(value or "").split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return "at least one user id is required"
    for part in parts:
        problem = validators.valid_telegram_user_id(part)
        if problem:
            return f"{part!r}: {problem}"
    return None


def validate_key(name: str) -> str | None:
    """A problem string for a key name, or None.

    The shape rule is the security boundary: a key that cannot contain `=`, a
    newline or a `#` cannot turn one assignment into two.
    """
    text = str(name or "")
    if not text:
        return "a key is required"
    if not KEY_RE.match(text):
        return (
            f"{text!r} is not a valid key name "
            "(upper case, digits and underscore, starting with a letter)"
        )
    return None


def validate_value(name: str, value: str, *, allow_unknown: bool = True) -> str | None:
    """A problem string for `name=value`, or None.

    An undocumented key is accepted (the codebase reads more keys than the
    schema documents) but still has to be a valid key name and a single line.
    """
    problem = validate_key(name)
    if problem:
        return problem
    text = "" if value is None else str(value)
    if any(char in text for char in FORBIDDEN_VALUE_CHARS):
        return "value must be a single line without control characters"
    spec = spec_for(name)
    if spec is None:
        if allow_unknown:
            return None
        return (
            f"{name} is not a documented setting"
            + (validators.suggest(name, tuple(BY_NAME)) if BY_NAME else "")
        )
    return spec.validate(text)


def describe(name: str) -> str:
    """One sentence for a key: what it does, what it accepts."""
    spec = spec_for(name)
    if spec is None:
        return ""
    bits = [spec.description]
    if spec.choices:
        bits.append("one of: " + ", ".join(spec.choices))
    if spec.kind == "int" and (spec.minimum is not None or spec.maximum is not None):
        low = spec.minimum if spec.minimum is not None else "-inf"
        high = spec.maximum if spec.maximum is not None else "inf"
        bits.append(f"range {low}..{high}")
    return " — ".join(part for part in bits if part)


# ---------------------------------------------------------------- the document
@dataclass(frozen=True)
class EnvLine:
    """One line of the file, kept as it was found.

    ``kind`` is ``blank``, ``comment``, ``pair`` or ``raw`` (a line that is not
    an assignment and not a comment — kept verbatim so a patch never eats it).
    """

    kind: str
    raw: str = ""
    key: str = ""
    value: str = ""

    def render(self) -> str:
        if self.kind == "pair":
            return render_assignment(self.key, self.value)
        return self.raw

    def to_json(self) -> dict[str, str]:
        if self.kind == "pair":
            return {"kind": self.kind, "key": self.key}
        return {"kind": self.kind, "raw": self.raw}


def render_value(value: str) -> str:
    """Render a value so it round-trips through the parser.

    Quoting is applied when the value contains a character a dotenv reader would
    otherwise reinterpret (whitespace, `#`, quotes, backslash) — Telegram tokens,
    ids and URLs stay unquoted and readable.
    """
    text = "" if value is None else str(value)
    if text == "":
        return ""
    if re.search(r"[\s#'\"\\]", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def render_assignment(key: str, value: str) -> str:
    return f"{key}={render_value(value)}"


def _unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return text


@dataclass(frozen=True)
class EnvDocument:
    """`.env` as an ordered list of lines, patchable without losing comments."""

    lines: tuple[EnvLine, ...] = field(default_factory=tuple)

    # ---------------------------------------------------------------- parsing
    @classmethod
    def parse(cls, text: str) -> "EnvDocument":
        """Parse `text`, preserving every line that is not an assignment."""
        lines: list[EnvLine] = []
        for raw in (text or "").splitlines():
            stripped = raw.strip()
            if not stripped:
                lines.append(EnvLine("blank", raw=raw))
                continue
            if stripped.startswith("#"):
                lines.append(EnvLine("comment", raw=raw))
                continue
            key, sep, value = raw.partition("=")
            candidate = key.strip()
            if sep and KEY_RE.match(candidate):
                lines.append(
                    EnvLine("pair", raw=raw, key=candidate, value=_unquote(value))
                )
            else:
                # `export KEY=value`, a bare word, a broken continuation: keep it
                # byte-for-byte rather than pretending to understand it.
                lines.append(EnvLine("raw", raw=raw))
        return cls(tuple(lines))

    # --------------------------------------------------------------- reading
    def values(self) -> dict[str, str]:
        """Every assignment; the last occurrence wins, as dotenv readers behave."""
        found: dict[str, str] = {}
        for line in self.lines:
            if line.kind == "pair":
                found[line.key] = line.value
        return found

    def occurrences(self, key: str) -> int:
        return sum(1 for line in self.lines if line.kind == "pair" and line.key == key)

    def duplicates(self) -> list[str]:
        """Keys assigned more than once — the earlier value never takes effect."""
        counts: dict[str, int] = {}
        for line in self.lines:
            if line.kind == "pair":
                counts[line.key] = counts.get(line.key, 0) + 1
        return sorted(key for key, count in counts.items() if count > 1)

    def keys(self) -> list[str]:
        return sorted(self.values())

    def get(self, key: str, default: str = "") -> str:
        return self.values().get(key, default)

    # --------------------------------------------------------------- patching
    def with_values(self, changes: Mapping[str, str]) -> "EnvDocument":
        """Return a new document with `changes` applied.

        Every occurrence of a changed key is rewritten (not just the first): two
        assignments of one key is a bug in the file, and leaving the older one
        behind would mean `atf config set` reported success while the effective
        value came from somewhere else. Keys that are absent are appended.
        """
        pending = {str(k): ("" if v is None else str(v)) for k, v in changes.items()}
        written: set[str] = set()
        lines: list[EnvLine] = []
        for line in self.lines:
            if line.kind == "pair" and line.key in pending:
                value = pending[line.key]
                if line.value != value:
                    lines.append(EnvLine("pair", key=line.key, value=value))
                else:
                    lines.append(line)
                written.add(line.key)
                continue
            lines.append(line)
        for key, value in pending.items():
            if key in written:
                continue
            lines.append(EnvLine("pair", key=key, value=value))
        return EnvDocument(tuple(lines))

    def without_keys(self, keys: Iterable[str]) -> "EnvDocument":
        """Drop the assignments for `keys` (comments are never touched)."""
        wanted = {str(key) for key in keys}
        return EnvDocument(
            tuple(
                line
                for line in self.lines
                if not (line.kind == "pair" and line.key in wanted)
            )
        )

    # -------------------------------------------------------------- rendering
    def render(self) -> str:
        """The file's text, with exactly one trailing newline.

        A file that ends without a newline would concatenate a later append onto
        its last line; a file with five blank lines at the end accumulates them
        on every write. One trailing newline is the stable fixed point.
        """
        body = "\n".join(line.render() for line in self.lines)
        body = body.rstrip("\n")
        return body + "\n" if body else ""

    def changed_lines(self, changes: Mapping[str, str]) -> list[str]:
        """Which keys in `changes` would actually change the file, in order."""
        current = self.values()
        return [
            key
            for key, value in changes.items()
            if current.get(key, None) != value
        ]

    def __iter__(self) -> Iterator[EnvLine]:
        return iter(self.lines)


def patch_text(text: str, changes: Mapping[str, str]) -> str:
    """Convenience: parse, patch, render — the pure part of a write."""
    return EnvDocument.parse(text).with_values(changes).render()


def specs_json() -> list[dict[str, object]]:
    """The schema as a JSON document (`atf config schema --json`)."""
    return [spec.to_json() for spec in SCHEMA]


__all__ = [
    "BY_NAME",
    "GROUPS",
    "KEY_RE",
    "PLACEHOLDERS",
    "SCHEMA",
    "EnvDocument",
    "EnvLine",
    "EnvVarSpec",
    "describe",
    "is_masked",
    "patch_text",
    "render_assignment",
    "render_value",
    "required_keys",
    "spec_for",
    "specs_in",
    "specs_json",
    "validate_key",
    "validate_value",
]
