// Tests for the Claude Code Declines mod (node:test, no dependencies).
//
// The mod's own hooks run against a small stand-in for Claude Code: a recorder
// for `on`, a Map behind `$.store`, and a stub for `$.session.root` and
// `$.command.register`. Nothing here starts Claude Code or touches ~/.claude.
// Run: node --test claude-plugin/tests
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(HERE, '..', 'declines')
const core = await import(join(PLUGIN, 'hooks', 'core.mjs'))
const { register } = await import(join(PLUGIN, 'hooks', 'register.mjs'))

const SECRET = 'CANARY-9f8e7d6c'
const DAY = 86400000

function makeEngine({ options, root = '/work/one', failStore = false } = {}) {
  const handlers = {}
  const on = (name, a, b) => {
    const [matcher, fn] = b ? [a, b] : [null, a]
    handlers[name] = { matcher, fn }
  }
  const store = new Map()
  const registered = []
  const $ = {
    store: {
      get: async (k) => {
        if (failStore) throw new Error('store down')
        return store.has(k) ? JSON.parse(JSON.stringify(store.get(k))) : undefined
      },
      set: async (k, v) => {
        if (failStore) throw new Error('store down')
        store.set(k, JSON.parse(JSON.stringify(v)))
      },
      delete: async (k) => void store.delete(k),
      keys: async () => {
        if (failStore) throw new Error('store down')
        return [...store.keys()]
      },
    },
    session: { root: async () => root },
    command: { register: async (spec) => void registered.push(spec) },
  }
  register(on, options)
  const engine = {
    $,
    store,
    registered,
    setRoot: (r) => void (root = r),
    // Fire an event through the mod's hook, with `verdict` standing for Claude Code's own answer.
    async check(call, verdict = { decision: 'ask' }) {
      const e = { tool: call.tool, input: call.input, tool_use_id: call.id, approver: call.approver ?? 'person' }
      return handlers['tool.check'].fn($, e, async () => verdict)
    },
    async ran(id) {
      return handlers['classic.PostToolUse'].fn($, { tool_use_id: id }, async (x) => x)
    },
    async failed(id) {
      return handlers['classic.PostToolUseFailure'].fn($, { tool_use_id: id }, async (x) => x)
    },
    async turnEnds(extra = {}) {
      return handlers['turn.complete'].fn($, { reason: 'answer', ...extra }, async (x) => x)
    },
    // The next request going to the model, which is when the previous step's tool calls have all settled.
    async step(extra = {}) {
      const stream = handlers['turn.step'].fn($, { turnId: 't', index: 1, ...extra }, async function* () {})
      for await (const _chunk of stream) void _chunk
    },
    async start() {
      return handlers['session.start'].fn($, {}, async (x) => x)
    },
    async command(args) {
      return handlers['command.run'].fn($, { command: 'declines', args }, async (x) => x)
    },
    handlers,
  }
  return engine
}

const bash = (command, id = 'toolu_1', extra = {}) => ({ tool: 'Bash', input: { command }, id, ...extra })
const records = (engine) => [...engine.store.entries()].filter(([k]) => k.startsWith('d:')).map(([, v]) => v)

// A decline, the way it happens: Claude Code asks, the person says no, the tool never runs, the turn ends with an answer.
async function decline(engine, call) {
  const out = await engine.check(call)
  assert.equal(out.decision, 'ask', 'first proposal is left to the person')
  await engine.turnEnds()
}

test('normalize drops dates, times, numbers, punctuation and date glue', () => {
  assert.equal(core.normalize('Run backup on Friday at 9:30am, 2026-11-23!'), 'run backup')
  assert.equal(core.normalize('Send report TOMORROW (3rd try) to /tmp/x'), 'send report try to tmp x')
  assert.equal(core.normalize(42), '')
})

test('Jaccard matching holds a reworded date but not an unrelated request', () => {
  const salt = 'saltsalt'
  const a = core.hashAll(salt, core.tokensOf('CronCreate', { prompt: 'Check the build status every Monday at 9am' }))
  const b = core.hashAll(salt, core.tokensOf('CronCreate', { prompt: 'Check the build status every Tuesday at 10am' }))
  const c = core.hashAll(salt, core.tokensOf('CronCreate', { prompt: 'Summarise unread email' }))
  assert.ok(core.jaccard(a, b) >= 0.6)
  assert.ok(core.jaccard(a, c) < 0.6)
  assert.equal(core.jaccard([], a), 0)
})

test('a call with nothing to compare is not fingerprintable', () => {
  assert.deepEqual(core.tokensOf('Bash', { command: '12345 2026-01-01' }), [])
  assert.deepEqual(core.tokensOf('Bash', {}), [])
  assert.deepEqual(core.tokensOf('Bash', null), [])
})

test('settings are clamped and default as documented', () => {
  assert.deepEqual(core.clampSettings(undefined), { threshold: 0.6, expireDays: 30, askSchedules: false })
  assert.deepEqual(core.clampSettings({ threshold: 5, expire_days: -3, ask_before_scheduling: 'yes' }), {
    threshold: 1,
    expireDays: 0,
    askSchedules: false,
  })
  assert.equal(core.clampSettings({ ask_before_scheduling: true }).askSchedules, true)
})

test('session.start registers /declines', async () => {
  const engine = makeEngine()
  await engine.start()
  assert.equal(engine.registered.length, 1)
  assert.equal(engine.registered[0].name, 'declines')
})

test('a person declining is recorded once the turn ends with an answer', async () => {
  const engine = makeEngine()
  await decline(engine, bash('./scripts/sync-prod.sh --token ' + SECRET))
  const recs = records(engine)
  assert.equal(recs.length, 1)
  assert.equal(recs[0].tool, 'Bash')
  assert.deepEqual(Object.keys(recs[0]).sort(), ['id', 'proj', 'toks', 'tool', 'ts'])
})

test('nothing readable is stored: no command words, no secret, no path', async () => {
  const engine = makeEngine({ root: '/home/sample-user/client-project' })
  await decline(engine, bash('curl -H "Authorization: ' + SECRET + '" https://example.test/deploy-prod'))
  const dump = JSON.stringify([...engine.store.entries()]).toLowerCase()
  for (const word of ['canary', 'authorization', 'curl', 'deploy', 'example', 'sample-user', 'client-project']) {
    assert.ok(!dump.includes(word), 'store must not contain ' + word)
  }
})

test('a call that ran after the prompt (approved) is not a decline', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_ok'))
  await engine.ran('toolu_ok')
  await engine.turnEnds()
  assert.equal(records(engine).length, 0)
})

test('a call that failed after the prompt (approved, then errored) is not a decline', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_err'))
  await engine.failed('toolu_err')
  await engine.turnEnds()
  assert.equal(records(engine).length, 0)
})

test('an interrupted or failed turn records nothing and forgets the call', async () => {
  const engine = makeEngine()
  for (const reason of ['aborted', 'error', 'refusal']) {
    await engine.check(bash('make release', 'toolu_' + reason))
    await engine.turnEnds({ reason })
  }
  await engine.turnEnds() // a later answered turn must not resurrect them
  assert.equal(records(engine).length, 0)
})

test('a subagent finishing a turn does not settle the main conversation\'s open prompt', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_wait'))
  await engine.turnEnds({ agentId: 'agent-7' })
  assert.equal(records(engine).length, 0)
  await engine.turnEnds()
  assert.equal(records(engine).length, 1)
})

test('a reworded repeat in the same turn is held as soon as the next step starts', async () => {
  const engine = makeEngine()
  assert.equal((await engine.check(bash('git push origin release-branch --force', 'toolu_a'))).decision, 'ask')
  // The person said no; Claude's next request goes out, and it tries again.
  await engine.step()
  assert.equal(records(engine).length, 1)
  const again = await engine.check(bash('git push --force origin release-branch', 'toolu_b'))
  assert.equal(again.decision, 'deny')
})

test('a step settles a call that ran as not declined', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_ok'))
  await engine.ran('toolu_ok')
  await engine.step()
  assert.equal(records(engine).length, 0)
})

test('a step belongs to its own loop: a subagent step leaves the main conversation\'s prompt open', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_main'))
  await engine.step({ agentId: 'agent-7' })
  assert.equal(records(engine).length, 0)
  await engine.step()
  assert.equal(records(engine).length, 1)
})

test('a subagent\'s own declined call is settled by that subagent\'s turn', async () => {
  const engine = makeEngine()
  await engine.handlers['tool.check'].fn(engine.$, { tool: 'Bash', input: { command: 'make release' }, tool_use_id: 'toolu_sub', approver: 'person', agentId: 'agent-7' }, async () => ({ decision: 'ask' }))
  await engine.turnEnds()
  assert.equal(records(engine).length, 0)
  await engine.turnEnds({ agentId: 'agent-7' })
  assert.equal(records(engine).length, 1)
})

test('only a person counts: host-decided, allowed and denied calls are never recorded', async () => {
  const engine = makeEngine()
  await engine.check(bash('make release', 'toolu_h', { approver: 'host' }))
  await engine.check(bash('make deploy', 'toolu_a'), { decision: 'allow' })
  await engine.check(bash('make destroy', 'toolu_d'), { decision: 'deny', reason: 'rule' })
  await engine.check({ tool: 'Bash', input: { command: 'make nothing' } }) // no call id
  await engine.turnEnds()
  assert.equal(records(engine).length, 0)
})

test('edits, plans and questions are never tracked', async () => {
  const engine = makeEngine()
  for (const tool of ['Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'ExitPlanMode', 'AskUserQuestion', 'EnterPlanMode']) {
    const out = await engine.check({ tool, input: { file_path: '/a/b.txt', content: 'hello world' }, id: 'toolu_' + tool })
    assert.equal(out.decision, 'ask')
  }
  await engine.turnEnds()
  assert.equal(records(engine).length, 0)
})

test('a repeat of a declined call is refused with the note, and the note does not echo the call', async () => {
  const engine = makeEngine()
  await decline(engine, bash('git push origin release-branch --force', 'toolu_1'))
  const out = await engine.check(bash('git push origin release-branch --force', 'toolu_2'))
  assert.equal(out.decision, 'deny')
  assert.match(out.reason, /^The user declined a matching Bash call on \d{4}-\d{2}-\d{2}\. Do not propose it again/)
  assert.match(out.reason, /if they want it, they will ask\.$/)
  for (const word of ['git', 'push', 'release', 'force']) assert.ok(!out.reason.toLowerCase().includes(word))
})

test('a lightly reworded repeat is held; an unrelated call still reaches the person', async () => {
  const engine = makeEngine()
  await decline(engine, bash('git push origin release-branch --force'))
  const reworded = await engine.check(bash('git push --force origin release-branch', 'toolu_r'))
  assert.equal(reworded.decision, 'deny')
  const unrelated = await engine.check(bash('npm test', 'toolu_u'))
  assert.equal(unrelated.decision, 'ask')
})

test('a decline holds only the same tool', async () => {
  const engine = makeEngine()
  await decline(engine, bash('fetch the report'))
  const other = await engine.check({ tool: 'PowerShell', input: { command: 'fetch the report' }, id: 'toolu_p' })
  assert.equal(other.decision, 'ask')
})

test('a decline holds only inside its own project', async () => {
  const engine = makeEngine({ root: '/work/one' })
  await decline(engine, bash('git push origin release-branch --force'))
  engine.setRoot('/work/two')
  const out = await engine.check(bash('git push origin release-branch --force', 'toolu_x'))
  assert.equal(out.decision, 'ask')
  engine.setRoot('/work/one')
  assert.equal((await engine.check(bash('git push origin release-branch --force', 'toolu_y'))).decision, 'deny')
})

test('the hold only replaces a prompt: a rule or mode that now allows the call wins', async () => {
  const engine = makeEngine()
  await decline(engine, bash('make release'))
  const out = await engine.check(bash('make release', 'toolu_z'), { decision: 'allow' })
  assert.equal(out.decision, 'allow')
})

test('a decline expires after expire_days, and 0 turns holding off', async () => {
  const engine = makeEngine()
  await decline(engine, bash('make release'))
  const [key] = [...engine.store.keys()].filter((k) => k.startsWith('d:'))
  const rec = engine.store.get(key)
  engine.store.set(key, { ...rec, ts: Date.now() - 31 * DAY })
  assert.equal((await engine.check(bash('make release', 'toolu_old'))).decision, 'ask')
  engine.store.set(key, { ...rec, ts: Date.now() - 29 * DAY })
  assert.equal((await engine.check(bash('make release', 'toolu_new'))).decision, 'deny')

  const off = makeEngine({ options: { expire_days: 0 } })
  await decline(off, bash('make release'))
  assert.equal((await off.check(bash('make release', 'toolu_off'))).decision, 'ask')
  assert.equal(records(off).length, 1, 'declines are still saved while holding is off')
})

test('threshold is respected', async () => {
  const strict = makeEngine({ options: { threshold: 1 } })
  await decline(strict, bash('git push origin release-branch --force'))
  assert.equal((await strict.check(bash('git push origin release-branch', 'toolu_s'))).decision, 'ask')
  assert.equal((await strict.check(bash('git push origin release-branch --force', 'toolu_t'))).decision, 'deny')
})

test('/declines list, forget and clear', async () => {
  const engine = makeEngine()
  assert.equal((await engine.command('')).text, 'No active declines.')
  await decline(engine, bash('make release', 'toolu_1'))
  await decline(engine, bash('npm publish', 'toolu_2'))
  const listed = (await engine.command('list')).text
  assert.match(listed, /^Active declines \(2\):/)
  assert.match(listed, /Bash {2}until \d{4}-\d{2}-\d{2} {2}\(this project\)/)
  assert.ok(!/make|publish|release/i.test(listed), 'the list does not show what was declined')

  const [first] = records(engine)
  assert.match((await engine.command('forget ' + first.id)).text, /^Forgot /)
  assert.equal(records(engine).length, 1)
  assert.match((await engine.command('forget nope')).text, /^No decline with id nope\./)
  assert.match((await engine.command('forget')).text, /^Usage:/)
  assert.match((await engine.command('banana')).text, /^Usage:/)

  assert.equal((await engine.command('clear')).text, 'Cleared 1 decline.')
  assert.equal(records(engine).length, 0)
})

test('forgetting one decline leaves another matching decline holding', async () => {
  const engine = makeEngine()
  await decline(engine, bash('make release', 'toolu_1'))
  const [rec] = records(engine)
  engine.store.set('d:zzzzzz', { ...rec, id: 'zzzzzz' })
  await engine.command('forget ' + rec.id)
  assert.equal((await engine.check(bash('make release', 'toolu_2'))).decision, 'deny')
  await engine.command('forget zzzzzz')
  assert.equal((await engine.check(bash('make release', 'toolu_3'))).decision, 'ask')
})

test('the store stays bounded', async () => {
  const engine = makeEngine()
  for (let i = 0; i < 205; i++) {
    engine.store.set('d:old' + i, { id: 'old' + i, ts: Date.now() - i * 1000, tool: 'Bash', proj: 'p', toks: ['a'] })
  }
  await decline(engine, bash('make release'))
  assert.ok(records(engine).length <= core.MAX_RECORDS)
  assert.ok(records(engine).some((r) => r.toks.length > 0 && r.proj !== 'p'), 'the newest record survives pruning')
})

test('ask_before_scheduling is off by default: CronCreate is left allowed', async () => {
  const engine = makeEngine()
  const out = await engine.check({ tool: 'CronCreate', input: { cron: '0 9 * * 1', prompt: 'check the build' }, id: 'toolu_c' }, { decision: 'allow' })
  assert.equal(out.decision, 'allow')
})

test('ask_before_scheduling turns a CronCreate into a prompt, whose decline then holds the same job', async () => {
  const engine = makeEngine({ options: { ask_before_scheduling: true } })
  const job = (cron, prompt, id) => ({ tool: 'CronCreate', input: { cron, prompt, recurring: true }, id })
  const first = await engine.check(job('0 9 * * 1', 'Check the build status every Monday', 'toolu_c1'), { decision: 'allow' })
  assert.equal(first.decision, 'ask')
  assert.equal(first.reason, 'Schedule this job?')
  await engine.turnEnds()
  assert.equal(records(engine).length, 1)

  const again = await engine.check(job('0 10 * * 2', 'Check the build status every Tuesday', 'toolu_c2'), { decision: 'allow' })
  assert.equal(again.decision, 'deny')
  assert.match(again.reason, /declined a matching CronCreate call/)
  const other = await engine.check(job('0 9 * * 1', 'Summarise unread email', 'toolu_c3'), { decision: 'allow' })
  assert.equal(other.decision, 'ask')
})

test('ask_before_scheduling never loosens: a deny stays a deny', async () => {
  const engine = makeEngine({ options: { ask_before_scheduling: true } })
  const out = await engine.check({ tool: 'CronCreate', input: { prompt: 'x y' }, id: 'toolu_d' }, { decision: 'deny', reason: 'rule' })
  assert.equal(out.decision, 'deny')
  assert.equal(out.reason, 'rule')
})

test('fails open: a broken store leaves Claude Code\'s own verdict in place or lets the hook fail', async () => {
  const engine = makeEngine({ failStore: true })
  // Claude Code skips a hook that throws, so the prompt appears as it would without the plugin.
  await assert.rejects(() => engine.check(bash('make release')))
  // Calls that never reach the store are unaffected.
  const allowed = await engine.check(bash('ls', 'toolu_f'), { decision: 'allow' })
  assert.equal(allowed.decision, 'allow')
})

test('the mod calls no network, process or file API', () => {
  const code = readFileSync(join(PLUGIN, 'hooks', 'register.mjs'), 'utf8')
  for (const banned of ['$.http', '$.process', '$.fs', '$.mcp', '$.model', '$.prompt', '$.env']) {
    assert.ok(!code.includes(banned), 'register.mjs must not use ' + banned)
  }
  assert.ok(!/^\s*import .* from ['"](?!\.\/)/m.test(code), 'only relative imports')
})

test('manifest, hooks file and marketplace are consistent', () => {
  const manifest = JSON.parse(readFileSync(join(PLUGIN, '.claude-plugin', 'plugin.json'), 'utf8'))
  const hooks = JSON.parse(readFileSync(join(PLUGIN, 'hooks', 'hooks.json'), 'utf8'))
  const market = JSON.parse(readFileSync(join(HERE, '..', '..', '.claude-plugin', 'marketplace.json'), 'utf8'))
  assert.equal(manifest.name, 'axiom-declines')
  assert.equal(manifest.displayName, 'Declines')
  assert.equal(manifest.version, '0.1.0')
  assert.equal(manifest.license, 'MIT')
  assert.equal(manifest.author.name, 'Deploy Axiom')
  assert.equal(manifest.homepage, manifest.repository)
  assert.deepEqual(hooks.modules, ['./register.mjs'])
  assert.ok(existsSync(join(PLUGIN, 'hooks', 'register.mjs')))
  assert.ok(existsSync(join(PLUGIN, '.claude-plugin', 'icon.png')))
  assert.equal(market.name, 'deploy-axiom-declines')
  assert.equal(market.plugins[0].name, manifest.name)
  assert.equal(market.plugins[0].source, './claude-plugin/declines')
  for (const key of ['threshold', 'expire_days', 'ask_before_scheduling']) assert.ok(manifest.userConfig[key], key)
})
