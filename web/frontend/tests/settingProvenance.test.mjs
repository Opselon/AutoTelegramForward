/**
 * The rules that decide what a settings row shows, and whether it can be handed
 * back to the bot's default.
 *
 * The component renders; these functions decide. They are plain TypeScript with
 * type-only imports, so the CI job compiles this one file and runs this test on
 * it — no bundler, no DOM, no test framework.
 *
 * Run: node tests/settingProvenance.test.mjs (after `tsc src/settingProvenance.ts ...`)
 */
import assert from 'node:assert/strict'

import {
  badgeFor,
  canReset,
  isInherited,
  rowValue,
} from '../.provenance-build/settingProvenance.js'

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

const SETTLE = (value, source, extra = {}) => ({
  value,
  source,
  default: false,
  inheritable: true,
  ...extra,
})

console.log('setting provenance')

test('an unchanged task shows the effective value the forwarder will use', () => {
  const settings = { remove_emojis: false }
  const sources = { remove_emojis: SETTLE(true, 'bot', { bot_default: true, bot_label: '@fwd_bot' }) }
  assert.equal(rowValue('remove_emojis', {}, settings, sources), true)
})

test('an unsaved edit outranks the settled value', () => {
  const settings = { remove_emojis: false }
  const sources = { remove_emojis: SETTLE(true, 'bot') }
  assert.equal(rowValue('remove_emojis', { remove_emojis: false }, settings, sources), false)
})

test('a response without provenance falls back to the stored value', () => {
  const settings = { remove_emojis: false }
  assert.equal(rowValue('remove_emojis', {}, settings, {}), false)
  assert.equal(rowValue('missing', {}, settings, {}), undefined)
})

test('a settled null wins over the stored value', () => {
  // Presence, not truthiness: a setting may legitimately be null.
  const settings = { signature: 'kept' }
  const sources = { signature: SETTLE(null, 'task') }
  assert.equal(rowValue('signature', {}, settings, sources), null)
})

test('a task that chose the registry default is still the task', () => {
  // The value equals the default, so it looks unchanged in `settings`, but the
  // server records the choice and the row must say so — otherwise the user
  // cannot tell an override from an inherited value.
  const settings = { remove_emojis: false }
  const sources = { remove_emojis: SETTLE(false, 'task', { default: false, pinned: true }) }
  assert.equal(rowValue('remove_emojis', {}, settings, sources), false)
  assert.equal(badgeFor(sources.remove_emojis).text, 'TASK')
  assert.equal(canReset('remove_emojis', {}, sources), true)
})

test('only a task-owned or edited row can be handed back', () => {
  const sources = {
    inherited: SETTLE(true, 'bot'),
    own: SETTLE(true, 'task'),
    plain: SETTLE(true, 'default'),
  }
  assert.equal(canReset('inherited', {}, sources), false)
  assert.equal(canReset('plain', {}, sources), false)
  assert.equal(canReset('own', {}, sources), true)
  // An unsaved edit is undoable by dropping the edit, so it offers the reset.
  assert.equal(canReset('inherited', { inherited: false }, sources), true)
})

test('the bot badge names the bot that supplies the value', () => {
  const badge = badgeFor(SETTLE(true, 'bot', { bot_label: '@fwd_bot' }))
  assert.equal(badge.text, 'BOT @fwd_bot')
  assert.match(badge.title, /Inherited/)
})

test('a nameless bot still gets a bot badge, not a default one', () => {
  assert.equal(badgeFor(SETTLE(true, 'bot')).text, 'BOT')
  assert.equal(badgeFor(SETTLE(true, 'bot')).color, '#c4b5fd')
})

test('an unknown source and an absent one are both shown as the default', () => {
  assert.equal(badgeFor(SETTLE(true, 'something_new')).text, 'DEFAULT')
  assert.equal(badgeFor(undefined), null)
})

test('inherited values are marked as inherited, edited and owned ones are not', () => {
  assert.equal(isInherited(SETTLE(true, 'bot')), true)
  assert.equal(isInherited(SETTLE(true, 'task')), false)
  assert.equal(isInherited(SETTLE(true, 'default')), false)
  assert.equal(isInherited(undefined), false)
})

if (failures) {
  console.log(`\n${failures} provenance check(s) failed`)
  process.exit(1)
}
console.log('\nall provenance checks passed')
