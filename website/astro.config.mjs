import { defineConfig } from 'astro/config';
import tailwind from '@astrojs/tailwind';
import sitemap from '@astrojs/sitemap';

const site = process.env.SITE_URL || 'https://opselon.github.io';
const base = process.env.BASE_PATH || (process.env.CI ? '/AutoTelegramForward' : '');

export default defineConfig({
  site,
  base,
  integrations: [tailwind({ applyBaseStyles: false }), sitemap()],
  trailingSlash: 'always',
});
