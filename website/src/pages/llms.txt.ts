import type { APIRoute } from 'astro';
import repoInfo from '../data/repo-info.json';
import { docsNav } from '../data/docs-nav';
import { featurePages } from '../data/features';
import { absolute } from '../utils/paths';

/**
 * llms.txt — the llmstxt.org convention: a short, link-dense map of the site
 * for language models and answer engines, so they read the curated pages
 * instead of guessing from navigation chrome.
 */
export const GET: APIRoute = () => {
  const lines: string[] = [];

  lines.push(`# ${repoInfo.name}`);
  lines.push('');
  lines.push(
    `> ${repoInfo.name} v${repoInfo.version} is a self-hosted Telegram message routing platform: one task fans out from M source chats to N target chats with per-task content rules, optional AI rewriting, durable ordered delivery and a fail-closed multi-tenant control plane. It ships as four microservices (bot, core, logger, web) plus a REST API, CLI, dashboard and Flutter client.`,
  );
  lines.push('');
  lines.push('Key facts, all derived from the repository at build time:');
  lines.push('');
  lines.push(`- Version: ${repoInfo.version} (${repoInfo.releaseTag}, ${repoInfo.releaseDate})`);
  lines.push(`- Source: ${repoInfo.repoUrl}`);
  lines.push(`- License: ${repoInfo.license}`);
  lines.push(`- Services: ${repoInfo.stats.services} (core, logger, api/web gateway, bot UI)`);
  lines.push(`- UI languages: ${repoInfo.locales.length} (${repoInfo.locales.join(', ')}) with enforced 1:1 catalog parity and strict English fallback`);
  lines.push(`- Localized strings: ${repoInfo.stats.i18nKeys}`);
  lines.push(`- CLI command groups: ${repoInfo.stats.cliGroups}`);
  lines.push(`- gRPC RPCs: ${repoInfo.stats.grpcRpcCount}`);
  lines.push(`- Registered REST routes parsed from api/main.go: ${repoInfo.apiRoutes.length}`);
  lines.push(`- Media classes handled: ${repoInfo.supportedMediaTypes.length}`);
  lines.push('');

  lines.push('## Start here');
  lines.push('');
  for (const entry of [
    ['Quickstart: verified stack in one pass', '/getting-started/quickstart/'],
    ['Installation overview', '/getting-started/'],
    ['Docker Compose deployment', '/getting-started/docker/'],
    ['Configuration reference', '/getting-started/configuration/'],
    ['Verification checklist', '/getting-started/verification/'],
    ['Account linking and login codes', '/getting-started/account-linking/'],
    ['Feature overview', '/features/'],
    ['Creating a forwarding task', '/features/task-creation/'],
    ['Architecture', '/architecture/'],
    ['Security model and tenant isolation', '/security/'],
    ['REST API reference', '/api/'],
    ['API authentication', '/api/authentication/'],
    ['CLI reference', '/cli/'],
    ['Operations and diagnostics', '/operations/'],
    ['Troubleshooting runbook', '/operations/troubleshooting/'],
    ['AI providers, keys and prompts in depth', '/operations/ai-providers/'],
    ['Status board: implemented, partial, planned', '/status/'],
    ['Interactive transformation playground', '/tools/playground/'],
    ['Release notes', '/releases/'],
    ['FAQ', '/faq/'],
  ]) {
    lines.push(`- [${entry[0]}](${absolute(entry[1])})`);
  }
  lines.push('');

  lines.push('## Feature deep dives');
  lines.push('');
  for (const feature of featurePages) {
    lines.push(`- [${feature.cardTitle}](${absolute(`/features/${feature.slug}/`)}): ${feature.description}`);
  }
  lines.push('');

  lines.push('## Documentation index');
  lines.push('');
  for (const section of docsNav) {
    lines.push(`### ${section.category}`);
    lines.push('');
    for (const item of section.items) {
      lines.push(`- [${item.title}](${absolute(item.path)}): ${item.desc}`);
    }
    lines.push('');
  }

  lines.push('## Optional');
  lines.push('');
  lines.push(`- [RSS feed](${absolute('/rss.xml')}): release notes and feature deep dives`);
  lines.push(`- [Search index](${absolute('/search.json')}): flat JSON of every documented page`);
  lines.push(`- [Sitemap](${absolute('/sitemap-index.xml')})`);
  lines.push('');

  return new Response(lines.join('\n'), {
    headers: { 'Content-Type': 'text/plain; charset=utf-8' },
  });
};
