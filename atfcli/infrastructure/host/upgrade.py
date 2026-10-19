"""Upgrading the stack: know what you run, move forward, and get back.

The CLI is the thing that upgrades the system, so the upgrade has to survive
both halves of that sentence:

* **Know what you run.** Every revision is described twice — the git commit
  (`a1b2c3d`) and the product version (`VERSION`) — because a checkout can sit
  between two releases and a `.pyz` install has no git history at all. When git
  is missing the module degrades to "version-only" instead of failing: a
  portable install must still be able to report its version and rebuild.
* **Leave a way back.** Before the first mutation of an upgrade the previous
  revision is written to `.atf/upgrade-state.json`, so `atf upgrade rollback`
  works even if the new code cannot start. The state file is written by the
  *old* code on purpose — new code that fails to import cannot be trusted to
  record anything.

Order of operations in `apply` matters and is deliberate: check, back up, fetch,
move, build, recreate, verify. The backup happens before anything changes, and
verification happens after the containers are up, so a failure is reported with
the reason rather than a half-upgraded stack and a shrug.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from ...domain.host import SERVICES
from .repo import CommandResult
from ...domain.errors import AtfError

#: Where the pre-upgrade revision and the run history live. In-repo so a
#: rollback works from any shell, listed in .gitignore so it is never committed.
STATE_DIR = ".atf"
STATE_FILE = "upgrade-state.json"
STASH_MESSAGE = "atf upgrade autostash"

#: Cap on the changelog we carry around: enough to see what is coming, not so
#: much that `--json` becomes unreadable.
CHANGELOG_LIMIT = 40


# --------------------------------------------------------------------- revisions


@dataclass(frozen=True)
class Revision:
    """One point in the history: a commit, a version, or both."""

    sha: str = ""
    short: str = ""
    version: str = ""
    branch: str = ""
    subject: str = ""
    date: str = ""

    @property
    def known(self) -> bool:
        return bool(self.sha or self.version)

    @property
    def label(self) -> str:
        if self.version and self.short:
            return f"v{self.version} ({self.short})"
        if self.version:
            return f"v{self.version}"
        if self.short:
            return self.short
        return "unknown"

    def to_json(self) -> dict[str, str]:
        return {
            "sha": self.sha,
            "short": self.short,
            "version": self.version,
            "branch": self.branch,
            "subject": self.subject,
            "date": self.date,
            "label": self.label,
        }


@dataclass
class UpgradeCheck:
    """What `atf upgrade check` learned, in one place."""

    current: Revision
    target: Revision
    branch: str = ""
    upstream: str = ""
    remote: str = ""
    behind: int = 0
    ahead: int = 0
    commits: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fetched: bool = False

    @property
    def up_to_date(self) -> bool:
        return not self.blockers and self.behind == 0 and not self.newer_version

    @property
    def newer_version(self) -> bool:
        return version_is_newer(self.target.version, self.current.version)

    @property
    def can_apply(self) -> bool:
        return not self.blockers

    def to_json(self) -> dict[str, Any]:
        return {
            "current": self.current.to_json(),
            "target": self.target.to_json(),
            "branch": self.branch,
            "upstream": self.upstream,
            "remote": self.remote,
            "behind": self.behind,
            "ahead": self.ahead,
            "up_to_date": self.up_to_date,
            "newer_version": self.newer_version,
            "commits": list(self.commits),
            "dirty": list(self.dirty),
            "blockers": list(self.blockers),
            "notes": list(self.notes),
            "fetched": self.fetched,
        }


@dataclass
class Step:
    """One phase of a run, named so output and JSON agree."""

    name: str
    status: str = "pending"  # ok | skip | fail | pending
    detail: str = ""

    def to_json(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class UpgradeRun:
    """The record of one upgrade (or rollback) attempt."""

    verb: str = "upgrade"
    from_: Revision = field(default_factory=Revision)
    to: Revision = field(default_factory=Revision)
    steps: list[Step] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    duration_s: float = 0.0

    def add(self, name: str, status: str, detail: str = "") -> Step:
        step = Step(name=name, status=status, detail=detail)
        self.steps.append(step)
        return step

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if s.status == "fail"]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_json(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "ok": self.ok,
            "from": self.from_.to_json(),
            "to": self.to.to_json(),
            "steps": [s.to_json() for s in self.steps],
            "findings": list(self.findings),
            "duration_s": round(self.duration_s, 2),
        }


# ------------------------------------------------------------------- version math

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def semver_tuple(text: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.match((text or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_is_newer(candidate: str, current: str) -> bool:
    """Is `candidate` a later release than `current`?

    Answered conservatively: two versions we cannot parse are *not* ordered, so
    the answer is False and the caller falls back to comparing commits. A wrong
    "yes" would make `--check` promise an upgrade that does not exist.
    """
    new, old = semver_tuple(candidate), semver_tuple(current)
    if new is None or old is None:
        return False
    return new > old


# ------------------------------------------------------------------------ git


def git_available(ctx) -> bool:
    res = _git(ctx, "--version", read_only=True)
    return res.ok


def is_checkout(ctx) -> bool:
    res = _git(ctx, "rev-parse", "--is-inside-work-tree", read_only=True)
    return res.ok and res.stdout.strip() == "true"


def _git(
    ctx,
    *args: str,
    check: bool = False,
    timeout: float | None = None,
    read_only: bool = False,
) -> CommandResult:
    """Run git, bypassing `--dry-run` for read-only subcommands.

    A dry run must not *change* anything, but it still has to *look*: if
    `rev-parse` is suppressed too, the CLI cannot tell it is inside a git
    checkout and `--dry-run upgrade` reports "not a git checkout" — a
    statement about the repository that is simply false.
    """
    argv = ["git", *args]
    if not _which(ctx, "git"):
        return CommandResult(argv=argv, returncode=127, stdout="", stderr="git not found", duration=0.0)
    if read_only and ctx.repo.dry_run:
        proc = subprocess.run(
            argv,
            cwd=str(ctx.repo.root),
            capture_output=True,
            text=True,
            timeout=timeout or 60.0,
        )
        return CommandResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            duration=0.0,
        )
    return ctx.repo.run(argv, check=check, quiet=True, timeout=timeout or 60.0)


def _which(ctx, name: str) -> bool:
    """Is `name` on PATH? (`ctx` is unused but keeps every probe's shape the same.)"""
    return bool(shutil.which(name))


def branch(ctx) -> str:
    res = _git(ctx, "rev-parse", "--abbrev-ref", "HEAD", read_only=True)
    name = res.stdout.strip()
    return "" if (not res.ok or name in ("", "HEAD")) else name


def upstream(ctx, *, branch_name: str = "") -> str:
    target = f"{branch_name}@{{upstream}}" if branch_name else "@{upstream}"
    res = _git(ctx, "rev-parse", "--abbrev-ref", target, read_only=True)
    return res.stdout.strip() if res.ok else ""


def remote_name(ctx, *, branch_name: str = "") -> str:
    up = upstream(ctx, branch_name=branch_name)
    return up.split("/", 1)[0] if "/" in up else "origin"


def remote_url(ctx, remote: str = "origin") -> str:
    res = _git(ctx, "remote", "get-url", remote, read_only=True)
    return res.stdout.strip() if res.ok else ""


def revision_of(ctx, ref: str = "HEAD") -> Revision:
    """Describe a ref, tolerating a missing git or a detached checkout."""
    rev = Revision()
    if is_checkout(ctx):
        res = _git(ctx, "log", "-1", "--format=%H%x1f%h%x1f%s%x1f%cI", ref, read_only=True)
        if res.ok and res.stdout.strip():
            parts = res.stdout.strip().split("\x1f")
            while len(parts) < 4:
                parts.append("")
            rev = Revision(sha=parts[0], short=parts[1], subject=parts[2], date=parts[3])
        res = _git(ctx, "show", f"{ref}:VERSION", read_only=True)
        rev = Revision(
            sha=rev.sha,
            short=rev.short,
            subject=rev.subject,
            date=rev.date,
            version=res.stdout.strip() if res.ok else ctx.repo.version,
        )
        return rev
    # No git: the working tree still knows its version.
    return Revision(version=ctx.repo.version)


def current_revision(ctx) -> Revision:
    return revision_of(ctx, "HEAD")


def dirty_files(ctx) -> list[str]:
    """Modified paths, ignoring the upgrade state file we write ourselves."""
    if not is_checkout(ctx):
        return []
    res = _git(ctx, "status", "--porcelain", read_only=True)
    if not res.ok:
        return []
    lines = []
    for raw in res.stdout.splitlines():
        path = raw[3:].strip()
        if path.startswith(f"{STATE_DIR}/") or path == STATE_DIR:
            continue
        lines.append(raw.strip())
    return lines


def fetch(ctx, remote: str = "origin", *, branch_name: str = "") -> bool:
    args = ["fetch", "--prune", "--tags"]
    if remote:
        args.append(remote)
    if branch_name:
        args.append(branch_name)
    res = _git(ctx, *args, timeout=180.0)
    return res.ok


def ahead_behind(ctx, left: str, right: str) -> tuple[int, int]:
    res = _git(ctx, "rev-list", "--left-right", "--count", f"{left}...{right}", read_only=True)
    if not res.ok:
        return (0, 0)
    parts = res.stdout.split()
    if len(parts) != 2:
        return (0, 0)
    return (int(parts[1]), int(parts[0]))  # (behind, ahead) relative to `left`


def changelog(ctx, from_ref: str, to_ref: str, *, limit: int = CHANGELOG_LIMIT) -> list[str]:
    res = _git(ctx, "log", "--oneline", "--no-decorate", f"--max-count={limit}", f"{from_ref}..{to_ref}", read_only=True)
    if not res.ok:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


# -------------------------------------------------------------------- state file


def state_path(ctx) -> Path:
    return ctx.repo.path(STATE_DIR, STATE_FILE)


def read_state(ctx) -> dict[str, Any]:
    path = state_path(ctx)
    if not path.exists():
        return {"history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt state file must not block an upgrade; the history is a
        # convenience, and refusing to start because we cannot parse our own
        # notes would be the worst possible failure mode.
        return {"history": [], "warning": f"{path} is unreadable; history reset"}
    if isinstance(data, dict):
        data.setdefault("history", [])
        return data
    return {"history": []}


def write_state(ctx, state: dict[str, Any]) -> None:
    path = state_path(ctx)
    ctx.repo.write_text(path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def previous_revision(ctx) -> Revision | None:
    state = read_state(ctx)
    entry = state.get("previous")
    if isinstance(entry, dict) and entry.get("sha"):
        return Revision(
            sha=str(entry.get("sha", "")),
            short=str(entry.get("short", "")),
            version=str(entry.get("version", "")),
            branch=str(entry.get("branch", "")),
            subject=str(entry.get("subject", "")),
            date=str(entry.get("date", "")),
        )
    return None


def history(ctx, *, limit: int = 10) -> list[dict[str, Any]]:
    state = read_state(ctx)
    entries = [e for e in state.get("history", []) if isinstance(e, dict)]
    return entries[-limit:][::-1]


def record(ctx, run: UpgradeRun) -> None:
    """Persist the revision we are leaving, before the next one lands."""
    state = read_state(ctx)
    if run.from_.sha or run.from_.version:
        state["previous"] = run.from_.to_json()
    state["current"] = run.to.to_json() if run.to.known else run.from_.to_json()
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["verb"] = run.verb
    state["result"] = "ok" if run.ok else "failed"
    state.setdefault("history", []).append(
        {
            "at": state["updated_at"],
            "verb": run.verb,
            "ok": run.ok,
            "from": run.from_.to_json(),
            "to": run.to.to_json(),
            "steps": [s.to_json() for s in run.steps],
        }
    )
    state["history"] = state["history"][-50:]
    write_state(ctx, state)


# ---------------------------------------------------------------------- checking


def check(ctx, *, branch_name: str = "", remote: str = "", do_fetch: bool = True) -> UpgradeCheck:
    """Compare what is running against what is available.

    Never raises for an unusual-but-tolerable situation (no git, no upstream,
    detached HEAD): those become `blockers` or `notes` so `atf upgrade check`
    can explain the state on a machine where upgrading is not even possible.
    """
    current = current_revision(ctx)
    chk = UpgradeCheck(current=current, target=current)
    chk.dirty = dirty_files(ctx)

    if not _which(ctx, "git"):
        chk.notes.append("git is not installed — code upgrades are unavailable; images can still be rebuilt")
        chk.remote = remote_url(ctx) or ""
        return chk
    if not is_checkout(ctx):
        chk.notes.append("this install is not a git checkout (portable or archive) — code upgrades are unavailable")
        return chk

    chk.branch = branch_name or branch(ctx)
    if not chk.branch:
        chk.blockers.append("HEAD is detached — check out a branch to upgrade")
        return chk
    if chk.branch.startswith("("):
        chk.blockers.append(f"could not determine the current branch ({chk.branch})")
        return chk

    chk.upstream = upstream(ctx, branch_name=chk.branch)
    chk.remote = remote or remote_name(ctx, branch_name=chk.branch)
    if not chk.upstream:
        chk.upstream = f"{chk.remote}/{chk.branch}"
        chk.notes.append(f"branch {chk.branch} has no upstream; assuming {chk.upstream}")

    if do_fetch:
        chk.fetched = fetch(ctx, chk.remote, branch_name=chk.branch)
        if not chk.fetched:
            chk.notes.append(f"could not reach remote {chk.remote!r} — reporting against the last known state")

    if not _ref_exists(ctx, chk.upstream):
        chk.blockers.append(f"{chk.upstream} does not exist locally (run `git fetch {chk.remote}`)")
        return chk

    chk.target = replace(revision_of(ctx, chk.upstream), branch=chk.branch)
    chk.behind, chk.ahead = ahead_behind(ctx, "HEAD", chk.upstream)
    if chk.behind:
        chk.commits = changelog(ctx, "HEAD", chk.upstream)
    if chk.newer_version and not chk.behind:
        chk.commits = changelog(ctx, "HEAD", chk.upstream) or ["(version bumped without new commits)"]
    if chk.ahead:
        chk.notes.append(
            f"local branch is {chk.ahead} commit(s) ahead of {chk.upstream} — "
            "a fast-forward upgrade will refuse until that divergence is resolved"
        )
    if chk.dirty:
        chk.notes.append(
            f"{len(chk.dirty)} uncommitted change(s) in the working tree — "
            "use --stash to park them for the upgrade (restore with `git stash pop`)"
        )
    return chk


def _ref_exists(ctx, ref: str) -> bool:
    return _git(ctx, "rev-parse", "--verify", "--quiet", ref, read_only=True).ok


def plan(ctx, chk: UpgradeCheck, *, build: bool = True, backup: bool = True) -> list[str]:
    """The steps `apply` would take, in order, for `--dry-run` and for humans."""
    steps: list[str] = ["preflight: git checkout, docker, free disk"]
    if backup:
        steps.append("backup: snapshot the database before touching anything")
    if chk.can_apply and (chk.behind or chk.newer_version):
        steps.append(f"fetch: {chk.remote}")
        steps.append(f"move: {chk.current.short or 'HEAD'} → {chk.target.label} (fast-forward only)")
    elif chk.can_apply:
        steps.append("move: already at the newest revision (nothing to pull)")
    else:
        steps.append(f"move: blocked — {'; '.join(chk.blockers)}")
    if build:
        steps.append("build: docker compose build")
    steps.append("recreate: docker compose up -d (containers replaced, aliases repaired)")
    steps.append("verify: doctor checks + port diagnosis")
    steps.append(f"record: previous revision saved to {STATE_DIR}/{STATE_FILE}")
    return steps


# ---------------------------------------------------------------------- applying


def apply(
    ctx,
    *,
    ref: str = "",
    branch_name: str = "",
    remote: str = "",
    rebuild: bool = True,
    backup: bool = True,
    verify: bool = True,
    prune: bool = False,
    stash: bool = False,
    force: bool = False,
    assume_yes: bool = False,
    fetch_remote: bool = True,
) -> UpgradeRun:
    """Move the checkout forward, rebuild, recreate, verify, record.

    `force` relaxes two refusals — a dirty worktree (stashed automatically) and
    a diverged branch (which becomes a plain checkout of the target) — because
    an operator who asks for a specific ref usually knows why.
    """
    from .commands import deploy

    run = UpgradeRun(verb="upgrade")
    run.from_ = current_revision(ctx)
    checkout = is_checkout(ctx)
    docker = _which(ctx, "docker")

    # 1. preflight ------------------------------------------------------------
    if not docker:
        # A source install has no images to rebuild. Failing here would make
        # `atf upgrade` useless on exactly the machines that install without
        # docker, so the sequence changes instead: refresh the virtualenv.
        run.add("preflight", "warn", "docker is not installed — refreshing the virtualenv instead of images")
        rebuild = False
    elif checkout and dirty_files(ctx) and not ref:
        if stash or force:
            res = _git(ctx, "stash", "push", "--include-untracked", "-m", STASH_MESSAGE)
            run.add(
                "stash",
                "ok" if res.ok else "fail",
                f"parked local changes as {STASH_MESSAGE!r} (restore with `git stash pop`)"
                if res.ok
                else (res.stderr or res.stdout).strip()[:200],
            )
            if not res.ok:
                return _finish(ctx, run)
        else:
            dirty = dirty_files(ctx)
            run.add(
                "preflight",
                "fail",
                f"{len(dirty)} uncommitted change(s): " + ", ".join(d.split()[-1] for d in dirty[:5]),
            )
            run.findings.append("commit or stash local changes, or re-run with --stash")
            return _finish(ctx, run)
    if docker:
        run.add("preflight", "ok", "docker present" + (" · git checkout detected" if checkout else " · no git checkout"))

    # 2. backup before anything changes --------------------------------------
    if backup:
        run.add(*_backup_database(ctx))

    # 3. decide the target ----------------------------------------------------
    chk = check(ctx, branch_name=branch_name, remote=remote, do_fetch=checkout and fetch_remote) if checkout else check(ctx)
    if ref:
        if not checkout:
            run.add("move", "fail", "--ref needs a git checkout")
            return _finish(ctx, run)
        if not _ref_exists(ctx, ref):
            run.add("move", "fail", f"unknown ref {ref!r}")
            run.findings.append("`atf upgrade check` lists what is available; tags come from `git fetch --tags`")
            return _finish(ctx, run)
        target = revision_of(ctx, ref)
        if target.sha == run.from_.sha:
            run.add("move", "skip", f"already at {target.label}")
            run.to = run.from_
            return _finish(ctx, run)
        _git(ctx, "checkout", ref)
        moved = _ref_exists(ctx, ref) and current_revision(ctx).sha == target.sha
        run.add(
            "move",
            "ok" if moved else "fail",
            f"checked out {ref} → {target.label}" if moved else f"git checkout {ref} failed",
        )
        if not moved:
            return _finish(ctx, run)
        run.to = target
    elif checkout:
        if chk.blockers and not force:
            run.add("move", "fail", "; ".join(chk.blockers))
            return _finish(ctx, run)
        if chk.behind == 0 and not chk.newer_version:
            run.add("move", "skip", f"already at {chk.current.label} — nothing to pull")
            run.to = chk.current
            if not rebuild:
                return _finish(ctx, run)
        else:
            args = ["merge", "--ff-only", chk.upstream]
            if chk.ahead and force:
                args = ["merge", "--ff-only", chk.upstream]
            res = _git(ctx, *args, timeout=120.0)
            if not res.ok and (chk.ahead or not force):
                run.add(
                    "move",
                    "fail",
                    (res.stderr or res.stdout).strip().splitlines()[-1][:200] if (res.stderr or res.stdout) else "merge refused",
                )
                run.findings.append(
                    f"the local branch has diverged from {chk.upstream}; commit/push or pass --ref <sha> to move to an exact revision"
                )
                return _finish(ctx, run)
            run.to = current_revision(ctx)
            detail = f"{run.from_.short or 'HEAD'} → {run.to.label}"
            if chk.behind:
                detail += f" ({chk.behind} commit(s))"
            run.add("move", "ok" if res.ok else "fail", detail)
            if not res.ok:
                return _finish(ctx, run)
    else:
        run.to = run.from_
        run.add("move", "skip", "no git checkout — rebuilding the current source")

    # 4. build ----------------------------------------------------------------
    services = list(SERVICES)
    if rebuild:
        run.add(*_build(ctx, services))
        if run.failures:
            return _finish(ctx, run)
    elif not docker:
        run.add(*_refresh_deps(ctx))
    else:
        run.add("build", "skip", "--no-build given")

    # 5. recreate + aliases (docker only) -------------------------------------
    if docker:
        try:
            started = time.time()
            deploy.recreate_containers(ctx, services)
            deploy.repair_networks(ctx, services)
            run.add("recreate", "ok", f"{len(services)} container(s) recreated in {time.time() - started:.1f}s")
        except AtfError as exc:
            run.add("recreate", "fail", exc.message)
            return _finish(ctx, run)
    else:
        run.add("recreate", "skip", "no docker — source install keeps running as-is")

    # 6. verify ---------------------------------------------------------------
    if verify:
        run.add(*_verify(ctx))

    # 7. prune (opt-in: rebuilding every arch leaves a lot of dead layers) ----
    if prune:
        res = ctx.repo.run(["docker", "image", "prune", "-f"], check=False, quiet=True)
        run.add("prune", "ok" if res.ok else "warn", "dangling images removed" if res.ok else res.stderr.strip()[:200])

    if not run.to.known:
        run.to = current_revision(ctx)
    return _finish(ctx, run)


def rollback(ctx, *, rebuild: bool = True, prune: bool = False, verify: bool = True) -> UpgradeRun:
    """Return to the revision recorded before the last upgrade."""
    target = previous_revision(ctx)
    run = UpgradeRun(verb="rollback")
    run.from_ = current_revision(ctx)
    if target is None:
        run.add("lookup", "fail", f"no previous revision in {STATE_DIR}/{STATE_FILE}")
        run.findings.append("the state file is written on every upgrade; a stack that was never upgraded has no way back")
        return _finish(ctx, run)
    if not is_checkout(ctx):
        run.add("lookup", "fail", "rollback needs a git checkout")
        return _finish(ctx, run)
    if not _ref_exists(ctx, target.sha):
        run.add("lookup", "fail", f"commit {target.short} is no longer in this repository")
        run.findings.append("the commit may have been garbage-collected; check out a tag instead")
        return _finish(ctx, run)
    res = _git(ctx, "checkout", target.sha)
    if not res.ok:
        run.add("move", "fail", (res.stderr or res.stdout).strip()[:200])
        return _finish(ctx, run)
    run.to = current_revision(ctx)
    run.add("move", "ok", f"{run.from_.short or 'HEAD'} → {run.to.label}")

    services = list(SERVICES)
    docker = _which(ctx, "docker")
    if rebuild and docker:
        from .commands import deploy

        run.add(*_build(ctx, services))
        if run.failures:
            return _finish(ctx, run)
        try:
            deploy.recreate_containers(ctx, services)
            deploy.repair_networks(ctx, services)
            run.add("recreate", "ok", f"{len(services)} container(s) recreated")
        except AtfError as exc:
            run.add("recreate", "fail", exc.message)
            return _finish(ctx, run)
    elif not docker:
        run.add(*_refresh_deps(ctx))
    else:
        run.add("build", "skip", "--no-build given")

    if verify:
        run.add(*_verify(ctx))
    if prune:
        res = ctx.repo.run(["docker", "image", "prune", "-f"], check=False, quiet=True)
        run.add("prune", "ok" if res.ok else "warn", "dangling images removed")
    return _finish(ctx, run)


# ------------------------------------------------------------------- run helpers


def _finish(ctx, run: UpgradeRun) -> UpgradeRun:
    run.duration_s = time.time() - run.started
    if run.ok and run.verb != "check":
        try:
            record(ctx, run)
        except OSError as exc:  # a read-only checkout still upgraded fine
            run.steps.append(Step("record", "warn", f"could not write state: {exc}"))
    return run


def _build(ctx, services: Sequence[str]) -> tuple[str, str, str]:
    before = time.time()
    ctx.out.step("Building service images…")
    code = ctx.repo.run_streaming(["docker", "compose", "build", *services], check=False)
    if code != 0:
        return ("build", "fail", f"docker compose build exited {code}")
    return ("build", "ok", f"images built in {time.time() - before:.1f}s")


def _backup_database(ctx) -> tuple[str, str, str]:
    """Snapshot the DB before an upgrade touches the containers.

    Done through the same path as `atf db backup` so the artifact an operator
    finds after a failed upgrade is the one they already know.
    """
    from .commands import db as dbcmd

    if not shutil.which("docker"):
        return ("backup", "skip", "docker is not installed — no container to snapshot")
    if not ctx.repo.container_running("core"):
        return ("backup", "skip", "core is not running — nothing to snapshot")
    try:
        class _Args:  # noqa: D401 - the db handler only reads `output`
            output = ""

        dbcmd._backup(ctx, _Args())
    except AtfError as exc:
        return ("backup", "warn", f"snapshot failed: {exc.message}")
    dest = f"data/backup_db_{ctx.repo.version}.db"
    return ("backup", "ok", f"database snapshot → {dest}")


def _refresh_deps(ctx) -> tuple[str, str, str]:
    """Source-mode upgrade step: bring the virtualenv up to the new pins.

    This is what `atf update` used to do by hand. It runs only when docker is
    absent, because in docker mode the images are the environment.
    """
    py = ctx.repo.python()
    requirement_files = [f for f in ("requirements.txt", "core/requirements.txt") if ctx.repo.exists(f)]
    if not requirement_files:
        return ("deps", "skip", "no requirements.txt — nothing to install")
    ctx.out.step("Refreshing virtualenv dependencies…")
    code = ctx.repo.run_streaming([py, "-m", "pip", "install", "--quiet", "--upgrade", *requirement_files], check=False)
    if code != 0:
        return ("deps", "warn", f"pip exited {code} — dependencies may be stale")
    return ("deps", "ok", f"installed from {', '.join(requirement_files)}")


def _verify(ctx) -> tuple[str, str, str]:
    """Run the doctor checks that a fresh upgrade most plausibly breaks.

    Which checks depends on how the stack is deployed: a docker install is
    verified by containers and published ports, a source install by the repo,
    the API, and the database. Running the docker checks on a source install
    would report a failure that is not one.
    """
    from .commands import doctor as doc

    docker = bool(shutil.which("docker"))
    wanted = ("containers", "api", "ports", "database") if docker else ("repo", "api", "database", "disk")
    ctx.out.step("Verifying the stack…")
    report = doc.Report()
    for check in doc.CHECKS:
        name = check.__name__.removeprefix("check_")
        if name not in wanted:
            continue
        try:
            check(ctx, report, fix=False)
        except AtfError as exc:
            report.add(name, "warn", exc.message)
        except Exception as exc:  # noqa: BLE001 - a broken check must not fail the upgrade
            report.add(name, "warn", f"{type(exc).__name__}: {exc}")
    failures = report.failures
    if failures:
        return ("verify", "fail", "; ".join(f"{f.check}: {f.detail}" for f in failures))
    return ("verify", "ok", f"health score {report.score()}/100, no failures")


# ---------------------------------------------------------------------- rendering


def render_check(ctx, chk: UpgradeCheck) -> None:
    out = ctx.out
    out.title("atf upgrade check")
    out.kv(
        [
            ("installed", chk.current.label),
            ("available", chk.target.label),
            ("branch", chk.branch or "—"),
            ("upstream", chk.upstream or "—"),
            ("behind", str(chk.behind)),
            ("ahead", str(chk.ahead)),
        ]
    )
    if chk.commits:
        out.line()
        out.line("  incoming:")
        for line in chk.commits[:CHANGELOG_LIMIT]:
            out.line(f"    • {line}")
    if chk.dirty:
        out.line()
        out.warn(f"{len(chk.dirty)} uncommitted change(s): " + ", ".join(d.split()[-1] for d in chk.dirty[:5]))
    for note in chk.notes:
        out.hint(note)
    for blocker in chk.blockers:
        out.fail(blocker)
    out.line()
    if chk.blockers:
        out.line("  upgrade not possible in this state")
    elif chk.up_to_date:
        out.ok("already at the newest revision")
    else:
        out.line(f"  {chk.behind} commit(s) behind — run `atf upgrade` to move forward")


def render_run(ctx, run: UpgradeRun) -> None:
    out = ctx.out
    out.title(f"atf upgrade {run.verb}")
    marks = {"ok": ("✓", "green"), "skip": ("·", "grey"), "fail": ("✗", "red"), "pending": ("…", "grey")}
    for step in run.steps:
        mark, colour = marks.get(step.status, ("?", "grey"))
        out.line(f"  {out.paint(mark, colour)} {step.name.ljust(10)} {step.detail}")
    out.line()
    if run.from_.known or run.to.known:
        out.line(f"  {run.from_.label} → {run.to.label}")
    for finding in run.findings:
        out.hint(finding)
    if run.ok:
        out.ok(f"{run.verb} complete in {run.duration_s:.1f}s")
    else:
        out.fail(f"{run.verb} failed after {run.duration_s:.1f}s")
        out.hint("nothing was left half-done on purpose: re-run `atf upgrade` or `atf upgrade rollback`")


def render_history(ctx, entries: list[dict[str, Any]]) -> None:
    out = ctx.out
    if not entries:
        out.line("no upgrades recorded yet")
        return
    rows = []
    for entry in entries:
        src = entry.get("from") or {}
        dst = entry.get("to") or {}
        rows.append(
            [
                str(entry.get("at", "")),
                str(entry.get("verb", "")),
                str(src.get("short") or src.get("version") or "?"),
                str(dst.get("short") or dst.get("version") or "?"),
                "ok" if entry.get("ok") else "failed",
            ]
        )
    out.table(("WHEN (UTC)", "VERB", "FROM", "TO", "RESULT"), rows, empty="no upgrades recorded yet")
