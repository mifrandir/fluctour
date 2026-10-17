import json
import math
import signal
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fluctour.cli import constraints_for, view
from fluctour.jobs import enqueue, job_state
from fluctour.server import Handler, dispatch, serve
from fluctour.store import Store
from test_fluctour import dossier, option
from test_server import FakeSocket


REQUEST = {"destination": "Barcelona", "start_date": "2027-06-11", "end_date": "2027-06-14", "activities": ["swimming"]}


class ServerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        profile = self.store.profile()
        profile.update(home_airport="HKG", passport_nationality="PRIVATE_PASSPORT", policy_text="PRIVATE_POLICY")
        self.store.save_profile(profile)
        self.trip_id = self.store.create_trip("PRIVATE_NOTES")
        self.runner = Mock(spec=["wake", "cancel"])
        self.inference_patch = patch("fluctour.jobs.research", side_effect=AssertionError("No inference in API tests"))
        self.inference_patch.start()
        self.addCleanup(self.inference_patch.stop)

    def request(self, suffix, payload):
        body = json.dumps(payload).encode()
        path = f"/api/trips/{self.trip_id}/{suffix}"
        raw = (f"POST {path} HTTP/1.1\r\nHost: localhost:8765\r\nContent-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        connection = FakeSocket(raw)
        server = SimpleNamespace(server_port=8765, data_root=Path(self.temp.name), preview_lock=Lock(), job_runner=self.runner)
        Handler(connection, ("127.0.0.1", 12345), server)
        head, response = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(response)

    def completed_job(self):
        enqueue(self.store, self.trip_id, REQUEST)
        key = "job:" + self.trip_id
        state = json.loads(self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
        state.update(status="completed", stage="completed")
        with self.store.db:
            self.store.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(state), key))

    def save_options(self):
        result = dossier([option(id="cheap", total_cost=5000), option(id="costly", total_cost=9000)])
        result["_profile"] = "stale-profile"
        result["_feedback_id"] = 0
        run = self.store.begin_run(self.trip_id, "source_import")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0}, result=result)

    def test_free_text_filters_saved_alternatives_immediately_and_preserves_audit(self):
        self.save_options()
        text = "max HKD8000; PRIVATE_EMPLOYER_REQUIREMENT"
        status, data = self.request("feedback", {"text": text})
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in data["options"]], ["cheap"])
        self.assertEqual(data["constraints"], {"max_cost": 8000})
        self.assertEqual(data["feedback_effects"]["applied"], ["max HKD8000"])
        self.assertEqual(data["pending_feedback"], ["PRIVATE_EMPLOYER_REQUIREMENT"])
        self.assertEqual(self.store.feedback(self.trip_id)[0]["text"], text)
        self.assertEqual(self.store.feedback(self.trip_id)[0]["constraints"], {"max_cost": 8000})
        self.assertIn("Profile changed", " ".join(data["options"][0]["assessment"]["blockers"]))
        self.assertEqual(len(self.store.runs(self.trip_id)), 1)

    def test_cli_view_interprets_saved_text_and_does_not_claim_unapplied_clauses(self):
        self.store.add_feedback(self.trip_id, "no overnight flights; 7-12 km hikes; liked Strava activity 123", {})
        self.assertEqual(constraints_for(self.store, self.trip_id), {"no_overnight": True})
        data = view(self.store, self.trip_id)
        self.assertEqual(data["feedback_effects"]["applied"], ["no overnight flights"])
        self.assertEqual(data["pending_feedback"], ["7-12 km hikes", "liked Strava activity 123"])

    def test_structured_controls_override_conflicting_text_without_false_applied_claim(self):
        status, data = self.request("feedback", {"text": "no overnight flights", "constraints": {"no_overnight": False}})
        self.assertEqual(status, 200)
        self.assertEqual(data["constraints"], {"no_overnight": False})
        self.assertEqual(data["feedback_effects"]["applied"], [])
        self.assertEqual(data["pending_feedback"], ["no overnight flights"])
        status, data = self.request("feedback", {"text": "max 2 days leave", "constraints": {"max_leave": 1}})
        self.assertEqual(status, 200)
        self.assertIn("max 2 days leave", data["feedback_effects"]["applied"])

    def test_activity_delta_queues_public_enum_only_without_private_feedback(self):
        self.completed_job()
        text = "more hiking; PRIVATE_EMPLOYER_RULE; 7-12 km hikes"
        status, data = self.request("feedback", {"text": text})
        self.assertEqual(status, 200)
        self.assertTrue(data["feedback_effects"]["research_queued"])
        self.assertEqual(data["job"]["request"]["activities"], ["hiking", "swimming"])
        self.assertNotIn("PRIVATE_EMPLOYER_RULE", json.dumps(data["job"]))
        self.assertNotIn("7-12", json.dumps(data["job"]))
        self.assertEqual(data["feedback_effects"]["recognized_activity_changes"][0]["activity"], "hiking")
        self.assertIn("more hiking", data["pending_feedback"])
        self.assertEqual(self.store.feedback(self.trip_id)[0]["text"], text)
        self.runner.wake.assert_called_once_with()

    def test_saved_budget_currency_cannot_silently_rebind_after_profile_change(self):
        for text, controls in (("under HKD8000", {}), ("max 9000", {}), ("Prefer quiet hotels", {"max_cost": 10000})):
            status, data = self.request("feedback", {"text": text, "constraints": controls})
            self.assertEqual(status, 200)
        profile = self.store.profile()
        profile["currency"] = "USD"
        self.store.save_profile(profile)
        data = view(self.store, self.trip_id)
        self.assertNotIn("max_cost", data["constraints"])
        self.assertNotIn("under HKD8000", data["feedback_effects"]["applied"])
        self.assertNotIn("max 9000", data["feedback_effects"]["applied"])
        self.assertIn("under HKD8000", data["pending_feedback"])
        self.assertIn("max 9000", data["pending_feedback"])
        self.assertTrue(any("Saved HKD cost cap" in item for item in data["pending_feedback"]))

    def test_legacy_explicit_currency_cap_is_not_reinterpreted_as_another_currency(self):
        self.store.add_feedback(self.trip_id, "under HKD8000", {"max_cost": math.nextafter(8000, -math.inf)})
        profile = self.store.profile()
        profile["currency"] = "USD"
        self.store.save_profile(profile)
        self.assertNotIn("max_cost", constraints_for(self.store, self.trip_id))

    def test_feedback_audit_and_currency_provenance_commit_atomically(self):
        self.store.db.execute("""CREATE TRIGGER reject_feedback_context BEFORE INSERT ON settings
            WHEN NEW.key LIKE 'feedback_context:%' BEGIN SELECT RAISE(ABORT,'PRIVATE_STORAGE_ERROR'); END""")
        status, response = self.request("feedback", {"text": "max 8000"})
        self.assertEqual(status, 500)
        self.assertEqual(self.store.feedback(self.trip_id), [])
        self.assertNotIn("PRIVATE_STORAGE_ERROR", json.dumps(response))

    def test_already_requested_activity_and_unsupported_negative_context_stay_pending(self):
        self.completed_job()
        status, data = self.request("feedback", {"text": "more swimming"})
        self.assertEqual(status, 200)
        self.assertFalse(data["feedback_effects"]["research_queued"])
        self.assertEqual(data["job"]["status"], "completed")
        self.assertEqual(data["pending_feedback"], ["more swimming"])
        status, data = self.request("feedback", {"text": "Do not include hiking and swimming"})
        self.assertEqual(status, 200)
        self.assertEqual(data["job"]["request"]["activities"], ["swimming"])
        self.assertFalse(data["feedback_effects"]["research_queued"])

    def test_active_job_keeps_delta_pending_and_does_not_claim_a_changed_request(self):
        enqueue(self.store, self.trip_id, REQUEST)
        status, data = self.request("feedback", {"text": "more hiking"})
        self.assertEqual(status, 200)
        self.assertFalse(data["feedback_effects"]["research_queued"])
        self.assertIsNotNone(data["feedback_effects"]["research_error"])
        self.assertEqual(data["job"]["request"]["activities"], ["swimming"])

    def test_explicit_refresh_validated_and_forwarded_only_when_present(self):
        self.completed_job()
        with patch("fluctour.server.enqueue", wraps=enqueue) as queued:
            status, _ = self.request("research", {})
            self.assertEqual(status, 202)
            self.assertEqual(queued.call_args.kwargs, {})
        for invalid in ("true", 1, None, []):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.request("research", {"refresh": invalid})[0], 400)
        with patch("fluctour.server.enqueue", return_value=job_state(self.store, self.trip_id)) as queued:
            self.assertEqual(self.request("research", {"refresh": True})[0], 202)
            self.assertEqual(queued.call_args.kwargs, {"refresh": True})

    def test_cancel_calls_persistent_request_and_worker_cancel_without_wake(self):
        self.completed_job()
        def cancel(store, trip_id):
            key = "job:" + trip_id
            state = json.loads(store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
            state.update(status="cancelled", message="Research stopped.")
            with store.db:
                store.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(state), key))
        with patch("fluctour.jobs.request_cancel", side_effect=cancel, create=True) as request_cancel:
            status, data = self.request("cancel", {})
        self.assertEqual(status, 202)
        self.assertEqual(data["job"]["status"], "cancelled")
        self.assertEqual(request_cancel.call_args.args[1], self.trip_id)
        self.runner.cancel.assert_called_once_with(self.trip_id)
        self.runner.wake.assert_not_called()
        self.assertEqual(self.request("cancel", {"extra": True})[0], 400)

    def test_sigterm_uses_graceful_worker_close_and_restores_previous_handler(self):
        fake_server = Mock()
        fake_server.__enter__ = Mock(return_value=fake_server)
        fake_server.__exit__ = Mock(return_value=False)
        runner = Mock()
        registered = []
        def set_handler(signum, handler):
            registered.append((signum, handler))
        def interrupt():
            registered[0][1](signal.SIGTERM, None)
        fake_server.serve_forever.side_effect = interrupt
        with patch("fluctour.server.ThreadingHTTPServer", return_value=fake_server), \
                patch("fluctour.server.Runner", return_value=runner), \
                patch("fluctour.server.signal.getsignal", return_value=signal.SIG_DFL), \
                patch("fluctour.server.signal.signal", side_effect=set_handler), patch("builtins.print"):
            serve(self.temp.name)
        runner.close.assert_called_once_with()
        self.assertEqual(registered[-1], (signal.SIGTERM, signal.SIG_DFL))


if __name__ == "__main__":
    unittest.main()
