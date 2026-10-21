"""Handler modules for the Telegram control panel.

Each module owns one surface (discovery, forwarding, autoreply, replace, ...)
and is mixed into :class:`ProBotUI`, which supplies the shared render helpers.
The table below is the single inventory the registry validates at startup and
the architecture test reads: adding a handler module means adding one row here.
"""

from __future__ import annotations

from .discovery import DiscoveryHandlers
from .forwarding import ForwardingHandlers
from .registry import (
    HandlerModuleSpec,
    HandlerRegistry,
    HandlerRegistryError,
    build_registry,
)

#: Every mounted module, in mount order.
HANDLER_MODULES = (
    HandlerModuleSpec(
        cls=DiscoveryHandlers,
        name="discovery",
        commands=("getchannel", "getgroup", "getuser", "getbot"),
    ),
    HandlerModuleSpec(
        cls=ForwardingHandlers,
        name="forwarding",
        register="register_forwarding_handlers",
    ),
)


def build_handler_registry() -> HandlerRegistry:
    """A validated registry for the panel's modules."""
    return build_registry(HANDLER_MODULES)


__all__ = [
    "DiscoveryHandlers",
    "ForwardingHandlers",
    "HANDLER_MODULES",
    "HandlerModuleSpec",
    "HandlerRegistry",
    "HandlerRegistryError",
    "build_handler_registry",
    "build_registry",
]
