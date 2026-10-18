"""The backup scope is a decision, so it is logged as one.

`owner_user_id = 0` is documented as "everything" - a whole-installation
migration - and it is also what a caller that never resolved an owner sends.
The API refuses an account-less caller before it reaches the core, so the two
cannot be confused on the wire; this suite pins the core's half of the
contract: the scope it actually used, and the line it leaves behind for each
one. A silent unscoped export of every tenant's live credentials is a defect
even when the caller was authorised.
"""

import asyncio
import logging

import pytest

from core.infrastructure.grpc_server.servicers import BackupControlServicer
from core.application.backup_service import serialize_bundle

OWNER = 5989354154
OTHER = 5094837833


class FakeBackup:
    """Records the scope the servicer asked for; returns a tiny snapshot."""

    def __init__(self) -> None:
        self.exported_owner = "unset"
        self.imported_target = "unset"
        self.restored_doc = None

    async def create_snapshot(self, owner_user_id=None):
        self.exported_owner = owner_user_id
        return {"format": 1, "counts": {"rules": 0}, "entities": {}}

    async def restore_snapshot(self, doc, target_owner_user_id=None, *, overwrite_existing=False):
        self.restored_doc = doc
        self.imported_target = target_owner_user_id
        return {"rules": {"imported": 0, "skipped": 0, "failed": 0}}


class ExportRequest:
    def __init__(self, owner_user_id: int, passphrase: str = "") -> None:
        self.owner_user_id = owner_user_id
        self.passphrase = passphrase


class ImportRequest:
    def __init__(self, target_owner_user_id: int, bundle: str = "") -> None:
        self.target_owner_user_id = target_owner_user_id
        # A real bundle: the servicer decrypts/deserializes before it ever looks
        # at the target, so the scope under test must be reached through a
        # payload the parser accepts.
        self.bundle = bundle or serialize_bundle({"format": 1, "entities": {}, "counts": {}})
        self.passphrase = ""
        self.overwrite_existing = False


def _export(owner_user_id: int):
    svc = FakeBackup()
    servicer = BackupControlServicer(svc)

    async def run():
        # The bundle is produced by the fake snapshot, so a passphrase would
        # fail to encrypt a plaintext bundle; the scope is what is under test.
        return await servicer.BackupExport(ExportRequest(owner_user_id), None)

    return svc, asyncio.run(run())


def test_an_owner_is_exported_as_that_owner(caplog):
    with caplog.at_level(logging.INFO, logger="core"):
        svc, resp = _export(OWNER)

    assert resp.success is True
    assert svc.exported_owner == OWNER, "a named owner must be scoped to that tenant"
    assert any(
        "[PIPELINE:backup] export scope owner=%d" % OWNER in r.getMessage()
        for r in caplog.records
    ), "the scope that was used must be on the record"
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_owner_zero_is_an_unscoped_export_and_says_so(caplog):
    """The documented single-box contract, made loud."""

    with caplog.at_level(logging.INFO, logger="core"):
        svc, resp = _export(0)

    assert resp.success is True
    assert svc.exported_owner is None, "0 must still mean the whole installation"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "an installation-wide credential export must not be silent"
    assert "ALL_TENANTS" in warnings[0].getMessage()


def test_a_synthetic_negative_owner_is_a_real_tenant(caplog):
    """An unlinked web account is a tenant with a negative id - not a wildcard."""

    with caplog.at_level(logging.INFO, logger="core"):
        svc, resp = _export(-2)

    assert resp.success is True
    assert svc.exported_owner == -2, "a negative owner id is an owner, not 'everything'"
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_an_import_target_is_the_tenant_it_lands_in(caplog):
    with caplog.at_level(logging.INFO, logger="core"):
        svc = FakeBackup()
        resp = asyncio.run(
            BackupControlServicer(svc).BackupImport(ImportRequest(OTHER), None)
        )

    assert resp.success is True
    assert svc.imported_target == OTHER
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_an_import_without_a_target_keeps_the_bundle_owner_ids(caplog):
    with caplog.at_level(logging.INFO, logger="core"):
        svc = FakeBackup()
        resp = asyncio.run(
            BackupControlServicer(svc).BackupImport(ImportRequest(0), None)
        )

    assert resp.success is True
    assert svc.imported_target is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "FROM_BUNDLE" in warnings[0].getMessage(), (
        "an owner-0 import lets the payload decide where records land: log it"
    )


def test_a_failing_export_still_reports_itself(caplog):
    """The scope logging must not swallow the pre-existing error path."""

    class Broken(FakeBackup):
        async def create_snapshot(self, owner_user_id=None):
            raise RuntimeError("disk went away")

    with caplog.at_level(logging.INFO, logger="core"):
        resp = asyncio.run(
            BackupControlServicer(Broken()).BackupExport(ExportRequest(OWNER), None)
        )

    assert resp.success is False
    assert "disk went away" in resp.error


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
