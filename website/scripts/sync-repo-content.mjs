import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '../../');
const websiteDir = path.resolve(__dirname, '../');

console.log('🔄 Running AutoTelegramForward Content Synchronization...');

// ---------------------------------------------------------------------------
// 1. Live facts read straight out of the checkout (never hand-maintained)
// ---------------------------------------------------------------------------
const read = (rel) => fs.readFileSync(path.join(rootDir, rel), 'utf8');

let version = '2.0.0';
try {
  const raw = read('VERSION').trim();
  if (raw) version = raw;
} catch (e) {
  console.warn('Could not read VERSION file:', e.message);
}

let gitCommit = 'HEAD';
let gitDate = new Date().toISOString();
try {
  gitCommit = execSync('git rev-parse --short HEAD', { cwd: rootDir }).toString().trim();
  gitDate = execSync('git log -1 --format=%cI', { cwd: rootDir }).toString().trim();
} catch (e) {
  console.warn('Git inspection skipped:', e.message);
}

// Locale catalog: 10 languages, 1:1 key parity is a hard CI gate.
let locales = ['en', 'fa', 'ru', 'zh', 'pt', 'uz', 'id', 'es', 'fr', 'it'];
let i18nKeyCount = 0;
try {
  const catalog = JSON.parse(read('core/infrastructure/telegram/locales/translations.json'));
  locales = Object.keys(catalog);
  i18nKeyCount = Object.keys(catalog.en || {}).length;
} catch (e) {
  console.warn('Locale catalog read skipped:', e.message);
}

// Counts that back the headline numbers, derived from the tree itself.
const count = (rel, test) => {
  let total = 0;
  const walk = (dir) => {
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (['node_modules', '.venv', '__pycache__', '.git', 'build', 'dist', 'ephemeral'].includes(entry.name)) continue;
        walk(full);
      } else if (test(entry.name)) {
        total++;
      }
    }
  };
  walk(path.join(rootDir, rel));
  return total;
};

const pythonTests = count('core/tests', (n) => n.startsWith('test_') && n.endsWith('.py'));
const goTests = count('api', (n) => n.endsWith('_test.go'));

let protoRpcCount = 0;
try {
  protoRpcCount = (read('proto/autoforward.proto').match(/^\s*rpc\s+/gm) || []).length;
} catch (e) {
  console.warn('proto inspection skipped:', e.message);
}

let featureCatalogCount = 0;
let featureCatalogNames = [];
// The catalog is the project's own statement of what is finished, partial and
// still planned. The status page renders it verbatim rather than paraphrasing
// it, so the site can never claim more than the repository does.
let featureCatalogDetail = [];
let featureCatalogStatus = { implemented: 0, partial: 0, planned: 0 };
const STATUS_BUCKET = {
  IMPLEMENTED: 'implemented',
  PARTIALLY_IMPLEMENTED: 'partial',
  NOT_IMPLEMENTED: 'planned',
};
try {
  const catalog = JSON.parse(read('docs/feature_catalog.json'));
  featureCatalogCount = catalog.features.length;
  featureCatalogNames = catalog.features.map((f) => f.name);
  featureCatalogDetail = catalog.features.map((f) => ({
    id: f.id,
    name: f.name,
    status: f.implementation_status || 'IMPLEMENTED',
    bucket: STATUS_BUCKET[f.implementation_status] || 'implemented',
    clients: f.clients || [],
    dbTables: f.db_tables || [],
    completionCondition: f.completion_condition || '',
    entryPoints: f.entry_points || [],
  }));
  for (const entry of featureCatalogDetail) {
    featureCatalogStatus[entry.bucket] += 1;
  }
} catch (e) {
  console.warn('feature catalog read skipped:', e.message);
}

let cliGroupCount = 0;
try {
  cliGroupCount = fs
    .readdirSync(path.join(rootDir, 'atfcli/commands'))
    .filter((n) => n.endsWith('.py') && !n.startsWith('_')).length;
} catch (e) {
  console.warn('CLI inspection skipped:', e.message);
}

// ---------------------------------------------------------------------------
// 2. Message classes the engine handles (Telegram layer)
// ---------------------------------------------------------------------------
const supportedMediaTypes = [
  { type: 'Text Messages', status: 'Full Support', desc: 'Formatting entities, emojis, links and styling preserved through the cleaner' },
  { type: 'Photos & Images', status: 'Full Support', desc: 'Direct forward, clean copy without the forward tag, or download-and-reupload' },
  { type: 'Photo / Video Albums', status: 'Full Support', desc: 'Media groups aggregated into one atomic job and delivered together' },
  { type: 'Videos & MP4', status: 'Full Support', desc: 'Streamed upload with caption, thumbnail and quality preserved' },
  { type: 'Documents & PDFs', status: 'Full Support', desc: 'Arbitrary documents and archives up to the Telegram size limit' },
  { type: 'Audio & Music', status: 'Full Support', desc: 'MP3/M4A tracks with artist, title and cover metadata' },
  { type: 'Voice Messages', status: 'Full Support', desc: 'OGG Opus voice notes with waveform and duration' },
  { type: 'Video Notes', status: 'Full Support', desc: 'Round video messages forwarded as native video notes' },
  { type: 'Stickers & Animations', status: 'Full Support', desc: 'Static WEBP, animated TGS and video WEBM stickers' },
  { type: 'Polls & Quizzes', status: 'Full Support', desc: 'Standard polls, multiple answers and quiz mode, results preserved' },
  { type: 'Locations & Contacts', status: 'Full Support', desc: 'Geolocations, venues and vCard contacts' },
  { type: 'Protected Content', status: 'Bypass Supported', desc: 'ChatForwardsRestricted channels re-uploaded through the media path' },
];

// ---------------------------------------------------------------------------
// 3. Verified capability groups (source of truth: core/, api/, web/, mobile/)
// ---------------------------------------------------------------------------
const verifiedFeatures = [
  {
    category: 'Routing & Task Model',
    items: [
      'One task = M source chats → N target chats (lists, not single values)',
      'Per-task content rules stored as metadata["content_policy"] — no schema migration needed',
      'Per-source and per-target policy overrides that can only add constraints, never relax them',
      'Primary source/target mirror always kept in step by the sanctioned set_targets() write path',
      'Copy mode (clean, no forward tag) vs Direct forward (native Telegram quote header)',
      'Forum topic targeting via message_thread_id, per target',
      'Priority ordering, execution order and per-task pause/resume',
    ],
  },
  {
    category: 'Content Policy Engine',
    items: [
      'Keyword blacklist and whitelist with any / all / phrase matching modes',
      'Word-boundary matching (cat never matches concatenate) with Unicode folding',
      'Persian/Arabic normalisation so a key typed on one keyboard catches text from another',
      'Link blocking and link stripping, plus ReDoS-refused regex_block / regex_require patterns',
      'Ordered find → replace pairs (literal or re: regex) applied in a single deterministic pass',
      'Length gates and media allow/deny gates evaluated cheapest-first',
    ],
  },
  {
    category: 'Transformation & Templates',
    items: [
      '12 validated shortcodes for headers and footers, interpolated per delivery',
      'Dynamic attribution hyperlinks for public and private source channels',
      'Emoji, extra-blank-line and inline-keyboard-button cleaning',
      'UTF-16 entity offset preservation so bold and italic spans survive cleaning',
    ],
  },
  {
    category: 'AI Rewriting',
    items: [
      'Per-task enable/disable toggle that never blocks delivery when the model is unreachable',
      'OpenAI, Anthropic, Gemini, Groq, DeepSeek and any OpenAI-compatible base URL',
      'Built-in prompt presets: Professional Clean, Persian↔English, Executive Summarizer, Format Enhancer',
      'Custom prompt templates with version history, validation and preview/test endpoints',
      'Temperature, model, target language and system/user prompt all set per configuration',
    ],
  },
  {
    category: 'Observability & Control',
    items: [
      'Central Logger microservice with queryable history (service, level, search, since, limit)',
      'Log explorer in web and mobile: live stream, filters, aggregate stats and tenant-scoped purge',
      'Attributable history — every entry carries its service, owner and correlation id',
      'Dead-letter queue inspect, replay and purge, tenant-scoped',
      'OUTCOME_UNCONFIRMED is a first-class outcome — a Telegram timeout is never silent',
      'Field-aware [REDACTED] filtering on every log line and error trace',
      'Live service topology probing with latency, version and per-service reachability',
    ],
  },
  {
    category: 'Delivery Guarantees',
    items: [
      'Durable queue with lease-based workers, exponential backoff and jitter',
      'Ordered delivery lanes — a burst is delivered in the order it was posted',
      'Atomic channel cursor persistence: the poller no longer races the connection',
      'SHA-256 content and album fingerprint deduplication',
      'Reply synchronisation and edit/delete synchronisation with a max_time_edit guard',
      'FloodWait detection with automatic pause and resume of the affected account',
      'Auto-disable of a task when the account loses write access (CHAT_WRITE_FORBIDDEN)',
    ],
  },
  {
    category: 'Clients & Interfaces',
    items: [
      'Telegram Bot UI with button-driven panels, submenus and FSM wizards',
      'React dashboard served by the Rust Axum gateway with embedded assets',
      'Flutter Android/iOS client exposing the same system surface as the dashboard',
      'Contract gate: shared fixtures keep web and mobile from drifting apart',
      'Telegram Mini App authentication alongside Bearer JWT for the REST API',
    ],
  },
  {
    category: 'DevOps & Operations',
    items: [
      `${cliGroupCount} CLI command groups covering setup, rules, logs, docker, k8s, certs and data`,
      'atf validate — read-only checks usable as a CI pre-deploy gate (--strict, --runtime, --deep)',
      'atf doctor and atf system upgrade: git pull → build → recreate → alias repair → health gate',
      'Boot persistence via systemd / launchd / Task Scheduler (atf system service install)',
      'Portable single-file CLI: atf release portable → dist/atf.pyz (standard library only)',
      'Container environment contract check that detects a container older than .env',
    ],
  },
  {
    category: 'Security & Multi-Tenancy',
    items: [
      'Fail-closed tenant isolation on every read and write, keyed on immutable numeric user ids',
      'Zero cross-tenant visibility across Bot, Web, Mobile, REST and gRPC',
      'AES-256-GCM encryption at rest for sessions, bot tokens, AI keys and backups',
      'Encrypted .atf export/import bundles for clone and migration',
      'Private gRPC network: core and logger bind to internal interfaces only',
    ],
  },
  {
    category: 'Localization',
    items: [
      `${locales.length} languages with mandatory 1:1 catalog parity (${locales.join(', ')})`,
      'Per-user language resolution with strict English fallback — never Persian',
      'RTL layout for Persian and Arabic-script content in web, mobile and bot UI',
      'Automated cross-language audits wired into the CLI validator and CI',
    ],
  },
];

// ---------------------------------------------------------------------------
// 4. Shortcodes (core/domain/services.py)
// ---------------------------------------------------------------------------
const supportedShortcodes = [
  { code: '[[ORIGIN_TEXT]]', desc: 'Message body after cleaner and policy passes', example: 'Breaking market report...' },
  { code: '[[ORIGIN_NAME]]', desc: 'Title of the source channel or supergroup', example: 'Crypto News Daily' },
  { code: '[[ORIGIN_USERNAME]]', desc: 'Public @handle of the source channel', example: 'cryptonewsdaily' },
  { code: '[[ORIGIN_USERID]]', desc: 'Numerical Telegram id of the author / sender', example: '10842918' },
  { code: '[[ORIGIN_POST_ID]]', desc: 'Original post id inside the source channel', example: '4921' },
  { code: '[[ORIGIN_CHAT_ID]]', desc: 'Numerical id of the source channel', example: '-1001695513745' },
  { code: '[[ORIGIN_POST_DATE]]', desc: 'Original publication timestamp (UTC)', example: '2026-10-16 14:00 UTC' },
  { code: '[[ORIGIN_NAME_URL]]', desc: 'Markdown hyperlink back to the original post', example: '[Crypto News](https://t.me/cryptonewsdaily/4921)' },
  { code: '[[SOURCE_NAME]]', desc: 'Alias for the source channel name', example: 'Crypto News Daily' },
  { code: '[[SENDER_CHAT]]', desc: 'Chat id or title the post was sent in', example: '-1001695513745' },
  { code: '[[FORWARD_FROM_CHAT]]', desc: 'Original channel when the incoming post was itself a forward', example: 'Reuters Telegram' },
  { code: '[[FROM_USER]]', desc: 'Display name of the original author', example: 'John Doe' },
];

// ---------------------------------------------------------------------------
// 5. Capability matrix across the four clients
// ---------------------------------------------------------------------------
const capabilityMatrix = [
  { feature: 'Channel / group / PV forwarding', bot: true, web: true, mobile: true, api: true },
  { feature: 'Multi-source → multi-target tasks', bot: true, web: true, mobile: true, api: true },
  { feature: 'Interactive login (phone / OTP / 2FA)', bot: true, web: true, mobile: true, api: true },
  { feature: 'Content policy (cleaner, filters, regex)', bot: true, web: true, mobile: true, api: true },
  { feature: 'Per-source / per-target policy overrides', bot: true, web: true, mobile: true, api: true },
  { feature: 'AI rewriting + prompt templates', bot: true, web: true, mobile: true, api: true },
  { feature: 'Deduplication, reply & edit sync', bot: true, web: true, mobile: true, api: true },
  { feature: 'Delays, scheduling & auto-delete', bot: true, web: true, mobile: true, api: true },
  { feature: `Localization (${locales.length} languages)`, bot: true, web: true, mobile: true, api: true },
  { feature: 'Log explorer, stats & DLQ control', bot: true, web: true, mobile: true, api: true },
  { feature: 'Live topology & health probing', bot: true, web: true, mobile: true, api: true },
  { feature: 'Encrypted backup & migration (.atf)', bot: true, web: true, mobile: true, api: true },
];

// ---------------------------------------------------------------------------
// 6. CLI surface (atfcli/commands — the groups the binary actually exposes)
// ---------------------------------------------------------------------------
const cliGroups = [
  { group: 'setup', what: 'Guided wizard: detect the platform, validate every answer, build, then prove each service is serving', examples: ['atf setup', 'atf setup --check', 'atf setup --yes', 'atf setup --answers answers.json'] },
  { group: 'status', what: 'Live service board with ports, reachability, task counts and the dashboard URL', examples: ['atf status', 'atf status --watch'] },
  { group: 'system', what: 'Stack lifecycle plus boot persistence and the full upgrade path', examples: ['atf system services', 'atf system upgrade', 'atf system service install'] },
  { group: 'validate', what: 'Read-only checks: layout, env, compose, catalog parity, secrets scan, database, proto, ports', examples: ['atf validate', 'atf validate --runtime', 'atf validate --strict --json', 'atf validate --deep'] },
  { group: 'doctor', what: 'Deep diagnosis across DB, binaries, ports and gRPC, with --fix to repair', examples: ['atf doctor', 'atf doctor --fix'] },
  { group: 'rules', what: 'Task control: list, show, create, update, clone, enable/disable, pause/resume, sources, path, simulate, settings', examples: ['atf rules list --json', 'atf rules sources <id> add <chat>', 'atf rules simulate <id> --text "hello"', 'atf rules settings set <id> clean_buttons=true'] },
  { group: 'logs', what: 'Log archive access from the terminal: tail, query, search, stats, export, purge', examples: ['atf logs tail --follow -n 50', 'atf logs query --level ERROR', 'atf logs stats'] },
  { group: 'data', what: 'Queue and DLQ inspection, aggregate stats, encrypted backup export / import', examples: ['atf data queue', 'atf data dlq --replay', 'atf data backup-export --out atf.atf'] },
  { group: 'sessions', what: 'Telegram session inventory, encrypted backup and terminate', examples: ['atf sessions list', 'atf sessions backup --out session.bak'] },
  { group: 'auth', what: 'Dashboard / API authentication for the CLI itself', examples: ['atf auth token --save', 'atf auth whoami'] },
  { group: 'admin', what: 'Administration: users, admins, settings, extensions, broadcast, overview', examples: ['atf admin overview', 'atf admin users list', 'atf admin broadcast --text "maintenance"'] },
  { group: 'config', what: 'Read and write configuration values with validation', examples: ['atf config list', 'atf config set ATF_BOT_TOKEN'] },
  { group: 'db', what: 'Database inspection, integrity status, snapshot and backup', examples: ['atf db status', 'atf db snapshot'] },
  { group: 'docker', what: 'Compose lifecycle for the four containers', examples: ['atf docker up', 'atf docker ps', 'atf docker logs core'] },
  { group: 'deploy', what: 'Build, recreate and verify in one gated command', examples: ['atf deploy --build'] },
  { group: 'certs', what: 'TLS certificate inspection, expiry checks and renewal', examples: ['atf certs check', 'atf certs renew'] },
  { group: 'net', what: 'Network inspection and the AI bridge reconnect path', examples: ['atf net inspect', 'atf net reconnect'] },
  { group: 'platform', what: 'Host detection plus the exact docker install commands for this distribution', examples: ['atf platform', 'atf platform --json'] },
  { group: 'release', what: 'Version, manifest, verification and the portable single-file build', examples: ['atf release version', 'atf release verify', 'atf release portable'] },
  { group: 'ci', what: 'Pipeline status, logs and live watch for GitHub Actions runs', examples: ['atf ci status', 'atf ci watch'] },
  { group: 'i18n', what: 'Cross-language catalog audits: parity, placeholders and RTL leakage', examples: ['atf i18n'] },
  { group: 'proto', what: 'Regenerate and verify gRPC stubs against the .proto sources', examples: ['atf proto'] },
  { group: 'test', what: 'Run the test suites: python, go, flutter or all', examples: ['atf test py', 'atf test all'] },
  { group: 'watch', what: 'Continuous supervision of the running stack', examples: ['atf watch'] },
];

// ---------------------------------------------------------------------------
// 7. Public REST surface (Go control plane, :8080)
// ---------------------------------------------------------------------------
const apiGroups = [
  {
    group: 'System & readiness',
    endpoints: [
      ['GET', '/api/system/setup', 'Ready verdict, version, env contract and live service probes (booleans only)'],
      ['GET', '/api/system/setup?probe=0', 'The same verdict without probing the services'],
      ['GET', '/api/system/env', 'Configuration contract: every key with set:true/false and a fix hint'],
      ['GET', '/api/health/services', 'Per-service reachability, latency and detail (core / logger / api / web)'],
      ['GET', '/healthz', 'Liveness probe used by the gateway and the upgrade gate'],
    ],
  },
  {
    group: 'Tasks & rules',
    endpoints: [
      ['GET', '/api/rules', "List the caller's tasks (tenant-scoped)"],
      ['POST', '/api/rules', 'Create a task with a source list, a target list and a content policy'],
      ['GET', '/api/rules/{id}', 'Read one task including its resolved settings'],
      ['PUT', '/api/rules/{id}', 'Update a task (optimistic concurrency, version bumped)'],
      ['DELETE', '/api/rules/{id}', 'Delete a task and its schedules'],
      ['POST', '/api/rules/{id}/pause', 'Pause a task: queued jobs defer instead of failing'],
      ['POST', '/api/rules/{id}/resume', 'Resume a task and unblock retry-wait jobs'],
      ['POST', '/api/rules/{id}/toggle', 'Enable or disable a task'],
      ['POST', '/api/simulate', 'Dry-run a message through a task and see the decision'],
    ],
  },
  {
    group: 'Task lifecycle (async jobs)',
    endpoints: [
      ['GET', '/api/tasks', "List the caller's async jobs (state machine, tenant-scoped)"],
      ['POST', '/api/tasks', 'Submit an async job (session backup, DLQ replay/purge, backup export); idempotent via idempotency_key'],
      ['GET', '/api/tasks/{id}', 'Poll one job: state, attempt, timestamps, structured error details'],
      ['POST', '/api/tasks/{id}/cancel', 'Cancel a queued job before a worker claims it'],
      ['POST', '/api/tasks/{id}/retry', 'Re-queue a failed job (attempt budget reset)'],
    ],
  },
  {
    group: 'Sessions',
    endpoints: [
      ['GET', '/api/sessions', "List the caller's Telegram sessions"],
      ['GET', '/api/sessions/{id}/chats', 'List the chats a session can see (sources and targets)'],
      ['POST', '/api/sessions/{id}/backup', 'Export an encrypted session backup'],
      ['POST', '/api/backup/export', 'Export an encrypted backup of sessions and settings'],
      ['POST', '/api/backup/import', 'Restore from an encrypted backup'],
      ['DELETE', '/api/sessions/{id}', 'Terminate a session and purge its credentials'],
    ],
  },
  {
    group: 'Filters & AI',
    endpoints: [
      ['GET', '/api/filters', 'List filter rules'],
      ['POST', '/api/filters', 'Create a filter rule'],
      ['PUT', '/api/filters/{id}', 'Update a filter rule'],
      ['DELETE', '/api/filters/{id}', 'Delete a filter rule'],
      ['GET', '/api/ai-configs', 'List AI rewrite configurations'],
      ['POST', '/api/ai-configs', 'Create an AI configuration (provider, model, prompts)'],
      ['PUT', '/api/ai-configs/{id}', 'Update an AI configuration'],
      ['POST', '/api/prompts/test', 'Send sample text through a rewrite prompt'],
    ],
  },
  {
    group: 'Observability',
    endpoints: [
      ['GET', '/api/logs?service=&level=&search=&limit=&since=', 'Query attributable log history with bounded paging'],
      ['GET', '/api/logs/stats', 'Aggregate counts by service and level'],
      ['GET', '/api/logs/policy', 'Which levels and services the engine admits'],
      ['POST', '/api/logs/purge', "Purge log history for the caller's scope"],
      ['GET', '/api/stats', 'Delivery throughput and outcome counters'],
      ['GET', '/api/queue', 'Queue depth and in-flight jobs'],
      ['GET', '/api/dlq', 'Dead-lettered jobs'],
      ['POST', '/api/dlq/{id}/retry', 'Replay a dead-lettered job'],
      ['DELETE', '/api/dlq', 'Purge dead-lettered jobs for the caller scope (answers with the number removed)'],
    ],
  },
];

// ---------------------------------------------------------------------------
// 7b. Complete REST route table, parsed from api/main.go
// ---------------------------------------------------------------------------
// The Go control plane registers every route as
//   <mux>.HandleFunc("VERB /path", handler)
// and mounts child muxes with
//   mux.Handle("/prefix/", middleware(childMux))
// so the public URL of a child route is prefix + path. Both statements are read
// here, otherwise the reference would drift the moment a handler is added.
const apiMainPath = path.join(rootDir, 'api/main.go');
const apiSource = fs.existsSync(apiMainPath) ? fs.readFileSync(apiMainPath, 'utf8') : '';

const muxGroupNames = {
  mux: 'Service health & system',
  authMux: 'Authentication',
  dashMux: 'Authenticated control plane',
  adminMux: 'Administration',
};

const muxPrefixes = { mux: '' };
const mounts = [];
const endpointDecls = [];

for (const rawLine of apiSource.split('\n')) {
  const line = rawLine.trim();
  if (!line || line.startsWith('//')) continue;

  const declaration = line.match(/^(\w+)\s*:=\s*http\.NewServeMux\(\)$/);
  if (declaration) {
    if (!(declaration[1] in muxPrefixes)) muxPrefixes[declaration[1]] = null;
    continue;
  }

  // A method-bearing Handle() is a route whose handler is wrapped in middleware
  // before registration — `mux.Handle("PUT /logs/policy", h.RequireAdmin(...))`.
  // The first version of this parser only read HandleFunc, so every admin-gated
  // route registered this way was missing from the published reference while
  // still answering on the wire. Ownership still comes from the mux it is
  // registered on, so the mount resolution below is unchanged. Checked before
  // the mount form, whose path never carries a verb.
  const guarded = line.match(/^(\w+)\.Handle\("(GET|POST|PUT|PATCH|DELETE|HEAD) ([^"]+)",\s*(.+)\)$/);
  if (guarded) {
    endpointDecls.push({
      owner: guarded[1],
      method: guarded[2],
      sub: `/${guarded[3]}`,
      handler: guarded[4].trim(),
      requiresAdmin: /RequireAdmin/.test(guarded[4]),
    });
    continue;
  }

  const mount = line.match(/^(\w+)\.Handle\("([^"]+)",\s*(.+)\)$/);
  if (mount) {
    const child = mount[3].match(/\b(\w+Mux)\b/);
    if (child)
      mounts.push({
        owner: mount[1],
        prefix: mount[2],
        child: child[1],
        // A child mux mounted through RequireAdmin hands every route inside it
        // the administrator gate, so the route's own handler text is not the
        // only place that gate can appear:
        //   dashMux.Handle("/admin/", http.StripPrefix("/admin", h.RequireAdmin(adminMux)))
        adminGated: /RequireAdmin/.test(mount[3]),
      });
    continue;
  }

  const endpoint = line.match(/^(\w+)\.HandleFunc\("(?:(GET|POST|PUT|PATCH|DELETE|HEAD) )?([^"]+)",\s*(.+?)\)$/);
  if (endpoint) {
    endpointDecls.push({
      owner: endpoint[1],
      method: endpoint[2] || 'GET',
      sub: endpoint[3],
      handler: endpoint[4].trim(),
    });
  }
}

// Resolve mount prefixes to a fixed point: a nested mux may be mounted before
// its own parent's prefix is known.
for (let pass = 0; pass < 8; pass += 1) {
  let changed = false;
  for (const mount of mounts) {
    const parentPrefix = muxPrefixes[mount.owner];
    if (parentPrefix === undefined || parentPrefix === null) continue;
    const resolved = (parentPrefix + mount.prefix).replace(/\/{2,}/g, '/').replace(/\/$/, '');
    if (muxPrefixes[mount.child] === null || muxPrefixes[mount.child] === undefined) {
      muxPrefixes[mount.child] = resolved;
      changed = true;
    }
  }
  if (!changed) break;
}

const apiRoutes = [];
const seenRoutes = new Set();

// A mux mounted behind the administrator gate confers that gate on every route
// it contains, however those routes were registered.
const adminGatedMuxes = new Set(mounts.filter((mount) => mount.adminGated).map((mount) => mount.child));

for (const decl of endpointDecls) {
  const base = muxPrefixes[decl.owner];
  if (base === undefined || base === null) continue;
  const full = (base + decl.sub).replace(/\/{2,}/g, '/');
  const key = `${decl.method} ${full}`;
  if (seenRoutes.has(key)) continue;
  seenRoutes.add(key);
  apiRoutes.push({
    group: muxGroupNames[decl.owner] || decl.owner,
    method: decl.method,
    path: full,
    handler: decl.handler,
    // `admin` is a stricter form of `bearer`: the route is only reachable with a
    // verified identity that also carries the administrator flag. It comes from
    // the route's own wrapper or from the gate on the mux it was mounted under.
    auth: decl.requiresAdmin || adminGatedMuxes.has(decl.owner)
      ? 'admin'
      : decl.owner === 'dashMux' || decl.owner === 'adminMux'
        ? 'bearer'
        : 'none',
  });
}

apiRoutes.sort((a, b) => {
  const order = Object.values(muxGroupNames);
  const gi = order.indexOf(a.group) - order.indexOf(b.group);
  if (gi !== 0) return gi;
  if (a.path === b.path) return 0;
  return a.path.localeCompare(b.path);
});

const apiRouteGroupOrder = Object.values(muxGroupNames).filter((name) =>
  apiRoutes.some((route) => route.group === name),
);

const releases = [
  {
    version: 'v2.0.0',
    tag: 'v2.0.0',
    date: '2026-10-16',
    title: 'v2.0 — Multi-Source Routing, Per-Task Policies, AI Control & Full Observability',
    summary:
      'v2.0 turns the forwarder into a routing platform: one task fans out from any number of sources to any number of targets, each with its own content policy. AI rewriting is switchable per task, the whole stack is observable, scriptable and upgradeable from the command line, and both clients share one contract-gated system surface.',
    highlights: [
      'M sources → N targets per task, with the primary mirror kept in step by a single sanctioned write path',
      'Per-task content rules (metadata["content_policy"]) plus per-source and per-target overrides that are additive only',
      'Unicode-folded, word-boundary keyword matching — one blacklist catches Persian, Arabic and English spellings alike',
      'ReDoS-refused regex gates, single-pass deterministic replacements and cheapest-first evaluation',
      'AI rewrite enable/disable per task, custom prompt templates with versioning, and delivery that never blocks when the model is unreachable',
      'Central logger with attributable, queryable history plus a log explorer with live stream, stats and purge',
      'Dead-letter queue inspect / replay / purge, tenant-scoped, with OUTCOME_UNCONFIRMED as a first-class outcome',
      'Ordered delivery lanes so a burst is delivered in the order it was posted',
      'Atomic channel cursor persistence — the poller can no longer race the connection',
      'Modern system surface shared by web and mobile, protected by a contract gate that fails on drift',
      `Full-stack DevOps CLI with ${cliGroupCount} command groups, plus atf validate as a CI pre-deploy gate`,
      'Automated upgrade engine: git pull → build → recreate → network alias repair → health gate',
      `${locales.length}-language UI with enforced 1:1 catalog parity and strict English fallback`,
    ],
  },
  {
    version: 'v1.5.2',
    tag: 'v1.5.2',
    date: '2026-10-13',
    title: 'Mobile release hardening',
    summary: 'Android permission and packaging fixes on top of the Flutter client preview.',
    highlights: [
      'INTERNET permission restored in the Android manifest',
      'Release APK and Play Store bundle pipeline stabilised',
      'Mobile reachability probing before any credential prompt',
    ],
  },
  {
    version: 'v1.5.1',
    tag: 'v1.5.1',
    date: '2026-10-13',
    title: 'Flutter client parity pass',
    summary: 'Mobile client aligned with the dashboard surface and the localization rules.',
    highlights: [
      'RTL/LTR layout handling verified on Android',
      'Secure credential storage for sessions and passwords',
      'Protocol version compatibility check against the API',
    ],
  },
  {
    version: 'v1.5.0',
    tag: 'v1.5.0',
    date: '2026-10-13',
    title: 'Flutter mobile client',
    summary: 'First packaged mobile release mirroring the dashboard capabilities.',
    highlights: [
      'Native Android build embedding the full control surface',
      'Encrypted local storage of the dashboard token',
      'Live health and topology screen on mobile',
    ],
  },
  {
    version: 'v1.4.1',
    tag: 'v1.4.1',
    date: '2026-10-11',
    title: 'Bot-layer tenant isolation',
    summary: 'Fail-closed ownership enforcement extended into the Telegram bot layer.',
    highlights: [
      'Numeric-id authorization for every bot action',
      'Clean onboarding for unlinked users (no premature dashboard links)',
      'Structured [PIPELINE:*] logging across the bot commands',
    ],
  },
];

const repoInfo = {
  name: 'AutoTelegramForward',
  repoUrl: 'https://github.com/Opselon/AutoTelegramForward',
  starsUrl: 'https://img.shields.io/github/stars/Opselon/AutoTelegramForward?style=social',
  docsUrl: 'https://opselon.github.io/AutoTelegramForward/',
  license: 'MIT',
  version,
  releaseTag: `v${version}`,
  releaseDate: '2026-10-16',
  gitCommit,
  gitDate,
  syncTimestamp: new Date().toISOString(),
  author: 'Opselon',
  maintainer: { name: '𝐂ap$iz𝗲Ʀ', handle: '@Capxi', url: 'https://t.me/Capxi' },
  locales,
  i18nKeyCount,
  stats: {
    pythonTests,
    goTests,
    grpcRpcCount: protoRpcCount,
    cliGroups: cliGroupCount,
    featureCatalogEntries: featureCatalogCount,
    services: 4,
    locales: locales.length,
    i18nKeys: i18nKeyCount,
    implementedFeatures: featureCatalogStatus.implemented,
    partialFeatures: featureCatalogStatus.partial,
    plannedFeatures: featureCatalogStatus.planned,
    apiRoutes: apiRoutes.length,
  },
  featureCatalog: featureCatalogNames,
  featureCatalogDetail,
  featureCatalogStatus,
  verifiedFeatures,
  supportedMediaTypes,
  supportedShortcodes,
  capabilityMatrix,
  cliGroups,
  // Parsed straight out of api/main.go: every registered route with its verb,
  // full URL and Go handler. Nothing here is hand-maintained.
  apiRoutes,
  apiRouteGroupOrder,
  // Versioned aliases the client (dashboard / mobile) actually calls.
  apiAuthRoutes: apiRoutes.filter((r) => r.group === 'Authentication'),
  apiGroups,
  lifecycleStates: [
    'RECEIVED', 'VALIDATING', 'AUTHORIZED', 'ACCEPTED', 'IN_PROGRESS',
    'RETRY_WAIT', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'OUTCOME_UNCONFIRMED',
  ],
};

fs.writeFileSync(path.join(websiteDir, 'src/data/repo-info.json'), JSON.stringify(repoInfo, null, 2), 'utf8');
fs.writeFileSync(path.join(websiteDir, 'src/data/releases.json'), JSON.stringify(releases, null, 2), 'utf8');

console.log(
  `✅ Synchronized ${version} (${gitCommit}) — ${featureCatalogCount} catalogued features, ` +
    `${cliGroupCount} CLI groups, ${protoRpcCount} gRPC RPCs, ${pythonTests} python test files, ` +
    `${locales.length} locales (${i18nKeyCount} keys).`
);
