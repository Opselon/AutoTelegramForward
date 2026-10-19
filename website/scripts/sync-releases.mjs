/**
 * Release history ingestion for the product site.
 *
 * The releases page used to carry a hand-written list that stopped at v2.0.0
 * while the repository had shipped v2.2.4 — the page announced "latest: v2.2.4"
 * in its own table of contents and then rendered v2.0.0 as the newest entry.
 * Nothing could catch that, because the list was prose.
 *
 * This script derives the list from two authorities in order of strength:
 *
 *   1. The repository's own tags (`git for-each-ref refs/tags`) — always
 *      available, gives every version and its date.
 *   2. The published GitHub releases, when a token is present — gives the real
 *      title, the release notes and the downloadable assets.
 *
 * A tokenless or offline build must still produce a correct *ordering* (the
 * newest tag wins) and must never publish less than the committed snapshot, so
 * the fallback replays the previous file verbatim instead of rewriting it with
 * degraded entries. Exit code is always 0: the site has to stay buildable for
 * anyone who clones the repository.
 */

import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, '../../');
const websiteDir = path.resolve(__dirname, '../');
const outPath = path.join(websiteDir, 'src/data/releases.json');

const REPO = 'Opselon/AutoTelegramForward';
const MAX_RELEASES = 14;

const log = (...args) => console.log('📦', ...args);

/** Semver-descending comparison of `vX.Y.Z` (pre-release suffixes sort last). */
function compareVersions(a, b) {
  const norm = (v) =>
    String(v)
      .replace(/^v/i, '')
      .split('-')[0]
      .split('.')
      .map((n) => parseInt(n, 10) || 0);
  const [x, y] = [norm(a), norm(b)];
  for (let i = 0; i < 3; i += 1) {
    if ((x[i] || 0) !== (y[i] || 0)) return (y[i] || 0) - (x[i] || 0);
  }
  // `1.0.0-rc1` is older than `1.0.0`.
  const pre = (v) => (String(v).includes('-') ? 0 : 1);
  return pre(b) - pre(a);
}

/** Every release tag in the checkout, newest first, with its commit date. */
function readTags() {
  try {
    const raw = execSync(
      "git for-each-ref --sort=-creatordate --format='%(refname:short)\t%(creatordate:short)' refs/tags",
      { cwd: rootDir },
    ).toString();
    return raw
      .split('\n')
      .map((line) => line.split('\t'))
      .filter(([tag]) => /^v?\d+\.\d+\.\d+/.test(tag || ''))
      .map(([tag, date]) => ({ tag, version: tag.replace(/^v/i, ''), date: date || '' }));
  } catch (error) {
    log(`git tags unavailable (${error.message.split('\n')[0]}) — falling back to the committed snapshot`);
    return [];
  }
}

/** A token from the environment, or from an authenticated `gh` CLI if present. */
function resolveToken() {
  const fromEnv = process.env.GH_TOKEN || process.env.GITHUB_TOKEN;
  if (fromEnv) return fromEnv.trim();
  try {
    return execSync('gh auth token', { cwd: rootDir, stdio: ['ignore', 'pipe', 'ignore'] })
      .toString()
      .trim();
  } catch {
    return '';
  }
}

/** One-line summary from a release body: the first paragraph that is prose. */
function summarise(body) {
  const lines = String(body || '').split('\n');
  const paragraph = lines
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#') && !line.startsWith('- ') && !line.startsWith('* ') && !line.startsWith('>') && line !== '---');
  const first = paragraph[0] || '';
  return first.replace(/[*_`]/g, '').replace(/\s+/g, ' ').slice(0, 400);
}

/** Bullet lines of a release body, minus the ones that are pure markup. */
function highlightsOf(body) {
  const out = [];
  for (const rawLine of String(body || '').split('\n')) {
    const line = rawLine.trim();
    const bullet = line.match(/^(?:[-*+]|\d+\.)\s+(.+)$/);
    if (!bullet) continue;
    let text = bullet[1]
      .replace(/`/g, '')
      .replace(/\*\*/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .trim();
    if (!text || text.length < 8) continue;
    if (/^(download|assets?|checksums?)\b/i.test(text)) continue;
    if (out.includes(text)) continue;
    out.push(text.length > 180 ? `${text.slice(0, 177)}…` : text);
    if (out.length >= 12) break;
  }
  return out;
}

function fromGitHub(release, tagDates) {
  const tag = release.tag_name || '';
  const version = tag.replace(/^v/i, '');
  return {
    version: `v${version}`,
    tag,
    date: (release.published_at || '').slice(0, 10) || tagDates[tag] || '',
    title: (release.name || `v${version}`).replace(/^AutoTelegramForward\s*/i, '').trim(),
    summary: summarise(release.body),
    highlights: highlightsOf(release.body),
    url: release.html_url || `https://github.com/${REPO}/releases/tag/${tag}`,
    prerelease: Boolean(release.prerelease),
    assets: (release.assets || []).map((asset) => ({
      name: asset.name,
      size: asset.size,
      downloads: asset.download_count,
      url: asset.browser_download_url,
    })),
    source: 'github-api',
  };
}

async function fetchReleases(token) {
  if (!token) return null;
  try {
    const response = await fetch(
      `https://api.github.com/repos/${REPO}/releases?per_page=30`,
      {
        headers: {
          Accept: 'application/vnd.github+json',
          Authorization: `Bearer ${token}`,
          'User-Agent': 'AutoTelegramForward-website',
        },
        signal: AbortSignal.timeout(15000),
      },
    );
    if (!response.ok) {
      log(`GitHub API answered ${response.status} — falling back to the committed snapshot`);
      return null;
    }
    const json = await response.json();
    if (!Array.isArray(json) || json.length === 0) return null;
    return json;
  } catch (error) {
    log(`GitHub API unreachable (${error.message}) — falling back to the committed snapshot`);
    return null;
  }
}

const tags = readTags();
const tagDates = Object.fromEntries(tags.map((entry) => [entry.tag, entry.date]));

let releases = null;
const token = resolveToken();
const published = await fetchReleases(token);

if (published) {
  releases = published
    .map((release) => fromGitHub(release, tagDates))
    .filter((release) => release.tag)
    .slice(0, MAX_RELEASES);
  log(`GitHub API: ${releases.length} releases, newest ${releases[0]?.tag}`);
} else if (fs.existsSync(outPath)) {
  // Replay the committed snapshot byte-for-byte: a tokenless build must not
  // overwrite real notes with degraded ones (and must leave git clean).
  const existing = JSON.parse(fs.readFileSync(outPath, 'utf8'));
  if (Array.isArray(existing) && existing.length > 0) {
    releases = existing.map((entry) => ({ ...entry, source: entry.source || 'snapshot' }));
    log(`reusing committed snapshot: ${releases.length} releases, newest ${releases[0]?.tag}`);
  }
}

if (!releases) {
  // Last resort: tag names and dates. Ordering is still correct, which is the
  // part that must never be wrong.
  releases = tags.slice(0, MAX_RELEASES).map((entry) => ({
    version: entry.tag,
    tag: entry.tag,
    date: entry.date,
    title: entry.tag,
    summary: 'Release notes are published on GitHub.',
    highlights: [],
    url: `https://github.com/${REPO}/releases/tag/${entry.tag}`,
    prerelease: false,
    assets: [],
    source: 'git-tags',
  }));
  log(`git tags only: ${releases.length} releases`);
}

// Ordering is derived, never inherited: the newest semver tag is the newest
// release, whatever order the source answered in.
releases.sort((a, b) => compareVersions(a.tag, b.tag));

const versionFile = (() => {
  try {
    return fs.readFileSync(path.join(rootDir, 'VERSION'), 'utf8').trim();
  } catch {
    return '';
  }
})();

let newest = releases[0];
if (versionFile && compareVersions(`v${versionFile}`, newest.tag) < 0) {
  // The checkout is newer than any published release. Say so instead of
  // presenting the previous release as current.
  releases.unshift({
    version: `v${versionFile}`,
    tag: '',
    date: new Date().toISOString().slice(0, 10),
    title: `v${versionFile} (unreleased)`,
    summary: 'In the repository: this version is tagged and published with the release workflow.',
    highlights: [],
    url: `https://github.com/${REPO}/releases`,
    prerelease: false,
    assets: [],
    source: 'checkout',
  });
  newest = releases[0];
  log(`checkout VERSION ${versionFile} is ahead of the newest tag ${newest.tag || '—'} — marked unreleased`);
}

fs.writeFileSync(outPath, `${JSON.stringify(releases, null, 2)}\n`, 'utf8');
log(`${releases.length} releases written — newest ${newest.version} (${newest.date || 'no date'}, source: ${newest.source})`);
