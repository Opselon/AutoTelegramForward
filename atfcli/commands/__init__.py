"""Per-task management: lifecycle, settings, sources, simulation.

Each module exposes:

    NAME        — the group name shown in `atf --help`
    HELP        — one line describing the group
    add_parser(subparsers, parents) — register the group and its verbs

Every verb parser sets `func=`, which the dispatcher calls as `func(ctx, args)`.
"""
