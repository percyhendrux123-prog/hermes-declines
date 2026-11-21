# SPEC: hermes-declines

## Why
Upstream PR NousResearch/hermes-agent#158289 lets the model propose scheduled jobs on a "Schedule this job?" card. Reviewers (zambodotdev, LangerSword) point out that a "no" lives only in the model's memory, so a declined job comes back next week under a new name or date. A "no" should be a record that is checked in code before a card is drawn.

## What it does
1. **Records declines.** Subscribe to `post_approval_response` (see hermes-ref/hermes_cli/plugins.py VALID_HOOKS comment, tools/approval_context.py, tools/approval_prompt.py). When the approval concerns a scheduled job (payload kind/pattern_key in {"schedule","schedule_edit"} when present, OR the approval was raised for a `cronjob_manage` call) and `choice` is a deny/decline, write one record: id, timestamp, session_key, surface, and a **fingerprint** of the proposed job.
   Also records from main: a `pre_tool_call` on `cronjob_manage` create/update stores the pending proposal per session so the later deny can be matched to its job.
2. **Fingerprint** = normalized (prompt text: lowercase, strip dates/times/numbers/punctuation, collapse whitespace; deliver target; schedule kind). Similarity = token Jaccard on the normalized prompt, same deliver target. Threshold configurable (default 0.6). Keep the normalized text and a sha256 of the raw prompt; do not store the raw prompt.
3. **Holds repeats.** `pre_tool_call` on `cronjob_manage` with action create (and update that changes prompt/schedule/deliver): if the proposal matches a decline that is still active, veto the call (read tools/cronjob_tools.py and the pre_tool_call veto contract in hermes-ref for the exact return shape) with a message to the model: "The user declined a matching job on <date> (<short normalized text>). Do not propose it again; if they want it, they will ask." Same check against existing jobs (read the cron jobs store via hermes-ref/cron/jobs.py helpers if importable at runtime, guarded by try/except): a near-duplicate of an existing active or PAUSED job is vetoed with "A matching job already exists (<name>, <state>); update or resume it instead of creating a new one." A paused job stays paused.
4. **User-initiated creates are not blocked forever.** Do not add arguments to core tools. Instead: a decline expires after `expire_days` (default 30) and the plugin registers a slash command `/declines` with `list`, `forget <id>`, `clear`. Forgetting a decline lets the next proposal through.
5. **Config** (plugin settings): `threshold`, `expire_days`, `check_existing_jobs` (default true), `state_dir` (default under HERMES_HOME/plugins-state/hermes-declines).
6. Fail-open everywhere: if parsing fails, allow the call and log.

## Acceptance
- Unit tests for: fingerprint normalization (date/time variants of the same job match; different jobs don't), deny recorded only for schedule-related approvals, allow not recorded, veto fires on a matching create, no veto after forget, no veto after expiry, near-duplicate of a paused job vetoed with the "resume" message, exception inside the hook does not raise, files are 0600.
- plugin.yaml validates in shape like the reference plugin; README with plain-English "what it does / what it doesn't" and the not-covered list (works best with PR #158289's schedule cards; on main it relies on whatever approval surfaces cronjob_manage).
- A short EVIDENCE.md in docs/ with the test output (counts only).
