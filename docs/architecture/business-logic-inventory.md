# Business Logic Inventory — AutoTelegramForward

**Status:** DISCOVERY-ONLY audit. No engine rewrites, no schema changes, no deploy, no merge.
**Source baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`
(Stage 1 = PR #284, Stage 2 = PR #286, Stage 3 = PR #288 — all merged)
**Method:** four parallel agents on non-overlapping scopes (brief §22), every agent claim
re-verified by the coordinator against source or by execution. Findings that did not
survive verification were downgraded or dropped — see `FINDINGS.md`.

> **Read this first:** the single most important discovery of this audit is that there are
> **five independent text/decision systems**, and most documentation before this point
> described only one of them.

---

## 1. The five systems

| # | Engine | Python definition | Production reach | Go counterpart |
|---|---|---|---|---|
| 1 | **FilterRule** (`FilterEngine.evaluate`) | `core/domain/services.py:37-190` | Ingest FILTER stage `pipeline.py:918` **and** Go delivery `delivery.go:212` | `worker/internal/engine/filter/filter.go:84` — active |
| 2 | **ContentPolicy** (`.evaluate` / `.transform`) | `core/domain/content_policy.py:691-1006` | FILTER stage `pipeline.py:974` + per-target `pipeline.py:1197-1220` | none — baked into payload (`delivery.go:193-199`) |
| 3 | **CleanerRule** (`.clean`, built by `build_cleaner()`) | `core/domain/entities.py:2335-2406` | TRANSFORM stage `pipeline.py:1408` | `transform.RemoveEmojis` etc. — **UNREFERENCED** (F-08) |
| 4 | **LinkManagementEngine** | `core/domain/services.py:758-1116` | TRANSFORM stage `pipeline.py:1448` | none (F-18) |
| 5 | **`FilterEngine.transform_text`** (legacy orchestrator) | `core/domain/services.py:194-276` | **NOT the durable pipeline.** Edit-sync `sync_engine.py:261` + gRPC preview `servicers.py:1523` | `transform.FormatText` — preview only |

System 5 is the trap. It re-implements the cleaner steps in a different order with different
code, and every pre-existing description of "the cleaner" followed it. The durable pipeline
(`_apply_transforms`) uses systems 3 + 4 + 2's `transform()` instead. Documenting
`transform_text` as "the cleaner" describes the wrong pipeline.

`FilterEngine.evaluate`, by contrast, is genuinely shared: the Python pipeline and the Go
worker both run it, which is why its divergences (F-11, F-12) matter for Stage 4.1.

## 2. Counts

| Metric | Count | Source |
|---|---|---|
| `SETTING_SPECS` registry keys | **76** | `core/domain/rule_settings.py:96` (AST-parsed, groups: cleanup 13, content 12, sending 11, media 10, ai 7, automation 7, branding 7, audience 4, keywords 3, route 2; 10 non-inheritable) |
| Settings with first-class fields | **13** more | inventory: `settings-inventory.json` (89 rows) |
| Feature IDs inventoried | **64** | `feature-inventory.json` |
| Distinct filtering engines | **2** (FilterRule, ContentPolicy) + media gate | §1 |
| Cleaner options | **9** (patterns, urls, mentions, hashtags, emoji, phones, empty lines, buttons, custom words) | `entities.py:2335-2406` |
| Replacement capabilities | **5** groups (storage compat, modes, smart-case, priority order, management) | `text_replace.py` |
| Control surfaces mapped | **7** (Bot, Web, Flutter, gRPC, REST, CLI, backup/restore) | Agent D §6 |
| Event types mapped | **10** | Agent C §1 |
| Runtime entry points | see `pipeline-order.json` (37 stages, 5 paths) | verified from source |
| Confirmed findings | **19** | `FINDINGS.md` |
| Characterization fixtures | **43** | `fixtures.json` |

## 3. The canonical processing order

Full stage-by-stage with line refs: `pipeline-order.json`. Diagrams in `diagrams/`.

Two corrections to the brief's illustrative outline, both verified from source:

1. **`materialize(rule, bot_tokens)` runs first** (`pipeline.py:787`), before the paused
   check. Every later gate reads the bot-defaults-merged view.
2. **`forward_every_n` runs after all filters** (`pipeline.py:1036`), not before
   transformation only — filtered messages consume no slot.

Per-rule gates (every one on the merged view): `materialize` → paused → `since_ts` →
`ignore_edits` + edit window → `skip_history` → content-dedup claim → FilterRule fetch +
owner check → `FilterEngine.evaluate` → session time-limited **defer** → media/link gates →
content-policy chain (task+source) → `forward_every_n` → `_apply_transforms` → targets +
branding geometry → **per target:** loop-skip → target-scoped policy evaluate + transform →
branding hop plan → enqueue → (after the fan-out) `mark_processed` → claim earned.

Transform order inside `_apply_transforms` (`pipeline.py:1401-1580`) — fixed, never
reordered by config: `CleanerRule.clean` → `LinkManagementEngine` (or `elif
link_replacement`) → `remove_emojis` flag → `_clamp_entities` → empty-after-clean drop
(the only drop inside transforms) → `ContentPolicy.transform` → AI → `replace_text` →
header/footer (raw, F-15) → watermark/link-buttons/affiliate → `_clamp_entities`.

## 4. Cross-language status (summary)

64 features: **51 FULLY_IMPLEMENTED, 5 PARTIALLY_IMPLEMENTED, 3 UNREFERENCED_HELPER,
2 STORED_BUT_UNUSED, 1 INCOMPATIBLE, 1 LEGACY_ONLY, 1 DUPLICATED.**

The four that define the migration's risk profile:

- **UNI-001 (INCOMPATIBLE)** — the emoji regex is byte-identical in Python and Go and
  wrong in both: it destroys non-emoji supplementary-plane scripts (Cuneiform) and
  shreds ZWJ/keycap sequences, while sparing arrows and combining marks. F-03.
- **UNI-002 / F-08 (UNREFERENCED_HELPER)** — the Go `transform` package compiles, is
  constructed at `delivery.go:120`, and **no method is ever called**. The mission brief's
  audit target #4 is exactly this confusion, now proven by grep.
- **FLT-004 / F-12 (PARTIALLY_IMPLEMENTED)** — Go's `rule.FilterRule.BlockLinks` field
  exists and is evaluated, but the rulestore projection never populates it because the
  `filter_rules` table has no such column. Latent until Worker activation defaults on.
- **ADV-004 / F-15 (PARTIALLY_IMPLEMENTED)** — header placeholders never interpolate on
  the durable path; the gRPC preview shows them expanded. The preview lies.

## 5. What must NOT change (behavior to preserve)

- The 11-step transform order in `_apply_transforms`.
- `FilterEngine.evaluate`'s check order — F-11 shows Go already differs; the Python order
  is the contract that production dashboards are built on.
- `apply_replacements` stable same-priority insertion ordering (text_replace.py:435).
- Exact-case-first smart-case legacy behavior (text_replace.py:385-390).
- ContentPolicy transform single-pass composition (opposite of RPL sequential, by design).
- `forward_every_n` running after all filters, and N=1 being a no-op.
- Content-policy target overrides being re-decided on the transformed text only
  (pipeline.py:1186-1196 — re-deciding the task's own policy was a real silent-drop bug).
- Cleaner/policy failures never dropping the message (pipeline.py:1437, 1493).
- Regex filters failing closed on an uncompileable pattern (services.py:138).

## 6. Where the truth lives

| Artifact | Content |
|---|---|
| `feature-inventory.json` | 64 features with owner, runtime path, status, evidence, migration stage |
| `settings-inventory.json` | all 89 settings (76 registry + 13 first-class) |
| `pipeline-order.json` | the source-verified stage order for all 5 paths |
| `fixtures.json` | 43 deterministic characterization fixtures |
| `FINDINGS.md` | 19 coordinator-verified findings with severity and fix pointers |
| `diagrams/*.mmd` | event-ingestion sequence, message-processing flow, parity map |
| `_handoff/a|b|c|d-*.md` | the four full agent audits with file:line citations |
| `docs/feature_catalog.json` | regenerated against `35ee530` (below) |

## 7. Catalog regeneration note

`docs/feature_catalog.json` was stale (`generated_from: master@d05f059`, 440 commits
behind, 19 features, none on the filter/cleaner/replace surface). It is regenerated in
this PR from the verified current baseline — see §6 of `feature-inventory.json` for the
mapping, and `docs/task-feature-switch-matrix.md` for the 14 previously-`PENDING`
switches now resolved by this audit.

## 8. Migration readiness (short form)

| Stage | Scope | Risk | Blocked by |
|---|---|---|---|
| 4.1 FilterRule + ContentPolicy | `filter.go` parity, `BlockLinks` projection, order alignment | medium | F-11, F-12, F-14 |
| 4.2 Cleaner + emoji | one Unicode-correct pattern shared by both languages, captions | high | F-03 (design needed), F-09 |
| 4.3 Replace | port `text_replace` semantics, single authority | medium | F-04 (precedence), F-15 (preview) |
| 4.4 Links + advanced transforms | port `LinkManagementEngine`, fix placeholder reach | medium | F-16, F-18 |
| 4.5 Event ingestion + orchestration | routing, FEN, dedup, keyword, branding fan-out | high | F-01, F-02, F-19 |
| Final | retire Python transform loops | — | all of the above + activation flip |

Full detail: `feature-inventory.json` `migration_stage` per feature, and `FINDINGS.md`.
