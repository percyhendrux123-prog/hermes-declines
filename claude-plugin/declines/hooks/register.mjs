// Declines for Claude Code: remembers a permission request you said no to and
// stops Claude asking for the same thing again.
//
// It never approves anything. The only thing it can do to a call is turn a
// permission prompt Claude Code was about to show into a refusal, with a note to
// Claude. If anything here throws or runs out of time, Claude Code skips the hook
// and the prompt appears as it would without the plugin (fail open).
import {
  USAGE,
  clampSettings,
  cyrb53,
  findMatch,
  hashAll,
  isRecord,
  parseArgs,
  pruneList,
  renderList,
  shouldTrack,
  tokensOf,
  vetoMessage,
} from './core.mjs'

const RECORD_PREFIX = 'd:'

async function getSalt($) {
  const saved = await $.store.get('salt')
  if (typeof saved === 'string' && saved.length >= 8) return saved
  const fresh = Math.random().toString(36).slice(2) + Date.now().toString(36)
  await $.store.set('salt', fresh)
  return fresh
}

async function projectOf($, salt) {
  let root = ''
  try {
    root = await $.session.root()
  } catch {
    root = ''
  }
  return cyrb53(salt + ':project:' + String(root)).toString(36)
}

async function loadAll($) {
  const keys = await $.store.keys()
  const out = []
  for (const key of keys) {
    if (typeof key !== 'string' || !key.startsWith(RECORD_PREFIX)) continue
    const value = await $.store.get(key)
    if (isRecord(value)) out.push(value)
  }
  return out
}

async function saveRecord($, rec, expireDays) {
  await $.store.set(RECORD_PREFIX + rec.id, rec)
  const doomed = pruneList(await loadAll($), Date.now(), expireDays)
  for (const id of doomed) await $.store.delete(RECORD_PREFIX + id)
}

// Settle what is waiting for one loop (the main conversation, or one subagent):
// whatever was put to a person and never ran was declined. `record` false drops
// the entries without saving them.
async function settle($, pending, agentId, record, expireDays, state) {
  const mine = [...pending.entries()].filter(([, call]) => call.agent === agentId)
  for (const [id] of mine) pending.delete(id)
  if (!record || !mine.length) return
  const salt = await getSalt($)
  for (const [, call] of mine) {
    state.counter += 1
    const ts = Date.now()
    const id = cyrb53(salt + ':' + ts + ':' + state.counter + ':' + call.tool).toString(36).slice(0, 6)
    await saveRecord($, { id, ts, tool: call.tool, proj: call.proj, toks: call.toks }, expireDays)
  }
}

async function runCommand($, args, pending, expireDays) {
  const { sub, rest } = parseArgs(args)
  const salt = await getSalt($)
  const proj = await projectOf($, salt)
  const records = await loadAll($)
  if (sub === 'list') return { text: renderList(records, proj, Date.now(), expireDays) }
  if (sub === 'forget') {
    const id = rest[0]
    if (!id) return { text: USAGE }
    if (!records.some((r) => r.id === id)) return { text: 'No decline with id ' + id + '.' }
    await $.store.delete(RECORD_PREFIX + id)
    return { text: 'Forgot ' + id + '. Another matching decline, if any, still holds.' }
  }
  if (sub === 'clear') {
    for (const r of records) await $.store.delete(RECORD_PREFIX + r.id)
    pending.clear()
    return { text: 'Cleared ' + records.length + ' decline' + (records.length === 1 ? '' : 's') + '.' }
  }
  return { text: USAGE }
}

export function register(on, options) {
  const cfg = clampSettings(options)
  // Calls that were put to a person and are waiting to learn how it went.
  // Memory only; nothing reaches the store until the turn ends with an answer.
  const pending = new Map()
  const state = { counter: 0 }

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'declines',
      description: 'List or forget permission requests you declined',
      argumentHint: '[list | forget <id> | clear]',
    })
    return next(e)
  })

  // Every permission decision passes through here. The hook acts only when Claude
  // Code's own answer is "ask the user", so rules, modes and other hooks that
  // allow or deny a call are never overridden.
  on('tool.check', async ($, e, next) => {
    let decided = await next(e)
    if (!shouldTrack(e.tool)) return decided
    if (cfg.askSchedules && e.tool === 'CronCreate' && decided.decision === 'allow') {
      decided = { decision: 'ask', reason: 'Schedule this job?' }
    }
    if (decided.decision !== 'ask') return decided

    const toks = tokensOf(e.tool, e.input)
    if (!toks.length) return decided
    const salt = await getSalt($)
    const probe = { tool: e.tool, proj: await projectOf($, salt), toks: hashAll(salt, toks) }

    const hit = findMatch(await loadAll($), probe, cfg.threshold, Date.now(), cfg.expireDays)
    if (hit) return { decision: 'deny', reason: vetoMessage(hit) }

    // A person was asked. Remember the call; the answer is learned later.
    if (e.approver === 'person' && typeof e.tool_use_id === 'string' && e.tool_use_id) {
      if (pending.size >= 200) pending.delete(pending.keys().next().value)
      pending.set(e.tool_use_id, { ...probe, agent: e.agentId })
    }
    return decided
  })

  // A call that ran, or failed while running, was not declined.
  on('classic.PostToolUse', async ($, e, next) => {
    if (e && typeof e.tool_use_id === 'string') pending.delete(e.tool_use_id)
    return next(e)
  })
  on('classic.PostToolUseFailure', async ($, e, next) => {
    if (e && typeof e.tool_use_id === 'string') pending.delete(e.tool_use_id)
    return next(e)
  })

  // A new request going to the model means every tool call of the previous step has
  // settled. One that was put to a person and never ran was declined, so it is saved
  // now, in time to hold a reworded repeat in the same turn.
  on('turn.step', async function* ($, e, next) {
    try {
      await settle($, pending, e.agentId, true, cfg.expireDays, state)
    } catch {
      // Never hold up the request over a record that could not be saved.
    }
    yield* next(e)
  })

  // The last step of a turn has no request after it, so settle here too. An
  // interrupted or failed turn proves nothing, so its calls are dropped unsaved.
  on('turn.complete', async ($, e, next) => {
    try {
      await settle($, pending, e.agentId, e.reason === 'answer', cfg.expireDays, state)
    } catch {
      // Same: the turn's answer goes on regardless.
    }
    return next(e)
  })

  on('command.run', { command: 'declines' }, async ($, e) => {
    return runCommand($, e.args, pending, cfg.expireDays)
  })
}
