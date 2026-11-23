# Declines for Claude Code

A "no" to a permission request should be a record that code checks, not something the model is trusted to remember.

Declines is a small Claude Code plugin. When you decline a permission request, it writes that down. If Claude later asks for the same thing, or a lightly reworded version of it, the prompt never reaches you: Claude is told you already said no.

It is the Claude Code counterpart of [hermes-declines](https://github.com/percyhendrux123-prog/hermes-declines), which does this for scheduled jobs proposed to Hermes Agent. The scheduled-job part does not carry over as is, because Claude Code does not ask permission before it schedules a task. See [Scheduled tasks](#scheduled-tasks).

It is a Claude Code mod: JavaScript handlers that run inside Claude Code. It needs Claude Code 2.1.287 or later. It was built and checked against 2.1.325.

## What it does

- **Remembers a decline.** When Claude Code asks you to approve a tool call and you say no, Declines saves one record: an id, the time, the tool name, a digest of the project, and salted hashes of the words in the request.
- **Holds repeats.** The next time a permission prompt is about to appear for a matching request, Declines replaces it with a refusal that Claude reads:
  > The user declined a matching Bash call on 2026-11-21. Do not propose it again, or a reworded version of it; if they want it, they will ask.

  The message never repeats the command.
- **Only replaces prompts.** It acts only when Claude Code's own answer is "ask the user". A call that a rule, a mode or another hook already allowed or denied is left alone, so adding an allow rule later wins over an old decline.
- **Lets you change your mind.** A decline stops holding after 30 days. `/declines list` shows active ones, `/declines forget <id>` removes one, `/declines clear` removes all.
- **Fails open.** If a handler throws or runs out of time, Claude Code skips it and the prompt appears as it would without the plugin. It makes no network calls and starts no processes.

### How "the same request" is decided

Same tool, same project, and the words overlap enough. Text is lowercased; dates, times, numbers, punctuation and words like "on", "at", "next" and the weekday and month names are removed. Two requests match when the overlap (Jaccard) of their remaining words is at least `threshold` (0.6). It is the same rule hermes-declines uses.

What is compared depends on the tool: the command for `Bash` and `PowerShell`, the URL for `WebFetch`, the query for `WebSearch`, the prompt for `Agent` and `CronCreate`, and every short string in the input for anything else, such as an MCP tool.

## What it stores, and what it doesn't

Per decline: `id`, `ts`, `tool`, `proj`, `toks`.

- `toks` is a list of salted 53-bit hashes of the request's words. The words, the command, its arguments and any paths are not stored. The hash is not encryption: a guessable word can be guessed. Treat the file as private.
- `proj` is a salted hash of the project root, so declines in one project do not hold in another. The path itself is not stored.
- Pending requests (asked, not yet answered) live in memory only.
- Records live in Claude Code's own plugin store, `~/.claude/plugins/store/` (a JSON file owned by you, mode 0600 as Claude Code writes it). At most 200 are kept; expired ones are removed when a new one is saved.
- `/declines list` shows the id, date, tool name and expiry. It does not show what was declined.

## Before you install

- **Runs code inside Claude Code.** A mod is not sandboxed and runs with your permissions. Read `claude-plugin/declines/hooks/register.mjs` and `core.mjs` first; together they are about 400 lines. `claude plugin validate claude-plugin/declines` prints the hooks it registers and every API call it makes.
- **API calls it makes:** `$.store` (get, set, delete, keys), `$.session.root`, `$.command.register`. Nothing else: no files, processes, network or model calls.
- **Not in Anthropic's plugin directory.** Plugins with mods are held back until the publisher is verified, so this installs from GitHub.

## Install

```
claude plugin marketplace add percyhendrux123-prog/hermes-declines
claude plugin install axiom-declines@deploy-axiom-declines
```

Claude Code then says three options are "not yet set". That is expected: each has a default, and you can change them in `/config`.

To try it for one session without installing:

```
claude --plugin-dir ./claude-plugin/declines
```

## Settings

| key | default | meaning |
| --- | --- | --- |
| `threshold` | `0.6` | how alike two requests must be (0.3 to 1) to count as the same |
| `expire_days` | `30` | days a decline holds. `0` turns holding off; declines are still saved |
| `ask_before_scheduling` | `false` | prompt before Claude schedules a session task, so there is something to decline |

## Scheduled tasks

Hermes proposes scheduled jobs on a "Schedule this job?" card, so a "no" has somewhere to be recorded. Claude Code has no such card. Its session scheduler (`CronCreate`, which backs `/loop`) is not permission-gated by default, and Claude Code reports nothing to a plugin when it creates one.

So the Claude Code version is the general idea, not a port of the scheduler:

- By default it remembers declines of any permission prompt: shell commands, web fetches, MCP tools, subagents and so on.
- With `ask_before_scheduling` on, a `CronCreate` call that Claude Code would run without asking becomes a prompt. If you decline it, the job is remembered like any other decline, and a reworded version of it is held. Only `CronCreate` is covered.

The Hermes plugin's duplicate-job check (refusing a job that matches one that already exists) is not ported. Cloud scheduled tasks (`RemoteTrigger`) and the `ScheduleWakeup` calls behind a self-paced `/loop` are not covered.

## Commands

```
/declines                 list active declines (same as /declines list)
/declines forget <id>     remove one
/declines clear           remove all, including expired ones
```

Forgetting one decline does not clear another that also matches.

## How it learns about a "no"

Claude Code does not tell a plugin what you answered in a permission dialog. Declines infers it:

1. When Claude Code's answer for a call is "ask", and a person can be asked, the call is held in memory.
2. A call that then runs, or fails while running, is dropped: you said yes.
3. When the next request goes to the model, or the turn ends with an answer, anything still held never ran. That is recorded as a decline.
4. A turn that you interrupt, or that fails, records nothing.

Subagents settle their own calls, so a background subagent's open prompt is not mistaken for yours.

## Known gaps

- **The dialog itself was not driven.** The inference above is tested against a stand-in for Claude Code and under Claude Code's own mod test kit, with its permission answer stubbed. It has not been run through a real interactive permission dialog, and no real headless run exercised it: `claude -p` cannot answer a dialog, so nobody is there to decline. If Claude Code orders `PostToolUse` or step events differently from what the docs describe, a decline could be missed or recorded late.
- **It cannot tell who said no.** A call that was asked and did not run is treated as declined. A `PermissionRequest` hook or a managed policy that denies the call after the ask looks the same. Auto mode's classifier was not tested.
- **Same-turn repeats.** A decline is saved when the next request goes to the model. A second identical call in the same assistant message, before that, is not held.
- **Edits are not tracked.** `Edit`, `MultiEdit`, `Write` and `NotebookEdit` are excluded, as are plan and question prompts. A declined edit is usually followed by a revised edit to the same file, and holding that would block the revision.
- **Wording matters.** It catches the same request with different dates and numbers, not a genuinely different one. A lower `threshold` catches more and risks holding unrelated requests.
- **Per machine.** Declines are kept per Claude Code config directory and are not shared between machines. A project that moves to a new path is a new project.
- **A hold is a message, not a lock.** It stops Claude's call. You can still run the command yourself.
- **Several sessions.** Each decline is its own key in the shared store, so two sessions saving at once do not overwrite each other. The store itself is not locked, so this is not tested under real contention.
- **Claude Code versions.** The `session.stop` event, which would clear held calls on an interrupt, needs 2.1.326, so this version does not use it; an interrupted turn is handled at `turn.complete` instead.

## Try it with no install

```
node --test claude-plugin/tests
```

The tests run the mod's real hooks against a small stand-in for Claude Code. They use no network, start no Claude Code and touch nothing in `~/.claude`. To run the same checks under Claude Code's own test kit, copy `claude-plugin/tests/engine/declines.engine.test.ts.sample` into `claude-plugin/declines/` as `declines.engine.test.ts` and run `claude plugin test` there.

## Undo

```
claude plugin uninstall axiom-declines
claude plugin marketplace remove deploy-axiom-declines
```

Records stay in `~/.claude/plugins/store/` until Claude Code clears an unused store (`cleanupPeriodDays`), or until you run `/declines clear` before uninstalling.

## How it was made

I designed it, set the rules for what it may store, and tested it on my own system. AI agents wrote the code under my spec and review. I read JavaScript; I don't claim to write it by hand.

Percy Keith, Deploy Axiom. MIT license.
