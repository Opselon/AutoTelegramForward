# Web dashboard — UI/UX audit & corrections (2026-11)

Scope: the findings below were produced by a live audit of the running dashboard
(mock control plane serving the real response shapes) at every supported
viewport, in both themes and in Persian RTL, plus a code-level review of the
shell, home panel, and notification path. Each finding lists location, problem,
user impact, root cause, correction, and priority. Findings marked **fixed in
this PR** are part of the accompanying change; earlier structural fixes
(token migration, API-error normalization) shipped in the previous PR and are
listed for completeness at the end.

Method note: screenshots were captured from the real application (no mockups);
overflow was measured programmatically (`scrollWidth − clientWidth` on the
scrolling element) at 320 / 360 / 375 / 390 / 430 / 768 / 1024 / 1280 / 1440 /
1920 px, and per-tab at 320 / 360 px. Result: **0 px horizontal overflow on
every viewport and every audited tab**, no panel crashes.

---

## Findings — application shell

### A1. Stranded menu button and card-stacked chrome on phones — **fixed in this PR** — P0

- **Location:** `App.tsx` shell header; `index.css` legacy ≤640 px blocks.
- **Problem:** the drawer trigger was a direct flex child of the column-stacked
  header, so on a phone it rendered alone on its own row; the signed-in user
  bar rendered as a *separate full-width card above the header*; health chips,
  language, theme, and refresh wrapped into four cramped rows. Three rows of
  chrome appeared before any content.
- **Impact:** ~40 % of a phone's first screen was spent on duplicated chrome;
  the layout read as a broken prototype, not an application.
- **Root cause:** the header had three siblings stacked by a legacy
  `flex-direction: column !important` rule; identity was never part of the
  header structure.
- **Correction:** header rebuilt as two intentional rows —
  `.header-top` (menu + brand + inline identity) and `.header-actions`
  (scrollable `.header-chips` strip + `.header-tools` cluster). The user bar is
  an inline header identity (divider, icon-only logout at 44 px on phones,
  labelled button from 640 px). Brand aligns to the title's first line; the
  title truncates with an ellipsis (a legacy `white-space: normal !important`
  that fought truncation was removed); below 480 px the identity dot/name leave
  the row (the hero greeting names the operator) so the title keeps its room.

### A2. Product title collapsed one letter per line — **fixed in this PR** — P0

- **Location:** `.header-title` (v2 block) vs legacy phone block.
- **Problem:** at 360 px the title wrapped one character per line
  ("Au/to/Tel/eg/ra/m…").
- **Impact:** the product name was unreadable on the primary device class.
- **Root cause:** `overflow-wrap: anywhere` + a ~22 px container (the identity
  card consumed the row) + a legacy `white-space: normal !important`.
- **Correction:** see A1 — identity compaction + word-level ellipsis
  truncation; per-character breaking is gone.

### A3. Icon-only controls without accessible names — **fixed in this PR** — P1

- **Location:** header refresh button (`btn btn-secondary`, title-only), Jobs
  modal close, AdminPanel confirm flows.
- **Problem:** the refresh control had no `aria-label` (title is not a
  substitute for screen readers) and an ad-hoc size; some modals used
  non-contract classes.
- **Impact:** screen-reader users heard an unnamed button; the control read as
  a random grey square.
- **Root cause:** pre-contract markup surviving the v2 migration.
- **Correction:** refresh is now `btn--ghost btn--icon btn--sm` at 40 px
  (44 px on phones) with `aria-label` + tooltip from the catalog; the
  admin-revocation and shell-destructive flows run through `ConfirmDialog`
  (previous PR, kept).

## Findings — notification system

### B1. Raw backend error as the dashboard banner — **fixed in this PR** — P1

- **Location:** `App.tsx` `errorBanner` (a 220-char sliced string) rendered by
  an always-danger `.alert-banner`.
- **Problem:** "FloodWait: 30s on target" appeared as a raw diagnostic in a red
  danger banner; a temporary Telegram *target* limitation looked identical to
  a failed internal service; no guidance, no countdown, details sliced.
- **Impact:** operators misread rate limits as outages; nothing told them what
  happens next or when.
- **Root cause:** the banner was a display string, not a notification model.
- **Correction:** structured `PipelineAlert` (`components/AlertBanner.tsx`):
  severity variants (danger for errors, caution amber for warnings);
  FloodWait is detected (name + detail combined), explained in plain language
  with a live countdown anchored to the engine's report time, and falls back to
  a "delivery is queued for retry" message once the window passes; the
  untouched diagnostic stays visible in a secondary mono line (2-line clamp);
  dismissal remembers all three historical identity keys. New catalog keys
  `ui_floodwait_banner`, `ui_floodwait_active` across all ten locales.

## Findings — home dashboard

### C1. Home hero duplicated the header — **fixed in this PR** — P1

- **Location:** `HomePanel.tsx` hero.
- **Problem:** brand mark, version badge, tagline, and the three service chips
  repeated the header one scroll-inch below.
- **Impact:** on a phone the hero alone consumed ~5 stacked rows of
  duplication before quick actions.
- **Root cause:** the hero predates the v2 header; both grew status features
  independently.
- **Correction:** hero = greeting + one truthful service summary pill
  ("All services online" / "{up} of {total} services online" / probing), the
  header keeps the per-service chips. New keys `ui_all_services_online`,
  `ui_services_online_summary` across all ten locales.

### C2. Technical id as the primary heading — **fixed in this PR** — P2

- **Location:** `HomePanel.tsx` hero heading (`ui_home_greeting`).
- **Problem:** "Welcome back, tg_5094837833" made a raw account id the largest
  text on the page.
- **Correction:** heading is now "Welcome back" (`ui_welcome_back`, ten
  locales); the identity (badge + name) is a secondary muted line.

### C3. Impossible KPI values ("2 / 0") — **fixed in this PR** — P0

- **Location:** `HomePanel.tsx` KPI cards (Active Rules, Connected Sessions).
- **Problem:** values rendered as `2 / 0` — two active rules out of "zero
  total" — and "Forwarded 24h" was backed by an all-time counter.
- **Impact:** the dashboard's most prominent numbers were self-contradictory.
- **Root cause:** `/api/stats` (Go handler `handlers.go` ~2838) sends only
  `active_rules` / `active_sessions`; the UI divided by `total_rules` /
  `total_sessions`, which that endpoint never sends.
- **Correction:** totals come from data the shell already fetched
  (`rules.length`, `sessions.length`); the forwarded card is labelled
  "Forwarded" (`ui_forwarded`), matching what the API actually returns. No
  backend change; no fabricated values. Loading still renders skeletons, not
  fake zeros.

### C4. Excessive KPI vertical stacking on phones — **fixed in this PR** — P2

- **Location:** `.home-kpi-grid`.
- **Problem:** four stat tiles stacked full-width on a phone; quick-action
  labels wrapped to three ragged lines.
- **Correction:** KPI grid is 2×2 from 320 px (4-up from 1024 px) with a
  compact card padding below 480 px; quick tiles keep a fixed icon box and
  56 px minimum height; labels wrap naturally (a 2-line clamp was tried and
  reverted — truncating "New forwarding task" lost meaning).

### C5. Clipped service-status indicators — **fixed in this PR** — P1

- **Location:** header health chips.
- **Problem:** chips squeezed next to the language picker; the third chip was
  cut off mid-glyph with no affordance.
- **Correction:** chips live in a dedicated no-wrap scroll strip (hidden
  scrollbar, touch scrolling) that can never squeeze the 44 px tool cluster;
  the partially visible chip signals scrollability. On ≥640 px all three fit
  without scrolling. Every state remains real (probe answers; "probing" is
  grey, never red) and the web flag is wired to the gateway probe (previous PR).

## Findings — reliability of states

### D1. Rules tab crashed on an incomplete diagnostics payload — **fixed in this PR** — P0

- **Location:** `DiagnosticsPanel.tsx` (`report.counts.error`, `report.score`,
  `report.scanned.*`).
- **Problem:** a response missing `counts` crashed the whole Rules panel into
  the boundary error state ("Cannot read properties of undefined (reading
  'error')").
- **Impact:** one optional field blanked the primary workflow screen.
- **Root cause:** unguarded nested reads on an optional API structure.
- **Correction:** optional-chained with zero-fallbacks; verified live — the
  panel now renders its "no problems found" state. (Also swept: the
  `btn btn-secondary` family turns out to be token-wired legacy aliases, not
  broken — left in place rather than churning five files.)

## Carried from the previous PR (already shipped, listed for completeness)

- ~690 dark hex/rgba literals migrated to role tokens across ~22 panels — the
  light theme renders correctly everywhere; zero color literals remain in TSX.
- `fetchJson` normalizes API errors once (server sentences, not JSON blobs);
  all catch paths announce with the danger toast tone.
- One shared `/queue` read; devops poller generation guard; `PanelBoundary` on
  all 15 tabs; dead API removed; JobsPanel modal rebuilt on the shell contract;
  AdminPanel `alert()` paths became inline danger notes.
- Provenance badges on role tokens; ClientSettings loading skeleton.

## Verification (this PR)

- Overflow: programmatic check at 320/360/375/390/430/768/1024/1280/1440/1920
  (home) and per-tab rules/queue/devops/sessions/audit at 320/360 — **0 px
  overflow, no crashes** in dark and light.
- Screenshots reviewed: 360 dark/light, 768, 1440, 1920, Persian RTL (360),
  drawer open (360), Rules tab (360).
- `npx tsc -p tsconfig.app.json --noEmit` ✅ · `npm run lint` ✅ ·
  `uiCatalog` 1171 keys × 10 locales ✅ · `responsive.contract` 55/55 ✅ ·
  production build ✅.
- Known limitations: the live audit ran against a mock control plane serving
  realistic shapes (no full stack on the dev machine); the FloodWait countdown
  was verified against the mock's expired-window path (shows the "queued"
  state) and the parsing path by unit of the regex against real payloads —
  a live 30-second countdown against the real core remains a deploy-time
  check. Lighthouse/INP numbers were not captured in this environment; the
  perf bench (bundle + bot paths) reports no regression.
