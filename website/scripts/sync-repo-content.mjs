import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '../../');
const websiteDir = path.resolve(__dirname, '../');

console.log('🔄 Running AutoTelegramForward Content Synchronization...');

// 1. Read VERSION
let version = '17.0.0';
try {
  const versionPath = path.join(rootDir, 'VERSION');
  if (fs.existsSync(versionPath)) {
    const raw = fs.readFileSync(versionPath, 'utf8').trim();
    if (raw) version = raw;
  }
} catch (e) {
  console.warn('Could not read VERSION file:', e.message);
}

// 2. Read git commit and date
let gitCommit = 'HEAD';
let gitDate = new Date().toISOString();
try {
  gitCommit = execSync('git rev-parse --short HEAD', { cwd: rootDir }).toString().trim();
  gitDate = execSync('git log -1 --format=%cI', { cwd: rootDir }).toString().trim();
} catch (e) {
  console.warn('Git inspection skipped:', e.message);
}

// 3. Supported Message Types verified in entities.py / durable_queue.py
const supportedMediaTypes = [
  { type: 'Text Messages', status: 'Full Support', desc: 'Preserves formatting entities, emojis, links, custom styling' },
  { type: 'Photos & Images', status: 'Full Support', desc: 'Direct forward, copy without tag, or re-upload bypass' },
  { type: 'Photo / Video Albums', status: 'Full Support', desc: 'Atomic media group aggregation and delivery' },
  { type: 'Videos & MP4', status: 'Full Support', desc: 'High-speed streaming, captions, thumbnail preservation' },
  { type: 'Documents & PDFs', status: 'Full Support', desc: 'Arbitrary documents, files, PDFs up to Telegram limits' },
  { type: 'Audio & Music', status: 'Full Support', desc: 'MP3/M4A music files with artist/title metadata' },
  { type: 'Voice Messages', status: 'Full Support', desc: 'OGG Opus voice notes, waveform preservation' },
  { type: 'Video Notes', status: 'Full Support', desc: 'Round video messages (telescope)' },
  { type: 'Stickers & Animations', status: 'Full Support', desc: 'Static WEBP, animated TGS, and video WEBM stickers' },
  { type: 'Polls & Quizzes', status: 'Full Support', desc: 'Standard polls, multiple answers, quiz mode' },
  { type: 'Locations & Contacts', status: 'Full Support', desc: 'Geolocations, vCard contacts, venues' },
  { type: 'Protected Content', status: 'Bypass Supported', desc: 'Automated download & re-upload for copy-restricted channels' }
];

// 4. Feature Highlights verified against codebase
const verifiedFeatures = [
  {
    category: 'Forwarding & Routing',
    items: [
      'Copy mode (no source tag) vs Direct Forward (with source tag)',
      'Multi-destination fan-out (forwarding to 1 to N targets simultaneously)',
      'Channel to Channel, Channel to Group, Group to Supergroup, and Bot to Channel',
      'Forum topic targeting (message_thread_id routing)',
      'Priority levels (high number evaluated first) and execution order',
      'Smart Routing: VIP vs Regular message classification and intermediate channel masking'
    ]
  },
  {
    category: 'Message Cleaning & Transformation',
    items: [
      'URL & Link removal (including markdown hyperlinks and Telegram links)',
      '@Username & mention removal (@channel, @user)',
      '#Hashtag removal with multi-lingual unicode support (#crypto, #خبر)',
      'Emoji removal and excessive blank line compaction',
      'Phone number removal (+1..., 09...)',
      'Inline keyboard button cleaning',
      'Custom words removal list with case-insensitive matching',
      'Text replacement mapping (old_word -> new_word or deletion)'
    ]
  },
  {
    category: 'Filters & Access Control',
    items: [
      'Keyword Whitelist (drop unless keyword is present)',
      'Keyword Blacklist (drop if forbidden keyword exists)',
      'Safe regular expression pattern matching (ReDoS protected)',
      'Granular media-type blocking (video, audio, stickers, voice, text, links)',
      'Minimum and maximum message length thresholds',
      'Service message auto-drop (join, leave, pin events)',
      'Re-evaluation of filters during message edits with destination message deletion on violation'
    ]
  },
  {
    category: 'Templates & Shortcodes',
    items: [
      'Flexible header and footer templates per forwarding rule',
      '12 validated shortcodes: [[ORIGIN_TEXT]], [[ORIGIN_NAME]], [[ORIGIN_USERNAME]], [[ORIGIN_USERID]], [[ORIGIN_POST_ID]], [[ORIGIN_CHAT_ID]], [[ORIGIN_POST_DATE]], [[ORIGIN_NAME_URL]], [[SOURCE_NAME]], [[SENDER_CHAT]], [[FORWARD_FROM_CHAT]], [[FROM_USER]]',
      'Dynamic source attribution links for public and private channels'
    ]
  },
  {
    category: 'Performance & Operations',
    items: [
      'Durable persistent queue with exponential backoff and jitter',
      'Account FloodWait detection with automated pause and resumption',
      'Auto-disable rule when account is banned in channel (CHAT_WRITE_FORBIDDEN)',
      'Media group (album) aggregation window (0.8s debounce)',
      'Edit message synchronization with max_time_edit guard window',
      'Delete synchronization (source message deleted -> target deleted)',
      'AES-256-GCM encrypted backup, restore, and migration bundles'
    ]
  },
  {
    category: 'Security & Multi-Tenancy',
    items: [
      'Strict server-side tenant isolation with Fail-Closed policy',
      'Zero cross-tenant visibility across Bot, Web Dashboard, and gRPC',
      'Separate per-user session storage and credential encryption',
      'Structured operational audit logs with [PIPELINE:*] tags'
    ]
  }
];

// 5. Shortcodes verified in domain/services.py
const supportedShortcodes = [
  { code: '[[ORIGIN_TEXT]]', desc: 'Message body after cleaner and filter passes', example: 'Breaking market report...' },
  { code: '[[ORIGIN_NAME]]', desc: 'Title of the source channel or supergroup', example: 'Crypto News Daily' },
  { code: '[[ORIGIN_USERNAME]]', desc: 'Public @handle of source channel', example: 'cryptonewsdaily' },
  { code: '[[ORIGIN_USERID]]', desc: 'Author / sender numerical Telegram ID', example: '10842918' },
  { code: '[[ORIGIN_POST_ID]]', desc: 'Original post numerical ID in source channel', example: '4921' },
  { code: '[[ORIGIN_CHAT_ID]]', desc: 'Numerical ID of source channel', example: '-1001695513745' },
  { code: '[[ORIGIN_POST_DATE]]', desc: 'Original post publication timestamp (UTC)', example: '2026-10-13 14:00 UTC' },
  { code: '[[ORIGIN_NAME_URL]]', desc: 'Markdown hyperlink to the original Telegram post', example: '[Crypto News](https://t.me/cryptonewsdaily/4921)' },
  { code: '[[SOURCE_NAME]]', desc: 'Alias for source channel or chat name', example: 'Crypto News Daily' },
  { code: '[[SENDER_CHAT]]', desc: 'Chat ID or title where the post was sent', example: '-1001695513745' },
  { code: '[[FORWARD_FROM_CHAT]]', desc: 'Original channel if incoming post was a forward', example: 'Reuters Telegram' },
  { code: '[[FROM_USER]]', desc: 'Full display name of original author/user', example: 'John Doe' },
];

// 6. Release history data
const releases = [
  {
    version: 'v17.0.0',
    date: '2026-10-13',
    title: 'Production Upgrade: Advanced Cleaner, Link Filters, Header/Footer Shortcodes & Bugfixes',
    summary: 'Comprehensive production upgrade introducing granular cleaner formats, media and link filters, 12 validated shortcodes, max_time_edit synchronization limit, copy-protected content bypass, and multi-tenant profile management.',
    highlights: [
      'Cleaner module with independent URL, @mention, #hashtag, emoji, phone, and custom words stripping',
      'Dedicated link blocking filter and granular media type blocking (video, audio, stickers, text, voice)',
      '12 validated header and footer shortcodes with dynamic channel hyperlink attribution',
      'max_time_edit rule threshold to ignore stale source edits with /forward command',
      'Automatic re-evaluation of whitelist/blacklist on edits with destination deletion on violation',
      'Bypass for copy-protected channels (ChatForwardsRestricted) via secure download and re-upload',
      'Batch media group (album) atomic forwarding and copying',
      'Account time-limit auto-pause and auto-resume in ClientPool and DurableQueueManager',
      'Multi-tenant profile dashboard with Restart Setup and Reset Config options'
    ]
  },
  {
    version: 'v16.0.0',
    date: '2026-10-12',
    title: 'High-Throughput Pipeline & Diagnostic Tracing',
    summary: 'Enhanced logging architecture with [PIPELINE:*] correlation IDs, improved error diagnostics, and queue backpressure controls.',
    highlights: [
      'Structured pipeline logs covering Normalize, Route, Filter, Transform, Dispatch, Delivered',
      'Improved gRPC retry policy with exponential backoff',
      'Pyrogram Unicode surrogate-pair decoding fixes for emojis and regional indicators',
      'Queue throughput metrics and latency counters'
    ]
  },
  {
    version: 'v15.0.0',
    date: '2026-10-10',
    title: 'Multi-Tenant Security Hardening & Bot UI Redesign',
    summary: 'Fail-closed ownership enforcement across all domain use-cases, gRPC services, and inline Telegram bot wizards.',
    highlights: [
      'Fail-closed tenant authorization on all rule and session mutations',
      'Interactive button-driven control panel with FSM wizard',
      'Isolated PVResponder configuration per owner_user_id',
      'Clean onboarding for unlinked users'
    ]
  }
];

const repoInfo = {
  name: 'AutoTelegramForward',
  repoUrl: 'https://github.com/Opselon/AutoTelegramForward',
  starsUrl: 'https://img.shields.io/github/stars/Opselon/AutoTelegramForward?style=social',
  license: 'MIT',
  version,
  gitCommit,
  gitDate,
  syncTimestamp: new Date().toISOString(),
  verifiedFeatures,
  supportedMediaTypes,
  supportedShortcodes,
  author: 'Opselon',
};

// Write output files
fs.writeFileSync(
  path.join(websiteDir, 'src/data/repo-info.json'),
  JSON.stringify(repoInfo, null, 2),
  'utf8'
);

fs.writeFileSync(
  path.join(websiteDir, 'src/data/releases.json'),
  JSON.stringify(releases, null, 2),
  'utf8'
);

console.log(`✅ Synchronized repo metadata for AutoTelegramForward ${version} (${gitCommit})!`);
