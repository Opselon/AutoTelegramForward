# Feature Ownership Matrix

**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`. Audit-only.

One row per feature. **A feature is only FULLY_IMPLEMENTED when its production call chain
is pasted, not inferred** — per the brief, a proto field, a compiling function, a unit test
or a UI button is never sufficient. Every "Actual runtime path" below was traced in this audit.

## Current ownership (verified)

| Feature | Python | Go | Actual runtime path | Parity | Stage |
|---|---|---|---|---|---|
| FilterRule | `services.py:37` | `filter.go:84` | `pipeline.py:918` + `delivery.go:212` | order + field gap | 4.1 |
| ContentPolicy | `content_policy.py:691` | — | `pipeline.py:974, 1197` | absent (baked) | 4.1 |
| Whitelist | 6 distinct impls | filter only | `FLT-006` + `POL-003` | divergent | 4.1 |
| Blacklist | 6 distinct impls | filter only | `FLT-006` + `POL-001` | divergent | 4.1 |
| Regex filter | `content_policy.compile_user_regex` | `filter.go:243` | `FLT-007` | RE2 vs `re` | 4.1 |
| Cleaner | `entities.py:2335` | **unreferenced** | `pipeline.py:1408` | none live | 4.2 |
| Emoji removal | `services.py:31` | `transform.go:33` | `pipeline.py:1413, 1461` | **INCOMPATIBLE** | 4.2 |
| Replace | `text_replace.py:415` | — | `pipeline.py:1543` | absent | 4.3 |
| Link management | `services.py:758` | strip only | `pipeline.py:1448` | absent | 4.4 |
| Header/Footer | `services.py:241` | `transform.FormatText` preview | `pipeline.py:1547` raw | placeholders dead | 4.4 |
| Watermark | `services.py:1441` | — | `pipeline.py:1558` | absent | 4.4 |
| AI transformation | `pipeline.py:1616` | — | `pipeline.py:1524` | absent | 4.4 |
| Keyword reply | `services.py:1477` | — | `pipeline.py:1298` | absent | 4.5 |
| Routing | `services.py:415` | — | `pipeline.py:633 ROUTING` | absent | 4.5 |
| Forward Every N | `forward_interval.py` | — | `pipeline.py:1036` | absent | 4.5 |
| Dedup | `pipeline.py:861` | fence only | `pipeline.py:861` + `delivery.go:238` | claim vs fence | 4.5 |
| Delivery/transport | enqueue | `delivery.go` | durable queue + worker | **COMPLETE** | — |
| Branding | `branding.py` + `pipeline.py:543` | `delivery.go` stage 1+2 | intent → checkpoint → replay | **COMPLETE** | — |
| Auto-delete | in-memory sleep | durable scheduler | `durable_queue.py:1835` + `delivery.go` | **DUPLICATED** | 4.5 |

## The six whitelist/blacklist implementations (they are NOT one feature)

| # | Setting | Store | Reader | Can reject? |
|---|---|---|---|---|
| 1 | `FilterRule.blacklist_keywords` / `whitelist_keywords` | `filter_rules` table | `FilterEngine.evaluate` | yes |
| 2 | `ForwardRule.blacklist` / `whitelist` | metadata | `classify_keyword_trigger` (keyword reply) | no — reply trigger |
| 3 | `ForwardRule.blacklist_users` / `whitelist_users` | metadata | **none** (STORED_BUT_UNUSED) | no |
| 4 | `ContentPolicy.blacklist_words` / `whitelist_words` | `metadata["content_policy"]` | `ContentPolicy.evaluate` | yes |
| 5 | `source_policies` / `target_policies` | nested in the policy blob | `policy_chain` | yes (additive) |
| 6 | `FilterRule.allowed/blocked_media_types` | `filter_rules` table | FilterEngine + media gate | yes |

## Dead and duplicated code (verified, not inferred)

| What | Where | Evidence |
|---|---|---|
| Go `transform` package (all methods) | `worker/internal/engine/transform/` | `delivery.go:87,120` constructs; **zero** call sites. F-08 |
| `filter_inline_keyboard` | `services.py:1065` | zero callers across `core`, `web`, `api`, `mobile`, `worker`. F-16 |
| `DuplicateDetectionService` | `services.py:678` | only a test caller. F-01 |
| `dedupe_policy` column | `entities.py:244` | persisted by 4 layers, zero runtime readers. F-01 |
| Emoji regex ×3 | `services.py:31` / `entities.py:103` / `transform.go:33` | byte-identical, all wrong. F-03 |
| Auto-delete Python loop | `durable_queue.py:1835` | duplicates the Go durable scheduler. F-19 |
| `HeaderFormatter` placeholders | `services.py:320` | only reachable from the legacy path. F-15 |

## Storage map (where each capability actually lives)

| Capability | Storage | Notes |
|---|---|---|
| FilterRule | `filter_rules` table | 9 legacy columns + optional `regex_block`/`regex_require`/`is_enabled` |
| ContentPolicy | `metadata["content_policy"]` | blob with `source_policies`/`target_policies` nested |
| Cleaner | `metadata["cleaner_config"]` + 8 legacy flags | config key wins over flag (build_cleaner merge) |
| Replace | `metadata["replacements"]` | 4 historical shapes (see RPL-001) |
| Link policy | **first-class columns** `link_policy`/`domain_allowlist`/`domain_blocklist`/`link_rewrite_map` | NOT in SETTING_SPECS, so the validated settings API cannot write them |
| Branding | `metadata["branding_*"]` | |
| Bot defaults | attached bot's defaults blob | merged at `pipeline.py:787` before every gate |

## Intended end-state (from the brief §18)

**Go API:** configuration, users/auth, rule management, validation.
**Go Worker:** all event-level business logic — filters, policies, cleaner, emoji, replace,
links, routing, FEN, keyword, dedup, transforms, delivery orchestration, scheduling.
**Python Telegram Runtime:** MTProto, sessions, update reception, typed RPC, media
transport, Bot API via the existing adapter, token resolution.

Python must not remain a hidden business-logic owner. The five-system map in
`business-logic-inventory.md` is the boundary contract: systems 1–4 move; system 5
(the legacy `transform_text` orchestrator) is retired, not ported.
