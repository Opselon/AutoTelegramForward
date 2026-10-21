"""The handler registry: what is mounted, in which order, and only once.

The control panel grew to a dozen-thousand-line module because every surface
registered its own pyrogram handlers from one method, with no record of what
owned which command. This registry is the single answer to "who handles this?".

A module declares:

* ``name`` — its own identity, for errors and the architecture test;
* ``commands`` — the slash commands it serves, which must exist in the command
  table and must not be a ``builtin`` (those are owned by dedicated handlers);
* ``register`` — the method on the UI that mounts its callback handlers, if any.

``mount()`` then checks the promise against the object actually being mounted:
the mixin must be present, every declared command must resolve to a
``_cmd_<name>`` method, and no command may be claimed twice. A missing or
duplicated handler becomes a startup error instead of a silent dead button.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple, Type

from core.application.command_registry import resolve

logger = logging.getLogger(__name__)


class HandlerRegistryError(RuntimeError):
    """A mounting contract was broken; startup must not continue silently."""


@dataclass(frozen=True)
class HandlerModuleSpec:
    """One handler module and the surface it promises to serve."""

    cls: Type[Any]
    name: str
    commands: Tuple[str, ...] = ()
    register: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise HandlerRegistryError("handler module needs a name")
        if self.register and not callable(getattr(self.cls, self.register, None)):
            raise HandlerRegistryError(
                f"handler module {self.name!r} declares register={self.register!r} "
                f"but {self.cls.__name__} has no such method"
            )
        # Validate the command claim at construction: a typo or a command another
        # handler already owns must fail where it is written, not on mount.
        for command in self.commands:
            spec = resolve(command)
            if spec is None:
                raise HandlerRegistryError(
                    f"module {self.name!r} declares unknown command /{command}"
                )
            if spec.builtin:
                raise HandlerRegistryError(
                    f"module {self.name!r} claims builtin command /{command}, "
                    "which a dedicated handler already owns"
                )


@dataclass
class HandlerRegistry:
    """An ordered set of handler modules, validated before anything is mounted."""

    modules: List[HandlerModuleSpec] = field(default_factory=list)
    _commands: Dict[str, str] = field(default_factory=dict, init=False)
    _names: set = field(default_factory=set, init=False)

    def add(self, module: HandlerModuleSpec) -> "HandlerRegistry":
        if module.name in self._names:
            raise HandlerRegistryError(f"duplicate handler module {module.name!r}")
        for command in module.commands:
            spec = resolve(command)
            if spec is None:
                raise HandlerRegistryError(
                    f"module {module.name!r} declares unknown command /{command}"
                )
            if spec.builtin:
                raise HandlerRegistryError(
                    f"module {module.name!r} claims builtin command /{command}, "
                    "which a dedicated handler already owns"
                )
            owner = self._commands.get(command)
            if owner is not None:
                raise HandlerRegistryError(
                    f"command /{command} is claimed by both {owner!r} and {module.name!r}"
                )
            self._commands[command] = module.name
        self._names.add(module.name)
        self.modules.append(module)
        return self

    def extend(self, modules: Iterable[HandlerModuleSpec]) -> "HandlerRegistry":
        for module in modules:
            self.add(module)
        return self

    def owner_of(self, command: str) -> str:
        return self._commands.get(command, "")

    def mount(self, bot: Any, ui: Any) -> None:
        """Mount every module on ``ui``, verifying the contract as it goes."""
        for module in self.modules:
            if not isinstance(ui, module.cls):
                raise HandlerRegistryError(
                    f"module {module.name!r} is not mixed into {type(ui).__name__}"
                )
            for command in module.commands:
                if not callable(getattr(ui, f"_cmd_{command}", None)):
                    raise HandlerRegistryError(
                        f"module {module.name!r} declares /{command} without a handler"
                    )
            if module.register:
                getattr(ui, module.register)(bot)
            logger.debug("[PIPELINE:HANDLERS] mounted module=%s", module.name)


def build_registry(modules: Sequence[HandlerModuleSpec]) -> HandlerRegistry:
    return HandlerRegistry().extend(modules)
