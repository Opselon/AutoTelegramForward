# API compatibility

## SemVer policy

- **Product version** (`VERSION` file, `product_version`): user-facing
  release line (`1.5.0`). Bumped per release; tags must match it
  (`v<version>`), enforced by the release `meta` job.
- **API version** (`api_version`, owned by the Go module in
  `api/internal/handlers/version.go` as `APIVersion`, currently `1.0.0`):
  bump MINOR for backwards-compatible endpoint additions, MAJOR for
  breaking changes.
- **Min supported client** (`min_supported_client`, `MinSupportedClient` in
  the same file): oldest client the server guarantees to work with.
- Mobile pins its floor separately in `mobile/assets/cfg/env.json`
  (`minSupportedApiVersion`) and refuses to operate against an older server
  instead of silently misbehaving.

## `GET /api/version` contract

Public, **unauthenticated**. Response shape (stable — additive changes only
within a major API version):

```json
{
  "product_version": "1.5.0",
  "api_version": "1.0.0",
  "min_supported_client": "1.0.0",
  "commit_sha": "abc123...",
  "features": ["rules", "filters", "ai_configs", "sessions", "logs", "dlq", "backup"]
}
```

- `product_version`: resolved at runtime from the repo-root `VERSION` file
  (falls back to the `-ldflags`-injected `ProductVersion`, then `"dev"`).
- `commit_sha`: `$ATF_COMMIT_SHA` env, else the `-ldflags`-injected
  `CommitSHA`, else `"unknown"`.
- `features`: capability keys clients can probe to gate UI without
  authenticating first.

`release/manifest.json` records `product_version`, `api_version`,
`min_supported_client`, `commit_sha`, and `features` for every release, so
an operator can reconcile a deployed server against the mobile build
without querying it.

## Migration strategy

1. **Additive first**: new endpoints/fields are added without removing old
   ones; MINOR bump only.
2. **Client gates on `min_supported_client`**: mobile compares the server's
   `api_version` against its `minSupportedApiVersion` at connect time
   (`ApiClient`/server-connection dialog) and surfaces an upgrade prompt
   instead of issuing calls the server cannot serve.
3. **Breaking changes** (MAJOR bump): keep the old endpoint serving for at
   least one MINOR cycle where feasible; announce the removal window in the
   release notes; bump `MinSupportedClient` so outdated clients fail fast
   with a clear message.
4. **Self-hosted upgrades** follow `docs/self-hosting.md` (rebuild on the
   new tag; `atf-data` volume untouched; roll back by checking out the
   previous tag).
