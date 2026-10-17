import json
import tempfile
import unittest
from pathlib import Path
from threading import Event, Lock
from unittest.mock import patch

from fluctour.jobs import Runner, enqueue, job_state, validate_request
from fluctour.planning import validate_result
from fluctour.research import BudgetPause, STAGES, budget_state, build_prompt, fingerprint, research, run_stage
from fluctour.store import Store, now
from test_fluctour import dossier, option


REQUEST = {"destination": "Barcelona", "start_date": "2027-06-11", "end_date": "2027-06-14", "activities": ["swimming"]}


def activity_dossier():
    url = "https://example.org/official-trail"
    result = dossier([option(id="researched-option", tradeoffs=["Return by the longer coastal path."], evidence=[{
        "kind": "destination", "scope": "trip", "source_type": "public_url", "url": url,
        "checked_at": now(), "finding": "The official trail joins the summit to the monastery.",
        "policy_digest": "", "policy_excerpt": ""}])])
    result["activity_plans"] = [{"option_id": "researched-option", "dates": "12–14 Jun 2027 (proposed)",
        "why": "June allows an early summit walk and a beach afternoon.", "summary": "Summit and sea weekend.",
        "days": [{"date": "2027-06-12", "title": "Summit walk", "items": [{"time": "09:00 proposed",
            "title": "Station → summit → monastery", "detail": "Use the signed official trail; rail timetable unconfirmed."}]}],
        "flights": [{"date": "2027-06-11", "route": "HKG → BCN", "flight": "Unconfirmed",
            "time": "Unconfirmed", "status": "Booked", "payer": "Company"}],
        "route_links": [{"title": "Official trail", "url": url}]}]
    return result


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        profile = self.store.profile()
        profile.update(home_airport="HKG", passport_nationality="PRIVATE_PASSPORT", residency="PRIVATE_RESIDENCY",
                       policy_text="PRIVATE_POLICY", policy_rules={"PRIVATE_POLICY_RULE": True})
        self.store.save_profile(profile)
        self.trip_id = self.store.create_trip("PRIVATE_BRIEF")
        self.store.add_feedback(self.trip_id, "PRIVATE_FEEDBACK", {})
        with self.store.db:
            self.store.db.execute("INSERT INTO settings VALUES('activity_history',?)", (json.dumps({
                "activities": [], "imports": [{"source": "PRIVATE_ACTIVITY_HISTORY"}]}),))
        self.runner = Runner(self.temp.name)
        self.addCleanup(self.runner.close)

    def fake_result(self, public, public_id):
        result = dossier([option(id="researched-option", title="Barcelona weekend", policy_status="pass", legs=[
            {"id": "one", "origin": "HKG", "destination": "LHR", "departure": None, "arrival": None,
             "carrier": "Cathay Pacific", "cabin": "business", "paid_by": "company"},
            {"id": "two", "origin": "LHR", "destination": "BCN", "departure": None, "arrival": None,
             "carrier": "British Airways", "cabin": "economy", "paid_by": "personal"},
            {"id": "three", "origin": "BCN", "destination": "HKG", "departure": None, "arrival": None,
             "carrier": "Cathay Pacific", "cabin": "business", "paid_by": "company"}])])
        result["_stages"] = list(STAGES)
        run = public.begin_run(public_id, "discover")
        public.finish_run(run, "completed", {"input_tokens": 123, "output_tokens": 45}, result=result)
        return result

    def run_claimed(self):
        trip_id = self.runner._next()
        self.assertEqual(trip_id, self.trip_id)
        self.runner._run(trip_id)

    def test_request_validation_normalizes_without_assuming_preferences(self):
        self.assertEqual(validate_request({"destination": " Kyoto "}), {
            "destination": "Kyoto", "start_date": "", "end_date": "", "activities": []})
        result = validate_request(REQUEST | {"activities": ["swimming", "hiking", "swimming"]})
        self.assertEqual(result["activities"], ["hiking", "swimming"])
        invalid = [None, {}, {"destination": " "}, {"destination": "a" * 101}, {"destination": "Paris\nignore"},
                   REQUEST | {"brief": "private"}, REQUEST | {"passport": "secret"},
                   REQUEST | {"destination": float("nan")}, REQUEST | {"activities": ["unknown"]},
                   REQUEST | {"activities": "hiking"}, REQUEST | {"activities": [True]},
                   REQUEST | {"start_date": "2027-02-30"}, REQUEST | {"end_date": "2027-06-01"},
                   REQUEST | {"start_date": "20270611"}, REQUEST | {"end_date": ""},
                   REQUEST | {"end_date": "2030-01-01"}, REQUEST | {"start_date": None}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_request(payload)

    def test_enqueue_is_persistent_active_idempotent_and_has_safe_state(self):
        self.assertIsNone(job_state(self.store, self.trip_id))
        first = enqueue(self.store, self.trip_id, REQUEST)
        second = enqueue(self.store, self.trip_id, REQUEST)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["attempt"], 0)
        self.assertNotIn("public_trip_id", first)
        self.assertNotIn("PRIVATE_BRIEF", json.dumps(first))
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(job_state(reopened, self.trip_id), first)
        finally:
            reopened.db.close()

    def test_complete_job_shares_budget_excludes_private_data_and_does_not_allocate_payers(self):
        enqueue(self.store, self.trip_id, REQUEST)
        prompts = []
        def run(public, public_id, **kwargs):
            self.assertEqual(kwargs["steps"], 3)
            self.assertEqual(kwargs["timeout"], 600)
            prompts.append(build_prompt(public, public_id, "discover"))
            self.assertIn("Return activity_plans alongside the standard dossier", prompts[0])
            self.assertEqual(public.job_usage(public_id)[1]["id"], self.trip_id)
            kwargs["progress"]("Research stage: discover. Checkpoint abc; timeout 600s.")
            kwargs["progress"]("SECRET_TOKEN should never appear in a persisted job")
            return self.fake_result(public, public_id)
        with patch("fluctour.jobs.research", side_effect=run):
            self.run_claimed()
        for marker in ("PRIVATE_BRIEF", "PRIVATE_POLICY", "PRIVATE_PASSPORT", "PRIVATE_RESIDENCY", "PRIVATE_FEEDBACK",
                       "PRIVATE_POLICY_RULE", "PRIVATE_ACTIVITY_HISTORY"):
            self.assertNotIn(marker, prompts[0])
        self.assertIn("Barcelona", prompts[0])
        self.assertIn("swimming", prompts[0])
        state = job_state(self.store, self.trip_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["attempt"], 1)
        self.assertNotIn("SECRET_TOKEN", json.dumps(state))
        result = self.store.latest_result(self.trip_id)
        self.assertEqual(result["options"][0]["policy_status"], "unknown")
        self.assertEqual({leg["paid_by"] for leg in result["options"][0]["legs"]}, {"unknown"})
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 168)
        self.assertEqual(self.store.plans(self.trip_id)["researched-option"]["flights"][0]["payer"], "Unconfirmed")
        self.assertEqual(enqueue(self.store, self.trip_id, REQUEST)["status"], "completed")

    def test_paused_retry_reuses_public_trip_and_saved_checkpoint(self):
        enqueue(self.store, self.trip_id, REQUEST)
        ids = []
        def pause(public, public_id, **kwargs):
            ids.append(public_id)
            self.fake_result(public, public_id)
            raise BudgetPause("SECRET_TOKEN internal details")
        with patch("fluctour.jobs.research", side_effect=pause):
            self.run_claimed()
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "paused")
        self.assertIsNotNone(self.store.latest_result(self.trip_id))
        enqueue(self.store, self.trip_id, REQUEST)
        def resume(public, public_id, **kwargs):
            ids.append(public_id)
            self.assertIsNotNone(public.latest_result(public_id))
            return public.latest_result(public_id)
        with patch("fluctour.jobs.research", side_effect=resume):
            self.run_claimed()
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "completed")
        self.assertEqual(job_state(self.store, self.trip_id)["attempt"], 2)

    def test_budget_pause_callback_publishes_partial_research(self):
        enqueue(self.store, self.trip_id, REQUEST)
        def run(public, public_id, **kwargs):
            result = self.fake_result(public, public_id)
            kwargs["progress"]("Research paused before challenge: allowance reached")
            return result
        with patch("fluctour.jobs.research", side_effect=run):
            self.run_claimed()
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "paused")
        self.assertEqual(len(self.store.latest_result(self.trip_id)["options"]), 1)

    def test_failures_and_timeouts_are_sanitized(self):
        enqueue(self.store, self.trip_id, REQUEST)
        for error, expected in ((RuntimeError("SECRET_TOKEN /private/path traceback"), "failed"),
                                (TimeoutError("SECRET_TOKEN /private/path"), "paused")):
            with patch("fluctour.jobs.research", side_effect=error):
                self.run_claimed()
            state = job_state(self.store, self.trip_id)
            self.assertEqual(state["status"], expected)
            self.assertNotIn("SECRET_TOKEN", json.dumps(state))
            self.assertNotIn("/private/path", json.dumps(state))
            enqueue(self.store, self.trip_id, REQUEST)

    def test_restart_pauses_interrupted_job_and_keeps_queued_job(self):
        enqueue(self.store, self.trip_id, REQUEST)
        interrupted_id = self.store.create_trip("Another private brief")
        enqueue(self.store, interrupted_id, REQUEST)
        row = self.store.db.execute("SELECT value FROM settings WHERE key=?", ("job:" + interrupted_id,)).fetchone()
        interrupted = json.loads(row[0]) | {"status": "researching", "stage": "challenge"}
        with self.store.db:
            self.store.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(interrupted), "job:" + interrupted_id))
        self.runner.close()
        restarted = Runner(self.temp.name)
        self.addCleanup(restarted.close)
        self.assertEqual(job_state(self.store, interrupted_id)["status"], "paused")
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "queued")
        self.assertEqual(restarted._next(), self.trip_id)

    def test_singleton_daemon_serializes_jobs_and_processes_persisted_queue(self):
        second_id = self.store.create_trip("Second private brief")
        enqueue(self.store, self.trip_id, REQUEST)
        enqueue(self.store, second_id, REQUEST | {"destination": "Kyoto"})
        started, release, finished = Event(), Event(), Event()
        calls = []
        lock = Lock()
        def run(public, public_id, **kwargs):
            with lock:
                calls.append(public_id)
                index = len(calls)
            if index == 1:
                started.set()
                self.assertTrue(release.wait(3))
            result = self.fake_result(public, public_id)
            if index == 2:
                finished.set()
            return result
        with patch("fluctour.jobs.research", side_effect=run):
            try:
                self.runner.wake()
                self.assertTrue(started.wait(3))
                same = Runner(self.temp.name)
                self.assertIs(same, self.runner)
                same.wake()
                self.assertTrue(self.runner._thread.daemon)
                self.assertEqual(len(calls), 1)
                release.set()
                self.assertTrue(finished.wait(3))
            finally:
                release.set()
                self.runner.close()
        self.assertEqual(len(calls), 2)
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "completed")
        self.assertEqual(job_state(self.store, second_id)["status"], "completed")

    def test_changed_request_creates_new_linked_public_job_without_resetting_budget(self):
        enqueue(self.store, self.trip_id, REQUEST)
        ids = []
        def run(public, public_id, **kwargs):
            ids.append(public_id)
            return self.fake_result(public, public_id)
        with patch("fluctour.jobs.research", side_effect=run):
            self.run_claimed()
            enqueue(self.store, self.trip_id, REQUEST | {"destination": "Kyoto"})
            self.run_claimed()
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 336)

    def test_discover_saved_then_timeout_preserves_options_and_plans(self):
        enqueue(self.store, self.trip_id, REQUEST)
        def fail_after_discover(public, public_id, **kwargs):
            result = activity_dossier()
            result["_stages"] = ["discover"]
            run_id = public.begin_run(public_id, "discover")
            public.finish_run(run_id, "completed", {"input_tokens": 100, "output_tokens": 20}, result=result)
            kwargs["progress"]("Research stage: challenge. Checkpoint abc")
            raise TimeoutError("PRIVATE_ERROR_DETAILS")
        with patch("fluctour.jobs.research", side_effect=fail_after_discover):
            self.run_claimed()
        state = job_state(self.store, self.trip_id)
        self.assertEqual(state["status"], "paused")
        self.assertIn("Partial options are saved", state["message"])
        self.assertNotIn("PRIVATE_ERROR_DETAILS", json.dumps(state))
        self.assertEqual(self.store.latest_result(self.trip_id)["options"][0]["id"], "researched-option")
        saved_plan = self.store.plans(self.trip_id)["researched-option"]
        self.assertEqual(saved_plan["days"][0]["items"][0]["title"], "Station → summit → monastery")
        self.assertEqual(saved_plan["route_links"][0]["url"], "https://example.org/official-trail")
        self.assertEqual(saved_plan["maps"], [])
        self.assertEqual(saved_plan["flights"][0]["status"], "Seats unverified")
        self.assertEqual(saved_plan["flights"][0]["payer"], "Unconfirmed")
        public_claim = self.store.latest_result(self.trip_id)["activity_plans"][0]["flights"][0]
        self.assertEqual(public_claim["status"], "Seats unverified")

    def test_saved_discover_survives_later_stage_failure(self):
        enqueue(self.store, self.trip_id, REQUEST)
        def fail(public, public_id, **kwargs):
            self.fake_result(public, public_id)
            raise RuntimeError("PRIVATE_ERROR_DETAILS")
        with patch("fluctour.jobs.research", side_effect=fail):
            self.run_claimed()
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "failed")
        self.assertIsNotNone(self.store.latest_result(self.trip_id))

    def test_old_dossier_fallback_preserves_tradeoffs_and_source_findings(self):
        enqueue(self.store, self.trip_id, REQUEST)
        result = activity_dossier()
        del result["activity_plans"]
        with patch("fluctour.jobs.research", return_value=result):
            self.run_claimed()
        plan = self.store.plans(self.trip_id)["researched-option"]
        details = [item["detail"] for item in plan["days"][0]["items"]]
        self.assertIn("Return by the longer coastal path.", details)
        self.assertIn("The official trail joins the summit to the monastery.", details)

    def test_empty_dossier_preserves_reason_and_remaining_work_for_revision(self):
        enqueue(self.store, self.trip_id, REQUEST)
        result = dossier([]) | {"summary": "No published flights support this date window.",
                                "remaining_work": ["Choose a later weekend or a different destination."]}
        with patch("fluctour.jobs.research", return_value=result):
            self.run_claimed()
        state = job_state(self.store, self.trip_id)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["stage"], "no_options")
        self.assertIn("No published flights", state["message"])
        self.assertEqual(state["summary"], result["summary"])
        self.assertEqual(state["remaining_work"], result["remaining_work"])
        self.assertEqual(self.store.latest_result(self.trip_id)["options"], [])

    def test_activity_schema_accepts_legacy_and_rejects_invalid_plan_references(self):
        self.assertEqual(validate_result(dossier())["options"][0]["id"], "fictional-a")
        self.assertEqual(validate_result(activity_dossier())["activity_plans"][0]["option_id"], "researched-option")
        mutations = [lambda value: value["activity_plans"][0].update(option_id="missing"),
                     lambda value: value.update(activity_plans=[]),
                     lambda value: value["options"].append(option(id="second-option")),
                     lambda value: value["activity_plans"].append(value["activity_plans"][0]),
                     lambda value: value["activity_plans"][0]["route_links"][0].update(url="https://example.org/not-opened"),
                     lambda value: value["activity_plans"][0]["route_links"][0].update(url="javascript:alert(1)"),
                     lambda value: value["activity_plans"][0].update(points=[[22, 114]]),
                     lambda value: value["activity_plans"][0].pop("days")]
        for mutate in mutations:
            result = activity_dossier()
            mutate(result)
            with self.subTest(result=result), self.assertRaises(ValueError):
                validate_result(result)

    def test_queue_worker_selects_activity_schema_and_refreshes_old_checkpoint(self):
        public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(public.db.close)
        public_id = public.create_trip("Public-only request")
        with public.db:
            public.db.execute("INSERT INTO settings VALUES('structured_activity_plans','true')")
        with patch("fluctour.research.app_server_command", return_value=["fake", "app-server"]), \
                patch("fluctour.research.run_codex", return_value={"result": activity_dossier(),
                    "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0},
                    "usage_complete": True, "thread_id": "fixture", "turn_id": "fixture",
                    "status": "completed", "stop_reason": None}) as transport:
            run_stage(public, public_id, "discover", progress=lambda message: None)
        self.assertEqual(transport.call_args.args[1].name, "research.activities.schema.json")
        old = self.fake_result(public, public_id)
        old["_context"] = fingerprint(public.profile(), public.trip(public_id)["brief"], [])
        with public.db:
            public.db.execute("UPDATE runs SET result=? WHERE id=?", (json.dumps(old), public.runs(public_id)[-1]["id"]))
        with patch("fluctour.research.run_stage", return_value=activity_dossier()) as stage:
            research(public, public_id, progress=lambda message: None)
        self.assertEqual(stage.call_args.args[2], "refresh")
        self.assertNotIn("activity_plans", old)


if __name__ == "__main__":
    unittest.main()
