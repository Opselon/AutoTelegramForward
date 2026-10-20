import repoInfo from './repo-info.json';
import { url } from '../utils/paths';

export interface NavItem {
  name: string;
  href: string;
  desc?: string;
}

export interface NavGroup {
  name: string;
  href: string;
  items?: NavItem[];
}

/** Primary navigation — kept short on purpose: 5 destinations, each a real hub. */
export const primaryNav: NavGroup[] = [
  {
    name: 'Features',
    href: url('/features/'),
    items: [
      { name: 'Feature overview', href: url('/features/'), desc: 'Every capability, grouped by subsystem' },
      { name: 'Multi-source → multi-target routing', href: url('/features/multi-source-routing/'), desc: 'M sources to N targets in one task' },
      { name: 'Per-task content rules', href: url('/features/per-task-content-rules/'), desc: 'Decide and rewrite before delivery' },
      { name: 'AI rewriting', href: url('/features/ai-rewriting/'), desc: 'Optional LLM pass with custom prompts' },
      { name: 'Observability & log history', href: url('/features/observability-and-logs/'), desc: 'Attributable logs, stats and DLQ control' },
      { name: 'Ordered delivery', href: url('/features/ordered-delivery/'), desc: 'Bursts arrive in the order posted' },
      { name: 'Dedup, replies & edits', href: url('/features/deduplication-and-replies/'), desc: 'No repeats, no orphaned replies' },
      { name: 'Clients & interfaces', href: url('/features/clients-and-interfaces/'), desc: 'Bot, web dashboard and mobile app' },
      { name: 'Multilingual UI', href: url('/features/multilingual-ui/'), desc: `${repoInfo.stats.locales} locales, 1:1 parity, RTL` },
      { name: 'DevOps CLI & upgrades', href: url('/features/devops-cli/'), desc: 'Validate, diagnose, upgrade' },
      { name: 'Creating a task', href: url('/features/task-creation/'), desc: 'Sources, targets, policies, simulate' },
      { name: 'All feature deep dives', href: url('/features/#deep-dives'), desc: 'Cleaner, filters, templates, security' },
    ],
  },
  {
    name: 'Get started',
    href: url('/getting-started/'),
    items: [
      { name: 'Quickstart', href: url('/getting-started/quickstart/'), desc: 'Verified stack, first message' },
      { name: 'Install overview', href: url('/getting-started/'), desc: 'Pick docker, source or portable' },
      { name: 'Docker Compose', href: url('/getting-started/docker/'), desc: 'Four containers, one command' },
      { name: 'Linux & Windows', href: url('/getting-started/linux-windows/'), desc: 'Native install and services' },
      { name: 'Telegram bot setup', href: url('/getting-started/telegram-bot-setup/'), desc: 'Token, admin id, first login' },
      { name: 'Account linking', href: url('/getting-started/account-linking/'), desc: 'Login codes and re-auth' },
      { name: 'Configuration', href: url('/getting-started/configuration/'), desc: 'Every environment key' },
      { name: 'Verification', href: url('/getting-started/verification/'), desc: 'Prove each service is serving' },
    ],
  },
  {
    name: 'Reference',
    href: url('/api/'),
    items: [
      { name: 'System atlas', href: url('/system/'), desc: 'The whole system, mapped from its own source' },
      { name: 'Connection map (3D)', href: url('/system/connections/'), desc: 'Simulate every runtime link' },
      { name: 'Dependency atlas', href: url('/system/dependencies/'), desc: 'Module graph, keystones, cycles' },
      { name: 'REST API', href: url('/api/'), desc: 'Control-plane endpoints and auth' },
      { name: 'API authentication', href: url('/api/authentication/'), desc: 'Tokens, limits, error codes' },
      { name: 'Endpoint reference', href: url('/api/endpoints/'), desc: `All ${repoInfo.apiRoutes.length} routes, with fields` },
      { name: 'Response envelope', href: url('/api/responses/'), desc: 'Body shapes, codes, guard order' },
      { name: 'Rate limits', href: url('/api/rate-limits/'), desc: 'Six classes, keys, fail modes' },
      { name: 'Task lifecycle', href: url('/api/task-lifecycle/'), desc: 'Durable jobs, states, retries' },
      { name: 'Status board', href: url('/status/'), desc: 'Implemented, partial and planned' },
      { name: 'CLI reference', href: url('/cli/'), desc: `${repoInfo.stats.cliGroups} command groups` },
      { name: 'Architecture', href: url('/architecture/'), desc: 'Services, pipeline, queues' },
      { name: 'Security model', href: url('/security/'), desc: 'Tenant isolation and encryption' },
      { name: 'Tenant isolation', href: url('/security/tenant-isolation/'), desc: 'Owner scoping, three refusals' },
      { name: 'Secrets & keys', href: url('/security/secrets/'), desc: 'Encryption, redaction, rotation' },
      { name: 'Request hardening', href: url('/security/request-hardening/'), desc: 'Caps, timeouts, SSRF guard' },
      { name: 'Operations', href: url('/operations/'), desc: 'Diagnostics and runbooks' },
      { name: 'Troubleshooting', href: url('/operations/troubleshooting/'), desc: 'Symptom, cause, fix' },
      { name: 'AI providers', href: url('/operations/ai-providers/'), desc: 'Keys, prompts, failover' },
      { name: 'Playground', href: url('/tools/playground/'), desc: 'Try the cleaner in the browser' },
    ],
  },
  {
    name: 'Learn',
    href: url('/tutorials/'),
    items: [
      { name: 'Tutorials & recipes', href: url('/tutorials/'), desc: 'Copy-paste walkthroughs' },
      { name: 'Releases', href: url('/releases/'), desc: `What shipped in v${repoInfo.version}` },
      { name: 'FAQ', href: url('/faq/'), desc: 'Straight answers' },
    ],
  },
];

/** Mobile bottom bar — the four destinations a phone user actually needs. */
export const bottomNav = [
  { name: 'Home', href: url('/'), icon: 'home' },
  { name: 'Features', href: url('/features/'), icon: 'sparkles' },
  { name: 'Install', href: url('/getting-started/'), icon: 'download' },
  { name: 'Playground', href: url('/tools/playground/'), icon: 'beaker' },
  { name: 'GitHub', href: repoInfo.repoUrl, icon: 'github', external: true },
];

export const footerColumns = [
  {
    title: 'Product',
    links: [
      { name: 'Feature overview', href: url('/features/') },
      { name: 'Multi-source routing', href: url('/features/multi-source-routing/') },
      { name: 'Content rules', href: url('/features/per-task-content-rules/') },
      { name: 'AI rewriting', href: url('/features/ai-rewriting/') },
      { name: 'Observability', href: url('/features/observability-and-logs/') },
      { name: 'Playground', href: url('/tools/playground/') },
    ],
  },
  {
    title: 'Build & run',
    links: [
      { name: 'Quickstart', href: url('/getting-started/quickstart/') },
      { name: 'Install overview', href: url('/getting-started/') },
      { name: 'Account linking', href: url('/getting-started/account-linking/') },
      { name: 'Docker Compose', href: url('/getting-started/docker/') },
      { name: 'Linux & Windows', href: url('/getting-started/linux-windows/') },
      { name: 'Configuration', href: url('/getting-started/configuration/') },
      { name: 'Verification', href: url('/getting-started/verification/') },
      { name: 'Operations', href: url('/operations/') },
      { name: 'Troubleshooting', href: url('/operations/troubleshooting/') },
    ],
  },
  {
    title: 'Reference',
    links: [
      { name: 'System atlas', href: url('/system/') },
      { name: 'Connection map (3D)', href: url('/system/connections/') },
      { name: 'Dependency atlas', href: url('/system/dependencies/') },
      { name: 'REST API', href: url('/api/') },
      { name: 'Endpoint reference', href: url('/api/endpoints/') },
      { name: 'Response envelope', href: url('/api/responses/') },
      { name: 'Rate limits', href: url('/api/rate-limits/') },
      { name: 'Task lifecycle', href: url('/api/task-lifecycle/') },
      { name: 'CLI reference', href: url('/cli/') },
      { name: 'Architecture', href: url('/architecture/') },
      { name: 'Security model', href: url('/security/') },
      { name: 'Tenant isolation', href: url('/security/tenant-isolation/') },
      { name: 'Secrets & keys', href: url('/security/secrets/') },
      { name: 'Request hardening', href: url('/security/request-hardening/') },
      { name: 'API authentication', href: url('/api/authentication/') },
      { name: 'Status board', href: url('/status/') },
      { name: 'Releases', href: url('/releases/') },
      { name: 'FAQ', href: url('/faq/') },
    ],
  },
  {
    title: 'Project',
    links: [
      { name: 'GitHub repository', href: repoInfo.repoUrl, external: true },
      { name: 'Latest release', href: `${repoInfo.repoUrl}/releases/latest`, external: true },
      { name: 'Issue tracker', href: `${repoInfo.repoUrl}/issues`, external: true },
      { name: 'Community (@Capxi)', href: repoInfo.maintainer.url, external: true },
      { name: 'MIT license', href: `${repoInfo.repoUrl}/blob/master/LICENSE`, external: true },
    ],
  },
];

export const localeLabels: Record<string, { label: string; native: string; rtl?: boolean }> = {
  en: { label: 'English', native: 'English' },
  fa: { label: 'Persian', native: 'فارسی', rtl: true },
  ru: { label: 'Russian', native: 'Русский' },
  zh: { label: 'Chinese', native: '中文' },
  pt: { label: 'Portuguese', native: 'Português' },
  uz: { label: 'Uzbek', native: 'Oʻzbekcha' },
  id: { label: 'Indonesian', native: 'Bahasa Indonesia' },
  es: { label: 'Spanish', native: 'Español' },
  fr: { label: 'French', native: 'Français' },
  it: { label: 'Italian', native: 'Italiano' },
};

export const seoKeywords = [
  'Telegram message forwarding',
  'Telegram auto forward bot',
  'telegram forwarding bot self-hosted',
  'Telegram automation',
  'self-hosted Telegram forwarder',
  'Telegram message routing',
  'multi-source multi-target forwarding',
  'Telegram forwarding rules and filters',
  'Telegram channel mirroring and crossposting',
  'Telegram content filter bot',
  'Telegram message cleaner',
  'Telegram AI rewriting and translation',
  'copy-protected Telegram channel forwarding',
  'Telegram bot forwarding without admin rights',
  'open source Telegram automation platform',
  'Telegram multi-tenant isolation',
  'Telegram forwarding API',
  'Telegram forwarding queue and retries',
];

export const site = {
  name: repoInfo.name,
  version: repoInfo.version,
  description:
    'Self-hosted Telegram message routing platform: M sources → N targets per task, per-task content rules, optional AI rewriting, durable ordered delivery, and a fail-closed multi-tenant control plane.',
  repoUrl: repoInfo.repoUrl,
  maintainer: repoInfo.maintainer,
  stats: repoInfo.stats,
  locales: repoInfo.locales,
};

export { repoInfo };
