#!/usr/bin/env node
/**
 * Fail-closed gate: user-visible web copy that never reaches a translation catalog.
 *
 * The dashboard shipped 458 inline `isRtl ? '…' : '…'` branches and hundreds of
 * `<button>Save</button>` / `placeholder="Search"` literals that no catalog ever
 * saw, so nine of the ten supported locales rendered English (and Persian leaked
 * into English). `scripts/scan_web_ui_copy.js` could only *list* candidates; this
 * gate fails the build unless every offender is either pinned in the content-keyed
 * baseline (`scripts/.web_ui_copy_baseline.txt`) or gone.
 *
 * What is copy (and therefore must resolve through the catalog):
 *   - JSX text nodes (`<span>Save changes</span>`, `{n} tasks remaining`),
 *   - string literals in copy attributes: placeholder / title / aria-label /
 *     aria-description / aria-valuetext / alt / label / tooltip,
 *   - string branches of `cond ? 'A' : 'B'`, `flag && 'A'`, `'A' ?? 'B'` and
 *     template literals, when the expression lands in a copy position
 *     (JSX child, copy attribute, copy-named object property or variable),
 *   - copy-named object properties (`{ label: 'Active' }`).
 *
 * What is not copy (documented exemptions, see `isTechnical`):
 *   brand names and proper nouns (`AutoTelegramForward`, `PRO GATEWAY`,
 *   `Anthropic`), URLs / routes / MIME types, CSS values,
 *   field names and identifiers (`filter_rule_id`, `atf_lang`, `error`),
 *   log levels and env constants (`ERROR`, `ATF_API_URL`), service names
 *   (`core`, `api`, `logger`, `web`) and their health badges (`Core:6001`),
 *   shell commands (`systemctl --user …`),
 *   format examples (`{source_title}`, `yyyy-MM-dd`, `sk-...`, `{"key": "value"}`,
 *   `JOIN, SCAM, PROMO`, `ms | v`), escaped glyph entities in JSX text (`&gt;`),
 *   numbers/emoji/punctuation.
 *
 * Deliberate non-targets, because they are translations rather than copy:
 * the catalog itself (`src/locales/**`), generated label tables (`*.generated.ts`,
 * or any file carrying an `@generated` marker), the `t()` implementation, and
 * any literal passed to a translation call (`t('key')`, `translate('key')`).
 * `<Trans>` children are also skipped: that component translates its children.
 *
 * Baseline format is shared with `scripts/ui_copy_baseline.py` (the reference
 * implementation): one `path|normalized text` entry per tolerated occurrence,
 * `#` comments allowed, whitespace-normalized so inserted lines/re-indentation
 * do not redden CI.
 *
 * Usage (from the repository root):
 *   node scripts/audit_web_ui_copy.js                  # report + exit 1 on new copy
 *   node scripts/audit_web_ui_copy.js --report         # also list baselined hits
 *   node scripts/audit_web_ui_copy.js --update-baseline
 *
 * Requires only the `typescript` package bundled in `web/frontend/node_modules`.
 */
'use strict';

const fs = require('fs');
const path = require('path');

// --------------------------------------------------------------------------- #
// CLI
// --------------------------------------------------------------------------- #
function parseArgs(argv) {
  const opts = {
    root: path.resolve(__dirname, '..'),
    src: '',
    baseline: '',
    typescript: '',
    update: false,
    report: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--update-baseline') opts.update = true;
    else if (arg === '--report') opts.report = true;
    else if (arg === '--root') opts.root = path.resolve(argv[++i]);
    else if (arg === '--src') opts.src = path.resolve(argv[++i]);
    else if (arg === '--baseline') opts.baseline = path.resolve(argv[++i]);
    else if (arg === '--typescript') opts.typescript = path.resolve(argv[++i]);
    else if (arg === '--help' || arg === '-h') {
      console.log('usage: node scripts/audit_web_ui_copy.js '
        + '[--update-baseline] [--report] [--root DIR] [--src DIR] '
        + '[--baseline FILE] [--typescript DIR]');
      process.exit(0);
    } else {
      console.error(`unknown argument: ${arg}`);
      process.exit(2);
    }
  }
  return opts;
}

const OPTS = parseArgs(process.argv.slice(2));
const ROOT = OPTS.root;
const SRC = OPTS.src || path.join(ROOT, 'web', 'frontend', 'src');
const BASELINE = OPTS.baseline || path.join(ROOT, 'scripts', '.web_ui_copy_baseline.txt');

function loadTypescript() {
  const candidates = [
    OPTS.typescript,
    path.join(ROOT, 'web', 'frontend', 'node_modules', 'typescript'),
    path.join(__dirname, '..', 'web', 'frontend', 'node_modules', 'typescript'),
    'typescript',
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch (err) {
      /* try the next candidate */
    }
  }
  return null;
}

const ts = loadTypescript();
if (!ts) {
  console.error('::error::typescript is not installed — cannot audit web UI copy. '
    + 'Run `npm ci` in web/frontend (or pass --typescript DIR).');
  process.exit(2);
}

// --------------------------------------------------------------------------- #
// Shared baseline semantics (mirror of scripts/ui_copy_baseline.py)
// --------------------------------------------------------------------------- #
const HEADER = '# Content-keyed baseline for the UI-copy gate: one '
  + "'path|normalized text'\n# entry per currently tolerated offender. "
  + 'Regenerate with --update-baseline.\n# Never hand-edit an entry to silence '
  + 'a new string: localize it instead.\n';

function normalize(text) {
  return String(text).replace(/\s+/g, ' ').trim();
}

function readBaseline(file) {
  if (!fs.existsSync(file)) return [];
  const entries = [];
  for (const raw of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const cut = line.indexOf('|');
    entries.push(cut === -1 ? line : `${line.slice(0, cut).trim()}|${normalize(line.slice(cut + 1))}`);
  }
  return entries;
}

function writeBaseline(file, entries) {
  const ordered = entries.slice().sort();
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, HEADER + (ordered.length ? `${ordered.join('\n')}\n` : ''), 'utf8');
  return ordered.length;
}

function evaluate(offenders, entries) {
  const remaining = new Map();
  for (const entry of entries) remaining.set(entry, (remaining.get(entry) || 0) + 1);
  let matched = 0;
  const unmatched = [];
  for (const offender of offenders) {
    const key = `${offender.path}|${normalize(offender.text)}`;
    const left = remaining.get(key) || 0;
    if (left > 0) {
      remaining.set(key, left - 1);
      matched += 1;
    } else {
      unmatched.push(offender);
    }
  }
  let unused = 0;
  for (const count of remaining.values()) unused += count;
  return { pinned: entries.length, matched, unmatched, unused };
}

// --------------------------------------------------------------------------- #
// What counts as copy
// --------------------------------------------------------------------------- #
const COPY_ATTRS = new Set([
  'placeholder', 'title', 'alt', 'label', 'tooltip',
  'aria-label', 'aria-description', 'aria-roledescription', 'aria-valuetext',
]);
const COPY_PROPS = new Set([
  'label', 'title', 'placeholder', 'alt', 'tooltip', 'message', 'description',
  'hint', 'caption', 'heading', 'subtitle', 'emptylabel', 'emptytext',
  'arialabel', 'text', 'prompt', 'notice', 'summary',
]);
const COPY_VAR_HINT = /(label|text|title|hint|placeholder|message|msg|desc|caption|tooltip|aria|alt|error|notice|warn|banner|empty|help|prompt|reason|summary|subtitle|heading|confirm)/i;
const I18N_CALLEES = /^(?:.*\.)?(?:t|tt|translate|trans|gettext|formatMessage|i18n\.t|\$t)$/;
// A catalog key, in the shape the repo uses (`ui_task_logs_title`, `rules.header`).
// A capitalized word (`'Save'`) or an RTL string is not a key: those are copy.
const KEY_LIKE = /^[a-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)*$/;
const ALERT_CALLEES = /^(?:window\.)?(?:alert|confirm|prompt)$|^set(?:Error|Message|Notice|Status|Toast|Banner|Hint|Warning)$|(?:toast|notify|showToast)$/i;
const TRANSLATION_COMPONENTS = /^(?:.*\.)?(?:Trans|FormattedMessage)$/;

const LATIN_WORD = /[A-Za-z]{2,}/;
const OTHER_SCRIPT = /[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0750-\u077F\u0900-\u097F\u0E00-\u0E7F\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]/;

function hasWordRun(text) {
  if (LATIN_WORD.test(text)) return true;
  if (!OTHER_SCRIPT.test(text)) return false;
  const letters = text.replace(/[^\p{L}]/gu, '');
  return letters.length >= 2;
}

/**
 * Proper nouns — product and company names the dashboard renders verbatim.
 *
 * A name reads the same in all ten locales, so "translating" one is the defect
 * (`Anthropic` is not a Persian company, and a localized `PRO GATEWAY` would
 * stop naming the edition in the header lockup). Keep this list to names only:
 * a descriptive label that happens to mention a product — `Custom
 * (OpenAI-compatible)` — is copy and must come from the catalog.
 */
const PROPER_NOUNS = new Set([
  'AutoTelegramForward', // the product
  'PRO GATEWAY', // the product edition, locked up next to it in the header
  'Anthropic', // AI provider company (`OpenAI` the identifier rule already keeps)
]);

function isTechnical(text) {
  const raw = normalize(text);
  if (!raw) return true;
  if (/^\{[A-Za-z0-9_]+\}$/.test(raw)) return true; // format placeholder {source_title}
  // A badge carries its decoration in front of a token (`❌ ERROR`, `⬇ CSV`,
  // `🔒 AES-GCM`): judge the token, not the emoji.
  const value = raw.replace(/^[^\p{L}\p{N}]+/u, '');
  if (!value) return true;
  if (!hasWordRun(value)) return true; // numbers, emoji, punctuation, separators
  if (PROPER_NOUNS.has(value)) return true; // product / company name
  if (/^(?:https?:|mailto:|tel:|wss?:|data:|blob:)/i.test(value) || value.includes('://')) return true;
  if (/^(?:application|text|image|audio|video|multipart)\/[a-z0-9.+-]+$/i.test(value)) return true;
  if (/^(?:systemctl|sudo|docker|npm|npx|pip|python3?|pytest|git|curl|cd|ls|export|flutter|dart|go)\s/.test(value)) return true;
  if (/^[\d\s.,:%+\-/]+$/.test(value)) return true;
  // Service health badge: the service name the API reports and the port it
  // listens on (`Core:6001`, `Log:6002`, `Web:8088`) — a status readout.
  if (/^[A-Z][A-Za-z]{1,7}:\d{2,5}$/.test(value)) return true;
  // Format examples show the *shape* of the input rather than saying something:
  // an elided secret after its prefix (`sk-...`, `123456789:AA…`), an
  // enumerated list of all-caps keywords (`JOIN, SCAM, PROMO`), a hex sample
  // (`e.g. 0123456789abcdef…`), a JSON object (`{"key": "value"}`). The elision
  // must follow a separator, so a sentence ellipsis (`Loading…`) stays copy.
  if (/^[A-Za-z0-9][A-Za-z0-9_-]*[-_:][A-Za-z0-9]*(?:\.{3}|…)$/.test(value)) return true;
  if (/^[A-Z0-9_]+(?:,\s*[A-Z0-9_]+)+$/.test(value)) return true;
  if (/^e\.g\.\s+[0-9a-f]+$/i.test(raw)) return true;
  if (/^\{\s*"[^"]{1,32}"\s*:\s*"[^"]{0,32}"\s*\}$/.test(raw)) return true;
  // A unit readout (`ms | v` — a duration and a version in a stat line): every
  // piece is a unit symbol of at most two letters. Three or more (`on/off`) is
  // a word, so it stays copy.
  const readout = value.split(/[|·/]/);
  if (readout.length > 1 && readout.every((piece) => /^[A-Za-z]{0,2}$/.test(piece.trim()))) return true;
  // A field name echoed with its separator in an audit row (`rule=8f3a1c…`).
  if (/^[a-z_][a-z0-9_]*=$/.test(value)) return true;
  // An escaped entity in JSX text (`&gt;`) reaches the parser undecoded; it is a
  // glyph, not a Latin word (`&amp;`, `&nbsp;`, `&#62;`).
  if (/^&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);$/.test(raw)) return true;
  if (/^(?:[\d.]+(?:px|rem|em|%|vh|vw|s|ms|fr)?|#[0-9a-f]{3,8}|rgba?\(|hsla?\(|calc\(|var\(|url\(|cubic-bezier\()/i.test(value)) return true;
  if (/^\/[a-z0-9_{}.\-/:]*$/i.test(value)) return true; // route / path
  if (/^[a-z0-9_{}.\-]+(?:\/[a-z0-9_{}.\-]+)+$/i.test(value)) return true; // relative path
  if (/^[A-Za-z0-9_-]+\.(?:css|scss|tsx?|jsx?|json|md|py|dart|go|sh|ya?ml|html|svg|png|jpe?g)$/i.test(value)) return true;
  if (/^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/.test(value)) return true; // identifier / slug / field name
  if (/^[A-Z][A-Z0-9_]*$/.test(value)) return true; // log level / constant
  if (/^[A-Z][A-Z0-9]*(?:[-_/][A-Z0-9]+)+$/.test(value)) return true; // AES-GCM, application/json
  if (/^[a-z]+(?:[A-Z][a-z0-9]*)+$/.test(value)) return true; // camelCase identifier
  if (/^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$/.test(value)) {
    // One dotted identifier: `core`, `atf_lang`, `App.css` are technical; a
    // single capitalized word (`Save`, `Cancel`, `Retry`) is copy.
    return !/^[A-Z][a-z]+$/.test(value);
  }
  if (/^\^.*\$?$/.test(value) || /^\/.*\/[gimsuy]*$/.test(value)) return true; // regex literal body
  if (/^[\w.$]+\([^)]*\)$/.test(value)) return true; // code fragment, e.g. setLang(en)
  return false;
}

function isCopyText(text) {
  return hasWordRun(text) && !isTechnical(text);
}

function attrName(node) {
  if (!node || !node.name) return '';
  const name = node.name;
  return (ts.isIdentifier(name) ? name.text : name.getText()).toLowerCase();
}

function textOfName(name) {
  if (!name) return '';
  if (ts.isIdentifier(name)) return name.text;
  return name.getText();
}

function isTranslationComponent(tagName) {
  return Boolean(tagName) && TRANSLATION_COMPONENTS.test(tagName);
}

function insideTranslationComponent(node) {
  let current = node.parent;
  for (let depth = 0; current && depth < 6; depth += 1) {
    if (ts.isJsxElement(current) || ts.isJsxSelfClosingElement(current)) {
      const opening = ts.isJsxElement(current) ? current.openingElement : current;
      if (isTranslationComponent(opening.tagName.getText())) return true;
    }
    if (ts.isJsxAttribute(current)) {
      const owner = current.parent && current.parent.parent;
      if (owner && ts.isJsxElement(owner) && isTranslationComponent(owner.openingElement.tagName.getText())) {
        return true;
      }
    }
    current = current.parent;
  }
  return false;
}

/** Why this literal/template sits in a user-visible position, or null. */
function copyContext(node) {
  let current = node;
  for (let depth = 0; depth < 10; depth += 1) {
    const parent = current.parent;
    if (!parent) return null;
    if (ts.isParenthesizedExpression(parent)) {
      current = parent;
      continue;
    }
    if (ts.isConditionalExpression(parent)) {
      current = parent; // `cond ? 'A' : 'B'` — judged by where the ternary lands
      continue;
    }
    if (ts.isBinaryExpression(parent)) {
      const kind = parent.operatorToken.kind;
      if (kind === ts.SyntaxKind.AmpersandAmpersandToken
        || kind === ts.SyntaxKind.BarBarToken
        || kind === ts.SyntaxKind.QuestionQuestionToken
        || kind === ts.SyntaxKind.PlusToken) {
        current = parent;
        continue;
      }
      return null;
    }
    if (ts.isJsxExpression(parent)) {
      const holder = parent.parent;
      if (holder && ts.isJsxAttribute(holder)) {
        return COPY_ATTRS.has(attrName(holder)) ? 'attribute' : null;
      }
      if (holder && (ts.isJsxElement(holder) || ts.isJsxFragment(holder))) return 'jsx-child';
      return null;
    }
    if (ts.isJsxAttribute(parent)) {
      return COPY_ATTRS.has(attrName(parent)) ? 'attribute' : null;
    }
    if (ts.isPropertyAssignment(parent)) {
      return COPY_PROPS.has(textOfName(parent.name).toLowerCase()) ? 'property' : null;
    }
    if (ts.isVariableDeclaration(parent)) {
      return COPY_VAR_HINT.test(textOfName(parent.name)) ? 'variable' : null;
    }
    if (ts.isCallExpression(parent)) {
      const callee = parent.expression.getText();
      if (I18N_CALLEES.test(callee)) {
        const first = parent.arguments[0];
        // `t('ui_key')`, `t('ui_key', {name})`, `t('ui_key', params, lang)`:
        // a key-like first argument makes the whole call a catalog lookup, and
        // the remaining arguments (params, locale) are not copy.
        if (first && ts.isStringLiteral(first) && KEY_LIKE.test(first.text)) return null;
        // Anything else — `t(fa, en)`, the dashboard's old positional helper —
        // is `isRtl ? fa : en` wearing a function name: the arguments are copy.
        return 'positional-i18n';
      }
      return ALERT_CALLEES.test(callee) ? 'call' : null;
    }
    return null;
  }
  return null;
}

// --------------------------------------------------------------------------- #
// Walking the files
// --------------------------------------------------------------------------- #
const SKIP_PATH = [
  /\.d\.ts$/,
  /\.(?:test|spec)\.[cm]?[jt]sx?$/,
  /(?:^|\/)(?:locales|translations)\//,
  /\.generated\.[cm]?[jt]sx?$/,
  /(?:^|\/)i18n\.[cm]?[jt]sx?$/,
];

function isSkipped(relPath, source) {
  if (SKIP_PATH.some((pattern) => pattern.test(relPath))) return true;
  const head = source.split('\n', 6).join('\n');
  return /@generated|eslint-disable ui-copy-gate/.test(head);
}

function walk(dir, out) {
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'dist' || entry.name.startsWith('.')) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (/\.(?:tsx|jsx|ts|js|mts|cts)$/.test(entry.name)) out.push(full);
  }
  return out;
}

function displayText(node) {
  // Template literals become `prefix${…}suffix` so interpolation stays visible
  // and the baseline text does not depend on the expression's spelling.
  if (ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  const parts = [node.head.text];
  for (const span of node.templateSpans) {
    parts.push('${…}');
    parts.push(span.literal.text);
  }
  return parts.join('');
}

function scanFile(absPath, relPath) {
  const source = fs.readFileSync(absPath, 'utf8');
  if (isSkipped(relPath, source)) return [];
  const kind = /\.(?:tsx|jsx)$/.test(absPath) ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sourceFile = ts.createSourceFile(absPath, source, ts.ScriptTarget.Latest, true, kind);
  const found = [];

  const report = (node, text, template = false) => {
    if (!text) return;
    if (template) {
      // A template literal is only copy if one of its static segments is copy:
      // `BOT ${x}` is a token plus a value, `${n} tasks` is a sentence.
      const segments = [node.head.text, ...node.templateSpans.map((s) => s.literal.text)];
      if (!segments.some((segment) => isCopyText(segment))) return;
    } else if (!isCopyText(text)) return;
    const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    found.push({
      path: relPath,
      line: line + 1,
      column: character + 1,
      text: normalize(text),
    });
  };

  const visit = (node) => {
    if (ts.isJsxText(node)) {
      if (!insideTranslationComponent(node)) report(node, node.text);
      return;
    }
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      if (insideTranslationComponent(node)) return;
      if (copyContext(node)) report(node, node.text);
      return;
    }
    if (ts.isTemplateExpression(node)) {
      if (insideTranslationComponent(node)) return;
      if (copyContext(node)) report(node, displayText(node), true);
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

const files = walk(SRC, []);
if (files.length === 0) {
  console.error(`::error::no web sources found under ${SRC}`);
  process.exit(2);
}

const offenders = [];
for (const file of files) {
  const relPath = path.relative(ROOT, file).split(path.sep).join('/');
  offenders.push(...scanFile(file, relPath));
}
offenders.sort((a, b) => (a.path === b.path
  ? (a.line - b.line || a.column - b.column)
  : a.path.localeCompare(b.path)));

// --------------------------------------------------------------------------- #
// Gate
// --------------------------------------------------------------------------- #
const baselineLabel = path.relative(ROOT, BASELINE).split(path.sep).join('/') || BASELINE;

if (OPTS.update) {
  const size = writeBaseline(BASELINE, offenders.map((o) => `${o.path}|${normalize(o.text)}`));
  console.log(`baseline updated: ${size} offender(s) pinned in ${baselineLabel} `
    + `(from ${files.length} web source file(s))`);
  process.exit(0);
}

const result = evaluate(offenders, readBaseline(BASELINE));
if (OPTS.report) {
  for (const offender of offenders) {
    console.log(`  ${offender.path}:${offender.line}:${offender.column}: ${offender.text}`);
  }
}
if (result.unmatched.length) {
  for (const offender of result.unmatched) {
    console.log(`::error file=${offender.path},line=${offender.line}::${offender.text}`);
    console.log(`  ${offender.path}:${offender.line}:${offender.column}: ${offender.text}`);
  }
  console.log(`FAIL: ${result.unmatched.length} unlocalized UI string(s) not in ${baselineLabel}. `
    + 'Resolve them through the translation catalog (t(\'key\')); do not add them to the baseline.');
  process.exit(1);
}
console.log(`PASS: ${offenders.length} UI string(s) checked in ${files.length} file(s), `
  + `${result.matched} baselined, 0 new unlocalized copy (${baselineLabel})`);
if (result.unused > 0) {
  console.log(`note: backlog shrank — regenerate the baseline: ${result.unused} of `
    + `${result.pinned} pinned offender(s) in `
    + `${baselineLabel} no longer occur (${result.matched} still present). `
    + 'Regenerate with --update-baseline in a follow-up commit.');
}
process.exit(0);
