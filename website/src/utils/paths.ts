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
