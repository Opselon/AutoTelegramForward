# Tenant-Isolation Hardening — v12.2

Scope: `security/tenant-isolation-hardening` worktree at `~/atf-security-audit`
(base `90063ba` = master @ v1.4.1). Production DB untouched by this work.

## Confirmed findings (evidence gathered 2026-10-11)

### P0 — `/api/v1/*` prefix bypasses authentication entirely
`api/main.go`: all v1 routes are registered on the bare `mux`, which is only
wrapped by RequestLogger / ErrorLogger / optional APIKeyAuth. `RequireAuth`
wraps only `dashMux` (`/api/*`). No internal client consumes `/api/v1/*`
(grep over `web/`, `api/`, `core/` shows zero callers).
**Live evidence:** `curl` with no `Authorization` header returns the full rule
list, the session list (phone number, TG id, username), and
`POST /api/v1/sessions/backup` returns the **encrypted session string** —
full account-takeover material — for `1d12518c…`. Remote-reachable
(`http://83.228.250.42:8080/api/v1/rules` → 200 from outside the box).
Pause/resume under v1 returned 404 because `identity(r)` is empty there —
the *only* reason v1 was not also a write surface.

### P1 — gRPC servicers perform by-id mutations with no ownership check
`core/infrastructure/grpc_server/servicers.py`:

| servicer | repo call | scoped? |
|---|---|---|
| `UpdateRule` (309) | `get(id)` → `update` | NO |
| `DeleteRule` (411) | `delete(id)` | NO |
| `PauseRule` (490) / `ResumeRule` (500) | `get(id)` → `update` | NO |
| `UpdateFilter` (753) | `get(id)` → `update` | NO |
| `DeleteFilter` (771) | `delete(id)` | NO |
| `UpdateAIConfig` (804) | `get(id)` → `update` | NO |
| `DeleteAIConfig` (827) | `delete(id)` | NO |
| `TestAIRewrite` (831) | `test_rewrite(id)` | NO — bills another tenant's key |
| `TestRule` (415) | `get(id)` | NO — reads rule config |
| `BackupSession` (160) | `get(id)` → returns `session_string_encrypted` | NO |
| `TerminateSession` (175) | `delete(id)` + `_pool.stop` | NO |
| `ResolveChat` (180) | `get(session_id)` or `list_all()` first active | NO — drives an arbitrary session |
| `RestoreSession` (166) | creates a session from a raw blob | NO |

Root cause: the HTTP layer's "list-then-filter-in-Go" guard
(`ownsRule`/`ownsFilter`/`ownsAIConfig`) is client-side filtering — it is the
*only* thing standing between a caller and the unscoped gRPC op. Any gRPC
client (or the unauthenticated v1 routes) skips it. The correct enforcement
point is the use-case boundary, which already has the owned variants
(`get_owned`/`update_owned`/`delete_owned`/`toggle_owned`) — they are simply
not called.

`owner_user_id = None` semantics are preserved: legacy/admin path (global
scope), so single-tenant installs keep working.

### P2 — bot FSM steps read `st.buffer["rule_id"]` without re-verification
Entry point `_cb_rule_edit_draft` (2311) is admin-gated, but the `_step_*`
handlers dispatched by `_route_text` never re-check ownership of that id.
Defense in depth: re-verify at the use-case call.

### P3 — internal gRPC has no auth at all
Core `:6001` binds `*:6001` and Logger `:6002` serve with no interceptor.
`ResolveChat` is reachable over gRPC and drives an arbitrary Telegram session.
Out of scope for a full mTLS rollout here, but documented; the owner-scoping
of `ResolveChat` removes the cross-tenant abuse vector.

## Fixes

1. **Delete the dead `/api/v1/*` block** from `api/main.go` (no callers;
   fully shadowed by the authenticated `/api/*` dashboard routes — every v1
   handler name maps 1:1 to an existing dashMux route).
2. **Proto**: add `int64 owner_user_id` to the by-id mutation requests that
   lack it — `UpdateRuleRequest` (via `ForwardRule`, already has field 46),
   `DeleteRuleRequest`, `PauseRuleRequest`, `UpdateFilterRequest` (via
   `FilterRule` field 11), `DeleteFilterRequest`, `UpdateAIConfigRequest`
   (via `AIConfig` field 12), `DeleteAIConfigRequest`, `TestAIRewriteRequest`,
   `TestRuleRequest`, `BackupSessionRequest`, `TerminateSessionRequest`,
   `RestoreSessionRequest`, `ResolveChatRequest`.
   Regenerate Go + Python stubs.
3. **Go**: pass `OwnerUserId: identity(r).UserID` on every such call; add
   ownership guards to the handlers that still lack them
   (`TestRule`, `TestAIRewrite`, `BackupSession`, `TerminateSession`,
   `ReplayDeadLetter` → already has owner field, `SetPVResponder`).
4. **Servicers**: route the by-id ops through the owned use-case variants.
   `owner_user_id == 0` (unauthenticated/legacy) is treated as
   *denied by default* for the secret-bearing and mutation ops, except where
   the legacy single-tenant path must still work — there, the caller is the
   API gateway with a verified identity and supplies a real uid.
5. **Bot FSM**: pass `self._scope_uid(uid)` into the `_step_*` use-case calls.
6. **Tests**: adversarial Go + pytest suites for every fixed path.
