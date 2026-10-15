"""Live system monitor and dashboard."""

from __future__ import annotations

import time

from . import _shared as sh
from .status import _board

NAME = "watch"
HELP = "Live streaming terminal dashboard"


def _watch(ctx, args) -> int:
    interval = args.interval or 2.0
    while True:
        if not ctx.out.json_mode and ctx.out.is_tty:
            ctx.out.raw("\033[H\033[J")
        _board(ctx, args)
        ctx.out.line()
        ctx.out.line(f"  Live monitor · {time.strftime('%H:%M:%S')} · press ctrl-c to exit")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_watch)
    parser.add_argument("-i", "--interval", type=float, default=2.0, help="refresh interval in seconds")
