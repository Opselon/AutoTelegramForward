import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';
import sitemap from '@astrojs/sitemap';
import repoInfo from './src/data/repo-info.json';

const site = process.env.SITE_URL || 'https://opselon.github.io';
const base = process.env.BASE_PATH || (process.env.CI ? '/AutoTelegramForward' : '');

/** Priority by route shape: the entry points rank above deep reference pages. */
const priorityOf = (pathname) => {
  const clean = pathname.replace(base, '').replace(/^\/|\/$/g, '');
  if (clean === '') return 1.0;
  if (clean === 'features' || clean === 'getting-started') return 0.9;
  if (clean.startsWith('getting-started/')) return 0.8;
  if (clean.startsWith('features/')) return 0.7;
  if (['api', 'cli', 'architecture', 'security', 'operations'].includes(clean)) return 0.7;
  if (['releases', 'faq', 'tutorials', 'tools/playground'].includes(clean)) return 0.6;
  return 0.5;
};

const changeFrequencyOf = (pathname) => {
  const clean = pathname.replace(base, '').replace(/^\/|\/$/g, '');
  if (clean === '' || clean === 'releases') return 'weekly';
  if (clean.startsWith('features/') || clean.startsWith('getting-started/')) return 'monthly';
  return 'monthly';
};

export default defineConfig({
  site,
  base,
  integrations: [
    tailwind({ applyBaseStyles: false }),
    sitemap({
      // The 404 route and the machine-readable endpoints are not pages a
      // crawler should index.
      filter: (page) => !/\/404\/?$/.test(page) && !/\.(json|txt|xml)$/.test(page),
      serialize: (item) => ({
        ...item,
        // Every page is rendered from the same checkout, so the last commit
        // date is the honest lastmod for all of them.
        lastmod: repoInfo.gitDate ? new Date(repoInfo.gitDate).toISOString() : new Date().toISOString(),
        changefreq: changeFrequencyOf(new URL(item.url).pathname),
        priority: priorityOf(new URL(item.url).pathname),
      }),
    }),
  ],
  trailingSlash: 'always',
});
