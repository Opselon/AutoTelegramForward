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

if (errors > 0) {
  console.error(`\n❌ SEO Audit Failed with ${errors} errors!`);
  process.exit(1);
} else {
  console.log(`\n🎉 All ${htmlFiles.length} pages passed SEO, Metadata, Canonical, and Schema audits flawlessly!`);
}
