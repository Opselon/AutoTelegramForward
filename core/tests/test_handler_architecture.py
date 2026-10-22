"""The handler-layer contract: one owner per command, no layering leaks.

These are architecture tests, not behaviour tests. They fail on the class of
change that produced the original problem — a surface registering handlers from
somewhere else, a command table entry with no handler behind it, or a handler
module reaching past the application layer into a repository.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.application.command_registry import ROUTED_TOKENS, resolve
from core.infrastructure.telegram.handlers import HANDLER_MODULES, build_handler_registry
from core.infrastructure.telegram.handlers.discovery import DiscoveryHandlers
from core.infrastructure.telegram.handlers.registry import (
    HandlerModuleSpec,
    HandlerRegistry,
    HandlerRegistryError,
)
from core.infrastructure.telegram.pro_bot_ui import ProBotUI

ROOT = Path(__file__).resolve().parents[2]
HANDLERS_DIR = ROOT / "core" / "infrastructure" / "telegram" / "handlers"


# --------------------------------------------------------------------------- #
# Registry: the mounting contract
# --------------------------------------------------------------------------- #
def test_registry_rejects_duplicate_module_names():
    registry = HandlerRegistry()
    registry.add(HandlerModuleSpec(cls=DiscoveryHandlers, name="same"))
    with pytest.raises(HandlerRegistryError):
        registry.add(HandlerModuleSpec(cls=DiscoveryHandlers, name="same"))


def test_registry_rejects_unknown_command():
    with pytest.raises(HandlerRegistryError):
        HandlerModuleSpec(cls=DiscoveryHandlers, name="x", commands=("no_such_command",))


def test_registry_rejects_builtin_command():
    """A dedicated handler owns /start; a module must not claim it too."""
    assert resolve("start") is not None and resolve("start").builtin
    with pytest.raises(HandlerRegistryError):
        HandlerModuleSpec(cls=DiscoveryHandlers, name="x", commands=("start",))


def test_registry_rejects_two_owners_for_one_command():
    registry = HandlerRegistry()
    registry.add(HandlerModuleSpec(cls=DiscoveryHandlers, name="a", commands=("getchannel",)))
    with pytest.raises(HandlerRegistryError):
        registry.add(HandlerModuleSpec(cls=DiscoveryHandlers, name="b", commands=("getchannel",)))


def test_registry_rejects_declared_command_without_handler():
    class Empty:
        pass

    registry = HandlerRegistry().add(
        HandlerModuleSpec(cls=Empty, name="empty", commands=("getchannel",))
    )
    with pytest.raises(HandlerRegistryError):
        registry.mount(object(), Empty())


def test_registry_rejects_module_not_mixed_into_ui():
    registry = HandlerRegistry().add(HandlerModuleSpec(cls=DiscoveryHandlers, name="d"))
    with pytest.raises(HandlerRegistryError):
        registry.mount(object(), object())


def test_registry_rejects_missing_registration_method():
    with pytest.raises(HandlerRegistryError):
        HandlerModuleSpec(cls=DiscoveryHandlers, name="x", register="register_nothing")


def test_registry_mounts_modules_in_order():
    calls: list[str] = []

    class First:
        def register_first(self, _bot):
            calls.append("first")

    class Second:
        def register_second(self, _bot):
            calls.append("second")

    class Ui(First, Second):
        pass

    registry = HandlerRegistry().extend([
        HandlerModuleSpec(cls=First, name="first", register="register_first"),
        HandlerModuleSpec(cls=Second, name="second", register="register_second"),
    ])
    registry.mount(object(), Ui())
    assert calls == ["first", "second"]


# --------------------------------------------------------------------------- #
# The panel's real inventory
# --------------------------------------------------------------------------- #
def test_every_module_command_has_one_handler_on_the_panel():
    for module in HANDLER_MODULES:
        for command in module.commands:
            handlers = [
                base for base in ProBotUI.__mro__
                if f"_cmd_{command}" in base.__dict__
            ]
            assert len(handlers) == 1, (
                f"/{command} is defined {len(handlers)} times in the panel's MRO: {handlers}"
            )


def test_discovery_module_declares_exactly_what_it_implements():
    declared = set()
    for module in HANDLER_MODULES:
        if module.cls is DiscoveryHandlers:
            declared.update(module.commands)
    assert declared == set(DiscoveryHandlers.discovery_commands)


def test_registry_builds_for_the_real_panel():
    registry = build_handler_registry()
    assert [m.name for m in registry.modules] == [
        "discovery", "forwarding", "forward_manage", "forward_select",
    ]
    assert registry.owner_of("getchannel") == "discovery"


def test_router_never_lists_a_command_without_a_handler():
    """Every routed token must resolve — aliases resolve to their real command."""
    missing = []
    for name in ROUTED_TOKENS:
        spec = resolve(name)
        target = spec.name if spec else name
        if not callable(getattr(ProBotUI, f"_cmd_{target}", None)):
            missing.append(name)
    assert not missing, f"routed commands with no handler: {missing}"


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_handler_modules_do_not_import_persistence():
    forbidden = ("persistence", "sqlite_repositories", "postgres_repositories", "database")
    offenders = {}
    for path in HANDLERS_DIR.glob("*.py"):
        leaked = [m for m in _imports(path) if any(f in m for f in forbidden)]
        if leaked:
            offenders[path.name] = leaked
    assert not offenders, f"handlers must go through use cases, not repositories: {offenders}"


def test_discovery_application_module_has_no_framework_imports():
    imports = _imports(ROOT / "core" / "application" / "discovery.py")
    assert not [m for m in imports if m.split(".")[0] in {"pyrogram", "telethon"}]
