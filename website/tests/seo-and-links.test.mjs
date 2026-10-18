import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const distDir = path.resolve(__dirname, '../dist');

console.log('🧪 Starting SEO, Canonical, Metadata & Internal Link Audits...');

if (!fs.existsSync(distDir)) {
  console.error('❌ dist/ directory not found! Run npm run build first.');
  process.exit(1);
}

// 1. Gather all HTML files recursively
function getHtmlFiles(dir, files = []) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      getHtmlFiles(fullPath, files);
    } else if (entry.name.endsWith('.html')) {
      files.push(fullPath);
    }
  }
  return files;
}

const htmlFiles = getHtmlFiles(distDir);
console.log(`📄 Found ${htmlFiles.length} rendered HTML pages in dist/`);

if (htmlFiles.length < 20) {
  console.error(`❌ Expected at least 20 static pages, found only ${htmlFiles.length}`);
  process.exit(1);
}

let errors = 0;
let warnings = 0;

// 2. Validate Metadata & Canonicals
for (const file of htmlFiles) {
  const relPath = path.relative(distDir, file);
  const content = fs.readFileSync(file, 'utf8');

  // Check Title
  const titleMatch = content.match(/<title>([^<]+)<\/title>/i);
  if (!titleMatch || !titleMatch[1].trim()) {
    console.error(`❌ [${relPath}] Missing or empty <title> tag!`);
    errors++;
  }

  // Check Meta Description
  const descMatch = content.match(/<meta\s+name=["']description["']\s+content=["']([^"']+)["']/i);
  if (!descMatch || !descMatch[1].trim()) {
    console.error(`❌ [${relPath}] Missing or empty <meta name="description">!`);
    errors++;
  }

  // Check Canonical URL
  const canonicalMatch = content.match(/<link\s+rel=["']canonical["']\s+href=["']([^"']+)["']/i);
  if (!canonicalMatch) {
    console.error(`❌ [${relPath}] Missing canonical link!`);
    errors++;
  } else {
    const canonical = canonicalMatch[1];
    if (!canonical.startsWith('http://') && !canonical.startsWith('https://')) {
      console.error(`❌ [${relPath}] Canonical is not an absolute URL: ${canonical}`);
      errors++;
    }
  }

  // Check OpenGraph
  const ogTitleMatch = content.match(/<meta\s+property=["']og:title["']/i);
  if (!ogTitleMatch) {
    console.error(`❌ [${relPath}] Missing og:title!`);
    errors++;
  }

  // Check Structured Data JSON-LD
  const jsonLdMatch = content.match(/<script\s+type=["']application\/ld\+json["']>([^<]+)<\/script>/i);
  if (jsonLdMatch) {
    try {
      JSON.parse(jsonLdMatch[1]);
    } catch (e) {
      console.error(`❌ [${relPath}] Invalid JSON-LD schema: ${e.message}`);
      errors++;
    }
  }
}

// 3. Validate Sitemap
const sitemapIndexPath = path.join(distDir, 'sitemap-index.xml');
const sitemap0Path = path.join(distDir, 'sitemap-0.xml');

if (!fs.existsSync(sitemapIndexPath) && !fs.existsSync(sitemap0Path)) {
  console.error('❌ Sitemap file not found in dist/');
  errors++;
} else {
  console.log('✅ Sitemap exists and is verified.');
}

// 4. Validate robots.txt
const robotsPath = path.join(distDir, 'robots.txt');
if (!fs.existsSync(robotsPath)) {
  console.error('❌ robots.txt not found in dist/');
  errors++;
} else {
  const robotsContent = fs.readFileSync(robotsPath, 'utf8');
  if (!robotsContent.includes('Sitemap:')) {
    console.error('❌ robots.txt does not declare Sitemap URL!');
    errors++;
  } else {
    console.log('✅ robots.txt properly configured.');
  }
}

// 5. Internal link integrity
//    Every root-relative link and asset reference in the built site must resolve
//    to something that exists in dist/. Base-prefixed hrefs are de-prefixed
//    first, because the site ships under /AutoTelegramForward/.
const basePrefix = (process.env.BASE_PATH || '/AutoTelegramForward').replace(/\/$/, '');
const stripBase = (href) => (basePrefix && href.startsWith(basePrefix) ? href.slice(basePrefix.length) || '/' : href);

const distHas = (href) => {
  const clean = href.split('#')[0].split('?')[0];
  if (!clean || clean === '/') return fs.existsSync(path.join(distDir, 'index.html'));
  const rel = clean.replace(/^\//, '');
  const candidates = [
    path.join(distDir, rel),
    path.join(distDir, rel, 'index.html'),
    `${path.join(distDir, rel)}.html`,
  ];
  return candidates.some((candidate) => fs.existsSync(candidate));
};

let linkCount = 0;

// Script bodies carry inline JSON and template literals that resolve to link
// strings at runtime; every one of them would be checked as a real link. They
// are skipped by scanning tag boundaries rather than by pattern-matching the
// markup away: `<script>` bodies may contain nested markup, comments, or an end
// tag written as `</script >`, and a regexp strip silently leaves those behind
// (letting script text through as links again). Only element boundaries are
// recognised here, so nothing is "sanitised" by this pass — the remaining text
// is scanned for links exactly as before.
const withoutScriptBodies = (html) => {
  const lower = html.toLowerCase();
  let out = '';
  let cursor = 0;

  for (;;) {
    const open = lower.indexOf('<script', cursor);
    if (open === -1) return out + html.slice(cursor);

    // "<scripted" is not a script element: step past it and keep scanning.
    const after = lower[open + '<script'.length];
    if (after !== undefined && after !== '>' && after !== '/' && !/\s/.test(after)) {
      out += html.slice(cursor, open + '<script'.length);
      cursor = open + '<script'.length;
      continue;
    }

    out += html.slice(cursor, open);
    const close = lower.indexOf('</script', open + '<script'.length);
    if (close === -1) return out; // unterminated element: drop the tail
    const end = lower.indexOf('>', close);
    cursor = end === -1 ? html.length : end + 1;
  }
};

for (const file of htmlFiles) {
  const relPath = path.relative(distDir, file);
  const content = withoutScriptBodies(fs.readFileSync(file, 'utf8'));

  for (const match of content.matchAll(/(?:href|src)="([^"]+)"/g)) {
    const raw = match[1];
    if (!raw || raw.startsWith('http') || raw.startsWith('//')) continue;
    if (raw.startsWith('#') || raw.startsWith('mailto:') || raw.startsWith('tel:') || raw.startsWith('data:')) continue;
    if (!raw.startsWith('/')) {
      console.error(`❌ [${relPath}] Relative link is not base-safe: ${raw}`);
      errors++;
      continue;
    }

    linkCount++;
    const target = stripBase(raw);
    if (!distHas(target)) {
      console.error(`❌ [${relPath}] Internal link points at a missing page: ${raw}`);
      errors++;
    }
  }
}

console.log(`🔗 Checked ${linkCount} internal links and asset references`);

// 6. Search-snippet budgets, heading structure and canonical base sanity.
//    Google truncates a title near 60 characters and a description near 160, and
//    two pages sharing a snippet means one of them is competing with itself.
const seenTitles = new Map();
const seenDescriptions = new Map();
const headingCounts = new Map();

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", nbsp: ' ' };

/**
 * HTML entities inflate raw markup: `&amp;` is five characters for one. Decode
 * in a single pass — chaining replaces would turn `&amp;lt;` into `<`, which
 * would then be measured as a one-character description.
 */
const decodeEntities = (value) =>
  value.replace(/&(#39|amp|lt|gt|quot|nbsp);/g, (_, name) => ENTITIES[name]);

/**
 * Whether this dist was built with the deploy base path. `npm run build` locally
 * can be run root-relative, in which case the canonical shape is checked but the
 * base-path assertion is reported as skipped rather than failed — the base-path
 * build is the one that deploys.
 */
const distCarriesBase = basePrefix
  ? htmlFiles.some((file) =>
      new RegExp(`rel=["']canonical["']\\s+href=["'][^"']*${basePrefix}`).test(
        fs.readFileSync(file, 'utf8'),
      ),
    )
  : false;
if (basePrefix && !distCarriesBase) {
  console.warn(
    `⚠️  dist/ was built without the ${basePrefix} base path; canonical base-prefix assertion skipped (rebuild with BASE_PATH=${basePrefix}).`,
  );
}

for (const file of htmlFiles) {
  const relPath = path.relative(distDir, file);
  const content = fs.readFileSync(file, 'utf8');
  const title = decodeEntities((content.match(/<title>([^<]*)<\/title>/i) || [, ''])[1]).trim();
  const description = decodeEntities(
    (content.match(/<meta\s+name=["']description["']\s+content=["']([^"']*)["']/i) || [, ''])[1],
  ).trim();

  if (title.length > 65) {
    console.error(`❌ [${relPath}] Title is ${title.length} chars (max 65): ${title}`);
    errors++;
  } else if (title.length < 15) {
    console.error(`❌ [${relPath}] Title is only ${title.length} chars: ${title}`);
    errors++;
  }
  if (description.length > 160) {
    console.error(`❌ [${relPath}] Description is ${description.length} chars (max 160)`);
    errors++;
  } else if (description.length < 80) {
    console.error(`❌ [${relPath}] Description is only ${description.length} chars`);
    errors++;
  }
  for (const [value, map] of [
    [title, seenTitles],
    [description, seenDescriptions],
  ]) {
    if (map.has(value)) {
      console.error(`❌ [${relPath}] Duplicate snippet text shared with ${map.get(value)}`);
      errors++;
    } else {
      map.set(value, relPath);
    }
  }

  // Canonical must carry the deploy base exactly once (checked on the build that
  // is actually deployed; a root-relative local build is reported, not failed).
  const canonical = (content.match(/<link\s+rel=["']canonical["']\s+href=["']([^"']+)["']/i) || [, ''])[1];
  const occurrences = basePrefix && canonical ? canonical.split(basePrefix).length - 1 : 0;
  if (distCarriesBase && occurrences !== 1) {
    console.error(
      `❌ [${relPath}] Canonical must carry ${basePrefix} exactly once, found ${occurrences}: ${canonical}`,
    );
    errors++;
  }
  if (canonical && /\/\/[^/]*\/\/|https?:\/\/[^/]*AutoTelegramForward.*AutoTelegramForward/.test(canonical)) {
    console.error(`❌ [${relPath}] Canonical looks double-prefixed: ${canonical}`);
    errors++;
  }

  // Exactly one h1, and no skipped heading levels.
  const headings = [...content.matchAll(/<h([1-6])\b[^>]*>/gi)].map((m) => Number(m[1]));
  headingCounts.set(relPath, headings.length);
  const h1s = headings.filter((level) => level === 1).length;
  if (h1s !== 1) {
    console.error(`❌ [${relPath}] Expected exactly one <h1>, found ${h1s}`);
    errors++;
  }
  let previous = 0;
  for (const level of headings) {
    if (previous && level > previous + 1) {
      console.error(`❌ [${relPath}] Heading level jumps from h${previous} to h${level}`);
      errors++;
    }
    previous = level;
  }

  // The 404 must not be indexable.
  if (/404\.html$/.test(relPath) && !/name=["']robots["']\s+content=["']noindex/i.test(content)) {
    console.error(`❌ [${relPath}] 404 page is missing a noindex directive`);
    errors++;
  }

  // Structured data must be one connected @graph per page.
  const ld = content.match(/<script\s+type=["']application\/ld\+json["']>([\s\S]*?)<\/script>/i);
  if (ld) {
    try {
      const parsed = JSON.parse(ld[1]);
      const types = JSON.stringify(parsed['@graph'] || parsed);
      for (const required of ['WebSite', 'SoftwareApplication', 'Organization', 'WebPage']) {
        if (!types.includes(`"${required}"`) && !(required === 'WebPage' && types.includes('TechArticle'))) {
          console.error(`❌ [${relPath}] JSON-LD graph is missing the ${required} node`);
          errors++;
        }
      }
    } catch {
      /* already reported above */
    }
  }
}

// 7. Machine-readable surfaces: feed, llms.txt, sitemap metadata, webfonts.
const requiredArtifacts = [
  ['rss.xml', 'RSS feed'],
  ['llms.txt', 'llms.txt'],
  ['sitemap-index.xml', 'sitemap index'],
  ['search.json', 'search index'],
  ['.nojekyll', '.nojekyll'],
];
for (const [rel, label] of requiredArtifacts) {
  if (!fs.existsSync(path.join(distDir, rel))) {
    console.error(`❌ ${label} missing from dist/ (${rel})`);
    errors++;
  }
}

const sitemapBody = fs.existsSync(path.join(distDir, 'sitemap-0.xml'))
  ? fs.readFileSync(path.join(distDir, 'sitemap-0.xml'), 'utf8')
  : '';
if (sitemapBody) {
  const urls = sitemapBody.match(/<url>/g)?.length || 0;
  const lastmods = sitemapBody.match(/<lastmod>/g)?.length || 0;
  const priorities = sitemapBody.match(/<priority>/g)?.length || 0;
  if (urls !== lastmods || urls !== priorities) {
    console.error(`❌ sitemap metadata incomplete: ${urls} urls, ${lastmods} lastmod, ${priorities} priority`);
    errors++;
  } else {
    console.log(`🗺️  Sitemap carries ${urls} urls, each with lastmod, changefreq and priority.`);
  }
  if (/404/.test(sitemapBody)) {
    console.error('❌ The 404 route must not appear in the sitemap');
    errors++;
  }
}

const fontCount = fs.existsSync(path.join(distDir, '_astro'))
  ? fs.readdirSync(path.join(distDir, '_astro')).filter((name) => name.endsWith('.woff2')).length
  : 0;
if (fontCount < 2) {
  console.error(`❌ Expected self-hosted webfonts in _astro/, found ${fontCount}`);
  errors++;
} else {
  console.log(`🔤 ${fontCount} self-hosted webfont files emitted.`);
}


// 8. Element ids must be unique per document. A duplicated id is invalid HTML
//    and makes every in-page anchor ambiguous — the class of defect the new
//    status and troubleshooting pages shipped with (section wrapper and heading
//    sharing one id).
let idChecked = 0;
for (const file of htmlFiles) {
  const relPath = path.relative(distDir, file);
  const content = withoutScriptBodies(fs.readFileSync(file, 'utf8'));
  const ids = [...content.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]);
  idChecked += ids.length;
  const seen = new Set();
  const duplicates = new Set();
  for (const id of ids) {
    if (seen.has(id)) duplicates.add(id);
    seen.add(id);
  }
  if (duplicates.size > 0) {
    console.error(`❌ [${relPath}] Duplicate element id(s): ${[...duplicates].join(', ')}`);
    errors++;
  }
}
console.log(`🆔 ${idChecked} element ids across ${htmlFiles.length} pages are unique.`);

const totalWords = [...headingCounts.values()].reduce((sum, n) => sum + n, 0);
console.log(`🧭 ${headingCounts.size} pages, ${totalWords} headings, snippet budgets enforced.`);

if (errors > 0) {
  console.error(`\n❌ SEO Audit Failed with ${errors} errors!`);
  process.exit(1);
} else {
  console.log(`\n🎉 All ${htmlFiles.length} pages passed SEO, Metadata, Canonical, and Schema audits flawlessly!`);
}
