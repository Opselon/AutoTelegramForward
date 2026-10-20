import type { APIRoute } from 'astro';

/**
 * The web app manifest is generated rather than kept in public/ so the scope
 * and every icon URL follow BASE_PATH. A hardcoded `/AutoTelegramForward/`
 * manifest silently stops matching the moment the site is served from a root
 * domain or a fork deploys under its own repository name.
 */
export const GET: APIRoute = () => {
  const base = import.meta.env.BASE_URL.endsWith('/') ? import.meta.env.BASE_URL : `${import.meta.env.BASE_URL}/`;
  const manifest = {
    name: 'AutoTelegramForward — Telegram Message Routing Platform',
    short_name: 'ATF',
    description:
      'Self-hosted Telegram routing: M sources to N targets, per-task content rules, optional AI rewriting and durable ordered delivery.',
    id: base,
    start_url: base,
    scope: base,
    display: 'standalone',
    display_override: ['window-controls-overlay', 'standalone'],
    orientation: 'any',
    background_color: '#070b14',
    theme_color: '#070b14',
    lang: 'en',
    dir: 'ltr',
    categories: ['developer', 'productivity', 'utilities'],
    icons: [
      { src: `${base}assets/icon-192.png`, sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: `${base}assets/icon-512.png`, sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: `${base}assets/icon-maskable-512.png`, sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
    shortcuts: [
      {
        name: 'Quickstart',
        short_name: 'Install',
        description: 'Install the stack and deliver a first message.',
        url: `${base}getting-started/quickstart/`,
      },
      {
        name: 'Playground',
        short_name: 'Playground',
        description: 'Try the cleaner and shortcodes in the browser.',
        url: `${base}tools/playground/`,
      },
    ],
  };

  return new Response(JSON.stringify(manifest, null, 2), {
    headers: {
      'Content-Type': 'application/manifest+json; charset=utf-8',
      'Cache-Control': 'public, max-age=3600',
    },
  });
};
