"""Presentation helpers for the interactive parts of the CLI.

Everything here writes through [Console], so the redaction rule still holds:
no widget in this module can print a secret, because nothing here prints
directly.

The gradients are truecolor ANSI and degrade to plain text when colour is off
(`NO_COLOR`, `--no-color`, a pipe, or `--json`).
"""

from __future__ import annotations

import getpass
import re
import sys
from typing import Callable, Iterable, Sequence

RGB = tuple[int, int, int]

#: Brand ramp: sky → violet. Used for headings and box borders.
BRAND: tuple[RGB, RGB] = ((56, 189, 248), (168, 85, 247))
GOOD: tuple[RGB, RGB] = ((34, 197, 94), (163, 230, 53))
WARN: tuple[RGB, RGB] = ((250, 204, 21), (251, 146, 60))
BAD: tuple[RGB, RGB] = ((248, 113, 113), (239, 68, 68))
INK: tuple[RGB, RGB] = ((148, 163, 184), (203, 213, 225))

_RESET = "\033[0m"


def _lerp(a: int, b: int, t: float) -> int:
    return int(round(a + (b - a) * t))


def gradient(text: str, start: RGB = BRAND[0], end: RGB = BRAND[1]) -> str:
    """Per-character truecolor ramp. Caller decides whether colour is on."""
    if not text:
        return text
    span = max(1, len(text) - 1)
    out = []
    for index, char in enumerate(text):
        r, g, b = (_lerp(start[i], end[i], index / span) for i in range(3))
        out.append(f"\033[38;2;{r};{g};{b}m{char}")
    return "".join(out) + _RESET


def tinted(console, text: str, ramp: tuple[RGB, RGB] = BRAND) -> str:
    """Gradient when the console has colour, plain text otherwise."""
    return gradient(text, *ramp) if getattr(console, "color", False) else text


def visible_width(text: str) -> int:
    """Length without ANSI escapes."""
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def banner(console, title: str, subtitle: str = "", *, ramp: tuple[RGB, RGB] = BRAND) -> None:
    """A bordered gradient header. Never shown in JSON mode."""
    if console.json_mode or console.quiet:
        return
    width = max(visible_width(title), visible_width(subtitle)) + 6
    width = min(max(width, 46), console.width())
    edge = tinted(console, "─" * width, ramp)
    console.line()
    console.line(edge)
    console.line("  " + tinted(console, title, ramp))
    if subtitle:
        console.line("  " + console.paint(subtitle, "grey"))
    console.line(edge)


def box(console, rows: Sequence[tuple[str, str]], *, title: str = "", ramp: tuple[RGB, RGB] = BRAND) -> None:
    """Key/value panel inside a gradient frame."""
    if console.json_mode or console.quiet or not rows:
        return
    pad = max((visible_width(str(k)) for k, _ in rows), default=0)
    body = [f"  {console.paint(str(k).ljust(pad), 'grey')}  {v}" for k, v in rows]
    inner = max([visible_width(line) for line in body] + [visible_width(title) + 2]) + 2
    console.line(tinted(console, "╭" + "─" * inner + "╮", ramp))
    if title:
        label = f" {title} "
        console.line(tinted(console, "│", ramp) + label.center(inner) + tinted(console, "│", ramp))
    for line in body:
        console.line(tinted(console, "│", ramp) + line.ljust(inner + (len(line) - visible_width(line))) + tinted(console, "│", ramp))
    console.line(tinted(console, "╰" + "─" * inner + "╯", ramp))


def timeline(console, entries: Sequence[tuple[str, str]], *, total: int | None = None) -> None:
    """A step list: (step name, status) where status is ok|warn|fail|skip."""
    if console.json_mode or console.quiet:
        return
    count = total or len(entries)
    marks = {
        "ok": ("✓", GOOD),
        "warn": ("!", WARN),
        "fail": ("✗", BAD),
        "skip": ("·", INK),
    }
    for index, (name, status) in enumerate(entries, 1):
        mark, ramp = marks.get(status, ("·", INK))
        counter = console.paint(f"[{index}/{count}]", "grey")
        console.line(f"  {counter} {tinted(console, mark, ramp)} {name}")


# --------------------------------------------------------------------- prompts
class AnswerError(ValueError):
    """Raised by a validator to reject an answer (message shown to the user)."""


def _read_line(prompt_text: str, *, secret: bool = False) -> str:
    # Flush first: with piped or redirected output the question would otherwise
    # appear *after* the answer it is waiting for.
    sys.stdout.flush()
    try:
        if secret:
            return getpass.getpass(prompt_text)
        return input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        # Non-interactive stdin (piped answers, cron): fall through to defaults.
        print()
        return ""


def ask(
    console,
    question: str,
    *,
    default: str = "",
    validate: Callable[[str], str | None] | None = None,
    hint: str = "",
    secret: bool = False,
    tries: int = 3,
    required: bool = False,
) -> str:
    """Ask until the answer validates, or the default is accepted.

    `validate` returns an error message for a bad answer, or None when it is
    good. Non-interactive input (EOF) accepts the default instead of hanging.
    """
    if console.json_mode:
        return default
    if secret:
        shown = "[kept from .env]" if default else "[required]"
    else:
        shown = default
    console.line()
    console.line("  " + console.paint(question, "bold") + (f"  {console.paint('[' + shown + ']', 'grey')}" if shown else ""))
    if hint:
        console.line("  " + console.paint(hint, "grey"))
    for attempt in range(tries):
        raw = _read_line("   › ", secret=secret).strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            console.warn("  this one is required")
            continue
        if validate is not None:
            problem = validate(raw)
            if problem:
                console.warn(f"  {problem}" if attempt + 1 < tries else f"  {problem} (last try, using the default)")
                if attempt + 1 >= tries:
                    return default
                continue
        return raw
    return default


def confirm(console, question: str, *, default: bool = True) -> bool:
    if console.json_mode:
        return default
    suffix = "Y/n" if default else "y/N"
    raw = _read_line(f"  {question} [{suffix}] › ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "1", "true")


# ------------------------------------------------------------------- validators
def valid_telegram_token(value: str) -> str | None:
    """BotFather tokens look like 123456789:AA... (never echo the value back)."""
    if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,}", value.strip()):
        return "that does not look like a BotFather token (expected <digits>:<35+ chars>)"
    return None


def valid_int(value: str, *, minimum: int = 1, maximum: int | None = None, label: str = "number") -> str | None:
    text = value.strip().lstrip("-")
    if not text.isdigit():
        return f"{label} must be numeric"
    number = int(text)
    if number < minimum:
        return f"{label} must be >= {minimum}"
    if maximum is not None and number > maximum:
        return f"{label} must be <= {maximum}"
    return None


def _listening_ports() -> set[int] | None:
    """Local TCP ports with a listener, from the kernel's own tables.

    Returns None when the tables are unreadable (non-Linux), so the caller can
    fall back to a probe.
    """
    ports: set[int] = set()
    readable = False
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii", errors="replace") as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        readable = True
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A == TCP_LISTEN
                continue
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except (IndexError, ValueError):
                continue
    return ports if readable else None


def _port_available(port: int) -> bool:
    """True when nothing on this host already listens on ``port``.

    Asked of the kernel rather than by binding a probe socket: a probe has to
    pick an interface, and binding the wildcard address would publish a
    listening socket on every interface for the duration of the check.
    """
    ports = _listening_ports()
    if ports is not None:
        return port not in ports
    import socket

    # No kernel table (macOS/Windows): ask the loopback address directly. A
    # connect probe reaches anything bound to 0.0.0.0 and never listens.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def valid_port(value: str) -> str | None:
    problem = valid_int(value, minimum=1, maximum=65535, label="port")
    if problem:
        return problem
    port = int(value)
    if not _port_available(port):
        return f"port {port} is already in use — pick another"
    return None


def valid_domain(value: str) -> str | None:
    if value and not re.fullmatch(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}", value.strip().lower()):
        return "that is not a valid domain name"
    return None


def valid_choice(options: Iterable[str]) -> Callable[[str], str | None]:
    allowed = tuple(options)

    def check(value: str) -> str | None:
        if value.strip().lower() not in allowed:
            return f"choose one of: {', '.join(allowed)}"
        return None

    return check


def table_with_marks(console, headers: Sequence[str], rows: Sequence[Sequence[str]], status_col: int) -> None:
    """`console.table` with the status cell colourised."""
    if console.json_mode:
        return
    painted = []
    for row in rows:
        cells = [str(c) for c in row]
        status = cells[status_col].lower()
        if status.startswith("ok") or status in ("yes", "healthy", "running"):
            cells[status_col] = console.paint(cells[status_col], "green")
        elif status.startswith("warn"):
            cells[status_col] = console.paint(cells[status_col], "yellow")
        elif status.startswith("fail") or status in ("no", "down"):
            cells[status_col] = console.paint(cells[status_col], "red")
        painted.append(cells)
    console.table(headers, painted)
