"""hermes-declines: a "no" to a scheduled job becomes a record that code checks.

Observer / fail-open: every hook catches its own exceptions, logs them and lets
the user's turn continue. The only thing this plugin ever does to a tool call
is veto a matching ``cronjob_manage`` create/update, with a message to the model.

No network calls. No raw prompts stored (normalized text + sha256 only).
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import core

try:
    from gateway.session_context import get_session_env
except ImportError:
    def get_session_env(name: str) -> str:
        return ""

log = logging.getLogger(__name__)

TOOL = "cronjob_manage"
SCHEDULE_KINDS = {"schedule": "create", "schedule_edit": "update"}
SCHEDULE_PATTERNS = {**SCHEDULE_KINDS, "cron_create": "create", "cron_update": "update"}
PENDING_TTL = 3600.0
PENDING_MAX = 200
SHORT_TEXT = 60
SNAPSHOT_TTL = 300.0
POLICY_BUDGET = 0.02  # seconds of comparison work, not a replacement for Hermes' safety timeout
MAX_JOBS = 256
MAX_FIELD = 8192
MAX_WRITES = 32
SLACK_THREAD_HORIZON_MINUTES = 60
ORIGIN_FIELDS = ("platform", "chat_id", "scope_id", "thread_id", "user_id")


def _short(text: str) -> str:
    if len(text) <= SHORT_TEXT:
        return text
    cut = text[:SHORT_TEXT]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "\u2026"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _fresh(view):
    deadline, rows = view
    return rows if time.monotonic() < deadline else ()


def _chat_scope(origin: Any) -> str:
    if not isinstance(origin, dict):
        return ""
    fields = tuple(_text(origin.get(key)) for key in ORIGIN_FIELDS)
    if not fields[0] or not fields[1] or any(len(value) > MAX_FIELD for value in fields):
        return ""
    return core.scope_digest("chat", *fields)


def _resolve_job(ref: str, jobs):
    if not ref:
        return None
    exact = next((job for job in jobs if job.get("id") == ref), None)
    if exact is not None:
        return exact
    lowered = ref.lower()
    matches = [job for job in jobs if _text(job.get("name")).lower() == lowered]
    return matches[0] if len(matches) == 1 else None



def _real_jobs() -> List[Dict[str, Any]]:
    """Read only the canonical per-profile file format; leave repair to Hermes."""
    data = core.read_snapshot(core.hermes_home() / "cron" / "jobs.json", verify_parent=True)
    if data is None:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
        raise ValueError("unsupported cron snapshot shape or size")
    ids = set()
    for job in jobs:
        ident = _text(job.get("id")) if isinstance(job, dict) else ""
        if not ident or ident in ids:
            raise ValueError("missing or repeated job ID")
        ids.add(ident)
        for field in ("prompt", "name", "deliver", "schedule_display"):
            value = job.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("unsupported cron record shape")
        if not isinstance(job.get("enabled", True), bool):
            raise ValueError("unsupported cron enabled flag")
    return jobs


def _job_state(job: Dict[str, Any]) -> str:
    stored = _text(job.get("state")).strip()
    if stored in ("completed", "error"):
        return stored
    return "scheduled" if job.get("enabled", True) else "paused"


class Declines:
    """All plugin behavior, with injectable config/jobs/clock so tests need no Hermes."""

    def __init__(self, get_config: Callable[[str, Any], Any],
                 jobs_loader: Callable[[], List[Dict[str, Any]]] = _real_jobs,
                 job_state: Callable[[Dict[str, Any]], str] = _job_state,
                 clock: Callable[[], float] = time.time, *, background_writes: bool = True):
        self._get_config = get_config
        self._jobs_loader = jobs_loader
        self._job_state = job_state
        self._clock = clock
        self._session_env = get_session_env
        self._settings = (core.DEFAULT_THRESHOLD, core.DEFAULT_EXPIRE_DAYS, True)
        self._jobs_view = self._decline_view = (0.0, ())
        self._decline_store_path: Optional[Path] = None
        self._jobs_lock = threading.Lock()
        self._decline_lock = threading.Lock()
        self._policy_slot = threading.Lock()
        self._background_writes = background_writes
        self._writes = deque()
        self._write_lock = threading.Lock()
        self._writer_running = False
        self._write_generation = 0
        # Native approval events may omit session/turn metadata. Never reuse a
        # call ID within the correlation window, even across sessions.
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        # Bounded, conservative fallback for calls we could not reserve. A hash
        # collision can suppress recording, but cannot attach the wrong proposal.
        self._pending_uncertain_until = [0.0] * PENDING_MAX

    # -- config ---------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._get_config(key, default)
        except Exception as exc:
            log.warning("hermes-declines: cannot read setting %s: %s", key, exc)
            return default
        return default if value is None else value

    def threshold(self) -> float:
        return core.coerce_float(self._cfg("threshold", core.DEFAULT_THRESHOLD), core.DEFAULT_THRESHOLD)

    def expire_days(self) -> float:
        return core.coerce_days(self._cfg("expire_days", core.DEFAULT_EXPIRE_DAYS), core.DEFAULT_EXPIRE_DAYS)

    def check_existing(self) -> bool:
        return core.coerce_bool(self._cfg("check_existing_jobs", True), True)

    def store(self) -> core.Store:
        configured = self._cfg("state_dir", "")
        if isinstance(configured, str) and configured.strip():
            return core.Store(Path(configured).expanduser())
        return core.Store(core.default_state_dir())

    # -- pending proposals ----------------------------------------------
    def _reserve_pending(self, kw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        call = _text(kw.get("tool_call_id"))
        if not call or len(call) > MAX_FIELD:
            return None
        bucket = hash(call) % len(self._pending_uncertain_until)
        if not self._pending_lock.acquire(blocking=False):
            # We couldn't check or retire a potentially reused ID. Ignore
            # approvals during its possible lifetime rather than wait or guess.
            self._pending_uncertain_until[bucket] = time.monotonic() + PENDING_TTL
            return None
        try:
            now = time.monotonic()
            self._pending = {k: v for k, v in self._pending.items() if now - v["ts"] < PENDING_TTL}
            if call in self._pending:
                self._pending[call] = {"fp": None, "ts": now}
                return None
            if len(self._pending) >= PENDING_MAX:
                self._pending_uncertain_until[bucket] = now + PENDING_TTL
                return None
            entry = {"fp": None, "ts": now}
            self._pending[call] = entry
            return entry if now >= self._pending_uncertain_until[bucket] else None
        finally:
            self._pending_lock.release()

    def _publish_pending(self, entry: Dict[str, Any], fp: Dict[str, str], action: str, kw: Dict[str, Any]) -> None:
        call = _text(kw.get("tool_call_id"))
        sid, skey, turn = (_text(kw.get(k)) for k in ("session_id", "session_key", "turn_id"))
        if not skey and sid and len(sid) <= MAX_FIELD and self._session_env("HERMES_SESSION_ID") == sid:
            skey = _text(self._session_env("HERMES_SESSION_KEY"))
        if max(len(sid), len(skey), len(turn)) > MAX_FIELD or not self._pending_lock.acquire(blocking=False):
            return
        try:
            if self._pending.get(call) is entry:
                entry.update(fp=fp, action=action, session_id=sid, session_key=skey, turn=turn)
        finally:
            self._pending_lock.release()

    def _forget_pending(self, kw: Dict[str, Any], *, action: str = "") -> Optional[Dict[str, Any]]:
        call = _text(kw.get("tool_call_id"))
        sid, skey = _text(kw.get("session_id")), _text(kw.get("session_key"))
        turn = _text(kw.get("turn_id"))
        # Even a lone pending call might not be the subject of an ID-less event:
        # the real subject may have completed, or never been seen by this process.
        if not call or len(call) > MAX_FIELD:
            return None
        with self._pending_lock:
            now = time.monotonic()
            entry = self._pending.get(call)
            if entry is None or now - entry["ts"] >= PENDING_TTL:
                return None
            for field, seen in (("session_id", sid), ("session_key", skey), ("turn", turn), ("action", action)):
                expected = entry.get(field)
                if seen and expected and seen != expected:
                    return None
            # Keep only the retired ID to reject delayed replays after completion.
            self._pending[call] = {"fp": None, "ts": now if entry["fp"] is not None else entry["ts"]}
            bucket = hash(call) % len(self._pending_uncertain_until)
            return entry if entry["ts"] >= self._pending_uncertain_until[bucket] else None

    # -- building a fingerprint from a tool call ---------------------------
    def _origin_scope(self, sid: str, schedule: Any = None, *, stored_origin=None) -> str:
        if not sid or len(sid) > MAX_FIELD:
            return ""
        if self._session_env("HERMES_SESSION_ID") == sid:
            route = {field: self._session_env("HERMES_SESSION_" + field.upper()) for field in ORIGIN_FIELDS}
            if (route["platform"] == "slack" and route["thread_id"]
                    and route["thread_id"] == self._session_env("HERMES_SESSION_MESSAGE_ID")):
                # Hermes uses a top-level Slack message as a synthetic thread only
                # for near one-shots. Absolute times can change sides while awaiting
                # approval; do not guess the route at the later creation time.
                if stored_origin is not None:
                    if not _text(stored_origin.get("thread_id")):
                        route["thread_id"] = ""  # an edit keeps its stored route; compare the same channel
                else:
                    text = _text(schedule).strip().lower()
                    if text.startswith("in "):
                        minutes = core.duration_minutes(text[3:])
                        if not minutes:
                            return ""  # unknown time, or already past when the host checks a zero delay
                        if minutes > SLACK_THREAD_HORIZON_MINUTES:
                            route["thread_id"] = ""
                    elif core.is_recurring_schedule(text):
                        route["thread_id"] = ""
                    else:
                        return ""
            scope = _chat_scope(route)
            if scope:
                return scope
        return core.scope_digest("session", sid)

    def _proposal(self, args: Dict[str, Any], jobs, sid: str) -> Optional[Dict[str, Any]]:
        """Fingerprint for a create/update, or None when the call is not one we judge.

        Returns {"fp": fingerprint, "self_id": job id or ""}.
        """
        action = _text(args.get("action")).strip().lower()
        if action == "create":
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return None  # skills-only / script jobs: nothing to compare
            fp = core.make_fingerprint(prompt, args.get("deliver"), args.get("schedule"))
            if core.uses_origin(fp["deliver"]):
                fp["origin_scope"] = self._origin_scope(sid, args.get("schedule"))
                if not fp["origin_scope"]:
                    return None
            return {"fp": fp, "self_id": ""}
        if action == "update":
            changes = any(args.get(k) is not None for k in ("prompt", "schedule", "deliver"))
            if not changes:
                return None
            existing = _resolve_job(_text(args.get("job_id")), jobs)
            if existing is None:
                return None  # let Hermes resolve/report the job; don't guess its effective fields
            prompt = args["prompt"] if args.get("prompt") is not None else existing.get("prompt")
            if not isinstance(prompt, str) or len(prompt) > MAX_FIELD or not prompt.strip():
                return None
            deliver = args["deliver"] if args.get("deliver") is not None else existing.get("deliver")
            schedule = args["schedule"] if args.get("schedule") not in (None, "") else existing.get("schedule_display")
            fp = core.make_fingerprint(prompt, core.normalize_deliver(deliver, "local"), schedule)
            if core.uses_origin(fp["deliver"]):
                origin = existing.get("origin")
                fp["origin_scope"] = _chat_scope(origin)
                if not fp["origin_scope"] or fp["origin_scope"] != self._origin_scope(sid, stored_origin=origin):
                    return None
            return {"fp": fp, "self_id": str(existing.get("id", ""))}
        return None

    def _refresh_jobs(self) -> None:
        if not self._jobs_lock.acquire(blocking=False):
            return
        try:
            deadline = time.monotonic() + SNAPSHOT_TTL
            jobs = tuple(islice(self._jobs_loader(), MAX_JOBS + 1))
            if len(jobs) > MAX_JOBS:
                raise ValueError("too many jobs for the snapshot")
            rows = []
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                row = {**job, "state": self._job_state(job)}
                prompt = _text(job.get("prompt"))
                row["fp"] = (core.make_fingerprint(prompt, core.normalize_deliver(job.get("deliver"), "local"),
                                                   job.get("schedule_display"),
                                                   origin_scope=_chat_scope(job.get("origin")))
                             if len(prompt) <= MAX_FIELD and prompt.strip() else None)
                rows.append(row)
            self._jobs_view = (deadline, tuple(rows))
        except Exception as exc:
            self._jobs_view = (0.0, ())
            log.warning("hermes-declines: cron snapshot unavailable (%s)", type(exc).__name__)
        finally:
            self._jobs_lock.release()

    def _refresh_declines(self, store: core.Store) -> None:
        deadline = time.monotonic() + SNAPSHOT_TTL
        try:
            rows = store.snapshot()
        except core.SnapshotTooLarge:
            if store.path != self._decline_store_path:
                self._decline_view = (0.0, ())
            log.warning("hermes-declines: decline store exceeds the snapshot limit; only a fresh cached view can apply")
            return  # Never extend a cached view's lifetime on a failed read.
        self._decline_store_path = store.path
        self._decline_view = (deadline, rows)

    def refresh(self, **kw: Any) -> None:
        """Off-policy I/O only, called by the bounded pre_llm_call hook."""
        self._settings = (self.threshold(), self.expire_days(), self.check_existing())
        if self._decline_lock.acquire(blocking=False):
            try:
                self._refresh_declines(self.store())
            except Exception as exc:
                self._decline_view = (0.0, ())
                log.warning("hermes-declines: decline snapshot unavailable (%s)", type(exc).__name__)
            finally:
                self._decline_lock.release()
        self._refresh_jobs()  # updates can need old fields even if duplicate vetoes are disabled

    def _save_decline(self, record, generation: int) -> None:
        try:
            with self._decline_lock:
                if generation != self._write_generation:
                    return  # a successful clear superseded this write, even if already dequeued
                store = self.store()
                store.add(**record, blocking=False, expire_days=self.expire_days())
                self._refresh_declines(store)
        except core.SnapshotTooLarge:
            log.warning("hermes-declines: denial not saved; decline history is full (use /declines forget or clear)")
        except Exception as exc:
            log.warning("hermes-declines: approval record or snapshot unavailable (%s)", type(exc).__name__)

    def _drain_writes(self) -> None:
        while True:
            with self._write_lock:
                if not self._writes:
                    self._writer_running = False
                    return
                context, record, generation = self._writes.popleft()
            context.run(self._save_decline, record, generation)

    def _submit_decline(self, **record: Any) -> None:
        with self._write_lock:  # memory only; never held by the worker during I/O
            generation = self._write_generation
            if self._background_writes:
                if len(self._writes) >= MAX_WRITES:
                    return
                self._writes.append((contextvars.copy_context(), record, generation))
                if not self._writer_running:
                    try:
                        threading.Thread(target=self._drain_writes, name="hermes-declines-writer", daemon=True).start()
                    except RuntimeError:
                        self._writes.pop()
                        raise
                    self._writer_running = True
                return
        self._save_decline(record, generation)

    # -- hooks ------------------------------------------------------------------
    def pre_tool_call(self, **kw: Any) -> Optional[Dict[str, Any]]:
        acquired = False
        try:
            if kw.get("tool_name") != TOOL:
                return None
            args = kw.get("args")
            if not isinstance(args, dict):
                return None
            action = _text(args.get("action"))
            if len(action) > MAX_FIELD:
                return None
            action = action.strip().lower()
            if action not in ("create", "update"):
                return None
            entry = self._reserve_pending(kw)
            acquired = self._policy_slot.acquire(blocking=False)
            if not acquired:
                return None
            deadline = time.monotonic() + POLICY_BUDGET
            for key in ("action", "prompt", "schedule", "deliver", "job_id"):
                value = args.get(key)
                if key == "deliver" and isinstance(value, (list, tuple)):
                    if len(value) > 32 or any(not isinstance(item, str) or len(item) > MAX_FIELD for item in value):
                        return None
                    if sum(map(len, value)) > MAX_FIELD:
                        return None
                elif value is not None and (not isinstance(value, str) or len(value) > MAX_FIELD):
                    return None
            jobs = _fresh(self._jobs_view)
            sid = _text(kw.get("session_id"))
            proposal = self._proposal(args, jobs, sid)
            if proposal is None:
                return None
            fp, self_id = proposal["fp"], proposal["self_id"]
            threshold, expire_days, check_existing = self._settings

            hit = core.find_match(_fresh(self._decline_view), fp, threshold, expire_days, self._clock(),
                                  deadline=deadline)
            if time.monotonic() >= deadline:
                return None
            if hit is not None:
                return {"action": "block", "message": (
                    "The user declined a matching job on %s. Do not propose it again; "
                    "if they want it, they will ask." % core.short_date(hit["ts"]))}

            if check_existing:
                dup_fp = fp
                if (not self_id and not core.normalize_deliver(args.get("deliver"), "") and sid
                        and self._session_env("HERMES_SESSION_ID") == sid
                        and not self._session_env("HERMES_SESSION_CHAT_ID")
                        and self._session_env("HERMES_SESSION_SOURCE") in ("cli", "tui", "desktop", "local")):
                    # Hermes stores an omitted local target as 'local'. Keep the historical decline
                    # scoped to the session, but compare the proposed output target to existing jobs.
                    dup_fp = {**fp, "deliver": "local", "origin_scope": ""}
                dup = self._existing_duplicate(dup_fp, threshold, self_id, jobs, deadline)
                if time.monotonic() >= deadline:
                    return None
                if dup is not None:
                    return {"action": "block", "message": (
                        "A matching job already exists (ID %s, %s); update or resume it instead of creating a new one."
                        % (dup[0], dup[1]))}

            if entry is not None and (not self_id or (args.get("prompt")
                    and core.normalize_deliver(args.get("deliver"), "") and not core.uses_origin(fp["deliver"]))):
                # Never persist a decline whose match fields came from a possibly stale job.
                self._publish_pending(entry, fp, action, kw)
        except Exception:  # no synchronous logging (and therefore no handler I/O) on this path
            pass
        finally:
            if acquired:
                self._policy_slot.release()
        return None

    def _existing_duplicate(self, fp: Dict[str, Any], threshold: float, self_id: str, jobs, deadline: float):
        for job in jobs:
            if time.monotonic() >= deadline:
                break
            if self_id and str(job.get("id", "")) == self_id:
                continue
            state = job["state"]
            if state in ("completed", "error"):
                continue  # only active or paused jobs count as duplicates
            jfp = job["fp"]
            if jfp is not None and core.fingerprints_match(jfp, fp, threshold):
                return (_text(job.get("id")) or "unknown", state)
        return None

    def post_tool_call(self, **kw: Any) -> None:
        try:
            if kw.get("tool_name") != TOOL:
                return
            self._forget_pending(kw)
            self._refresh_jobs()
        except Exception as exc:
            log.warning("hermes-declines post_tool_call: %s: %s", type(exc).__name__, exc)

    def post_approval_response(self, **kw: Any) -> None:
        try:
            if not core.is_user_deny(kw.get("choice")):
                return
            if kw.get("coalesced") or _text(kw.get("decided_by")) == "aux_llm":
                return  # follower of an already-recorded prompt, or a model's verdict rather than the user's
            action = self._schedule_action(kw)
            if not action:
                return  # the generic plugin gate doesn't emit the effective (possibly rewritten) arguments
            entry = self._forget_pending(kw, action=action)
            if entry is not None and entry["fp"] is not None:
                self._submit_decline(session_key=_text(kw.get("session_key")), surface=_text(kw.get("surface")),
                                     fingerprint=entry["fp"], now=self._clock())
        except Exception as exc:
            log.warning("hermes-declines post_approval_response: %s: %s", type(exc).__name__, exc)

    @staticmethod
    def _schedule_action(kw: Dict[str, Any]) -> str:
        kind = _text(kw.get("kind")).strip().lower()
        if kind and kind not in SCHEDULE_KINDS:
            return ""
        keys = [kw.get("pattern_key")]
        pks = kw.get("pattern_keys")
        if isinstance(pks, (list, tuple)):
            keys.extend(pks)
        structured = {_text(key).strip().lower() for key in keys}
        if any(key.startswith("plugin_rule:") for key in structured):
            return ""
        actions = {SCHEDULE_PATTERNS[key] for key in structured if key in SCHEDULE_PATTERNS}
        if kind:
            actions.add(SCHEDULE_KINDS[kind])
        return actions.pop() if len(actions) == 1 else ""

    # -- /declines --------------------------------------------------------------
    def command(self, raw_args: str = "") -> str:
        try:
            parts = (raw_args or "").split()
            sub = parts[0].lower() if parts else "list"
            store = self.store()
            if sub == "list":
                days = self.expire_days()
                rows = store.active(days, self._clock())
                if not rows:
                    return "No active declines."
                lines = ["Active declines (expire after %g days):" % days]
                for r in sorted(rows, key=lambda r: r["ts"]):
                    left = max(0.0, r["ts"] + days * core.DAY - self._clock()) / core.DAY
                    lines.append("%s  %s  %s  [%s, %.0f d left]" % (
                        r["id"], core.short_date(r["ts"]), _short(r["fingerprint"].get("text", "")),
                        r["fingerprint"].get("deliver", ""), left))
                return "\n".join(lines)
            if sub == "forget":
                if len(parts) != 2:
                    return "Usage: /declines forget <id>"
                with self._decline_lock:
                    rec = store.forget(parts[1])
                    if rec and store.path == self._decline_store_path:
                        deadline, rows = self._decline_view
                        self._decline_view = (deadline, tuple(row for row in rows if row.get("id") != rec["id"]))
                    self._refresh_declines(store)
                return ("Forgot %s. Matching decline removed." % rec["id"]) if rec \
                    else "No single decline matches '%s'. Use /declines list." % parts[1]
            if sub == "clear":
                with self._decline_lock:
                    count = store.clear()
                    with self._write_lock:
                        self._write_generation += 1
                        self._writes.clear()
                    self._refresh_declines(store)
                return "Cleared %d decline(s)." % count
            return "Usage: /declines list | forget <id> | clear"
        except Exception as exc:
            log.warning("hermes-declines /declines: %s: %s", type(exc).__name__, exc)
            return "hermes-declines could not run that command (see the Hermes log)."


def register(ctx) -> None:
    def get_config(key, default=None):
        getter = getattr(ctx, "get_config", None)
        return getter(key, default) if callable(getter) else default

    plugin = Declines(get_config)
    ctx.register_hook("pre_llm_call", plugin.refresh)
    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook("post_tool_call", plugin.post_tool_call)
    ctx.register_hook("post_approval_response", plugin.post_approval_response)
    ctx.register_command("declines", plugin.command,
                         description="List, forget or clear declined scheduled jobs",
                         args_hint="list | forget <id> | clear")
