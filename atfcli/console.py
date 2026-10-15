"""Terminal output for the ATF CLI.

Two rules shape this module:

1. **Nothing is printed directly.** Every message goes through [Console], which
   runs it past a redaction filter first. A leaked bot token or API key in a
   pasted terminal transcript is a real incident, and the only way to make that
   impossible is to have exactly one write path.
2. **`--json` is a first-class mode, not an afterthought.** Scripts and cron jobs
   consume this CLI, so any command's result can be emitted as one JSON object
   on stdout with nothing else mixed in.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------- ANSI colors

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("ATF_FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class Console:
    """Redaction-safe writer with human and JSON modes."""

    def __init__(
        self,
        *,
        json_mode: bool = False,
        color: bool | None = None,
        quiet: bool = False,
        verbose: bool = False,
        stream=None,
        err_stream=None,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.verbose = verbose
        self.out = stream or sys.stdout
        self.err = err_stream or sys.stderr
        if color is None:
            color = _supports_color(self.out)
        self.color = bool(color)
        # Values that must never appear in output. Registered by whoever learns
        # them (auth, env loading, admin settings read).
        self._secrets: set[str] = set()

    # ------------------------------------------------------------- redaction
    def register_secret(self, value: Any) -> None:
        """Remember a secret so it can never be printed by accident."""
        if value is None:
            return
        text = str(value).strip()
        # Short values produce false positives all over normal output ("1",
        # "abc"), so only strings long enough to actually be a credential are
        # filtered.
        if len(text) >= 8:
            self._secrets.add(text)

    def redact(self, text: str) -> str:
        if not text or not self._secrets:
            return text
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text

    # ------------------------------------------------------------------ style
    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(_CODES.get(s, "") for s in styles)
        return f"{prefix}{text}{_RESET}"

    def width(self) -> int:
        try:
            return max(40, min(200, shutil.get_terminal_size((100, 24)).columns))
        except Exception:
            return 100

    @property
    def is_tty(self) -> bool:
        return bool(getattr(self.out, "isatty", lambda: False)())

    def raw(self, text: str) -> None:
        """Write an ANSI control sequence (cursor move, clear) with no newline."""
        if self.json_mode or self.quiet:
            return
        print(text, end="", file=self.out)

    # ------------------------------------------------------------- primitives
    def _write(self, text: str, *, error: bool = False, raw: bool = False) -> None:
        # In JSON mode stdout carries exactly one JSON document, so human
        # chatter is suppressed rather than interleaved.
        if self.json_mode and not error:
            return
        if self.quiet and not error:
            return
        if not raw:
            text = self.redact(text)
        print(text, file=self.err if error else self.out)

    def line(self, text: str = "") -> None:
        self._write(text)

    def info(self, text: str) -> None:
        self._write(f"{self.paint('•', 'cyan')} {text}")

    def ok(self, text: str) -> None:
        self._write(f"{self.paint('✓', 'green')} {text}")

    def warn(self, text: str) -> None:
        self._write(f"{self.paint('!', 'yellow')} {text}", error=True)

    def fail(self, text: str) -> None:
        self._write(f"{self.paint('✗', 'red')} {text}", error=True)

    def step(self, text: str) -> None:
        self._write(self.paint(f"→ {text}", "bold"))

    def hint(self, text: str) -> None:
        if not text:
            return
        self._write(self.paint(f"  hint: {text}", "grey"), error=True)

    def debug(self, text: str) -> None:
        if self.verbose:
            self._write(self.paint(f"  {text}", "grey"))

    def title(self, text: str) -> None:
        self._write("")
        self._write(self.paint(text, "bold"))

    def rule(self) -> None:
        self._write(self.paint("─" * min(self.width(), 72), "grey"))

    # ------------------------------------------------------------------- data
    def json(self, payload: Any) -> None:
        """Emit the machine-readable result. Always printed, never colourised."""
        print(json.dumps(payload, indent=2, default=str, ensure_ascii=False), file=self.out)

    def emit(self, payload: Any, human: str | None = None) -> None:
        """Print JSON in JSON mode, otherwise the human rendering."""
        if self.json_mode:
            self.json(payload)
        elif human is not None:
            self._write(human)

    def kv(self, pairs: Iterable[tuple[str, Any]], *, indent: int = 0) -> None:
        pad = max((len(str(k)) for k, _ in pairs), default=0)
        prefix = " " * indent
        for key, value in pairs:
            self._write(f"{prefix}{self.paint(str(key).ljust(pad), 'grey')}  {value}")

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        empty: str = "nothing to show",
        max_width: int | None = None,
    ) -> None:
        if self.json_mode:
            return
        if not rows:
            self._write(self.paint(f"  {empty}", "grey"))
            return
        cells = [[("" if c is None else str(c)) for c in row] for row in rows]
        widths = [
            min(
                max([len(str(headers[i]))] + [len(r[i]) for r in cells]),
                max_width or 60,
            )
            for i in range(len(headers))
        ]

        def fmt(row: Sequence[str]) -> str:
            out = []
            for i, cell in enumerate(row):
                clipped = cell
                if len(clipped) > widths[i]:
                    clipped = clipped[: max(1, widths[i] - 1)] + "…"
                out.append(clipped.ljust(widths[i]))
            return "  ".join(out).rstrip()

        self._write("  " + self.paint(fmt(list(headers)), "bold"))
        for row in cells:
            self._write("  " + fmt(row))

    def banner(self, service: str, status: str, *, good: bool) -> None:
        mark = self.paint("✓", "green") if good else self.paint("✗", "red")
        self._write(f"  {mark} {service.ljust(14)} {status}")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def truncate(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"
