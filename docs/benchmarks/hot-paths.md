# Hot paths: measured before/after

Every number below comes from a command in this document, run against the same
stack and the same data. No figure is an estimate.

## 1. `GET /api/v1/health/services` — 52 ms → 0.49 ms

**Root cause (not "the database is slow")**: the probe loop waited with
`time.After(50 * time.Millisecond)` between checks, so every request paid a
fixed ~50 ms tax even when core answered in 3 ms. The logger probe ran
sequentially after it, and each request re-probed everything.

**Fix** (`api/internal/handlers/health_probe.go`): probes run concurrently under
one 4.5 s deadline and the snapshot is cached for 2 s with a singleflight
refresh — a burst of dashboard polls triggers one probe round, not one per
request. A background refresh finishes even if the client disconnects, so the
next poll is warm. `ATF_PROBE_TTL_MS` overrides the TTL; failures are never
cached longer than the same 2 s, so an outage still shows up immediately.
Response shape is unchanged (`services`, `status`, `time`); `cached` and
`age_ms` are additive.

| | before | after |
|---|---|---|
| p50 | 52.13 ms | **0.49 ms** |
| p95 | 64.09 ms | **0.77 ms** |
| p99 | 122.17 ms | **1.98 ms** |
| req/s (serial) | 18.5 | **1891.1** |
| cold first call after restart | ~52 ms | **8.8 ms** |

```
python scripts/bench/ui_perf_bench.py --http-only --http-runs 80 --label before-80 --base http://127.0.0.1:8080
python scripts/bench/ui_perf_bench.py --http-only --http-runs 80 --label after-api-80
```

Cold path is the first call in a fresh process (container restarted, cache
empty, all four probes in flight): `0.008829 s`, `cached=false`; the next call
`0.00078 s`, `cached=true age_ms=25`.

## 2. `GET /api/logs/stats` — 41.8 ms → 1.73 ms

The endpoint counted rows by re-reading the whole log table on every poll.
It now reads a cached aggregate and the counts are backed by an index; the
`logs_tail` list benefits from the same index work.

| | before | after |
|---|---|---|
| p50 | 41.82 ms | **1.73 ms** |
| p95 | 43.74 ms | **2.61 ms** |
| p99 | 44.29 ms | **4.70 ms** |
| req/s | 39.7 | **559.9** |

`/api/tasks` 2.01 → **1.45 ms** p50 (p95 3.05 → 1.88), `/api/logs?limit=50`
p95 4.75 → 4.45 ms, `/api/version` 0.70 → 0.46 ms.

`/api/rules` and `/api/rules/settings/schema` are **unchanged**, not faster and
not slower: an 80-sample sweep showed ±30 % swings in both directions, so they
were re-measured interleaved (150 samples alternating between the two builds):

| endpoint | prod p50 / p95 / p99 | candidate p50 / p95 / p99 |
|---|---|---|
| `/api/rules` | 2.17 / 5.08 / 8.03 ms | 2.15 / 4.86 / **5.72** ms |
| `/api/rules/settings/schema` | 2.46 / 4.37 / 6.93 ms | 2.47 / 4.11 / **6.49** ms |

That interleaving matters: the 40-sample sweep that `ui_perf_compare.py` runs by
default reported rules_list p95 as +46 %; at 150 interleaved samples the tail is
*better*. Tails need ≥80 samples per endpoint, which is why the defaults in
`ui_perf_bench.py` were raised to 80.

## 3. Bot menu build — 0.058 ms → 0.015 ms, 1 DB read → 0

Every `/start`, menu callback and "back" button rebuilds ~20 buttons. The build
re-resolved the language from SQLite on each render, recompiled the style
patterns per button and re-formatted every placeholder per call.

`I18n` now merges each locale's table once, keys the format cache by resolved
locale (not `id(table)`, whose ids are recycled) and `ProBotUI`/`ui_styles`
memoize the compiled patterns; the language is resolved from the client's state
instead of the database.

| | before | after |
|---|---|---|
| menu build p50 | 0.058 ms | **0.015 ms** |
| menu build p95 | 0.066 ms | **0.021 ms** |
| DB reads / build | 1.00 | **0** |
| `I18n.t()` | 0.359 µs | **0.211 µs** |
| button text + style | 2.016 µs | **0.436 µs** |

Budget is 2 ms (`docs/ui-ux-system.md` §5); the gate is
`core/tests/test_bot_menu_render_perf.py`, which measures the real builders and
fails if a rebuild reintroduces a DB read or a per-button regex pass.

## 4. Rule matching on 1 000 rules

`list_active_by_source` scanned all rules and evaluated a correlated
`json_each` sub-query per row. It is now a compound query that uses
`idx_rules_extra_sources` for the extra-source branch, and the dedupe/map probes
are covered by their own indexes.

```
python scripts/bench/rule_resolution_bench.py --rules 1000 --processed 100000 --map 50000
```

| lookup | before p50 | after p50 |
|---|---|---|
| `list_active_by_source` | 41.45 ms | **0.0014 ms** |
| `was_processed` (100 k rows) | 0.0038 ms | 0.0037 ms |
| **`resolve_message_total`** | **44.53 ms** | **1.22 ms** |
| `matching_rules` (1 000 rules on one chat) | 1.19 ms | 1.22 ms |

Honest caveat: the remaining 1.2 ms is the Python-side `matching_rules` filter
over *every* rule in the fixture's single hot chat, i.e. ~1.2 µs per rule. That
is O(rules watching the source) — realistically tens, not a thousand — so the
budget for it is stated per rule rather than promising "1 ms at 1 000 rules".
The queries are index seeks; `EXPLAIN QUERY PLAN` is printed by the bench so a
future scan is visible.

## 5. Gates

- `go build ./...`, `go vet ./...` clean; `go test ./...` (including the new
  `health_probe_test.go` and `stats_cache_test.go`) passes.
- `pytest core/tests/test_bot_menu_render_perf.py core/tests/test_i18n.py
  core/tests/test_sqlite_repositories.py core/tests/test_bot_registry_*
  core/tests/test_registry_* core/tests/test_ui_values_come_from_the_catalog.py
  core/tests/test_no_raw_catalog_key_reaches_the_user.py` → 253 passed.
- Web/mobile/telegram copy gates and `audit_cross_language.py` unchanged and
  passing (no user-facing string changed in this branch).
