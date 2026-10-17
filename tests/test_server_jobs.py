import json
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fluctour.activities import import_activities
from fluctour.jobs import _public_context, enqueue
from fluctour.server import Handler
from fluctour.store import Store
from test_server import FakeSocket


PUBLIC_REQUEST = {"destination": "Barcelona", "start_date": "2027-06-11",
                  "end_date": "2027-06-14", "activities": ["swimming", "hiking"]}


class ServerJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.runner = Mock(spec=["wake"])
        inference_patch = patch("fluctour.jobs.research")
        self.inference = inference_patch.start()
        self.addCleanup(inference_patch.stop)
        runner_patch = patch("fluctour.server.Runner")
        self.runner_constructor = runner_patch.start()
        self.addCleanup(runner_patch.stop)

    def tearDown(self):
        self.inference.assert_not_called()
        self.runner_constructor.assert_not_called()

    def request(self, path="/api/trips", method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Host": "localhost:8765"}
        if method == "POST":
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        raw = f"{method} {path} HTTP/1.1\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
        socket = FakeSocket(raw.encode("ascii") + body)
        server = SimpleNamespace(server_port=8765, data_root=Path(self.temp.name),
                                 preview_lock=Lock(), job_runner=self.runner)
        Handler(socket, ("127.0.0.1", 12345), server)
        head, response = bytes(socket.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(response)

    def create(self, brief="PRIVATE_TRAVEL_BRIEF"):
        return self.request(method="POST", payload={"brief": brief, "research": PUBLIC_REQUEST})

    def test_successful_create_persists_public_job_and_wakes_attached_runner(self):
        status, response = self.create()
        self.assertEqual(status, 201)
        self.assertEqual(response["trip"]["brief"], "PRIVATE_TRAVEL_BRIEF")
        self.assertEqual(response["job"]["status"], "queued")
        self.assertEqual(response["job"]["stage"], "queued")
        self.assertEqual(response["job"]["attempt"], 0)
        self.assertEqual(response["job"]["request"], PUBLIC_REQUEST | {"activities": ["hiking", "swimming"]})
        self.runner.wake.assert_called_once_with()
        trip_id = response["trip"]["id"]
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(reopened.trip(trip_id)["brief"], "PRIVATE_TRAVEL_BRIEF")
            saved = json.loads(reopened.db.execute("SELECT value FROM settings WHERE key=?",
                                                   ("job:" + trip_id,)).fetchone()[0])
            self.assertEqual(saved["request"], response["job"]["request"])
            self.assertEqual(reopened.runs(trip_id), [])
        finally:
            reopened.db.close()

    def test_existing_private_notes_are_saved_without_entering_public_request(self):
        trip_id = self.store.create_trip("Original private notes")
        status, response = self.request(f"/api/trips/{trip_id}/research", "POST",
            {"research": PUBLIC_REQUEST, "brief": "Updated PRIVATE_NOTES"})
        self.assertEqual(status, 202)
        self.assertEqual(response["trip"]["brief"], "Updated PRIVATE_NOTES")
        self.assertEqual(self.store.trip(trip_id)["brief"], "Updated PRIVATE_NOTES")
        self.assertNotIn("PRIVATE_NOTES", json.dumps(response["job"]))
        status, _ = self.request(f"/api/trips/{trip_id}/research", "POST",
            {"research": PUBLIC_REQUEST, "brief": " "})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.trip(trip_id)["brief"], "Updated PRIVATE_NOTES")

    def test_invalid_request_rejects_before_partial_trip_or_job_creation(self):
        bad_research = [None, [], {}, PUBLIC_REQUEST | {"destination": ""},
                        PUBLIC_REQUEST | {"activities": ["private-policy"]},
                        PUBLIC_REQUEST | {"start_date": "2027-02-30"},
                        PUBLIC_REQUEST | {"end_date": "2027-06-10"},
                        PUBLIC_REQUEST | {"passport": "PRIVATE_IDENTITY"}]
        payloads = [{"brief": "A valid brief", "research": research} for research in bad_research]
        payloads += [{"brief": " ", "research": PUBLIC_REQUEST},
                     {"brief": 42, "research": PUBLIC_REQUEST},
                     {"brief": "Trip", "research": PUBLIC_REQUEST, "run_unbounded": True}]
        for payload in payloads:
            with self.subTest(payload=payload):
                status, response = self.request(method="POST", payload=payload)
                self.assertEqual(status, 400)
                self.assertIn("error", response)
                self.assertEqual(self.store.trips(), [])
                self.assertEqual(self.store.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'job:%'").fetchone()[0], 0)
                self.runner.wake.assert_not_called()
        queued_ids = []
        for index in range(20):
            trip_id = self.store.create_trip(f"Queued trip {index}")
            enqueue(self.store, trip_id, PUBLIC_REQUEST)
            queued_ids.append(trip_id)
        status, response = self.create("Draft rejected by the full queue")
        self.assertEqual(status, 400)
        self.assertIn("queue is full", response["error"])
        self.assertEqual({trip["id"] for trip in self.store.trips()}, set(queued_ids))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'job:%'").fetchone()[0], 20)
        self.runner.wake.assert_not_called()

    def test_snapshot_exposes_saved_progress_and_retry_reuses_saved_public_request(self):
        status, response = self.create()
        self.assertEqual(status, 201)
        trip_id = response["trip"]["id"]
        key = "job:" + trip_id
        saved = json.loads(self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
        saved.update(status="paused", stage="challenge", attempt=1,
                     message="Saved research is ready to retry.", public_trip_id="PRIVATE_WORKER_TRIP_ID")
        with self.store.db:
            self.store.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(saved), key))
        self.runner.reset_mock()
        status, snapshot = self.request("/api/trips/" + trip_id)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["job"]["status"], "paused")
        self.assertEqual(snapshot["job"]["stage"], "challenge")
        self.assertEqual(snapshot["job"]["attempt"], 1)
        self.assertEqual(snapshot["job"]["message"], "Saved research is ready to retry.")
        self.assertNotIn("public_trip_id", snapshot["job"])
        self.runner.wake.assert_not_called()
        status, retried = self.request("/api/trips/" + trip_id + "/research", "POST", {})
        self.assertEqual(status, 202)
        self.assertEqual(retried["job"]["status"], "queued")
        self.assertEqual(retried["job"]["request"], snapshot["job"]["request"])
        self.assertEqual(retried["job"]["attempt"], 1)
        self.assertEqual(len(self.store.trips()), 1)
        self.runner.wake.assert_called_once_with()
        saved_after = json.loads(self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
        self.assertEqual(saved_after["public_trip_id"], "PRIVATE_WORKER_TRIP_ID")

    def test_public_job_request_and_generated_context_exclude_private_saved_data(self):
        profile = self.store.profile()
        profile.update(home_airport="HKG", passport_nationality="PRIVATE_PASSPORT",
                       residency="PRIVATE_RESIDENCY", policy_text="PRIVATE_EMPLOYER_POLICY",
                       policy_rules={"provenance": "PRIVATE_EMPLOYER_RULE"})
        self.store.save_profile(profile)
        import_activities(self.store, json.dumps([{
            "id": 101, "sport_type": "Hike", "start_date": "2026-10-01T00:00:00Z",
            "name": "PRIVATE_ACTIVITY_NAME", "location_city": "PRIVATE_ACTIVITY_CITY",
            "distance": 7000, "moving_time": 7000,
        }]), format="json")
        status, response = self.create()
        self.assertEqual(status, 201)
        trip_id = response["trip"]["id"]
        self.store.add_feedback(trip_id, "PRIVATE_FEEDBACK", {})
        public_brief, public_profile = _public_context(self.store, response["job"]["request"])
        exported = json.dumps(response["job"]) + public_brief + json.dumps(public_profile)
        for marker in ("PRIVATE_TRAVEL_BRIEF", "PRIVATE_PASSPORT", "PRIVATE_RESIDENCY",
                       "PRIVATE_EMPLOYER_POLICY", "PRIVATE_EMPLOYER_RULE", "PRIVATE_ACTIVITY_NAME",
                       "PRIVATE_ACTIVITY_CITY", "PRIVATE_FEEDBACK"):
            self.assertNotIn(marker, exported)
        self.assertIn("Barcelona", public_brief)
        self.assertIn("hiking", public_brief)
        self.assertIn("swimming", public_brief)
        self.assertEqual(public_profile["home_airport"], "HKG")
        self.assertIsNone(public_profile["passport_nationality"])
        self.assertEqual(public_profile["policy_text"], "")


if __name__ == "__main__":
    unittest.main()
