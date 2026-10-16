import type { APIRoute } from 'astro';
import { docsNav } from '../data/docs-nav';
import { featurePages, featureHub } from '../data/features';
import { url } from '../utils/paths';

/**
 * Build-time search index for the header search overlay.
 * One flat array of entries: title, url, description, section, keywords.
 */
export const GET: APIRoute = () => {
  const entries: Array<{ title: string; url: string; desc: string; section: string; keywords?: string }> = [];

  for (const section of docsNav) {
    for (const item of section.items) {
      entries.push({
        title: item.title,
        url: url(item.path),
        desc: item.desc,
        section: section.category,
      });
    }
  }

  for (const page of featurePages) {
    entries.push({
      title: page.cardTitle,
      url: url(`/features/${page.slug}/`),
      desc: page.description,
      section: page.category,
      keywords: [...page.keywords, ...page.sections.map((s) => s.title)].join(' '),
    });
  }

  for (const group of featureHub) {
    for (const item of group.items) {
      if (entries.some((entry) => entry.url === url(item.href))) continue;
      entries.push({
        title: item.title,
        url: url(item.href),
        desc: item.desc,
        section: group.group,
      });
    }
  }

  return new Response(JSON.stringify(entries), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
};
