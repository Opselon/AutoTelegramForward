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

## Log purge contract

The log archive is a single shared store: it is written by every service and
carries no tenant column on its purge path. The purge therefore cannot be
scoped by filtering — a `DELETE`-style filter applied to a shared store is a
cross-tenant destructive operation, and it used to be reachable by any
authenticated caller (an omitted `rule_id` purged everything).

- `POST /api/logs/purge` — body optional, `{rule_id?, older_than_hours?, ...}`.
  - A **non-administrator MUST name a task they own** in `rule_id`; anything
    else is `403` `rule_scope_required`. An id that is not the caller's is
    `404`, deliberately indistinguishable from an unknown one.
  - The installation-wide purge (no `rule_id`) is **administrator-only**.
  - The command is audited with the verified actor and the resolved scope.
- `DELETE /api/dlq` — purge, answering with the number of entries removed.

This is a deliberate, security-driven narrowing of an endpoint that previously
accepted an unscoped call. The route, the method and the request fields are
unchanged, so no client breaks when it already passes `rule_id`; a client that
relied on the installation-wide form now gets a named refusal instead of
silently deleting other tenants' history. The dashboard
(`handlePurgeLogs`) and the CLI both surface the scope requirement rather than
asking the user to sign in again.

## Dead-letter queue contract

A message that exhausts its retry budget lands in the dead-letter queue as one
row per `(rule, source, message, target)`. Every client — the dashboard, the
Android app, the CLI and the bot — reaches it through the same owner-scoped
repository contract, so the count a badge shows and the rows a list returns
cannot disagree:

- `GET /api/dlq` — list, newest first, with `rule_id`, `since`, `limit`
  filters applied **before** the limit.
- `POST /api/dlq/{id}/retry` — replay. The job's existing `delivery_jobs` row is
  **reset** (status `PENDING`, `attempts` back to 0, full retry budget
  restored); a second row is never inserted.
- `DELETE /api/dlq` — purge, answering with the number of entries removed.

Replay answers with a documented result code rather than an unqualified
success:

| `error_code` | meaning |
| --- | --- |
| `requeued` | the job is back in the queue |
| `not_found` | no such entry **in the caller's scope** (a foreign id and an unknown id are indistinguishable) |
| `already_pending` | the job is already queued or in flight |
| `already_sent` | the job has already been delivered |
| `replay_failed` | the entry exists but could not be woken |

Scope is a set of rule ids resolved server-side from the caller identity. An
empty set means the caller owns nothing and therefore sees nothing; an id that
is not in the set is `not_found`, never `forbidden` — the API does not confirm
the existence of another tenant's entries.

## Dashboard login contract

A dashboard session is opened by a **single-use login code**, never by a
Telegram user id:

- `POST /api/auth/exchange-bot-token` — body `{"login_code": "<code>"}`. Answers
  `200` with the session, or `401` with `error_code` `LOGIN_CODE_REQUIRED` (no
  code supplied) / `INVALID_LOGIN_CODE` (unknown, expired or already used —
  deliberately indistinguishable). Posting a `user_id` is refused: a Telegram id
  is public, so minting from one would let any caller open any account.
- `POST /api/auth/tma` — Mini App `init_data`, verified against the bot token by
  HMAC-SHA256 before the origin is read. This is the only path where a user id
  mints a session, and only because Telegram signed it.
- `?code=` on the dashboard URL is the bot's 1-tap link: 10-minute lifetime,
  burned on first use, stored only as a SHA-256 digest, and stripped from the
  address bar on arrival. `?token=` (a legacy session link) still works for its
  own 7-day life and is also stripped.
- `PATCH /api/auth/change-password` — requires a `Bearer` token; the subject of
  the change is the token's user, and a body `user_id` naming anybody else is
  `403`.

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
