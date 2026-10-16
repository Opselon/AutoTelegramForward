import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Theme-parity audit.
 *
 * The light theme is a set of overrides layered on markup that was written for
 * a dark canvas. That is only safe if it is exhaustive: a single dark-only
 * utility left behind produces dark text on a light background, which no
 * amount of build success reveals. This test enumerates every colour utility
 * the source actually uses, maps it to the CSS class selector it compiles to,
 * and fails if global.css carries no `html[data-theme='light']` counterpart.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.resolve(__dirname, '../src');
const distDir = path.resolve(__dirname, '../dist');
const cssPath = path.join(srcDir, 'styles/global.css');

let errors = 0;
const fail = (message) => {
  console.error(`❌ ${message}`);
  errors++;
};

const css = fs.readFileSync(cssPath, 'utf8');

// Utilities whose appearance is theme-independent: brand gradients, decorative
// dots and ring/shadow colours that are already scheme-neutral. Gradient stops
// are checked as whole combinations further down instead.
const THEME_NEUTRAL = new Set([
  'from-tg-blue',
  'to-brand-500',
  'from-brand-500',
  'to-tg-blue',
  'bg-purple-400',
]);

// Already correctly light-safe values (white text on a coloured chip, etc.).
const COLOUR_FAMILIES =
  '(?:slate|sky|emerald|amber|rose|purple|violet|brand|tg|white|black|surface)';
const UTILITY_RE = new RegExp(
  `\\b(?:bg|text|border|divide|ring|shadow|placeholder|from|via|to)-${COLOUR_FAMILIES}(?:-[a-z]+)?(?:-\\d{2,3})?(?:/\\d+)?`,
  'g',
);

const walk = (dir) =>
  fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === 'assets' ? [] : walk(full);
    return /\.(astro|ts|mjs)$/.test(entry.name) ? [full] : [];
  });

const files = walk(srcDir);
const used = new Map(); // utility -> file that uses it
for (const file of files) {
  const text = fs.readFileSync(file, 'utf8');
  for (const match of text.matchAll(UTILITY_RE)) {
    if (!used.has(match[0])) used.set(match[0], path.relative(srcDir, file));
  }
}

/** Tailwind escapes `/` in class selectors; `:` in variants too. */
const escapeSelector = (utility) =>
  `.${utility.replace(/[/:[\]]/g, (char) => `\\${char}`)}`;

/**
 * A utility counts as covered when the light layer defines it — either bare or
 * behind the interaction variant the markup actually used (`hover:bg-x`,
 * `focus:ring-x`, `group-hover:text-x`).
 */
const declaredInLight = (utility) => {
  const selector = escapeSelector(utility);
  return [
    `html[data-theme='light'] ${selector}`,
    `html[data-theme='light'] .hover\\:${selector.slice(1)}:hover`,
    `html[data-theme='light'] .focus\\:${selector.slice(1)}:focus`,
    `html[data-theme='light'] .group:hover .group-hover\\:${selector.slice(1)}`,
  ].some((needle) => css.includes(needle));
};

const uncovered = [];
for (const [utility, file] of [...used].sort()) {
  if (THEME_NEUTRAL.has(utility)) continue;
  // Gradient stops are verified as whole combinations further down: a stop on
  // its own means nothing, because the combination rule overrides the generated
  // background-image rather than the `--tw-gradient-*` variables.
  if (/^(from|via|to)-/.test(utility)) continue;
  if (declaredInLight(utility)) continue;
  uncovered.push(`${utility} (${file})`);
}

if (uncovered.length) {
  fail(
    `${uncovered.length} colour utilities have no light-theme counterpart:\n     - ${uncovered.join('\n     - ')}`,
  );
} else {
  console.log(`🎨 ${used.size} colour utilities all have a light-theme counterpart.`);
}

// Gradient combinations are dark-canvas artefacts: each `bg-gradient-to-*`
// element with slate/sky stops needs one explicit light background-image.
const ungradiented = [];
for (const file of files) {
  const text = fs.readFileSync(file, 'utf8');
  for (const match of text.matchAll(/class="([^"]*bg-gradient-to-[^"]*)"/g)) {
    const classes = match[1].split(/\s+/);
    const gradient = classes.find((c) => c.startsWith('bg-gradient-to-'));
    const stops = classes.filter((c) => /^(from|via|to)-/.test(c));
    if (!stops.some((s) => /slate|sky-950/.test(s))) continue; // brand gradients are scheme-neutral
    const selector = `html[data-theme='light'] .${gradient}.${stops.map((s) => s.replace('/', '\\/')).join('.')}`;
    if (!css.includes(selector)) {
      ungradiented.push(`${selector} (${path.relative(srcDir, file)})`);
    }
  }
}
if (ungradiented.length) {
  fail(`gradients without a light variant:\n     - ${ungradiented.join('\n     - ')}`);
} else {
  console.log('🌈 Every dark-canvas gradient has a light variant.');
}

// Both themes must define the full token set, and the built CSS must carry both
// palettes — a token defined in only one theme is a silent regression.
const tokens = ['--bg-page', '--bg-elevated', '--text-primary', '--text-secondary', '--text-muted', '--border-color', '--accent'];
const lightBlock = css.slice(css.indexOf("html[data-theme='light'] {"), css.indexOf('}', css.indexOf("html[data-theme='light'] {")));
for (const token of tokens) {
  if (!new RegExp(`${token}\\s*:`).test(lightBlock)) fail(`light theme does not redefine ${token}`);
  if (!new RegExp(`${token}\\s*:`).test(css.slice(0, css.indexOf("html[data-theme='light'] {")))) {
    fail(`dark theme does not define ${token}`);
  }
}

if (fs.existsSync(distDir)) {
  const cssFiles = fs
    .readdirSync(path.join(distDir, '_astro'))
    .filter((name) => name.endsWith('.css'));
  const bundle = cssFiles.map((name) => fs.readFileSync(path.join(distDir, '_astro', name), 'utf8')).join('\n');
  const themes = (bundle.match(/data-theme/g) || []).length;
  if (themes < 2) fail('built stylesheet does not contain the light-theme overrides');
  else console.log(`📦 Built CSS carries ${themes} data-theme selectors.`);

  const fonts = fs.readdirSync(path.join(distDir, '_astro')).filter((name) => name.endsWith('.woff2'));
  if (fonts.length < 2) fail('self-hosted webfonts are missing from the build output');
  else console.log(`🔤 ${fonts.length} self-hosted font files in the build output.`);
}

if (errors > 0) {
  console.error(`\n❌ Theme coverage audit failed with ${errors} error(s).`);
  process.exit(1);
}
console.log('\n🎉 Theme coverage, gradient parity and font self-hosting verified.');
