# Agent F3 — Settings/persistence consistency audit + Stage 4.1 design

Repo /root/AutoTelegramForward, branch `audit/complete-business-logic-inventory` (PR #289, head c74498d, CLEAN, 16/16 green). Read-only audit + docs. **Do not merge, deploy, touch schemas, or edit production code.**

## Your scope (files only you create)
1. `docs/behavior-audit/settings-persistence-audit.md`
2. `docs/architecture/stage-4.1-implementation-plan.md`

**No other file.** Never edit `fixtures.json`, `feature_catalog.json`, or another agent's file.

## Task 1 — settings persistence audit (read-only)
For all 89 settings in `docs/behavior-audit/settings-inventory.json`, verify from source each one's persistence and control-surface reach:
- SQLite: `core/infrastructure/persistence/database.py` (MIGRATIONS, max version 33) + `sqlite_repositories.py`
- PostgreSQL: `core/infrastructure/persistence/postgres_repositories.py` + `scripts/postgres/schema.sql`
- Proto: `proto/autoforward.proto` + `core/proto/autoforward_pb2.py`
- Go API: `api/internal/handlers/`
- Bot/Web/Mobile exposure (grep only — pro_bot_ui.py is 13k lines, outline it)
- Backup/restore: `core/application/backup_service.py`

For each setting record exactly one persistence status: `STORED_AND_CONSUMED`, `STORED_BUT_UNUSED`, `RUNTIME_ONLY`, `UI_ONLY`, `INHERITED` (bot defaults, `core/domain/bot_defaults.py` + `core/application/effective_settings.py`), `DERIVED`, `DEPRECATED`, or `DUPLICATED`.

Focus on the brief's Phase F list: `dedupe_policy` (F-01), `block_links` (F-12), bound disagreements (F-17), `album_aggregation_window_seconds` (F-02), `auto_delete_seconds`, `forward_delay_seconds`, `apply_to_caption` (F-09), and all link-related settings (`link_policy`, `domain_allowlist`, `domain_blocklist`, `link_rewrite_map`, `link_replacement`, `remove_links`, `blocked_media_types=link`, `FilterRule.block_links`).

**Answer the critical question for F-12:** does a `block_links` column exist on `filter_rules` in EITHER database? Grep `CREATE TABLE filter_rules` and every `ALTER TABLE filter_rules` in `database.py`, and the Postgres schema. Report the exact column list of `filter_rules` for both stores.

## Task 2 — Stage 4.1 implementation plan (design only, no code)
Write `docs/architecture/stage-4.1-implementation-plan.md` covering 4.1a (BlockLinks projection, F-12), 4.1b (canonical link classification, F-14), 4.1c (evaluation order alignment, F-11). For each: exact files likely to change, required migrations (SQLite + Postgres), acceptance tests, risks, rollback, post-implementation ownership, and the dependency order between a/b/c. Include a compatibility matrix (SQLite/Postgres/Go store/Python repos/proto/API/worker/tests/backup-restore) and define backward compatibility + rollout. Explicitly state Stage 4.1 is planned, not implemented. Read `docs/behavior-audit/FINDINGS.md` and `docs/architecture/migration-readiness.md` first so the plan is consistent with verified findings — especially that `block_links` is a task-level setting (`ForwardRule.block_links`, metadata) which ALSO exists on the FilterRule dataclass, and that the pipeline gates it at `pipeline.py:957` via `link_block_reason`.

## Task 3 — report
Files inspected (with line refs for the column lists), files changed, exact evidence for each persistence status, remaining uncertainty. Do not recommend storing new fields unless runtime/operational value is proven.
