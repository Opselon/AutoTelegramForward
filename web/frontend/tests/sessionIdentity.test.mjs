/**
 * The identity rules behind the dashboard's session state.
 *
 * Run by the CI "Session identity rules" step:
 *   node node_modules/typescript/bin/tsc src/sessionIdentity.ts \
 *     --ignoreConfig --outDir .identity-build \
 *     --module esnext --target es2022 --moduleResolution bundler --skipLibCheck
 *   node tests/sessionIdentity.test.mjs
 *
 * The regressions these pin: a second tab's login/logout left this tab showing
 * the previous account's dashboard, and adopting a different account reused the
 * mounted React tree instead of remounting it.
 */
import assert from 'node:assert/strict'

import {
  SESSION_TOKEN_KEY,
  appKey,
  describesSameAccount,
  isSessionStorageEvent,
  normaliseAccountId,
} from '../.identity-build/sessionIdentity.js'

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

console.log('session identity')

test('the token key is the one session.ts writes', () => {
  assert.equal(SESSION_TOKEN_KEY, 'auth_token')
})

test('a number id and its string form name the same account', () => {
  assert.equal(normaliseAccountId(5989354154), '5989354154')
  assert.equal(normaliseAccountId('5989354154'), '5989354154')
  assert.equal(describesSameAccount(5989354154, '5989354154'), true)
})

test('two different accounts never compare equal', () => {
  assert.equal(describesSameAccount(5989354154, 6278059256), false)
  assert.equal(describesSameAccount('5989354154', '5989354154 '), true)
})

test('a missing or nonsense id names nobody, and is not an account', () => {
  for (const bad of [undefined, null, 0, -5, '', '   ', 'abc', '12abc', '1e9', {}, []]) {
    assert.equal(normaliseAccountId(bad), '', `accepted ${JSON.stringify(bad)}`)
  }
  assert.equal(describesSameAccount(null, undefined), true)
})

test('the React key is stable per account and distinct across accounts', () => {
  assert.equal(appKey({ user_id: 5989354154 }), 'account:5989354154')
  assert.equal(appKey({ user_id: '5989354154' }), appKey({ user_id: 5989354154 }))
  assert.notEqual(appKey({ user_id: 5989354154 }), appKey({ user_id: 6278059256 }))
  assert.equal(appKey(null), 'account:anonymous')
  assert.notEqual(appKey(null), appKey({ user_id: 5989354154 }))
})

test('a storage event for our token is a session change', () => {
  assert.equal(isSessionStorageEvent({ key: 'auth_token' }), true)
})

test('localStorage.clear() (key null) is also a session change', () => {
  assert.equal(isSessionStorageEvent({ key: null }), true)
})

test('unrelated keys and malformed events are not', () => {
  assert.equal(isSessionStorageEvent({ key: 'atf_lang' }), false)
  assert.equal(isSessionStorageEvent({ key: 'atf_dismissed_errors' }), false)
  assert.equal(isSessionStorageEvent({}), false)
  assert.equal(isSessionStorageEvent(null), false)
  assert.equal(isSessionStorageEvent(undefined), false)
  assert.equal(isSessionStorageEvent('auth_token'), false)
})

if (failures > 0) {
  console.log(`\n${failures} assertion group(s) failed`)
  process.exit(1)
}
console.log('\nall session identity rules hold')
