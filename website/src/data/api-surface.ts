/**
 * Per-route prose for the API reference.
 *
 * The *route list* is not written here. It is parsed from `api/main.go` at build
 * time (scripts/sync-repo-content.mjs → repo-info.json), including the routes
 * that are registered through middleware (`mux.Handle("PUT /logs/policy",
 * h.RequireAdmin(...))`). This file only says what each parsed route is for, what
 * it accepts and — where it matters — what the wire actually returns.
 *
 * Why the split: a hand-written route table rots the moment a handler is added,
 * while a parsed table with no prose is unusable. tests/api-surface.test.mjs
 * joins the two and fails when a registered route has no entry here, so a new
 * endpoint cannot ship undocumented.
 *
 * Every field name and every bound below is read from the Go handler (or the
 * protobuf request it binds) rather than inferred:
 *  - task bounds            api/internal/handlers/tasks.go, core/application/user_tasks.py
 *  - login / TMA shapes     api/internal/handlers/auth.go, proto/autoforward.proto
 *  - probe + setup payloads api/internal/handlers/system.go, readiness.go, version.go
 *  - refusal mapping        api/internal/handlers/handlers.go (grpcError, writeJSON)
 */

export interface DetailField {
  name: string;
  type: string;
  /** `false` renders as Optional, `'conditional'` as Conditional. */
  required?: boolean | 'conditional';
  notes: string;
}

export interface EndpointDetail {
  summary: string;
  /** Free-form caveat rendered as its own row. */
  note?: string;
  statuses?: string[];
  fields?: DetailField[];
  /** A request body worth showing. */
  request?: string;
  /** A response body worth showing. */
  response?: string;
}

export const groupBlurbs: Record<string, { blurb: string; scope: string }> = {
  'Service health & system': {
    blurb:
      'The unauthenticated surface an operator, a load balancer or the upgrade gate calls: liveness, readiness, the version block, the environment contract and the live probe of every service in the chain. These routes answer without a token because they must be callable precisely when authentication is the thing that is broken.',
    scope: 'public probe',
  },
  Authentication: {
    blurb:
      'Public by definition — this is where a caller stops being anonymous. Three ways in: username and password, a single-use code the bot hands to an already-verified Telegram user, or signed Mini App init data. Every route here is inside the authentication rate class, so the brute-force budget is enforced before a token is ever minted.',
    scope: 'public, rate-limited',
  },
  'Authenticated control plane': {
    blurb:
      'The tenant surface. Each call resolves the caller from the verified token and then queries only that identity’s rows; an owner id in the body is never read. A row that belongs to somebody else is reported as missing, so ids cannot be probed.',
    scope: 'tenant-scoped',
  },
  Administration: {
    blurb:
      'Installation-wide operations. The route is wrapped in the administrator gate, and the core re-checks the role on the other side of gRPC, so a forged or stale admin claim fails twice.',
    scope: 'administrator only',
  },
};

export function scopeNoteFor(path: string, auth: string): string {
  if (auth === 'admin' || path.startsWith('/api/admin/')) {
    return 'Installation-wide. Administrator flag required on the route and re-verified by the core.';
  }
  if (auth === 'bearer') {
    return 'The verified user id is the only tenant key; a caller-supplied owner id is ignored.';
  }
  if (path.startsWith('/api/auth/')) {
    return 'Anonymous caller; the response is what establishes identity.';
  }
  return 'No identity and no tenant data — deployment-level facts only.';
}

const ok = ['200'];
const auth = ['200', '401'];
const owner = ['200', '401', '404'];
const write = ['200', '400', '401', '404'];

export const endpointDetail: Record<string, EndpointDetail> = {
  // ---------------------------------------------------- service health & system
  'GET /healthz': {
    summary: 'Liveness. Answers 200 as soon as the process can serve, with nothing checked downstream.',
    note: 'Liveness must not depend on a dependency: a core outage is not a reason to restart a healthy API process.',
    statuses: ['200'],
    response: `{
  "status": "ok",
  "time": "2026-10-19T00:00:00Z"
}`,
  },
  'GET /health': {
    summary: 'Alias of /healthz, kept for gateways that were configured against it.',
    statuses: ok,
  },
  'GET /readyz': {
    summary:
      'Readiness, and a strictly stronger signal than liveness: it pings the rate-limiter store and answers 503 when a wired limiter cannot enforce the budget it promised.',
    note: 'A deployment with the limiter disabled is ready — the in-process fallback is then the whole limit. Only a wired limiter that cannot reach its store makes this 503.',
    statuses: ['200', '503'],
    response: `{
  "status": "ready",
  "ready": true,
  "time": "2026-10-19T00:00:00Z",
  "checks": [
    { "name": "ratelimit", "ready": true, "detail": "redis" }
  ]
}`,
  },
  'GET /ready': {
    summary: 'Alias of /readyz.',
    statuses: ['200', '503'],
  },
  'GET /api/version': {
    summary:
      'The client-negotiation block: product version, API contract version, the oldest client still supported, the commit that built the binary, and the capability keys a client may probe before using a feature.',
    statuses: ok,
    response: `{
  "product_version": "2.2.4",
  "api_version": "1.0.0",
  "min_supported_client": "1.0.0",
  "commit_sha": "07f3613",
  "features": ["rules", "filters", "ai_configs", "sessions", "logs", "dlq", "backup", "system_setup", "env_contract"]
}`,
  },
  'GET /api/system/setup': {
    summary:
      'One answer to “is this deployment usable?”: the version block, the environment contract, a live probe of all four services, the rate-limiter summary and the next command to run.',
    note: 'Add ?probe=0 to skip the live probes and get the same verdict from configuration alone — the form the web dashboard uses when a probe would be too slow.',
    statuses: ok,
    response: `{
  "ready": true,
  "ratelimit": { "mode": "redis", "enforcing": true },
  "product_version": "2.2.4",
  "api_version": "1.0.0",
  "deployment": "docker-compose",
  "checks": [{ "key": "ATF_BOT_TOKEN", "set": true, "required": true }],
  "missing_required": [],
  "missing_recommended": [],
  "services": [{ "name": "core", "online": true, "latency_ms": 4, "detail": "gRPC + pipeline" }],
  "next_steps": ["atf validate --runtime — full health re-check of the live stack"]
}`,
  },
  'GET /api/v1/system/setup': {
    summary: 'Versioned alias of /api/system/setup — same body, same booleans-only rule.',
    statuses: ok,
  },
  'GET /api/system/env': {
    summary:
      'The environment contract: every recognised key with whether it is set and whether it is required, plus the hints that turn a missing key into an actionable next step. Values are never returned.',
    note: 'A secret is reported as set:true or set:false. The value itself has no route, by design.',
    statuses: ok,
    response: `{
  "checks": [
    { "key": "ATF_BOT_TOKEN", "set": true, "required": true, "hint": "Create the bot with @BotFather" }
  ],
  "missing_required": [],
  "missing_recommended": ["ATF_REDIS_PASSWORD"],
  "deployment": "docker-compose",
  "ok": true
}`,
  },
  'GET /api/v1/system/env': {
    summary: 'Versioned alias of /api/system/env.',
    statuses: ok,
  },
  'GET /api/health/services': {
    summary:
      'Live reachability of every service in the chain — core (gRPC channel plus pipeline loop), logger, web and this process — probed in parallel with short timeouts.',
    note: 'The overall status is "healthy" only when every probe answers; a single failure degrades the whole stack to "degraded" with a per-service detail.',
    statuses: ok,
    response: `{
  "status": "healthy",
  "time": "2026-10-19T00:00:00Z",
  "services": [
    { "name": "core", "online": true, "detail": "gRPC + pipeline loop", "latency_ms": 4 },
    { "name": "logger", "online": true, "detail": "log store answers", "latency_ms": 2 }
  ]
}`,
  },
  'GET /api/v1/health/services': {
    summary: 'Versioned alias of /api/health/services.',
    statuses: ok,
  },
  'GET /api/healthz': {
    summary:
      'Liveness at the /api prefix: identical to /healthz, for callers that prefix every path with /api.',
    statuses: ok,
  },
  'GET /api/health': {
    summary: 'Liveness alias at the /api prefix.',
    statuses: ok,
  },
  'GET /api/v1/health': {
    summary: 'Liveness versioned alias.',
    statuses: ok,
  },
  'GET /api/readyz': {
    summary:
      'Readiness at the /api prefix: identical to /readyz (pings the rate-limiter store; 503 when a wired limiter cannot enforce its budget).',
    statuses: ['200', '503'],
  },
  'GET /api/ready': {
    summary: 'Readiness alias at the /api prefix.',
    statuses: ['200', '503'],
  },
  'GET /api/v1/ready': {
    summary: 'Readiness versioned alias.',
    statuses: ['200', '503'],
  },
  'GET /api/system/ready': {
    summary: 'Readiness under the system prefix (same readiness gate).',
    statuses: ['200', '503'],
  },
  'GET /api/gateway': {
    summary:
      'Gateway/runtime info for the dashboard header: whether core is reachable, its version and uptime, session and rule counts and the service ports the panel shows.',
    note: 'Always answers 200 — even when core is unreachable, atf_core_online is then false, so the panel degrades instead of erroring.',
    statuses: ok,
    response: `{
  "atf_core_online": true,
  "version": "2.2.4",
  "uptime_seconds": 1043,
  "total_rules": 3,
  "total_sessions": 2,
  "grpc_port": 8080
}`,
  },
  'GET /api/v1/gateway': {
    summary: 'Versioned alias of /api/gateway.',
    statuses: ok,
  },
  'GET /api/openapi.yaml': {
    summary:
      'The control plane’s own OpenAPI 3.1 document. It ships inside the image next to the code that serves it, so the published contract cannot drift from the binary.',
    note: 'This route is public on purpose: the schema is not tenant data, and a client needs it before it has a token.',
    statuses: ok,
  },

  // ------------------------------------------------------------ authentication
  'POST /api/auth/login': {
    summary: 'Username and password → a 7-day HS256 JWT, its expiry and the account record.',
    note: 'A wrong password and an unknown username return the same refusal, so the endpoint is not an account oracle.',
    statuses: ['200', '400', '401', '429'],
    fields: [
      { name: 'username', type: 'string', notes: 'Account username.' },
      { name: 'password', type: 'string', notes: 'Account password.' },
      { name: 'telegram_user_id', type: 'int64', required: false, notes: 'Optional link proof when the call is issued from the bot.' },
    ],
    request: `{
  "username": "operator",
  "password": "…"
}`,
    response: `{
  "success": true,
  "token": "<jwt>",
  "expires_at": 1790000000,
  "account": { "user_id": 5094837833, "username": "operator", "is_admin": false }
}`,
  },
  'POST /api/auth/register': {
    summary: 'Creates a web account and returns the account record. The first account on a fresh install is the administrator.',
    statuses: ['201', '400', '409', '429'],
    fields: [
      { name: 'username', type: 'string', notes: 'Unique login name.' },
      { name: 'password', type: 'string', notes: 'Rejected when weak (WEAK_PASSWORD).' },
      { name: 'telegram_user_id', type: 'int64', required: false, notes: 'Links the account to a Telegram identity at creation.' },
    ],
  },
  'POST /api/auth/exchange-bot-token': {
    summary:
      'Exchanges a single-use login code — minted by the bot for the Telegram user it already authenticated — for the same 7-day JWT.',
    note:
      'It deliberately does not accept a user id. A Telegram id is public, so minting a session for one would let any caller sign in as any account; sending user_id is accepted only so the field can be rejected.',
    statuses: ['200', '400', '401', '429'],
    fields: [
      { name: 'login_code', type: 'string', notes: 'Single-use, 10-minute code from the bot’s dashboard link.' },
    ],
    response: `{
  "success": true,
  "token": "<jwt>",
  "expires_at": 1790000000
}`,
  },
  'POST /api/auth/tma': {
    summary:
      'Verifies the HMAC-SHA256 signature of a Telegram Mini App initData payload against the bot token, then issues the same JWT for the verified numeric user id.',
    statuses: ['200', '400', '401', '429'],
    fields: [{ name: 'init_data', type: 'string', notes: 'The raw window.Telegram.WebApp.initData string.' }],
  },
  'GET /api/auth/me': {
    summary: 'The identity behind the presented token: user id, username and whether it carries the administrator flag.',
    statuses: auth,
    response: `{
  "user_id": 5094837833,
  "username": "operator",
  "is_admin": false
}`,
  },
  'GET /api/auth/bot-login-link': {
    summary:
      'Mints the caller’s own single-use dashboard login code and returns the relative link to open. Requires a valid Bearer token — the code is minted for the identity the token proves, never for an id in the request.',
    note:
      'A 60-second per-user cooldown applies: while a code is live and unused the endpoint answers 429 with Retry-After instead of minting another, so a double click cannot invalidate the link that was just handed out. Codes die after 10 minutes or one use.',
    statuses: ['200', '401', '429'],
    response: `{
  "code": "…",
  "expires_at": 1790000000,
  "link": "/dashboard/login?code=…"
}`,
  },
  'PATCH /api/auth/change-password': {
    summary: 'Changes the caller’s own password; the old password is required and verified before the new one is hashed.',
    statuses: ['200', '400', '401'],
    fields: [
      { name: 'old_password', type: 'string', notes: 'Verified before any change is stored.' },
      { name: 'new_password', type: 'string', notes: 'Rejected when weak.' },
    ],
  },

  // ------------------------------------------------- authenticated control plane
  'GET /api/rules': {
    summary: 'Lists the caller’s forwarding tasks, newest first, including their lists of sources and targets.',
    statuses: auth,
  },
  'POST /api/rules': {
    summary: 'Creates a forwarding task. The first source and the first target are mirrored into the legacy scalar columns by one write path.',
    statuses: ['201', '400', '401', '422'],
    fields: [
      { name: 'name', type: 'string', notes: 'Human label shown in every client.' },
      { name: 'source_chat_ids', type: 'string[]', required: false, notes: 'M sources; the first entry becomes the primary source.' },
      { name: 'target_chat_ids', type: 'string[]', required: false, notes: 'N targets; the first entry becomes the primary target.' },
      { name: 'mode', type: 'string', required: false, notes: 'copy or forward — the delivery mode applied to every target.' },
      { name: 'settings', type: 'object', required: false, notes: 'Per-task cleaner, filters, header/footer, AI and schedule settings.' },
    ],
    request: `{
  "name": "Desk feed → VIP + free tier",
  "source_chat_ids": ["@desk_feed", "-1001695513745"],
  "target_chat_ids": ["-1002000000001", "-1002000000002"],
  "mode": "copy",
  "settings": { "clean_links": true, "header_template": "📰 [[ORIGIN_NAME]]" }
}`,
  },
  'GET /api/rules/{id}': {
    summary: 'One task, ownership-checked before the read.',
    statuses: owner,
  },
  'PUT /api/rules/{id}': {
    summary: 'Full update of a task. A stale version is refused rather than silently overwriting a concurrent edit.',
    statuses: [...write, '409', '422'],
  },
  'PATCH /api/rules/{id}': {
    summary:
      'Partial update: the body carries only what changes and update_mask names it — the only way to set a value to false, zero or empty.',
    statuses: [...write, '409', '422'],
  },
  'DELETE /api/rules/{id}': {
    summary: 'Deletes a task and its policy. The delivery history stays queryable by the caller.',
    statuses: owner,
  },
  'POST /api/rules/{id}/toggle': {
    summary: 'Flips enabled state without touching any other field.',
    statuses: write,
  },
  'POST /api/rules/{id}/pause': {
    summary: 'Pauses delivery for one task; in-flight jobs are deferred rather than failed.',
    statuses: write,
  },
  'POST /api/rules/{id}/resume': {
    summary: 'Resumes a paused task and drains what was deferred.',
    statuses: write,
  },
  'POST /api/rules/{id}/stop': {
    summary: 'Stops the task’s poller. Unlike pause, nothing is queued for later.',
    statuses: write,
  },
  'POST /api/rules/{id}/restart': {
    summary: 'Restarts the poller after a stop, re-reading the task’s current settings.',
    statuses: write,
  },
  'POST /api/rules/{id}/duplicate': {
    summary: 'Clones a task — same policy, same lists — as a new row owned by the caller.',
    statuses: ['201', '401', '404'],
  },
  'POST /api/rules/{id}/route-path': {
    summary: 'Sets the source→target path in one call, keeping the legacy mirror consistent.',
    statuses: write,
  },
  'GET /api/rules/settings/schema': {
    summary:
      'The JSON schema of the per-task settings surface. It needs no task, so an editor can be built before the user owns anything.',
    statuses: auth,
  },
  'GET /api/rules/{id}/settings': {
    summary: 'Stored settings, the schema that describes them, and the effective values with the provenance of each one.',
    note: 'Effective values are what the forwarder will actually use, so a client that echoes its own settings back cannot pin a value the task merely inherits from its bot.',
    statuses: owner,
    response: `{
  "success": true,
  "settings": { "clean_links": true },
  "schema": { "type": "object" },
  "effective": { "clean_links": { "value": true, "source": "rule" } }
}`,
  },
  'PUT /api/rules/{id}/settings': {
    summary: 'Replaces the task’s stored settings after schema validation.',
    statuses: write,
  },
  'PATCH /api/rules/{id}/settings': {
    summary: 'Applies only the named settings; everything else keeps its current value.',
    statuses: write,
  },
  'GET /api/rules/{id}/logs': {
    summary: 'One task’s complete delivery history, ownership-checked before the read.',
    statuses: owner,
  },
  'GET /api/filters': {
    summary: 'Lists the caller’s saved keyword filters (blacklist, whitelist, link and media gates).',
    statuses: auth,
  },
  'POST /api/filters': {
    summary: 'Creates a reusable filter that tasks can reference instead of repeating keyword lists.',
    statuses: ['201', '400', '401'],
  },
  'GET /api/filters/{id}': {
    summary: 'Reads one saved filter. A filter the caller does not own is a 404, so the route never confirms somebody else’s filter exists.',
    statuses: owner,
  },
  'PUT /api/filters/{id}': {
    summary: 'Replaces a filter definition.',
    statuses: write,
  },
  'POST /api/filters/{id}/toggle': {
    summary:
      'Flips only the filter’s enable/disable switch: the server turns the value it currently stores, so the lists never travel with the request and a stale client cannot overwrite a write another device made. A disabled filter is skipped (allowed), never deleted — re-enabling restores its exact lists.',
    statuses: ['200', '401', '404'],
  },
  'DELETE /api/filters/{id}': {
    summary: 'Deletes a filter; tasks that referenced it keep their own inline rules.',
    statuses: owner,
  },
  'POST /api/filters/test': {
    summary: 'Previews one of the caller’s filters against a sample message: the Core’s canonical matcher decides whether the text would pass. Read-only — nothing is saved and nothing is forwarded.',
    statuses: ['200', '400', '401', '404'],
  },
  'GET /api/sessions': {
    summary: 'Lists the caller’s Telegram sessions with their account, status and last-seen time.',
    statuses: auth,
  },
  'GET /api/sessions/{sessionId}/chats': {
    summary:
      'The chat list for the source and target pickers, scoped entirely from the token identity.',
    note:
      'A session that exists but has no live Telegram client answers 503 session_not_connected — a different fact from a session that is not yours, which is a 404.',
    statuses: ['200', '401', '404', '503'],
  },
  'POST /api/sessions/{id}/backup': {
    summary: 'Starts a session backup as a durable async task (kind session.backup) and returns the task row.',
    statuses: write,
  },
  'DELETE /api/sessions/{id}': {
    summary: 'Terminates a session and drops its live client. The stored session file is removed.',
    statuses: owner,
  },
  'GET /api/stats': {
    summary: 'Aggregate counters for the caller’s tasks: delivered, failed, queued and dead-lettered.',
    statuses: auth,
  },
  'GET /api/queue': {
    summary: 'Queue depth and worker state. A healthy queue reports zeroes; it never reports a backlog it does not have.',
    statuses: auth,
  },
  'GET /api/dlq': {
    summary: 'The caller’s dead-letter entries with their failure reason and original target.',
    statuses: auth,
  },
  'POST /api/dlq/{id}/retry': {
    summary: 'Replays one dead-lettered delivery. A refused replay still returns its reason on the wire instead of an empty body.',
    statuses: write,
  },
  'DELETE /api/dlq': {
    summary: 'Purges the caller’s dead-letter queue. Ownership is applied before the delete, not after.',
    statuses: auth,
  },
  'GET /api/diagnostic-events': {
    summary:
      'The persisted failure-intelligence events for the caller — every delivery attempt with its raw Telegram error (RPC code, id, retry-after) next to the normalized category, reason and origin.',
    statuses: auth,
  },
  'GET /api/diagnostic-events/timeline': {
    summary:
      'One source message’s whole processing history, oldest first: every strategy attempted, every Telegram answer, every retry and fallback — the original failure preserved even when a later fallback succeeded.',
    statuses: auth,
  },
  'GET /api/diagnostic-events/facets': {
    summary:
      'Event counts grouped by one whitelisted dimension (category, stage, severity, reason, strategy, origin) — the error-rate views behind the monitoring dashboard.',
    statuses: auth,
  },
  'POST /api/simulate': {
    summary:
      'Dry-runs a task against a sample message and returns the text that would be delivered — the cleaner, filters, replacements, header and footer applied in the real order.',
    statuses: ['200', '400', '401', '404'],
    fields: [
      { name: 'rule_id', type: 'string', notes: 'The task whose policy is applied.' },
      { name: 'text', type: 'string', notes: 'Sample message body.' },
    ],
  },
  'GET /api/logs': {
    summary: 'Bounded, filterable query over the caller’s log history (level, task, time window, limit).',
    note: 'Reads page with limit/since rather than returning unbounded history; the collection policy decides which levels are stored at all.',
    statuses: auth,
  },
  'GET /api/logs/stats': {
    summary:
      'Counts per level and per task over a window — the numbers behind the log explorer. Also carries `admission`: the levels the logger engine itself collects and refuses, and how many events it filtered, so "collected by a producer" can be told apart from "stored by the engine".',
    statuses: auth,
  },
  'GET /api/logs/policy': {
    summary:
      'The collection policy each producer applies (scope=api, scope=core) next to the engine\'s own admission gate (scope=engine). A level the producers admit but the engine omits is counted as filtered, not stored — the two halves of the contract in one answer.',
    statuses: auth,
  },
  'PUT /api/logs/policy': {
    summary:
      'Changes what the core collects, through the process that enforces it. Administrator-only and audited, because this is the one control that decides what the evidence trail keeps.',
    statuses: ['200', '400', '401', '403'],
  },
  'GET /api/logs/health': {
    summary: 'Whether the logger answers, and whether the policy in force matches the one configured.',
    statuses: auth,
  },
  'GET /api/logs/export': {
    summary: 'A downloadable copy of the filtered history as JSON or CSV, so evidence can leave the box without screen-scraping.',
    statuses: auth,
  },
  'POST /api/logs/purge': {
    summary:
      'Deletes the caller’s stored log history inside the requested window. A tenant must name one of their own tasks: the archive is one shared store with no tenant column on this path, so the installation-wide purge is administrator-only and answers 403 otherwise.',
    statuses: ['200', '400', '401', '403', '404'],
  },
  'POST /api/logs/diagnostics/start': {
    summary: 'Starts a bounded diagnostic capture for support. Administrator-only.',
    statuses: ['200', '401', '403'],
  },
  'POST /api/logs/diagnostics/stop': {
    summary: 'Stops the diagnostic capture and returns what it collected. Administrator-only.',
    statuses: ['200', '401', '403'],
  },
  'GET /api/audit/events': {
    summary: 'Tenant-scoped audit events: who changed what, when, and under which correlation id.',
    statuses: auth,
  },
  'GET /api/diagnostics': {
    summary:
      'Preflight findings for the caller’s own rules, sessions and AI configs — the problems that would make a task fail, reported before it runs.',
    statuses: auth,
  },
  'GET /api/prompts': {
    summary: 'The caller’s AI rewrite prompt templates.',
    statuses: auth,
  },
  'POST /api/prompts': {
    summary: 'Creates a prompt template; new versions are stored rather than overwriting history.',
    statuses: ['201', '400', '401'],
  },
  'PUT /api/prompts/{id}': {
    summary: 'Replaces a prompt template’s body and name.',
    statuses: write,
  },
  'DELETE /api/prompts/{id}': {
    summary: 'Deletes a prompt template. Tasks that referenced it fall back to the default prompt.',
    statuses: owner,
  },
  'POST /api/prompts/test': {
    summary: 'Runs a prompt against sample text through the tenant’s AI provider and returns the model output.',
    statuses: ['200', '400', '401', '502'],
  },
  'GET /api/ai-configs': {
    summary: 'The caller’s AI providers. The API key is write-only: it is returned masked or omitted, never in clear text.',
    statuses: auth,
  },
  'POST /api/ai-configs': {
    summary: 'Stores a provider configuration. The base URL is validated by the endpoint guard before it is accepted.',
    statuses: ['201', '400', '401'],
  },
  'PUT /api/ai-configs/{id}': {
    summary: 'Updates a provider configuration; sending a new key replaces the stored one.',
    statuses: write,
  },
  'DELETE /api/ai-configs/{id}': {
    summary: 'Deletes a provider configuration and its stored key.',
    statuses: owner,
  },
  'GET /api/ai-configs/providers': {
    summary: 'The provider catalogue: known endpoints, default models and which fields each one needs.',
    statuses: auth,
  },
  'POST /api/ai-configs/check-key': {
    summary:
      'Health-probes a provider with the key that was typed, before it is stored, and reports whether the model actually answered.',
    note: 'The endpoint guard refuses loopback, link-local and metadata addresses unless the operator has explicitly allowed private AI peers.',
    statuses: ['200', '400', '401'],
  },
  'GET /api/auto-reply/settings': {
    summary:
      'The caller’s auto-reply configuration: master switch, target mode, tone, language, response length, context depth, cooldown, daily cap and quiet hours.',
    statuses: auth,
  },
  'PUT /api/auto-reply/settings': {
    summary:
      'Replaces the configuration. The owner is taken from the verified token, never from the body.',
    note: 'Tone, length and target mode are validated against their enumerations; an unknown value is refused rather than stored as a default.',
    statuses: write,
  },
  'GET /api/auto-reply/prompts': {
    summary: 'The caller’s prompt library, including the built-in default the settings fall back to.',
    statuses: auth,
  },
  'POST /api/auto-reply/prompts': {
    summary: 'Creates a personal prompt template.',
    note: 'A prompt owned by another tenant is never updated: the id is re-issued for the caller instead.',
    statuses: ['201', '400', '401'],
  },
  'GET /api/auto-reply/prompts/export': {
    summary: 'The prompt library as a portable bundle, for import on another installation.',
    statuses: auth,
  },
  'POST /api/auto-reply/prompts/import': {
    summary: 'Imports a previously exported bundle into the caller’s library.',
    note: 'Ids owned by someone else are re-issued, so an import can never adopt or overwrite another tenant’s prompt.',
    statuses: ['200', '400', '401'],
  },
  'PUT /api/auto-reply/prompts/{id}': {
    summary: 'Updates one of the caller’s prompt templates.',
    statuses: write,
  },
  'DELETE /api/auto-reply/prompts/{id}': {
    summary: 'Deletes one of the caller’s prompt templates.',
    note: 'Another tenant’s id answers 404, not 403: the refusal does not confirm that the id exists.',
    statuses: owner,
  },
  'POST /api/auto-reply/prompts/{id}/duplicate': {
    summary: 'Copies one of the caller’s prompts into a new one, keeping the body and name suffix.',
    statuses: ['201', '401', '404'],
  },
  'GET /api/auto-reply/targets': {
    summary: 'The caller’s contact targets, each with its own enabled flag.',
    statuses: auth,
  },
  'POST /api/auto-reply/targets': {
    summary: 'Adds contact targets in bulk; a chat that already exists is updated in place.',
    statuses: ['201', '400', '401'],
  },
  'DELETE /api/auto-reply/targets': {
    summary: 'Removes contact targets in bulk.',
    statuses: ['200', '400', '401'],
  },
  'POST /api/auto-reply/targets/resolve': {
    summary:
      'Resolves chat ids to titles and types through the caller’s live sessions, for the contact picker.',
    note: 'A session id owned by someone else is refused as not found; the ids that could not be resolved are returned in unresolved.',
    statuses: ['200', '400', '401'],
  },
  'PUT /api/auto-reply/targets/{chatId}': {
    summary: 'Updates one target, including its per-chat enabled flag.',
    statuses: write,
  },
  'GET /api/auto-reply/rules': {
    summary: 'The caller’s rules in the order the engine applies them: highest priority first.',
    statuses: auth,
  },
  'POST /api/auto-reply/rules': {
    summary: 'Creates or updates a rule with conditions, exclusions, a cooldown and an action.',
    note: 'Regex conditions are compiled and length-capped when they are stored, so an invalid pattern fails here instead of on every inbound message.',
    statuses: ['200', '400', '401'],
  },
  'POST /api/auto-reply/rules/reorder': {
    summary: 'Persists an explicit priority order for the caller’s rules.',
    statuses: ['200', '400', '401'],
  },
  'PUT /api/auto-reply/rules/{id}': {
    summary: 'Updates one of the caller’s rules.',
    statuses: write,
  },
  'DELETE /api/auto-reply/rules/{id}': {
    summary: 'Deletes one of the caller’s rules.',
    note: 'Replies already queued against that rule are cancelled rather than sent without it.',
    statuses: owner,
  },
  'GET /api/auto-reply/jobs': {
    summary: 'A page of the caller’s reply jobs, filterable by status and chat.',
    note: 'Every job row carries the correlation id used by the structured logs, so a delivered reply can be traced back to the message that caused it.',
    statuses: auth,
  },
  'POST /api/auto-reply/jobs/{id}/cancel': {
    summary: 'Cancels one of the caller’s queued or retrying jobs.',
    statuses: owner,
  },
  'POST /api/auto-reply/jobs/{id}/retry': {
    summary: 'Requeues one of the caller’s dead-lettered jobs for another attempt.',
    statuses: owner,
  },
  'GET /api/auto-reply/stats': {
    summary:
      'Counters for the caller: replies sent, failures in the last day, queue depth by status, enabled chats and mean send latency.',
    statuses: auth,
  },
  'POST /api/auto-reply/preview': {
    summary:
      'Runs the configured provider once for a sample message and returns the reply without sending it.',
    note: 'The reply is generated by the same prompt assembly the worker uses, so the preview cannot disagree with what would be sent.',
    statuses: ['200', '400', '401'],
  },
  'GET /api/pv-responder': {
    summary: 'The private-chat auto-responder configuration: persona, typing delay, cooldown, history depth.',
    statuses: auth,
  },
  'POST /api/pv-responder': {
    summary: 'Creates or replaces the auto-responder configuration.',
    statuses: write,
  },
  'PUT /api/pv-responder': {
    summary: 'Same write path as POST, kept for clients that PUT.',
    statuses: write,
  },
  'GET /api/bot-tokens': {
    summary: 'The bots this tenant has registered, each with the tasks that inherit from it.',
    statuses: auth,
  },
  'POST /api/bot-tokens': {
    summary: 'Registers a bot token. The token is validated with Telegram before it is stored.',
    statuses: ['201', '400', '401'],
  },
  'DELETE /api/bot-tokens/{id}': {
    summary: 'Removes a bot registration; tasks attached to it must be re-pointed.',
    statuses: owner,
  },
  'GET /api/bot-tokens/{id}/defaults': {
    summary: 'The defaults every task attached to this bot inherits, with where each value comes from.',
    statuses: owner,
  },
  'PUT /api/bot-tokens/{id}/defaults': {
    summary: 'Sets the inherited defaults for every task attached to this bot.',
    statuses: write,
  },
  'GET /api/client/settings': {
    summary: 'The caller’s client-side preferences (theme, language, notification choices).',
    statuses: auth,
  },
  'POST /api/client/credentials': {
    summary: 'Stores the caller’s personal client credentials. Write-only: they are never read back.',
    statuses: ['200', '400', '401'],
  },
  'POST /api/backup/export': {
    summary: 'Starts a whole-installation export as a durable async task (kind backup.export).',
    statuses: ['201', '202', '400', '401'],
  },
  'POST /api/backup/import': {
    summary: 'Imports a backup bundle into the caller’s tenant; encrypted bundles are decrypted with the operator key.',
    statuses: ['200', '400', '401', '422'],
  },
  'GET /api/tasks': {
    summary:
      'Lists the caller’s durable async jobs, filterable by status and kind, with a bounded page (limit 1–200, default 50).',
    statuses: auth,
    fields: [
      { name: 'status', type: 'string', required: false, notes: 'PENDING · RUNNING · RETRY_WAIT · SUCCEEDED · FAILED · CANCELLED' },
      { name: 'kind', type: 'string', required: false, notes: 'session.backup · dlq.replay · dlq.purge · backup.export' },
      { name: 'limit', type: 'int', required: false, notes: '1–200, default 50.' },
      { name: 'offset', type: 'int', required: false, notes: 'Default 0.' },
    ],
    response: `{
  "tasks": [
    {
      "id": "6f1c…",
      "owner_user_id": 5094837833,
      "kind": "dlq.replay",
      "status": "RUNNING",
      "attempt": 1,
      "max_attempts": 3,
      "created_at": 1790000000
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}`,
  },
  'POST /api/tasks': {
    summary:
      'Registers a durable, tenant-owned job. Resubmitting the same (owner, kind, idempotency_key) returns the original task with already_existed:true instead of a duplicate.',
    note:
      'The owner is taken from the verified token, never from the body. Payload and result are each capped at 64 KiB and must be JSON; max_attempts is clamped to 8.',
    statuses: ['201', '200', '400', '401'],
    fields: [
      { name: 'kind', type: 'string', notes: 'One of session.backup, dlq.replay, dlq.purge, backup.export.' },
      { name: 'payload', type: 'object', required: false, notes: 'JSON object; ≤ 64 KiB; stored canonically.' },
      { name: 'idempotency_key', type: 'string', required: false, notes: 'Client-chosen; makes retries safe.' },
      { name: 'max_attempts', type: 'int', required: false, notes: 'Default 3, clamped to 8.' },
    ],
    request: `{
  "kind": "dlq.replay",
  "payload": { "ids": ["a1"] },
  "idempotency_key": "replay-2026-10-19",
  "max_attempts": 3
}`,
    response: `{
  "task": {
    "id": "6f1c…",
    "owner_user_id": 5094837833,
    "kind": "dlq.replay",
    "status": "PENDING",
    "idempotency_key": "replay-2026-10-19",
    "attempt": 0,
    "max_attempts": 3
  },
  "already_existed": false,
  "id": "6f1c…",
  "status": "PENDING"
}`,
  },
  'GET /api/tasks/{id}': {
    summary: 'One job. A job that belongs to another tenant is reported as missing, exactly like a job that does not exist.',
    statuses: owner,
  },
  'POST /api/tasks/{id}/cancel': {
    summary: 'Requests cancellation. A terminal job refuses with not_cancellable; a running one with running_not_cancellable.',
    statuses: ['200', '400', '401', '404'],
  },
  'POST /api/tasks/{id}/retry': {
    summary: 'Re-queues a FAILED job with a fresh attempt budget. Any other state refuses with not_retryable.',
    statuses: ['200', '400', '401', '404'],
  },

  // --------------------------------------------------------------- administration
  'GET /api/admin/overview': {
    summary: 'Installation totals: accounts, tasks, deliveries, queue depth and service health in one response.',
    statuses: ['200', '401', '403'],
  },
  'GET /api/admin/users': {
    summary: 'Paginated account list with status and link state.',
    statuses: ['200', '401', '403'],
  },
  'POST /api/admin/users/{id}/status': {
    summary: 'Enables or disables one account. A disabled account’s tokens stop resolving on the next request.',
    statuses: ['200', '400', '401', '403', '404'],
  },
  'POST /api/admin/broadcast': {
    summary: 'Sends an operator announcement to every linked user through the bot.',
    statuses: ['200', '400', '401', '403'],
  },
  'GET /api/admin/settings': {
    summary: 'Installation settings currently in force.',
    statuses: ['200', '401', '403'],
  },
  'POST /api/admin/settings': {
    summary: 'Saves installation settings; secret values are write-only and never echoed.',
    statuses: ['200', '400', '401', '403'],
  },
  'GET /api/admin/admins': {
    summary: 'The administrator roster.',
    statuses: ['200', '401', '403'],
  },
  'POST /api/admin/admins': {
    summary: 'Grants the administrator flag to an existing account.',
    statuses: ['200', '201', '400', '401', '403'],
  },
  'DELETE /api/admin/admins/{id}': {
    summary: 'Revokes the administrator flag. The last remaining administrator cannot be removed.',
    statuses: ['200', '400', '401', '403', '404'],
  },
  'GET /api/admin/extensions': {
    summary: 'Installed extensions and whether each one is enabled.',
    statuses: ['200', '401', '403'],
  },
  'POST /api/admin/extensions/{id}/toggle': {
    summary: 'Enables or disables one extension.',
    statuses: ['200', '401', '403', '404'],
  },
  // ------------------------------------------------------------ replace rules
  'GET /api/replace-rules': {
    summary:
      'One page of the caller’s replace rules, with search, a status filter, a task filter and the counts the header shows.',
    note: 'A rule assigned to three tasks is one row here: the copies inside the tasks are grouped by rule id. The counts describe the filtered set, so “3 enabled” next to a task filter never counts a rule that task does not run.',
    statuses: auth,
    fields: [
      { name: 'q', type: 'string', required: false, notes: 'Matches a rule’s name, source text or replacement.' },
      { name: 'status', type: 'string', required: false, notes: 'all (default), enabled or disabled.' },
      { name: 'task_id', type: 'string', required: false, notes: 'Narrow to the rules one forwarding task runs.' },
      { name: 'page', type: 'integer', required: false, notes: '1-based; a page past the end is clamped, not an error.' },
      { name: 'per_page', type: 'integer', required: false, notes: '1–100, default 20.' },
    ],
    response: `{
  "rules": [
    {
      "id": "5f1c9a7d2e4b8c01", "name": "Promo prices", "source": "promo", "to": "OFFER",
      "mode": "literal", "flags": "", "whole_word": false, "enabled": true, "priority": 100,
      "task_ids": ["t1", "t2"], "task_names": ["Desk feed", "VIP"],
      "version": 3, "updated_at": 1792548120, "badge": "lit"
    }
  ],
  "total": 1, "page": 1, "pages": 1, "per_page": 20,
  "counts": { "total": 1, "enabled": 1, "disabled": 0 },
  "query": "", "status": "all", "task_id": "",
  "tasks": [{ "id": "t1", "name": "Desk feed" }]
}`,
  },
  'POST /api/replace-rules': {
    summary:
      'Creates a replace rule and assigns it to one or more forwarding tasks; one copy is stored in each named task.',
    note: 'task_ids is required — a rule nobody runs would never fire. The replacement is validated at write time, so an invalid regex is refused here rather than silently skipped at forward time.',
    statuses: ['201', '400', '401', '413', '503'],
    fields: [
      { name: 'name', type: 'string', required: false, notes: 'Human label; falls back to the source text in lists.' },
      { name: 'source', type: 'string', required: true, notes: 'The text (or pattern) to find.' },
      { name: 'to', type: 'string', required: false, notes: 'The replacement; empty deletes the match.' },
      { name: 'mode', type: 'string', required: false, notes: 'literal (default) or regex.' },
      { name: 'flags', type: 'string', required: false, notes: 'Subset of imsx; i is ignore case, w is whole word.' },
      { name: 'whole_word', type: 'boolean', required: false, notes: 'Shorthand for adding w to flags.' },
      { name: 'enabled', type: 'boolean', required: false, notes: 'Default true.' },
      { name: 'priority', type: 'integer', required: false, notes: '0–1000, default 100; higher runs first.' },
      { name: 'task_ids', type: 'string[]', required: true, notes: 'The tasks that should run the rule.' },
    ],
    request: `{
  "name": "Promo prices",
  "source": "promo\\s*(\\d+)",
  "to": "OFFER \\1",
  "mode": "regex",
  "flags": "i",
  "priority": 200,
  "task_ids": ["t1", "t2"]
}`,
  },
  'GET /api/replace-rules/tasks': {
    summary: 'The caller’s forwarding tasks, for the rule editor’s picker.',
    statuses: auth,
    response: `{ "tasks": [{ "id": "t1", "name": "Desk feed" }] }`,
  },
  'GET /api/replace-rules/{id}': {
    summary: 'One replace rule, with the tasks it is assigned to.',
    note: 'A rule that belongs to another tenant answers 404, exactly like one that does not exist — an id is never proof of ownership.',
    statuses: ['200', '401', '404'],
  },
  'PUT /api/replace-rules/{id}': {
    summary: 'Updates a rule’s fields and its task assignments.',
    note: 'task_ids replaces the assignment set: a task left out stops running the rule, and the copy inside it is removed rather than left orphaned.',
    statuses: ['200', '400', '401', '404', '413'],
  },
  'PATCH /api/replace-rules/{id}': {
    summary: 'Partial update: only the keys present in the body are written.',
    note: 'The distinction is load-bearing — a body of {"enabled": false} disables the rule, while the same body on a non-partial write would blank the pattern it is switching off.',
    statuses: ['200', '400', '401', '404', '413'],
  },
  'DELETE /api/replace-rules/{id}': {
    summary: 'Deletes a rule from every task it is assigned to, and reports how many copies were removed.',
    statuses: ['200', '401', '404'],
    response: `{ "ok": true, "removed": 2 }`,
  },
  'POST /api/replace-rules/{id}/enable': {
    summary: 'Sets an absolute enabled state on every copy of the rule.',
    note: 'Absolute rather than a toggle: a retried request (a double tap, a retry after a timeout) lands on the state the user asked for instead of flipping it back.',
    statuses: ['200', '400', '401', '404', '413'],
    fields: [{ name: 'enabled', type: 'boolean', required: true, notes: 'The state to store.' }],
    request: `{ "enabled": false }`,
  },
  'POST /api/replace-rules/{id}/duplicate': {
    summary: 'Copies a rule to a new id, keeping it assigned to the same tasks.',
    statuses: ['201', '401', '404'],
    fields: [{ name: 'name', type: 'string', required: false, notes: 'The copy’s label; empty keeps the original’s name.' }],
  },
  'POST /api/replace-rules/test': {
    summary:
      'Previews a rule against sample text using the same engine a forwarded message goes through, so the preview cannot promise an output the pipeline would not produce.',
    note: 'Pass rule_id to preview a stored rule, and any of source/to/mode/flags/whole_word to try an edit before saving it.',
    statuses: ['200', '400', '401', '413'],
    fields: [
      { name: 'rule_id', type: 'string', required: false, notes: 'The stored rule to use as the base.' },
      { name: 'sample', type: 'string', required: true, notes: 'The text to transform.' },
      { name: 'source', type: 'string', required: false, notes: 'Overrides the stored pattern.' },
      { name: 'to', type: 'string', required: false, notes: 'Overrides the stored replacement.' },
      { name: 'mode', type: 'string', required: false, notes: 'literal or regex.' },
      { name: 'flags', type: 'string', required: false, notes: 'Subset of imsx.' },
    ],
    response: `{ "ok": true, "input": "promo 50", "output": "OFFER 50", "matches": 1, "changed": true }`,
  },
  'GET /api/admin/ratelimit': {
    summary:
      'The limiter’s own control surface: effective class policies, whether the store is reachable, and whether requests are currently served by the in-process fallback.',
    statuses: ['200', '401', '403'],
    response: `{
  "enabled": true,
  "backend": "redis",
  "degraded": false,
  "classes": {
    "auth":  { "limit": 30,  "window_seconds": 60, "burst": 10, "scope": "ip",  "fail": "closed" },
    "read":  { "limit": 600, "window_seconds": 60, "burst": 120, "scope": "u",  "fail": "local" },
    "write": { "limit": 120, "window_seconds": 60, "burst": 30, "scope": "u",   "fail": "local" },
    "admin": { "limit": 300, "window_seconds": 60, "burst": 60, "scope": "u",   "fail": "local" }
  }
}`,
  },
};
