// The key-check panel's decisions, asserted without a browser.
//
// Run by the CI "Action result rules" step alongside actionResult.test.mjs:
//   node node_modules/typescript/bin/tsc src/providerKey.ts --ignoreConfig \
//     --outDir .actions-build --module esnext --target es2022 \
//     --moduleResolution bundler --skipLibCheck
//   node tests/providerKey.test.mjs

import assert from 'node:assert/strict'
import { describeKeyCheck, KEY_CHECK_REASONS } from '../.actions-build/providerKey.js'

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
  const view = describeKeyCheck({ success: true, healthy: true, latency_ms: 214.6, detail: '', error_code: '' }, false)
  assert.equal(view.ok, true)
  assert.match(view.text, /215 ms/)
  assert.equal(view.detail, '')
})

check('a rejected key is named, not shown as a spinner', () => {
  const view = describeKeyCheck({
    success: false, healthy: false, latency_ms: 90,
    detail: 'Incorrect API key provided: [REDACTED]', error_code: 'invalid_key',
  }, false)
  assert.equal(view.ok, false)
  assert.equal(view.text, KEY_CHECK_REASONS.invalid_key.en)
  assert.match(view.detail, /Incorrect API key/)
})

check('a blocked endpoint says the server cannot reach it', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, detail: '', error_code: 'endpoint_blocked' }, false)
  assert.equal(view.ok, false)
  assert.match(view.text, /cannot be reached from the server/)
})

check('a blocked endpoint is not confused with a bad key', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, detail: '', error_code: 'endpoint_blocked' }, true)
  assert.notEqual(view.text, KEY_CHECK_REASONS.invalid_key.fa)
  assert.equal(view.text, KEY_CHECK_REASONS.endpoint_blocked.fa)
})

check('every reason the core can send has both languages', () => {
  const codes = [
    'invalid_key', 'missing_key', 'missing_endpoint', 'endpoint_invalid',
    'endpoint_blocked', 'endpoint_unreachable', 'timeout', 'rate_limited',
    'provider_error', 'probe_rejected', 'unknown_provider', 'config_not_found',
  ]
  for (const code of codes) {
    const entry = KEY_CHECK_REASONS[code]
    assert.ok(entry, `missing a label for ${code}`)
    assert.ok(entry.en && entry.fa, `incomplete label for ${code}`)
  }
})

check('persian output is persian, and never a fallback to english', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'invalid_key' }, true)
  assert.equal(view.text, KEY_CHECK_REASONS.invalid_key.fa)
  assert.ok(!/[A-Za-z]{4,}/.test(view.text), `english leaked into: ${view.text}`)
})

check('no answer yet renders nothing at all', () => {
  const view = describeKeyCheck(null, false)
  assert.deepEqual(view, { ok: false, text: '', detail: '' })
})

check('an unidentified code still shows the code rather than nothing', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 0, error_code: 'something_new' }, false)
  assert.equal(view.text, 'something_new')
})

check('the server-side detail is passed through, trimmed', () => {
  const view = describeKeyCheck({ success: false, healthy: false, latency_ms: 5, detail: '  HTTP 500  ', error_code: 'provider_error' }, false)
  assert.equal(view.detail, 'HTTP 500')
})

if (failures) {
  console.error(`${failures} providerKey check(s) failed`)
  process.exit(1)
}
console.log('providerKey: all checks passed')
