import { expect, test } from 'claude-code/testing'

import { hasMention, parseOverride, plan } from './logic'
import type { Situation } from './logic'

const base: Situation = {
  mode: 'auto',
  decision: { ok: true, tier: 'heavy', model: 'opus', effort: 'high', reason: 'test', manages_current: true },
  currentModel: 'claude-sonnet-5-5',
  knownEffort: 'medium',
  hasEffortCommand: true,
  isHeadless: false,
  isTurnRunning: false,
  hasAttachments: false,
  hasMention: false,
}

test('auto mode switches model and effort when both differ', async () => {
  expect(plan(base)).toEqual({ kind: 'switch', setModel: true, setEffort: true, why: 'model' })
})

test('M1: effort alone is switched when the model already matches', async () => {
  const p = plan({ ...base, currentModel: 'claude-opus-5-5', knownEffort: 'medium' })
  expect(p).toEqual({ kind: 'switch', setModel: false, setEffort: true, why: 'effort only' })
})

test('nothing to do when model and effort match', async () => {
  expect(plan({ ...base, currentModel: 'claude-opus-5-5', knownEffort: 'high' }).kind).toBe('pass')
})

test('unknown effort counts as different, so it gets set once', async () => {
  const p = plan({ ...base, currentModel: 'claude-opus-5-5', knownEffort: null })
  expect(p.kind).toBe('switch')
})

test('no /effort command: only the model matters', async () => {
  expect(plan({ ...base, currentModel: 'claude-opus-5-5', hasEffortCommand: false }).kind).toBe('pass')
})

test('every unsafe case advises instead of dropping the prompt', async () => {
  const cases: Array<[Partial<Situation>, string]> = [
    [{ mode: 'advise' }, 'advise mode'],
    [{ decision: { ...base.decision, manages_current: false } }, 'model not managed by the router'],
    [{ isHeadless: true }, 'headless: /model cannot run'],
    [{ isTurnRunning: true }, 'sent while Claude works: resending would delay it'], // M2
    [{ hasAttachments: true }, 'has images, which cannot be resent'],
    [{ hasMention: true }, 'has @mentions, which are not expanded on resend'], // M3
  ]
  for (const [change, why] of cases) {
    expect(plan({ ...base, ...change })).toEqual({ kind: 'advise', why })
  }
})

test('a failed decision passes the prompt through untouched', async () => {
  expect(plan({ ...base, decision: { ok: false, error: 'timeout' } }).kind).toBe('pass')
})

test('L6: override with and without text', async () => {
  expect(parseOverride('!heavy fix the bug')).toEqual({ tier: 'heavy', text: 'fix the bug' })
  expect(parseOverride('!HEAVY')).toEqual({ tier: 'heavy', text: '' })
  expect(parseOverride('!heavyweight boxing')).toEqual({ tier: null, text: '!heavyweight boxing' })
  expect(parseOverride('plain prompt')).toEqual({ tier: null, text: 'plain prompt' })
})

test('M3: mention detection', async () => {
  expect(hasMention('look at @src/app.py please')).toBe(true)
  expect(hasMention('@README.md')).toBe(true)
  expect(hasMention('mail me at henric@example.com')).toBe(false)
  expect(hasMention('no mentions here')).toBe(false)
})
