# Telegram Runtime Operations — runbook

## Running it

In stage 1 the runtime runs **inside the Core process**: the Core's
`main.py` binds the `tfgw/v1` servicer on `ATF_RUNTIME_ADDR` (default
`0.0.0.0:50053`) alongside its control-plane port 50051. There is no new
service to operate.

Standalone (development / integration tests):

```bash
ATF_CORE_RPC_TOKEN=<secret> python -m telegram_runtime.main --addr 0.0.0.0:50053
```

An empty `ATF_CORE_RPC_TOKEN` exits with status 2 before binding anything
— this is intentional: unauthenticated access to these RPCs would let any
caller on the network drive Telegram sessions.

## Sessions

* The runtime is an adapter **over** the Core's `ClientPool`; it does not
  own a second pool. Restarting the runtime does not log anybody out,
  because the session rows live in the Core's encrypted store.
* `IsSessionReady` reports `ready` only when a live, connected, not-
  time-limited client exists. A *connecting* session is not ready — the
  worker defers rather than failing the job.
* A time-limited (FloodWait-suspended) session is reported as
  `SESSION_TIME_LIMITED`, which the worker translates into a `FLOOD_WAIT`
  defer for the demanded window.

## The contract the runtime serves

`worker/proto/telegram_gateway.proto`, stamped `tfgw/v1`. Health check:

```bash
grpcurl -H "authorization: Bearer <token>" \
  -d '{"ping":"1"}' <runtime>:50053 TelegramGateway/GatewayHealth
# -> {"contract":"tfgw/v1","contractMajor":1,"contractMinor":0,"status":"ok"}
```

## Media

The runtime writes downloads into a path the **Go Worker owns**
(`/var/lib/atf/media/<job id>` in the container, the `atf-worker-media`
volume). The worker owns cleanup. Do not point `ATF_MEDIA_DIR` at a
directory another service manages.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `UNAUTHENTICATED: runtime rpc credential required` | missing or wrong bearer token | both sides must set the same `ATF_CORE_RPC_TOKEN` |
| `refused unauthenticated runtime rpc` in the runtime log | a caller presented no credential | find the caller — it is not the worker |
| every send fails `SESSION_UNAVAILABLE` | the session has no live client | check the Core's session state, not the runtime's |
| `MEDIA_DOWNLOAD_FAILED` but the file exists | the staged path vanished between staging and upload | check the volume's free space and the cleanup policy |
| `contract` mismatch on `GatewayHealth` | the worker speaks a newer contract than the runtime | upgrade both from the same commit |
