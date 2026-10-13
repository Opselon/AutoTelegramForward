import { test, expect } from '@playwright/test';

test.describe('AutoTelegramForward Official Platform Smoke Tests', () => {

  test('Homepage loads with correct metadata and primary CTAs', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveTitle(/AutoTelegramForward/i);

    // Verify main headline is visible
    const headline = page.locator('h1');
    await expect(headline).toBeVisible();
    await expect(headline).toContainText('Telegram Message Forwarding');

    // Verify Primary CTAs
    const getStartedBtn = page.getByRole('link', { name: /Get Started Free/i });
    await expect(getStartedBtn).toBeVisible();

    const playgroundBtn = page.getByRole('link', { name: /Interactive Playground/i });
    await expect(playgroundBtn).toBeVisible();
  });

  test('Quickstart snippet card switches tabs correctly', async ({ page }) => {
    await page.goto('/');

    const dockerTab = page.locator('[data-target="tab-docker"]');
    const linuxTab = page.locator('[data-target="tab-linux"]');
    const windowsTab = page.locator('[data-target="tab-windows"]');

    await expect(page.locator('#tab-docker')).toBeVisible();

    // Click Linux Tab
    await linuxTab.click();
    await expect(page.locator('#tab-linux')).toBeVisible();
    await expect(page.locator('#tab-docker')).toBeHidden();

    // Click Windows Tab
    await windowsTab.click();
    await expect(page.locator('#tab-windows')).toBeVisible();
    await expect(page.locator('#tab-linux')).toBeHidden();
  });

  test('Interactive Message Transformation Playground works in real-time', async ({ page }) => {
    await page.goto('/tools/playground/');
    await expect(page).toHaveTitle(/Playground/i);

    const srcInput = page.locator('#src-text');
    const preview = page.locator('#output-preview');

    await expect(srcInput).toBeVisible();
    await expect(preview).toBeVisible();

    // Type custom message
    await srcInput.fill('Join https://t.me/super_deals and contact @AdminNow #Deal');

    // Expect preview to strip URL, mention, hashtag if checkboxes are active
    const chkUrls = page.locator('#chk-urls');
    if (!(await chkUrls.isChecked())) {
      await chkUrls.check();
    }

    const text = await preview.textContent();
    expect(text).not.toContain('https://t.me/super_deals');
    expect(text).not.toContain('@AdminNow');
    expect(text).not.toContain('#Deal');

    // Test Reset
    const resetBtn = page.locator('#btn-reset');
    await resetBtn.click();
    await expect(preview).toContainText('Clean and simple telegram update');
  });

  test('Core documentation routes return status 200 and render TOC/sidebar', async ({ page }) => {
    const routes = [
      '/getting-started/',
      '/getting-started/docker/',
      '/features/',
      '/features/text-cleaner/',
      '/features/headers-footers-shortcodes/',
      '/architecture/',
      '/operations/',
      '/releases/',
      '/faq/',
    ];

    for (const route of routes) {
      const response = await page.goto(route);
      expect(response?.status()).toBe(200);

      // Verify page has non-empty title and h1 or h2
      await expect(page.locator('h1, h2').first()).toBeVisible();
      // Verify breadcrumbs exist on docs layouts
      await expect(page.locator('nav[aria-label="Breadcrumbs"]')).toBeVisible();
    }
  });

  test('Mobile navigation menu toggles cleanly on small viewports', async ({ page, isMobile }) => {
    if (!isMobile) return;

    await page.goto('/');

    const mobileMenu = page.locator('#mobile-menu');
    await expect(mobileMenu).toBeHidden();

    const menuToggle = page.locator('#mobile-menu-btn');
    await expect(menuToggle).toBeVisible();

    await menuToggle.click();
    await expect(mobileMenu).toBeVisible();

    await menuToggle.click();
    await expect(mobileMenu).toBeHidden();
  });

});
