# UI/UX system contract — web, mobile, bot

One design language across the three surfaces. Numbers here are the acceptance bar; a screen that renders but
breaks one of these is not done. Keep this file in sync when a token or budget changes.

## 1. Design tokens

Shared *names* across surfaces (web CSS custom properties, Flutter constants, bot style registry). Values may
differ per platform; semantics may not.

| Token | Web | Flutter | Use |
|---|---|---|---|
| `space-1..6` | 4 / 8 / 12 / 16 / 24 / 32 px | `AppSpacing.xs..xxl` | all padding, gaps, margins |
| `radius-sm/md/lg/full` | 8 / 12 / 18 / 999 px | `AppRadius.sm..full` | cards, inputs, buttons, pills |
| `type-xs..2xl` | 12 / 13 / 14 / 16 / 20 / 24 / 32 px | `AppType.*` | body text is 14 (web) / 14sp (mobile) |
| `elev-1..3` | `--shadow-1..3` | `elevation 1..3` | card, popover, modal |
| `motion-fast/base/slow` | 120 / 200 / 320 ms | `AppDurations.*` | hover, expand, page change |
| `z-nav/toast/modal/tooltip` | 100 / 300 / 400 / 500 | n/a | fixed ladder, never ad-hoc numbers |

Colour is referenced by **role**, never by hex in a component: `surface`, `surface-2`, `surface-3`, `border`,
`text`, `text-muted`, `primary`, `primary-hover`, `success`, `warning`, `danger`, `info`, plus `*-soft` for
tinted backgrounds. A new hex literal inside a component is a review failure.

## 2. Breakpoints

| Surface | Compact | Medium | Expanded |
|---|---|---|---|
| Web | < 640 px (single column, stacked cards) | 640–1023 px (2 columns, drawer nav) | ≥ 1024 px (sidebar, 3 columns) |
| Mobile | < 600 dp (bottom NavigationBar) | 600–839 dp (NavigationRail) | ≥ 840 dp (rail + list/detail two-pane) |

Rules: content max-width 1440 px (web) / 720 dp (mobile single column), no horizontal scroll on the page body at
the smallest width, sticky header + sticky primary action, `env(safe-area-inset-*)` on the web shell,
`SafeArea` on mobile.

## 3. Component contract

Every surface ships the same six primitives, and screens use them instead of bespoke markup:

- **Card** — surface-2, radius-md, elev-1, 16 px padding, optional header row with a title and one action.
- **Button** — variants `primary`, `ghost`, `danger`, `link`; sizes `sm` (32 px / 36 dp) and `md` (40 px / 44 dp);
  disabled = 40% opacity + no pointer events; loading = spinner replaces the label, width locked.
- **Field** — label above, hint below, error text in `danger`, 44 px minimum control height; never a placeholder
  used as the only label.
- **Badge** — status pill: `success`, `warning`, `danger`, `info`, `neutral`, one line, truncates with tooltip.
- **Skeleton / Empty / Error** — a panel is never blank: skeleton while loading (3 rows minimum), empty state with
  an icon, one sentence and a call to action, error state with the reason and a Retry button.
- **Toast** — transient success/failure feedback, `aria-live="polite"` (web) / `SemanticsService.announce` (mobile);
  the bot equivalent is a catalog-resolved `cq.answer(...)` toast.

## 4. Interaction & accessibility

- Minimum target: 44×44 px (web), 48×48 dp (mobile); 8 px minimum gap between adjacent targets.
- Visible `:focus-visible` ring on every interactive element (2 px, offset 2, `primary`); keyboard order follows
  visual order; ESC closes overlays; modals trap focus and restore it on close.
- Contrast ≥ 4.5:1 for body text and ≥ 3:1 for large text and UI borders, in **both** themes.
- Icon-only controls carry an accessible name (`aria-label` / `Semantics(label:)` / Telegram button text).
- `prefers-reduced-motion` (web) and `MediaQuery.disableAnimations` (mobile) disable non-essential motion.
- Text scaling 0.8–1.3 must not clip: no fixed-height rows around text; wrap, don't truncate labels that carry meaning.
- RTL: `dir` flips with the locale; use logical properties (`margin-inline-*`, `inset-inline-*`, `EdgeInsetsDirectional`),
  mirror directional icons, keep numbers/latin identifiers LTR inside RTL text.

## 5. Performance budgets

| Metric | Budget |
|---|---|
| Web initial JS, gzip | ≤ 130 KB (baseline was 308 KB in one chunk) |
| Web any single chunk, gzip | ≤ 200 KB |
| Web route chunks | ≥ 6 |
| API p50 / p95 (user-scoped reads) | ≤ 5 ms / ≤ 15 ms |
| `health/services` warm p50 | ≤ 10 ms (was 52 ms) |
| `logs/stats` p95 at 100k rows | ≤ 5 ms (was 51 ms) |
| Bot menu build (40 buttons) | ≤ 2 ms |
| Rule resolution at 1000 rules | ≤ 1 ms |
| Long list rendering | virtualized: no more than ~50 mounted rows |
| Mobile debug build time | no regression > 10% vs baseline |

Measure with `python3 scripts/bench/ui_perf_bench.py --label <name>`; gate with
`python3 scripts/bench/ui_perf_compare.py <before.json> <after.json>` (fails at +15% latency, +5% bundle).

## 6. Definition of done (per screen)

1. Renders at 360 px / 768 px / 1440 px (web) or compact/medium/expanded (mobile) with no overflow or clipping.
2. Uses design tokens and shared primitives; no new hex values, no ad-hoc spacing.
3. Has loading, empty and error states, plus a retry path.
4. Keyboard/screen-reader reachable; focus ring visible; target sizes honoured.
5. Every string resolves through the ten-locale catalog; no literals; RTL verified for `fa`.
6. Covered by a test: widget test (mobile), component/contract test (web), pytest (bot), plus the perf gate when the
   screen touches a hot path.
