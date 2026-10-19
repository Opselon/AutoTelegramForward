"""Compatibility alias: the host context now lives in
:mod:`atfcli.infrastructure.host.repo`.

``RepoContext`` is portfolio-wide vocabulary by now (commands, tests and the
deploy scripts talk about "the context"), so the name is re-exported rather than
renamed. The machine model itself — service and container names, ports, the
network aliases — moved to :mod:`atfcli.domain.host`, which is what the commands
should read.
"""

from __future__ import annotations

from .domain.host import (  # noqa: F401  (canonical home is the domain)
    AI_NETWORK,
    COMPOSE_NETWORK,
    CONTAINERS,
    NETWORK_ALIASES,
    SERVICES,
)
from .domain.secrets import SECRET_NAME_PATTERN, is_secret_name  # noqa: F401
from .infrastructure.host.repo import (  # noqa: F401  (legacy path)
    CommandResult,
    DockerInfo,
    RepoContext,
    collect_settings,
    find_repo_root,
    merge_dotted,
    parse_jsonish,
)

__all__ = [
    "AI_NETWORK",
    "COMPOSE_NETWORK",
    "CONTAINERS",
    "NETWORK_ALIASES",
    "SECRET_NAME_PATTERN",
    "SERVICES",
    "CommandResult",
    "DockerInfo",
    "RepoContext",
    "collect_settings",
    "find_repo_root",
    "is_secret_name",
    "merge_dotted",
    "parse_jsonish",
]
