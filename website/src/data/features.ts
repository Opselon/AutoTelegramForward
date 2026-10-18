/**
 * Deep-dive feature pages.
 *
 * Every claim on these pages is traceable to the current checkout:
 *  - routing lists + policy storage → core/domain/entities.py
 *  - matching semantics             → core/domain/content_policy.py
 *  - prompt presets                 → core/application/prompt_service.py
 *  - CLI surface                    → atfcli/commands/*
 *  - REST surface                   → api/internal/handlers/*
 */

import repoInfo from './repo-info.json';

export interface FeatureTable {
  headers: string[];
  rows: string[][];
  caption?: string;
}

export interface FeatureCode {
  label: string;
  language: 'bash' | 'json' | 'text' | 'python' | 'yaml';
  content: string;
}

export interface FeatureSection {
  id: string;
  title: string;
  lead?: string;
  paragraphs?: string[];
  bullets?: string[];
  code?: FeatureCode;
  table?: FeatureTable;
  note?: string;
}

export interface FeatureFaq {
  q: string;
  a: string;
}

export interface FeaturePage {
  slug: string;
  /** Short label for cards and sidebars. */
  cardTitle: string;
  /** Full page heading. */
  title: string;
  seoTitle: string;
  description: string;
  category: string;
  icon: string;
  highlight: string;
  isNew?: boolean;
  keywords: string[];
  sections: FeatureSection[];
  faq: FeatureFaq[];
  related: { name: string; path: string }[];
}

export const featurePages: FeaturePage[] = [
  {
    slug: 'multi-source-routing',
    cardTitle: 'Multi-source → multi-target routing',
    title: 'One task, M sources to N targets',
    seoTitle: 'Multi-Source to Multi-Target Routing',
    description:
      'A single task can watch any number of source chats and deliver to any number of targets, with the primary pair mirrored and forum topics addressable per target.',
    category: 'Routing',
    icon: 'route',
    highlight: 'Lists instead of single values, with a sanctioned write path that keeps the legacy mirror in step.',
    isNew: true,
    keywords: [
      'Telegram multi-source forwarding',
      'Telegram multi-target routing',
      'Telegram fan-out forwarding',
      'Telegram forum topic forwarding',
    ],
    sections: [
      {
        id: 'why',
        title: 'Why lists, not one source per task',
        lead: 'Duplicating a task per source was the old workaround: same cleaner, same filters, same footer, N times to maintain.',
        bullets: [
          'One policy edit applies to every source and every target of that task.',
          'Statistics, queue depth and dead letters stay aggregated per task instead of scattered across clones.',
          'Adding a source or a target is a list mutation, not a new task with its own session and schedule.',
          'The primary source and primary target are derived from the lists — never configured twice.',
        ],
        note: 'Multi-source is stored inside the existing detection_criteria JSON column, so upgrading does not change your database shape.',
      },
      {
        id: 'model',
        title: 'The data model',
        paragraphs: [
          'A task carries source_chat_ids and target_chat_ids. The first entry is mirrored into the legacy scalar fields (source_chat_id, target_chat_id) by a single sanctioned write path, set_targets(), which accepts ids, titles, or both and always leaves the pair consistent.',
          'Readers are defensive: when the mirror is empty — for example on a row written by an older release — the destination is resolved from the list itself. That is what makes the format backward compatible without a migration.',
        ],
        code: {
          label: 'A task as the API sees it',
          language: 'json',
          content: `{
  "name": "Desk feed → VIP + free tier",
  "source_chat_ids": ["@desk_feed", "-1001695513745"],
  "target_chat_ids": ["-1002000000001", "-1002000000002"],
  "mode": "copy",
  "priority": 10,
  "content_policy": { "enabled": true, "remove_links": true }
}`,
        },
      },
      {
        id: 'targets',
        title: 'Per-target addressing',
        bullets: [
          'Every target entry keeps its own title, so dashboards and the bot show a human name next to the numeric id.',
          'Forum topics are addressed per target with message_thread_id: a task can post to a topic in one supergroup and a plain channel in the same run.',
          'Copy mode drops the forward tag; direct forward keeps the native quote header. The mode is a task property, applied to every target.',
          'A target that rejects writes auto-disables that task instead of retrying forever.',
        ],
      },
      {
        id: 'operations',
        title: 'Adding sources and targets from the CLI',
        code: {
          label: 'atf CLI',
          language: 'bash',
          content: `# create a task with two sources and two targets in one shot
atf rules create --name "Desk feed" \\
  --source @desk_feed --source -1001695513745 \\
  --target -1002000000001 --target -1002000000002

# mutate the lists later
atf rules sources <task-id> list
atf rules sources <task-id> add @second_desk
atf rules sources <task-id> remove @desk_feed

# see the resolved route, including which target gets what
atf rules path <task-id>`,
        },
      },
    ],
    faq: [
      {
        q: 'Do I have to reconfigure targets after upgrading to v2.0?',
        a: 'No. Existing single-source and single-target tasks are read as a one-entry list, and the first list entry keeps mirroring into the legacy field. Nothing has to be migrated.',
      },
      {
        q: 'Can one target have different rules from another in the same task?',
        a: 'Yes — policy overrides exist per source and per target. An override is additive only: it can narrow what is forwarded or add a transform, but it can never relax the task policy.',
      },
      {
        q: 'What happens when the primary target is deleted but the list has other entries?',
        a: 'Resolution falls back to the list, so delivery continues to the remaining targets. The mirror is rewritten on the next write to the task.',
      },
    ],
    related: [
      { name: 'Per-task content rules', path: '/features/per-task-content-rules/' },
      { name: 'Dedup, replies & edits', path: '/features/deduplication-and-replies/' },
      { name: 'REST API reference', path: '/api/' },
    ],
  },
  {
    slug: 'per-task-content-rules',
    cardTitle: 'Per-task content rules',
    title: 'Decide and rewrite before delivery',
    seoTitle: 'Per-Task Telegram Content Rules and Filters',
    description:
      'Blacklists, whitelists, link handling, regex gates, length limits and ordered replacements — evaluated cheapest-first, with per-source and per-target overrides.',
    category: 'Filtering',
    icon: 'filter',
    highlight: 'Word-boundary, Unicode-folded matching that behaves the same in Persian, Arabic and English.',
    isNew: true,
    keywords: [
      'Telegram keyword blacklist whitelist',
      'Telegram regex message filter',
      'Telegram content policy',
      'Telegram message cleaning rules',
    ],
    sections: [
      {
        id: 'fields',
        title: 'What a policy contains',
        table: {
          caption: 'ContentPolicy fields (core/domain/content_policy.py)',
          headers: ['Field', 'Meaning'],
          rows: [
            ['blacklist_words', 'Never forward a message containing one of these words.'],
            ['whitelist_words', 'Forward only when these words are present.'],
            ['whitelist_mode', 'any (default), all, or phrase — how the whitelist is satisfied.'],
            ['block_links', 'Refuse any message that contains a link.'],
            ['remove_links', 'Forward the message with links stripped out.'],
            ['strip_words', 'Forward the message with these words removed.'],
            ['replacements', 'Ordered find → replace pairs, literal or re: regex.'],
            ['regex_block / regex_require', 'Refuse on match / require a match; hostile patterns are rejected.'],
            ['min_length / max_length', 'Length gates applied before any expensive work.'],
            ['allowed_media / blocked_media', 'Media allow and deny gates.'],
            ['case_sensitive / word_boundary / normalize', 'Matching semantics, all overridable per task.'],
          ],
        },
      },
      {
        id: 'semantics',
        title: 'The matching semantics that matter',
        bullets: [
          'Unicode folding: Persian and Arabic spell the same letter several ways (ي vs ی, ك vs ک, أ vs ا) and use zero-width joiners. Both the message and the rule are folded before comparison, so one key catches every spelling.',
          'Word boundaries: cat does not match concatenate. Keywords that begin or end with a non-word character (#promo, @channel, a URL) skip the boundary check, because a boundary there would be wrong.',
          'Cheapest-first evaluation: lengths, media, links, literal words, then regexes. A burst of fifty posts never pays for regexes it does not need.',
          'Single-pass replacements: replacements run over the original text in one pass, so a replacement value can never be re-scanned by a later rule. No cascades, no surprises.',
          'Patterns are compiled once and cached, and nested-quantifier patterns like (a+)+ are refused outright rather than trusted with a worker.',
        ],
      },
      {
        id: 'overrides',
        title: 'Overrides are additive only',
        paragraphs: [
          'The task policy is the floor. A per-source or per-target override can add constraints — a stricter blacklist for a public target, link stripping only for the free tier — but every policy in the chain must allow the message for it to be delivered.',
          'Transforms apply in chain order: task first, then source, then target. That ordering is deterministic and visible in the log entry, which records the policy provenance that decided the outcome.',
        ],
      },
      {
        id: 'try',
        title: 'Dry-run a policy before it goes live',
        code: {
          label: 'Simulate against real text',
          language: 'bash',
          content: `# does this message survive my task's policy?
atf rules simulate <task-id> --text "Join our VIP group at t.me/vip — 50% off today"

# settings are validated before they are written
atf rules settings schema
atf rules settings set <task-id> remove_links=true block_links=false
atf rules settings get <task-id>`,
        },
        note: 'The web dashboard and the mobile app expose the same fields, so a rule configured in the bot is visible and editable everywhere.',
      },
    ],
    faq: [
      {
        q: 'Will a blacklist I typed in Persian catch text written on an Arabic keyboard?',
        a: 'Yes. Letter variants (Arabic yeh, alef forms, kaf) and zero-width joiners are folded before comparison, so both spellings normalise to the same string.',
      },
      {
        q: 'Can a per-target override loosen the task rules?',
        a: 'No. Overrides are additive by construction: each policy in the chain must allow the message. A target can be stricter than the task, never more permissive.',
      },
      {
        q: 'What happens to a message that a rule rejects after it was already delivered?',
        a: 'Edits are re-evaluated against the policy; if the edited message no longer passes, the destination copy is deleted. Set max_time_edit to bound how far back edits are honoured.',
      },
      {
        q: 'Are regex patterns safe to hand to this system?',
        a: 'Patterns are compiled once and cached, and nested-quantifier constructions are refused at validation time instead of being run against traffic.',
      },
    ],
    related: [
      { name: 'Multi-source → multi-target routing', path: '/features/multi-source-routing/' },
      { name: 'Cleaner & templates deep dive', path: '/features/text-cleaner/' },
      { name: 'Header & footer shortcodes', path: '/features/headers-footers-shortcodes/' },
    ],
  },
  {
    slug: 'ai-rewriting',
    cardTitle: 'AI rewriting',
    title: 'An optional rewriting pass you control per task',
    seoTitle: 'Telegram AI Rewriting with Custom Prompts',
    description:
      'Enable or disable LLM rewriting per task, pick a provider, write your own prompts with version history, and keep forwarding even when the model is unreachable.',
    category: 'Transformation',
    icon: 'sparkles',
    highlight: 'Toggle per task. If the provider is down, messages still go out — rewritten text is a bonus, never a dependency.',
    isNew: true,
    keywords: [
      'Telegram AI rewrite',
      'Telegram LLM message rewriting',
      'OpenAI Anthropic Gemini Telegram bot',
      'custom prompt templates',
    ],
    sections: [
      {
        id: 'control',
        title: 'Enable it where it earns its keep',
        bullets: [
          'The rewrite toggle lives on the task, so a news feed can rewrite while an archive feed stays byte-exact.',
          'A reachability probe reports whether the configured provider actually answers, instead of silently skipping the pass.',
          'When the provider is unreachable or the response fails validation, the original text is forwarded unchanged and the event is logged with its reason.',
          'Rewrites run before delivery and after the content policy, so the model never sees text that was already going to be dropped.',
        ],
      },
      {
        id: 'providers',
        title: 'Providers and configuration',
        table: {
          caption: 'AIConfig fields (core/domain/entities.py)',
          headers: ['Field', 'Purpose'],
          rows: [
            ['provider', 'OpenAI, Anthropic, Gemini, Groq, DeepSeek or a custom OpenAI-compatible endpoint.'],
            ['model', 'Model id, for example gpt-4o-mini.'],
            ['api_key / base_url', 'Encrypted at rest with AES-256-GCM; base_url points at a self-hosted or proxy endpoint.'],
            ['system_prompt', 'The instruction the model receives.'],
            ['user_prompt_template', 'Template with {text} as the message placeholder.'],
            ['temperature', 'Sampling temperature per configuration.'],
            ['target_language', 'Requested output language, including auto for the Persian↔English preset.'],
            ['is_enabled', 'Configuration-level switch, independent of the per-task toggle.'],
          ],
        },
      },
      {
        id: 'prompts',
        title: 'Custom prompts with version history',
        paragraphs: [
          'Prompt templates are first-class records: name, description, system prompt, user template, target language and a version number that increments on every save. Edit history is retained, so a prompt that made output worse can be rolled back by number.',
          'Four presets ship with the service: Professional Clean, Persian↔English Translator, Executive Summarizer and Telegram Format Enhancer. Any of them can be copied into a custom template and edited without touching the original.',
        ],
        code: {
          label: 'Built-in presets',
          language: 'text',
          content: `sys_pro_clean        Professional Clean — strips promo text, ads and @channel mentions
sys_translate_fa_en  Persian <-> English Translator — keeps tickers and technical terms
sys_summarizer       Executive Summarizer — headline plus 2-3 bullets plus takeaway
sys_format_enhancer  Telegram Format Enhancer — bold section headers, clean lists`,
        },
      },
      {
        id: 'api',
        title: 'Test before you route',
        code: {
          label: 'REST + CLI',
          language: 'bash',
          content: `# test a configuration against sample text
curl -sX POST https://your-host/api/v1/ai/<config-id>/test \\
  -H "Authorization: Bearer $ATF_TOKEN" \\
  -H 'Content-Type: application/json' \\
  -d '{"text": "Join our VIP group — 50% OFF today!!!"}'

# the CLI reports provider reachability as part of the diagnosis
atf doctor
atf config list`,
        },
        note: 'Prompt testing is available in the bot UI, the dashboard and the mobile app; the same endpoints back all three.',
      },
    ],
    faq: [
      {
        q: 'What happens if my AI provider is down?',
        a: 'The rewrite is skipped, the original message is delivered, and a log entry records the reason. Delivery never depends on a third-party API.',
      },
      {
        q: 'Are my API keys stored in plain text?',
        a: 'No. Configuration secrets are encrypted at rest with AES-256-GCM using your master key, and log lines pass through field-aware redaction.',
      },
      {
        q: 'Can I use a self-hosted or OpenAI-compatible model?',
        a: 'Yes. Set the provider to a custom OpenAI-compatible entry and point base_url at your endpoint; model and prompts behave the same way.',
      },
    ],
    related: [
      { name: 'AI providers, keys and prompts in depth', path: '/operations/ai-providers/' },
      { name: 'Per-task content rules', path: '/features/per-task-content-rules/' },
      { name: 'Observability & log history', path: '/features/observability-and-logs/' },
      { name: 'REST API reference', path: '/api/' },
    ],
  },
  {
    slug: 'observability-and-logs',
    cardTitle: 'Observability & log history',
    title: 'Attributable history, not a wall of text',
    seoTitle: 'Forwarder Observability & Log History',
    description:
      'Every delivery is attributable: the central logger stores queryable history with service, owner and correlation id, and dead letters can be replayed.',
    category: 'Operations',
    icon: 'activity',
    highlight: 'Outcome is never guessed: UNKNOWN/OUTCOME_UNCONFIRMED is a first-class state between success and failure.',
    isNew: true,
    keywords: [
      'Telegram forwarder logs',
      'delivery audit trail',
      'dead letter queue replay',
      'service health topology',
    ],
    sections: [
      {
        id: 'logger',
        title: 'A logger that owns its own history',
        bullets: [
          'The central logger is a separate microservice with its own storage, so a busy forwarding run cannot slow down log queries.',
          'Every entry is attributable: service, owner, severity, timestamp and a correlation id that ties a pipeline stage back to the message it belongs to.',
          'Structured [PIPELINE:*] stages — RECEIVED, VALIDATING, AUTHORIZED, ACCEPTED, IN_PROGRESS, RETRY_WAIT, SUCCEEDED, FAILED, CANCELLED, OUTCOME_UNCONFIRMED.',
          'Raw message bodies are deliberately not persisted. Content is represented by ids, hashes and type metadata — minimum necessary by design.',
          'Retention is bounded and purge is tenant-scoped, so one tenant can never delete another tenant\'s history.',
        ],
      },
      {
        id: 'explorer',
        title: 'The explorer, in every client',
        table: {
          caption: 'Filters accepted by the log query API',
          headers: ['Parameter', 'Effect'],
          rows: [
            ['service', 'Narrow to one emitter: core, api, web or logger.'],
            ['level', 'DEBUG, INFO, WARNING or ERROR.'],
            ['search', 'Substring match across the message field.'],
            ['since', 'Only entries newer than this timestamp.'],
            ['limit', 'Page size, bounded on the server before it is narrowed to an int32 offset.'],
          ],
        },
        paragraphs: [
          'The same query surface backs the bot submenu, the dashboard log explorer and the mobile app, and the CLI exposes it as atf logs query / search / stats / tail / export / purge.',
        ],
      },
      {
        id: 'outcomes',
        title: 'Honest outcomes, including the awkward one',
        paragraphs: [
          'Telegram cannot promise exactly-once delivery, so this system does not pretend otherwise. A request that times out without confirmation becomes OUTCOME_UNCONFIRMED — visible, queryable and replayable — rather than a silent success or a phantom failure.',
          'After the configured retries, a job that still cannot be delivered moves to the dead-letter queue with its full attempt history. DLQ entries can be inspected, replayed or purged, always inside the owning tenant\'s scope.',
        ],
        code: {
          label: 'Triage from the terminal',
          language: 'bash',
          content: `atf logs query --level ERROR --since 1h
atf logs stats
atf data queue                 # depth, in-flight leases, retry waits
atf data dlq                   # what failed, and why
atf data dlq --replay          # push them back through the pipeline`,
        },
      },
      {
        id: 'health',
        title: 'Topology and health',
        bullets: [
          'Four services are probed independently: core, logger, api and web, each with reachability, latency and version.',
          'GET /api/health/services returns the same data the dashboard tile renders, for your own monitoring stack.',
          'atf system services prints a colourised table with fix hints and exits non-zero if something is down, ready for a cron check.',
        ],
      },
    ],
    faq: [
      {
        q: 'Can I see logs for another user\'s task?',
        a: 'No. Queries are scoped to the authenticated tenant before any filter is applied, and purge is scoped the same way.',
      },
      {
        q: 'Are message bodies stored in the logs?',
        a: 'No. Logs carry ids, hashes, lengths and type metadata. Message content is not persisted by the logger, and secrets are redacted before a line is emitted.',
      },
      {
        q: 'What is OUTCOME_UNCONFIRMED?',
        a: 'It is the state for a delivery whose result Telegram never confirmed, typically a timeout. It is queryable and replayable, and it prevents a timeout from being reported as a success or as a definitive failure.',
      },
    ],
    related: [
      { name: 'Ordered delivery', path: '/features/ordered-delivery/' },
      { name: 'DevOps CLI & upgrades', path: '/features/devops-cli/' },
      { name: 'Operations runbook', path: '/operations/' },
    ],
  },
  {
    slug: 'ordered-delivery',
    cardTitle: 'Ordered delivery',
    title: 'A burst arrives in the order it was posted',
    seoTitle: 'Ordered Durable Telegram Delivery Queue',
    description:
      'Lease-based durable workers, per-channel delivery lanes that preserve post order, atomic cursor persistence, retry with jitter, and no duplicate sends.',
    category: 'Reliability',
    icon: 'layers',
    highlight: 'Ordering is per channel lane, so parallelism never reshuffles a conversation.',
    isNew: true,
    keywords: [
      'Telegram message ordering',
      'durable delivery queue',
      'exponential backoff retry',
      'Telegram flood wait handling',
    ],
    sections: [
      {
        id: 'lanes',
        title: 'Lanes, not a free-for-all',
        paragraphs: [
          'Posts from one source channel are assigned to that channel\'s delivery lane. Within a lane, order is preserved; across lanes, work runs in parallel. That is what lets a ten-message burst come out in the right sequence while three other channels are being served at the same time.',
          'This is why a busy multi-source task does not interleave two channels\' posts, and why a retry of an older message never overtakes a newer one in the same lane.',
        ],
        bullets: [
          'Per-channel ordering with parallel execution across channels.',
          'Lease-based jobs: a worker holds a job for a bounded time, so a crashed worker releases work instead of stranding it.',
          'Exponential backoff with jitter, so retries do not synchronise into a thundering herd.',
          'Backpressure control: the queue depth and in-flight count are visible, and new work defers rather than failing.',
        ],
      },
      {
        id: 'cursors',
        title: 'Cursor persistence that cannot race',
        paragraphs: [
          'The channel cursor records how far a source has been read. Persisting it non-atomically meant the poller and the connection could both write, and a crash between the two could skip or repeat messages. Cursors are now persisted atomically, which removed the last source of double delivery in the poller path.',
        ],
        note: 'Ordered lanes plus atomic cursors plus SHA-256 content dedup together give at-least-once reading with observable, bounded duplication rather than silent reshuffling.',
      },
      {
        id: 'floodwait',
        title: 'When Telegram says slow down',
        table: {
          caption: 'Rate and failure handling',
          headers: ['Event', 'Behaviour'],
          rows: [
            ['FloodWait', 'The affected account pauses, the queue stops consuming for that account, and delivery resumes automatically when the limit expires.'],
            ['CHAT_WRITE_FORBIDDEN', 'The task is auto-disabled instead of retrying into a channel that will never accept it.'],
            ['Transient network error', 'Retry with backoff and jitter, up to the configured attempt limit.'],
            ['Unconfirmed timeout', 'Job becomes OUTCOME_UNCONFIRMED and is visible in the DLQ for replay.'],
            ['Edited source message', 'Delivered copies are edited in place; policy is re-evaluated and stale edits are ignored beyond max_time_edit.'],
          ],
        },
      },
      {
        id: 'observing',
        title: 'Watching the queue',
        code: {
          label: 'Queue visibility',
          language: 'bash',
          content: `atf data queue            # depth per state, leases, retry waits
atf data stats            # throughput and outcome counters
atf status --watch        # live board: services, ports, tasks
atf watch                 # continuous supervision`,
        },
      },
    ],
    faq: [
      {
        q: 'Is delivery exactly-once?',
        a: 'No system can promise that against Telegram. This one promises order within a channel lane, atomic cursor persistence, content deduplication, and an explicit OUTCOME_UNCONFIRMED state instead of silent ambiguity.',
      },
      {
        q: 'What happens to queued jobs when I pause a task?',
        a: 'They are deferred with backoff rather than failed, and resume unblocks retry-wait jobs when you turn the task back on.',
      },
      {
        q: 'Can I limit throughput to protect an account?',
        a: 'Yes — per-target delays and scheduling windows throttled delivery, and FloodWait handling pauses an account automatically when Telegram asks for it.',
      },
    ],
    related: [
      { name: 'Dedup, replies & edits', path: '/features/deduplication-and-replies/' },
      { name: 'Observability & log history', path: '/features/observability-and-logs/' },
      { name: 'Scheduling & delays', path: '/features/scheduling-and-delays/' },
    ],
  },
  {
    slug: 'deduplication-and-replies',
    cardTitle: 'Dedup, replies & edits',
    title: 'No repeat posts, no orphaned replies',
    seoTitle: 'Telegram Dedup, Reply & Edit Sync',
    description:
      'SHA-256 content and album fingerprinting kills duplicate forwards, reply chains stay linked, and edits and deletions propagate within a bounded window.',
    category: 'Reliability',
    icon: 'copy',
    highlight: 'Fingerprints cover albums too, so a re-posted media group is caught as one repeat, not five.',
    isNew: true,
    keywords: [
      'Telegram duplicate message detection',
      'reply synchronization Telegram',
      'edit delete sync forwarding',
    ],
    sections: [
      {
        id: 'dedup',
        title: 'Content fingerprinting',
        bullets: [
          'SHA-256 fingerprints over normalized content and album membership are stored per task, so the same post arriving through two sources never lands twice.',
          'Albums are fingerprinted as a group: a re-sent media group is one duplicate, not a partial re-delivery.',
          'Fingerprints are tenant-scoped and pruned with retention, keeping the table bounded on long-running installs.',
          'A message that was legitimately edited is allowed through again — dedup applies to content, not to message ids.',
        ],
      },
      {
        id: 'replies',
        title: 'Conversation structure survives the move',
        paragraphs: [
          'When reply synchronisation is enabled, a reply in the source is delivered as a reply to the copied parent message in the target, with the mapping kept in the message_map table. Threads therefore stay readable instead of collapsing into a flat feed.',
        ],
        table: {
          caption: 'Mapping table responsibilities',
          headers: ['Table', 'What it maps'],
          rows: [
            ['message_map', 'source message id → target message id, per task and target, so replies and edits can find their counterpart.'],
            ['content_dedup', 'content fingerprint → first delivery, so duplicates are dropped before dispatch.'],
            ['delivery_jobs', 'the durable queue: state, attempts, lease holder and next retry time.'],
          ],
        },
      },
      {
        id: 'edits',
        title: 'Edits and deletes follow the message',
        bullets: [
          'An edited source message updates the delivered copy in place instead of posting again.',
          'Edits older than max_time_edit are ignored, so a bulk edit of historical posts cannot rewrite weeks of archives.',
          'If an edited message no longer satisfies the content policy, the delivered copy is deleted.',
          'Deleting the source deletes the delivered copies — including every target of a multi-target task.',
        ],
      },
      {
        id: 'config',
        title: 'Turning each behaviour on',
        code: {
          label: 'Per-task settings',
          language: 'bash',
          content: `atf rules settings set <task-id> sync_replies=true
atf rules settings set <task-id> max_time_edit=3600
atf rules settings get <task-id>`,
        },
        note: 'Settings are validated against the task schema before they are written — an unknown key is refused, not stored.',
      },
    ],
    faq: [
      {
        q: 'Will the same post arriving from two different sources be forwarded twice?',
        a: 'Not within the same task. The content fingerprint is checked before dispatch, and a match is dropped as a duplicate.',
      },
      {
        q: 'Does a deleted source post delete my archive copy?',
        a: 'Yes, when deletion synchronisation is active: the mapping table identifies every delivered copy and removes it.',
      },
      {
        q: 'Can I keep the archive even when the source edits history?',
        a: 'Yes — set max_time_edit to a small window. Edits older than the window are ignored while live posts still update.',
      },
    ],
    related: [
      { name: 'Ordered delivery', path: '/features/ordered-delivery/' },
      { name: 'Media, albums & copy bypass', path: '/features/media-and-albums/' },
      { name: 'Scheduling & delays', path: '/features/scheduling-and-delays/' },
    ],
  },
  {
    slug: 'clients-and-interfaces',
    cardTitle: 'Clients & interfaces',
    title: 'Four clients, one contract',
    seoTitle: 'Telegram Bot, Web Dashboard and Mobile App',
    description:
      'Telegram bot UI, React dashboard behind a Rust Axum gateway, Flutter client and a REST/gRPC control plane — all enforced against the same contract fixtures.',
    category: 'Interfaces',
    icon: 'devices',
    highlight: 'A contract gate compares the shared fixtures, so web and mobile cannot quietly drift apart.',
    isNew: true,
    keywords: [
      'Telegram bot panel',
      'Telegram web dashboard',
      'Telegram Flutter app',
      'Mini App authentication',
    ],
    sections: [
      {
        id: 'surfaces',
        title: 'What each surface is for',
        table: {
          caption: 'Client responsibilities',
          headers: ['Client', 'Best for', 'Auth'],
          rows: [
            ['Telegram bot', 'Quick edits from a phone: toggle a task, add a keyword, check stats, switch language.', 'Telegram identity, numeric user id authorization'],
            ['Web dashboard', 'Full configuration: tasks, policies, AI prompts, logs, topology, backups.', 'Bearer JWT, plus Telegram Mini App validation'],
            ['Flutter mobile app', 'The same system surface as the dashboard, with encrypted local storage of the token.', 'Dashboard token over the REST API'],
            ['REST / gRPC', 'Automation, CI and your own tooling.', 'Bearer JWT with tenant scoping'],
          ],
        },
      },
      {
        id: 'contract',
        title: 'The contract gate',
        paragraphs: [
          'Shared fixtures — the setup verdict, the environment contract and the service health payload — are checked against both clients. A field that exists on one surface but not the other fails the gate instead of shipping as a subtle inconsistency.',
          'This is the mechanism that keeps the phrase "the same system surface" honest: it is verified, not asserted.',
        ],
        bullets: [
          'Setup and readiness screens answer three questions identically everywhere: is it configured, is it reachable, what is missing.',
          'Environment reporting returns booleans and fix hints only — no secret value is ever echoed back to a client.',
          'Health and topology views share one payload shape across web, mobile and the API.',
        ],
      },
      {
        id: 'onboarding',
        title: 'Onboarding that does not lie',
        bullets: [
          'An unlinked user sees the linking flow, not a dashboard full of empty panels or a link to an app they cannot use yet.',
          'Mobile probes server reachability and protocol compatibility before asking for a credential.',
          'The setup verdict distinguishes "not configured" from "configured but down", and each state carries an actionable hint.',
        ],
        note: 'Every client speaks the same ten languages, including RTL layout for Persian.',
      },
      {
        id: 'automate',
        title: 'Drive it from CI',
        code: {
          label: 'Readiness from a script',
          language: 'bash',
          content: `curl -s https://your-host/api/system/setup?probe=0 | jq '{ready, version}'
curl -s https://your-host/api/health/services | jq '.services[] | {name, reachable, latencyMs}'
atf system services --json | jq '{healthy, total, failing}'`,
        },
      },
    ],
    faq: [
      {
        q: 'Do the mobile app and the dashboard have the same features?',
        a: 'They expose the same system surface, and a shared contract fixture set is checked in CI so a divergence fails the build rather than reaching users.',
      },
      {
        q: 'Can I use the REST API without the dashboard?',
        a: 'Yes. The API is independently authenticated with Bearer JWT and is the same control plane the dashboard and CLI use.',
      },
      {
        q: 'Is there a Telegram Mini App?',
        a: 'Mini App authentication is supported alongside JWT, so a WebApp can hand off a validated Telegram identity to the control plane.',
      },
    ],
    related: [
      { name: 'Multilingual UI', path: '/features/multilingual-ui/' },
      { name: 'REST API reference', path: '/api/' },
      { name: 'Getting started', path: '/getting-started/' },
    ],
  },
  {
    slug: 'multilingual-ui',
    cardTitle: 'Multilingual UI',
    title: 'Ten languages, one catalog, zero drift',
    seoTitle: 'Ten-Language Telegram UI, 1:1 Parity',
    description:
      'English, Persian, Russian, Chinese, Portuguese, Uzbek, Indonesian, Spanish, French and Italian share one catalog with enforced 1:1 parity.',
    category: 'Localization',
    icon: 'globe',
    highlight: 'Parity is enforced by audit, not by intention: a missing key fails the gate before it can ship.',
    isNew: true,
    keywords: [
      'Telegram bot multilingual',
      'Persian Telegram bot',
      'RTL Telegram interface',
      'i18n catalog parity',
    ],
    sections: [
      {
        id: 'languages',
        title: 'The ten languages',
        table: {
          caption: 'Shipped locales',
          headers: ['Code', 'Language', 'Script'],
          rows: [
            ['en', 'English', 'LTR'],
            ['fa', 'Persian', 'RTL'],
            ['ru', 'Russian', 'LTR (Cyrillic)'],
            ['zh', 'Chinese', 'LTR (Han)'],
            ['pt', 'Portuguese', 'LTR'],
            ['uz', 'Uzbek', 'LTR (Latin)'],
            ['id', 'Indonesian', 'LTR'],
            ['es', 'Spanish', 'LTR'],
            ['fr', 'French', 'LTR'],
            ['it', 'Italian', 'LTR'],
          ],
        },
      },
      {
        id: 'rules',
        title: 'The three rules the codebase enforces',
        bullets: [
          'One catalog, ten columns. A key exists in every language or the parity audit fails — there is no partial translation state in a release.',
          'No hardcoded language branching in UI code. Labels resolve through the translation helper with an explicit language, and English is the only fallback. Persian is never used as a default.',
          'RTL correctness is checked, not assumed: the audit verifies that English values contain no RTL script, so a copy-paste slip cannot leak Persian into the English interface.',
        ],
        note: 'Language resolution is per user, switchable at runtime from the bot, the dashboard and the mobile app.',
      },
      {
        id: 'audits',
        title: 'Audits you can run yourself',
        code: {
          label: 'Verify parity locally',
          language: 'bash',
          content: `atf i18n                  # catalog parity, placeholders, RTL leakage
atf validate              # includes the catalog checks in the standard gate
atf validate --deep       # adds the repository's own cross-language audits`,
        },
      },
      {
        id: 'contributing',
        title: 'Adding a language',
        paragraphs: [
          'Add the column to the catalog, and the audits will tell you exactly which keys are missing and which placeholders do not match. Placeholder parity matters as much as key parity: a translation that drops {count} breaks the message it renders.',
        ],
      },
    ],
    faq: [
      {
        q: 'Is the interface fully translated or partly translated?',
        a: 'Fully. Every shipped locale carries the complete catalog, and the parity audit fails the build if a key is missing in any language.',
      },
      {
        q: 'What language do I see if my language is not supported?',
        a: 'English, always. Fallback is explicit and one-directional — Persian is never used as a fallback for another language.',
      },
      {
        q: 'Does the RTL layout work on the mobile app too?',
        a: 'Yes. RTL/LTR layout handling is implemented in the web dashboard, the Flutter app and the bot keyboard layouts.',
      },
    ],
    related: [
      { name: 'Clients & interfaces', path: '/features/clients-and-interfaces/' },
      { name: 'DevOps CLI & upgrades', path: '/features/devops-cli/' },
      { name: 'FAQ', path: '/faq/' },
    ],
  },
  {
    slug: 'devops-cli',
    cardTitle: 'DevOps CLI & upgrades',
    title: 'Operate the stack from one command',
    seoTitle: 'DevOps CLI, Validate & Self-Upgrade',
    description:
      `${repoInfo.stats.cliGroups} command groups cover setup, tasks, logs, backups, docker, k8s and TLS, with a read-only validator for CI and an upgrade engine behind a health gate.`,
    category: 'Operations',
    icon: 'terminal',
    highlight: 'atf validate is read-only and exits non-zero on failure, so it works as a pre-deploy gate in any pipeline.',
    isNew: true,
    keywords: [
      'self-hosted Telegram bot CLI',
      'Telegram bot upgrade engine',
      'deployment validation gate',
      'systemd service install',
    ],
    sections: [
      {
        id: 'lifecycle',
        title: 'Lifecycle in one place',
        code: {
          label: 'Everyday commands',
          language: 'bash',
          content: `atf setup                 # guided wizard: validate, build, then prove each service
atf status --watch        # live board of services, ports and tasks
atf doctor --fix          # diagnose and repair
atf system upgrade        # git pull -> build -> recreate -> alias repair -> health gate
atf system service install# start the stack at boot (systemd / launchd / Task Scheduler)
atf release portable      # build dist/atf.pyz — the single-file CLI`,
        },
      },
      {
        id: 'validate',
        title: 'The validator',
        paragraphs: [
          'atf validate runs read-only checks and exits 0 only when every one passes. --strict promotes warnings to failures, --runtime adds live checks against the running stack, and --deep runs the repository audits. --json prints exactly one JSON object, so a pipeline can consume it.',
        ],
        table: {
          caption: 'Representative checks',
          headers: ['Check', 'What it proves'],
          rows: [
            ['layout', 'The checkout is complete: compose file, Dockerfiles, CLI and services.'],
            ['env-file / env-keys / env-secrets', 'Required keys exist, secrets meet the length floor, .env is not world readable.'],
            ['env-typos / env-ignored', 'No stray keys the example file does not document; .env is git-ignored.'],
            ['compose', 'docker-compose.yml parses cleanly.'],
            ['catalog / catalog-placeholders / catalog-rtl', 'Ten-language key and placeholder parity, and no RTL leakage into English.'],
            ['secrets-scan', 'No credential-shaped literal in any tracked file.'],
            ['database / proto / ports', 'SQLite integrity, generated stubs not older than the .proto sources, no foreign process on ATF ports.'],
            ['container-env (runtime)', 'The running API reports the configuration it actually received — this is the check that catches a container older than .env.'],
          ],
        },
      },
      {
        id: 'upgrade',
        title: 'Upgrades with a health gate',
        paragraphs: [
          'atf system upgrade pulls, rebuilds, recreates the containers, repairs the compose network aliases and then requires the health check to pass. If the stack does not come back healthy, the failure is loud rather than a half-migrated install.',
          'Container recreation is the step that most often breaks networking: a manually recreated container loses its compose alias. The upgrade path reconnects the aliases, so the core, api and web names stay resolvable to each other and to the external AI bridge.',
        ],
      },
      {
        id: 'scripts',
        title: 'Scriptable output',
        code: {
          label: 'CI-friendly',
          language: 'bash',
          content: `atf validate --strict --json | jq '.checks[] | select(.status != "ok")'
atf system services --json | jq '{healthy, total, failing}'
atf status --json | jq '.services[] | {name, port, reachable}'`,
        },
        note: 'Every line the CLI writes passes a single redaction filter, so a bot token or API key cannot leak into a transcript or a CI log.',
      },
    ],
    faq: [
      {
        q: 'Can I use the validator in CI without touching a running stack?',
        a: 'Yes. atf validate is read-only by default; --runtime only adds checks, and nothing it runs mutates the deployment.',
      },
      {
        q: 'How do I know an upgrade worked?',
        a: 'atf system upgrade ends in a health gate over all four services. atf system services reports state, health, endpoint and version per service, and exits non-zero if anything is down.',
      },
      {
        q: 'Do I need docker to use the CLI?',
        a: 'No. There are docker, source and portable modes; atf platform prints the exact install commands for the host you are on, and the portable build is a single standard-library file.',
      },
    ],
    related: [
      { name: 'Observability & log history', path: '/features/observability-and-logs/' },
      { name: 'CLI reference', path: '/cli/' },
      { name: 'Operations runbook', path: '/operations/' },
    ],
  },
];

export const featureBySlug = Object.fromEntries(featurePages.map((p) => [p.slug, p]));

/** Everything the /features/ hub lists — new deep dives first, then the established pages. */
export const featureHub = [
  {
    group: 'New in v2.0',
    items: featurePages.map((p) => ({
      title: p.cardTitle,
      href: `/features/${p.slug}/`,
      desc: p.description,
      icon: p.icon,
      badge: p.category,
      isNew: true,
    })),
  },
  {
    group: 'Core engine',
    items: [
      {
        title: 'Automated forwarding modes',
        href: '/features/automated-forwarding/',
        desc: 'Copy mode versus direct forward, channel, group and topic routing, priority ordering.',
        icon: 'route',
        badge: 'Core engine',
      },
      {
        title: 'Tasks & destination management',
        href: '/features/rules-and-destinations/',
        desc: 'Create, edit, clone, pause and delete tasks; masks and intermediate channels.',
        icon: 'list',
        badge: 'Core engine',
      },
      {
        title: 'Media, albums & copy bypass',
        href: '/features/media-and-albums/',
        desc: 'Albums as one atomic job, and re-upload delivery for protected channels.',
        icon: 'image',
        badge: 'Media',
      },
    ],
  },
  {
    group: 'Content pipeline',
    items: [
      {
        title: 'Text cleaner',
        href: '/features/text-cleaner/',
        desc: 'Strip URLs, mentions, hashtags, emojis and phone numbers, with entity offsets preserved.',
        icon: 'eraser',
        badge: 'Cleaning',
      },
      {
        title: 'Whitelist, blacklist & filters',
        href: '/features/whitelist-blacklist-filters/',
        desc: 'Keyword gates, link blocking, media gates and ReDoS-safe regex rules.',
        icon: 'filter',
        badge: 'Filtering',
      },
      {
        title: 'Header & footer shortcodes',
        href: '/features/headers-footers-shortcodes/',
        desc: 'Twelve validated shortcodes and dynamic attribution hyperlinks.',
        icon: 'code',
        badge: 'Templates',
      },
      {
        title: 'Replacements & regex',
        href: '/features/replacements-and-regex/',
        desc: 'Literal and pattern replacements with deterministic single-pass behaviour.',
        icon: 'wand',
        badge: 'Transform',
      },
      {
        title: 'Scheduling & delays',
        href: '/features/scheduling-and-delays/',
        desc: 'Per-target delays, operating windows and the max_time_edit guard.',
        icon: 'clock',
        badge: 'Timing',
      },
    ],
  },
  {
    group: 'Platform',
    items: [
      {
        title: 'Multi-tenant security',
        href: '/features/multi-tenant-security/',
        desc: 'Fail-closed isolation on immutable numeric ids, with zero cross-tenant visibility.',
        icon: 'shield',
        badge: 'Security',
      },
      {
        title: 'Backup, clone & migration',
        href: '/features/backup-clone-migration/',
        desc: 'AES-256-GCM encrypted .atf bundles that move a whole configuration.',
        icon: 'archive',
        badge: 'Data',
      },
    ],
  },
];
