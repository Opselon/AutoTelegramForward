"""No two images may share a BuildKit cache mount.

`docker compose up -d --build` builds services in parallel, and a cache mount with
no explicit `id=` is keyed by its *target path* alone — so `Dockerfile.web` and
`Dockerfile.logger-rust` were both mounting `/usr/local/cargo/registry` onto one
shared cache. Cargo is not safe against a concurrent unpack of the same crate:
one build unpacked `tracing`, the other saw a half-written directory with
`.cargo-ok` already present and died with `File exists (os error 17)`, exit 101.

That is what turned the v2.2.2 release's `clean-install` job red — its
`docker compose up -d --build` — which gates the whole release (`publish` only
runs when `clean-install` succeeds), so the release published nothing. It was
invisible in CI because `docker-build` builds one image per job: the race needs
two images in flight at once, which only compose does.

Two rules, both machine-checked here: every cache mount names its cache, and no
cache name is used by two different Dockerfiles. Sharing *within* one Dockerfile
is the point — a dependency-warming layer and the final build must hit the same
cache — so this checks across files, not across mounts.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(REPO.glob("Dockerfile*")) + sorted(REPO.glob("**/Dockerfile"))

MOUNT_RE = re.compile(r"--mount=type=cache,([^\s\\]*)")
CACHE_ID_RE = re.compile(r"(?:^|,)id=([^,\s]+)")
CACHE_TARGET_RE = re.compile(r"(?:^|,)target=([^,\s]+)")


def _dockerfiles() -> list[Path]:
    seen: dict[Path, None] = {}
    for path in DOCKERFILES:
        if path.is_file() and ".git" not in path.parts:
            seen[path.resolve()] = None
    return sorted(seen)


def _cache_mounts(path: Path) -> list[str]:
    return MOUNT_RE.findall(path.read_text(encoding="utf-8"))


def test_there_are_dockerfiles_to_check():
    assert {"Dockerfile.core", "Dockerfile.api", "Dockerfile.logger-rust",
            "Dockerfile.web"} <= {p.name for p in _dockerfiles()}


def test_every_cache_mount_names_its_cache():
    unnamed: list[str] = []
    for path in _dockerfiles():
        for options in _cache_mounts(path):
            if not CACHE_ID_RE.search(options):
                target = CACHE_TARGET_RE.search(options)
                unnamed.append(f"{path.name}:{target.group(1) if target else options}")

    assert not unnamed, (
        f"cache mounts without an explicit id= {unnamed}. An unnamed mount is "
        "keyed by its target path, so another image mounting the same path shares "
        "it — and parallel compose builds then race inside it."
    )


def test_no_cache_name_is_shared_by_two_dockerfiles():
    owners: dict[str, set[str]] = {}
    for path in _dockerfiles():
        for options in _cache_mounts(path):
            match = CACHE_ID_RE.search(options)
            if match:
                owners.setdefault(match.group(1), set()).add(path.name)

    shared = {name: sorted(files) for name, files in owners.items() if len(files) > 1}

    assert not shared, (
        f"cache id(s) {shared} are mounted by more than one image. BuildKit keys a "
        "cache by id alone, so those builds read and write one directory "
        "concurrently: pull the ids apart per image."
    )


def test_a_cargo_registry_cache_is_locked_against_concurrent_unpacks():
    """Cargo's own unpack is not concurrency-safe; `sharing=locked` serialises it.

    Belt and braces next to the unique ids above: even a mistake that reintroduces
    sharing must not corrupt the registry.
    """
    for path in _dockerfiles():
        for options in _cache_mounts(path):
            target = CACHE_TARGET_RE.search(options)
            if not target or not target.group(1).endswith("cargo/registry"):
                continue
            assert "sharing=locked" in options, (
                f"{path.name}: the cargo registry cache mount must be declared "
                "sharing=locked — a concurrent unpack of the same crate aborts the "
                "build with `File exists (os error 17)`"
            )
