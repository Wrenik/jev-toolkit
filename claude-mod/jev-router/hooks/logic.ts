// Pure decision logic for jev-router: no `$`, so it can be tested directly.

export const TIERS = ['light', 'standard', 'heavy'] as const
export type Tier = (typeof TIERS)[number]
export const MODES = ['off', 'advise', 'auto'] as const
export type Mode = (typeof MODES)[number]

// Prompts from the person: the terminal (composer), the desktop app (sdk) or a
// phone (bridge). Our own resend has a plugin origin and is never routed again.
export const PERSON_ORIGINS: readonly string[] = ['composer', 'sdk', 'bridge']

export type Decision = {
  ok: boolean
  tier?: string
  model?: string
  effort?: string
  reason?: string
  error?: string
  total_ms?: number
  cached?: boolean
  // False when the session runs a model the router doesn't manage (Fable, opusplan, [1m]).
  manages_current?: boolean
}

// "!heavy fix the login bug" or a bare "!heavy" forces a tier without the judge.
const OVERRIDE = /^!(light|standard|heavy)(?:\s+|$)/i

export function parseOverride(text: string): { tier: Tier | null; text: string } {
  const match = OVERRIDE.exec(text)
  if (!match) return { tier: null, text }
  return { tier: match[1].toLowerCase() as Tier, text: text.slice(match[0].length) }
}

// `@path` mentions are only expanded for the person's own prompt, not for a
// plugin's resend, so a prompt holding one must not be dropped and resent.
export function hasMention(text: string): boolean {
  return /(^|\s)@[^\s@]+/.test(text)
}

export function modelMatches(current: string, alias: string): boolean {
  return current.toLowerCase().includes(alias.toLowerCase())
}

export type Situation = {
  mode: Mode
  decision: Decision
  currentModel: string
  // The effort this session is known to run, or null when unknown.
  knownEffort: string | null
  hasEffortCommand: boolean
  isHeadless: boolean
  isTurnRunning: boolean
  hasAttachments: boolean
  hasMention: boolean
}

export type Plan =
  | { kind: 'pass'; why: string }
  | { kind: 'advise'; why: string }
  | { kind: 'switch'; setModel: boolean; setEffort: boolean; why: string }

// What to do with a routed prompt. Only 'switch' drops and resends it.
export function plan(s: Situation): Plan {
  const d = s.decision
  if (!d.ok || !d.model) return { kind: 'pass', why: 'not routed' }
  const needModel = !modelMatches(s.currentModel, d.model)
  const needEffort = s.hasEffortCommand && !!d.effort && s.knownEffort !== d.effort
  if (!needModel && !needEffort) return { kind: 'pass', why: 'already right' }

  // Every reason the prompt must not be dropped and resent.
  if (s.mode !== 'auto') return { kind: 'advise', why: 'advise mode' }
  if (d.manages_current === false) return { kind: 'advise', why: 'model not managed by the router' }
  if (s.isHeadless) return { kind: 'advise', why: 'headless: /model cannot run' }
  if (s.isTurnRunning) return { kind: 'advise', why: 'sent while Claude works: resending would delay it' }
  if (s.hasAttachments) return { kind: 'advise', why: 'has images, which cannot be resent' }
  if (s.hasMention) return { kind: 'advise', why: 'has @mentions, which are not expanded on resend' }
  return { kind: 'switch', setModel: needModel, setEffort: needEffort, why: needModel ? 'model' : 'effort only' }
}
