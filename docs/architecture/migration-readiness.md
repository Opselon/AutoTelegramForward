# Migration Readiness — Stage 4 blueprint

**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`. Audit-only.
This is a plan, not an implementation. Nothing in this PR changes runtime behavior.

Per-feature readiness: `docs/behavior-audit/feature-inventory.json`
(`migration_stage` per feature). Findings: `docs/behavior-audit/FINDINGS.md`.

---

## Readiness matrix

| Stage | Scope | Features | Python today | Go today | Parity risk | Compat risk |
|---|---|---|---|---|---|---|
| **4.1** | FilterRule + ContentPolicy | FLT×9, POL×7, MED×2 | full | filter only; BlockLinks never populated (F-12); order differs (F-11) | **high** | medium |
| **4.2** | Cleaner + emoji | CLN×6, UNI×2 | full | **unreferenced** (F-08); emoji INCOMPATIBLE (F-03) | **high** | high |
| **4.3** | Replace | RPL×5 | full | absent | medium | medium |
| **4.4** | Links + advanced transforms | LNK×7, ADV×8, AI-001 | full | absent (F-18); placeholders dead (F-15) | medium | medium |
| **4.5** | Event ingestion + orchestration | RT×2, EVT, DUP×3, FEN, KW, BRD, ALB | full | claim/fence only; dedup Python-only (Agent C P-1) | **high** | high |
| **Final** | Retire Python loops | — | — | — | — | — |

## Recommended sequence (dependency-ordered, not the brief's suggestion)

The brief proposed 4.1 → 4.5 with ownership transfer at the end. The audit found one
dependency that reorders it: **Stage 4.5's event-level dedup (content fingerprint) is
Python-only and the Go fence keys on source id** (Agent C P-1, confirmed medium). As long
as the Python pipeline owns ingestion, the fingerprint gate and the fence overlap
correctly; moving ingestion first would open a duplicate-delivery window. Keep 4.5 last.

1. **4.1 — FilterRule + ContentPolicy.** Port ContentPolicy evaluation (the chain, the
   three whitelist modes, the per-list switches) and fix F-12 (project `block_links` into
   the Go rule) and F-11 (align the check order). F-14's three link predicates must become
   one shared predicate before this stage can be called complete — otherwise the two
   engines keep disagreeing about what a link is.
2. **4.2 — Cleaner + emoji.** F-03 is the blocker: the pattern is wrong in both languages
   and Go RE2 cannot represent Unicode property classes the way Python `re` can. The fix
   needs a design decision (a shared, property-based pattern vs an explicit code-point
   table) and cross-language fixtures — `fixtures.json` UNI-* pins current (broken)
   behavior so the change is visible. F-09 (caption) is decided here too.
3. **4.3 — Replace.** Port `text_replace` semantics — smart case, stable priority order,
   backreference handling, the four storage shapes. F-04 precedence (`remove_links` vs
   `link_replacement`) must be settled to one documented rule first.
4. **4.4 — Links + advanced transforms.** Port `LinkManagementEngine` whole (F-18), wire
   `filter_inline_keyboard` for the first time (F-16 is a missing feature, not a port),
   and make header placeholders reach the durable path (F-15).
5. **4.5 — Event ingestion + orchestration.** Port routing, FEN, dedup, keyword reply,
   album handling, and the fan-out. Retire the Python in-memory auto-delete loop (F-19)
   and decide `dedupe_policy` (implement or drop the column — F-01).
6. **Final.** Flip Worker activation to default in a separately reviewed release; retire
   the Python transform loops.

## Highest-risk dependencies

| Risk | Why | Blocks |
|---|---|---|
| F-03 emoji pattern | wrong in both languages; Go RE2 lacks the Unicode property syntax Python uses | 4.2 |
| F-08 transform package | the Go code exists and looks complete; wiring it would silently change behavior because it has never run | 4.2, 4.3, 4.4 |
| F-12 BlockLinks | a stored switch that silently stops working when the worker takes over | 4.1, activation flip |
| Python-only content dedup | moving ingestion before porting the fingerprint opens a duplicate window | 4.5 ordering |
| F-15 placeholder preview | the gRPC TestRule preview shows behavior production does not deliver; any fix changes preview output | 4.4, 4.3 |
| Captions via `effective_text` (F-09) | cleaner and replace both see the caption through a property, not a flag | 4.2, 4.3 |

## Rollback strategy (per stage)

Every stage is independently reversible while Python remains the ingest owner: revert the
Worker activation flag and the Python pipeline resumes full ownership. This is the safety
net the current architecture buys, and it expires at the Final stage — which is why the
activation flip is a separate release.

## Migration backlog (fix PRs, separate from this audit)

| Priority | PR | Finding | Acceptance |
|---|---|---|---|
| P0 | project `block_links` into the Go filter projection | F-12 | fixture FLT-004: link message dropped on both paths |
| P0 | one shared "contains a link" predicate | F-14 | LNK-GATE-01/02 pass on both engines |
| P1 | Unicode-correct shared emoji pattern | F-03 | UNI-EMOJI-01..05 flip to the fixed expectation |
| P1 | header placeholders reach the durable path | F-15 | ADV-HEADER-01 output matches the preview |
| P2 | mention `{4,64}` vs `{3,32}`, hashtag `#\w+` vs `#[^\s#@]+`, phone regex | F-07 | CLN-MENTION-01, CLN-HASHTAG-01, CLN-PHONE-01 |
| P2 | `remove_links` / `link_replacement` precedence | F-04 | LNK-REMOVE-LINKS-01 single output |
| P2 | auto-delete bounds | F-17 | ADV-AUTODELETE-01 one bound |
| P3 | album TTL reads the rule field | F-02 | dispatcher reads `album_aggregation_window_seconds` |
| P3 | decide `dedupe_policy` (implement or drop) | F-01 | a setting named in the UI changes behavior or is removed |
| P3 | wire `filter_inline_keyboard` or remove it | F-16 | a link policy strips inline URL buttons |
