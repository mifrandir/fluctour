import tempfile
import unittest
from threading import Event, Thread
from unittest.mock import patch

from fluctour.jobs import Runner, _read, _save, enqueue, job_state, request_cancel
from fluctour.research import ResearchCancelled
from fluctour.store import Store
from test_fluctour import dossier, option


REQUEST = {"destination": "Barcelona", "start_date": "2027-06-11",
           "end_date": "2027-06-14", "activities": ["swimming"]}


class JobControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        profile = self.store.profile()
        profile["home_airport"] = "HKG"
        self.store.save_profile(profile)
        self.trip_id = self.store.create_trip("Private trip request")
        self.runner = Runner(self.temp.name)
        self.addCleanup(self.runner.close)

    @staticmethod
    def save_checkpoint(store, trip_id):
        result = dossier([option(id="saved-option", title="Saved research")])
        run_id = store.begin_run(trip_id, "discover")
        store.finish_run(run_id, "completed", {"input_tokens": 100, "output_tokens": 20}, result=result)
        return result

    def test_completed_refresh_queues_and_reuses_checkpoint_identity(self):
        enqueue(self.store, self.trip_id, REQUEST)
        calls = []

        def research(public, public_id, **kwargs):
            calls.append((public_id, kwargs["refresh"]))
            if kwargs["refresh"]:
                self.assertIsNotNone(public.latest_result(public_id))
            return self.save_checkpoint(public, public_id)

        with patch("fluctour.jobs.research", side_effect=research):
            self.runner._run(self.runner._next())
            completed = _read(self.store, self.trip_id)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(enqueue(self.store, self.trip_id, REQUEST)["status"], "completed")
            self.assertIsNone(self.runner._next())
            refreshed = enqueue(self.store, self.trip_id, REQUEST, refresh=True)
            self.assertEqual(refreshed["status"], "queued")
            self.assertEqual(refreshed["attempt"], 1)
            self.assertEqual(_read(self.store, self.trip_id)["public_trip_id"], completed["public_trip_id"])
            self.runner._run(self.runner._next())
        self.assertEqual(calls, [(completed["public_trip_id"], False), (completed["public_trip_id"], True)])
        self.assertEqual(job_state(self.store, self.trip_id)["attempt"], 2)
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "completed")

    def test_active_jobs_refuse_changes_and_refresh_without_mutation(self):
        enqueue(self.store, self.trip_id, REQUEST)
        with patch("fluctour.jobs.research") as research:
            for status in ("queued", "researching", "cancelling"):
                with self.subTest(status=status):
                    state = _read(self.store, self.trip_id)
                    state["status"] = status
                    _save(self.store, self.trip_id, state)
                    before = _read(self.store, self.trip_id)
                    for request, refresh in ((REQUEST | {"destination": "Kyoto"}, False), (REQUEST, True)):
                        with self.assertRaisesRegex(ValueError, "Stop the current research"):
                            enqueue(self.store, self.trip_id, request, refresh=refresh)
                        self.assertEqual(_read(self.store, self.trip_id), before)
                    self.assertEqual(enqueue(self.store, self.trip_id, REQUEST)["status"], status)
            research.assert_not_called()

    def test_queued_stop_prevents_claim_and_preserves_saved_options(self):
        self.save_checkpoint(self.store, self.trip_id)
        enqueue(self.store, self.trip_id, REQUEST)
        with patch("fluctour.jobs.research") as research:
            stopped = request_cancel(self.store, self.trip_id)
            self.assertEqual(stopped["status"], "cancelled")
            self.assertIsNone(self.runner._next())
            self.assertEqual(request_cancel(self.store, self.trip_id), stopped)
            research.assert_not_called()
        self.assertEqual(self.store.latest_result(self.trip_id)["options"][0]["id"], "saved-option")
        self.assertEqual(job_state(self.store, self.trip_id)["attempt"], 0)

    def test_stop_after_claim_before_event_installation_prevents_inference(self):
        enqueue(self.store, self.trip_id, REQUEST)
        claimed = self.runner._next()
        self.assertEqual(claimed, self.trip_id)
        self.assertEqual(request_cancel(self.store, self.trip_id)["status"], "cancelling")
        self.runner.cancel(self.trip_id)
        with patch("fluctour.jobs.research") as research:
            self.runner._run(claimed)
            research.assert_not_called()
        self.assertEqual(job_state(self.store, self.trip_id)["status"], "cancelled")
        self.assertIsNone(_read(self.store, self.trip_id)["public_trip_id"])

    def test_inflight_stop_signals_transport_and_publishes_saved_checkpoint(self):
        self.check_inflight_stop(return_late_result=False)

    def test_late_result_cannot_complete_cancelled_job(self):
        self.check_inflight_stop(return_late_result=True)

    def check_inflight_stop(self, *, return_late_result):
        enqueue(self.store, self.trip_id, REQUEST)
        claimed = self.runner._next()
        entered, observed_cancel = Event(), Event()
        received_events = []

        def research(public, public_id, **kwargs):
            result = self.save_checkpoint(public, public_id)
            received_events.append(kwargs["cancel_event"])
            entered.set()
            if not kwargs["cancel_event"].wait(3):
                raise TimeoutError("Test did not receive cancellation")
            observed_cancel.set()
            if return_late_result:
                return result
            raise ResearchCancelled("Stopped")

        worker = Thread(target=self.runner._run, args=(claimed,), daemon=True)
        with patch("fluctour.jobs.research", side_effect=research) as mocked:
            worker.start()
            try:
                self.assertTrue(entered.wait(3), "Worker did not enter the mocked transport")
                self.assertEqual(request_cancel(self.store, self.trip_id)["status"], "cancelling")
                self.runner.cancel(self.trip_id)
                self.assertTrue(observed_cancel.wait(3), "Cancellation did not reach the transport")
            finally:
                self.runner.cancel(self.trip_id)
                worker.join(timeout=4)
            self.assertFalse(worker.is_alive(), "Cancelled worker did not finish")
            mocked.assert_called_once()
        self.assertTrue(received_events[0].is_set())
        state = job_state(self.store, self.trip_id)
        self.assertEqual((state["status"], state["stage"]), ("cancelled", "cancelled"))
        self.assertIn("Partial options are saved", state["message"])
        self.assertEqual(self.store.latest_result(self.trip_id)["options"][0]["id"], "saved-option")
        self.assertIn("saved-option", self.store.plans(self.trip_id))
        self.assertIsNone(self.runner._active_trip_id)


if __name__ == "__main__":
    unittest.main()
