/**
 * The picker's ordering rule, asserted without a browser.
 *
 * `pinned` is a claim about the user's own Telegram chat list, and every
 * surface that lists chats for a source/target picker must present it the same
 * way the bot does. This test runs the rule directly from the compiled module,
 * the same way the rest of the dashboard's source-only logic is tested.
 *
 * Run (what CI does):
 *   node node_modules/typescript/bin/tsc src/chatOrder.ts --ignoreConfig \
 *     --outDir .chatorder-build --module esnext --target es2022 \
 *     --moduleResolution bundler --skipLibCheck
 *   node tests/chatOrder.test.mjs
 */
import assert from 'node:assert/strict'

import { pinnedFirstOrder } from '../.chatorder-build/chatOrder.js'

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

console.log('chat order')

check('a blank page returns untouched (no pinned anywhere)', () => {
  const input = [{ chat_id: '1' }, { chat_id: '2', pinned: false }]
  assert.equal(pinnedFirstOrder(input), input)
})

check('pinned rows lead; everything else keeps the server order', () => {
  const ordered = pinnedFirstOrder([
    { chat_id: 'a' },
    { chat_id: 'b', pinned: false },
    { chat_id: 'c', pinned: true },
    { chat_id: 'd' },
  ])
  assert.deepEqual(ordered.map((c) => c.chat_id), ['c', 'a', 'b', 'd'])
})

check('several pinned rows keep their own arrival order', () => {
  const ordered = pinnedFirstOrder([
    { chat_id: 'x' },
    { chat_id: 'p2', pinned: true },
    { chat_id: 'x2' },
    { chat_id: 'p1', pinned: true },
  ])
  assert.deepEqual(ordered.map((c) => c.chat_id), ['p2', 'p1', 'x', 'x2'])
})

check('absent, false and true all parse as their own pin state', () => {
  const ordered = pinnedFirstOrder([
    { chat_id: 'no-flag' },
    { chat_id: 'explicit-false', pinned: false },
    { chat_id: 'explicit-true', pinned: true },
  ])
  assert.deepEqual(ordered.map((c) => c.chat_id), ['explicit-true', 'no-flag', 'explicit-false'])
})

check('an empty page stays empty', () => {
  assert.deepEqual(pinnedFirstOrder([]), [])
})

if (failures > 0) process.exit(1)
console.log(`chat order: ${failures === 0 ? 'all checks passed' : `${failures} failed`}`)
