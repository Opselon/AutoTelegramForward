"""The layering is enforced, not documented.

`atfcli` is a four-layer package:

    domain          pure rules and models — no I/O, no argparse, no HTTP
    application     use cases + ports (Protocols) — may import domain
    infrastructure  adapters over the API and the host — may import application
    commands        argparse presentation — may import application

An architecture that only lives in a docstring rots the first time someone needs
"just one" import back the other way. These tests read the source instead, so a
violation fails CI on the commit that introduces it rather than the week it
becomes load-bearing.

The user-facing behaviour tests (parsing, dispatch, live API calls) live in
`test_atfcli.py`; this file is only about dependency direction and the contracts
the generic surfaces depend on.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import atfcli

PKG = Path(atfcli.__file__).parent
LAYERS = ("domain", "application", "infrastructure", "commands")

#: Import targets each layer is forbidden to reach for, by layer.
FORBIDDEN = {
    "domain": (
        "atfcli.infrastructure",
        "atfcli.commands",
        "atfcli.application",
        "requests",
        "urllib.request",
        "socket",
        "subprocess",
    ),
    "application": (
        "atfcli.infrastructure",
        "atfcli.commands",
        "argparse",
        "requests",
        "subprocess",
    ),
    "infrastructure": ("atfcli.commands",),
}


def _python_files(layer: str) -> list[Path]:
    root = PKG / layer
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by a file, with relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.relative_to(PKG.parent).with_suffix("").parts[:-1])
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                # `from . import x` / `from ..domain import y` -> the module the
                # statement actually resolves to.
                prefix = ".".join(base + ([node.module] if node.module else []))
                found.add(prefix)
                found.update(f"{prefix}.{alias.name}" for alias in node.names)
            elif node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layer_never_imports_what_it_must_not(layer: str) -> None:
    forbidden = FORBIDDEN[layer]
    offenders: list[str] = []
    for path in _python_files(layer):
        for module in _imported_modules(path):
            for banned in forbidden:
                if module == banned or module.startswith(banned + "."):
                    offenders.append(f"{path.name} -> {module}")
    assert not offenders, f"{layer} layer reached outside its ring: {offenders}"


def test_argparse_is_confined_to_the_single_adapter_module() -> None:
    """The domain states rules; exactly one module translates them for argparse.

    `domain/validators.py` owns the one `argparse.ArgumentTypeError` in the layer
    (see `argparse_type`). Any *other* domain module importing argparse means a
    rule has been written for the command line first and the use case second,
    which is how the two paths drift.
    """
    offenders = [
        path.name
        for path in _python_files("domain")
        if path.name != "validators.py" and "argparse" in _imported_modules(path)
    ]
    assert not offenders, f"argparse leaked outside validators.py: {offenders}"


def test_domain_modules_are_importable_without_side_effects() -> None:
    """Importing the domain must not touch the disk, the network or the cli."""
    for module in pkgutil.iter_modules([str(PKG / "domain")]):
        importlib.import_module(f"atfcli.domain.{module.name}")


def test_application_modules_import_cleanly() -> None:
    importlib.import_module("atfcli.application.container")
    importlib.import_module("atfcli.application.use_cases")


def test_every_use_case_is_wired_in_the_composition_root() -> None:
    """A use case that is not in `UseCases` is dead code and a broken promise."""
    from atfcli.application import container

    wired = {field for field in container.UseCases.__dataclass_fields__}
    assert {"tasks", "sessions", "logs", "delivery", "backup", "admin", "audit", "ops"} <= wired
    assert {"ai", "prompts", "filters"} <= wired


def test_resource_specs_have_one_flag_per_editable_field() -> None:
    """The flag a human types and the key the API reads must come from one place."""
    from atfcli.domain.resources import CATALOGUE

    for group, spec in CATALOGUE.items():
        assert spec.path.startswith("/api/"), f"{group}: {spec.path} is not an API path"
        flags = [spec.flag_of(field) for field in spec.editable]
        assert len(flags) == len(set(flags)), f"{group}: two editable fields share a flag"
        for field, flag in zip(spec.editable, flags):
            assert flag.islower() and " " not in flag, f"{group}: {field} -> {flag!r} is not a CLI flag"
            assert spec.flag_map()[flag.replace("-", "_")] == field, f"{group}: {field} does not round-trip"


def test_resource_spec_flag_map_matches_the_wire_names() -> None:
    """AI configs speak camelCase on the wire; the CLI must still spell them out."""
    from atfcli.domain.resources import AI_CONFIGS

    assert AI_CONFIGS.flag_of("apiKey") == "api-key"
    assert AI_CONFIGS.flag_map()["api_key"] == "apiKey"
    assert AI_CONFIGS.flag_of("system_prompt") == "system-prompt"


def test_validators_are_shared_between_prompting_and_argparse() -> None:
    """One rule, three consumers: the wizard, `type=` and the use case.

    Every validator answers with a problem string (what `ui.ask(validate=...)`
    wants); `argparse_type` is the single adapter that turns that same string into
    argparse's exception. The test asserts they agree, so a rule cannot be
    phrased differently on different paths.
    """
    from atfcli.domain import validators

    kinds = ["chat", "telegram_id", "level", "service", "port", "severity"]
    for kind in kinds:
        broken = "!!not valid!!"
        problem = validators.check(kind, broken)
        assert problem, f"{kind}: {broken!r} was accepted by validate()"
        converter = validators.argparse_type(kind)
        with pytest.raises(Exception) as caught:
            converter(broken)
        assert str(caught.value) == problem, f"{kind}: argparse says {caught.value!r}, validate() says {problem!r}"

    # A flag whose default is "": argparse runs `type=` over the default too, so
    # the optional adapter must let the omitted case through while the strict one
    # still refuses it. Both come from the same validator.
    strict = validators.argparse_type("service")
    lax = validators.argparse_type("service", optional=True)
    assert lax("") == ""
    with pytest.raises(Exception):
        strict("")


def test_command_registry_and_modules_stay_in_step() -> None:
    """Every module named in the registry exists, and each declares NAME/HELP."""
    import atfcli.cli as cli

    for name in cli.COMMAND_MODULES:
        module = importlib.import_module(f"atfcli.commands.{name}")
        assert getattr(module, "NAME", name) == name, f"{name}: NAME mismatch"
        assert module.HELP, f"{name}: no HELP line"
        assert callable(module.add_parser), f"{name}: no add_parser"


def test_new_feature_groups_are_reachable_from_the_parser() -> None:
    """The AI, prompts, filters and audit surfaces are registered, not just present."""
    import atfcli.cli as cli

    parser = cli.build_parser()
    parser = parser[0] if isinstance(parser, tuple) else parser
    choices = parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
    for group in ("ai", "prompts", "filters", "audit", "rules", "logs", "data", "admin", "status"):
        assert group in choices, f"`atf {group}` is not registered"
