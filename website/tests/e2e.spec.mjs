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

  test('Every new v2.0 feature deep dive renders with FAQ structured data', async ({ page }) => {
    const slugs = [
      'multi-source-routing',
      'per-task-content-rules',
      'ai-rewriting',
      'observability-and-logs',
      'ordered-delivery',
      'deduplication-and-replies',
      'clients-and-interfaces',
      'multilingual-ui',
      'devops-cli',
    ];

    for (const slug of slugs) {
      const response = await page.goto(`/features/${slug}/`);
      expect(response?.status()).toBe(200);

      await expect(page.locator('h1')).toBeVisible();
      await expect(page.locator('nav[aria-label="Breadcrumbs"]')).toBeVisible();

      // Each deep dive must ship a FAQPage graph node for rich results.
      const html = await page.content();
      expect(html).toContain('FAQPage');
    }
  });

  test('REST and CLI references render their generated tables', async ({ page }) => {
    await page.goto('/api/');
    await expect(page.locator('h1')).toBeVisible();
    // The route table is generated from api/main.go at build time.
    const endpointRows = page.locator('.spec-table tbody tr');
    expect(await endpointRows.count()).toBeGreaterThan(50);
    // Mobile fallback cards must exist alongside the table.
    expect(await page.locator('.spec-cards .spec-card').count()).toBeGreaterThan(50);

    await page.goto('/cli/');
    await expect(page.locator('h1')).toBeVisible();
    const groups = await page.locator('code:has-text("atf ")').count();
    expect(groups).toBeGreaterThan(20);
  });

  test('Security page states the isolation model and its limits', async ({ page }) => {
    await page.goto('/security/');
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.getByText(/fail-closed/i).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Known limits' })).toBeVisible();
  });

  test('Search overlay finds new documentation from the generated index', async ({ page, isMobile }) => {
    if (isMobile) return;

    await page.goto('/');
    await page.locator('#search-open').click();

    const input = page.locator('#search-input');
    await expect(input).toBeVisible();
    await input.fill('rewrite');

    const results = page.locator('#search-results a');
    await expect(results.first()).toBeVisible();
    expect(await results.count()).toBeGreaterThan(0);
  });

  test('Bottom navigation is mobile-only', async ({ page, isMobile }) => {
    await page.goto('/');
    const bottomNav = page.locator('.bottom-nav');

    if (isMobile) {
      await expect(bottomNav).toBeVisible();
    } else {
      await expect(bottomNav).toBeHidden();
    }
  });


test.describe('System atlas — derived maps and simulations', () => {

  test('Atlas hub renders the runtime topology and inspects a component on click', async ({ page }) => {
    const response = await page.goto('/system/');
    expect(response?.status()).toBe(200);
    await expect(page.locator('h1')).toContainText('System atlas');

    const map = page.locator('[data-topology]');
    await expect(map).toBeVisible();
    // Every component from the derived graph is drawn, not a subset.
    const nodes = map.locator('[data-node]');
    expect(await nodes.count()).toBeGreaterThanOrEqual(12);

    // The detail panel is server-rendered, so it already has content.
    const detail = page.locator('[data-topology-detail]');
    await expect(detail).toContainText('Pipeline');

    // Clicking a component re-renders the panel from the embedded data.
    await map.locator('[data-node][data-id="api-rest"]').click();
    await expect(detail).toContainText('REST control plane');
    await expect(detail.locator('a[href*="github.com"]').first()).toBeVisible();
  });

  test('Dependency atlas loads the real graph, filters it and inspects a module', async ({ page }) => {
    await page.goto('/system/dependencies/');
    await expect(page.locator('h1')).toContainText('Dependency');

    await expect(page.locator('[data-depmap]')).toHaveAttribute('data-dep-ready', 'true', { timeout: 15000 });
    const counts = await page.evaluate(() => {
      const api = window.__atlas2d;
      return { nodes: api.nodes, edges: api.edges, visible: api.visibleCount() };
    });
    expect(counts.nodes).toBeGreaterThan(200);
    expect(counts.edges).toBeGreaterThan(400);
    expect(counts.visible).toBeGreaterThan(100); // tests are hidden by default

    // Turning a subsystem off must remove exactly its nodes from the canvas.
    const before = counts.visible;
    await page.locator('[data-dep-subsystem="core"]').click();
    const afterDisable = await page.evaluate(() => window.__atlas2d.visibleCount());
    expect(afterDisable).toBeLessThan(before);
    await page.locator('[data-dep-subsystem="core"]').click();
    expect(await page.evaluate(() => window.__atlas2d.visibleCount())).toBe(before);

    // Generated stubs are in the data and excluded on request.
    await page.locator('[data-dep-hide-generated]').check();
    expect(await page.evaluate(() => window.__atlas2d.visibleCount())).toBeLessThan(before);

    // Search narrows the canvas and opens the first match in the detail panel.
    await page.locator('[data-dep-search]').fill('pipeline');
    await expect(page.locator('[data-dep-hud-count]')).toContainText('match');
    await page.locator('[data-dep-search]').press('Enter');
    await expect(page.locator('[data-dep-detail]')).toContainText('pipeline');
    await expect(page.locator('[data-dep-detail] a[href*="github.com"]').first()).toBeVisible();
  });

  test('3D connection simulation replays a real flow packet by packet', async ({ page }) => {
    await page.goto('/system/connections/');
    await expect(page.locator('h1')).toContainText('3D');

    const sim = page.locator('[data-sim3d]');
    await expect(sim).toHaveAttribute('data-sim-ready', 'true', { timeout: 15000 });

    const linkCount = await page.evaluate(() => window.__atlas3d.links);
    expect(linkCount).toBeGreaterThan(15);

    // Every step of every flow is in the document, not only in the canvas.
    const steps = sim.locator('[data-sim-steps] li');
    expect(await steps.count()).toBeGreaterThan(15);

    // Pick a scenario, then assert the trace actually grew.
    await sim.locator('[data-sim-flow="forward"]').click();
    await expect(sim.locator('[data-sim-flow="forward"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(sim.locator('[data-sim-log] li').first()).toContainText('replay started', { timeout: 10000 });
    await expect(sim.locator('[data-sim-steps="forward"] li[data-active="true"]').first()).toBeVisible({ timeout: 10000 });
    const logged = await page.evaluate(() => window.__atlas3d.logLines());
    expect(logged).toBeGreaterThan(1);

    // Stop returns the scene to idle and preserves the steps as text.
    const forwardSteps = sim.locator('[data-sim-steps="forward"] li');
    const forwardCount = await forwardSteps.count();
    await sim.locator('[data-sim-stop]').click();
    await expect(sim.locator('[data-sim-title]')).toHaveText('Idle drift');
    await expect(forwardSteps).toHaveCount(forwardCount);
  });

  test('Every atlas route is linked from navigation and indexed for search', async ({ page, isMobile }) => {
    if (isMobile) return;
    await page.goto('/system/');
    const footer = page.locator('footer');
    await expect(footer.getByRole('link', { name: 'Dependency atlas' })).toBeVisible();

    await page.goto('/');
    await page.locator('#search-open').click();
    const input = page.locator('#search-input');
    // The overlay matches whole tokens against title/description, so the query
    // has to be a token that the atlas entry really carries.
    await input.fill('keystone');
    const results = page.locator('#search-results a');
    await expect(results.first()).toBeVisible();
    expect(await results.count()).toBeGreaterThan(0);
    await expect(page.locator('#search-results a[href*="/system/dependencies/"]').first()).toBeVisible();
  });

});

});
