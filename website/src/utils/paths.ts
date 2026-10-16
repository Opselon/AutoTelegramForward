/**
 * Returns path prefixed with Astro's configured BASE_URL.
 * Supports both root domain deployment and GitHub Pages subpath deployment.
 */
export function url(path: string): string {
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  if (!path || path === '/') {
    return base ? `${base}/` : '/';
  }
  const clean = path.startsWith('/') ? path : `/${path}`;
  return `${base}${clean}`;
}

const siteUrl = (import.meta.env.SITE || 'https://opselon.github.io').replace(/\/$/, '');

/**
 * Absolute, base-prefixed URL for anything that leaves the page: canonical
 * links, Open Graph images, JSON-LD `@id`s, feed entries, llms.txt links.
 *
 * Idempotent on purpose — it is fed both base-less paths and paths that
 * `url()` already prefixed, and a double prefix is invisible in the source and
 * only shows up in production.
 */
export function absolute(path = '/'): string {
  if (/^https?:\/\//.test(path)) return path;
  const base = import.meta.env.BASE_URL.replace(/\/$/, '');
  const clean = path.startsWith('/') ? path : `/${path}`;
  const withBase = base && !clean.startsWith(`${base}/`) && clean !== base ? `${base}${clean}` : clean;
  return `${siteUrl}${withBase}`;
}
