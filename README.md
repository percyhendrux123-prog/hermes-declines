# hermes-declines

> **Part of the seat**, a collection of Hermes plugins that work together: watch, decide, act, prove, hand off. This one covers **decide**: it remembers your no. [See the whole collection](https://github.com/percyhendrux123-prog#the-seat-a-collection-of-hermes-plugins).

A "no" to a scheduled job should be a record that code checks, not something the model is trusted to remember.

[Hermes Agent](https://github.com/NousResearch/hermes-agent) lets the model propose scheduled jobs through `cronjob_manage`, and upstream PR [#158289](https://github.com/NousResearch/hermes-agent/pull/158289) puts each proposal on a "Schedule this job?" card. If you say no, the only memory of it is in the conversation, so the same job tends to come back next week under a new name or date. This plugin writes your "no" down and checks it before the next proposal goes through.

## What it does

- **Records declines.** When you deny an approval that is about a scheduled job, it saves one record: an id, the time, the session key, the surface (cli, gateway, ...) and a fingerprint of the job.
- **Holds repeats.** When the model calls `cronjob_manage` to create a job (or update its prompt, schedule or delivery) and the proposal matches a decline that is still active, the call is vetoed with a message to the model:
  > The user declined a matching job on 2026-11-21. Do not propose it again; if they want it, they will ask.
- **Stops quiet duplicates.** A proposal that matches an existing active **or paused** job is vetoed too: "A matching job already exists (ID <id>, <state>); update or resume it instead of creating a new one." The veto never echoes the prompt or job name. A paused job stays paused; the plugin never changes a job.
- **Lets you change your mind.** A decline expires after `expire_days` (30). `/declines list` shows active ones, `/declines forget <id>` removes one matching decline, and `/declines clear` removes all declines. Forgetting doesn't clear another matching decline, override duplicate detection, or bypass another Hermes guard. No arguments are added to core tools.
- **Fails open.** The policy hook uses only in-memory snapshots, never a filesystem lock, cron read, config read or log write. It skips comparisons if another comparison is running or its 20 ms budget is spent. The host's safety timeout remains enabled. Snapshots refresh before a model turn and cron data also refreshes after cron tool calls. Unavailable or more-than-five-minute-old snapshots are skipped. No network calls.

### How "same job" is decided

The prompt is lowercased, and dates, times, numbers, month/weekday names, "today/tomorrow", "on/at/next" and punctuation are stripped, then whitespace is collapsed. Two proposals match when their delivery target is the same and the token overlap (Jaccard) of the normalized prompts is at least `threshold` (0.6). An omitted target or a target that includes `origin` must also match the host's chat route (including thread and participant when present). Without verified chat context, decline matching for `origin` stays within the same Hermes session. The plugin skips an existing-job `origin` match when its stored route cannot be verified; it never treats `origin` as a wildcard or guesses it from tool arguments. Legacy unscoped `origin` declines remain manageable but do not veto.

When the host identifies a local session, duplicate detection can compare omitted delivery against an existing `local` job. Clearing delivery on an update is also local, not `origin`. For new jobs from top-level Slack messages, the plugin uses the channel route for recognized recurring intervals (including leading zeroes), weekday/time phrases such as `every Monday at 9am`, and five- or six-field cron expressions with numeric lists, zero-padded values, named months/weekdays, or a standalone last-weekday selector such as `0 9 * * L5` (last Friday of the month). It also uses the channel for supported `in <duration>` one-shots over 60 minutes; positive relative one-shots up to 60 minutes retain the message thread. Real Slack threads are not dropped. This is bounded route recognition, not schedule validation: the host still validates expressions. It classifies `every 0m` as recurring, like the host, but abstains on a zero-delay one-shot (`in 0m`). It also skips an absolute timestamp or unrecognized schedule rather than guessing the channel or thread. Absolute schedules can cross the horizon while the user is deciding. Slack edits keep the job's stored origin and compare it with the current chat rather than reparsing the display schedule.

## What it stores

Per decline: id, timestamp, session key, surface, normalized prompt text, SHA-256 of the raw prompt, delivery target, schedule kind, and a digest of the host chat or session for `origin`. **Normalized text is sensitive, not redacted.** It can retain names and other private details and is shown by `/declines list`. The raw prompt, tool arguments, results and other messages are not stored. Pending proposals live in memory only.

Files are written under `HERMES_HOME/plugins-state/hermes-declines/` (or `state_dir`): the directory is 0700 and every file is 0600. Expiry stops a decline from vetoing and hides it from the list; it does not by itself delete the record. `/declines clear` deletes all records, including expired ones, and discards writes already accepted by this plugin instance but not yet saved. The count reports saved records removed. New denials after the clear can still be recorded; it does not cancel work queued in other processes.

Denials are saved by a background writer, not by the approval prompt. A busy lock, full queue, disk failure or process exit before the write can leave a denial unrecorded. Matching starts once the write completes. A write that would grow the decline store past 256 KiB first tries removing expired records; if it still won't fit, the new denial is not saved, the existing file is left intact, and Hermes logs a warning. For a legacy oversized file, only a still-fresh view of the same store can veto. `/declines forget` removes the selected record from that cached view immediately, even if the file is still oversized. A fresh process has no such view: a later denial can restore a bounded store if expired records can be pruned, or you can make room with `/declines forget` or `/declines clear`. Refresh never rewrites the file. The cron snapshot is also limited to 256 jobs and proposal fields to 8,192 characters. Veto and listing dates are UTC.

## Settings

Under `plugins.entries.hermes-declines.settings` in Hermes config:

| key | default | meaning |
| --- | --- | --- |
| `threshold` | `0.6` | similarity (0-1) at which two jobs count as the same |
| `expire_days` | `30` | days a decline stays active; `0` turns holds off |
| `check_existing_jobs` | `true` | also veto near-duplicates of existing active/paused jobs; disabling this doesn't stop the snapshot from supplying unchanged fields for update checks |
| `state_dir` | `HERMES_HOME/plugins-state/hermes-declines` | where records are kept |

## Install and test

Copy or clone this directory into your Hermes plugins directory as `hermes-declines` and enable it. Validate with `hermes plugins validate <dir>`. Tests need only the standard library: `python3 -m unittest discover -s tests`.

## Not covered

- **Hermes must emit a native schedule approval response.** If `cronjob_manage` never prompts, there is no "no" to record. The plugin recognizes the `cron_create` and `cron_update` pattern keys emitted by [#158289](https://github.com/NousResearch/hermes-agent/pull/158289), even when the hook has no `kind` field. It also accepts `schedule` / `schedule_edit` as a kind or pattern key; `kind` is the clearer signal if the host supplies it. A generic `plugin_rule:*` denial is ignored, even with an exact call ID: the host does not report the effective arguments, which another hook may have changed. Do not combine recording with hooks that rewrite scheduling arguments.
- **Only a human "no" counts.** Timeouts, cancelled prompts and the auxiliary-model "smart deny" are not recorded as declines.
- **The deny is tied to an exact pending tool-call ID in the same process.** Missing, unknown or ambiguous IDs, conflicting session or turn identifiers, create/edit mismatches, and callbacks after completion or restart do not record anything. A session or turn alone is not enough: even a lone pending proposal may not be the one that was denied. The plugin rejects reused call IDs across sessions within its one-hour correlation window, even if comparison was skipped. It tracks up to 200 identities. If the table is busy or full, uncertain IDs (and IDs that map to the same bounded bookkeeping slot) may go unrecorded. Free-text mentions are never used for attribution. Declines of edits are only saved if the proposal supplies its own prompt and explicit non-`origin` delivery. A schedule-only denial is never recorded: a snapshot can inform a veto but is never used to persist an inherited prompt or target.
- **Wording matters.** It catches the same job with different dates and times, not a genuinely reworded job. A lower `threshold` catches more and risks holding unrelated jobs. A different deliver target is a different job. Schedule kind is stored but not part of the comparison.
- **Only `cronjob_manage` create/update calls are held.** Jobs made with `hermes cron` on the command line, the dashboard or an API are not checked. Pause, resume, run, remove and list are never touched. Skills-only or script-only jobs with no prompt are not compared.
- **Updates need a resolvable job in the snapshot.** Exact IDs win; otherwise only a unique case-insensitive name is accepted. The plugin excludes that job from the duplicate check and leaves missing or ambiguous references to Hermes itself.
- **A veto is a message, not a lock on the user.** It stops the model's call; you can still create the job yourself outside this plugin's check.
- **Cron snapshot compatibility.** The manifest's minimum Hermes version is 0.22.5. The optional existing-job check targets versions that write the standard per-profile `HERMES_HOME/cron/jobs.json` as `{"jobs": [...]}` with full prompts and normal job IDs, delivery targets, and state fields, as in the reviewed host source. No `cron.jobs` import, lock, repair, write, or truncated public tool listing is used. Custom cron store overrides and legacy formats are not supported. Neither the `cron` directory under the host-selected profile home nor the `jobs.json` file may be a symlink. Missing, malformed, non-regular, oversized (over 256 KiB or 256 jobs), or stale snapshots also disable existing-job checks and updates that need old fields, including schedule-only updates. Fingerprintable creates still check the decline snapshot. Completed and errored jobs do not count as duplicates.
- **Records are per Hermes home/profile** and are not shared between profiles.
- **Native approval verification.** Tests replay the CLI payload observed on upstream PR #158289. A live smoke against that PR's core (head `dd4e675b10`, mock model, throwaway Hermes home) passed for create and edit cards: deny recorded, reworded repeat vetoed with no card, unrelated job prompted, duplicate vetoed, `forget` and `clear` worked. The same smoke was rerun on the released revision with the same results; a deny on an edit that only changes the schedule is not recorded, by design. Validate on your own Hermes host before enabling it.

## License

MIT
