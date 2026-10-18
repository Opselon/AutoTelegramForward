import { test, expect } from '@playwright/test';
import { CONTRAST_AUDIT } from './contrast-audit.mjs';

/**
 * Accessibility and interaction audit.
 *
 * The theme work in this release layers a light palette over markup authored for
 * a dark canvas. Contrast is the failure mode a successful build cannot catch,
 * so this spec computes real WCAG 2.1 ratios from the rendered page in both
 * themes — including the gradient-painted surfaces, which are the ones that hide.
 */

const PAGES = [
  '/',
  '/features/',
  '/features/multi-source-routing/',
  '/api/',
  '/getting-started/docker/',
  // The atlas pages add new surfaces (canvas HUD, step cards, verification
  // pills) whose contrast is exactly the kind of thing a build cannot check.
  '/system/',
  '/system/dependencies/',
  '/system/connections/',
  // The v3 product surface added new painted panels (hero console, stage rail,
  // status board, documentation hub) on both the landing page and the new docs.
  '/status/',
  '/operations/troubleshooting/',
  '/operations/ai-providers/',
  '/getting-started/account-linking/',
  '/api/authentication/',
];

const setTheme = async (page, theme) => {
  await page.addInitScript((value) => {
    try {
      window.localStorage.setItem('atf-theme', value);
    } catch {}
  }, theme);
};

for (const theme of ['dark', 'light']) {
  for (const path of PAGES) {
    test(`WCAG AA contrast holds in the ${theme} theme on ${path}`, async ({ page }) => {
      await setTheme(page, theme);
      await page.goto(path, { waitUntil: 'networkidle' });
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);

      const result = await page.evaluate(CONTRAST_AUDIT);

      expect(result.checked, `no text nodes were measured on ${path}`).toBeGreaterThan(5);
      const summary = result.failures
        .slice(0, 12)
        .map(
          (f) =>
            `${f.ratio}:1 (needs ${f.required}) ${f.fontSize}px/${f.weight} fg ${f.colour} on ${f.background} — ${f.classes || f.selector} "${f.text}"`,
        )
        .join('\n  ');
      expect(
        result.failures.length,
        `${result.failures.length}/${result.checked} text nodes fall below WCAG AA on ${path} (${theme}):\n  ${summary}`,
      ).toBe(0);
    });
  }
}

test('Theme toggle cycles system, dark and light and persists the choice', async ({ page }) => {
  await page.goto('/');
  const root = page.locator('html');
  const toggle = page.locator('#theme-toggle');
  await expect(toggle).toBeVisible();

  const resolved = () => root.getAttribute('data-theme');
  const mode = () => root.getAttribute('data-theme-mode');
  const stored = () => page.evaluate(() => window.localStorage.getItem('atf-theme'));

  // The cycle is system → light → dark → system; only resolved palettes are
  // asserted for change, because `system` can legitimately match its neighbour.
  await expect.poll(mode).toBe('system');
  const seen = [await resolved()];

  await toggle.click();
  expect(await mode()).toBe('light');
  expect(await resolved()).toBe('light');
  expect(await stored()).toBe('light');
  seen.push(await resolved());

  await toggle.click();
  expect(await mode()).toBe('dark');
  expect(await resolved()).toBe('dark');
  expect(await stored()).toBe('dark');
  seen.push(await resolved());

  // Back to system, which clears the stored override.
  await toggle.click();
  expect(await mode()).toBe('system');
  expect(await stored()).toBeNull();
  expect(new Set(seen).size).toBeGreaterThan(1);

  // An explicit choice survives a reload instead of snapping back to system.
  await toggle.click();
  const chosen = await resolved();
  await page.reload();
  expect(await resolved()).toBe(chosen);
  expect(await mode()).toBe(chosen);

  // The browser chrome colour follows the palette.
  const metaColour = await page.getAttribute('meta[name="theme-color"]', 'content');
  expect(metaColour).toBe(chosen === 'light' ? '#f6f8fc' : '#0b1120');
});

test('Command palette opens from the keyboard, filters and navigates', async ({ page }) => {
  await page.goto('/');
  await page.keyboard.press('ControlOrMeta+k');

  const input = page.locator('#search-input');
  await expect(input).toBeVisible();
  await expect(input).toBeFocused();

  await input.fill('dedup');
  const results = page.locator('#search-results li[role="option"]');
  await expect(results.first()).toBeVisible();
  await expect(results.first()).toContainText(/dedup/i);
  // The first hit is highlighted on render so a keyboard user can hit Enter
  // immediately, and the input points at it for screen readers.
  await expect(results.first()).toHaveAttribute('aria-selected', 'true');
  await expect(input).toHaveAttribute('aria-activedescendant', 'search-option-0');

  // Arrow keys move the highlight; Enter follows it.
  await page.keyboard.press('ArrowDown');
  const expected = await page.evaluate(() => {
    const active = document.querySelector('#search-results li[aria-selected="true"] a');
    return active?.getAttribute('href');
  });
  expect(expected).toBeTruthy();

  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(new RegExp(expected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\/$/, '')));

  await page.keyboard.press('ControlOrMeta+k');
  await expect(input).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(input).toBeHidden();
});

test('Back-to-top control appears after scrolling and returns to the top', async ({ page }) => {
  await page.goto('/');
  const button = page.locator('#to-top');
  await expect(button).toBeHidden();

  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await expect(button).toBeVisible();

  await button.click();
  await expect.poll(() => page.evaluate(() => window.scrollY), { timeout: 5000 }).toBeLessThan(40);
});

test('On-page table of contents tracks the reading position', async ({ page, isMobile }) => {
  test.skip(isMobile, 'the sidebar table of contents is desktop-only');
  await page.goto('/features/multi-source-routing/');

  // Only the desktop sidebar copy is rendered at this breakpoint; the inline
  // <details> copy is display:none, so scope the assertions to the aside.
  const tocLinks = page.locator('aside a.toc-link');
  expect(await tocLinks.count()).toBeGreaterThan(2);

  const target = tocLinks.nth(2);
  const id = (await target.getAttribute('href')).replace('#', '');

  // The scroll-spy activates the last heading whose top has passed 120px from
  // the viewport top, so land this heading just inside that line (and clear of
  // the sticky header) instead of below it. The jump must be instant: the page
  // sets `scroll-behavior: smooth`, which would leave the measurement mid-flight.
  await page.evaluate((anchor) => {
    const heading = document.getElementById(anchor);
    window.scrollTo({ top: heading.getBoundingClientRect().top + window.scrollY - 100, behavior: 'instant' });
  }, id);
  const landed = await page.evaluate(
    (anchor) => document.getElementById(anchor).getBoundingClientRect().top,
    id,
  );
  expect(landed, 'the heading could not be scrolled into the spy threshold').toBeLessThanOrEqual(120);

  await expect
    .poll(() => page.evaluate(() => document.querySelector('a.toc-link.is-active')?.getAttribute('href')))
    .toBe(`#${id}`);
});

test('Mobile drawer closes itself after a navigation choice', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'desktop uses the inline header navigation');
  await page.goto('/');

  const drawerToggle = page.locator('#mobile-menu-btn');
  await expect(drawerToggle).toBeVisible();
  await drawerToggle.click();
  await expect(drawerToggle).toHaveAttribute('aria-expanded', 'true');

  // The drawer carries its own appearance control, so the theme is reachable
  // without the header bar.
  await expect(page.locator('#theme-toggle-mobile')).toBeVisible();

  const drawerLink = page.locator('#mobile-menu a[href]:visible').first();
  await expect(drawerLink).toBeVisible();
  await drawerLink.click();
  await expect(drawerToggle).toHaveAttribute('aria-expanded', 'false');
});

test('Self-hosted webfonts are the ones actually rendered', async ({ page }) => {
  await page.goto('/');
  const sampled = await page.evaluate(() => {
    const families = (selector) =>
      [...document.querySelectorAll(selector)].map((element) => getComputedStyle(element).fontFamily);
    return {
      text: families('h1, h2, p, a, li').slice(0, 40),
      mono: families('code, pre, kbd, .font-mono').slice(0, 20),
    };
  });
  expect(sampled.text.some((family) => /Inter/i.test(family))).toBe(true);
  expect(sampled.mono.length).toBeGreaterThan(0);
  expect(sampled.mono.every((family) => /JetBrains Mono/i.test(family))).toBe(true);

  // The faces must come from our own origin, not fonts.googleapis.com.
  const fontRequests = await page.evaluate(() =>
    performance
      .getEntriesByType('resource')
      .filter((entry) => entry.name.endsWith('.woff2'))
      .map((entry) => new URL(entry.name).pathname),
  );
  expect(fontRequests.length).toBeGreaterThan(0);
  expect(fontRequests.some((path) => /jetbrains-mono/i.test(path))).toBe(true);
  expect(fontRequests.some((path) => /inter/i.test(path))).toBe(true);
});
