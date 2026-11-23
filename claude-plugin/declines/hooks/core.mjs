// Pure logic for Declines: no Claude Code calls and no I/O in this file.
// register.mjs owns every call to the mods API ($) and passes plain data in here.

export const DAY_MS = 86400000
export const MAX_TEXT = 8192
export const MAX_TOKENS = 128
export const MAX_RECORDS = 200

// Tools whose "no" is a normal part of working, not a proposal Claude should drop.
// A declined edit is usually followed by a revised edit to the same file, and a
// declined plan by a revised plan, so holding either would block the revision.
export const NEVER_TRACKED = new Set([
  'AskUserQuestion',
  'EnterPlanMode',
  'ExitPlanMode',
  'Edit',
  'MultiEdit',
  'Write',
  'NotebookEdit',
])

// Which input fields say what a call is about. Anything not listed falls back to
// every short string in the input, minus the fields that only describe the call.
const SALIENT = {
  Bash: ['command'],
  PowerShell: ['command'],
  WebFetch: ['url'],
  WebSearch: ['query'],
  Agent: ['prompt'],
  Task: ['prompt'],
  CronCreate: ['prompt'],
  Skill: ['skill', 'args'],
}
const NOISE_KEYS = new Set(['description', 'timeout', 'run_in_background', 'dangerouslyDisableSandbox'])

const MONTHS =
  'january february march april may june july august september october november december ' +
  'jan feb mar apr jun jul aug sep sept oct nov dec'
const WEEKDAYS = 'monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun'
const GLUE = 'today tomorrow tonight yesterday on at by around next this coming until till am pm'
const DROP_WORDS = new Set((MONTHS + ' ' + WEEKDAYS + ' ' + GLUE).split(' '))

const RE_ISO = /\b\d{4}-\d{1,2}-\d{1,2}(?:[t ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?\b/g
const RE_SLASH_DATE = /\b\d{1,4}[/.]\d{1,2}(?:[/.]\d{1,4})?\b/g
const RE_CLOCK = /\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?/g
const RE_DIGIT_TOKEN = /\b\d\w*/g
const RE_PUNCT = /[^\p{L}\p{N}]+/gu

export function shouldTrack(tool) {
  return typeof tool === 'string' && tool.length > 0 && !NEVER_TRACKED.has(tool)
}

// Same normalisation as the Hermes plugin: lowercase; drop dates, times, numbers,
// punctuation and the words that only introduce a date; collapse whitespace.
export function normalize(text) {
  if (typeof text !== 'string') return ''
  let s = text.slice(0, MAX_TEXT).toLowerCase()
  s = s.replace(RE_ISO, ' ').replace(RE_SLASH_DATE, ' ').replace(RE_CLOCK, ' ').replace(RE_DIGIT_TOKEN, ' ')
  s = s.replace(RE_PUNCT, ' ')
  return s
    .split(' ')
    .filter((t) => t && !DROP_WORDS.has(t))
    .join(' ')
}

function leafStrings(value, depth, out) {
  if (out.length > 64) return
  if (typeof value === 'string') {
    out.push(value)
  } else if (depth < 3 && Array.isArray(value)) {
    for (const v of value) leafStrings(v, depth + 1, out)
  } else if (depth < 3 && value && typeof value === 'object') {
    for (const k of Object.keys(value).sort()) {
      if (!NOISE_KEYS.has(k)) leafStrings(value[k], depth + 1, out)
    }
  }
}

// The text a call is compared on. Never stored: only salted token hashes leave this module.
export function salientText(tool, input) {
  if (!input || typeof input !== 'object') return ''
  const keys = SALIENT[tool]
  if (keys) {
    return keys
      .map((k) => (typeof input[k] === 'string' ? input[k] : ''))
      .filter(Boolean)
      .join(' ')
  }
  const out = []
  leafStrings(input, 0, out)
  return out.join(' ')
}

export function tokensOf(tool, input) {
  const text = normalize(salientText(tool, input))
  if (!text) return []
  const seen = new Set()
  for (const t of text.split(' ')) {
    if (seen.size >= MAX_TOKENS) break
    seen.add(t)
  }
  return [...seen]
}

// Small non-cryptographic 53-bit hash (cyrb53). It keeps plain words out of the
// store file; it is not encryption, and a guessable word can still be guessed.
export function cyrb53(str, seed = 0) {
  let h1 = 0xdeadbeef ^ seed
  let h2 = 0x41c6ce57 ^ seed
  for (let i = 0; i < str.length; i++) {
    const ch = str.charCodeAt(i)
    h1 = Math.imul(h1 ^ ch, 2654435761)
    h2 = Math.imul(h2 ^ ch, 1597334677)
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507)
  h1 ^= Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507)
  h2 ^= Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  return 4294967296 * (2097151 & h2) + (h1 >>> 0)
}

export function hashToken(salt, token) {
  return cyrb53(salt + ':' + token).toString(36)
}

export function hashAll(salt, tokens) {
  return [...new Set(tokens.map((t) => hashToken(salt, t)))].sort()
}

export function jaccard(a, b) {
  if (!a.length || !b.length) return 0
  const set = new Set(a)
  let both = 0
  for (const x of new Set(b)) if (set.has(x)) both += 1
  const union = new Set(a).size + new Set(b).size - both
  return union === 0 ? 0 : both / union
}

export function isActive(rec, now, expireDays) {
  if (!(expireDays > 0)) return false
  return now - rec.ts < expireDays * DAY_MS
}

// A decline holds a call when it is the same tool, in the same project, still
// within its expiry, and the hashed tokens overlap by at least `threshold`.
export function findMatch(records, probe, threshold, now, expireDays) {
  let best = null
  let bestScore = 0
  for (const rec of records) {
    if (rec.tool !== probe.tool || rec.proj !== probe.proj) continue
    if (!isActive(rec, now, expireDays)) continue
    const score = jaccard(rec.toks, probe.toks)
    if (score >= threshold && score > bestScore) {
      best = rec
      bestScore = score
    }
  }
  return best
}

export function isRecord(v) {
  return (
    !!v &&
    typeof v === 'object' &&
    typeof v.id === 'string' &&
    typeof v.ts === 'number' &&
    typeof v.tool === 'string' &&
    typeof v.proj === 'string' &&
    Array.isArray(v.toks)
  )
}

export function dateOf(ts) {
  return new Date(ts).toISOString().slice(0, 10)
}

export function vetoMessage(rec) {
  return (
    'The user declined a matching ' +
    rec.tool +
    ' call on ' +
    dateOf(rec.ts) +
    '. Do not propose it again, or a reworded version of it; if they want it, they will ask.'
  )
}

export function clampSettings(options) {
  const o = options && typeof options === 'object' ? options : {}
  const num = (v, d, lo, hi) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d)
  return {
    threshold: num(o.threshold, 0.6, 0.3, 1),
    expireDays: Math.floor(num(o.expire_days, 30, 0, 365)),
    askSchedules: o.ask_before_scheduling === true,
  }
}

// Keep the store bounded: drop what has expired, then the oldest beyond MAX_RECORDS.
export function pruneList(records, now, expireDays) {
  const keepFor = expireDays > 0 ? expireDays : 30
  const live = records.filter((r) => now - r.ts < keepFor * DAY_MS).sort((a, b) => b.ts - a.ts)
  const keep = new Set(live.slice(0, MAX_RECORDS - 1).map((r) => r.id))
  return records.filter((r) => !keep.has(r.id)).map((r) => r.id)
}

export function parseArgs(args) {
  const parts = String(args ?? '').trim().split(/\s+/).filter(Boolean)
  return { sub: (parts[0] ?? 'list').toLowerCase(), rest: parts.slice(1) }
}

export function renderList(records, proj, now, expireDays) {
  const active = records.filter((r) => isActive(r, now, expireDays)).sort((a, b) => b.ts - a.ts)
  if (!active.length) return 'No active declines.'
  const lines = active.map(
    (r) =>
      r.id +
      '  ' +
      dateOf(r.ts) +
      '  ' +
      r.tool +
      '  until ' +
      dateOf(r.ts + expireDays * DAY_MS) +
      (r.proj === proj ? '  (this project)' : '  (another project)'),
  )
  return 'Active declines (' + active.length + '):\n' + lines.join('\n')
}

export const USAGE = 'Usage: /declines [list] | /declines forget <id> | /declines clear'
