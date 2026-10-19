import { test, expect } from '@playwright/test';

/**
 * Responsive contract for the API and security surfaces.
 *
 * These pages carry the two things most likely to widen a document: a long mono
 * string (a path, an id, a header) and a wide table. Both are supposed to
 * scroll inside their own box. A narrow viewport is where that either holds or
 * the whole page starts sliding sideways, so the assertion is deliberately
 * blunt: the document must never be wider than the viewport.
 *
 * Each width gets its own context rather than resizing the shared page. In the
 * mobile project the emulated device metrics are fixed at creation, so
 * setViewportSize() moves the visual viewport while the layout viewport stays
 * put — which reports an overflow that the CSS never caused. A fresh,
 * non-emulated context per width measures the stylesheet at that width and
 * nothing else.
 *
 * The filter checks come second because progressive enhancement is a promise
 * the markup has to keep on its own: every card is rendered by the server, and
 * the script may only narrow what is already there.
 */

const PAGES = [
  // Site-wide chrome is checked on the home page: the header grew past the
  // viewport at 1024px once the docs gained pages, which is a layout fact about
  // the site, not about one article.
  '/',
  '/api/',
  '/api/endpoints/',
  '/api/responses/',
  '/api/rate-limits/',
  '/api/task-lifecycle/',
  '/security/',
  '/security/tenant-isolation/',
  '/security/secrets/',
  '/security/request-hardening/',
  // Pages whose samples and tables are the widest content on the site.
  '/cli/',
  '/getting-started/',
  '/status/',
];

// 320 is the narrowest phone still in use; 1280 is where the two-column
// layouts (request/response panes, endpoint bodies) switch on.
const WIDTHS = [320, 360, 414, 768, 1024, 1280];

/** The widest element that reaches past the viewport, described for a failure. */
const measure = () => {
  const root = document.documentElement;

  let worst = { selector: '', right: 0 };
  for (const element of document.querySelectorAll('body *')) {
    const rect = element.getBoundingClientRect();
    if (rect.width > 0 && rect.right > root.clientWidth + 1 && rect.right > worst.right) {
      const id = element.id ? `#${element.id}` : '';
      const cls = typeof element.className === 'string' ? element.className.split(/\s+/).filter(Boolean)[0] : '';
      worst = { selector: `${element.tagName.toLowerCase()}${id}${cls ? `.${cls}` : ''}`, right: Math.round(rect.right) };
    }
  }

  return { viewport: root.clientWidth, document: root.scrollWidth, worst };
};

test.describe('responsive layout', () => {
  for (const path of PAGES) {
    test(`${path} never scrolls sideways, 320px to 1280px`, async ({ browser }) => {
      for (const width of WIDTHS) {
        const context = await browser.newContext({
          viewport: { width, height: 900 },
          deviceScaleFactor: 1,
          isMobile: false,
        });
        const page = await context.newPage();
        await page.goto(path, { waitUntil: 'domcontentloaded' });

        const overflow = await page.evaluate(measure);

        expect(
          overflow.document,
          `at ${width}px the document is ${overflow.document}px wide (viewport ${overflow.viewport}px); the widest offender is ${overflow.worst.selector} reaching ${overflow.worst.right}px`,
        ).toBeLessThanOrEqual(overflow.viewport + 1);

        await context.close();
      }
    });
  }

  test('the request/response panes stack before they widen the page', async ({ browser }) => {
    const columns = async (width) => {
      const context = await browser.newContext({ viewport: { width, height: 900 }, isMobile: false });
      const page = await context.newPage();
      await page.goto('/api/responses/', { waitUntil: 'domcontentloaded' });
      const value = await page
        .locator('.reqres')
        .first()
        .evaluate((element) => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).length);
      await context.close();
      return value;
    };

    expect(await columns(1280)).toBe(2);
    expect(await columns(768)).toBe(1);
  });
});

test.describe('endpoint explorer', () => {
  test('every route is server-rendered before any script runs', async ({ browser }) => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    await page.goto('/api/endpoints/', { waitUntil: 'domcontentloaded' });

    const count = await page.locator('[data-endpoint]').count();
    const declared = await page.locator('[data-explorer-count]').getAttribute('data-total');

    expect(declared).toBeTruthy();
    expect(count).toBe(Number(declared));
    // Every one of them is visible: the filtering script is the only thing that
    // ever sets `hidden`, so an unscripted page shows the whole table.
    await expect(page.locator('[data-endpoint]:visible')).toHaveCount(count);
    await expect(page.locator('[data-endpoint][hidden]')).toHaveCount(0);

    await context.close();
  });

  test('the filter narrows the table and clearing restores it', async ({ page }) => {
    await page.goto('/api/endpoints/', { waitUntil: 'domcontentloaded' });

    const cards = page.locator('[data-endpoint]');
    const total = await cards.count();
    const visible = page.locator('[data-endpoint]:visible');

    await expect(visible).toHaveCount(total);

    await page.fill('#endpoint-filter', 'idempotency');
    const narrowed = await visible.count();
    expect(narrowed).toBeGreaterThan(0);
    expect(narrowed).toBeLessThan(total);
    await expect(page.locator('[data-explorer-count]')).toContainText(`of ${total}`);

    // The clear control only exists while a filter is in force.
    const clear = page.locator('[data-explorer-clear]');
    await expect(clear).toBeVisible();
    await clear.click();
    await expect(visible).toHaveCount(total);

    // A verb chip is a toggle, and it composes with the text filter.
    const postChip = page.locator('[data-explorer-method="POST"]');
    await postChip.click();
    await expect(postChip).toHaveAttribute('aria-pressed', 'true');
    const posts = await visible.count();
    expect(posts).toBeGreaterThan(0);
    expect(posts).toBeLessThan(total);
    await expect(page.locator('[data-endpoint]:visible').first()).toHaveAttribute('data-method', 'POST');

    await postChip.click();
    await expect(postChip).toHaveAttribute('aria-pressed', 'false');
    await expect(visible).toHaveCount(total);
  });

  test('a filtered group is hidden whole, so no empty heading is left behind', async ({ page }) => {
    await page.goto('/api/endpoints/', { waitUntil: 'domcontentloaded' });

    await page.fill('#endpoint-filter', 'ratelimit');
    const emptyGroups = await page.evaluate(() =>
      [...document.querySelectorAll('[data-explorer-group]')].filter(
        (group) => !group.hidden && !group.querySelector('[data-endpoint]:not([hidden])'),
      ).length,
    );
    expect(emptyGroups).toBe(0);
  });
});
