/**
 * The dashboard catalog's contract, asserted without a browser.
 *
 * The dashboard used to carry 458 `isRtl ? '…' : '…'` branches and a pile of
 * Persian-only literals, so a missing translation was invisible: the screen just
 * showed the other language. The catalog makes the gaps countable, and this test
 * counts them:
 *
 * - ten locales, one key set, no empty value;
 * - the same `{placeholder}` set as the English reference, so a Persian sentence
 *   cannot quietly print a literal `{schema_2}`;
 * - no RTL characters under `en` (a Persian leak was rendered to English users);
 * - no U+FFFD anywhere (a mojibake'd translation);
 * - every `t('…')` key in `src/**` exists, so a typo fails here and not on a
 *   screen nobody opened.
 *
 * Run (what CI should do):
 *   node node_modules/typescript/bin/tsc src/locales/types.ts src/locales/index.ts \
 *     src/locales/en.ts src/locales/fa.ts src/locales/pt.ts src/locales/uz.ts \
 *     src/locales/ru.ts src/locales/id.ts src/locales/es.ts src/locales/fr.ts \
 *     src/locales/it.ts src/locales/zh.ts --ignoreConfig --outDir .catalog-build \
 *     --module esnext --target es2022 --moduleResolution bundler --skipLibCheck
 *   node tests/uiCatalog.test.mjs
 */
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  CATALOGS,
  SUPPORTED_LANGS,
  interpolate,
  makeT,
  t,
} from '../.catalog-build/index.js'
import { normalizeLang, REFERENCE_LANG } from '../.catalog-build/types.js'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

let failures = 0
const check = (name, fn) => {
  try {
    fn()
    console.log(`  ok   ${name}`)
  } catch (err) {
    failures += 1
    console.error(`  FAIL ${name}\n       ${err.message}`)
  }
}

const LANGS = ['en', 'fa', 'pt', 'uz', 'ru', 'id', 'es', 'fr', 'it', 'zh']
const reference = CATALOGS[REFERENCE_LANG]
const keys = Object.keys(reference)

/** `{name}` placeholders, as a sorted list, in order of appearance. */
const placeholders = (value) => (value.match(/\{([a-zA-Z0-9_]+)\}/g) ?? []).sort()

/** Right-to-left scripts: Hebrew, Arabic, Syriac, Thaana, NKo, presentation forms. */
const RTL = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]/

function* sourceFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) yield* sourceFiles(path)
    else if (/\.tsx?$/.test(entry.name)) yield path
  }
}

console.log('ui catalog')

check('the catalog ships exactly the ten languages, in picker order', () => {
  assert.deepEqual(Object.keys(CATALOGS), LANGS)
  assert.deepEqual(SUPPORTED_LANGS.map((l) => l.code), LANGS)
})

check('every endonym is non-empty, unique, and only Persian is RTL', () => {
  const names = SUPPORTED_LANGS.map((l) => l.endonym.trim())
  assert.equal(new Set(names).size, LANGS.length, 'duplicate endonym')
  for (const l of SUPPORTED_LANGS) assert.ok(l.endonym.trim(), `${l.code} has no endonym`)
  assert.deepEqual(
    SUPPORTED_LANGS.filter((l) => l.rtl).map((l) => l.code),
    ['fa'],
  )
})

check('the English reference is the fallback, not Persian', () => {
  assert.equal(REFERENCE_LANG, 'en')
  assert.equal(normalizeLang('de-DE'), 'en')
  assert.equal(normalizeLang('fa-IR'), 'fa')
  assert.equal(normalizeLang(''), 'en')
  assert.equal(normalizeLang('zh-Hans'), 'zh')
})

check('a substantial catalog, not an empty one', () => {
  assert.ok(keys.length > 500, `only ${keys.length} keys`)
})

check('all ten locales carry the same keys as the English reference', () => {
  for (const lang of LANGS) {
    const got = Object.keys(CATALOGS[lang]).sort()
    const want = [...keys].sort()
    const missing = want.filter((k) => !got.includes(k))
    const extra = got.filter((k) => !want.includes(k))
    assert.deepEqual(missing.slice(0, 10), [], `${lang} is missing keys`)
    assert.deepEqual(extra.slice(0, 10), [], `${lang} has keys English does not`)
    assert.equal(got.length, want.length, `${lang} key count`)
  }
})

check('no locale has an empty or whitespace-only value', () => {
  const empty = []
  for (const lang of LANGS) {
    for (const key of keys) {
      const value = CATALOGS[lang][key]
      if (typeof value !== 'string' || !value.trim()) empty.push(`${lang}/${key}`)
    }
  }
  assert.deepEqual(empty.slice(0, 10), [], `${empty.length} empty values`)
})

check('every locale keeps the English placeholder set', () => {
  const drift = []
  for (const key of keys) {
    const want = placeholders(reference[key])
    for (const lang of LANGS) {
      const got = placeholders(CATALOGS[lang][key])
      if (got.join(',') !== want.join(',')) {
        drift.push(`${key} [${lang}] en=${want.join('')} ${lang}=${got.join('')}`)
      }
    }
  }
  assert.deepEqual(drift.slice(0, 10), [], `${drift.length} placeholder mismatches`)
})

check('no English value contains RTL script', () => {
  const leaks = keys.filter((k) => RTL.test(reference[k]))
  assert.deepEqual(leaks.slice(0, 10), [], `${leaks.length} Persian/Hebrew leaks under en`)
})

check('no value anywhere contains U+FFFD', () => {
  const broken = []
  for (const lang of LANGS) {
    for (const key of keys) {
      if (CATALOGS[lang][key].includes('\uFFFD')) broken.push(`${lang}/${key}`)
    }
  }
  assert.deepEqual(broken.slice(0, 10), [], `${broken.length} mojibake values`)
})

check('no value is just its own key', () => {
  const echoes = keys.filter((k) => CATALOGS[REFERENCE_LANG][k] === k)
  assert.deepEqual(echoes.slice(0, 10), [], `${echoes.length} keys rendered verbatim`)
})

check('interpolate substitutes known names and leaves unknown ones alone', () => {
  assert.equal(interpolate('a {n} b', { n: 3 }), 'a 3 b')
  assert.equal(interpolate('a {n} b'), 'a {n} b')
  assert.equal(interpolate('a {missing} b', { n: 3 }), 'a {missing} b')
  assert.equal(interpolate('{a}{a}', { a: 'x' }), 'xx')
})

check('t renders the requested language, and falls back to English', () => {
  const key = keys.find((k) => placeholders(reference[k]).length === 0 && /[a-z]/i.test(reference[k]))
  assert.equal(t(key, undefined, 'en'), reference[key])
  assert.equal(t(key, undefined, 'ru'), CATALOGS.ru[key])
  // An unknown language code normalises to the reference rather than crashing.
  assert.equal(t(key, undefined, 'de'), reference[key])
  // An unknown key renders itself, loudly, instead of returning nothing.
  assert.equal(t('ui_no_such_key_exists', undefined, 'ru'), 'ui_no_such_key_exists')
})

check('makeT binds one language without touching the call site', () => {
  const key = keys.find((k) => placeholders(reference[k]).length === 0 && /[a-z]/i.test(reference[k]))
  for (const lang of LANGS) {
    assert.equal(makeT(lang)(key), CATALOGS[lang][key], lang)
  }
  assert.equal(makeT(null)(key), reference[key])
})

check('every t(…) key used in src/** exists in the catalog', () => {
  const used = new Map()
  for (const file of sourceFiles(SRC)) {
    // Forward slashes: on a Windows checkout the separator is `\` and the
    // generated-catalog modules below would fail the skip check.
    const rel = file.slice(SRC.length).split('\\').join('/')
    if (/(^|\/)locales\//.test(rel)) continue
    const text = readFileSync(file, 'utf8')
    for (const m of text.matchAll(/(^|[^A-Za-z0-9_.$])t\(\s*(['"])([^'"\\]+)\2/g)) {
      used.set(m[3], rel)
    }
  }
  assert.ok(used.size > 200, `only ${used.size} t() call sites found — did the scanner drift?`)
  const missing = [...used].filter(([key]) => !(key in reference))
  assert.deepEqual(
    missing.slice(0, 10).map(([key, file]) => `${key} (${file})`),
    [],
    `${missing.length} keys are used but not in the catalog`,
  )
})

check('no source file still branches on isRtl to pick a string', () => {
  // `isRtl ? 'فارسی' : 'English'` was the old shape, in 458 places. Layout may
  // still read `isRtl` — `dir`, `textAlign`, a border side — so this only
  // objects when a branch is *copy*: it holds RTL script, or it is a string the
  // catalog already carries in any language. `isRtl ? 'none' : '3px solid …'`
  // is a border, not a sentence.
  const catalogValues = new Set()
  for (const lang of LANGS) for (const k of keys) catalogValues.add(CATALOGS[lang][k])
  // A layout keyword can coincide with a catalog entry (`left`/`right` are both
  // `textAlign` values and, in some language, a word); these are never copy.
  const LAYOUT = new Set(['left', 'right', 'auto', 'none', 'ltr', 'rtl', 'center', 'start', 'end', 'inherit'])
  const stripComments = (text) =>
    text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
  const offenders = []
  for (const file of sourceFiles(SRC)) {
    if (/(^|\/)locales\//.test(file.slice(SRC.length).split('\\').join('/'))) continue
    const text = stripComments(readFileSync(file, 'utf8'))
    const rx = /isRtl\s*\?\s*(['"`])([^'"`]*)\1\s*:\s*(['"`])([^'"`]*)\3/g
    for (const m of text.matchAll(rx)) {
      const branches = [m[2], m[4]]
      if (branches.every((b) => LAYOUT.has(b.trim()))) continue
      const isCopy = branches.some((b) => RTL.test(b) || catalogValues.has(b))
      if (!isCopy) continue
      offenders.push(
        `${file.slice(SRC.length)}:${text.slice(0, m.index).split('\n').length} ${branches.join(' / ')}`,
      )
    }
  }
  assert.deepEqual(offenders.slice(0, 10), [], `${offenders.length} string branches on isRtl`)
})

if (failures) {
  console.error(`\n${failures} catalog check(s) failed`)
  process.exitCode = 1
} else {
  console.log(`\n${keys.length} keys × ${LANGS.length} locales: all checks passed`)
}
