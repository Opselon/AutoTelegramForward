"""Handler modules for the Telegram control panel.

Each module owns one surface (discovery, forwarding, autoreply, replace, ...)
and is mixed into :class:`ProBotUI`, which supplies the shared render helpers.
The table below is the single inventory the registry validates at startup and
the architecture test reads: adding a handler module means adding one row here.
"""

from __future__ import annotations

from .discovery import DiscoveryHandlers
from .forward_manage import ForwardManageHandlers
from .forward_select import ForwardSelectHandlers
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
        register="register_discovery_handlers",
    ),
    HandlerModuleSpec(
        cls=ForwardingHandlers,
        name="forwarding",
        register="register_forwarding_handlers",
    ),
    # Task management is a separate module on purpose: `/forward add` and the
    # status console kept growing, and list/edit/delete/bulk answer from their
    # own file with their own tests.
    HandlerModuleSpec(
        cls=ForwardManageHandlers,
        name="forward_manage",
        register="register_forward_manage_handlers",
    ),
    # The multi-select route picker: one screen family that owns sources,
    # targets, direct ids, validation and the pre-create plan.
    HandlerModuleSpec(
        cls=ForwardSelectHandlers,
        name="forward_select",
        register="register_forward_select_handlers",
    ),
)


def build_handler_registry() -> HandlerRegistry:
    """A validated registry for the panel's modules."""
    return build_registry(HANDLER_MODULES)


__all__ = [
    "DiscoveryHandlers",
    "ForwardSelectHandlers",
    "ForwardManageHandlers",
    "ForwardingHandlers",
    "HANDLER_MODULES",
    "HandlerModuleSpec",
    "HandlerRegistry",
    "HandlerRegistryError",
    "build_handler_registry",
    "build_registry",
]
