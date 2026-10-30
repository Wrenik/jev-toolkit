import type { Register } from 'claude-code'

import { MODES, PERSON_ORIGINS, hasMention, modelMatches, parseOverride, plan } from './logic'
import type { Decision, Mode } from './logic'

// jev-router: before each prompt, ask the local jev_router server (Python, in
// jev-toolkit) which tier the prompt needs, then advise or switch the model.
// The server owns the judge, the policy and the decision log; this mod only acts.

const BASE = 'http://127.0.0.1:8765'
const HEADERS = { 'X-Jev-Router': '1', 'Content-Type': 'application/json' }
// Fail open after this: the prompt goes through unrouted.
const TIMEOUT_MS = 2500

// Set from `register`'s options: where jev-toolkit lives (userConfig
// `toolkitPath`, else the JEV_TOOLKIT environment variable).
let toolkitPath = ''
let previousTier: string | null = null
let last: Decision | null = null
let isStarting = false
// When the main conversation last finished an answer; the cache lives ~1 h from then.
let lastTurnEnd: number | null = null
let isTurnRunning = false
let hasEffortCommand = false
// The effort this session runs, as far as the mod knows: what /effort last set,
// or the effortLevel setting; null until either is seen.
let knownEffort: string | null = null
// Why the last prompt was or wasn't routed; shown by /jev.
let lastSeen = 'no prompt seen since load'

async function getMode($: any): Promise<Mode> {
  const mode = await $.store.get('mode')
  return MODES.includes(mode) ? mode : 'advise'
}

async function resolveToolkit($: any): Promise<string> {
  if (toolkitPath) return toolkitPath
  return ((await $.env.get('JEV_TOOLKIT')) ?? '').replace(/\\/g, '/').replace(/\/$/, '')
}

async function pythonOf($: any, toolkit: string): Promise<string> {
  const isWindows = (await $.env.get('OS')) === 'Windows_NT'
  return isWindows ? `${toolkit}/.venv/Scripts/python.exe` : `${toolkit}/.venv/bin/python`
}

async function fetchJson($: any, path: string, init: any, ms: number): Promise<any> {
  const timeout = $.clock.sleep(ms).then(() => null)
  const response = await Promise.race([$.http.fetch(`${BASE}${path}`, { headers: HEADERS, ...init }), timeout])
  if (response === null) throw new Error(`timeout after ${ms} ms`)
  return JSON.parse(response.text)
}

async function health($: any): Promise<any> {
  try {
    return await fetchJson($, '/health', {}, 800)
  } catch {
    return null
  }
}

// Starts the server as a child of this session unless one is already up
// (another session may own it). Not awaited by prompts: they fail open meanwhile.
async function ensureServer($: any): Promise<void> {
  if (isStarting || (await health($))?.ok) return
  const toolkit = await resolveToolkit($)
  if (!toolkit) {
    $.ui.log('jev-router: set toolkitPath (or JEV_TOOLKIT) to your jev-toolkit folder; routing is off until then.')
    return
  }
  isStarting = true
  try {
    const child = $.process.spawn({ argv: [await pythonOf($, toolkit), '-m', 'jev_router', 'serve'], cwd: toolkit })
    for await (const piece of child) {
      if (piece && typeof piece.text === 'string') $.ui.log(`jev-router server: ${piece.text.trim()}`, { to: 'debug' })
    }
  } catch (err) {
    $.ui.log(`jev-router: could not start the router server: ${String((err as Error)?.message ?? err)}`)
  } finally {
    isStarting = false
  }
}

// Asks the running server to exit (it may be another session's, or stale code),
// then starts a fresh one.
async function restartServer($: any): Promise<string> {
  try {
    await fetchJson($, '/shutdown', { method: 'POST', body: '{}' }, 1500)
  } catch {
    // Nothing was running, or it was already going down.
  }
  await $.clock.sleep(500)
  void ensureServer($)
  for (let i = 0; i < 20; i += 1) {
    await $.clock.sleep(300)
    const h = await health($)
    if (h?.ok) return `server restarted (code ${h.code_version})`
  }
  return 'server did not come back within 6 s; it starts again on the next prompt'
}

async function askRouter($: any, prompt: string, forcedTier: string | null, mode: Mode): Promise<Decision> {
  // Context size and idle time let the server avoid costly step-downs: a model
  // switch throws away the prompt cache, so with a big warm context it only goes up.
  const usage = await $.session.usage()
  const now = await $.clock.now()
  const body = JSON.stringify({
    prompt,
    forced_tier: forcedTier,
    previous_tier: previousTier,
    current_model: await $.session.model(),
    context_tokens: usage?.context?.tokens ?? null,
    idle_ms: lastTurnEnd === null ? null : now - lastTurnEnd,
    mode,
  })
  try {
    return await fetchJson($, '/route', { method: 'POST', body }, TIMEOUT_MS)
  } catch (err) {
    return { ok: false, error: String((err as Error)?.message ?? err) }
  }
}

// Runs after the dropped prompt's hook has returned, once the session is idle.
async function switchAndResend($: any, d: Decision, text: string, setModel: boolean, setEffort: boolean): Promise<void> {
  try {
    if (setModel) await $.command.run({ command: 'model', args: d.model })
    if (setEffort) await $.command.run({ command: 'effort', args: d.effort })
    await $.prompt.submit({ text, asUser: true })
  } catch (err) {
    // Never lose the person's prompt: put it back in the box, or at least say so.
    const why = String((err as Error)?.message ?? err)
    try {
      await $.prompt.fill({ text, mode: 'replace' })
      $.ui.toast(`jev-router: switch failed (${why}); your prompt is back in the box.`)
    } catch {
      $.ui.toast(`jev-router: switch failed (${why}) and the prompt could not be put back: please send it again.`)
    }
  }
}

function describe(d: Decision | null): string {
  if (!d) return 'no decision yet'
  if (!d.ok) return `not routed: ${d.error}`
  return `${d.tier} → ${d.model}/${d.effort} (${d.reason}) · ${d.total_ms} ms${d.cached ? ' · cached' : ''}`
}

export const register: Register = (on, options) => {
  toolkitPath = String((options as any)?.toolkitPath ?? '').replace(/\\/g, '/').replace(/\/$/, '')

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    $.command.register({ name: 'jev', description: 'jev-router: status, /jev mode off|advise|auto, /jev restart' })
    hasEffortCommand = (await $.command.list()).some((c: any) => c.name === 'effort')
    const level = await $.settings.read({ key: 'effortLevel' })
    knownEffort = typeof level === 'string' ? level : null
    void ensureServer($)
    return started
  })

  // Remember what /model and /effort set, whoever ran them (the person or this mod).
  on('command.run', { command: 'effort' }, async ($, e, next) => {
    const result = await next(e)
    const level = e.args.trim().split(/\s+/)[0]
    if (level) knownEffort = level
    return result
  })

  on('command.run', { command: 'jev' }, async ($, e, next) => {
    const [word, value] = e.args.trim().split(/\s+/)
    if (word === 'mode' && (MODES as readonly string[]).includes(value)) {
      await $.store.set('mode', value)
      return { text: `jev-router mode: ${value} (applies to every session)` }
    }
    if (word === 'restart') return { text: `jev-router: ${await restartServer($)}` }
    const h = await health($)
    const server = h?.ok
      ? `up at ${BASE}, code ${h.code_version}${h.stale ? ' — STALE: the code changed since it started, run /jev restart' : ''}`
      : `down at ${BASE}`
    return {
      text: [
        `running model: ${await $.session.model()} (what the next prompt is sent to)`,
        `known effort: ${knownEffort ?? 'unknown'}`,
        `jev-router mode: ${await getMode($)} (change: /jev mode off|advise|auto; applies to every session)`,
        `server: ${server}`,
        `toolkit: ${(await resolveToolkit($)) || 'NOT SET (userConfig toolkitPath or JEV_TOOLKIT)'}`,
        `effort switching: ${hasEffortCommand ? 'yes (/effort)' : 'no /effort command here, model only'}`,
        'privacy: routed prompts are sent to TypeSafe (secrets are redacted first); /jev mode off stops that',
        `last: ${describe(last)}`,
        `last prompt seen: ${lastSeen}`,
      ].join('\n'),
    }
  })

  // Only the main conversation counts: a subagent's turns don't keep its cache warm.
  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) isTurnRunning = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      isTurnRunning = false
      lastTurnEnd = await $.clock.now()
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    lastSeen = `origin=${e.origin?.kind ?? 'none'} command=${e.command ? 'yes' : 'no'} meta=${e.isMeta ? 'yes' : 'no'}`
    if (e.command || e.isMeta || !PERSON_ORIGINS.includes(e.origin?.kind)) {
      lastSeen += ' → skipped'
      return next(e)
    }
    const mode = await getMode($)
    if (mode === 'off') return next(e)
    lastSeen += ' → routing'

    const forced = parseOverride(e.text)
    const text = forced.text

    if (!(await health($))?.ok) {
      void ensureServer($)
      $.ui.status('Jev: router server starting, this prompt was not routed')
      return next({ ...e, text })
    }

    const d = await askRouter($, text, forced.tier, mode)
    last = d
    const current = String(await $.session.model())
    const surfaces = await $.session.surfaces()
    const p = plan({
      mode,
      decision: d,
      currentModel: current,
      knownEffort,
      hasEffortCommand,
      isHeadless: surfaces.length === 0,
      isTurnRunning,
      hasAttachments: (e.attachments?.length ?? 0) > 0,
      hasMention: hasMention(text),
    })
    lastSeen += ` surfaces=[${surfaces.join(',')}] → ${p.kind} (${p.why})`
    $.ui.status(`Jev: ${describe(d)}${d.ok && d.model && !modelMatches(current, d.model) ? ` · now ${current}` : ''}`)

    // Remember a tier only once the session actually runs it.
    if (p.kind === 'pass' && d.ok) previousTier = d.tier ?? null
    if (p.kind === 'switch') previousTier = d.tier ?? null
    if (p.kind !== 'switch') {
      // A model the person chose that the router doesn't manage: status line only, no toast each prompt.
      if (p.kind === 'advise' && d.manages_current !== false) $.ui.toast(`Jev suggests ${d.model}/${d.effort} (${d.reason}). Not switched: ${p.why}.`)
      return next({ ...e, text })
    }

    // Auto: drop now, switch once idle, then send the same text again.
    $.clock.after(50, () => void switchAndResend($, d, text, p.setModel, p.setEffort))
    const what = p.setModel ? `${d.model}/${d.effort}` : `effort ${d.effort}`
    return { drop: `Jev: ${d.tier} → switching to ${what}, then resending your prompt.` }
  }).catch(($, e, next) => next(e)) // a failure here must never block the prompt: fail open

}
