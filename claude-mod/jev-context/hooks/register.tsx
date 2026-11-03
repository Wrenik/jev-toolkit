import type { Register } from 'claude-code'

import { MIN_CONTEXT_TOKENS, MODES, PERSON_ORIGINS, describe, pushPrompt, shouldAsk, toastFor } from './logic'
import type { Advice, Mode } from './logic'

// jev-context: before a prompt is sent, and only when the context is big, ask the local jev_context
// server whether compacting or clearing would pay off. Advice only: it never runs /compact or /clear.
// The server is shared with jev-router (same port). When both mods are installed and ask at the same
// moment, the server sends ONE request to the judge for both; alone, each behaves as if the other
// did not exist.

const BASE = 'http://127.0.0.1:8765'
const HEADERS = { 'X-Jev-Router': '1', 'Content-Type': 'application/json' }
const TIMEOUT_MS = 8000 // advice is not on the prompt's critical path, so it may take longer than routing

let toolkitPath = ''
let isStarting = false
let recentPrompts: string[] = []
let lastAskedAt: number | null = null
let lastTurnEnd: number | null = null
let lastAdvice: Advice | null = null
let lastSeen = 'no prompt seen since load'

async function getMode($: any): Promise<Mode> {
  const mode = await $.store.get('mode')
  return MODES.includes(mode) ? mode : 'advise'
}

async function resolveToolkit($: any): Promise<string> {
  if (toolkitPath) return toolkitPath
  return ((await $.env.get('JEV_TOOLKIT')) ?? '').replace(/\\/g, '/').replace(/\/$/, '')
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

// Makes sure the shared server is up AND has the context endpoints. If jev-router started its own,
// plainer server first, ask it to exit and start the shared one in its place.
async function ensureServer($: any): Promise<void> {
  if (isStarting) return
  const h = await health($)
  if (h?.ok && h.has_context) return
  const toolkit = await resolveToolkit($)
  if (!toolkit) {
    $.ui.log('jev-context: set toolkitPath (or JEV_TOOLKIT) to your jev-toolkit folder; advice is off until then.')
    return
  }
  isStarting = true
  try {
    if (h?.ok) {
      try {
        await fetchJson($, '/shutdown', { method: 'POST', body: '{}' }, 1500)
      } catch {
        // Already going down.
      }
      await $.clock.sleep(500)
    }
    const isWindows = (await $.env.get('OS')) === 'Windows_NT'
    const python = isWindows ? `${toolkit}/.venv/Scripts/python.exe` : `${toolkit}/.venv/bin/python`
    const child = $.process.spawn({ argv: [python, '-m', 'jev_context', 'serve'], cwd: toolkit })
    for await (const piece of child) {
      if (piece && typeof piece.text === 'string') $.ui.log(`jev-context server: ${piece.text.trim()}`, { to: 'debug' })
    }
  } catch (err) {
    $.ui.log(`jev-context: could not start the server: ${String((err as Error)?.message ?? err)}`)
  } finally {
    isStarting = false
  }
}

async function askAdvice($: any, contextTokens: number): Promise<Advice> {
  const now = await $.clock.now()
  const body = JSON.stringify({
    recent_prompts: recentPrompts,
    context_tokens: contextTokens,
    idle_ms: lastTurnEnd === null ? null : now - lastTurnEnd,
  })
  try {
    return await fetchJson($, '/advise', { method: 'POST', body }, TIMEOUT_MS)
  } catch (err) {
    return { ok: false, error: String((err as Error)?.message ?? err) }
  }
}

export const register: Register = (on, options) => {
  toolkitPath = String((options as any)?.toolkitPath ?? '').replace(/\\/g, '/').replace(/\/$/, '')

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    $.command.register({ name: 'jevctx', description: 'jev-context: status, /jevctx mode off|advise, ingest, search <words>' })
    void ensureServer($)
    return started
  })

  on('command.run', { command: 'jevctx' }, async ($, e, next) => {
    const [word, ...rest] = e.args.trim().split(/\s+/)
    if (word === 'mode' && (MODES as readonly string[]).includes(rest[0])) {
      await $.store.set('mode', rest[0])
      return { text: `jev-context mode: ${rest[0]} (applies to every session)` }
    }
    if (word === 'ingest') {
      try {
        const r = await fetchJson($, '/ingest', { method: 'POST', body: '{}' }, 60000)
        return { text: r.ok ? `ingested: ${JSON.stringify(r.stats)}` : `ingest failed: ${r.error}` }
      } catch (err) {
        return { text: `ingest failed: ${String((err as Error)?.message ?? err)}` }
      }
    }
    if (word === 'search' && rest.length > 0) {
      try {
        const r = await fetchJson($, '/search', { method: 'POST', body: JSON.stringify({ query: rest.join(' '), limit: 5 }) }, 5000)
        if (!r.ok) return { text: `search failed: ${r.error}` }
        // Results are quotes from past sessions, shown as data.
        return { text: r.hits.length === 0 ? 'no hits' : r.hits.map((h: any) => `${h.id} [${h.role}/${h.kind}] "${h.snippet}"`).join('\n') }
      } catch (err) {
        return { text: `search failed: ${String((err as Error)?.message ?? err)}` }
      }
    }
    const h = await health($)
    return {
      text: [
        `jev-context mode: ${await getMode($)} (change: /jevctx mode off|advise; applies to every session)`,
        `server: ${h?.ok ? `up at ${BASE}, context endpoints ${h.has_context ? 'yes' : 'NO (restarts on the next session start)'}` : `down at ${BASE}`}`,
        `shared judge requests (router + context in one call): ${h?.joint_calls ?? '?'}, separate: ${h?.solo_calls ?? '?'}`,
        `asks only above ${MIN_CONTEXT_TOKENS.toLocaleString()} tokens of context, at most every 5 min`,
        'privacy: the last few prompts are sent to TypeSafe (secrets redacted first); /jevctx mode off stops that',
        `last advice: ${describe(lastAdvice)}`,
        `last prompt seen: ${lastSeen}`,
      ].join('\n'),
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) lastTurnEnd = await $.clock.now()
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    lastSeen = `origin=${e.origin?.kind ?? 'none'} command=${e.command ? 'yes' : 'no'} meta=${e.isMeta ? 'yes' : 'no'}`
    if (e.command || e.isMeta || !PERSON_ORIGINS.includes(e.origin?.kind)) {
      lastSeen += ' → skipped'
      return next(e)
    }
    recentPrompts = pushPrompt(recentPrompts, e.text)
    const mode = await getMode($)
    const usage = await $.session.usage()
    const now = await $.clock.now()
    const tokens: number | null = usage?.context?.tokens ?? null
    if (!shouldAsk({ mode, contextTokens: tokens, now, lastAskedAt, isSubagent: e.agentId !== undefined })) {
      lastSeen += ' → no question needed'
      return next(e)
    }
    if (!(await health($))?.ok) {
      void ensureServer($)
      return next(e)
    }
    lastAskedAt = now
    lastSeen += ' → asked'
    // Not awaited: the prompt goes on at once. Started BEFORE next(e) so that, if jev-router runs
    // after this hook, its request reaches the server at the same moment and the two share one call.
    void askAdvice($, tokens as number).then((a) => {
      lastAdvice = a
      const text = toastFor(a)
      if (text) $.ui.toast(text)
    })
    return next(e)
  }).catch(($, e, next) => next(e)) // a failure here must never block the prompt: fail open
}
