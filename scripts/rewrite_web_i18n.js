/**
 * ONE-SHOT migration of the dashboard from `isRtl ? 'فارسی' : 'English'` (and
 * its `t('فارسی', 'English')` cousin) to catalog keys.
 *
 * The dashboard had 458 language branches over 13 files. Hand-editing them is a
 * guarantee of drift (one file gets `t('ui_save')`, the next keeps a ternary),
 * so the rewrite is mechanical and reviewable: the TypeScript compiler parses
 * each file, every language branch becomes `t('<key>')` with the English text
 * as the key's source, and the same English string always yields the same key.
 *
 * This script is one-shot provenance, not a build step: after the migration the
 * tree is the source of truth and the script must not be re-run (it would find
 * no branches, and would re-key anything a human later renamed).
 *
 * Usage, from `web/frontend` (the bundled typescript is the parser):
 *   NODE_PATH=./node_modules node ../../scripts/rewrite_web_i18n.js --report
 *   NODE_PATH=./node_modules node ../../scripts/rewrite_web_i18n.js --write <keys.json>
 *
 * `--report` prints the candidate inventory and the keys it would create.
 * `--write` rewrites the sources and dumps the key table (key -> {en, fa,
 * params}) for the catalog files, which are authored separately.
 */
const ts = require('typescript')
const fs = require('fs')
const path = require('path')

const FRONTEND = path.resolve(__dirname, '..', 'web', 'frontend')
const SRC = path.join(FRONTEND, 'src')
const PERSIAN = /[\u0600-\u06ff]/
const SKIP_FILES = new Set([
  // Generated from the bot catalog by scripts/gen_web_setting_labels.py.
  path.join(SRC, 'settingLabels.generated.ts'),
  // Rewritten by hand: a plain key table, not a JSX language branch.
  path.join(SRC, 'addons.ts'),
  path.join(SRC, 'providerKey.ts'),
  path.join(SRC, 'settingProvenance.ts'),
])
const SKIP_DIRS = new Set(['locales', 'node_modules', 'dist'])

const args = process.argv.slice(2)
const MODE = args.includes('--write') ? 'write' : 'report'
const KEYS_OUT = args.find((a) => !a.startsWith('--')) || '/root/.hermes/cache/scratch/web_keys.json'

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue
    const p = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(p, out)
    else if ((p.endsWith('.tsx') || p.endsWith('.ts')) && !SKIP_FILES.has(p)) out.push(p)
  }
  return out.sort()
}

/* ------------------------------------------------------------------ keys --- */

const EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{2190}-\u{21FF}\u{FE0F}\u{200D}\u{20E3}\u{2122}\u{00A9}\u{00AE}]/gu

/** A stable, readable key from the English source text. */
function slug(text) {
  const base = text
    .replace(EMOJI, ' ')
    .replace(/[\u0600-\u06ff]/g, ' ')
    .replace(/&[a-z]+;/gi, ' ')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 64)
    .replace(/_+$/g, '')
  return base
}

/** Area of a file, used only to disambiguate a key collision. */
function areaOf(file) {
  const rel = path.relative(SRC, file)
  const parts = rel.split(path.sep)
  const name = parts[parts.length - 1].replace(/\.tsx?$/, '')
  if (parts[0] === 'pages') return name.toLowerCase()
  if (name === 'App') return 'app'
  return name
    .replace(/Panel$/, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
}

/* ------------------------------------------------ template -> {placeholder} -- */

const NOT_A_NAME = new Set([
  'Math', 'Object', 'String', 'Number', 'Boolean', 'JSON', 'Array', 'Date', 'React',
  'join', 'slice', 'round', 'reduce', 'values', 'keys', 'entries', 'length', 'map',
  'filter', 'split', 'trim', 'toLowerCase', 'toUpperCase', 'toLocaleString', 'toString',
  'startsWith', 'includes', 'push', 'concat', 'floor', 'max', 'min', 'abs', 'now',
  'String', 'parseInt', 'parseFloat', 'replace', 'padStart', 'Boolean', 'localeCompare',
])

function paramNameFor(exprText, used) {
  const idents = (exprText.match(/[A-Za-z_$][A-Za-z0-9_$]*/g) || []).filter(
    (n) => !NOT_A_NAME.has(n),
  )
  let name = (idents[0] || 'value').replace(/[^A-Za-z0-9_]/g, '')
  if (!name) name = 'value'
  if (name.length > 1 && /^[a-z]/.test(name)) name = name
  let candidate = name
  let i = 2
  while (used.has(candidate)) candidate = `${name}_${i++}`
  used.add(candidate)
  return candidate
}

/**
 * A template literal (or plain string) as a catalog value plus its parameters.
 * `` `forwarded from ${origins.join(' / ')}` `` -> `forwarded from {origins}`
 * with `origins` -> `origins.join(' / ')`, which is what the call site passes.
 */
function valueAndParams(node, sf, usedNames) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
    return { value: node.text, params: [] }
  }
  if (!ts.isTemplateExpression(node)) return null
  let value = node.head.text
  const params = []
  for (const span of node.templateSpans) {
    const exprText = span.expression.getText(sf)
    const name = paramNameFor(exprText, usedNames)
    params.push({ name, expr: exprText })
    value += '{' + name + '}' + span.literal.text
  }
  return { value, params }
}

/* ---------------------------------------------------------- collect edits --- */

const files = walk(SRC)
const units = [] // one per rewrite site
const manual = [] // sites the script refuses to guess at

for (const file of files) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const lineOf = (n) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1
  const add = (node, unit) => units.push({ file, start: node.getStart(sf), end: node.getEnd(), sf, ...unit })
  const note = (node, why) =>
    manual.push({ file: path.relative(FRONTEND, file), line: lineOf(node), why, text: node.getText(sf).replace(/\s+/g, ' ').slice(0, 180) })

  const inTernaryBranch = (node) => {
    let p = node.parent
    while (p) {
      if (ts.isConditionalExpression(p) && (p.whenTrue === node || p.whenFalse === node)) return true
      if (ts.isJsxElement(p) || ts.isJsxAttribute(p)) return false
      p = p.parent
    }
    return false
  }

  const visit = (node) => {
    /* 1. `isRtl ? 'فارسی' : 'English'` and friends, with or without templates. */
    if (ts.isConditionalExpression(node)) {
      const a = node.whenTrue
      const b = node.whenFalse
      const lit = (n) =>
        ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n) || ts.isTemplateExpression(n)
      if (lit(a) && lit(b)) {
        const usedNames = new Set()
        const A = valueAndParams(a, sf, usedNames)
        const B = valueAndParams(b, sf, usedNames)
        const aFa = PERSIAN.test(A.value)
        const bFa = PERSIAN.test(B.value)
        if (aFa !== bFa) {
          const faSide = aFa ? A : B
          const enSide = aFa ? B : A
          if (PERSIAN.test(enSide.value)) {
            note(node, 'both branches are Persian')
          } else if (faSide.params.length !== enSide.params.length) {
            note(node, 'branches interpolate a different number of values')
          } else {
            add(node, {
              kind: 'branch',
              en: enSide.value,
              fa: faSide.value,
              params: enSide.params,
              faParams: faSide.params,
            })
          }
        }
      }
    }

    /* 2. `t('فارسی', 'English')` / `t('English', 'فارسی')` — the old helper. */
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === 't' &&
      node.arguments.length === 2 &&
      node.arguments.every((a) => ts.isStringLiteral(a) || ts.isTemplateExpression(a) ||
        ts.isNoSubstitutionTemplateLiteral(a))
    ) {
      const usedNames = new Set()
      const A = valueAndParams(node.arguments[0], sf, usedNames)
      const B = valueAndParams(node.arguments[1], sf, usedNames)
      if (A && B) {
        const aFa = PERSIAN.test(A.value)
        const bFa = PERSIAN.test(B.value)
        if (aFa !== bFa) {
          const faSide = aFa ? A : B
          const enSide = aFa ? B : A
          if (!PERSIAN.test(enSide.value) && faSide.params.length === enSide.params.length) {
            add(node, { kind: 'call', en: enSide.value, fa: faSide.value, params: enSide.params, faParams: faSide.params })
          } else {
            note(node, 't(fa, en) pair the script cannot split')
          }
        }
      }
    }

    /* 3. A bare Persian literal: Persian-only copy, a leak for every other language. */
    const isStr =
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateExpression(node)
    if (isStr && !ts.isJsxAttribute(node.parent)) {
      const raw = ts.isTemplateExpression(node) ? node.getText(sf) : node.text
      if (PERSIAN.test(raw) && !inTernaryBranch(node) && !isInsideCallPair(node, sf)) {
        manual.push({
          file: path.relative(FRONTEND, file),
          line: lineOf(node),
          why: 'bare Persian literal (no English side)',
          text: node.getText(sf).replace(/\s+/g, ' ').slice(0, 200),
          node,
          sf,
          size: node.getEnd() - node.getStart(sf),
        })
      }
    }

    ts.forEachChild(node, visit)
  }

  // A string that is one argument of the old two-language `t(...)` is handled
  // by rule 2; do not report it again as a bare leak.
  const isInsideCallPair = (node) =>
    ts.isCallExpression(node.parent) && node.parent.expression.getText(sf) === 't' && node.parent.arguments.length === 2

  visit(sf)
}

/* --------------------------------------------------------------- key table -- */

/** Deduplicate by English value; one English string is one key. */
const byEn = new Map()
const keyTable = []
const takenKeys = new Map() // key -> english
for (const u of units.sort((x, y) => (x.file + x.start).localeCompare(y.file + y.start))) {
  const existing = byEn.get(u.en)
  if (existing) {
    if (existing.fa !== u.fa) {
      manual.push({
        file: path.relative(FRONTEND, u.file),
        line: 0,
        why: 'same English, two Persian drafts',
        text: u.en,
      })
    }
    u.key = existing.key
    continue
  }
  let base = 'ui_' + (slug(u.en) || 'text')
  let key = base
  if (takenKeys.has(key) && takenKeys.get(key) !== u.en) {
    key = `ui_${areaOf(u.file)}_${slug(u.en)}`
  }
  const entry = { key, en: u.en, fa: u.fa, params: u.params.map((p) => p.name) }
  byEn.set(u.en, entry)
  takenKeys.set(key, u.en)
  keyTable.push(entry)
  u.key = key
}

/* ------------------------------------------------------------------ apply -- */

function replacement(u) {
  if (!u.params.length) return `t('${u.key}')`
  const pairs = u.params.map((p) => `${p.name}: ${p.expr}`).join(', ')
  return `t('${u.key}', { ${pairs} })`
}

const byFile = new Map()
for (const u of units) {
  if (!u.key) continue
  if (!byFile.has(u.file)) byFile.set(u.file, [])
  byFile.get(u.file).push(u)
}

let rewritten = 0
if (MODE === 'write') {
  for (const [file, list] of byFile) {
    const text = fs.readFileSync(file, 'utf8')
    // Innermost first: a replacement never overlaps one already applied.
    const sorted = list.slice().sort((a, b) => b.start - a.start)
    let out = text
    let lastStart = Infinity
    for (const u of sorted) {
      if (u.end > lastStart) continue // nested inside an already-replaced branch
      out = out.slice(0, u.start) + replacement(u) + out.slice(u.end)
      lastStart = u.start
      rewritten += 1
    }
    fs.writeFileSync(file, out)
  }
  fs.writeFileSync(KEYS_OUT, JSON.stringify(keyTable, null, 1))
}

console.log(`files scanned: ${files.length}`)
console.log(`rewrite sites: ${units.length} (keys: ${keyTable.length})`)
if (MODE === 'write') console.log(`rewritten: ${rewritten}; key table -> ${KEYS_OUT}`)
console.log(`manual sites (bare Persian): ${manual.filter((m) => m.why === 'bare Persian literal (no English side)').length}`)
if (args.includes('--list-bare')) {
  for (const m of manual.filter((x) => x.why === 'bare Persian literal (no English side)')) {
    console.log(`  ${m.file}:${m.line}  ${m.text}`)
  }
}
const otherManual = manual.filter((m) => m.why !== 'bare Persian literal (no English side)')
console.log(`other manual sites: ${otherManual.length}`)
for (const m of otherManual) console.log(`  ${m.file}:${m.line}  ${m.why}\n     ${m.text}`)
if (MODE !== 'write') {
  for (const k of keyTable.slice(0, 40)) console.log(`  ${k.key} = ${JSON.stringify(k.en)}`)
}
