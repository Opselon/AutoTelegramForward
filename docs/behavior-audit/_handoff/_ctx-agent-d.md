# Agent D — Advanced Transforms, Links & Control Surfaces

## Mission
Stage 4.0 discovery-only audit of AutoTelegramForward (repo: /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory`, baseline master `35ee530`). One of four parallel agents. **Audit only — do NOT rewrite engines, change schemas, or change runtime behavior.**

## Your scope (feature prefixes)
`ADV-*` (AdvancedSettingsEngine), `AI-*`, `LNK-*` (LinkManagementEngine proper), `DLV-*` (delivery/transport), header/footer, captions, and ALL control surfaces: Web, Bot, Mobile (Flutter), gRPC/REST API, CLI, backup/restore.

## Hard rules
- Cite `file:line` for every behavioral claim.
- Never edit any file outside `docs/behavior-audit/_handoff/d-advanced-surfaces.md`.
- Never edit `docs/feature_catalog.json` or another agent's handoff.
- Never write Go/Python production code; never `git commit`/`git push`.
- `FULLY_IMPLEMENTED` requires a pasted production call chain reaching the code.

## Required deliverable
ONE file: `docs/behavior-audit/_handoff/d-advanced-surfaces.md` with these sections:

### 1. AdvancedSettingsEngine (ADV-*)
`core/domain/services.py:1409-1567`:
- `apply_content_transforms` (1441): order = watermark (1) → link_buttons (2, paid add-on `is_addon_enabled`) → affiliate_kit (3). Constants: WATERMARK_MAX_LEN=200, KEYWORD_REPLY_MAX_LEN=4096. Pipeline caller: `core/application/pipeline.py:1558`. Failure → deliver untransformed (pipeline.py:1568).
- `classify_keyword_trigger` (1477) — Agent C owns KW-*; you document only the add-on gate mechanics (`is_addon_enabled`, `paid_addons`).
- `get_auto_delete_seconds` (1498): TTL 0=keep, add-on gate, bounded to 31_536_000.
- `is_edit_within_window` (1513), `get_dispatch_delay` (1538).
- Add-on keys: CRYPTO, WATERMARK, LINK_BUTTONS, AFFILIATE, OCR, SMART_REWRITE, KEYWORD_REPLY, AUTO_DELETE (services.py:1431-1439). `is_addon_enabled` at entities.py:1634.
- Watermark placement: appends `\n\n` at END (after header/footer — pipeline.py:1558 runs after 1547). Duplicate prevention: `if wm not in text`.
- `watermark`/`watermark_text` (entities.py:1383/1622), `link_buttons` (1393), `affiliate_kit` (1406), `crypto_mode` (1222), `button_to_text` (1362), `paid_addons` (1526).

### 2. Header / Footer / captions
- `FilterEngine.apply_header_footer` (services.py:241-252): `\n\n` join, duplicate-header/footer prevention, `.strip()`.
- `HeaderFormatter.format_text` (services.py:320) — placeholders; list the placeholder names it supports.
- Pipeline application at `pipeline.py:1547-1552` (with payload formatting).
- `CaptionManagementEngine` (services.py:1118): 1024-char caption limit, header/footer dedup, entity offsets, splitting (`split_long_caption` entities.py:408, `caption_max_length` 251, `media_handling` 243, `custom_caption_template` 412, `preserve_signature` 252).

### 3. LinkManagementEngine (LNK-*)
`core/domain/services.py:758-1116`:
- 8 policy values in `core/domain/value_objects.py:151` LinkPolicy enum.
- `resolve_policy` (1001): fail-closed to PRESERVE_ALL on unknown value; `remove_links` flag upgrades PRESERVE_ALL → REMOVE_ALL_URLS (1019-1023).
- `apply_to_payload` (1030) — pipeline caller at `pipeline.py:1448`; entity source: `payload.caption_entities` when media else `payload.entities` (1040).
- `process_text_and_entities` (836): removal/rewrite, `_should_remove` (860), `_rewrite_url` (877), `_shifted` (973) UTF-16 offset adjustment.
- `is_safe_url` (769): SSRF — private IP, loopback, link-local, metadata endpoint, scheme allowlist (http/https/tg only).
- `extract_domain` (807), `matches_domain` (822, subdomain match), `TG_DOMAINS` (767).
- `filter_inline_keyboard` (1118) — inline-button filtering per policy.
- First-class fields: `link_policy` (entities.py 403), `domain_allowlist` (404), `domain_blocklist` (405), `link_rewrite_map` (406).
- Document which features are distinct (the brief §9 list of 13 — `remove_links` vs `link_replacement` vs `link_policy` vs `domain_allowlist` vs `domain_blocklist` vs `link_rewrite_map` vs `blocked_media_types=link` vs `FilterRule.block_links` vs ContentPolicy link logic — each needs a distinct entry saying which authority it reads).
- Go: `worker/internal/engine/transform/transform.go` `urlRegex` (21) + `ApplyTargetOverride` (56) + `HasLink` (118) — note Go only handles RemoveLinks + StripWords for TARGET overrides; no domain allowlist/blocklist, no rewrite map, no entity handling, no inline-keyboard filter.

### 4. AI transforms (AI-*)
`pipeline.py:1616` `_run_ai_transform` and `pipeline.py:1724` `_handle_ai_fallback`; `AIConfig` (entities.py:2413), `AIPrompt` (2434), `AIPromptVersion` (2452), `ai_config_id` (401), `ai_fallback_policy` (427: SEND_ORIGINAL/DROP/RETRY/QUARANTINE), `ai_prompt_version` (428), `ai_timeout_seconds` (429), `ai_secondary_config_id` (430), `ai_rewrite_active` (1470), `smart_ai_rewrite` (1464), `ocr_image_ai` (1450), `news_digest` (1492), `translate_lang` (1373), `ai_creativity` (1420), `ai_max_tokens` (1435); `core/infrastructure/ai/`.

### 5. Delivery & transport (DLV-*)
- `DeliveryStateMachine` (services.py:1337), delivery stages (COPYING_TO_C/FORWARDING_TO_B/SENDING, services.py:1400-1413).
- `delivery_transport` USER/BOT (entities.py:642), `bot_token_id` (620), fallback when no token (651-656), `use_bot_route` (663).
- Go: `worker/internal/engine/delivery/delivery.go` full claim→dispatch flow (190-360), `branding_stage.go`, scheduler `worker/internal/engine/scheduler/`, transports, `transportFor`/`botTarget`.
- Queue: `core/infrastructure/telegram/durable_queue.py` (enqueue_delivery, job payload, what is baked in).
- Rate limits: `rate_limit_per_minute` (241), `rate_limit_burst` (242), Go `targetRateWait`.

### 6. Control surfaces (map every entry point per feature)
For each of: Web (`web/`), Bot (`core/infrastructure/telegram/pro_bot_ui.py` 13199 lines, `replace_bot_ui.py`), Mobile (`mobile/`), gRPC (`core/infrastructure/grpc_server/servicers.py` 2950 lines), REST (`api/internal/handlers/`), CLI (`atfcli/`), backup/restore: state which settings/features each surface can read, write, and which paths bypass the shared services. Note any surface that writes a setting no engine reads.

### 7. Parity findings + fixtures + unknowns
Each finding: minimal input fixture, Python output, Go output, expected, severity, recommended separate fix.

## Starting points
`core/domain/services.py` (758-1567), `core/domain/value_objects.py`, `core/domain/entities.py`, `core/application/pipeline.py` (1448-1580), `worker/internal/engine/delivery/`, `worker/internal/engine/transform/transform.go`, `core/infrastructure/telegram/pro_bot_ui.py`, `core/infrastructure/grpc_server/servicers.py`.
