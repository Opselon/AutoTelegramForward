#!/usr/bin/env node
/**
 * Live UI audit: measure the deployed dashboard at phone/tablet/desktop widths.
 *
 * The bundle the users actually load is the only honest subject: this drives the
 * real app with a real session in Chromium and reads layout metrics from the DOM,
 * so "it looks fine" becomes four numbers a reviewer can diff.
 *
 *   node scripts/bench/ui_live_audit.mjs                        # 360/768/1440
 *   node scripts/bench/ui_live_audit.mjs --widths 320,360,414
 *   node scripts/bench/ui_live_audit.mjs --url http://127.0.0.1:8088
 *   node scripts/bench/ui_live_audit.mjs --shot docs/benchmarks/ui-360.png
 *
 * A dashboard session is required (the landing page has no data panels). Point the
 * run at one of: $ATF_BENCH_TOKEN, --token <jwt>, or /tmp/atf-bench-web-token.json
 * (written by scripts/bench/ui_perf_bench.py, which mints it through the core).
 *
 * Exit code 1 when a budget from docs/ui-ux-system.md is broken:
 * page overflow > 0, tap targets < 44px > 0, text under 12px, unnamed icon buttons > 0.
 */
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

// Playwright ships with the docs site, not with this script: resolve it there
// instead of forcing a second copy of the browser download into the repo root.
const require = createRequire(import.meta.url);
function loadPlaywright() {
  const candidates = [
    process.env.PLAYWRIGHT_ROOT,
    'website/node_modules',
    'web/frontend/node_modules',
    'node_modules',
  ].filter(Boolean);
  for (const root of candidates) {
    const entry = resolve(root, 'playwright');
    if (existsSync(entry)) return require(entry);
  }
  return require('playwright');
}
const { chromium } = loadPlaywright();

const args = process.argv.slice(2);
const argOf = (name, fallback = null) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};

const BASE = argOf('url', process.env.ATF_BENCH_URL || 'https://api.legoten.com');
const WIDTHS = argOf('widths', '360,768,1440').split(',').map((w) => Number(w.trim()));
const HEIGHT = Number(argOf('height', '900'));
const OUT = argOf('out', 'docs/benchmarks/ui-live-audit.json');
const TOKEN_FILE = '/tmp/atf-bench-web-token.json';

function token() {
  const explicit = argOf('token', process.env.ATF_BENCH_TOKEN);
  if (explicit) return explicit;
  try {
    return JSON.parse(readFileSync(TOKEN_FILE, 'utf8')).token;
  } catch {
    return '';
  }
}

const MEASURE = () => {
  const vw = window.innerWidth;
  const all = [...document.querySelectorAll('*')];
  const scrollerOf = (el) => {
    let p = el.parentElement;
    while (p) {
      const style = getComputedStyle(p);
      if (/(auto|scroll)/.test(style.overflowX)) return p;
      p = p.parentElement;
    }
    return null;
  };
  const overflowing = all.filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.right > vw + 1;
  });
  const realOverflow = overflowing.filter((el) => !scrollerOf(el));
  const targets = all
    .filter((el) => ['BUTTON', 'A', 'INPUT', 'SELECT'].includes(el.tagName))
    .filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && (r.height < 44 || r.width < 44);
    });
  const unnamed = all.filter(
    (el) => el.tagName === 'BUTTON' && !(el.innerText || '').trim() && !el.getAttribute('aria-label') && !el.title,
  );
  const sizes = all
    .slice(0, 3000)
    .map((el) => parseFloat(getComputedStyle(el).fontSize) || 0)
    .filter((v) => v > 0);
  const pages = document.documentElement.scrollWidth > vw + 1;
  return {
    vw,
    pageScrollX: pages,
    realOverflow: realOverflow.length,
    realOverflowSamples: realOverflow.slice(0, 6).map((el) => ({
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 50),
      right: Math.round(el.getBoundingClientRect().right),
    })),
    clippedInScrollers: overflowing.length - realOverflow.length,
    smallTargets: targets.length,
    unnamedIconButtons: unnamed.length,
    minFontPx: sizes.length ? Math.round(Math.min(...sizes) * 10) / 10 : null,
    under12px: sizes.filter((v) => v < 12).length,
    drawerTrigger: !!(document.querySelector('[aria-label*="menu" i], .hamburger, [data-drawer-toggle], button[aria-expanded]')),
  };
};

const budgets = (m) => {
  const problems = [];
  if (m.pageScrollX) problems.push(`page scrolls sideways at ${m.vw}px`);
  if (m.realOverflow > 0) problems.push(`${m.realOverflow} element(s) overflow the page at ${m.vw}px`);
  if (m.smallTargets > 0) problems.push(`${m.smallTargets} tap target(s) under 44px at ${m.vw}px`);
  if (m.unnamedIconButtons > 0) problems.push(`${m.unnamedIconButtons} unnamed icon button(s) at ${m.vw}px`);
  if (m.under12px > 0) problems.push(`${m.under12px} text node(s) under 12px at ${m.vw}px`);
  return problems;
};

const jwt = token();
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: WIDTHS[0], height: HEIGHT }, deviceScaleFactor: 1 });
const page = await context.newPage();
if (jwt) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate((t) => localStorage.setItem('auth_token', t), jwt);
}

const report = { url: BASE, authenticated: Boolean(jwt), measured_at: new Date().toISOString(), widths: {} };
const problems = [];
for (const width of WIDTHS) {
  await page.setViewportSize({ width, height: HEIGHT });
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);
  const metrics = await page.evaluate(MEASURE);
  report.widths[width] = metrics;
  const found = budgets(metrics);
  problems.push(...found);
  console.log(
    `${String(width).padStart(4)}px  overflow=${metrics.realOverflow}  clipped=${metrics.clippedInScrollers}  ` +
      `smallTargets=${metrics.smallTargets}  unnamed=${metrics.unnamedIconButtons}  minFont=${metrics.minFontPx}px  ` +
      `drawer=${metrics.drawerTrigger}`,
  );
  const shot = argOf('shot');
  if (shot) {
    const target = shot.includes('%w') ? shot.replace('%w', String(width)) : shot;
    mkdirSync(dirname(resolve(target)), { recursive: true });
    await page.screenshot({ path: target, fullPage: true });
    console.log(`  shot -> ${target}`);
  }
}
await browser.close();

mkdirSync(dirname(resolve(OUT)), { recursive: true });
writeFileSync(OUT, `${JSON.stringify(report, null, 2)}\n`);
console.log(`saved -> ${OUT}`);
if (problems.length) {
  console.log(`\nFAIL: ${problems.length} budget violation(s)`);
  for (const p of problems) console.log(`  - ${p}`);
  process.exit(1);
}
console.log('\nPASS: every width is inside the UI budget');
