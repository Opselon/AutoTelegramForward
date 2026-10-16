/**
 * Whole-site WCAG AA contrast report across both themes.
 *
 * Run from website/ with a preview server on :4321:
 *   node tests/contrast-report.mjs
 * Prints every distinct failure (deduped by colour pair + selector) so fixes can
 * be batched instead of discovered one test run at a time.
 */
import { chromium } from 'playwright';
import fs from 'node:fs';
import { CONTRAST_AUDIT } from './contrast-audit.mjs';

const BASE = process.env.PREVIEW_URL || 'http://localhost:4321';

const routes = ['/'];
const walk = (dir, prefix = '') => {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) walk(`${dir}/${entry.name}`, `${prefix}${entry.name}/`);
    else if (entry.name === 'index.html') routes.push(`/${prefix}`);
  }
};
walk('dist');

const browser = await chromium.launch();
const report = {};
let pagesWithFailures = 0;
let totalChecked = 0;
let rendered = 0;

for (const theme of ['dark', 'light']) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.addInitScript((value) => {
    try {
      window.localStorage.setItem('atf-theme', value);
    } catch {}
  }, theme);
  const page = await context.newPage();

  for (const route of [...new Set(routes)]) {
    await page.goto(`${BASE}${route}`, { waitUntil: 'networkidle' });
    const result = await page.evaluate(CONTRAST_AUDIT);
    rendered += 1;
    totalChecked += result.checked;
    if (result.failures.length) {
      pagesWithFailures += 1;
      console.log(`${theme.padEnd(5)} ${route.padEnd(40)} ${result.failures.length}/${result.checked}`);
    }
    for (const failure of result.failures) {
      const key = `${failure.classes} || ${failure.colour} || ${failure.fontSize}/${failure.weight}`;
      report[key] = report[key] || { ...failure, ratio: 99, routes: [] };
      report[key].ratio = Math.min(report[key].ratio, failure.ratio);
      report[key].routes.push(`${theme}${route}`);
    }
  }
  await context.close();
}
await browser.close();

const rows = Object.values(report).sort((a, b) => a.ratio - b.ratio);
console.log(
  `\n===== ${rows.length} distinct failures across ${rendered} page/theme renders ` +
    `(${totalChecked} text nodes measured, ${pagesWithFailures} renders with failures) =====`,
);
for (const row of rows) {
  console.log(
    `\n${row.ratio}:1 (needs ${row.required}) ${row.fontSize}px/${row.weight}` +
      `\n  fg ${row.colour} on bg ${row.background}` +
      `\n  classes: ${row.classes}` +
      `\n  ${row.selector}` +
      `\n  "${row.text}" — ${[...new Set(row.routes)].length} renders, e.g. ${row.routes.slice(0, 3).join(', ')}`,
  );
}
if (!rows.length) console.log('\n✅ every measured text node clears WCAG AA in both themes.');
