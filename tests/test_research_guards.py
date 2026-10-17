import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from fluctour.research import (BudgetPause, ResearchCancelled, SESSION_RESERVATION, STAGE_TOKEN_LIMIT,
                              budget_state, research, run_stage)
from fluctour.store import Store
from test_fluctour import dossier, option


def outcome(**changes):
    return {"result": dossier([]), "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50},
            "usage_complete": True, "thread_id": "fixture-thread", "turn_id": "fixture-turn",
            "status": "completed", "stop_reason": None, **changes}


class ResearchGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.trip_id = self.store.create_trip("Private local brief")
        self.public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(self.public.db.close)
        profile = self.public.profile()
        profile["home_airport"] = "HKG"
        self.public.save_profile(profile)
        self.public_id = self.public.create_trip("Public-only route request")
        self.public.set_budget_owner(self.public_id, self.trip_id)
        preflight = patch("fluctour.research.app_server_command", return_value=["fake", "app-server"])
        self.preflight = preflight.start()
        self.addCleanup(preflight.stop)

    def checkpoint(self):
        saved = dossier([]) | {"summary": "Previously saved public alternatives"}
        run = self.public.begin_run(self.public_id, "source_import")
        self.public.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0}, result=saved)
        return saved

    def test_live_usage_is_charged_before_interruption_and_unknown_tail_preserves_checkpoint(self):
        saved = self.checkpoint()
        live = []
        def interrupted(prompt, schema, workspace, limits, **callbacks):
            self.assertEqual(limits["token_limit"], STAGE_TOKEN_LIMIT)
            self.assertLess(limits["finalize_token_threshold"], limits["token_limit"])
            for counts in ({"input_tokens": 29000, "output_tokens": 2000, "cached_input_tokens": 1000},
                           {"input_tokens": 34000, "output_tokens": 4000, "cached_input_tokens": 1000}):
                callbacks["on_usage"](counts)
                snapshot = budget_state(self.store, self.trip_id)
                live.append(snapshot["job_used_or_reserved"])
                self.assertGreaterEqual(snapshot["job_used_or_reserved"],
                                        counts["input_tokens"] + counts["output_tokens"] + SESSION_RESERVATION)
                self.assertEqual(self.public.runs(self.public_id)[-1]["status"], "running")
            return outcome(result=None, usage=counts, usage_complete=False,
                           status="interrupted", stop_reason="token_limit")
        with patch("fluctour.research.run_codex", side_effect=interrupted):
            with self.assertRaises(BudgetPause):
                run_stage(self.public, self.public_id, "challenge", progress=lambda _: None)
        self.assertEqual(live, [81000, 88000])
        self.assertEqual(self.public.latest_result(self.public_id), saved)
        interrupted_run = self.public.runs(self.public_id)[-1]
        self.assertEqual(interrupted_run["status"], "interrupted")
        self.assertIsNone(interrupted_run["input_tokens"])
        self.assertEqual(interrupted_run["observed_input_tokens"], 34000)
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 88000)

    def test_shared_allowance_callback_requests_stop_before_final_result(self):
        with self.store.db:
            self.store.db.execute("UPDATE trips SET budget=60000 WHERE id=?", (self.trip_id,))
        def limited(prompt, schema, workspace, limits, **callbacks):
            self.assertEqual(limits["token_limit"], 10000)
            self.assertFalse(callbacks["on_usage"]({"input_tokens": 10000, "output_tokens": 1000, "cached_input_tokens": 0}))
            return outcome(result=None, usage={"input_tokens": 10000, "output_tokens": 1000},
                           usage_complete=False, status="interrupted", stop_reason="token_limit")
        with patch("fluctour.research.run_codex", side_effect=limited):
            with self.assertRaises(BudgetPause):
                run_stage(self.public, self.public_id, "discover", progress=lambda _: None)
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 61000)

    def test_cancelled_completed_stage_keeps_checkpoint_and_stops_following_stages(self):
        cancel = Event()
        def completed(prompt, schema, workspace, limits, **callbacks):
            callbacks["on_usage"]({"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50})
            cancel.set()
            return outcome()
        with patch("fluctour.research.run_codex", side_effect=completed) as transport:
            with self.assertRaises(ResearchCancelled):
                research(self.public, self.public_id, steps=3, progress=lambda _: None, cancel_event=cancel)
        transport.assert_called_once()
        self.assertEqual(len(self.public.runs(self.public_id)), 1)
        self.assertEqual(self.public.latest_result(self.public_id)["_stages"], ["discover"])

    def test_cancellation_before_launch_and_failed_preflight_never_fall_back_to_exec(self):
        cancel = Event()
        cancel.set()
        with patch("fluctour.research.run_codex") as transport, patch("fluctour.research.codex_command") as legacy:
            with self.assertRaises(ResearchCancelled):
                run_stage(self.public, self.public_id, "discover", cancel_event=cancel)
            self.preflight.assert_not_called()
            transport.assert_not_called()
            legacy.assert_not_called()
        with patch("fluctour.research.app_server_command", side_effect=ValueError("Unavailable safe transport")), \
                patch("fluctour.research.run_codex") as transport, patch("fluctour.research.codex_command") as legacy:
            with self.assertRaises(ValueError):
                run_stage(self.public, self.public_id, "discover")
            transport.assert_not_called()
            legacy.assert_not_called()
        self.assertEqual(self.public.runs(self.public_id), [])
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 0)

    def test_completed_turn_without_final_usage_retains_observed_lower_bound_and_unknown_fields(self):
        def unobserved_tail(prompt, schema, workspace, limits, **callbacks):
            callbacks["on_usage"]({"input_tokens": 200, "output_tokens": 40, "cached_input_tokens": 50})
            return outcome(usage={}, usage_complete=False)
        with patch("fluctour.research.run_codex", side_effect=unobserved_tail):
            run_stage(self.public, self.public_id, "discover", progress=lambda _: None)
        run = self.public.runs(self.public_id)[0]
        self.assertEqual(run["status"], "completed")
        self.assertIsNone(run["input_tokens"])
        self.assertIsNone(run["output_tokens"])
        self.assertEqual(run["observed_input_tokens"], 200)
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 50240)

    def test_home_airport_contradiction_cannot_replace_saved_dossier(self):
        saved = self.checkpoint()
        legs = [{"id": "outbound", "origin": "LHR", "destination": "NRT", "departure": None, "arrival": None,
                 "carrier": "Unconfirmed", "cabin": "unknown", "paid_by": "unknown"},
                {"id": "return", "origin": "NRT", "destination": "LHR", "departure": None, "arrival": None,
                 "carrier": "Unconfirmed", "cabin": "unknown", "paid_by": "unknown"}]
        bad = dossier([option(legs=legs, departure=None, destination_arrival=None,
                              return_departure=None, return_arrival=None, usable_hours=None)])
        with patch("fluctour.research.run_codex", return_value=outcome(result=bad)):
            with self.assertRaisesRegex(ValueError, "home airport"):
                run_stage(self.public, self.public_id, "challenge", progress=lambda _: None)
        self.assertEqual(self.public.latest_result(self.public_id), saved)
        self.assertEqual(self.public.runs(self.public_id)[-1]["status"], "failed")
        self.assertEqual(self.public.runs(self.public_id)[-1]["input_tokens"], 100)
        self.assertEqual(self.public.runs(self.public_id)[-1]["output_tokens"], 20)
        self.assertEqual(budget_state(self.store, self.trip_id)["job_used_or_reserved"], 120)

    def test_transport_instruction_layers_are_captured_before_a_transport_failure(self):
        import json
        from fluctour.codex_transport import BASE_INSTRUCTIONS, DEVELOPER_INSTRUCTIONS
        def failed(*args, **kwargs):
            row = self.public.runs(self.public_id)[-1]
            progress = self.public.db.execute("SELECT value FROM settings WHERE key=?", ("progress:" + row["id"],)).fetchone()
            harness = json.loads(progress[0])["harness"]
            self.assertEqual(harness["prompt_layers"], {"base": BASE_INSTRUCTIONS, "developer": DEVELOPER_INSTRUCTIONS})
            self.assertNotIn("finalize_requested", harness)
            raise OSError("Fixture transport failed before a reply")
        with patch("fluctour.research.run_codex", side_effect=failed):
            with self.assertRaises(OSError):
                run_stage(self.public, self.public_id, "discover", progress=lambda _: None)

    def test_activity_contract_is_required_even_if_transport_returns_legacy_json(self):
        with self.public.db:
            self.public.db.execute("INSERT INTO settings VALUES('structured_activity_plans','true')")
        with patch("fluctour.research.run_codex", return_value=outcome(result=dossier([]))):
            with self.assertRaisesRegex(ValueError, "activity plans"):
                run_stage(self.public, self.public_id, "discover", progress=lambda _: None)
        self.assertIsNone(self.public.latest_result(self.public_id))


if __name__ == "__main__":
    unittest.main()
