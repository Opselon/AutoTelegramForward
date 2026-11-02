# Behavior Audit — Stage 4.0 evidence bundle

**Baseline:** master `35ee530cc80cd17d1ac7d702c4e5f5ea8024889a`
(Stage 1 = PR #284, Stage 2 = PR #286, Stage 3 = PR #288 — all merged)
**Scope:** discovery-only. No engine rewrite, no schema change, no deploy, no merge.

## Method

Four parallel agents worked non-overlapping scopes (brief §22): filters/content-policies,
cleaner/replace, routing/pipeline, advanced-transforms/surfaces. **Every agent claim was
re-verified by the coordinator against source or by execution** before it entered the
shared register. Agent self-reports that did not survive verification were downgraded or
rejected — see the `DOWNGRADED` section of `FINDINGS.md`.

## Files

| File | Content |
|---|---|
| `COORDINATOR_VOCABULARY.md` | shared feature-ID scheme, the five-engine synthesis, verified pipeline orders |
| `FINDINGS.md` | initial 19 coordinator-verified findings (F-01 … F-19) |
| `FINDINGS_STATUS.md` | **Final status register for F-01 through F-21** (verified by execution), Phase B.1 emoji double-processing resolution |
| `feature-inventory.json` | 64 features: owner, runtime path, status, evidence, migration stage |
| `settings-inventory.json` | all 89 settings (76 `SETTING_SPECS` + 13 first-class) |
| `settings-persistence-audit.md` | Phase F read-only persistence audit of all 89 settings across SQLite/PostgreSQL/Proto/API |
| `pipeline-order.json` | source-verified stage order for all 5 paths (37 stages) |
| `fixtures.json` | 43 deterministic characterization fixtures with verified production expectations |
| `fixture-coverage.json` | Phase D machine-readable fixture coverage report (37 executable, 12 Go-mapped, 8 divergences) |
| `fixture-coverage.md` | Phase D human-readable fixture coverage and reachability gap report |
| `diagrams/event-ingestion.mmd` | Mermaid sequence diagram, update → confirmed delivery |
| `diagrams/message-processing.mmd` | processing-stage flow with dead-code callouts |
| `diagrams/parity-map.mmd` | Python/Go ownership and divergence map |
| `_handoff/a-filters-content-policies.md` | Agent A: 76-setting registry, FilterRule contract, ContentPolicy, media, routing |
| `_handoff/b-cleaner-replace.md` | Agent B: cleaner, emoji/unicode, replace engine |
| `_handoff/c-routing-pipeline.md` | Agent C: event lifecycle, pipeline order, dedup, FEN, keyword, branding |
| `_handoff/d-advanced-surfaces.md` | Agent D: advanced settings, links, AI, delivery, 7 control surfaces |
| `_handoff/_fixture-contract.md` | Shared schema, routing tokens, and integrity gates for test runners |

The human-readable architecture documents that consume this bundle live in
`docs/architecture/`:
- `business-logic-inventory.md`
- `behavior-contract.md`
- `feature-ownership-matrix.md`
- `processing-pipeline.md`
- `feature-dependency-graph.md`
- `migration-readiness.md`
- `stage-4.1-implementation-plan.md` (Phase G Stage 4.1 readiness plan)

Executable test infrastructure:
- `core/tests/audit/` (Python fixture schema validator & executable runner — 96 passing tests)
- `worker/internal/audit/` (Go fixture loader, schema validator, and unsupported feature reporter)
- `worker/internal/engine/filter/filter_fixture_test.go` (Go FilterRule executable tests)
- `worker/internal/engine/transform/transform_fixture_test.go` (Go transform/emoji executable tests)

## The headline discovery

There are **five independent text/decision systems**, not one — and the pre-existing
documentation described only one of them. `FilterEngine.transform_text`
(`services.py:194`) is *not* the durable pipeline's transform path; the durable path uses
`CleanerRule.clean()` + `LinkManagementEngine` + `ContentPolicy.transform()`. System 5's
real production callers are edit-sync and the gRPC `TestRule` preview only.

## Fixture principles

Synthetic, deterministic, privacy-safe (no real chat ids or tokens), independent of live
Telegram accounts and production database rows, and reusable across Python and Go —
every fixture names its engine and, where the engines disagree, gives both expected
outputs so the divergence stays visible.
