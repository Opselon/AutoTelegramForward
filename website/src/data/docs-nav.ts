/**
 * Single source of truth for the documentation sidebar and the search index.
 * Paths are stored base-less; prefix them with url() at render time.
 */
export interface DocLink {
  title: string;
  path: string;
  desc: string;
  isNew?: boolean;
}

export interface DocSection {
  category: string;
  items: DocLink[];
}

export const docsNav: DocSection[] = [
  {
    category: 'Getting started',
    items: [
      { title: 'Quickstart', path: '/getting-started/quickstart/', desc: 'Zilch to a verified stack and a first delivered message, in one pass.', isNew: true },
      { title: 'Install overview', path: '/getting-started/', desc: 'Choose docker, source or portable mode and know what each gives you.' },
      { title: 'Docker Compose deployment', path: '/getting-started/docker/', desc: 'Run the four services with one compose file and verify the stack.' },
      { title: 'Linux & Windows setup', path: '/getting-started/linux-windows/', desc: 'Native installers, virtualenv setup and boot persistence.' },
      { title: 'Telegram bot configuration', path: '/getting-started/telegram-bot-setup/', desc: 'BotFather token, admin id, first login and the panel.' },
      { title: 'Account linking', path: '/getting-started/account-linking/', desc: 'Single-use login codes, the exchange endpoint and re-authentication.', isNew: true },
      { title: 'Configuration & environment', path: '/getting-started/configuration/', desc: 'Every environment key, with its default and secret status.' },
      { title: 'Verifying the installation', path: '/getting-started/verification/', desc: 'Prove each service is serving before you trust it.' },
    ],
  },
  {
    category: 'New in v2.0',
    items: [
      { title: 'Multi-source → multi-target routing', path: '/features/multi-source-routing/', desc: 'One task, many sources, many targets.', isNew: true },
      { title: 'Per-task content rules', path: '/features/per-task-content-rules/', desc: 'Blacklists, whitelists, links, regex and replacements.', isNew: true },
      { title: 'AI rewriting', path: '/features/ai-rewriting/', desc: 'Optional LLM pass with custom prompt templates.', isNew: true },
      { title: 'Observability & log history', path: '/features/observability-and-logs/', desc: 'Attributable logs, stats, DLQ replay.', isNew: true },
      { title: 'Ordered delivery', path: '/features/ordered-delivery/', desc: 'Bursts delivered in posted order.', isNew: true },
      { title: 'Dedup, replies & edits', path: '/features/deduplication-and-replies/', desc: 'No repeats, no orphaned replies.', isNew: true },
      { title: 'Clients & interfaces', path: '/features/clients-and-interfaces/', desc: 'Bot, dashboard, mobile and API parity.', isNew: true },
      { title: 'Multilingual UI', path: '/features/multilingual-ui/', desc: 'Ten locales with enforced 1:1 parity.', isNew: true },
      { title: 'DevOps CLI & upgrades', path: '/features/devops-cli/', desc: 'Validate, diagnose and upgrade the stack.', isNew: true },
    ],
  },
  {
    category: 'Feature deep dives',
    items: [
      { title: 'Feature overview', path: '/features/', desc: 'Everything the platform does, grouped by subsystem.' },
      { title: 'Creating a task', path: '/features/task-creation/', desc: 'Field by field: sources, targets, modes, policies and simulate.', isNew: true },
      { title: 'Automated forwarding modes', path: '/features/automated-forwarding/', desc: 'Copy versus direct forward, topics and priority.' },
      { title: 'Tasks & destinations', path: '/features/rules-and-destinations/', desc: 'Create, edit, clone, pause and delete tasks.' },
      { title: 'Text cleaner', path: '/features/text-cleaner/', desc: 'URL, mention, hashtag, emoji and phone cleaning.' },
      { title: 'Whitelist, blacklist & filters', path: '/features/whitelist-blacklist-filters/', desc: 'Keyword gates, link rules and media filters.' },
      { title: 'Header & footer shortcodes', path: '/features/headers-footers-shortcodes/', desc: 'Twelve validated shortcodes for attribution.' },
      { title: 'Replacements & regex', path: '/features/replacements-and-regex/', desc: 'Literal and pattern replacements.' },
      { title: 'Scheduling & delays', path: '/features/scheduling-and-delays/', desc: 'Delays, windows and the edit guard.' },
      { title: 'Forward every N messages', path: '/features/forward-every-n/', desc: 'Sample a busy source: 1, 6, 11, 16 …', isNew: true },
      { title: 'Media, albums & copy bypass', path: '/features/media-and-albums/', desc: 'Albums and protected-channel re-upload.' },
      { title: 'Backup, clone & migration', path: '/features/backup-clone-migration/', desc: 'Encrypted .atf bundles.' },
      { title: 'Multi-tenant security', path: '/features/multi-tenant-security/', desc: 'Fail-closed isolation and encryption.' },
    ],
  },
  {
    category: 'System atlas',
    items: [
      { title: 'System atlas overview', path: '/system/', desc: 'The whole repository mapped from its own source, with every claim traced.', isNew: true },
      { title: 'Dependency & structure atlas', path: '/system/dependencies/', desc: 'Module graph, keystone ranking, cross-subsystem traffic and cycles.', isNew: true },
      { title: 'Connection map (3D)', path: '/system/connections/', desc: 'Simulate the runtime links and replay a request path packet by packet.', isNew: true },
    ],
  },
  {
    category: 'Reference',
    items: [
      { title: 'REST API', path: '/api/', desc: 'Control-plane endpoints, authentication and error model.' },
      { title: 'Endpoint reference', path: '/api/endpoints/', desc: 'Every registered route with its verb, auth level, fields and a real response.', isNew: true },
      { title: 'Response envelope', path: '/api/responses/', desc: 'The three body shapes, every status code and the guard chain that produces it.', isNew: true },
      { title: 'Rate limits & quotas', path: '/api/rate-limits/', desc: 'The six classes, how each key is derived and what happens when the store fails.', isNew: true },
      { title: 'Task lifecycle', path: '/api/task-lifecycle/', desc: 'Durable jobs: states, transitions, idempotency, retries and bounds.', isNew: true },
      { title: 'API authentication', path: '/api/authentication/', desc: 'Tokens, bearer headers, rate limits, error codes and curl recipes.', isNew: true },
      { title: 'CLI reference', path: '/cli/', desc: 'Every command group, with examples.' },
      { title: 'Architecture', path: '/architecture/', desc: 'Services, pipeline stages, queues and storage.' },
      { title: 'Security model', path: '/security/', desc: 'Tenant isolation, encryption and redaction.' },
      { title: 'Tenant isolation', path: '/security/tenant-isolation/', desc: 'Owner scoping per surface, the three refusals and the owner-zero trap.', isNew: true },
      { title: 'Secrets & keys', path: '/security/secrets/', desc: 'What is encrypted, what is write-only, redaction and rotation.', isNew: true },
      { title: 'Request hardening', path: '/security/request-hardening/', desc: 'Body caps, timeouts, the SSRF guard on AI endpoints and shutdown.', isNew: true },
      { title: 'Operations & diagnostics', path: '/operations/', desc: 'Runbooks for the failures you will actually meet.' },
      { title: 'Troubleshooting', path: '/operations/troubleshooting/', desc: 'Symptom, cause, fix — and what to collect before asking for help.', isNew: true },
      { title: 'AI providers & prompts', path: '/operations/ai-providers/', desc: 'Keys, check-key probes, the endpoint guard, failover and what a broken provider does to delivery.', isNew: true },
      { title: 'Status board', path: '/status/', desc: 'Implemented, partial and planned, straight from the feature catalog.', isNew: true },
    ],
  },
  {
    category: 'Learn',
    items: [
      { title: 'Tutorials & recipes', path: '/tutorials/', desc: 'Copy-paste walkthroughs for common setups.' },
      { title: 'Template playground', path: '/tools/playground/', desc: 'Try the cleaner and shortcodes in the browser.' },
      { title: 'Releases', path: '/releases/', desc: 'What shipped, and when.' },
      { title: 'FAQ', path: '/faq/', desc: 'Straight answers to the frequent questions.' },
    ],
  },
];

export const allDocLinks: DocLink[] = docsNav.flatMap((s) => s.items);

/** Ordered flat list used for prev/next paging inside the docs layout. */
export const docsOrder: string[] = allDocLinks.map((l) => l.path);
