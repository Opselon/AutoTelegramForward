"""AutoTelegramForward control CLI.

Importing this package must stay cheap: `atfcli.cli` pulls command modules
lazily so `atf --help` does not pay for the whole surface.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__", "main"]


def main(argv: list[str] | None = None) -> None:
    from .cli import main as _main

    _main(argv)
