# Coordinator vocabulary — Stage 4.0 business-logic inventory

**Source baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a` (PRs #284, #286, #288 merged).
**Branch:** `audit/complete-business-logic-inventory`
**Status:** DISCOVERY-ONLY. No engine rewrites, no schema changes, no deploy, no merge.

This file is the **shared vocabulary** for the four audit agents. It records what
the coordinator has already verified from source so no agent re-derives it and so
feature IDs stay consistent. Every agent must use these exact names for these
exact things, and must cite `file:line` evidence of its own for anything new.

---

## 0. The four synthesis engines (verified — do not re-derive)

The single most important discovery: **there are four independent text/decision
systems, not one.** Confusing them is the easiest way to produce a wrong catalog.

| # | Engine | Definition site | Prod. call path | Scope |
|---|---|---|---|---|
| 1 | **FilterRule / `FilterEngine.evaluate`** | `core/domain/services.py:37` | `pipeline.py:918` (FILTER stage) **and** Go `delivery.go:212` at delivery time | Attached standalone filter object (`forward_filter_rules` table); decides ALLOW/DROP only |
| 2 | **`ContentPolicy.evaluate` / `.transform()`** | `core/domain/content_policy.py:691` | `pipeline.py:984` (task+source chain, FILTER stage) and `pipeline.py:1197-1205` (target override, ENQUEUE stage) | Per-task metadata blob `metadata["content_policy"]` + `source_policies` / `target_policies` overrides |
| 3 | **`CleanerRule.clean()`** (built by `ForwardRule.build_cleaner()`) | `core/domain/entities.py:2335` (`build_cleaner` at 808) | `pipeline.py:1408` (`_apply_transforms`) | Per-task metadata `cleaner_config` + 8 legacy flag fields; removal-only transform |
| 4 | **`LinkManagementEngine`** | `core/domain/services.py:758` | `pipeline.py:1448` (`_apply_transforms`) | First-class `ForwardRule` columns `link_policy` / `domain_allowlist` / `domain_blocklist` / `link_rewrite_map`; entity-aware, UTF-16 offset shifting |

**Note — `FilterEngine.transform_text()` is a FIFTH, mostly-legacy transform path**
(`core/domain/services.py:194-276`). It is NOT called by the durable pipeline's
`_apply_transforms`. Its real production callers are:
- `sync_engine.py:261` — edit synchronization re-transform
- `grpc_server/servicers.py:1523` — a gRPC preview/test surface
- `services.py:412` — `TransformationService.transform_text`

The durable path (`_apply_transforms`, `pipeline.py:1401`) re-implements the same
seven cleaner steps in a different order with different code (CleanerRule +
LinkManagementEngine + ContentPolicy.transform). Any document that describes only
`transform_text()` describes the wrong pipeline.

### Verified processing order — `_apply_transforms` (durable, `pipeline.py:1401-1580`)

1. **CleanerRule.clean(text)** — `pipeline.py:1408`; failure → deliver uncleaned (never drop)
2. **`LinkManagementEngine.apply_to_payload`** — `pipeline.py:1448` when `link_policy != PRESERVE_ALL` or `remove_links`; elif `link_replacement` → `FilterEngine.replace_links` (`pipeline.py:1456`)
3. **`FilterEngine.remove_emojis`** — `pipeline.py:1461` (the flag path, NOT the cleaner path)
4. **`_clamp_entities`** — `pipeline.py:1464`
5. **empty-after-clean drop** — `pipeline.py:1467` (only drop inside transforms; reason `empty_after_clean`)
6. **`ContentPolicy.transform(text)`** — `pipeline.py:1496` (task-level remove_links / strip_words / replacements)
7. **AI transform** — `pipeline.py:1524` (`_run_ai_transform`, gated by `ai_config_id` + `ai_rewrite_active`)
8. **`FilterEngine.replace_text(rule.replacements)`** — `pipeline.py:1543` (single authority for `text_replace` entries)
9. **Header / Footer** — `pipeline.py:1547-1552`
10. **`AdvancedSettingsEngine.apply_content_transforms`** — `pipeline.py:1558` (watermark, link_buttons, affiliate_kit)
11. **`_clamp_entities`** (final) — `pipeline.py:1573`

### Verified FILTER stage order (re-verified by coordinator + Agent C)

0. **`materialize(rule, bot_tokens)`** — `pipeline.py:787`. Bot defaults merged BEFORE every gate; all later gates read the merged view.
1. Rule paused / auto-paused (`pipeline.py:789`)
2. `since_ts` too-old check (`pipeline.py:801`)
3. `ignore_edits` / edit-time window (`pipeline.py:811`, `824`)
4. `skip_history` duplicate-event check (`pipeline.py:837`)
5. Content dedup fingerprint claim (`pipeline.py:861`) — released in `finally` (`pipeline.py:752`)
6. FilterRule fetch + owner check (`pipeline.py:892-916`)
7. `FilterEngine.evaluate(payload, filter_rule)` — `pipeline.py:918`
8. Session time-limited **defer, not drop** (`pipeline.py:935-949`)
9. `media_block_reason` + `link_block_reason` — `pipeline.py:955` (`media_filter.py:77/94`)
10. **Content policy chain (task + source) evaluate** — `pipeline.py:974-1016`
11. **`forward_every_n` slot gate** — `pipeline.py:1036-1084` (AFTER all filters; filtered messages consume no slot)

### Verified enqueue-stage per-target work (`pipeline.py:1150-1260`)

For each target: loop-skip → **target-scoped override evaluate** (only
`source`-prefixed-`target:` policies, on the transformed text) → **target-scoped
override transform** (`chain[1:]`) → branding intent/perform → durable enqueue.

**Key semantic (documented at `pipeline.py:1186-1196`):** the task's own policy is
evaluated on the *source* message in the FILTER stage; at enqueue only the
*target-scoped override* is re-decided, on the transformed text, because
re-deciding the task's own policy would judge the task's own transforms.

---

## 1. Confirmed baseline facts (coordinator-verified)

| Fact | Evidence |
|---|---|
| Master SHA | `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a` |
| PRs #284 / #286 / #288 | All `MERGED` (verified via `gh pr list`) |
| `SETTING_SPECS` count | **76 entries** (`core/domain/rule_settings.py:96`) |
| `ForwardRule` field count | ~55 first-class dataclass fields + ~120 metadata-backed properties (`core/domain/entities.py:210-2308`) |
| Feature catalog is stale | `docs/feature_catalog.json` says `generated_from: master@d05f059`; **440 commits** behind master |
| Switch matrix is stale | `docs/task-feature-switch-matrix.md` evidence measured against `sha-9aeb96d` / `sha-649c969`; 14 of 28 switches `PENDING` |
| Required CI checks | `python-tests`, `go-tests`, `web-frontend`, `flutter`, `docker-build`, `Rust Logger (fmt, clippy, test)` (via `gh api .../branches/master/protection`) |
| Worker activation | Go Worker owns delivery **only when configured**; `pipeline.py:532` `_worker_owns_execution()`; Python pipeline still does filters + all transforms + per-target overrides |
| What the Go Worker re-evaluates | Only the attached `FilterRule` (`delivery.go:212`, comment at 193-199 explicitly says ingest-side content policies are "baked into the job payload") and per-target `transform.TargetOverride` (RemoveLinks/StripWords) |

## 2. Feature ID scheme (USE THESE EXACT IDS)

Prefix by domain, then snake_case. An ID names one *behavior*, and two features
that share a name but read different stores are **two IDs**.

`FLT-*` FilterRule path · `POL-*` ContentPolicy path · `CLN-*` Cleaner path ·
`LNK-*` LinkManagementEngine · `RPL-*` Replace engine · `ADV-*` AdvancedSettingsEngine ·
`RT-*` Routing/SmartRoutingEngine · `DUP-*` Dedup · `EVT-*` Event lifecycle ·
`BRD-*` Branding · `DLV-*` Delivery/transport · `SET-*` Settings registry ·
`MED-*` Media filtering · `UNI-*` Unicode/parity · `AI-*` AI transforms ·
`KW-*` Keyword reply · `FEN-*` Forward Every N.

When an agent needs a new ID it takes its own prefix. **No agent may renumber or
rename another agent's IDs.**

## 3. Status vocabulary (use exactly these tokens)

`FULLY_IMPLEMENTED` · `PARTIALLY_IMPLEMENTED` · `LEGACY_ONLY` · `UI_ONLY` ·
`STORED_BUT_UNUSED` · `UNREFERENCED_HELPER` · `DUPLICATED` · `INCOMPATIBLE` ·
`NOT_IMPLEMENTED` · `NOT_VERIFIED`

`FULLY_IMPLEMENTED` requires the agent to paste the production call chain
(function → function → function) that reaches the code. A test file, a proto
field, a UI button or a compiling function is never sufficient.

## 4. Non-overlapping ownership (from the mission brief §22)

| Agent | Owns (feature prefixes) | May read but must not document as primary |
|---|---|---|
| **A — Filters & content policies** | `FLT`, `POL`, `MED`, `RT` (rule matching only), `SET` (registry semantics) | cleaner, replace |
| **B — Cleaner & replace** | `CLN`, `RPL`, `UNI` (emoji/unicode), `LNK` (cleaner-side `remove_links` + `link_replacement` only) | content policy |
| **C — Routing & pipeline** | `EVT`, `DUP`, `FEN`, `KW`, `BRD` (pipeline-side), album/event ordering, all pipeline-stage order | individual engines |
| **D — Advanced transforms & surfaces** | `ADV`, `AI`, `LNK` (LME proper), `DLV`, header/footer, captions, Web/Bot/Mobile/API/CLI surfaces, backup/restore | pipeline order |

**Conflict rule:** `remove_links` is touched by B (cleaner flag) and D (LME
`resolve_policy` upgrade at `services.py:1001-1028`). B documents the flag
semantics; D documents the engine. Neither edits the other's section.

**`EVT-*` belongs to C.** The event lifecycle is C's exclusively; D documents
only delivery/transport (`DLV-*`), which starts at job claim.

## 5. Output contract — every agent writes ONE handoff file

Path: `docs/behavior-audit/_handoff/<letter>-<slug>.md` (create the dir).

Required sections:
1. **Inventory table** — every setting/feature with the columns from brief §4/§5
2. **Verified call paths** — `file:line` chains, pasted not paraphrased
3. **Parity findings** — each with: minimal input fixture, Python output, Go
   output, expected behavior, severity, recommended *separate* fix PR
4. **Fixtures proposed** — scenario id + one-line description only (coordinator builds the JSON)
5. **Unknowns** — what could not be verified and why

Hard rules: cite `file:line` for every behavioural claim; never edit
`docs/feature_catalog.json`, another agent's handoff file, or any file outside
`docs/behavior-audit/_handoff/`; never write Go or Python production code; never
run `git commit`/`git push`; never claim parity without a pasted call path.
