// The provider-key reason table, asserted in every locale, node-only.
//
// Run by the CI "Action result rules" step alongside actionResult.test.mjs:
//   node node_modules/typescript/bin/tsc src/providerKey.ts --ignoreConfig \
//     --outDir .actions-build --module esnext --target es2022 \
//     --moduleResolution bundler --skipLibCheck
//   node tests/providerKey.test.mjs

import assert from 'node:assert/strict'
import {
  CHECK_FAILED,
  KEY_ACCEPTED,
  describeKeyCheck,
  describeSaveFailure,
  KEY_CHECK_REASONS,
} from '../.actions-build/providerKey.js'

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

console.log('provider key check')

check('a healthy answer reports the latency and no reason', () => {
  const view = describeKeyCheck({ success: true, healthy: true, latency_ms: 214.6, detail: '', error_code: '' }, 'en')
  assert.equal(view.ok, true)
  assert.match(view.text, /215 ms/)
  assert.equal(view.detail, '')
})

check('a rejected key is named, not shown as a spinner', () => {
  const view = describeKeyCheck({
    success: false, healthy: false, latency_ms: 90,
    detail: 'Incorrect API key provided: [REDACTED]', error_code: 'invalid_key',
  }, 'en')
  assert.equal(view.ok, false)
  assert.equal(view.text, KEY_CHECK_REASONS.invalid_key.en)
  assert.match(view.detail, /Incorrect API key/)
})

check('a blocked endpoint says the server cannot reach it', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, detail: '', error_code: 'endpoint_blocked' }, 'en')
  assert.equal(view.ok, false)
  assert.match(view.text, /cannot be reached from the server/)
})

check('a blocked endpoint is not confused with a bad key', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, detail: '', error_code: 'endpoint_blocked' }, 'fa')
  assert.notEqual(view.text, KEY_CHECK_REASONS.invalid_key.fa)
  assert.equal(view.text, KEY_CHECK_REASONS.endpoint_blocked.fa)
})

// Boolean `false` used to mean English and `true` Persian; every caller now
// passes a locale, so a boolean must never come back as a language.
check('a boolean argument is not a language', () => {
  for (const bad of [false, true, undefined, null]) {
    const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'invalid_key' }, bad)
    assert.equal(view.text, KEY_CHECK_REASONS.invalid_key.en, String(bad))
  }
  assert.equal(describeSaveFailure('endpoint_blocked', true), KEY_CHECK_REASONS.endpoint_blocked.en)
})

check('every reason the core can send has all ten languages', () => {
  const codes = [
    'invalid_key', 'missing_key', 'missing_endpoint', 'endpoint_invalid',
    'endpoint_blocked', 'endpoint_unreachable', 'timeout', 'rate_limited',
    'provider_error', 'probe_rejected', 'unknown_provider', 'config_not_found',
  ]
  // 'probe_failed' is not sent by the core today; it is the dashboard's own
  // catch-all and stays complete so the table cannot regress.
  for (const code of [...codes, 'probe_failed']) {
    const entry = KEY_CHECK_REASONS[code]
    assert.ok(entry, `missing a label for ${code}`)
    for (const lang of ['en', 'fa', 'pt', 'uz', 'ru', 'id', 'es', 'fr', 'it', 'zh']) {
      assert.ok(entry[lang]?.trim(), `${code} has no ${lang} text`)
    }
  }
  assert.equal(Object.keys(KEY_CHECK_REASONS).length, codes.length + 1)
})

check('persian output is persian, and never a fallback to english', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'invalid_key' }, 'fa')
  assert.equal(view.text, KEY_CHECK_REASONS.invalid_key.fa)
  assert.ok(!/[A-Za-z]{4,}/.test(view.text), `english leaked into: ${view.text}`)
})

check('every locale answers in its own language', () => {
  const view = (lang) => describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'endpoint_blocked' }, lang).text
  assert.equal(view('en'), KEY_CHECK_REASONS.endpoint_blocked.en)
  assert.equal(view('ru'), KEY_CHECK_REASONS.endpoint_blocked.ru)
  assert.equal(view('zh'), KEY_CHECK_REASONS.endpoint_blocked.zh)
  assert.equal(view('pt-BR'), KEY_CHECK_REASONS.endpoint_blocked.pt)
  assert.equal(view('zh-Hans'), KEY_CHECK_REASONS.endpoint_blocked.zh)
  // An unsupported locale normalises to English rather than Persian.
  assert.equal(view('de-DE'), KEY_CHECK_REASONS.endpoint_blocked.en)
  assert.equal(view(''), KEY_CHECK_REASONS.endpoint_blocked.en)
})

check('the healthy banner is localized without re-reading the reason table', () => {
  assert.match(describeKeyCheck({ success: true, healthy: true, latency_ms: 12, detail: '', error_code: '' }, 'ru').text, /Ключ принят/)
  assert.match(describeKeyCheck({ success: true, healthy: true, latency_ms: 12, detail: '', error_code: '' }, 'es').text, /Clave aceptada/)
})

check('no reason text is repeated across locales', () => {
  for (const [code, entry] of Object.entries(KEY_CHECK_REASONS)) {
    const values = Object.values(entry)
    assert.equal(new Set(values).size, values.length, `${code} repeats a translation`)
    for (const v of values) assert.ok(!v.includes('\uFFFD'), `${code} is mojibake`)
  }
  for (const table of [KEY_ACCEPTED, CHECK_FAILED]) {
    const values = Object.values(table)
    assert.equal(new Set(values).size, values.length, 'a shared template repeats')
  }
})

check('no answer yet renders nothing at all', () => {
  const view = describeKeyCheck(null, 'en')
  assert.deepEqual(view, { ok: false, text: '', detail: '' })
})

check('an unidentified code still shows the code rather than nothing', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'something_new' }, 'en')
  assert.equal(view.text, 'something_new')
})

check('the server-side detail is passed through, trimmed', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 5, detail: '  HTTP 500  ', error_code: 'provider_error' }, 'en')
  assert.equal(view.detail, 'HTTP 500')
})

check('a saved-but-refused endpoint is explained, not printed as a code', () => {
  const view = describeSaveFailure('endpoint_blocked', 'en')
  assert.equal(view, KEY_CHECK_REASONS.endpoint_blocked.en)
  assert.ok(!view.includes('endpoint_blocked'), `a raw code reached the user: ${view}`)
  assert.equal(describeSaveFailure('endpoint_blocked', 'fa'), KEY_CHECK_REASONS.endpoint_blocked.fa)
  assert.equal(describeSaveFailure('endpoint_blocked', 'fr'), KEY_CHECK_REASONS.endpoint_blocked.fr)
})

check('an unrecognized save failure is passed through unchanged', () => {
  assert.equal(describeSaveFailure('boom', 'ru'), 'boom')
  assert.equal(describeSaveFailure('', 'ru'), '')
})

if (failures) {
  console.error(`${failures} providerKey check(s) failed`)
  process.exit(1)
}
console.log('providerKey: all checks passed')
