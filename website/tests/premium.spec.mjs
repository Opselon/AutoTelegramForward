import { test, expect } from '@playwright/test';

/**
 * Premium product surface.
 *
 * The landing page gained a two-column hero, an animated console, a stage rail,
 * a status board generated from the feature catalog and a documentation hub.
 * These specs assert the parts a build cannot see: that the new sections are
 * actually painted, that the generated counts agree with the generated list,
 * that no new element pushes the page wider than the viewport, and that every
 * new documentation route answers with a real page.
 */

const NEW_ROUTES = [
  '/getting-started/quickstart/',
  '/getting-started/account-linking/',
  '/features/task-creation/',
  '/api/authentication/',
  '/operations/troubleshooting/',
  '/operations/ai-providers/',
  '/status/',
];

test.describe('Premium landing surface', () => {
  test('hero renders the console, both primary calls to action and the status pill', async ({ page }) => {
    await page.goto('/');

    await expect(page.locator('h1')).toContainText('Telegram');
    await expect(page.getByRole('link', { name: /Get Started Free/i })).toBeVisible();
    await expect(page.getByRole('link', { name: /Interactive Playground/i })).toBeVisible();

    // The console is the signature visual; it must render its trace lines.
    const console_ = page.locator('[data-console]');
    await expect(console_).toBeVisible();
    await expect(console_.locator('.console-line')).not.toHaveCount(0);
    await expect(console_).toContainText('Example pipeline trace');

    // The version pill routes to the status board, so a reader can check claims.
    await expect(page.locator('.pill-live')).toHaveAttribute('href', /\/status\/$/);
  });

  test('the pipeline rail shows every documented stage', async ({ page }) => {
    await page.goto('/');
    const stages = page.locator('#how-it-works .rail-stage');
    await expect(stages).toHaveCount(5);
    await expect(stages.first()).toContainText('Ingest');
    await expect(stages.last()).toContainText('Deliver');
  });

  test('the status board counts agree with the entries it renders', async ({ page }) => {
    await page.goto('/');

    // Each summary card states "<n> of <total> catalogued capabilities", and the
    // panel below lists exactly <n> entries: the page cannot overstate itself.
    const summary = await page.locator('#status .panel').first().innerText();
    expect(summary).toMatch(/\d+ of \d+ catalogued capabilities/);

    for (const label of ['Implemented', 'Partial', 'Planned']) {
      const panel = page.locator('#status .panel', { has: page.locator(`.status-chip:text-is("${label}")`) });
      await expect(panel.first()).toBeVisible();
    }

    const rendered = await page.evaluate(() => {
      // Two panels carry each label's chip: the summary card (with the count
      // badge) and the list card (with one row per entry). Both must agree.
      const out = {};
      for (const panel of document.querySelectorAll('#status .panel')) {
        const chip = panel.querySelector('.status-chip');
        if (!chip) continue;
        const label = chip.textContent.trim();
        if (!['Implemented', 'Partial', 'Planned'].includes(label)) continue;
        const badge = panel.querySelector('.tabular');
        out[label] = out[label] || {};
        if (badge) out[label].counted = Number(badge.textContent.trim());
        else out[label].listed = panel.querySelectorAll('li').length;
      }
      return out;
    });

    // The count badge and the listed entries must be the same number: the page
    // cannot state one total and render another.
    for (const [label, value] of Object.entries(rendered)) {
      expect(value.counted, `${label} bucket is missing its count`).toBeGreaterThan(0);
      expect(value.listed, `${label} bucket is missing its entry list`).toBeGreaterThan(0);
      expect(value.counted, `${label} bucket count disagrees with its listed entries`).toBe(value.listed);
    }
    // The implemented panel lists one row per implemented capability.
    const implementedPanel = page
      .locator('#status .panel')
      .filter({ has: page.locator('.status-chip:text-is("Implemented")') })
      .last();
    const implementedRows = await implementedPanel.locator('li').count();
    expect(implementedRows).toBeGreaterThan(5);
  });

  test('the landing page never scrolls sideways', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow, 'horizontal overflow on a 390px viewport').toBeLessThanOrEqual(1);
  });

  test('reveal animation lands without hiding content', async ({ page }) => {
    // Motion is additive: whatever the observer does, the text stays visible.
    await page.goto('/');
    await page.mouse.wheel(0, 2400);
    await page.waitForTimeout(400);
    const hidden = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.reveal')).filter((node) => {
        const box = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        const onScreen = box.top < window.innerHeight && box.bottom > 0;
        return onScreen && (style.opacity === '0' || style.visibility === 'hidden');
      }).length;
    });
    expect(hidden).toBe(0);
  });

  test('desktop navigation is visible above the breakpoint and collapsed below it', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await expect(page.locator('nav[aria-label="Main navigation"]')).toBeVisible();
    await expect(page.locator('#mobile-menu-btn')).toBeHidden();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await expect(page.locator('nav[aria-label="Main navigation"]')).toBeHidden();
    await expect(page.locator('#mobile-menu-btn')).toBeVisible();
  });
});

test.describe('New documentation routes', () => {
  for (const route of NEW_ROUTES) {
    test(`${route} renders a real page with breadcrumbs and one h1`, async ({ page }) => {
      const response = await page.goto(route);
      expect(response?.status()).toBe(200);

      await expect(page.locator('h1')).toHaveCount(1);
      await expect(page.locator('nav[aria-label="Breadcrumbs"]')).toBeVisible();

      // The sidebar is the documentation index; every new page must appear in it.
      await expect(page.locator('a.docs-nav-link[href$="' + route + '"]').first()).toBeAttached();
    });
  }

  test('the search index reaches the new pages', async ({ page, isMobile }) => {
    if (isMobile) return;
    await page.goto('/');
    await page.locator('#search-open').click();
    const input = page.locator('#search-input');
    await input.fill('troubleshooting');
    await expect(page.locator('#search-results a').first()).toBeVisible();
    await expect(page.locator('#search-results a[href*="/operations/troubleshooting/"]').first()).toBeVisible();
  });

  test('the FAQ covers account linking and the status board is reachable from the footer', async ({ page }) => {
    await page.goto('/faq/');
    await expect(page.getByRole('heading', { name: 'Accounts & access' })).toBeVisible();
    await page.getByText('How long is a login link valid, and how many times can it be used?').click();
    await expect(page.getByText(/Ten minutes, once/)).toBeVisible();

    await page.goto('/');
    await expect(page.locator('footer a[href$="/status/"]').first()).toBeAttached();
  });
});

test.describe('Theme surface integrity', () => {
  /**
   * Regression: the v3 light-theme override repainted `.text-gradient` with the
   * `background` shorthand, which resets `background-clip` to `border-box`.
   * The headline then rendered as a solid gradient rectangle behind fully
   * transparent glyphs — invisible in dark mode, obvious once the theme
   * flipped. Assert the computed clip, not the declaration, in both themes.
   */
  test('the gradient headline stays clipped to its glyphs in both themes', async ({ page }) => {
    await page.goto('/');

    for (const theme of ['dark', 'light']) {
      await page.evaluate((value) => localStorage.setItem('atf-theme', value), theme);
      await page.reload();

      const state = await page.locator('h1 .text-gradient').evaluate((el) => {
        const styles = getComputedStyle(el);
        return {
          theme: document.documentElement.dataset.theme,
          backgroundClip: styles.backgroundClip,
          webkitClip: styles.webkitBackgroundClip,
          fill: styles.webkitTextFillColor,
        };
      });

      expect(state.theme).toBe(theme);
      expect(state.backgroundClip).toBe('text');
      expect(state.webkitClip).toBe('text');
      expect(state.fill).toBe('rgba(0, 0, 0, 0)');
    }

    await page.evaluate(() => localStorage.removeItem('atf-theme'));
  });
});
