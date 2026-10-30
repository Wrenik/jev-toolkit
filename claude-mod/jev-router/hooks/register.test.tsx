import { expect, test } from 'claude-code/testing'

// Everything the mod reaches through `$` is answered here, standing in for the
// engine and for the jev_router server.
function engine(on: any, decision: object, { model = 'claude-sonnet-5-5', mode = 'auto' } = {}) {
  const posted: string[] = []
  on('http.fetch', (_: any, e: any) => {
    if (String(e.url).endsWith('/health')) {
      return { value: { status: 200, ok: true, headers: {}, text: '{"ok": true, "code_version": "test", "stale": false}' } }
    }
    posted.push(typeof e.body === 'string' ? e.body : typeof e.init?.body === 'string' ? e.init.body : 'KEYS:' + Object.keys(e).join(','))
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(decision) } }
  })
  on('store.get', () => ({ value: mode }))
  on('session.model', () => ({ value: model }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: 3000, window: 200000 }, rateLimits: [] } }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('command.list', () => ({ value: [{ name: 'effort' }] }))
  on('command.register', () => ({ value: { command: 'jev' } }))
  on('settings.read', () => ({ value: { effortLevel: 'medium' } }))
  on('env.get', () => ({ value: undefined }))
  // The mod races each fetch against a sleep (its timeout); here the sleep
  // simply never wins, so the fetch answer always arrives first.
  on('clock.sleep', () => new Promise(() => {}))
  on('clock.now', () => ({ value: 1_000_000 }))
  // Run the mod's deferred switch right away so a test sees the whole sequence.
  const ran: string[] = []
  on('clock.after', (_: any, e: any) => {
    const fn = e.fn ?? e.callback ?? e.handler
    if (typeof fn === 'function') fn()
    return { value: undefined }
  })
  on('command.run', (_: any, e: any) => {
    ran.push(`/${e.command} ${e.args}`)
    return { text: 'ok' }
  })
  on('prompt.fill', () => ({ isFilled: true }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text }))
  return Object.assign(posted, { ran })
}

const HEAVY = { ok: true, tier: 'heavy', model: 'opus', effort: 'high', reason: 'test', total_ms: 1, manages_current: true }

test('auto: a prompt needing another model is dropped for a switch', async ($, on) => {
  const posted = engine(on, HEAVY)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.prompt.submit({ text: 'refactor the auth module', origin: { kind: 'composer' } } as any)
  expect(posted.length).toBe(1)
  expect(String(r.drop ?? '')).toContain('switching to opus/high')
  await new Promise(resolve => setTimeout(resolve, 50))
  expect((posted as any).ran).toEqual(['/model opus', '/effort high'])
})

test('M3: the same prompt with an @mention is not dropped', async ($, on) => {
  engine(on, HEAVY)
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.prompt.submit({ text: 'refactor @src/auth.py', origin: { kind: 'composer' } } as any)
  expect(r.drop).toBeUndefined()
  expect(r.text).toBe('refactor @src/auth.py')
})

test('H1: on a model the router does not manage, nothing is dropped', async ($, on) => {
  engine(on, { ...HEAVY, manages_current: false }, { model: 'claude-fable-5-1' })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.prompt.submit({ text: 'refactor the auth module', origin: { kind: 'composer' } } as any)
  expect(r.drop).toBeUndefined()
})

test('the override prefix is stripped and sent as a forced tier', async ($, on) => {
  const posted = engine(on, { ...HEAVY, model: 'sonnet', effort: 'medium', tier: 'standard' })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.prompt.submit({ text: '!standard hello', origin: { kind: 'composer' } } as any)
  expect(JSON.parse(posted[0]).forced_tier).toBe('standard')
  expect(JSON.parse(posted[0]).prompt).toBe('hello')
  expect(r.text).toBe('hello')
})

test('a server failure lets the prompt through (fail open)', async ($, on) => {
  engine(on, { ok: false, error: 'JudgeUnavailable: down' })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  const r: any = await $.prompt.submit({ text: 'anything', origin: { kind: 'composer' } } as any)
  expect(r.drop).toBeUndefined()
  expect(r.text).toBe('anything')
})


test('an effort the mod set is remembered: the next prompt does not set it again', async ($, on) => {
  const posted = engine(on, HEAVY, { model: 'claude-opus-5-5' })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  await $.prompt.submit({ text: 'refactor the auth module', origin: { kind: 'composer' } } as any)
  await new Promise(resolve => setTimeout(resolve, 50))
  expect((posted as any).ran).toEqual(['/effort high'])
  const r: any = await $.prompt.submit({ text: 'and the tests too', origin: { kind: 'composer' } } as any)
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(r.drop).toBeUndefined()
  expect((posted as any).ran).toEqual(['/effort high'])
})
