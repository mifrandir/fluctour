import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fluctour.cli import constraints_for, main, view
from fluctour.planning import (assess, evidence_fresh, leave_required, overnight,
                              policy_digest, policy_evidence_valid, rank_options, validate_result)
from fluctour.research import (SESSION_RESERVATION, admit, budget_state, build_prompt,
                              codex_command, research)
from fluctour.store import DEFAULT_PROFILE, Store, now


def option(**updates):
    value = {
        "id": "fictional-a", "title": "Fictional test itinerary", "destination": "Test city",
        "departure": "2027-01-07T22:30:00+08:00",
        "destination_arrival": "2027-01-08T02:00:00+09:00",
        "return_departure": "2027-01-10T16:00:00+09:00",
        "return_arrival": "2027-01-10T19:00:00+08:00",
        "currency": "HKD", "total_cost": 5000, "cost_complete": True,
        "cost_breakdown": ["Fictional total: 5000"], "usable_hours": 48,
        "overnight_flight": False, "policy_status": "unknown",
        "rationale": "Fictional fixture; not a travel recommendation.", "tradeoffs": [],
        "evidence": [], "unresolved": [], "legs": [],
    }
    value.update(updates)
    return value


def dossier(options=None):
    return {"summary": "Fictional research fixture.", "options": options if options is not None else [option()],
            "coverage": ["Test dates"], "remaining_work": ["Obtain live evidence."]}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.profile = copy.deepcopy(DEFAULT_PROFILE)

    def test_after_work_does_not_charge_thursday(self):
        result = leave_required(option(), self.profile)
        self.assertEqual(result["dates"], ["2027-01-08"])
        self.assertEqual(result["leave_home_at"], "2027-01-07T18:00:00+08:00")
        self.assertTrue(result["provisional"])

    def test_earlier_flight_charges_departure_workday(self):
        self.assertEqual(leave_required(option(departure="2027-01-07T22:00:00+08:00"),
                                        self.profile)["days"], 2)

    def test_holiday_is_explicit(self):
        self.profile["holidays"] = ["2027-01-08"]
        self.assertEqual(leave_required(option(), self.profile)["days"], 0)

    def test_after_midnight_departure_uses_previous_evening(self):
        result = leave_required(option(departure="2027-01-08T00:30:00+08:00"), self.profile)
        self.assertEqual(result["leave_home_at"], "2027-01-07T20:00:00+08:00")
        self.assertEqual(result["days"], 1)

    def test_recovery_can_require_monday_leave(self):
        self.profile["recovery_minutes"] = 13 * 60
        self.assertIn("2027-01-11", leave_required(option(), self.profile)["dates"])

    def test_return_timezone_is_converted(self):
        value = option(return_arrival="2027-01-10T23:00:00-05:00")
        self.assertIn("2027-01-11", leave_required(value, self.profile)["dates"])

    def test_dst_uses_home_timezone(self):
        self.profile.update(timezone="America/New_York", outbound_transfer_minutes=0,
                            airport_buffer_minutes=0, home_transfer_minutes=0)
        value = option(departure="2027-03-12T18:00:00-05:00",
                       return_arrival="2027-03-15T12:00:00Z")
        self.assertEqual(leave_required(value, self.profile)["days"], 0)

    def test_naive_dates_are_rejected(self):
        with self.assertRaises(ValueError):
            leave_required(option(departure="2027-01-07T22:30:00"), self.profile)

    def test_freshness_and_future_timestamps(self):
        current = datetime.now(timezone.utc)
        evidence = {"kind": "availability", "checked_at": (current - timedelta(hours=7)).isoformat()}
        self.assertFalse(evidence_fresh(evidence, current))
        evidence["checked_at"] = (current + timedelta(seconds=1)).isoformat()
        self.assertFalse(evidence_fresh(evidence, current))

    def test_missing_evidence_never_passes(self):
        result = assess(option(), self.profile)
        self.assertEqual(result["status"], "needs_checks")
        self.assertEqual(result["booking_status"], "not_booked")
        self.assertTrue(any("availability" in x for x in result["blockers"]))
        self.assertTrue(any("Employer policy" in x for x in result["blockers"]))

    def test_room_availability_does_not_prove_flight_availability(self):
        evidence = {"kind": "availability", "scope": "accommodation", "url": "https://example.com/fixture",
                    "checked_at": now(), "finding": "Fictional room evidence.",
                    "source_type": "public_url", "policy_digest": "", "policy_excerpt": ""}
        result = assess(option(evidence=[evidence]), self.profile)
        self.assertTrue(any("outbound availability" in x for x in result["blockers"]))
        self.assertTrue(any("return availability" in x for x in result["blockers"]))
        self.assertFalse(any("accommodation availability" in x for x in result["blockers"]))

    def test_source_labels_cannot_certify_supplier_inventory(self):
        self.profile.update(home_airport="HKG", calendar_confirmed=True, timing_confirmed=True,
                            policy_confirmed=True, policy_text="Business travel only.",
                            passport_nationality="DE", residency="Hong Kong")
        checks = [("price", "trip"), ("availability", "outbound"), ("availability", "return"),
                  ("availability", "accommodation"), ("entry", "trip"), ("policy", "trip"),
                  ("schedule", "outbound"), ("schedule", "return")]
        evidence = [{"kind": k, "scope": s, "url": "https://example.com/route", "checked_at": now(),
                     "finding": "Model claim", "source_type": "public_url", "policy_digest": "",
                     "policy_excerpt": ""} for k, s in checks]
        result = assess(option(evidence=evidence, policy_status="pass"), self.profile)
        self.assertEqual(result["status"], "needs_checks")
        self.assertTrue(any("independently verified" in x for x in result["blockers"]))

    def test_no_overnight_uses_dates_instead_of_model_flag(self):
        self.assertTrue(overnight(option(overnight_flight=False)))
        self.assertEqual(rank_options(dossier(), self.profile, {"no_overnight": True}), [])

    def test_private_policy_evidence_binds_to_current_text(self):
        self.profile["policy_text"] = "London business class twice per year."
        e = {"kind": "policy", "scope": "trip", "url": "", "checked_at": now(), "finding": "Allowance",
             "source_type": "profile_policy", "policy_digest": policy_digest(self.profile),
             "policy_excerpt": "London business class twice per year."}
        validate_result(dossier([option(evidence=[e])]))
        self.assertTrue(policy_evidence_valid(e, self.profile))
        self.profile["policy_text"] = "London business class once per year."
        self.assertFalse(policy_evidence_valid(e, self.profile))

    def test_unknown_flight_and_price_are_explicit(self):
        value = option(departure=None, destination_arrival=None, return_departure=None, return_arrival=None,
                       total_cost=None, usable_hours=None, cost_complete=False, overnight_flight=None)
        validate_result(dossier([value]))
        self.assertIsNone(leave_required(value, self.profile)["days"])
        self.assertIsNone(overnight(value))

    def test_filters_do_not_compare_unlike_currencies(self):
        data = dossier([option(), option(id="other", currency="USD", total_cost=100)])
        self.assertEqual([o["id"] for o in rank_options(data, self.profile, {"max_cost": 6000})],
                         ["fictional-a"])

    def test_filter_and_ranking_use_calculated_leave(self):
        data = dossier([option(id="cheap", total_cost=2000, departure="2027-01-07T22:00:00+08:00"),
                        option(id="afterwork")])
        self.assertEqual(rank_options(data, self.profile)[0]["id"], "afterwork")
        self.assertEqual(len(rank_options(data, self.profile, {"max_leave": 1})), 1)
        self.assertEqual(rank_options(data, self.profile, {"max_cost": 1000}), [])

    def test_schema_rejects_bad_or_unsafe_output(self):
        validate_result(dossier())
        invalid = [dossier([option(total_cost=float("nan"))]),
                   dossier([option(total_cost=-1)]), dossier([option(usable_hours=1000)]),
                   dossier([option(), option()]), dossier([option(total_cost=True)]),
                   dossier([option(evidence=[{"kind": "price", "scope": "trip", "url": "file:///etc/passwd",
                                             "checked_at": now(), "finding": "No"}])])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_result(value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.trip = self.store.create_trip("Fictional local test request")

    def save(self, result=None, usage=None):
        run = self.store.begin_run(self.trip, "discover")
        self.store.finish_run(run, "completed", usage or {"input_tokens": 100, "output_tokens": 50},
                              result=result or dossier())
        return run

    def test_checkpoint_persists_across_connections(self):
        self.save()
        other = Store(self.temp.name)
        try:
            self.assertEqual(other.latest_result(self.trip)["summary"], "Fictional research fixture.")
        finally:
            other.db.close()

    def test_failed_stage_preserves_prior_dossier(self):
        self.save()
        run = self.store.begin_run(self.trip, "challenge")
        self.store.finish_run(run, "failed", {}, error="fixture")
        self.assertEqual(self.store.latest_result(self.trip)["summary"], "Fictional research fixture.")
        self.assertEqual(budget_state(self.store, self.trip)["job_used_or_reserved"], 150 + SESSION_RESERVATION)

    def test_cached_tokens_are_counted_once(self):
        self.save(usage={"input_tokens": 100, "output_tokens": 50, "cached_input_tokens": 80})
        self.assertEqual(budget_state(self.store, self.trip)["job_used_or_reserved"], 150)

    def test_global_budget_covers_other_trips(self):
        profile = self.store.profile()
        profile["monthly_token_budget"] = 50000
        self.store.save_profile(profile)
        self.save()
        other = self.store.create_trip("Second test")
        with self.assertRaisesRegex(ValueError, "allowance"):
            admit(self.store, other)

    def test_interruption_and_exclusive_worker(self):
        self.store.begin_run(self.trip, "discover")
        with self.store.worker_lock():
            self.assertEqual(self.store.runs(self.trip)[0]["status"], "interrupted")
            with self.assertRaisesRegex(ValueError, "Another research worker"):
                with self.store.worker_lock():
                    pass

    def test_feedback_filters_do_not_launch_codex(self):
        self.save()
        with patch("fluctour.research.subprocess.Popen", side_effect=AssertionError("No inference")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", self.temp.name, "feedback", self.trip,
                                   "--max-cost", "1000"]), 0)
        self.assertEqual(view(self.store, self.trip)["options"], [])
        self.assertEqual(len(self.store.runs(self.trip)), 1)
        self.store.add_feedback(self.trip, "", {"clear": True})
        self.assertEqual(constraints_for(self.store, self.trip), {})

    def test_pending_feedback_is_not_dropped(self):
        for index in range(12):
            self.store.add_feedback(self.trip, f"request-{index}", {})
        prompt = build_prompt(self.store, self.trip, "discover")
        self.assertIn("request-0", prompt)
        self.assertIn("request-11", prompt)

    def test_applied_requirements_remain_in_every_stage(self):
        self.store.add_feedback(self.trip, "NEVER USE SEPARATE TICKETS", {})
        saved = dossier()
        saved["_feedback_id"] = self.store.feedback(self.trip)[0]["id"]
        self.save(saved)
        self.assertIn("NEVER USE SEPARATE TICKETS", build_prompt(self.store, self.trip, "challenge"))

    def test_personal_profile_cannot_enter_external_research_prompt(self):
        profile = self.store.profile()
        profile.update(passport_nationality="German", policy_text="Private reimbursement details")
        self.store.save_profile(profile)
        with self.assertRaisesRegex(ValueError, "stay local"):
            build_prompt(self.store, self.trip, "discover")

    def test_public_research_store_does_not_inherit_private_profile(self):
        profile = self.store.profile()
        profile.update(passport_nationality="German", policy_text="Private reimbursement details")
        self.store.save_profile(profile)
        child = Store(self.store.root / "public-research")
        try:
            trip = child.create_trip("Public airport route reference")
            prompt = build_prompt(child, trip, "discover")
            self.assertNotIn("Private reimbursement details", prompt)
            self.assertNotIn('"German"', prompt)
        finally:
            child.db.close()

    def test_invalid_profile_is_not_persisted(self):
        profile = self.store.profile()
        profile["calendar_confirmed"] = "false"
        with self.assertRaises(ValueError):
            self.store.save_profile(profile)
        self.assertIs(self.store.profile()["calendar_confirmed"], False)

    def test_invalid_timezone_and_holidays_return_clear_errors(self):
        for key, value in (("timezone", "No/Such_Zone"), ("holidays", None), ("work_start", 9)):
            with self.subTest(key=key):
                profile = self.store.profile()
                profile[key] = value
                with self.assertRaises(ValueError):
                    self.store.save_profile(profile)

    def test_shell_metacharacters_remain_data(self):
        self.store.add_feedback(self.trip, '$(touch /tmp/fluctour-no) `whoami`', {})
        self.assertIn("$(touch /tmp/fluctour-no)", build_prompt(self.store, self.trip, "discover"))
        with patch("fluctour.research.shutil.which", return_value="/usr/bin/codex"):
            command = codex_command(Path("/tmp/work"), Path("/tmp/schema"), Path("/tmp/out"))
        self.assertEqual(command[-1], "-")
        self.assertIn("read-only", command)
        self.assertIn("--ignore-user-config", command)
        self.assertFalse(any("bypass" in arg for arg in command))

    def test_nan_filter_is_rejected(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--data-dir", self.temp.name, "feedback", self.trip,
                                   "--max-cost", "nan"]), 1)
        self.assertEqual(self.store.feedback(self.trip), [])


class RuntimeTests(unittest.TestCase):
    """Exercise the real subprocess protocol using a local fake executable."""

    def setUp(self):
        StoreTests.setUp(self)
        from fluctour.codex_transport import DISABLED_FEATURES
        self.fake = Path(self.temp.name) / "fake_codex.py"
        self.fake.write_text("DISABLED=" + repr(DISABLED_FEATURES) + '\n' + '''import json, os, sys
thread_id='fixture-thread-'+str(os.getpid())
turn_id='fixture-turn-'+str(os.getpid())
def emit(message):
    print(json.dumps(message),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    method=request.get('method')
    if method=='initialize':
        emit({'id':request['id'],'result':{'userAgent':'offline-test'}})
    elif method=='config/read':
        emit({'id':request['id'],'result':{'config':{
            'features':dict.fromkeys(DISABLED,False),'mcp_servers':{},'model_providers':{},
            'model_provider':'openai','web_search':'live','project_doc_max_bytes':0}}})
    elif method=='thread/start':
        emit({'id':request['id'],'result':{'thread':{'id':thread_id,'ephemeral':True},
            'approvalPolicy':'never','sandbox':{'type':'readOnly'},'instructionSources':[]}})
    elif method=='turn/start':
        prompt=request['params']['input'][0]['text']
        emit({'id':request['id'],'result':{'turn':{'id':turn_id,'status':'inProgress'}}})
        emit({'method':'turn/started','params':{'threadId':thread_id,'turn':{'id':turn_id,'status':'inProgress'}}})
        if 'SLOW_FIXTURE' in prompt:
            continue
        if 'FAIL_FIXTURE' in prompt:
            emit({'method':'turn/completed','params':{'threadId':thread_id,
                'turn':{'id':turn_id,'status':'failed','error':{'message':'Offline fixture failure'}}}})
            continue
        emit({'method':'thread/tokenUsage/updated','params':{'threadId':thread_id,'turnId':turn_id,
            'tokenUsage':{'last':{},'total':{'inputTokens':100,'outputTokens':50,
                'cachedInputTokens':80,'reasoningOutputTokens':0,'totalTokens':150}}}})
        result={'summary':'Fake Codex result','options':[],
            'coverage':['fixture'],'remaining_work':['live research']}
        emit({'method':'item/completed','params':{'threadId':thread_id,'turnId':turn_id,
            'item':{'id':'answer','type':'agentMessage','phase':'final_answer','text':json.dumps(result)}}})
        emit({'method':'turn/completed','params':{'threadId':thread_id,'turn':{'id':turn_id,'status':'completed'}}})
    elif method=='turn/interrupt':
        emit({'id':request['id'],'result':{}})
        emit({'method':'turn/completed','params':{'threadId':thread_id,'turn':{'id':turn_id,'status':'interrupted'}}})
''')
        self.command_patch = patch("fluctour.codex_transport.app_server_command", return_value=
                                   [sys.executable, "-u", str(self.fake)])
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.preflight_patch = patch("fluctour.research.app_server_command", return_value=
                                     [sys.executable, "-u", str(self.fake)])
        self.preflight_patch.start()
        self.addCleanup(self.preflight_patch.stop)

    def test_three_stages_and_resume_without_duplicate_calls(self):
        research(self.store, self.trip, steps=1, progress=lambda _: None)
        self.assertEqual(len(self.store.runs(self.trip)), 1)
        research(self.store, self.trip, progress=lambda _: None)
        self.assertEqual(len(self.store.runs(self.trip)), 3)
        research(self.store, self.trip, progress=lambda _: None)
        self.assertEqual(len(self.store.runs(self.trip)), 3)
        self.assertEqual(len({r["thread_id"] for r in self.store.runs(self.trip)}), 3)
        self.assertEqual(budget_state(self.store, self.trip)["job_used_or_reserved"], 450)

    def test_feedback_refresh_preserves_completed_stage_coverage(self):
        research(self.store, self.trip, progress=lambda _: None)
        self.store.add_feedback(self.trip, "Different neighborhood", {})
        research(self.store, self.trip, progress=lambda _: None)
        self.assertEqual(self.store.runs(self.trip)[-1]["stage"], "refresh")
        research(self.store, self.trip, progress=lambda _: None)
        self.assertEqual(len(self.store.runs(self.trip)), 4)

    def test_timeout_is_durable_and_process_is_reaped(self):
        trip = self.store.create_trip("SLOW_FIXTURE")
        with self.assertRaises(TimeoutError):
            research(self.store, trip, steps=1, timeout=1, progress=lambda _: None)
        run = self.store.runs(trip)[0]
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["thread_id"])
        self.assertIsNone(self.store.latest_result(trip))
        self.assertEqual(budget_state(self.store, trip)["job_used_or_reserved"], SESSION_RESERVATION)

    def test_failure_does_not_automatically_retry(self):
        trip = self.store.create_trip("FAIL_FIXTURE")
        with self.assertRaises(ValueError):
            research(self.store, trip, progress=lambda _: None)
        self.assertEqual(len(self.store.runs(trip)), 1)
        self.assertEqual(self.store.runs(trip)[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
