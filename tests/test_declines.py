"""Tests for hermes-declines. Run: python3 -m unittest discover -s tests

Stdlib only, no network, no Hermes install. Temp dirs live under the Hermes scratch
directory and paths are compared through os.path.realpath.
"""
import fcntl
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import _load  # noqa: E402

plugin, core = _load.load()

NOW = 1_800_000_000.0  # fixed clock
DAY = 86400.0

PROMPT = "Remind me to renew my passport and check the visa rules for Japan"
PROMPT_DATED = "Remind me to renew my passport, and check the visa rules for Japan on Friday 12 June 2026 at 9:30 AM!"
OTHER = "Summarize unread Slack messages from the engineering channel"


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        _load.SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="declines-test-", dir=str(_load.SCRATCH))
        self.state = os.path.join(self.tmp, "state")
        self.cfg = {"state_dir": self.state}  # type: dict
        self.jobs = []
        self.clock = Clock()
        self.d = plugin.Declines(lambda k, default=None: self.cfg.get(k, default),
                                 jobs_loader=lambda: list(self.jobs), clock=self.clock, background_writes=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def create(self, prompt=PROMPT, deliver=None, schedule="every monday 9am", call="c1", sid="s1", **extra):
        args = {"action": "create", "prompt": prompt, "schedule": schedule}
        if deliver is not None:
            args["deliver"] = deliver
        args.update(extra)
        self.d.refresh()
        return self.d.pre_tool_call(tool_name="cronjob_manage", args=args, tool_call_id=call, session_id=sid)

    def deny(self, **kw):
        base = dict(choice="deny", kind="schedule", pattern_key="schedule", session_key="sk1",
                    session_id="s1", tool_call_id="c1", surface="cli")
        base.update(kw)
        return self.d.post_approval_response(**base)

    def records(self):
        return core.Store(self.state).all()

    def decline(self, prompt=PROMPT, **kw):
        self.assertIsNone(self.create(prompt, **kw))
        self.deny()
        self.assertEqual(len(self.records()), 1)


class TestFingerprint(unittest.TestCase):
    def test_date_time_variants_of_same_job_match(self):
        a = core.make_fingerprint(PROMPT, "local", "every monday 9am")
        b = core.make_fingerprint(PROMPT_DATED, "local", "2026-06-12T09:30:00")
        self.assertEqual(a["text"], "remind me to renew my passport and check the visa rules for japan")
        self.assertEqual(a["text"], b["text"])
        self.assertTrue(core.fingerprints_match(a, b, 0.6))

    def test_more_variants(self):
        base = core.make_fingerprint("Call the dentist about the invoice", "telegram:1", None)
        for variant in ("call the dentist about the invoice on 6/12/2026",
                        "Call the dentist about the invoice tomorrow at 3pm",
                        "CALL the dentist, about the invoice (Thursday June 5th, 14:00)",
                        "Call the dentist about invoice 2026-06-12T09:00:00"):
            fp = core.make_fingerprint(variant, "telegram:1", None)
            self.assertTrue(core.fingerprints_match(base, fp, 0.6), variant)

    def test_different_jobs_do_not_match(self):
        a = core.make_fingerprint(PROMPT, None, None)
        b = core.make_fingerprint(OTHER, None, None)
        self.assertFalse(core.fingerprints_match(a, b, 0.6))
        self.assertLess(core.jaccard(a["text"], b["text"]), 0.2)

    def test_different_deliver_target_does_not_match(self):
        a = core.make_fingerprint(PROMPT, "telegram:1", None)
        b = core.make_fingerprint(PROMPT, "discord:2", None)
        self.assertFalse(core.fingerprints_match(a, b, 0.6))
        self.assertTrue(core.fingerprints_match(a, core.make_fingerprint(PROMPT, " Telegram:1 ", None), 0.6))

    def test_omitted_deliver_is_origin(self):
        self.assertEqual(core.normalize_deliver(None), "origin")
        self.assertEqual(core.normalize_deliver(""), "origin")

    def test_raw_prompt_not_stored_but_hashed(self):
        fp = core.make_fingerprint(PROMPT_DATED, None, "0 9 * * *")
        self.assertEqual(len(fp["sha256"]), 64)
        self.assertNotIn("Friday", str(fp))
        self.assertNotIn("9:30", str(fp))
        self.assertEqual(fp["schedule_kind"], "cron")

    def test_schedule_kinds(self):
        kinds = {"every 2h": "interval", "30m": "interval", "in 30m": "oneshot", "2026-06-01T09:00:00": "oneshot",
                 "every monday 9am": "natural", "weekdays at 9am": "natural", "0 9 * * *": "cron", None: "unknown"}
        for s, k in kinds.items():
            self.assertEqual(core.schedule_kind(s), k, s)

    def test_threshold_is_configurable(self):
        a = core.make_fingerprint("renew passport and check visa rules", "local", None)
        b = core.make_fingerprint("renew passport and book flights", "local", None)
        self.assertTrue(core.fingerprints_match(a, b, 0.3))
        self.assertFalse(core.fingerprints_match(a, b, 0.9))

    def test_non_string_inputs(self):
        self.assertEqual(core.normalize_prompt(None), "")
        self.assertEqual(core.normalize_prompt(42), "")
        empty = core.make_fingerprint("12:30 2026-01-01", None, None)
        self.assertEqual(empty["text"], "")
        self.assertFalse(core.fingerprints_match(empty, core.make_fingerprint("17", None, None), 0.6))


class TestRecording(Base):
    def test_interleaved_approval_correlates_only_verified_proposals(self):
        self.assertIsNone(self.create(call="a"))
        self.assertIsNone(self.create(OTHER, call="b"))
        # Structured fields exposed by the host's approval emitter. The text is
        # display data, not a correlation key. No live approval UI is required.
        event = dict(choice="deny", pattern_key="schedule",
                     pattern_keys=["schedule"], session_key="sk1",
                     session_id="s1", turn_id="t1", surface="gateway",
                     command="cronjob_manage", description="Schedule this job?")
        for call_id in ("unknown", ""):
            with self.subTest(call_id=call_id):
                self.d.post_approval_response(**event, tool_call_id=call_id)
                self.assertEqual(self.records(), [])
        self.d.post_tool_call(tool_name="cronjob_manage", tool_call_id="a", session_id="s1")
        misleading = {**event, "pattern_key": "unrelated", "pattern_keys": []}
        self.d.post_approval_response(**misleading, tool_call_id="")
        self.d.post_approval_response(**event, tool_call_id="a")
        self.assertEqual(self.records(), [])
        self.d.post_approval_response(**event, tool_call_id="")  # could be a late denial for A
        self.assertEqual(self.records(), [])
        self.d.post_approval_response(**event, tool_call_id="b")
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()], [core.normalize_prompt(OTHER)])

    def test_unfingerprintable_parallel_proposal_prevents_idless_attribution(self):
        self.assertIsNone(self.create(call="a"))
        self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1",
                          tool_call_id="b", args={"action": "create", "skills": ["example"], "schedule": "30m"}))
        self.deny(tool_call_id="")
        self.assertEqual(self.records(), [])

    def test_generic_plugin_approval_does_not_claim_rewritten_arguments(self):
        self.assertIsNone(self.create(call="a"))
        self.deny(kind="", pattern_key="plugin_rule:cronjob_manage", tool_call_id="a")
        self.assertEqual(self.records(), [])

    def test_schedule_only_denial_never_persists_inherited_prompt(self):
        self.jobs = [{"id": "j1", "prompt": PROMPT, "deliver": "local", "state": "scheduled"}]
        self.d.refresh()
        self.jobs = [{**self.jobs[0], "prompt": OTHER}]
        self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1",
                          tool_call_id="c1", args={"action": "update", "job_id": "j1", "schedule": "30m"}))
        self.deny(kind="schedule_edit", pattern_key="schedule_edit")
        self.assertEqual(self.records(), [])

    def test_schedule_deny_is_recorded_with_fields(self):
        self.decline()
        rec = self.records()[0]
        self.assertEqual(rec["session_key"], "sk1")
        self.assertEqual(rec["surface"], "cli")
        self.assertEqual(rec["ts"], NOW)
        self.assertTrue(rec["id"])
        self.assertEqual(rec["fingerprint"]["text"], core.normalize_prompt(PROMPT))
        self.assertEqual(rec["fingerprint"]["sha256"], core.make_fingerprint(PROMPT, None, None)["sha256"])
        self.assertNotIn("prompt", rec)
        self.assertNotIn(PROMPT, repr(rec))

    def test_denial_needs_exact_call_id_and_matching_schedule_action(self):
        event = dict(choice="deny", kind="schedule", surface="gateway", session_id="s1",
                     session_key="sk1", turn_id="t1")
        context = dict(tool_name="cronjob_manage", session_id="s1", turn_id="t1")
        for ident, prompt in (("a", PROMPT), ("b", OTHER)):
            self.assertIsNone(self.d.pre_tool_call(**context, tool_call_id=ident,
                              args={"action": "create", "prompt": prompt, "schedule": "30m"}))
            if ident == "a":
                self.d.post_tool_call(**context, tool_call_id=ident)
        for bad in ({}, {"tool_call_id": ""}, {"tool_call_id": "a"},
                    {"tool_call_id": "b", "kind": "schedule_edit"},
                    {"kind": "schedule_edit"}, {"tool_call_id": "b", "pattern_key": "schedule_edit"}):
            with self.subTest(bad=bad):
                self.d.post_approval_response(**{**event, **bad})
                self.assertEqual(self.records(), [])
        self.d.post_approval_response(**event, tool_call_id="b")
        self.d.post_approval_response(**event, tool_call_id="b")  # replay cannot write again
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()], [core.normalize_prompt(OTHER)])

    def test_matching_by_tool_call_id_alone(self):
        self.assertIsNone(self.create(call="c9", sid="sA"))
        self.d.post_approval_response(choice="denied", tool_call_id="c9", surface="tui", kind="schedule")
        self.assertEqual(len(self.records()), 1)

    def test_replayed_native_denial_cannot_bind_a_reused_call_id(self):
        event = dict(choice="deny", pattern_key="cron_create", session_id="s1", tool_call_id="reused")
        for turn, prompt in (("A", PROMPT), ("B", OTHER)):
            self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1", turn_id=turn,
                              tool_call_id="reused", args={"action": "create", "prompt": prompt}))
            self.d.post_approval_response(**event)
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()], [core.normalize_prompt(PROMPT)])
        self.assertIsNone(self.create(OTHER, call="unique"))
        self.d.post_approval_response(**{**event, "tool_call_id": "unique"})
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()],
                         [core.normalize_prompt(PROMPT), core.normalize_prompt(OTHER)])

    def test_busy_policy_cannot_attribute_native_denial_to_reused_identity(self):
        context = dict(tool_name="cronjob_manage", session_id="s1", turn_id="turn")
        event = dict(pattern_key="cron_create", pattern_keys=["cron_create"], session_key="default",
                     surface="cli", choice="deny", session_id="s1")
        for name, slot in (("comparison", self.d._policy_slot), ("identity", self.d._pending_lock)):
            with self.subTest(busy=name):
                self.d.command("clear")
                args = dict(action="create", prompt=PROMPT, deliver="local", schedule="30m")
                call = {**context, "tool_call_id": name}
                self.assertIsNone(self.d.pre_tool_call(**call, args=args))

                def submit_reused_id():
                    self.d.pre_tool_call(**call, args={**args, "prompt": OTHER})
                    returned.set()

                # Hold admission or bookkeeping while B arrives; the hook must not wait on either.
                returned = threading.Event()
                worker = threading.Thread(target=submit_reused_id, daemon=True)
                with slot:
                    worker.start()
                    timely = returned.wait(0.2)
                worker.join(1)
                self.assertTrue(timely, "policy hook waited for a busy lock")
                self.d.post_approval_response(**event, tool_call_id=name)
                self.assertEqual(self.records(), [], "a denial for B must not record A")
                self.assertIsNone(self.create(deliver="local", call="next-" + name))
        self.d.command("clear")
        # Use an unrelated ID with a distinct suppression slot for the positive control.
        unique = next(f"unique-{i}" for i in range(1000)
                      if hash(f"unique-{i}") % plugin.PENDING_MAX != hash("identity") % plugin.PENDING_MAX)
        self.assertIsNone(self.create(OTHER, deliver="local", call=unique))
        self.d.post_approval_response(**event, tool_call_id=unique)
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()], [core.normalize_prompt(OTHER)])

    def test_native_cli_schedule_pattern_keys_record_create_and_edit_without_kind(self):
        self.jobs = [{"id": "j1", "prompt": OTHER, "deliver": "local"}]
        self.d.refresh()
        for action, pattern, kind, prompt in (("create", "cron_create", "schedule", PROMPT),
                                               ("update", "cron_update", "schedule_edit", OTHER)):
            for explicit in (False, True):
                with self.subTest(action=action, explicit_kind=explicit):
                    call = f"{action}-{explicit}"
                    args = dict(action=action, prompt=prompt, schedule="30m", deliver="local")
                    if action == "update":
                        args["job_id"] = "j1"
                    self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", args=args,
                                      tool_call_id=call, session_id="s1"))
                    event = dict(pattern_key=pattern, pattern_keys=[pattern], session_key="default",
                                 surface="cli", choice="deny", tool_call_id=call, session_id="s1")
                    if explicit:
                        event["kind"] = kind
                    self.d.post_approval_response(**{**event, "pattern_keys": [pattern, "plugin_rule:cron"]})
                    self.assertEqual(self.records(), [])
                    self.d.post_approval_response(**event)
                    self.assertEqual([(r["session_key"], r["surface"], r["fingerprint"]["text"])
                                      for r in self.records()], [("default", "cli", core.normalize_prompt(prompt))])
                    self.d.command("clear")

    def test_non_schedule_approval_is_not_recorded(self):
        self.assertIsNone(self.create())
        self.deny(kind="", pattern_key="recursive delete", tool_call_id="other",
                  command="rm -rf /tmp/x", description="dangerous command")
        self.deny(kind="command", pattern_key="terminal", tool_call_id="")
        self.assertEqual(self.records(), [])

    def test_allow_is_not_recorded(self):
        self.assertIsNone(self.create())
        for choice in ("once", "session", "always", "approve"):
            self.deny(choice=choice)
        self.assertEqual(self.records(), [])

    def test_non_user_outcomes_are_not_recorded(self):
        self.assertIsNone(self.create())
        for choice in ("timeout", "cancelled", "smart_deny", "notify_failed", "transport_error", None, 7):
            self.deny(choice=choice)
        self.deny(decided_by="aux_llm")
        self.deny(coalesced=True)
        self.assertEqual(self.records(), [])

    def test_deny_without_known_proposal_records_nothing(self):
        self.deny()
        self.assertEqual(self.records(), [])

    def test_vetoed_proposal_is_not_pending(self):
        self.decline()
        self.assertIsNotNone(self.create(call="c2"))
        self.deny(tool_call_id="c2")
        self.assertEqual(len(self.records()), 1)

    def test_post_tool_call_clears_pending(self):
        self.assertIsNone(self.create())
        self.d.post_tool_call(tool_name="cronjob_manage", tool_call_id="c1", session_id="s1")
        self.deny()
        self.assertEqual(self.records(), [])

    def test_update_proposal_is_recorded(self):
        self.jobs = [{"id": "j1", "name": "old", "prompt": OTHER, "deliver": "local", "state": "scheduled",
                      "enabled": True, "schedule_display": "every 2h"}]
        self.d.refresh()
        res = self.d.pre_tool_call(tool_name="cronjob_manage", tool_call_id="c1", session_id="s1",
                                   args={"action": "update", "job_id": "j1", "prompt": PROMPT, "deliver": "local"})
        self.assertIsNone(res)
        self.d.post_approval_response(choice="deny", kind="schedule_edit", tool_call_id="c1", session_id="s1")
        self.assertEqual(self.records()[0]["fingerprint"]["deliver"], "local")
        self.assertEqual(self.records()[0]["fingerprint"]["text"], core.normalize_prompt(PROMPT))


class TestVeto(Base):
    def test_implicit_delivery_is_chat_scoped_and_veto_hides_old_text(self):
        env = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_ID": "100",
               "HERMES_SESSION_THREAD_ID": "main", "HERMES_SESSION_ID": "s1"}
        self.d._session_env = env.get
        self.decline()
        with self.subTest("another chat"):
            env.update(HERMES_SESSION_CHAT_ID="200", HERMES_SESSION_ID="s2")
            self.assertIsNone(self.create(PROMPT, sid="s2", call="c2"))
        with self.subTest("same chat after a session reset"):
            env.update(HERMES_SESSION_CHAT_ID="100", HERMES_SESSION_ID="s3")
            result = self.create(PROMPT_DATED, sid="s3", call="c3")
            self.assertEqual(result["action"], "block")
            self.assertNotIn("passport", result["message"])
        self.clock.t += 31 * DAY
        self.jobs = [{"id": "j1", "name": PROMPT, "prompt": PROMPT, "state": "paused", "enabled": False,
                      "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "200", "thread_id": "main"}}]
        with self.subTest("implicit delivery is not a wildcard"):
            self.assertIsNone(self.create(PROMPT, sid="s3", call="c4"))
            env.update(HERMES_SESSION_CHAT_ID="200", HERMES_SESSION_ID="s2")
            duplicate = self.create(PROMPT, sid="s2", call="c5")
            self.assertEqual(duplicate["action"], "block")
            self.assertNotIn("passport", duplicate["message"])
        self.assertEqual(len(self.records()), 1)  # expiry does not delete the record

    def test_veto_fires_on_matching_create_with_message(self):
        self.decline()
        res = self.create(PROMPT_DATED, schedule="2026-07-01T10:00:00", call="c2")
        self.assertEqual(res["action"], "block")
        self.assertEqual(res["message"], (
            "The user declined a matching job on %s. Do not propose it again; "
            "if they want it, they will ask." % core.short_date(NOW)))

    def test_different_job_is_not_vetoed(self):
        self.decline()
        self.assertIsNone(self.create(OTHER, call="c2"))

    def test_other_deliver_target_is_not_vetoed(self):
        self.decline()
        self.assertIsNone(self.create(PROMPT, deliver="telegram:5", call="c2"))

    def test_veto_on_matching_update(self):
        self.decline(deliver="local")
        self.jobs = [{"id": "j1", "name": "x", "prompt": OTHER, "deliver": "local", "state": "scheduled",
                      "enabled": True, "schedule_display": "daily"}]
        self.d.refresh()
        res = self.d.pre_tool_call(tool_name="cronjob_manage", tool_call_id="c3", session_id="s1",
                                   args={"action": "update", "job_id": "j1", "prompt": PROMPT_DATED})
        self.assertEqual(res["action"], "block")
        self.assertIn("declined a matching job", res["message"])

    def test_non_creating_actions_and_other_tools_untouched(self):
        self.decline()
        for args in ({"action": "list"}, {"action": "pause", "job_id": "j1"}, {"action": "remove", "job_id": "j1"},
                     {"action": "run", "job_id": "j1", "prompt": PROMPT}, {"action": "update", "job_id": "j1", "name": "n"},
                     {"action": "create", "schedule": "30m"}):
            self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", args=args), args)
        self.assertIsNone(self.d.pre_tool_call(tool_name="terminal", args={"action": "create", "prompt": PROMPT}))

    def test_no_veto_after_forget(self):
        self.decline()
        rec_id = self.records()[0]["id"]
        self.assertIsNotNone(self.create(call="c2"))
        self.assertIn("Forgot", self.d.command("forget " + rec_id))
        self.assertIsNone(self.create(call="c3"))

    def test_no_veto_after_clear(self):
        self.decline()
        self.assertIn("Cleared 1", self.d.command("clear"))
        self.assertIsNone(self.create(call="c3"))

    def test_no_veto_after_expiry_and_veto_before(self):
        self.decline()
        self.clock.t = NOW + 29.9 * DAY
        self.assertIsNotNone(self.create(call="c2"))
        self.clock.t = NOW + 30.1 * DAY
        self.assertIsNone(self.create(call="c3"))

    def test_expire_days_configurable(self):
        self.cfg["expire_days"] = 2
        self.decline()
        self.clock.t = NOW + 1 * DAY
        self.assertIsNotNone(self.create(call="c2"))
        self.clock.t = NOW + 3 * DAY
        self.assertIsNone(self.create(call="c3"))

    def test_threshold_configurable_in_hook(self):
        self.decline("renew passport and check visa rules for japan trip")
        loose = "renew passport and book flights for japan trip"
        self.cfg["threshold"] = 0.95
        self.assertIsNone(self.create(loose, call="c2"))
        self.cfg["threshold"] = 0.3
        self.assertIsNotNone(self.create(loose, call="c3"))

    def test_bad_config_values_fall_back_to_defaults(self):
        self.cfg.update(threshold="lots", expire_days=-4, check_existing_jobs="maybe")
        self.assertEqual(self.d.threshold(), 0.6)
        self.assertEqual(self.d.expire_days(), 30)
        self.assertTrue(self.d.check_existing())


class TestExistingJobs(Base):
    def job(self, **kw):
        j = {"id": "j1", "name": "Passport watch", "prompt": PROMPT, "deliver": "telegram:1",
             "state": "scheduled", "enabled": True, "schedule_display": "every monday 9am"}
        j.update(kw)
        return j

    def test_paused_near_duplicate_is_vetoed_with_resume_message(self):
        self.jobs = [self.job(state="paused", enabled=False)]
        res = self.create(PROMPT_DATED, deliver="telegram:1")
        self.assertEqual(res, {"action": "block", "message": (
            "A matching job already exists (ID j1, paused); update or resume it instead of creating a new one.")})
        # and the paused job is never touched by the plugin
        self.assertEqual(self.jobs[0]["state"], "paused")

    def test_active_near_duplicate_is_vetoed(self):
        self.jobs = [self.job()]
        res = self.create(PROMPT, deliver="telegram:1")
        self.assertIn("ID j1, scheduled", res["message"])

    def test_completed_or_errored_jobs_are_not_duplicates(self):
        for state in ("completed", "error"):
            self.jobs = [self.job(state=state, enabled=False)]
            self.assertIsNone(self.create(PROMPT, deliver="telegram:1", call="c" + state), state)

    def test_different_job_or_target_is_not_duplicate(self):
        self.jobs = [self.job()]
        self.assertIsNone(self.create(OTHER, deliver="telegram:1", call="a"))
        self.assertIsNone(self.create(PROMPT, deliver="discord:9", call="b"))

    def test_delivery_defaults_follow_local_and_recurring_slack_routes(self):
        env = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_SOURCE": "cli"}
        self.d._session_env = env.get
        with self.subTest("omitted CLI delivery matches canonical local job"):
            self.jobs = [self.job(deliver="local")]
            self.assertIn("ID j1", (self.create() or {}).get("message", ""))
        with self.subTest("clearing an update target is not origin"):
            self.jobs = [self.job(deliver="origin", origin={"platform": "telegram", "chat_id": "100"}),
                         self.job(id="j2", prompt=OTHER, deliver="origin",
                                  origin={"platform": "telegram", "chat_id": "100"})]
            env.update(HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="100")
            self.d.refresh()
            res = self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1", tool_call_id="update",
                                      args={"action": "update", "job_id": "j2", "deliver": "", "prompt": PROMPT})
            self.assertIsNone(res)
        with self.subTest("recurring Slack delivery ignores the creating message's synthetic thread"):
            env.update(HERMES_SESSION_PLATFORM="slack", HERMES_SESSION_THREAD_ID="456",
                       HERMES_SESSION_MESSAGE_ID="456")
            self.jobs = [self.job(deliver="origin", origin={"platform": "slack", "chat_id": "100"})]
            self.assertIn("ID j1", (self.create(schedule="0 9 * * *") or {}).get("message", ""))

    def test_slack_one_shot_matches_only_the_host_delivery_route(self):
        origin = {"platform": "slack", "chat_id": "channel", "scope_id": "team", "user_id": "member"}
        env = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_THREAD_ID": "message",
               "HERMES_SESSION_MESSAGE_ID": "message",
               **{"HERMES_SESSION_" + key.upper(): value for key, value in origin.items()}}
        self.d._session_env = env.get
        for schedule, target, expected in (("in 2h", "", "channel"),
                                           ("in 30m", "message", "thread"),
                                           ("in 1 hour", "message", "thread"),
                                           ("in 61 mins", "", "channel"),
                                           ("in ٠٦١m", "", "channel")):
            with self.subTest(schedule=schedule):
                self.jobs = [self.job(id="wrong", deliver="origin",
                                      origin={**origin, "thread_id": "message" if not target else ""}),
                             self.job(id=expected, deliver="origin", origin={**origin, "thread_id": target})]
                result = self.create(schedule=schedule, call=schedule)
                self.assertIn(f"ID {expected}", (result or {}).get("message", ""))
        self.jobs = [self.job(deliver="origin", origin={**origin, "thread_id": "message"})]
        self.assertIsNone(self.create(schedule="2026-06-01T09:00:00", call="unknown-origin"))
        env["HERMES_SESSION_MESSAGE_ID"] = "reply"  # a real thread does not use the synthetic drop rule
        self.assertIn("ID j1", self.create(schedule="in 2h", call="real-thread")["message"])

    def test_slack_recurring_denial_matches_only_the_host_route(self):
        origin = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_PLATFORM": "slack",
                  "HERMES_SESSION_CHAT_ID": "channel", "HERMES_SESSION_SCOPE_ID": "team"}
        env = dict(origin)
        self.d._session_env = env.get
        schedules = ("every 0000001h", "every ٠٠٠٠٠٠١h", "every " + "0" * 640 + "1m", "every 0m", "0m",
                     "every Monday at 9am", "weekdays at 9am", "every Mon, Wed and Fri at 9:30 pm",
                     "0 9 * JAN,MAR MON-FRI", "0 9 * * MON-FRI 15")
        for i, schedule in enumerate(schedules):
            with self.subTest(schedule=schedule):
                self.d.command("clear")
                env.update(HERMES_SESSION_THREAD_ID="top", HERMES_SESSION_MESSAGE_ID="top")
                call = f"original-{i}"
                self.assertIsNone(self.create(schedule=schedule, call=call))
                self.d.post_approval_response(choice="deny", pattern_key="cron_create", pattern_keys=["cron_create"],
                                              session_id="s1", tool_call_id=call, surface="gateway")
                self.assertEqual([r["fingerprint"]["origin_scope"] for r in self.records()],
                                 [core.scope_digest("chat", "slack", "channel", "team", "", "")])
                env.update(HERMES_SESSION_THREAD_ID="another", HERMES_SESSION_MESSAGE_ID="another")
                self.assertIn("declined", (self.create(schedule=schedule, call=f"repeat-{i}") or {}).get("message", ""))
                env["HERMES_SESSION_MESSAGE_ID"] = "reply"  # recurring jobs from real threads stay there
                self.assertIsNone(self.create(schedule=schedule, call=f"real-thread-{i}"))
                env["HERMES_SESSION_MESSAGE_ID"] = "another"
                self.assertIsNone(self.create(schedule="in 30m", call=f"near-once-{i}"))

    def test_slack_cron_lists_last_weekday_and_padded_fields_keep_channel_declines(self):
        env = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_PLATFORM": "slack",
               "HERMES_SESSION_CHAT_ID": "channel", "HERMES_SESSION_SCOPE_ID": "team"}
        self.d._session_env = env.get
        schedules = (",".join(map(str, range(60))) + " 9 * * *", "0 9 * * L5", "0000 9 * * *")
        for i, schedule in enumerate(schedules):
            with self.subTest(schedule=schedule):
                self.d.command("clear")
                env.update(HERMES_SESSION_THREAD_ID="top", HERMES_SESSION_MESSAGE_ID="top")
                call = f"cron-{i}"
                self.assertIsNone(self.create(schedule=schedule, call=call))
                self.d.post_approval_response(choice="deny", pattern_key="cron_create",
                                              session_id="s1", tool_call_id=call, surface="gateway")
                self.assertEqual([r["fingerprint"]["origin_scope"] for r in self.records()],
                                 [core.scope_digest("chat", "slack", "channel", "team", "", "")])
                env.update(HERMES_SESSION_THREAD_ID="next", HERMES_SESSION_MESSAGE_ID="next")
                self.assertIn("declined", (self.create(schedule=schedule, call=f"repeat-{i}") or {}).get("message", ""))
                env["HERMES_SESSION_MESSAGE_ID"] = "reply"
                self.assertIsNone(self.create(schedule=schedule, call=f"real-thread-{i}"))

    def test_slack_edit_matches_the_stored_origin_not_the_display_schedule(self):
        origin = {"platform": "slack", "chat_id": "channel", "thread_id": "top"}
        env = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_MESSAGE_ID": "top",
               **{"HERMES_SESSION_" + key.upper(): value for key, value in origin.items()}}
        self.d._session_env = env.get
        self.jobs = [self.job(id="existing", deliver="origin", origin=origin),
                     self.job(id="edited", prompt=OTHER, deliver="origin", origin=origin,
                              schedule_display="every 2h")]
        self.d.refresh()
        kw = dict(tool_name="cronjob_manage", session_id="s1",
                  args={"action": "update", "job_id": "edited", "prompt": PROMPT})
        result = self.d.pre_tool_call(**kw, tool_call_id="same")
        self.assertIn("ID existing", (result or {}).get("message", ""))
        env.update(HERMES_SESSION_THREAD_ID="different", HERMES_SESSION_MESSAGE_ID="different")
        self.assertIsNone(self.d.pre_tool_call(**kw, tool_call_id="wrong-thread"))

    def test_unverified_slack_schedules_abstain_without_burning_the_host_timeout(self):
        origin = {"platform": "slack", "chat_id": "channel"}
        env = {"HERMES_SESSION_ID": "s1", "HERMES_SESSION_THREAD_ID": "top", "HERMES_SESSION_MESSAGE_ID": "top",
               **{"HERMES_SESSION_" + key.upper(): value for key, value in origin.items()}}
        self.d._session_env = env.get
        self.jobs = [self.job(deliver="origin", origin=origin)]
        self.d.refresh()
        with self.subTest("malformed relative schedule stays within the dispatcher test timeout"):
            args = {"action": "create", "prompt": PROMPT, "schedule": "in" + " " * (plugin.MAX_FIELD - 3) + "z"}
            duration = []
            for n in range(3):
                start = time.monotonic()
                result = self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1", args=args,
                                              tool_call_id=f"malformed-{n}")
                duration.append(time.monotonic() - start)
                self.assertIsNone(result)
            self.assertLess(min(duration), 0.2)
        with self.subTest("non-schedule text is not a verified channel route"):
            for i, schedule in enumerate(("weekly maybe Tuesday", "every Monday at 99am", "0 99 * * * 0",
                                          "2026-06-01T09:00:00", "in 0m", "0 9 ? * MON",
                                          "L5 9 * * *", "0 9 * * L8", "0 9 * * L5,MON", "0060 9 * * *")):
                self.assertIsNone(self.create(schedule=schedule, call=f"unverified-{i}"), schedule)

    def test_omitted_deliver_does_not_match_unrelated_explicit_target(self):
        self.jobs = [self.job()]
        self.assertIsNone(self.create(PROMPT, call="a"))

    def test_updating_the_matching_job_itself_is_allowed(self):
        self.jobs = [self.job()]
        self.d.refresh()
        res = self.d.pre_tool_call(tool_name="cronjob_manage", tool_call_id="c", session_id="s",
                                   args={"action": "update", "job_id": "j1", "schedule": "every tuesday 9am"})
        self.assertIsNone(res)

    def test_update_resolution_matches_host_and_never_guesses_missing_fields(self):
        own = self.job()
        other = self.job(id="j2", name="j1", prompt=OTHER)
        cases = (
            ("PASSPORT WATCH", [own], {"prompt": PROMPT, "deliver": "telegram:1"}),
            ("j1", [other, own], {"prompt": PROMPT, "deliver": "telegram:1"}),
            ("Passport watch", [own, self.job(id="j2", name="passport watch")], {"schedule": "daily"}),
            ("missing", [own], {"prompt": PROMPT, "deliver": "telegram:1"}),
        )
        for ref, rows, changes in cases:
            with self.subTest(ref=ref):
                self.jobs = rows
                self.d.refresh()
                self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1",
                                  args={"action": "update", "job_id": ref, **changes}))

    def test_update_into_another_existing_job_is_vetoed(self):
        self.jobs = [self.job(), self.job(id="j2", name="Other", prompt=OTHER)]
        self.d.refresh()
        res = self.d.pre_tool_call(tool_name="cronjob_manage", tool_call_id="c", session_id="s",
                                   args={"action": "update", "job_id": "j2", "prompt": PROMPT_DATED, "deliver": "telegram:1"})
        self.assertIn("ID j1", res["message"])

    def test_check_existing_jobs_can_be_turned_off(self):
        self.cfg["check_existing_jobs"] = False
        self.jobs = [self.job(state="paused", enabled=False)]
        self.assertIsNone(self.create(PROMPT, deliver="telegram:1"))

    def test_cron_snapshot_is_read_only_bounded_and_independent_of_declines(self):
        home = Path(self.tmp) / "home"
        cron = home / "cron"
        cron.mkdir(parents=True)
        path = cron / "jobs.json"
        prefix = "weather " * 16
        full = prefix + "forecast storm rainfall wind coast warnings"
        other = prefix + "stock price revenue guidance dividend market"
        job = self.job(prompt=full, state="paused", enabled=False)
        valid = json.dumps({"jobs": [job]}).encode()
        oversized = json.dumps({"jobs": [{**job, "metadata": "x" * (256 * 1024)}]}).encode()
        path.write_bytes(valid)
        self.d._jobs_loader = plugin._real_jobs
        private = types.SimpleNamespace(
            list_jobs=mock.Mock(side_effect=lambda **kw: json.loads(path.read_bytes())["jobs"]),
            effective_job_state=mock.Mock(return_value="paused"),
        )
        with mock.patch.object(core, "hermes_home", return_value=home), mock.patch.dict(sys.modules, {"cron.jobs": private}):
            with self.subTest("uses full prompt without importing private reader"):
                self.assertIsNone(self.create(other, deliver="telegram:1", call="other"))
                res = self.create(full, deliver="telegram:1", call="full")
                self.assertEqual(res and res["action"], "block")
                self.assertEqual(path.read_bytes(), valid)
                self.assertEqual(list(cron.iterdir()), [path])
            self.decline(deliver="local")
            for bad in (b"{not json", oversized, json.dumps([job]).encode()):
                with self.subTest(bytes=len(bad)):
                    path.write_bytes(bad)
                    self.assertIsNone(self.create(full, deliver="telegram:1", call="degraded"))
                    self.assertEqual(self.create(deliver="local", call="repeated")["action"], "block")
                    self.assertIsNone(self.d.pre_tool_call(tool_name="cronjob_manage", session_id="s1",
                                      args={"action": "update", "job_id": "j1", "schedule": "30m"}))
                    self.assertEqual(path.read_bytes(), bad)
                    self.assertEqual(list(cron.iterdir()), [path])
            with self.subTest("reject linked oversized store before opening its leaf"):
                path.write_bytes(oversized)
                outside = Path(self.tmp) / "other-cron"
                cron.rename(outside)
                cron.symlink_to(outside, target_is_directory=True)
                with mock.patch.object(core.os, "open", wraps=os.open) as opened:
                    self.assertIsNone(self.create(full, deliver="telegram:1", call="linked-oversize"))
                self.assertFalse(any(Path(call.args[0]).name == "jobs.json" for call in opened.call_args_list),
                                 "the reader opened another directory's leaf before rejecting the snapshot")
                self.assertEqual((outside / "jobs.json").read_bytes(), oversized)
                self.assertEqual(self.create(deliver="local", call="independent")["action"], "block")
        private.list_jobs.assert_not_called()
        private.effective_job_state.assert_not_called()

    def test_linked_cron_directory_never_supplies_another_profiles_jobs(self):
        home = Path(self.tmp) / "home"
        other_cron = Path(self.tmp) / "other" / "cron"
        other_cron.mkdir(parents=True)
        payload = json.dumps({"jobs": [self.job(id="other-job")]}).encode()
        (other_cron / "jobs.json").write_bytes(payload)
        home.mkdir()
        cron = home / "cron"
        cron.symlink_to(other_cron, target_is_directory=True)
        self.d._jobs_loader = plugin._real_jobs
        with mock.patch.object(core, "hermes_home", return_value=home):
            self.decline(deliver="local")
            self.assertIsNone(self.create(deliver="telegram:1", call="linked"))
            self.assertIn("declined", self.create(deliver="local", call="decline")["message"])
            cron.unlink()
            cron.mkdir()
            local_path = cron / "jobs.json"
            local_path.write_text(json.dumps({"jobs": [self.job(id="local-job")]}), encoding="utf-8")
            self.assertIn("ID local-job", self.create(deliver="telegram:1", call="plain")["message"])

            opened = os.open
            def swap_then_open(path, flags, *args, **kwargs):
                if Path(path).name == "jobs.json":
                    cron.rename(home / "original-cron")
                    cron.symlink_to(other_cron, target_is_directory=True)
                return opened(path, flags, *args, **kwargs)

            with mock.patch.object(core.os, "open", side_effect=swap_then_open):
                result = self.create(deliver="telegram:1", call="swapped")
            self.assertIn("ID local-job", (result or {}).get("message", ""))
            self.assertEqual((other_cron / "jobs.json").read_bytes(), payload)

    def test_unreadable_cron_store_allows(self):
        def boom():
            raise RuntimeError("cron database corrupted")
        self.d._jobs_loader = boom
        self.assertIsNone(self.create())

    def test_decline_takes_precedence_over_existing_job_message(self):
        self.jobs = [self.job()]
        self.d.store().add(session_key="", surface="cli", fingerprint=core.make_fingerprint(PROMPT, "telegram:1", None), now=NOW)
        res = self.create(PROMPT, deliver="telegram:1")
        self.assertIn("declined", res["message"])


class TestFailOpen(Base):
    def test_busy_stores_do_not_trip_host_policy_timeout(self):
        # The standalone suite needs no Hermes. The source-parity run uses the
        # actual dispatcher and its fail-closed callback suppression.
        callback = self.d.pre_tool_call
        ref = os.environ.get("HERMES_AGENT_SRC")
        if ref:
            source = Path(ref) / "hermes_cli" / "plugins_dispatch.py"
            spec = importlib.util.spec_from_file_location("_declines_dispatch_test", source)
            dispatcher = importlib.util.module_from_spec(spec)
            stubs = {
                spec.name: dispatcher,
                "hermes_cli.middleware": types.SimpleNamespace(OBSERVER_SCHEMA_VERSION="hermes.observer.v1"),
                "hermes_cli.plugins": types.SimpleNamespace(_resolve_hook_callback_timeout=lambda: 0.2),
            }
            with mock.patch.dict(sys.modules, stubs):
                spec.loader.exec_module(dispatcher)
            host = dispatcher.PluginDispatchMixin()
            host._hook_timeout_lock = threading.Lock()
            host._hook_timeout_suppressed_until = {}
            host._hook_timeout_suppression_seconds = 60
            host._hook_running_callbacks = {}
            host._hook_abandoned = {}
            host._hook_failures_reported = set()

            def invoke(**kw):
                with mock.patch.dict(sys.modules, stubs):
                    return host._run_hook_callbacks("pre_tool_call", [callback], kw)
        else:
            def invoke(**kw):
                results = []
                worker = threading.Thread(target=lambda: results.append(callback(**kw)), daemon=True)
                worker.start()
                worker.join(0.2)
                return [r for r in results if r is not None] if results else ["timed out"]

        os.makedirs(self.state)
        cron_called = threading.Event()
        self.d._jobs_loader = lambda: cron_called.set() or []
        args = {"action": "create", "prompt": PROMPT, "schedule": "30m"}
        with open(os.path.join(self.state, "declines.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            policy = invoke(tool_name="cronjob_manage", args=args, tool_call_id="busy", session_id="s1")
            unrelated = invoke(tool_name="web_search", args={}, tool_call_id="unrelated", session_id="s1")
            self.assertEqual((policy, unrelated, cron_called.is_set()), ([], [], False))

    def test_approval_observer_returns_while_disk_write_is_stalled(self):
        self.assertIsNone(self.create(deliver="local"))
        self.d._background_writes = True  # exercise the registered production mode
        entered, release, returned, persisted = (threading.Event() for _ in range(4))
        original = self.d._save_decline

        def slow_disk(record, generation):
            entered.set()
            if release.wait(2):
                original(record, generation)
            persisted.set()

        def observe():
            self.deny()
            returned.set()

        with mock.patch.object(self.d, "_save_decline", side_effect=slow_disk):
            caller = threading.Thread(target=observe, daemon=True)
            caller.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(returned.wait(0.3), "approval observer waited for disk")
                self.assertFalse(persisted.is_set())
            finally:
                release.set()
                caller.join(2)
                self.assertTrue(persisted.wait(2))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.create(deliver="local", call="after-write")["action"], "block")

    def test_saturated_policy_skips_work_instead_of_queueing_it(self):
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        calls = []
        args = {"action": "create", "prompt": PROMPT, "deliver": "local", "schedule": "30m"}

        def expensive(*_args, **_kw):
            calls.append(threading.get_ident())
            entered.set()
            release.wait(2)

        def invoke(call):
            self.d.pre_tool_call(tool_name="cronjob_manage", args=args, session_id="s1", tool_call_id=call)
            if call == "second":
                second_done.set()

        first = threading.Thread(target=invoke, args=("first",), daemon=True)
        second = threading.Thread(target=invoke, args=("second",), daemon=True)
        with mock.patch.object(core, "find_match", side_effect=expensive):
            first.start()
            try:
                self.assertTrue(entered.wait(1))
                second.start()
                self.assertTrue(second_done.wait(0.3), "callback queued behind another cron decision")
                self.assertEqual(len(calls), 1)
                self.assertIsNone(self.d.pre_tool_call(tool_name="web_search", args={}))
            finally:
                release.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)

    def test_exception_in_pre_tool_call_does_not_raise(self):
        self.decline()
        with mock.patch.object(core, "find_match", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.create(call="c2"))

    def test_exception_in_approval_hook_does_not_raise(self):
        self.assertIsNone(self.create())
        with mock.patch.object(core.Store, "add", side_effect=OSError("disk full")):
            with self.assertLogs(plugin.log, level="WARNING"):
                self.assertIsNone(self.deny())

    def test_exception_in_post_tool_call_does_not_raise(self):
        with mock.patch.object(self.d, "_forget_pending", side_effect=ValueError("x")):
            with self.assertLogs(plugin.log, level="WARNING"):
                self.assertIsNone(self.d.post_tool_call(tool_name="cronjob_manage"))

    def test_config_read_failure_uses_defaults(self):
        def bad(key, default=None):
            raise RuntimeError("config unreadable")
        d = plugin.Declines(bad, jobs_loader=lambda: [], clock=self.clock)
        d._get_config = bad
        with mock.patch.object(core, "default_state_dir", return_value=core.Path(self.state)):
            self.assertIsNone(d.pre_tool_call(tool_name="cronjob_manage", args={"action": "create", "prompt": "x y z"}))

    def test_garbage_payloads_do_not_raise(self):
        for kw in ({}, {"tool_name": "cronjob_manage"}, {"tool_name": "cronjob_manage", "args": "nope"},
                   {"tool_name": "cronjob_manage", "args": {"action": 5, "prompt": 7}},
                   {"tool_name": "cronjob_manage", "args": {"action": "create", "prompt": ["a"], "deliver": {"x": 1}}},
                   {"tool_name": None, "args": None}):
            self.assertIsNone(self.d.pre_tool_call(**kw), kw)
        for kw in ({}, {"choice": "deny"}, {"choice": "deny", "pattern_key": 5, "pattern_keys": "x", "kind": ["a"]},
                   {"choice": ["deny"]}):
            self.assertIsNone(self.d.post_approval_response(**kw), kw)
        self.assertIsNone(self.d.post_tool_call(tool_name=None))

    def test_corrupt_store_fails_open_and_is_preserved(self):
        os.makedirs(self.state, mode=0o700)
        path = os.path.join(self.state, "declines.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(self.create())
        self.assertEqual(self.records(), [])
        self.assertTrue([n for n in os.listdir(self.state) if ".corrupt-" in n])

    def test_unwritable_state_dir_does_not_raise(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        self.assertIsNone(self.create())  # proposal seen while the store still works
        self.cfg["state_dir"] = os.path.join(blocker, "sub")  # parent is a file: cannot be created
        with self.assertLogs(plugin.log, level="WARNING"):
            self.assertIsNone(self.create(OTHER, call="c2"))
        with self.assertLogs(plugin.log, level="WARNING"):
            self.assertIsNone(self.deny())
        self.assertIn("could not run", self.d.command("list"))


class TestFiles(Base):
    def test_decline_capacity_preserves_active_vetoes_and_handles_legacy_oversize(self):
        cap = 4096  # exercise the real serialized size boundary with small synthetic records
        store = self.d.store()
        active = "Review the migration calendar"
        incoming = "z" * 1800

        def fresh():
            return plugin.Declines(self.d._get_config, jobs_loader=lambda: [], clock=self.clock,
                                   background_writes=False)

        with mock.patch.object(core, "MAX_SNAPSHOT_BYTES", cap):
            for n in range(2):
                store.add(session_key="old-session", surface="cli", now=NOW - 31 * DAY,
                          fingerprint=core.make_fingerprint(f"expired {n} " + "x" * 1200, "local", None))
            store.add(session_key="active", surface="cli", now=NOW,
                      fingerprint=core.make_fingerprint(active, "local", None))
            self.assertLessEqual(store.path.stat().st_size, cap)
            self.assertIsNone(self.create(incoming, deliver="local"))
            self.deny()
            self.assertLessEqual(store.path.stat().st_size, cap)
            self.assertEqual({r["fingerprint"]["text"] for r in self.records()}, {active.lower(), incoming})

            self.d = fresh()
            for i, prompt in enumerate((active, incoming)):
                self.assertIn("declined", self.create(prompt, deliver="local", call=str(i))["message"])
            before = store.path.read_bytes()
            self.assertIsNone(self.create("y" * 3500, deliver="local", call="full"))
            with self.assertLogs(plugin.log, level="WARNING") as reported:
                self.deny(tool_call_id="full")
            self.assertIn("not saved", " ".join(reported.output))
            self.assertEqual(store.path.read_bytes(), before)

            legacy = json.loads(before)
            legacy["declines"].append({"id": "old", "ts": NOW - 31 * DAY,
                                       "fingerprint": core.make_fingerprint("h" * cap, "local", None)})
            store.path.write_text(json.dumps(legacy), encoding="utf-8")
            legacy_bytes = store.path.read_bytes()
            with self.assertLogs(plugin.log, level="WARNING"):
                self.assertIn("declined", self.create(active, deliver="local", call="warm")["message"])
            self.d = fresh()
            with self.assertLogs(plugin.log, level="WARNING"):
                self.assertIsNone(self.create(active, deliver="local", call="cold"))
            self.assertEqual(store.path.read_bytes(), legacy_bytes)  # a refresh never repairs the file
            with self.assertLogs(plugin.log, level="WARNING"):
                self.assertIsNone(self.create(OTHER, deliver="local", call="recover"))
            self.deny(tool_call_id="recover")
            self.d = fresh()
            self.assertLessEqual(store.path.stat().st_size, cap)
            self.assertIn("declined", self.create(active, deliver="local", call="recovered")["message"])

    def test_state_files_are_0600_and_dir_0700(self):
        self.decline()
        self.d.command("forget nonexistent")
        self.assertEqual(os.path.realpath(str(self.d.store().dir)), os.path.realpath(self.state))
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        names = os.listdir(self.state)
        self.assertIn("declines.json", names)
        for n in names:
            mode = stat.S_IMODE(os.stat(os.path.join(self.state, n)).st_mode)
            self.assertEqual(mode, 0o600, n)
        self.assertFalse([n for n in names if ".tmp-" in n])

    def test_files_stay_0600_after_rewrite_with_loose_umask(self):
        old = os.umask(0o000)
        try:
            self.decline()
            self.d.command("clear")
        finally:
            os.umask(old)
        for n in os.listdir(self.state):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, n)).st_mode), 0o600, n)

    def test_default_state_dir_is_under_hermes_home(self):
        home = os.path.join(self.tmp, "home")
        with mock.patch.dict(os.environ, {"HERMES_HOME": home}):
            with mock.patch.dict(sys.modules, {"hermes_constants": None}):
                got = core.default_state_dir()
        self.assertEqual(os.path.realpath(str(got)),
                         os.path.realpath(os.path.join(home, "plugins-state", "hermes-declines")))

    def test_state_dir_setting_is_honoured(self):
        self.assertEqual(os.path.realpath(str(self.d.store().dir)), os.path.realpath(self.state))

    def test_store_holds_no_raw_prompt(self):
        self.decline(PROMPT_DATED)
        with open(os.path.join(self.state, "declines.json")) as fh:
            blob = fh.read()
        self.assertNotIn("Friday", blob)
        self.assertNotIn("passport,", blob)
        self.assertNotIn("Remind", blob)


class TestCommand(Base):
    def test_forget_drops_cached_veto_and_does_not_inflate_compact_storage(self):
        store = self.d.store()
        store.path.parent.mkdir()
        rows = [{"id": ident, "ts": NOW, "fingerprint": core.make_fingerprint(prompt, "local", None)}
                for ident, prompt in (("remove", PROMPT), ("retain", OTHER))]
        rows[1]["legacy_metadata"] = "漢" * 900  # valid compact UTF-8, costly if rewritten as ASCII escapes
        with mock.patch.object(core, "MAX_SNAPSHOT_BYTES", 4096):
            for legacy in (True, False):
                with self.subTest(oversized_legacy=legacy):
                    self.d = plugin.Declines(self.d._get_config, jobs_loader=lambda: [], clock=self.clock,
                                             background_writes=False)
                    data = {"version": 1, "declines": list(rows)}
                    store.path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                    self.assertIn("declined", self.create(PROMPT, deliver="local")["message"])
                    if legacy:
                        data["declines"].append({"id": "large", "ts": NOW - 40 * DAY,
                                                 "fingerprint": core.make_fingerprint("k" * 4096, "local", None)})
                        store.path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                    self.assertIn("Matching decline removed", self.d.command("forget remove"))
                    if not legacy:  # a new process must also enforce the remaining decline
                        self.assertLessEqual(store.path.stat().st_size, 4096)
                        self.d = plugin.Declines(self.d._get_config, jobs_loader=lambda: [], clock=self.clock,
                                                 background_writes=False)
                    self.assertIsNone(self.create(PROMPT, deliver="local", call="removed"))
                    hit = self.create(OTHER, deliver="local", call="kept")
                    self.assertIn("declined", (hit or {}).get("message", ""))

    def test_clear_fences_dequeued_and_queued_denials_but_accepts_new_ones(self):
        self.d._background_writes = True
        dequeued, release, done = (threading.Event() for _ in range(3))
        new_prompt = "Review the migration calendar"
        original = self.d._save_decline

        def stall_before_store_lock(record, *args):
            text = record["fingerprint"]["text"]
            if text == core.normalize_prompt(PROMPT):
                dequeued.set()
                if not release.wait(2):
                    return
            original(record, *args)
            if text == core.normalize_prompt(new_prompt):
                done.set()

        with mock.patch.object(self.d, "_save_decline", side_effect=stall_before_store_lock):
            try:
                self.assertIsNone(self.create(PROMPT, deliver="local", call="dequeued"))
                self.deny(tool_call_id="dequeued")
                self.assertTrue(dequeued.wait(1))
                self.assertIsNone(self.create(OTHER, deliver="local", call="queued"))
                self.deny(tool_call_id="queued")
                self.assertEqual(self.d.command("clear"), "Cleared 0 decline(s).")
                self.assertIsNone(self.create(new_prompt, deliver="local", call="new"))
                self.deny(tool_call_id="new")
            finally:
                release.set()
            self.assertTrue(done.wait(2))
        self.assertEqual([r["fingerprint"]["text"] for r in self.records()], [core.normalize_prompt(new_prompt)])
        self.assertIsNone(self.create(PROMPT, deliver="local", call="check-old"))
        self.assertIn("declined", self.create(new_prompt, deliver="local", call="check-new")["message"])

    def test_list_forget_clear(self):
        self.assertEqual(self.d.command(""), "No active declines.")
        self.decline()
        rid = self.records()[0]["id"]
        out = self.d.command("list")
        self.assertIn(rid, out)
        self.assertIn("remind me to renew my passport", out)
        self.assertIn("30 d left", out)
        self.assertIn("No single decline", self.d.command("forget zzzz"))
        self.assertIn("Usage", self.d.command("forget"))
        self.assertIn("Forgot " + rid, self.d.command("forget " + rid[:4]))
        self.assertEqual(self.d.command("list"), "No active declines.")
        self.assertIn("Usage", self.d.command("bogus"))

    def test_forget_reports_only_removal_when_duplicate_still_blocks(self):
        self.decline(deliver="local")
        ident = self.records()[0]["id"]
        self.jobs = [{"id": "j1", "prompt": PROMPT, "deliver": "local", "state": "scheduled"}]
        self.assertEqual(self.d.command("forget " + ident), f"Forgot {ident}. Matching decline removed.")
        self.assertEqual(self.records(), [])
        result = self.create(deliver="local", call="still-duplicate")
        self.assertIn("already exists", result["message"])

    def test_list_hides_expired(self):
        self.decline()
        self.clock.t = NOW + 31 * DAY
        self.assertEqual(self.d.command("list"), "No active declines.")


class TestRegister(unittest.TestCase):
    def test_register_wires_hooks_and_command(self):
        class Ctx:
            def __init__(self):
                self.hooks, self.commands = {}, {}

            def register_hook(self, name, cb, **kw):
                self.hooks[name] = cb

            def register_command(self, name, handler, description="", args_hint="", **kw):
                self.commands[name] = (handler, args_hint)

            def get_config(self, key, default=None):
                return default

        ctx = Ctx()
        plugin.register(ctx)
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "pre_tool_call", "post_tool_call", "post_approval_response"})
        self.assertIn("declines", ctx.commands)

    def test_manifest_declares_registered_hooks(self):
        with open(os.path.join(str(_load.ROOT), "plugin.yaml")) as fh:
            text = fh.read()
        for needle in ("name: hermes-declines", "manifest_version: 2", "provides_hooks:", "- pre_llm_call", "- pre_tool_call",
                       "- post_approval_response", "- post_tool_call", "config_schema:", "threshold:",
                       "expire_days:", "check_existing_jobs:", "state_dir:"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
