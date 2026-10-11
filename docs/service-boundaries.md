# Service Boundary & Inter-Service Contracts — v12.2

## Components

| Service | Port | Bind | Auth |
|---|---|---|---|
| Core (gRPC) | 6001 | `*:6001` | **none — internal trust zone** |
| Logger (gRPC) | 6002 | `127.0.0.1:6002` | none — local only |
| API gateway (HTTP) | 8080 | `*:8080` | Bearer JWT (validated against Core) |
| Web dashboard | 8088 | `*:8088` | static + talks to API only |

## Trust model

The API gateway is the **only** authentication boundary. It validates the
caller's JWT against Core's `ValidateToken` and derives `Identity.UserID`
from it (middleware `authMiddleware`, mounted via `RequireAuth` on
`/api/*`). That identity is then forwarded to Core on every request.

Before v12.2 the gateway performed ownership itself by listing the caller's
entities and filtering in Go ("list-then-filter"). That is client-side
filtering: it is bypassable by any direct gRPC caller, and before this
change the whole `/api/v1/*` prefix bypassed authentication entirely.

**v12.2 moves enforcement to the use-case boundary inside Core.** Every
by-id read/mutation now flows through `get_owned`/`update_owned`/
`delete_owned`/`toggle_owned`, with `owner_user_id` passed from the gateway.

## Per-endpoint contracts

### Rule lifecycle
| Op | Identity | Enforcement |
|---|---|---|
| `ListRules` | `OwnerUserId` in request | repo `list_by_owner` |
| `CreateRule` | `rule.OwnerUserId` from `Identity.UserID` | stamped on create |
| `UpdateRule` | `rule.OwnerUserId` | `get_owned` before mutate |
| `DeleteRule` | `DeleteRuleRequest.owner_user_id` | `delete_owned`, PermissionError → denial |
| `PauseRule`/`ResumeRule` | `PauseRuleRequest.owner_user_id` | `get_owned` before mutate |
| `TestRule` | `TestRuleRequest.owner_user_id` | `get_owned` — only owner may dry-run |

### Sessions
| Op | Identity | Enforcement |
|---|---|---|
| `BackupSession` | `owner_user_id` | `get_owned` — only owner receives the session string |
| `RestoreSession` | `owner_user_id` | restored session is bound to the importer |
| `TerminateSession` | `owner_user_id` | `delete_owned`, PermissionError → denial |
| `ResolveChat` | `owner_user_id` | session choice scoped to caller; `list_by_owner` |

### Filters / AI configs
| Op | Identity | Enforcement |
|---|---|---|
| `UpdateFilter`/`DeleteFilter` | entity / `owner_user_id` | `get_owned` / `delete_owned` |
| `UpdateAIConfig`/`DeleteAIConfig` | entity / `owner_user_id` | same |
| `TestAIRewrite` | `owner_user_id` | `test_rewrite_owned` — refuses to bill a foreign key |
| `TestPrompt` | `owner_user_id` | `get_owned` on the referenced AI config |

### Delivery / DLQ
Scoped since v12 via `_owner_rule_ids`: stats, dead-letter list, replay and
purge are all restricted to the caller's rule ids.

### Logging
The Logger has **no tenant tagging**. Logs are operational (service/level/
category) and are shared. This is deliberate: they carry no message content
or tenant identifiers. Query/Purge are authenticated at the gateway; purge
is an operator action exposed only to authenticated dashboard users.

## Failure semantics

- **Missing/forged/expired token** → 401 at the gateway, before Core is
  reached. `identity(r).UserID == 0` inside an authenticated route is
  treated as "no tenant scope" — the entity must be in the legacy/system
  scope (owner 0) or the call is refused.
- **Cross-tenant access** → 404 from the gateway (rule/filter/config "not
  found") and a warning event from Core (`DeleteRule: owner=… denied …`).
  Denials never reveal whether the entity exists for another tenant.
- **Timeouts/retries** cannot bypass authorization: the owner check happens
  inside the same use-case call that performs the mutation, so a retried
  request re-verifies ownership.

## Remaining known exposure

Core (`:6001`) and Logger (`:6002`) have no authentication of their own.
They rely on the network being a private trust zone. v12.2 removes the
cross-tenant abuse vector from the Core API surface (all by-id ops are
owner-scoped), but a network-level attacker could still read
`BackupSession` if it can reach 6001. Mitigations: bind Core to loopback in
multi-tenant deployments, or front it with an authenticated reverse proxy.
