# worker/test/live — Live delivery matrix

Rerunnable live verification of the Go Worker's delivery path against the real
stack (core + worker + Telegram + 9router). Nothing here is mocked.

## Run

```
# From the repo root, with the stack up (docker compose ps):
python3 worker/test/live/run_matrix.py        # probes Telegram, writes MANIFEST.json
go test ./worker/test/live/ -run TestLiveMatrixInvariants
```

Environment needed inside `atf-core`: `ATF_MASTER_KEY` (already set by
compose). The harness runs inside the container:

```
docker cp worker/test/live/run_matrix.py atf-core:/tmp/ && \
docker exec atf-core python /tmp/run_matrix.py
```

## Safety contract

* The baseline task (`ea565d5c-ceb0-4f1c-b5f5-79eca2911642`) is never edited.
* Every replica rule carries `metadata.matrix = "live-test-v1"` and is deleted
  by the harness at the end; a start-time sweep also deletes leftovers from a
  previously killed run.
* Raw message text is never written to disk: the manifest stores sha256 of the
  delivered text plus pass/fail and reason codes.
* `delivery_jobs` rows for replicas are removed at the end so the baseline's
  durable aggregation is untouched.
