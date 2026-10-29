/**
 * Responsive + design-system contract, asserted from source without a browser.
 *
 * The live audit (`scripts/bench/ui_live_audit.mjs`) proves the built dashboard
 * behaves at 360/768/1440 with real data. This test is the cheap, deterministic
 * half that runs in CI on every commit: it fails when the *rules* that make the
 * audit pass are deleted — a token renamed, a breakpoint dropped, a physical
 * `margin-left` reintroduced into the RTL-sensitive layer, a 10px font size put
 * back into a component, the drawer turned into an off-canvas translate.
 *
 * Run: node tests/responsive.contract.test.mjs
 */
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
// Windows checkouts carry CRLF; the assertions below match LF-joined snippets,
// so the source is normalized before any string math (content is unchanged).
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n')
const css = read('src/index.css')

let checks = 0
const ok = (name, condition, detail = '') => {
  checks += 1
  if (!condition) {
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
    process.exitCode = 1
  } else {
    console.log(`ok   ${name}`)
  }
}

/* Everything from the design-system marker on is the new layer: tokens,
   primitives, shell and breakpoints. Assertions about RTL and hex apply there,
   because that is the code this change owns (the legacy sheet above it is being
   retired panel by panel and is not yet logical-property clean). */
const marker = 'DESIGN SYSTEM v2'
ok('design-system layer is present', css.includes(marker))
const layer = css.slice(css.indexOf(marker))
const tokenBlock = layer.slice(layer.indexOf(':root {'), layer.indexOf('\n}', layer.indexOf(':root {')))

/* ---- 1. the token contract from docs/ui-ux-system.md §1 ---- */
const REQUIRED_TOKENS = [
  '--space-1', '--space-2', '--space-3', '--space-4', '--space-5', '--space-6',
  '--radius-sm', '--radius-md', '--radius-lg', '--radius-full',
  '--type-xs', '--type-sm', '--type-base', '--type-md', '--type-lg', '--type-xl', '--type-2xl',
  '--shadow-1', '--shadow-2', '--shadow-3',
  '--motion-fast', '--motion-base', '--motion-slow',
  '--z-nav', '--z-toast', '--z-modal',
  '--color-bg', '--color-surface', '--color-text', '--color-text-muted',
  '--color-primary', '--color-success', '--color-warning', '--color-danger',
]
const missingTokens = REQUIRED_TOKENS.filter((name) => !tokenBlock.includes(`${name}:`))
ok(`all ${REQUIRED_TOKENS.length} required tokens are declared`, missingTokens.length === 0, missingTokens.join(', '))

/* The smallest type is 12px and it is a token, not a magic number per panel. */
ok('type floor is a token worth 12px', /--type-xs:\s*12px/.test(tokenBlock))

/* ---- 2. no hex outside the token block ---- */
const withoutTokens = layer.replace(tokenBlock, '')
const hexOutside = withoutTokens.match(/#[0-9a-fA-F]{3,8}\b/g) ?? []
ok('no hex colour outside the token block', hexOutside.length === 0, hexOutside.slice(0, 5).join(' '))

/* ---- 3. logical properties only (RTL correctness, spec §4) ---- */
const PHYSICAL = [/(^|\s)margin-left:/m, /(^|\s)margin-right:/m, /(^|\s)padding-left:/m,
  /(^|\s)padding-right:/m, /(^|\s)border-left:/m, /(^|\s)border-right:/m]
const physical = PHYSICAL.filter((re) => re.test(withoutTokens))
ok('design-system layer uses logical properties only', physical.length === 0)

/* ---- 4. breakpoints: 640 / 768 / 1024 ---- */
const media = [...layer.matchAll(/@media \(([^)]+)\)/g)].map((m) => m[1])
for (const bp of ['max-width: 639px', 'min-width: 640px', 'max-width: 767px', 'max-width: 1023px', 'min-width: 1024px']) {
  ok(`breakpoint ${bp} exists`, media.includes(bp))
}

/* ---- 5. the drawer is a clip, not an off-canvas translate ----
   A translated drawer in RTL sits past the right edge and is measured as
   horizontal overflow (and inflates scrollWidth); clipping never leaves the
   viewport. This is the regression that took the RTL audit from 1 to 0. */
// Slice the *docked/overlay* rule, not the base rule (the toast's centring
// translate sits in between and is not part of the drawer).
const drawerRule = layer.slice(layer.indexOf('position: fixed;\n    inset-block: 0;'), layer.indexOf('.app-sidebar.is-open'))
ok('drawer hides with clip-path', drawerRule.includes('clip-path: inset('))
ok('drawer does not translate off-canvas', !/translateX\(/.test(drawerRule))
ok('drawer has an open state', layer.includes('.app-sidebar.is-open'))

/* ---- 6. tables collapse to labelled cards below 768px ---- */
ok('responsive table class exists', layer.includes('table.responsive-table'))
ok('table cells carry their column label', /content:\s*attr\(data-label\)/.test(layer))

/* ---- 7. 44px targets and a visible focus ring ---- */
ok('buttons declare a 44px block size', /min-block-size:\s*44px/.test(layer))
ok('checkboxes keep a 44px hit area', /input\[type='checkbox'\][\s\S]{0,400}inline-size:\s*44px/.test(layer))
ok('focus-visible ring is defined', layer.includes(':focus-visible'))
ok('reduced motion is honoured', layer.includes('prefers-reduced-motion: reduce'))

/* ---- 8. the 12px floor is not re-broken in components ----
   The before-audit found 41 text nodes at 10.4px, all from inline `fontSize`.
   A new sub-12px literal anywhere in a component fails here, not in a browser. */
const tsxFiles = []
const walk = (dir) => {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full)
    else if (entry.endsWith('.tsx')) tsxFiles.push(full)
  }
}
walk(join(ROOT, 'src'))
const tiny = []
for (const file of tsxFiles) {
  const text = readFileSync(file, 'utf8').replace(/\r\n/g, '\n')
  for (const m of text.matchAll(/fontSize:\s*'([0-9.]+)rem'/g)) {
    if (Number(m[1]) < 0.75) tiny.push(`${file}: ${m[0]}`)
  }
  for (const m of text.matchAll(/fontSize:\s*(\d+)\b/g)) {
    if (Number(m[1]) < 12) tiny.push(`${file}: ${m[0]}`)
  }
}
ok(`no component sets type below 12px (${tsxFiles.length} files scanned)`, tiny.length === 0, tiny.slice(0, 4).join(' | '))

/* ---- 9. the panels actually use the primitives ---- */
const primitives = read('src/components/ui/Primitives.tsx')
for (const name of ['Card', 'Button', 'Field', 'Badge', 'SkeletonRows', 'EmptyState', 'ErrorState', 'Toast']) {
  ok(`primitive ${name} is exported`, new RegExp(`export function ${name}\\b`).test(primitives))
}
ok('primitives carry no hex colours', !/#[0-9a-fA-F]{3,8}\b/.test(primitives))

const consumers = [
  ...readdirSync(join(ROOT, 'src/panels')).map((f) => `src/panels/${f}`),
  'src/components/Navigation.tsx',
  'src/components/PanelBoundary.tsx',
]
const users = consumers.filter((rel) => read(rel).includes("from '../components/ui/Primitives'"))
ok('panels import the shared primitives', users.length >= 5, `${users.length} of ${consumers.length}`)

/* ---- 10. shell accessibility landmarks ---- */
const app = read('src/App.tsx')
ok('skip-to-content link exists', app.includes('skip-link'))
ok('toast live region is always mounted', /aria-live="polite"/.test(app))
ok('main landmark is focusable for the skip link', /id="main-content"[^>]*tabIndex=\{-1\}/.test(app))

const nav = read('src/components/Navigation.tsx')
ok('navigation is labelled', nav.includes('aria-label'))
ok('drawer trigger reports expansion', nav.includes('aria-expanded') && nav.includes('aria-controls'))

/* ---- 11. contrast: the dimmest text token still clears 4.5:1 ----
   The audit cannot see contrast, and the panels used to paint their captions in
   `#64748b` — 3.7:1 on a card, an AA failure under spec §4. It is now the
   `--color-text-muted` token, and this check keeps a new literal out. */
const relativeLuminance = (hex) => {
  const value = hex.replace('#', '')
  const channel = (i) => {
    const c = parseInt(value.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4)
}
const contrast = (a, b) => {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
const tokenValue = (name) => (tokenBlock.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`)) ?? [])[1]
for (const [fg, bg] of [['--color-text', '--color-bg'], ['--color-text', '--color-surface'],
  ['--color-text-muted', '--color-surface'], ['--color-text-subtle', '--color-surface']]) {
  const a = tokenValue(fg)
  const b = tokenValue(bg)
  ok(`${fg} on ${bg} clears 4.5:1`, a && b && contrast(a, b) >= 4.5, a && b ? `${contrast(a, b).toFixed(2)}:1` : 'token missing')
}
const dimLiterals = []
for (const file of tsxFiles) {
  const text = readFileSync(file, 'utf8')
  for (const dim of ['#64748b', '#475569']) if (text.includes(dim)) dimLiterals.push(`${file}: ${dim}`)
}
ok('no component reintroduces a sub-4.5:1 text colour', dimLiterals.length === 0, dimLiterals.slice(0, 3).join(' | '))

/* ---- 12. the new strings exist in ten catalogs, not in code ----
   Every key this change introduced must resolve in all ten locales; the copy
   gate proves the rest, this proves the shell's own keys were not forgotten. */
const LANGS = ['en', 'fa', 'pt', 'uz', 'ru', 'id', 'es', 'fr', 'it', 'zh']
const NEW_KEYS = ['ui_open_navigation', 'ui_close_navigation', 'ui_skip_to_content',
  'ui_panel_error_title', 'ui_panel_error_body', 'ui_panel_loading', 'ui_empty_title',
  'ui_empty_body', 'ui_try_again', 'ui_nav_primary']
for (const lang of LANGS) {
  const catalog = read(`src/locales/${lang}.ts`)
  const missing = NEW_KEYS.filter((key) => !catalog.includes(`"${key}":`))
  ok(`${lang} catalog carries the shell keys`, missing.length === 0, missing.join(', '))
}

assert.equal(process.exitCode ?? 0, 0, 'contract violations above')
console.log(`\n${checks} checks: ${process.exitCode ? 'FAILED' : 'all passed'}`)
