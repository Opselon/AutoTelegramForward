"""Backup and restore of the whole tenant's state.

Restore is the highest-stakes command in the CLI, so the checks happen here — in
local code that a test can drive — rather than in the command body: the bundle
must exist, overwriting live state requires an explicit flag, and a failure the
server reports inside a 200 is turned into a real error instead of a green line.

The export side writes through the :class:`~atfcli.application.gateways.FileWriter`
port, so `--output` is honoured by the same code the tests exercise with a fake
writer (and `--dry-run` can be added in one place later).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ...domain.errors import UsageError
from ..gateways import FileWriter
from . import _payload


class Backups:
    """Use cases over export/restore."""

    def __init__(self, gateway, files: FileWriter | None = None) -> None:
        self.gateway = gateway
        self.files = files

    def export(self, *, passphrase: str = "", output: str = "") -> _payload.Outcome:
        document = _payload.mapping(self.gateway.export(passphrase=passphrase))
        bundle = str(document.get("bundle") or "")
        warnings: list[_payload.Warning_] = []
        if not bundle:
            raise UsageError(
                str(document.get("error") or "the server returned an empty bundle"),
                hint="the export is written by atf-core; check `atf doctor` and the core logs",
            )
        detail = "export ready"
        if output:
            text = bundle if bundle.endswith("\n") else bundle + "\n"
            try:
                if self.files is not None:
                    self.files.write_text(output, text)
                else:  # no writer injected (library use): write it directly
                    Path(output).expanduser().write_text(text, encoding="utf-8")
            except OSError as exc:
                raise UsageError(
                    f"cannot write {output}: {exc}",
                    hint="pick a writable path, or omit --output to print the bundle",
                ) from exc
            detail = f"export written to {output} ({len(text)} bytes)"
        if not passphrase:
            warnings.append(
                _payload.Warning_(
                    "the bundle is not passphrase-protected",
                    hint=(
                        "it carries this tenant's sessions and API keys — treat it like a "
                        "credential, or re-export with --passphrase"
                    ),
                )
            )
        outcome = _payload.Outcome(document, detail, warnings=warnings)
        return outcome

    def summarise(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Counts the export reports, for the human summary line."""
        counts = document.get("counts")
        if isinstance(counts, Mapping):
            return dict(counts)
        try:
            parsed = json.loads(str(document.get("bundle") or "{}"))
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, Mapping):
            counts = parsed.get("counts") or parsed.get("summary")
            if isinstance(counts, Mapping):
                return dict(counts)
        return {}

    def inspect(self, bundle: str) -> _payload.Outcome:
        """Describe a bundle locally, before anything is written.

        Restoring is irreversible from the CLI's side, so the operator can look
        first: what version wrote it, which tenant it belongs to, and how many of
        each record it carries.
        """
        path = _bundle_path(bundle)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read {path}: {exc}") from exc
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise UsageError(
                f"{path} is not a JSON bundle: {exc}",
                hint="the file must be what `atf data backup-export` wrote",
            ) from exc
        if not isinstance(document, Mapping):
            raise UsageError(f"{path} does not contain a bundle object")
        counts = self.summarise({"bundle": text})
        detail = f"{path} ({len(text)} bytes)"
        if document.get("encrypted") or document.get("passphrase") or document.get("ciphertext"):
            detail += " — encrypted: --passphrase is required to restore it"
        return _payload.Outcome({"bundle_file": str(path), "counts": counts, **dict(document)}, detail)

    def restore(
        self,
        bundle: str,
        *,
        passphrase: str = "",
        overwrite: bool = False,
        confirm: bool = False,
    ) -> _payload.Outcome:
        path = _bundle_path(bundle)
        if not overwrite and not confirm:
            raise UsageError(
                "restoring replaces the tenant's current state",
                hint="pass --yes to confirm, or --overwrite to replace colliding tasks",
            )
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read {path}: {exc}") from exc
        document = _payload.mapping(
            self.gateway.restore(text, passphrase=passphrase, overwrite=overwrite)
        )
        if document.get("success") is False:
            raise UsageError(
                str(document.get("message") or document.get("error") or "the server refused the restore"),
                hint=_hint_for(document),
            )
        restored = document.get("restored") or document.get("imported")
        detail = "restore complete"
        if restored is not None:
            detail = f"restored {restored} record(s)"
        return _payload.Outcome(document, detail)


def _bundle_path(bundle: str) -> Path:
    """Resolve and check the bundle path once, for every verb that needs it."""
    if not bundle:
        raise UsageError(
            "a bundle path is required",
            hint="pass the file written by `atf data backup-export`",
        )
    path = Path(bundle).expanduser()
    if not path.exists():
        raise UsageError(f"no bundle at {path}")
    if not path.is_file():
        raise UsageError(f"{path} is not a file")
    return path


def _hint_for(document: Mapping[str, Any]) -> str:
    message = str(document.get("message") or document.get("error") or "").lower()
    if "passphrase" in message or "password" in message or "decrypt" in message:
        return "the bundle is encrypted — pass --passphrase (or set ATF_BACKUP_PASSPHRASE)"
    if "version" in message:
        return "the bundle came from a different version; re-export it from the source deployment"
    if "bundle" in message:
        return "check the file is the JSON produced by `atf backup export`"
    return "check the core logs for the import failure"
