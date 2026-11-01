# gRPC Contracts — two versioned surfaces, one repo

## 1. The control-plane contract (unchanged)

`proto/autoforward.proto` — the Go API's view of the Python Core. **This
contract is untouched by this PR**: no service, rpc or message was added,
removed or renumbered. 15 services, ~100 RPCs, exactly as before.

## 2. The Telegram Gateway contract (NEW — `tfgw/v1`)

`worker/proto/telegram_gateway.proto` is the narrow, business-neutral
transport contract between the Go Worker and the Python Telegram Runtime.

### Design rules the contract enforces

* **Business-neutral capabilities only.** There is no generic "invoke a
  Python method" RPC anywhere in the file. Go cannot name a pyrogram class
  or a Python module.
* **Go decides WHAT; Python executes WHAT Telegram said.** The runtime
  contains no retry, no business sleeps, no routing, no filter decisions.
* **Typed errors, not stringly ones.** Every response carries an optional
  `TelegramOperationError` whose `code` is the shared
  `DeliveryFailureCode` enum. A *Telegram* refusal is that typed field; a
  *transport* failure (runtime unreachable, deadline exceeded) is a
  gRPC-level error — the two stay distinguishable because only the transport
  one is ambiguous ("did Telegram get it?").
* **Identifiers only in metadata.** `RequestMeta` carries a correlation id,
  worker id, job id and a flat diagnostics map. It structurally cannot hold
  message content: the field set is `{correlation_id, worker_id, job_id,
  fields}` (asserted by `test_request_meta_is_only_identifiers`).
* **Session identity is an opaque id.** Every request names a session by
  `session_id`; there is no `session_string` field anywhere in the contract
  (asserted by `test_session_ids_never_carry_secrets`).
* **Correlation chain.** API request → job id → worker execution → gRPC
  metadata (`x-atf-correlation-id`) → runtime operation → RPC result →
  delivery state update.

### The surface

| RPC | Purpose |
|---|---|
| `IsSessionReady` | readiness (a *connecting* session is not ready) |
| `GetMessage`, `GetAlbumMessageIds`, `GetFileExtension` | minimal source reads |
| `DownloadMedia`, `ReloadMedia`, `PrepareMediaUpload` | staged media (shared scratch volume) |
| `ForwardMessages` | native forward (author header preserved) |
| `CopyMessage`, `CopyMessages`, `CopyMediaGroup` | clean copies, albums |
| `SendMessage` | plain transformed text |
| `SendMedia` | staged-file upload (the re-upload fallback) |
| `GatewayHealth` | contract version stamp |

### Media transport — bounded by construction

Media is **never streamed through either process's memory**: the worker
allocates a unique per-job path in the shared scratch volume
(`ATF_MEDIA_DIR`, the `atf-worker-media` compose volume) and the runtime
writes there; the upload is staged from the same path. The path is unique by
construction (job ids are unique), so two concurrent re-uploads cannot
collide — asserted by `TestMediaScratchIsPerJob`.

### Evolution

`<<contract: tfgw/v1>>` is stamped in the file and into every call's
metadata (`x-atf-contract`). Backwards compatibility rules:

* add only optional fields and new RPCs;
* never reuse a field number, never change a field type, never remove an RPC;
* the runtime reports `contract_major`/`contract_minor` on `GatewayHealth`,
  and the Go client refuses a runtime whose major version it does not speak.

**Regeneration is checked in CI**: the committed `worker/proto/gen/{go,
python}` stubs must match a fresh `protoc` run over the `.proto`, so a
contract change that forgot to regenerate fails the build.

### Authentication

The runtime's gRPC server requires the same shared `ATF_CORE_RPC_TOKEN`
bearer credential the Go API already uses against the Core, compared with
`hmac.compare_digest` (no secret-timing leak). **An empty token is a
refuse-to-start condition on both sides**:

* Go: `grpcclient.New` refuses to build a client (`client.go`);
* Go: `config.Validate` fails with a refusal naming `ATF_CORE_RPC_TOKEN`
  (`config_test.go::TestValidateRefusesEmptyToken`);
* Python: `telegram_runtime.main` exits with status 2 before binding.
