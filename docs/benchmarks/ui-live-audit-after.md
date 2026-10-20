# Live dashboard audit — after

Same harness as the before run (`scripts/bench/ui_live_audit.mjs`), same build
type, same authenticated session, served locally with `/api` proxied to the live
API. Raw report: `ui-after.json` (regenerate with the command below).

| width | page overflow | clipped in scrollers | targets < 44 px | unnamed icon buttons | text < 12 px | min font | drawer | sheet |
|---|---|---|---|---|---|---|---|---|
| 360 | 0 | **0** (before 64) | **0** (before 17) | 0 | **0** (before 41) | **12 px** (before 10.4) | visible, closed | sidebar not docked |
| 768 | 0 | **0** (before 51) | **0** | 0 | **0** | 12 px | visible, closed | sidebar not docked |
| 1440 | 0 | **0** (before 25) | **0** | 0 | **0** | 12 px | **no trigger** | **rail docked** |

`PASS: every width is inside the UI budget`.

## Drawer behaviour (verified in Chromium at 360 px, not just measured)

- tap the trigger → `aria-expanded=true`, `clip-path: inset(0)`,
  `pointer-events: auto`, body scroll locked, scrim present;
- focus moves to the active nav item; the first Tab stays inside the drawer;
- Escape closes it, restores `clip-path: inset(0 0 0 100%)` and returns focus to
  the trigger (which is `inert`-free only while open).

This step is what caught the real defect the metrics could not: the drawer
trapped Tab but never moved focus in, so the first Tab walked the page behind it.

## RTL at 360 px (`fa`)

`dir="rtl"`, `lang="fa"`, `documentElement.scrollWidth = 360` (no horizontal
overflow), Persian UI strings rendered from the catalog, zero clipped elements.

## Reproduce

```
node scripts/bench/ui_live_audit.mjs \
  --url http://127.0.0.1:8123 \
  --widths 360,768,1440 \
  --token "$ATF_BENCH_TOKEN" \
  --out docs/benchmarks/ui-after.json
```

(`8123` was a preview server serving `web/frontend/dist` and proxying `/api` to
`https://api.legoten.com`; any deployment or preview works.)
