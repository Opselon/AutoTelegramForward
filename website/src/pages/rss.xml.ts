import type { APIRoute } from 'astro';
import repoInfo from '../data/repo-info.json';
import releases from '../data/releases.json';
import { featurePages } from '../data/features';
import { absolute } from '../utils/paths';

/**
 * RSS 2.0 feed: every release note plus every feature deep dive, newest first.
 * Gives feed readers and aggregators a subscribable, dated surface — the one
 * content-freshness signal a documentation site usually lacks.
 */
const escape = (value: string) =>
  value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');

export const GET: APIRoute = () => {
  const releaseItems = releases.map((release) => ({
    title: release.title,
    link: absolute('/releases/'),
    guid: `${repoInfo.repoUrl}/releases/tag/${release.tag}`,
    date: new Date(`${release.date}T12:00:00Z`).toUTCString(),
    description: `${release.summary} Highlights: ${release.highlights.slice(0, 4).join(' · ')}`,
    category: 'Release',
  }));

  const featureItems = featurePages.map((feature, index) => ({
    title: `${feature.cardTitle} — ${repoInfo.name}`,
    link: absolute(`/features/${feature.slug}/`),
    guid: absolute(`/features/${feature.slug}/`),
    // Stagger one per hour so the feed keeps a stable, meaningful order.
    date: new Date(Date.parse(repoInfo.releaseDate) + index * 3600_000).toUTCString(),
    description: feature.description,
    category: 'Feature',
  }));

  const items = [...releaseItems, ...featureItems]
    .sort((a, b) => Date.parse(b.date) - Date.parse(a.date))
    .slice(0, 40);

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>${escape(`${repoInfo.name} — releases and feature deep dives`)}</title>
    <link>${absolute('/')}</link>
    <description>${escape(
      `${repoInfo.name} is a self-hosted Telegram message routing platform. This feed carries every release note and every feature deep dive.`,
    )}</description>
    <language>en</language>
    <generator>Astro</generator>
    <lastBuildDate>${new Date(repoInfo.syncTimestamp).toUTCString()}</lastBuildDate>
    <atom:link href="${absolute('/rss.xml')}" rel="self" type="application/rss+xml" />
${items
  .map(
    (item) => `    <item>
      <title>${escape(item.title)}</title>
      <link>${item.link}</link>
      <guid isPermaLink="false">${escape(item.guid)}</guid>
      <pubDate>${item.date}</pubDate>
      <category>${escape(item.category)}</category>
      <description>${escape(item.description)}</description>
    </item>`,
  )
  .join('\n')}
  </channel>
</rss>
`;

  return new Response(xml, {
    headers: { 'Content-Type': 'application/rss+xml; charset=utf-8' },
  });
};
