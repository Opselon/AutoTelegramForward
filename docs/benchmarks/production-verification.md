# Production verification — `sha-06a4420`

Post-merge state of `master` (`06a4420`, the five PRs #216–#220) as deployed on
the live stack. Same harnesses as the pre-merge runs; URLs point at production.

## Live layout audit (authenticated session, `https://api.legoten.com`)

| width | overflow | clipped | targets < 44 px | unnamed | text < 12 px | min font | drawer | rail |
|---|---|---|---|---|---|---|---|---|
| 360 | 0 | 0 | 0 | 0 | 0 | 12 px | visible, closed | not docked |
| 768 | 0 | 0 | 0 | 0 | 0 | 12 px | visible, closed | not docked |
| 1440 | 0 | 0 | 0 | 0 | 0 | 12 px | **no trigger** | **docked** |

`PASS: every width is inside the UI budget`. Raw: `ui-after-production.json`.

The locale split is real in production: `assets/fa-B7nM-JBT.js` (60.4 KB) and
`assets/zh-ZtlGj6td.js` are fetched on demand, not in the initial payload.

## API over the public HTTPS edge (`https://api.legoten.com`, 80 samples)

| endpoint | p50 | p95 | p99 |
|---|---|---|---|
| `/api/v1/health/services` | 5.78 ms | 11.50 ms | 17.46 ms |
| `/api/logs/stats` | 4.85 ms | 8.38 ms | 13.81 ms |
| `/api/rules` | 6.15 ms | 8.73 ms | 19.32 ms |
| `/api/tasks` | 5.14 ms | 9.77 ms | 17.42 ms |
| `/api/version` | 4.87 ms | 7.86 ms | 8.26 ms |

These include TLS + the Caddy/web proxy hop, so they are not comparable to the
localhost numbers in `hot-paths.md`; the before/after comparison for the API
itself stays the localhost one (52.13 → 0.49 ms p50 for health).

`health/services` reports its own freshness (`cached`, `age_ms`): under a
continuous poll the snapshot stays inside the 2 s TTL; a request that arrives
after an idle gap gets the previous snapshot immediately and a refresh behind it,
with `age_ms` telling the client how old the answer is.

## Reproduce

```
node scripts/bench/ui_live_audit.mjs --url https://api.legoten.com \
  --widths 360,768,1440 --token "$ATF_BENCH_TOKEN" \
  --out docs/benchmarks/ui-after-production.json

python3 scripts/bench/ui_perf_bench.py --http-only --http-runs 80 \
  --label post-deploy --base https://api.legoten.com
```
