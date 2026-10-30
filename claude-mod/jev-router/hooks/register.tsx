import type { Register } from 'claude-code'

// jev-router: before each prompt, ask the local jev_router server (Python, in
// jev-toolkit) which tier the prompt needs, then advise or switch the model.
// The server owns the judge, the policy and the decision log; this mod only acts.

const TOOLKIT = 'C:/Users/Henric/Documents/jev-toolkit'
const PYTHON = `${TOOLKIT}/.venv/Scripts/python.exe`
const BASE = 'http://127.0.0.1:8765'
const HEADERS = { 'X-Jev-Router': '1', 'Content-Type': 'application/json' }
// Fail open after this: the prompt goes through unrouted.
const TIMEOUT_MS = 2500
const MODES = ['off', 'advise', 'auto'] as const
type Mode = (typeof MODES)[number]
// "!heavy fix the login bug" forces a tier without asking the judge.
const OVERRIDE = /^!(light|standard|heavy)\s+/i
const PERSON_ORIGINS: readonly string[] = ['composer', 'sdk', 'bridge']

type Decision = {
  ok: boolean
  tier?: string
  model?: string
  effort?: string
  reason?: string
  error?: string
  total_ms?: number
  cached?: boolean
}

let previousTier: string | null = null
let last: Decision | null = null
let isStarting = false
let hasEffortCommand = false
// Why the last prompt was or wasn't routed; shown by /jev.
let lastSeen = 'no prompt seen since load'

async function getMode($: any): Promise<Mode> {
  const mode = await $.store.get('mode')
  return MODES.includes(mode) ? mode : 'advise'
}

async function fetchJson($: any, path: string, init: any, ms: number): Promise<any> {
  const timeout = $.clock.sleep(ms).then(() => null)
  const response = await Promise.race([$.http.fetch(`${BASE}${path}`, { headers: HEADERS, ...init }), timeout])
  if (response === null) throw new Error(`timeout after ${ms} ms`)
  return JSON.parse(response.text)
}

async function isUp($: any): Promise<boolean> {
  try {
    return (await fetchJson($, '/health', {}, 800)).ok === true
  } catch {
    return false
  }
}

// Starts the server as a child of this session unless one is already up
// (another session may own it). Not awaited by prompts: they fail open meanwhile.
async function ensureServer($: any): Promise<void> {
  if (isStarting || (await isUp($))) return
  isStarting = true
  try {
    const child = $.process.spawn({ argv: [PYTHON, '-m', 'jev_router', 'serve'], cwd: TOOLKIT })
    for await (const piece of child) {
      if (piece && typeof piece.text === 'string') $.ui.log(`jev-router server: ${piece.text.trim()}`, { to: 'debug' })
    }
  } catch (err) {
    $.ui.log(`jev-router: could not start the router server: ${String((err as Error)?.message ?? err)}`)
  } finally {
    isStarting = false
  }
}

async function askRouter($: any, prompt: string, forcedTier: string | null, mode: Mode): Promise<Decision> {
  const body = JSON.stringify({
    prompt,
    forced_tier: forcedTier,
    previous_tier: previousTier,
    current_model: await $.session.model(),
    mode,
  })
  try {
    return await fetchJson($, '/route', { method: 'POST', body }, TIMEOUT_MS)
  } catch (err) {
    return { ok: false, error: String((err as Error)?.message ?? err) }
  }
}

// Runs after the dropped prompt's hook has returned, once the session is idle.
async function switchAndResend($: any, d: Decision, text: string): Promise<void> {
  try {
    await $.command.run({ command: 'model', args: d.model })
    if (hasEffortCommand) await $.command.run({ command: 'effort', args: d.effort })
    await $.prompt.submit({ text, asUser: true })
  } catch (err) {
    // Never lose the person's prompt: put it back in the box.
    await $.prompt.fill({ text, mode: 'replace' })
    $.ui.toast(`jev-router: switch failed (${String((err as Error)?.message ?? err)}); your prompt is back in the box.`)
  }
}

function describe(d: Decision | null): string {
  if (!d) return 'no decision yet'
  if (!d.ok) return `not routed: ${d.error}`
  return `${d.tier} → ${d.model}/${d.effort} (${d.reason}) · ${d.total_ms} ms${d.cached ? ' · cached' : ''}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    $.command.register({ name: 'jev', description: 'jev-router: show status, or /jev mode off|advise|auto' })
    hasEffortCommand = (await $.command.list()).some((c: any) => c.name === 'effort')
    void ensureServer($)
    return started
  })

  on('command.run', { command: 'jev' }, async ($, e, next) => {
    const [word, value] = e.args.trim().split(/\s+/)
    if (word === 'mode' && (MODES as readonly string[]).includes(value)) {
      await $.store.set('mode', value)
      return { text: `jev-router mode: ${value}` }
    }
    const up = await isUp($)
    return {
      text: [
        `jev-router mode: ${await getMode($)} (change: /jev mode off|advise|auto)`,
        `server: ${up ? 'up' : 'down'} at ${BASE}`,
        `effort switching: ${hasEffortCommand ? 'yes (/effort)' : 'no /effort command here, model only'}`,
        `last: ${describe(last)}`,
        `last prompt seen: ${lastSeen}`,
      ].join('\n'),
    }
  })

  on('prompt.submit', async ($, e, next) => {
    // Only the person's own prompts: the terminal (composer), the desktop app (sdk)
    // or a phone (bridge). Our own resend arrives with a plugin origin and is skipped.
    lastSeen = `origin=${e.origin?.kind ?? 'none'} command=${e.command ? 'yes' : 'no'} meta=${e.isMeta ? 'yes' : 'no'}`
    if (e.command || e.isMeta || !PERSON_ORIGINS.includes(e.origin?.kind)) {
      lastSeen += ' → skipped'
      return next(e)
    }
    const mode = await getMode($)
    if (mode === 'off') return next(e)
    lastSeen += ' → routing'

    let text = e.text
    const forced = OVERRIDE.exec(text)
    if (forced) text = text.slice(forced[0].length)

    if (!(await isUp($))) {
      void ensureServer($)
      $.ui.status('Jev: router server starting, this prompt was not routed')
      return next({ ...e, text })
    }

    const d = await askRouter($, text, forced ? forced[1].toLowerCase() : null, mode)
    last = d
    if (!d.ok || !d.model) {
      $.ui.status(`Jev: ${describe(d)}`)
      return next({ ...e, text })
    }
    previousTier = d.tier ?? null

    const current = String(await $.session.model())
    const isRightModel = current.toLowerCase().includes(d.model.toLowerCase())
    $.ui.status(`Jev: ${describe(d)}${isRightModel ? '' : ` · now ${current}`}`)
    if (isRightModel) return next({ ...e, text })

    // Suggest only in advise mode, for prompts with images (can't be resent), and
    // headless (`claude -p`: no surface, and /model can't run there, so a dropped
    // prompt would be lost).
    const surfaces = await $.session.surfaces()
    const isHeadless = surfaces.length === 0
    lastSeen += ` surfaces=[${surfaces.join(',')}]`
    if (mode === 'advise' || (e.attachments?.length ?? 0) > 0 || isHeadless) {
      $.ui.toast(`Jev suggests ${d.model}/${d.effort} for this (${d.reason}). Running on ${current}.`)
      return next({ ...e, text })
    }

    // Auto: drop now, switch model once idle, then send the same text again.
    $.clock.after(50, () => void switchAndResend($, d, text))
    return { drop: `Jev: ${d.tier} → switching to ${d.model}/${d.effort}, then resending your prompt.` }
  })
}
