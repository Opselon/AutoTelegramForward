# AutoTelegramForward Official Website & Documentation Platform

A modern, fast, mobile-first product website and technical documentation platform for **AutoTelegramForward**, built with **Astro**, **Tailwind CSS**, and **TypeScript**.

## Architecture & Principles
- **Static Generation First:** Zero client-side framework overhead (no heavy React/Vue runtime on content pages), resulting in perfect Lighthouse scores, sub-second load times, and effortless static hosting on GitHub Pages.
- **Dynamic Content Synchronization:** Automated build pre-hook (`scripts/sync-repo-content.mjs`) extracts authoritative repository version (`VERSION`), latest git commit hash, date, verified message types, shortcodes, and changelogs directly into `src/data/repo-info.json` and `src/data/releases.json`.
- **Advanced Technical SEO:** Pre-rendered HTML, valid Schema.org structured data (`SoftwareApplication`, `TechArticle`, `BreadcrumbList`), canonical URLs, dynamic XML sitemaps, and robots.txt.
- **Interactive Sandbox:** Pure client-side interactive message cleaner & shortcode transformation simulator (`src/components/Playground.astro`) allowing users to test regex cleaners and shortcodes before configuring their bots.
- **Comprehensive Quality Gates:** 100% automated SEO audits (`tests/seo-and-links.test.mjs`) and browser-level Playwright tests (`tests/e2e.spec.mjs`) verifying desktop & mobile viewports, drawer navigation, and playground responsiveness.

## Directory Structure
```
website/
├── public/
│   └── robots.txt             # Crawl directives & sitemap location
├── scripts/
│   └── sync-repo-content.mjs  # Authoritative repository ingestion
├── src/
│   ├── components/            # Astro components (Header, Footer, SEO, Playground, etc.)
│   ├── data/                  # Ingested repository metadata & release changelogs
│   ├── layouts/               # BaseLayout & DocsLayout (with sidebar, TOC, breadcrumbs)
│   ├── pages/                 # Route hierarchy (25 production pages)
│   ├── styles/                # Tailwind & custom glassmorphism stylesheet
│   └── utils/paths.ts         # GitHub Pages base path prefix handler
├── tests/
│   ├── e2e.spec.mjs           # Playwright browser smoke & mobile tests
│   └── seo-and-links.test.mjs # SEO, canonical, schema, and sitemap audits
├── astro.config.mjs           # Astro configuration with dynamic BASE_PATH
├── tailwind.config.mjs        # Design system & color tokens
└── deploy-pages.yml           # GitHub Pages automated deployment workflow
```

## Local Development & Testing
```bash
# 1. Install dependencies
npm install --legacy-peer-deps

# 2. Synchronize repository metadata and build
npm run build

# 3. Run full verification suite (SEO audit + Playwright tests)
npm run test

# 4. Preview locally
npm run preview
```

## GitHub Pages Deployment
A ready-to-run GitHub Actions workflow is provided at `website/deploy-pages.yml`.
To activate automatic GitHub Pages deployment:
1. Ensure GitHub Pages in your repository settings is set to **GitHub Actions** (Settings → Pages → Source → GitHub Actions).
2. Copy `website/deploy-pages.yml` to `.github/workflows/deploy-pages.yml`.
3. Every push to `master` will automatically run tests, build the site, and deploy to `https://opselon.github.io/AutoTelegramForward/`.
