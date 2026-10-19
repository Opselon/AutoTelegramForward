import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const root = path.resolve(__dirname, '..');

console.log('🧪 Auditing the API reference against the parsed route table...');

const repoInfoPath = path.join(root, 'src/data/repo-info.json');
const surfacePath = path.join(root, 'src/data/api-surface.ts');

for (const [file, hint] of [
  [repoInfoPath, 'Run `node scripts/sync-repo-content.mjs` (npm run build does it) to regenerate it from api/main.go.'],
  [surfacePath, 'It is the hand-written half of the reference: one entry per registered route.'],
]) {
  if (!fs.existsSync(file)) {
    console.error(`❌ ${path.relative(root, file)} is missing. ${hint}`);
    process.exit(1);
  }
}

const repoInfo = JSON.parse(fs.readFileSync(repoInfoPath, 'utf8'));
const surface = fs.readFileSync(surfacePath, 'utf8');

const routes = Array.isArray(repoInfo.apiRoutes) ? repoInfo.apiRoutes : [];
if (routes.length < 40) {
  console.error(
    `❌ repo-info.json carries only ${routes.length} routes. The parser is likely broken — the route table in the documentation would silently shrink, which is exactly the drift this gate exists to catch.`,
  );
  process.exit(1);
}

let errors = 0;

/**
 * The route table is parsed from `api/main.go`; this file supplies the prose.
 * Joining them is the whole point: a registered route with no entry ships
 * undocumented, and an entry with no route is a page describing an endpoint the
 * binary does not answer on.
 *
 * Entries are read with a line-anchored pattern rather than by importing the
 * module, so the gate does not depend on a TypeScript loader being present.
 */
const described = new Set();
for (const match of surface.matchAll(/^\s*'([A-Z]+ \/[^']*)':\s*\{/gm)) {
  described.add(match[1]);
}

const registered = new Map(routes.map((route) => [`${route.method} ${route.path}`, route]));

console.log(`🔎 ${registered.size} registered routes, ${described.size} described routes.`);

for (const [key, route] of registered) {
  if (!described.has(key)) {
    console.error(
      `❌ ${key} is registered in api/main.go (${route.handler}, group "${route.group}") but has no entry in src/data/api-surface.ts. Add one — a new route must not ship undocumented.`,
    );
    errors++;
  }
}

for (const key of described.keys()) {
  if (!registered.has(key)) {
    console.error(
      `❌ ${key} is described in src/data/api-surface.ts but no longer registered in api/main.go. Remove the entry, or restore the route — the reference must not promise an endpoint that does not exist.`,
    );
    errors++;
  }
}

// Every entry needs the one field the explorer renders unconditionally.
const summaries = [...surface.matchAll(/^\s*summary:/gm)].length;
if (summaries < described.size) {
  console.error(
    `❌ Only ${summaries} of ${described.size} entries declare a summary. Every documented route must say what it is for — the explorer renders that line on every card.`,
  );
  errors++;
}

// The auth taxonomy has to stay expressible: each value the parser can emit must
// be handled by the page, so a new class cannot render as a blank chip.
const authValues = new Set(routes.map((route) => route.auth));
for (const value of authValues) {
  if (!['admin', 'bearer', 'none'].includes(value)) {
    console.error(`❌ Unknown auth class "${value}" in repo-info.json — the explorer would render an empty chip.`);
    errors++;
  }
}
for (const value of ['admin', 'bearer', 'none']) {
  if (!authValues.has(value)) {
    console.error(
      `❌ No route reports auth "${value}". One class has disappeared from the parsed table — either a whole mux is no longer being read, or the taxonomy changed without the explorer being updated.`,
    );
    errors++;
  }
}

// Every group the parser produces needs a blurb, or the "what each group is
// scoped to" section renders an empty card.
const blurbsBlock = surface.slice(
  surface.indexOf('export const groupBlurbs'),
  surface.indexOf('export function scopeNoteFor'),
);
const blurbed = new Set(
  [...blurbsBlock.matchAll(/^\s*'?([A-Za-z][^':\n]*)'?:\s*\{/gm)].map((match) => match[1].trim()),
);
const groups = new Set(routes.map((route) => route.group));

for (const group of groups) {
  if (!blurbed.has(group)) {
    console.error(`❌ Route group "${group}" has no entry in groupBlurbs (src/data/api-surface.ts).`);
    errors++;
  }
}
for (const group of blurbed) {
  if (!groups.has(group)) {
    console.error(`❌ groupBlurbs describes "${group}", which no route belongs to any more.`);
    errors++;
  }
}

if (errors > 0) {
  console.error(`\n❌ API reference audit failed with ${errors} error(s).`);
  process.exit(1);
}

console.log(
  `🎉 All ${registered.size} registered routes are documented, in ${groups.size} groups, across ${authValues.size} auth classes.`,
);
