/**
 * What the dashboard may conclude from a control-plane action response.
 *
 * The regression these assert: a refusal used to arrive with no `success` field
 * at all, and the dashboard's `success === false` test therefore never fired —
 * a refused replay removed the row it had failed to re-queue.
 *
 * Run: node tests/actionResult.test.mjs (after compiling src/actionResult.ts)
 */
import assert from 'node:assert/strict'

import {
  ACTION_REFUSALS,
  isAccepted,
  purgedCount,
  refusalReason,
} from '../.actions-build/actionResult.js'

let failures = 0
function test(name, fn) {
  try {
    fn()
    console.log(`  ok   ${name}`)
  } catch (err) {
    failures += 1
    console.log(`  FAIL ${name}\n       ${err.message}`)
  }
}

console.log('action result')

test('an explicit true is the only success', () => {
  assert.equal(isAccepted({ success: true, message: 'requeued' }), true)
})

test('an explicit false is a refusal', () => {
  for (const code of ACTION_REFUSALS) {
    assert.equal(
      isAccepted({ success: false, message: code, error_code: code }),
      false,
      `${code} was read as an accepted action`,
    )
  }
})

test('a body that omits success is a refusal (the live defect)', () => {
  // Byte-for-byte what the API used to answer for a refused replay.
  const live = { message: 'not_found', error_code: 'not_found' }
  assert.equal(isAccepted(live), false)
  assert.equal(refusalReason(live), 'not_found')
})

test('a refusal is named, not just negated', () => {
  assert.equal(refusalReason({ success: false, error_code: 'already_sent' }), 'already_sent')
  assert.equal(refusalReason({ success: false, message: 'purge_failed' }), 'purge_failed')
  // Neither field: still a reason, so the UI never shows an empty parenthesis.
  assert.equal(refusalReason({ success: false }, 'refused'), 'refused')
  assert.equal(refusalReason(null, 'refused'), 'refused')
})

test('a non-object body is not a success', () => {
  for (const body of [null, undefined, '', 'ok', 0, [], ['success']]) {
    assert.equal(isAccepted(body), false, `${JSON.stringify(body)} was read as success`)
  }
})

test('the purge count comes from the sentence the core sent', () => {
  assert.equal(purgedCount({ success: true, message: 'purged 3' }), 3)
  assert.equal(purgedCount({ success: true, message: 'purged 0' }), 0)
  assert.equal(purgedCount({ success: true, message: 'purged' }), null)
  assert.equal(purgedCount({ success: false, message: 'purge_failed' }), null)
  assert.equal(purgedCount(null), null)
})

test('a whitespace-only message is not a reason', () => {
  assert.equal(refusalReason({ success: false, message: '   ' }, 'refused'), 'refused')
})

if (failures > 0) {
  console.log(`\n${failures} failing`)
  process.exit(1)
}
console.log('\nall green')
