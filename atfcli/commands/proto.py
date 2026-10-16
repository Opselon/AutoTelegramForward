"""Protocol buffer compilation and generation."""

from __future__ import annotations

from . import _shared as sh

NAME = "proto"
HELP = "Compile protocol buffers across Python and Go services"


def _gen(ctx, args) -> int:
    ctx.out.step("Compiling protocol buffer definitions...")
    res = ctx.repo.run_streaming([ctx.repo.python(), "scripts/gen_proto.py"], check=False)
    if res != 0:
        ctx.out.fail("Protobuf compilation failed")
        return 1
    ctx.out.ok("Protobuf stubs generated successfully")
    return 0


def add_parser(subparsers, parents) -> None:
    parser = subparsers.add_parser(NAME, help=HELP, description=HELP, parents=parents)
    parser.set_defaults(func=_gen)
