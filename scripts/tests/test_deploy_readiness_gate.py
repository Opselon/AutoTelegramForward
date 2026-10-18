"""Regression tests: a deploy is not done until the API can enforce its limits.

Defect being pinned (observed live): a compose run was pointed at an env file that
did not carry ``ATF_REDIS_PASSWORD``, so the api and core containers were started
with an EMPTY one while the redis service still required a password. The api
started, logged ``ratelimit: enabled (redis:tb, ...)``, and then refused every
token exchange with ``503 rate_limit_unavailable``; core logged "Authentication
required" every 30s and fell back to "assume it is the only replica". Every
container stayed healthy and ``GET /healthz`` answered 200, so the deploy finished
with its ``== healthy ==`` banner and wrote its deployed-tag file for a stack whose
authentication was down. The gap was the gate: container health was the only thing
asked.

Contract these tests enforce:
  * the deploy decides on the API's OWN readiness answer (an HTTP probe of
    ``/readyz``), not on container health alone;
  * a stack that never reports ready fails the deploy (non-zero) and says why —
    it does not get recorded as the deployed tag;
  * a stack that becomes ready inside the window passes the gate;
  * the gate reads its endpoint and window from the environment, so a deployment
    that publishes the API on another port can still be gated;
  * an operator can only skip the gate by saying so explicitly.

The first four execute the script's own ``wait_ready`` function under bash with a
stubbed ``curl``: real shell, real control flow, real exit status. A text-level
grep would pass on a function that never loops or never fails, so the last test
(the one wiring check) is explicitly structural and labelled as such.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "scripts" / "deploy_release.sh"

# A stub `curl` that answers the way the real one does: -f exits 22 on an error
# status, and only a ready body exits 0. It becomes ready on the Nth attempt so a
# test can drive both "ready immediately" and "ready after a retry".
STUB_CURL = """#!/bin/sh
state="${STUB_STATE}/attempts"
n=$(cat "$state" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$state"
if [ "$n" -ge "${STUB_READY_AFTER:-1}" ]; then
  echo '{"status":"ready","ready":true,"checks":[{"name":"ratelimit","ok":true}]}'
  exit 0
fi
echo '{"status":"not-ready","ready":false,"checks":[{"name":"ratelimit","ok":false,"detail":"NOAUTH"}]}'
exit 22
"""


def deploy_text() -> str:
    return DEPLOY.read_text(encoding="utf-8")


def wait_ready_function() -> str:
    """The script's own wait_ready function, extracted by brace matching.

    Extracting (rather than re-writing) is the point: the test then runs the code
    that ships. A missing function fails the test loudly instead of silently
    passing an empty string to bash.
    """
    text = deploy_text()
    match = re.search(r"^wait_ready\(\)\s*\{$", text, re.MULTILINE)
    assert match, "scripts/deploy_release.sh no longer defines wait_ready()"
    lines = text[match.start():].splitlines()
    depth = 0
    body: list[str] = []
    for line in lines:
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(body)


def run_wait_ready(tmp_path: Path, *, ready_after: int, timeout: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the shipped wait_ready under bash with a stubbed curl on PATH."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    curl.write_text(STUB_CURL, encoding="utf-8")
    curl.chmod(0o755)
    # sleep is NOT stubbed: the gate's retry pacing is part of what is under test.
    # A no-op sleep turns the polling loop into a spin, which reaches any "ready
    # after N attempts" threshold inside any window — so a "never ready" fixture
    # would pass as ready and prove nothing.

    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_STATE": str(tmp_path),
        "STUB_READY_AFTER": str(ready_after),
        "READY_URL": "http://127.0.0.1:8080/readyz",
        "READY_TIMEOUT": timeout,
    }
    env.pop("ATF_SKIP_READY_GATE", None)
    env.update(extra_env or {})

    script = f"{wait_ready_function()}\nwait_ready\n"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


def test_a_stack_that_never_reports_ready_fails_the_gate(tmp_path: Path) -> None:
    result = run_wait_ready(tmp_path, ready_after=99, timeout="1")

    assert result.returncode != 0, (
        "a deployment whose readiness endpoint never reports ready must fail the deploy; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "127.0.0.1:8080/readyz" in combined, (
        f"the failure must name the endpoint it could not verify; got {combined!r}"
    )
    assert "ratelimit" in combined or "NOAUTH" in combined, (
        f"the failure must carry the reason from the readiness body; got {combined!r}"
    )


def test_a_stack_that_becomes_ready_inside_the_window_passes(tmp_path: Path) -> None:
    # Not ready on the first probe, ready on the second: the gate must retry, not
    # give up on the first 503.
    result = run_wait_ready(tmp_path, ready_after=2, timeout="30")

    assert result.returncode == 0, (
        f"a readiness endpoint that answers on retry must pass the gate; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    attempts = (tmp_path / "attempts").read_text(encoding="utf-8").strip()
    assert int(attempts) >= 2, f"the gate did not retry; attempts={attempts}"


def test_the_gate_is_skippable_only_on_request(tmp_path: Path) -> None:
    result = run_wait_ready(
        tmp_path, ready_after=99, timeout="1", extra_env={"ATF_SKIP_READY_GATE": "1"}
    )

    assert result.returncode == 0, (
        "an explicit ATF_SKIP_READY_GATE=1 must let an operator deploy anyway; "
        f"stderr={result.stderr!r}"
    )
    assert "skip" in (result.stdout + result.stderr).lower(), (
        "skipping verification must be said out loud, not silently tolerated"
    )


def test_the_gate_refuses_to_guess_when_it_cannot_probe(tmp_path: Path) -> None:
    # curl absent from PATH: the gate cannot verify. It must fail loudly rather
    # than report a stack ready that nobody checked.
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    # bash itself is invoked by absolute path: stripping PATH must remove curl
    # from the picture, not the interpreter running the gate.
    bash = shutil.which("bash") or "/bin/bash"
    env = {
        **os.environ,
        "PATH": str(empty),
        "READY_URL": "http://127.0.0.1:8080/readyz",
        "READY_TIMEOUT": "1",
    }
    result = subprocess.run(
        [bash, "-c", f"{wait_ready_function()}\nwait_ready\n"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0, "an unverifiable deployment must not pass silently"
    assert "curl" in (result.stdout + result.stderr).lower(), (
        f"the failure must name what is missing; got {result.stdout + result.stderr!r}"
    )


def test_readiness_is_checked_before_the_deploy_is_recorded() -> None:
    """Structural wiring check (the behaviour itself is covered above).

    The deployed-tag file is what the next deploy reads as "currently running", so
    it must only be written once readiness has passed.
    """
    text = deploy_text()

    assert "wait_ready" in text, "the deploy script never calls its readiness gate"
    gate_at = text.index("wait_ready\n") if "wait_ready\n" in text else text.index("wait_ready")
    record_at = text.index("> \"$DEPLOYED_TAG_FILE\"")
    assert gate_at < record_at, (
        "the deployed tag is recorded before readiness is verified, so a stack whose "
        "authentication is down would be remembered as the deployed release"
    )


def test_the_readiness_endpoint_matches_the_route_the_api_serves() -> None:
    text = deploy_text()
    default_url = re.search(r"ATF_READY_URL:-\"?(https?://[^}\"'\s]+)", text)
    assert default_url, "the readiness URL must come from ATF_READY_URL with a default"

    path = urlsplit(default_url.group(1)).path.rstrip("/")
    assert path, f"the readiness URL must carry a path: {default_url.group(1)}"

    main_go = (REPO_ROOT / "api" / "main.go").read_text(encoding="utf-8")
    # The route is registered as `mux.HandleFunc("GET /readyz", h.Ready)`, so the
    # method and path are one string: ask for the pair, not for a bare quote.
    assert f"GET {path}" in main_go, (
        f"the gate probes {path} but api/main.go does not register that route"
    )
