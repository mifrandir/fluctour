import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fluctour.harness import inspect_harness
from fluctour.codex_transport import BASE_INSTRUCTIONS, DEVELOPER_INSTRUCTIONS, FINALIZE_INSTRUCTIONS
from fluctour.jobs import enqueue
from fluctour.research import build_prompt, run_stage
from fluctour.store import Store


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        profile = self.store.profile()
        profile.update(passport_nationality="PRIVATE_PASSPORT", residency="PRIVATE_RESIDENCY",
                       policy_text="PRIVATE_POLICY")
        self.store.save_profile(profile)
        self.trip_id = self.store.create_trip("PRIVATE_BRIEF")
        self.public = Store(Path(self.temp.name) / "public-research")
        self.addCleanup(self.public.db.close)
        self.public_id = self.public.create_trip("Public Barcelona hiking request")
        self.public.set_budget_owner(self.public_id, self.trip_id)

    def saved_run(self, captured=None, stage="discover"):
        run_id = self.public.begin_run(self.public_id, stage)
        self.public.finish_run(run_id, "completed", {"input_tokens": 100, "output_tokens": 20})
        if captured is not None:
            directory = self.public.root / "research"
            directory.mkdir(exist_ok=True)
            target = directory / (run_id + ".prompt.txt")
            target.write_text(captured)
            target.chmod(0o600)
            self.public.save_progress(run_id, {"harness": {
                "prompt_sha256": hashlib.sha256(captured.encode()).hexdigest(),
                "timeout_seconds": 600, "schema_name": "research.activities.schema.json",
                "schema_sha256": "a" * 64, "cli_settings": {"sandbox": "read-only"}}})
        return run_id

    def test_current_templates_and_settings_without_research_or_private_state(self):
        before = self.store.db.total_changes
        with patch("fluctour.research.subprocess.Popen", side_effect=AssertionError("No inference")):
            result = inspect_harness(self.store, self.trip_id)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(result["runs"], [])
        self.assertIsNone(result["current_public_request"])
        self.assertIsNone(result["current_templates"]["public_request_brief"])
        self.assertIn("Treat brief", result["current_templates"]["instructions"])
        self.assertIn("activity_plans", result["current_templates"]["activity_plan_instructions"])
        self.assertEqual(result["execution_settings"]["default_timeout_seconds"], 600)
        self.assertEqual(result["execution_settings"]["cli_default_timeout_seconds"], 900)
        self.assertEqual(result["execution_settings"]["sandbox"], "read-only")
        self.assertEqual(result["execution_settings"]["web_search"], "live")
        self.assertEqual(result["execution_settings"]["model_policy"], "Codex CLI default (not pinned)")
        self.assertEqual(result["execution_settings"]["transport"], "app-server")
        self.assertFalse(result["execution_settings"]["ignore_user_config"])
        self.assertTrue(result["execution_settings"]["configuration_verification_required"])
        self.assertNotIn("configuration_verified", result["execution_settings"])
        self.assertEqual(result["execution_settings"]["observed_token_limit"], 30000)
        self.assertEqual(result["execution_settings"]["finalize_token_threshold"], 20000)
        self.assertIn("in addition to observed usage", result["execution_settings"]["reservation_note"])
        self.assertIn("billing can overshoot", result["execution_settings"]["token_limit_note"])
        self.assertEqual(result["current_templates"]["base_instructions"], BASE_INSTRUCTIONS)
        self.assertEqual(result["current_templates"]["developer_instructions"], DEVELOPER_INSTRUCTIONS)
        self.assertEqual(result["current_templates"]["finalize_instructions"], FINALIZE_INSTRUCTIONS)
        self.assertIn("activity_plans", result["current_schemas"]["activities"]["required"])
        serialized = json.dumps(result)
        for marker in ("PRIVATE_BRIEF", "PRIVATE_POLICY", "PRIVATE_PASSPORT", "PRIVATE_RESIDENCY", self.temp.name):
            self.assertNotIn(marker, serialized)

    def test_current_public_request_builder_uses_explicit_request_without_private_context(self):
        profile = self.store.profile()
        profile["home_airport"] = "HKG"
        self.store.save_profile(profile)
        request = {"destination": "Barcelona", "start_date": "2027-06-11", "end_date": "2027-06-14", "activities": ["hiking"]}
        enqueue(self.store, self.trip_id, request)
        before = self.store.db.total_changes
        result = inspect_harness(self.store, self.trip_id)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(result["current_public_request"], request)
        brief = result["current_templates"]["public_request_brief"]
        for marker in ("Barcelona", "2027-06-11", "2027-06-14", "HKG", "hiking"):
            self.assertIn(marker, brief)
        for marker in ("PRIVATE_BRIEF", "PRIVATE_POLICY", "PRIVATE_PASSPORT", "PRIVATE_RESIDENCY"):
            self.assertNotIn(marker, brief)
        self.assertEqual(self.store.runs(self.trip_id), [])
        profile["home_airport"] = None
        self.store.save_profile(profile)
        missing_airport = inspect_harness(self.store, self.trip_id)
        self.assertIsNone(missing_airport["current_templates"]["public_request_brief"])
        self.assertEqual(missing_airport["current_public_request"], request)

    def test_linked_run_exact_prompt_and_unknown_historical_prompt_are_distinct(self):
        historical = self.saved_run()
        captured = self.saved_run("Exact public prompt\nRoute evidence and prior-stage handoff.", stage="challenge")
        result = inspect_harness(self.store, self.trip_id)
        runs = {run["id"]: run for run in result["runs"]}
        self.assertEqual(runs[historical]["prompt"]["status"], "not_captured")
        self.assertIsNone(runs[historical]["prompt"]["text"])
        self.assertIsNone(runs[historical]["settings"])
        self.assertEqual(runs[captured]["prompt"]["status"], "captured")
        self.assertEqual(runs[captured]["prompt"]["text"], "Exact public prompt\nRoute evidence and prior-stage handoff.")
        self.assertEqual(runs[captured]["settings"]["timeout_seconds"], 600)
        self.assertEqual(runs[captured]["settings"]["transport"], "exec")
        self.assertEqual(runs[captured]["source"], "public_research")
        self.assertEqual(result["budget"]["job_used_or_reserved"], 240)

    def test_local_composition_and_unlinked_research_are_excluded(self):
        run = self.store.begin_run(self.trip_id, "local_composition")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0}, error="PRIVATE_LOCAL_ERROR")
        other = self.public.create_trip("Other public request")
        unlinked = self.public.begin_run(other, "discover")
        self.public.finish_run(unlinked, "failed", {}, error="PRIVATE_STDERR_TOKEN")
        linked = self.saved_run()
        result = inspect_harness(self.store, self.trip_id)
        self.assertEqual([row["id"] for row in result["runs"]], [linked])
        self.assertNotIn("PRIVATE_STDERR_TOKEN", json.dumps(result))

    def test_unknown_usage_stays_unknown(self):
        run = self.public.begin_run(self.public_id, "discover")
        self.public.record_usage(run, {"input_tokens": 400, "output_tokens": 80, "cached_input_tokens": 100})
        self.public.finish_run(run, "failed", {}, error="PRIVATE_ERROR")
        result = inspect_harness(self.store, self.trip_id)
        self.assertIsNone(result["runs"][0]["usage"]["input_tokens"])
        self.assertIsNone(result["runs"][0]["usage"]["output_tokens"])
        self.assertEqual(result["runs"][0]["usage"]["observed_input_tokens"], 400)
        self.assertEqual(result["runs"][0]["usage"]["observed_output_tokens"], 80)
        self.assertEqual(result["runs"][0]["usage"]["charged_or_reserved_tokens"], 50480)
        self.assertEqual(result["budget"]["job_used_or_reserved"], 50480)
        self.assertEqual(result["budget"]["unknown_usage_sessions"], 1)

    def test_capture_precedes_transport_and_matches_exact_prompt_with_private_permissions(self):
        with patch("fluctour.research.now", return_value="2026-10-16T00:00:00+00:00"):
            expected = build_prompt(self.public, self.public_id, "discover")
            def attempted_start(prompt, schema, workspace, limits, **kwargs):
                run_id = self.public.runs(self.public_id)[0]["id"]
                target = self.public.root / "research" / (run_id + ".prompt.txt")
                self.assertTrue(target.is_file())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(target.read_text(), expected)
                self.assertEqual(prompt, expected)
                self.assertEqual(Path(workspace), target.parent)
                self.assertEqual(limits["timeout_seconds"], 37)
                self.assertTrue(Path(schema).is_file() if not isinstance(schema, dict) else bool(schema))
                raise OSError("PRIVATE_TRANSPORT_ERROR")
            with patch("fluctour.research.app_server_command", return_value=["fake", "app-server"]), \
                    patch("fluctour.research.run_codex", side_effect=attempted_start):
                with self.assertRaises(OSError):
                    run_stage(self.public, self.public_id, "discover", timeout=37, progress=lambda _: None)
        result = inspect_harness(self.store, self.trip_id)
        self.assertEqual(result["runs"][0]["prompt"]["text"], expected)
        self.assertEqual(result["runs"][0]["settings"]["timeout_seconds"], 37)
        self.assertEqual(result["runs"][0]["settings"]["transport"], "app-server")
        self.assertEqual(result["runs"][0]["settings"]["observed_token_limit"], 30000)
        self.assertIsNone(result["runs"][0]["settings"]["configuration_verified"])
        self.assertNotIn("PRIVATE_TRANSPORT_ERROR", json.dumps(result))

    def test_captured_transport_metadata_is_allowlisted_and_failed_configuration_is_not_verified(self):
        run = self.saved_run("Exact public request")
        progress = self.public.progress(run)
        progress["harness"].update(observed_token_limit=30000, finalize_token_threshold=20000,
            stop_reason="unsafe_configuration", transport_status="failed", secret="PRIVATE_SECRET")
        progress["harness"]["cli_settings"].update(transport="app-server", configuration_verified=True,
            configuration_verification_required=True, ignore_user_config=False, access_token="PRIVATE_TOKEN")
        self.public.save_progress(run, progress)
        result = inspect_harness(self.store, self.trip_id)
        settings = result["runs"][0]["settings"]
        self.assertEqual(settings["transport"], "app-server")
        self.assertEqual(settings["observed_token_limit"], 30000)
        self.assertEqual(settings["finalize_token_threshold"], 20000)
        self.assertEqual(settings["transport_status"], "failed")
        self.assertEqual(settings["stop_reason"], "unsafe_configuration")
        self.assertFalse(settings["configuration_verified"])
        self.assertNotIn("configuration_verified", settings["cli_settings"])
        self.assertTrue(settings["cli_settings"]["configuration_verification_required"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))

    def test_captured_instruction_layers_and_verified_handshake_are_distinct_from_current_policy(self):
        run = self.saved_run("Exact public request")
        progress = self.public.progress(run)
        progress["harness"].update(configuration_verified=True,
            prompt_layers={"base": "Captured base layer", "developer": "Captured developer layer", "other": "PRIVATE_SECRET"},
            finalize_requested=True, finalize_instruction="Captured checkpoint request")
        progress["harness"]["cli_settings"]["transport"] = "app-server"
        self.public.save_progress(run, progress)
        settings = inspect_harness(self.store, self.trip_id)["runs"][0]["settings"]
        self.assertTrue(settings["configuration_verified"])
        self.assertEqual(settings["prompt_layers"], {"base": "Captured base layer", "developer": "Captured developer layer"})
        self.assertTrue(settings["finalize_requested"])
        self.assertEqual(settings["finalize_instruction"], "Captured checkpoint request")
        self.assertNotIn("PRIVATE_SECRET", json.dumps(settings))

    def test_symlinks_changed_oversized_and_missing_capture_are_not_read(self):
        run = self.saved_run("Exact public prompt")
        target = self.public.root / "research" / (run + ".prompt.txt")
        target.write_text("CHANGED_PRIVATE_CONTENT")
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"][0]["prompt"]["status"], "unavailable")
        target.unlink()
        secret = self.public.root / "strava.json"
        secret.write_text("SECRET_AUTH_TOKEN")
        target.symlink_to(secret)
        result = inspect_harness(self.store, self.trip_id)
        self.assertNotIn("SECRET_AUTH_TOKEN", json.dumps(result))
        self.assertEqual(result["runs"][0]["prompt"]["status"], "unavailable")
        target.unlink()
        target.write_bytes(b"x" * 360001)
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"][0]["prompt"]["status"], "unavailable")
        target.unlink()
        os.mkfifo(target)
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"][0]["prompt"]["status"], "unavailable")
        target.unlink()
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"][0]["prompt"]["status"], "unavailable")

    def test_invalid_ids_and_outside_root_ledger_paths_do_not_open_files(self):
        run = self.saved_run("Exact public prompt")
        with self.store.db:
            self.store.db.execute("UPDATE usage_ledger SET data_root='/tmp' WHERE id=?", (run,))
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"], [])
        with self.store.db:
            self.store.db.execute("UPDATE usage_ledger SET data_root=?,id='../strava' WHERE id=?", (str(self.public.root), run))
        self.assertEqual(inspect_harness(self.store, self.trip_id)["runs"], [])


if __name__ == "__main__":
    unittest.main()
