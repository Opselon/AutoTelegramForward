# ATF Web Dashboard (frontend)

React 19 + Vite (Rolldown) + TypeScript. No UI framework: one hand-rolled
design system in `src/index.css`, six shared primitives in
`src/components/ui/Primitives.tsx`, and a ten-locale catalog that every visible
string must resolve through.

The gateway embeds this folder's `dist/` output (`web/backend`, rust-embed) —
`npm run build` is the only thing the Docker image needs from here.

## Design system

- **Tokens** (`index.css`, "DESIGN SYSTEM v2" block): colour by *role*
  (`--color-*`), `--space-1..6`, `--radius-*`, `--type-xs..2xl`, `--shadow-1..3`,
  `--motion-*`, z-ladder. `docs/ui-ux-system.md` §1 is the contract; the test
  `tests/responsive.contract.test.mjs` enforces it, including 4.5:1 text
  contrast **per theme**.
- **Two themes, one attribute.** `<html data-theme="dark|light">` switches a
  single token-override block. Components never read the theme and never store
  accent shades — derived glows/borders use
  `color-mix(in oklab, var(--color-x) NN%, transparent)`. `src/theme.ts` is the
  only writer; `index.html` applies the stored choice pre-paint.
- **Primitives**: `Card`, `Button` (typed icon-only), `Field`, `Badge`,
  `SkeletonRows`, `EmptyState`/`ErrorState`, `Toast`, plus `ModalShell` for
  overlays (focus trap, Escape, scroll lock). Panels compose these; a new hex
  literal or ad-hoc spacing in a panel is a review failure.

## Rules that CI enforces (do not fight them)

1. **No hardcoded copy.** Every user-facing string is `t('key')` from
   `src/locales/`, defined in all ten locales (`en fa pt uz ru id es fr it zh`).
   Gates: `scripts/audit_web_ui_copy.js` (repo root) + `tests/uiCatalog.test.mjs`.
2. **Contract test** (`tests/responsive.contract.test.mjs`): tokens present,
   logical properties only, 44 px targets, drawer clip behaviour, 12 px type
   floor, per-theme contrast, shell a11y landmarks.
3. **Chunk budgets** (ui-ux-system.md §5): initial JS ≤ 130 KB gzip, no chunk
   > 200 KB. `react` and `lucide-react` are pre-split vendor chunks; panels are
   route-level lazy chunks.

## Commands

```bash
npm ci
npm run dev        # vite on :3000, /api proxied to the gateway on :8088
npm run build      # dist/ for the gateway to embed
npm run lint       # oxlint

# node-run contract tests (see each test header for its tsc prelude)
node tests/responsive.contract.test.mjs
```
