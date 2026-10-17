import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from fluctour.cli import main, view
from fluctour.planning import leave_required
from fluctour.research import BudgetPause, admit, budget_state, recover, research
from fluctour.service import attach_public_research
from fluctour.store import Store
from test_fluctour import dossier, option


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.trip = self.store.create_trip("Local private request")

    def complete(self, store, trip, result=None, input_tokens=100, output_tokens=50, stage="discover"):
        run = store.begin_run(trip, stage)
        store.finish_run(run, "completed", {"input_tokens": input_tokens, "output_tokens": output_tokens},
                         result=result or dossier([]))
        return run

    def test_nested_public_store_shares_monthly_limit_and_usage(self):
        profile = self.store.profile()
        profile["monthly_token_budget"] = 100000
        self.store.save_profile(profile)
        public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(public.db.close)
        trip = public.create_trip("Public route")
        self.complete(public, trip, input_tokens=60000, output_tokens=0)
        self.complete(self.store, self.trip, input_tokens=40000, output_tokens=0)
        for store, trip_id in ((self.store, self.trip), (public, trip)):
            state = budget_state(store, trip_id)
            self.assertEqual(state["monthly_used_or_reserved"], 100000)
            self.assertEqual(state["monthly_limit"], 100000)
            with self.assertRaises(BudgetPause):
                admit(store, trip_id)

    def test_nested_research_cannot_acquire_parallel_worker_lock(self):
        public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(public.db.close)
        with self.store.worker_lock():
            with self.assertRaisesRegex(ValueError, "Another research worker"):
                with public.worker_lock():
                    pass

    def test_multiple_public_jobs_share_the_parent_trip_allowance(self):
        public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(public.db.close)
        first = public.create_trip("First public source batch")
        second = public.create_trip("Second public source batch")
        for trip in (first, second):
            public.set_budget_owner(trip, self.trip)
            self.complete(public, trip, input_tokens=60000, output_tokens=0)
        self.assertEqual(budget_state(self.store, self.trip)["job_used_or_reserved"], 120000)
        self.assertEqual(budget_state(public, second)["job_used_or_reserved"], 120000)
        self.assertEqual(budget_state(self.store, self.trip)["sessions_used"], 2)
        with self.assertRaises(BudgetPause):
            admit(public, second)

    def test_recovery_replays_known_usage_instead_of_unknown_reservation(self):
        run = self.store.begin_run(self.trip, "discover")
        self.store.finish_run(run, "interrupted", {})
        directory = self.store.root / "research"
        directory.mkdir()
        (directory / (run + ".json")).write_text(json.dumps(dossier([])))
        (directory / (run + ".events.jsonl")).write_text(json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 120000, "output_tokens": 10000,
                                                   "cached_input_tokens": 80000}}) + "\n")
        recover(self.store, self.trip)
        self.assertEqual(budget_state(self.store, self.trip)["job_used_or_reserved"], 130000)
        with self.assertRaises(BudgetPause):
            admit(self.store, self.trip)

    def test_budget_pause_preserves_and_returns_current_dossier(self):
        result = dossier([])
        result["_stages"] = ["discover"]
        self.complete(self.store, self.trip, result, input_tokens=114557, output_tokens=0)
        output = []
        returned = research(self.store, self.trip, progress=output.append)
        self.assertEqual(returned["summary"], result["summary"])
        self.assertTrue(any("paused" in line for line in output))
        self.assertEqual(len(self.store.runs(self.trip)), 1)

    def test_public_composition_uses_local_profile_and_immediate_feedback(self):
        profile = self.store.profile()
        profile.update(home_airport="HKG", work_end="17:00", passport_nationality="German",
                       residency="Hong Kong", policy_text="Private rule " + "A" * 10000,
                       policy_confirmed=True)
        self.store.save_profile(profile)
        public = Store(self.store.root / "public-research")
        self.addCleanup(public.db.close)
        trip = public.create_trip("Public airport routes")
        self.complete(public, trip, dossier([option()]))
        attach_public_research(self.store, self.trip, trip)
        state = view(self.store, self.trip)
        self.assertEqual(state["options"][0]["assessment"]["leave"]["days"], 1)
        self.assertFalse(any("Home airport" in b for b in state["options"][0]["assessment"]["blockers"]))
        self.assertEqual(state["budget"]["sessions_used"], 0)
        self.assertEqual(state["budget"]["job_used_or_reserved"], 0)
        self.assertEqual(state["budget"]["monthly_used_or_reserved"], 150)
        self.assertLessEqual(len(state["research"]["options"][0]["evidence"][0]["policy_excerpt"]), 2000)
        attach_public_research(self.store, self.trip, trip)
        self.assertEqual(len(self.store.runs(self.trip)), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir", self.temp.name, "feedback", self.trip,
                                   "--max-cost", "1000"]), 0)
        self.assertEqual(view(self.store, self.trip)["options"], [])
        self.assertEqual(budget_state(self.store, self.trip)["monthly_used_or_reserved"], 150)

    def test_business_workdays_are_not_presented_as_personal_leave(self):
        value = option(legs=[{"paid_by": "company"}])
        self.assertIsNone(leave_required(value, self.store.profile())["days"])


if __name__ == "__main__":
    unittest.main()
