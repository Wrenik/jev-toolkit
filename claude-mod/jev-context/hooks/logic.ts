// Pure decision logic for jev-context: no `$`, so it can be tested directly.

export const MODES = ['off', 'advise'] as const
export type Mode = (typeof MODES)[number]

// Prompts from the person; same origins as jev-router.
export const PERSON_ORIGINS: readonly string[] = ['composer', 'sdk', 'bridge']

// Below this context size compacting never pays (docs/CONTEXT_COMPILER.md section 4.2), so we do not
// even ask the judge.
export const MIN_CONTEXT_TOKENS = 100_000
// At most one question per this many ms, so a long session is not asked about on every prompt.
export const MIN_GAP_MS = 5 * 60 * 1000
export const MAX_PROMPTS = 5

export type Advice = {
  ok: boolean
  action?: 'keep' | 'compact_now' | 'clear_with_seed'
  reason?: string
  judge_used?: boolean
  break_even_turns?: number | null
  expected_turns?: number
  error?: string
}

export type AskInput = {
  mode: Mode
  contextTokens: number | null
  now: number
  lastAskedAt: number | null
  isSubagent: boolean
}

export function shouldAsk(s: AskInput): boolean {
  if (s.mode === 'off' || s.isSubagent) return false
  if (s.contextTokens === null || s.contextTokens < MIN_CONTEXT_TOKENS) return false
  if (s.lastAskedAt !== null && s.now - s.lastAskedAt < MIN_GAP_MS) return false
  return true
}

export function pushPrompt(prompts: string[], text: string): string[] {
  return [...prompts, text].slice(-MAX_PROMPTS)
}

// Only a non-"keep" advice interrupts the person; "keep" stays silent (status line only).
export function toastFor(a: Advice): string | null {
  if (!a.ok || !a.action || a.action === 'keep') return null
  const n = a.break_even_turns != null ? ` (pays off after ~${a.break_even_turns.toFixed(1)} turns)` : ''
  if (a.action === 'compact_now') return `Jev suggests /compact now${n}. ${a.reason ?? ''}`.trim()
  return `Jev suggests /clear: the task looks finished and little of this context is still needed. ${a.reason ?? ''}`.trim()
}

export function describe(a: Advice | null): string {
  if (!a) return 'no advice yet'
  if (!a.ok) return `no advice: ${a.error}`
  return `${a.action}: ${a.reason}${a.judge_used === false ? ' [judge was unavailable]' : ''}`
}
