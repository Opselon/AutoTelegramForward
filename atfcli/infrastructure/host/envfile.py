"""`.env` on disk: timestamps backups, atomic replace, verify-then-rollback.

Why every rule here is a separate knob
-------------------------------------
A configuration write is the one CLI operation whose failure mode is "the stack
no longer starts", so each step exists for a specific incident-shaped reason:

* **seeded temp file + `os.replace`** — a crash or a full disk mid-write must not
  leave half of a file behind. Renames are atomic on POSIX, so the file is either
  the old one or the new one, never torn.
* **fill-then-revision backup** — `atf config set` hand-copies the previous file
  to `.env.bak.<timestamp>` before touching it. A mistyped value is one
  `atf config rollback` away from gone. Backups are capped (`KEEP`) and trimmed
  oldest-first so this cannot become unbounded disk growth.
* **fsync before rename** — a rename to a block that hasn't actually been laid
  down can lose it during reboot, and the block is the config.
* **post-verify + automatic rollback** — after the rename, the file is read back
  and byte-compared. A filesystem that accepted the write 'successfully' and
  handed back something else (NFS, a broken overlay, an out-of-memory host) is
  not a plausible story worth keeping: the backup is restored *by the CLI* and
  the error says so.
* **0600** — the file carries bot tokens and signing secrets. The CLI writes it
  0600 even if it found it wider, so one repair cannot regress the permissions
  another repair fixed.

A symlink `.env` is resolved and its *target* replaced, so the symlink survives
(atomic replace of the symlink itself would be the opposite of what an operator
who symlinked it wanted).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ...domain import envschema
from ...domain.errors import CommandFailed, NotFoundError

#: How many timestamped backups survive a run of `atf config set` calls.
KEEP: int = 5

#: Backup name pattern: `.env.bak.<timestamp>`, e.g. `.env.bak.20261022T154501`.
SUFFIX = ".bak."


@dataclass(frozen=True)
class BackupEntry:
    """One rollback point a human can also `cp` by hand."""

    name: str
    path: Path
    size: int
    created_at: str
    keys: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "size": self.size,
            "created_at": self.created_at,
            "keys": self.keys,
        }


@dataclass(frozen=True)
class WriteResult:
    """What a successful write did, for the report the command prints."""

    path: Path
    backup: str = ""
    mode: int = 0
    keys: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "backup": self.backup,
            "mode": oct(self.mode),
            "keys": self.keys,
        }


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


class EnvFileStore:
    """The port implementation over one `.env` path — the only file I/O here."""

    def __init__(self, path: Path, *, keep: int = KEEP, clock=time.time) -> None:
        self._path = Path(path)
        self._keep = keep
        self._clock = clock

    # ------------------------------------------------------------------ paths
    @property
    def path(self) -> Path:
        """The real file behind the path: a symlink's target, not the link."""
        if self._path.is_symlink():
            return Path(os.path.realpath(self._path))
        return self._path

    def exists(self) -> bool:
        return self.path.exists()

    def mode(self) -> int:
        return self.path.stat().st_mode & 0o777 if self.path.exists() else 0

    # ------------------------------------------------------------------ locks
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Best-effort advisory lock, so two CLI runs cannot interleave writes.

        `fcntl` is POSIX-only; on a platform without it (Windows) the write is
        atomic-per-file anyway, so skipping the lock keeps the port portable.
        """
        try:
            import fcntl  # noqa: PLC0415 - POSIX-only optional dependency
        except ImportError:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(self.path) + ".lock"
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ---------------------------------------------------------------- reading
    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def text_or_empty(self) -> str:
        try:
            return self.read()
        except (OSError, UnicodeDecodeError):
            # A `.env` that cannot be read as UTF-8 is still writable: the
            # document type would mangle its undecodably byte, so the store
            # refuses to rewrite it rather than silently truncating a file the
            # stack also reads.
            raise CommandFailed(
                f"{self.path} is not readable as UTF-8 — fix or replace it by hand",
                hint="back up the file, then run `atf config set` again",
            ) from None

    def document(self) -> envschema.EnvDocument:
        return envschema.EnvDocument.parse(self.text_or_empty())

    # ---------------------------------------------------------------- backups
    def backups(self) -> list[BackupEntry]:
        parent = self.path.parent
        prefix = self.path.name + SUFFIX
        rows: list[BackupEntry] = []
        try:
            candidates = sorted(parent.glob(prefix + "*"))
        except OSError:
            candidates = []
        for path in candidates:
            name = path.name[len(prefix) :]
            try:
                stat = path.stat()
            except OSError:
                continue
            keys = 0
            try:
                keys = len(envschema.EnvDocument.parse(path.read_text(encoding="utf-8")).values())
            except (OSError, UnicodeDecodeError):
                pass
            stamp = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            rows.append(BackupEntry(name=name, path=path, size=stat.st_size, created_at=stamp, keys=keys))
        rows.sort(key=lambda row: row.name, reverse=True)
        return rows

    def backup(self) -> str:
        """Copy the current file to `.env.bak.<stamp>`, trim to `KEEP`, return the name."""
        if not self.exists():
            return ""
        stamp = _timestamp()
        target = self.path.with_name(self.path.name + SUFFIX + stamp)
        seq = 0
        while target.exists():
            seq += 1
            target = self.path.with_name(f"{self.path.name}{SUFFIX}{stamp}-{seq}")
        shutil.copyfile(self.path, target)
        target.chmod(0o600)
        self._trim()
        # The name actually used (never re-derive: a second-boundary tick or an
        # `-N` collision suffix would make the caller look for the wrong file).
        return target.name[len(self.path.name) + len(SUFFIX) :]

    def _trim(self) -> None:
        entries = self.backups()
        for stale in entries[self._keep :]:
            try:
                stale.path.unlink()
            except OSError:
                pass

    def backup_names(self) -> list[str]:
        return [entry.name for entry in self.backups()]

    # ---------------------------------------------------------------- writing
    def write(self, text: str) -> WriteResult:
        """Back up, atomically replace, verify, and roll back on mismatch."""
        with self._locked():
            return self._write_locked(text)

    def _write_locked(self, text: str) -> WriteResult:
        """The write itself. The caller owns the lock — `restore` nests, and a
        second `flock()` on the same file from the same process deadlocks."""
        backup = self.backup()
        real = self.path
        real.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(real.parent),
            prefix="." + real.name + ".tmp.",
            delete=False,
            suffix="",
        )
        tmp = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            tmp.chmod(0o600)
            os.replace(tmp, real)
        except BaseException:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise
        self._fsync_dir(real.parent)
        if self.read() != text:
            self._rollback(backup)
            raise CommandFailed(
                f"{real} did not hold the written bytes after the replace",
                hint="the previous file was restored automatically; retry once, then check the disk",
            )
        keys = len(envschema.EnvDocument.parse(text).values())
        return WriteResult(path=real, backup=backup, mode=self.mode(), keys=keys)

    def _fsync_dir(self, directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _rollback(self, backup: str) -> None:
        if not backup:
            return
        source = self.path.with_name(self.path.name + SUFFIX + backup)
        if not source.exists():
            return
        shutil.copyfile(source, self.path)
        self.path.chmod(0o600)

    # ---------------------------------------------------------------- restore
    def restore(self, name: str) -> WriteResult:
        """Restore a named backup, seeding a backup of what is there now."""
        # The name is a suffix supplied by the operator, so it is reduced to a
        # bare timestamp token before it ever touches the path: a `../` or `/`
        # in it must not become a traversal.
        wanted = str(name or "").strip().strip("/")
        if not wanted or "/" in wanted or "\\" in wanted or ".." in wanted:
            raise NotFoundError(
                f"no backup named {name!r}",
                hint="`atf config backups` lists the rollback points",
            )
        target = self.path.with_name(self.path.name + SUFFIX + wanted)
        if not target.exists():
            raise NotFoundError(
                f"no backup named {name!r}",
                hint="`atf config backups` lists the rollback points",
            )
        with self._locked():
            # `self.write` would take the lock again (deadlock); the unlocked
            # form shares the backup/verify/rollback machinery.
            return self._write_locked(target.read_text(encoding="utf-8"))


class RepoEnvFileStore(EnvFileStore):
    """The real store, bound to a :class:`RepoContext`.

    The context caches the parsed `.env` for the fast `env()`/`env_file_values`
    reads; a store that wrote through the context must drop that cache, or the
    rest of the invocation would keep answering with the pre-write values (the
    bug that made `atf config set K=V && atf config get K` inside one process
    show the old value).
    """

    def __init__(self, repo, *, keep: int = KEEP, clock=time.time) -> None:
        super().__init__(repo.path(".env"), keep=keep, clock=clock)
        self._repo = repo

    def write(self, text: str) -> WriteResult:
        result = super().write(text)
        self._repo.refresh_env()
        return result


__all__ = [
    "BackupEntry",
    "KEEP",
    "RepoEnvFileStore",
    "SUFFIX",
    "WriteResult",
    "EnvFileStore",
]
