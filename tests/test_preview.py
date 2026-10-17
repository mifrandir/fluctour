import json
import tempfile
import unittest

from fluctour.preview import preview_data, write_preview
from fluctour.store import Store, now
from fluctour.planning import policy_digest
from test_fluctour import dossier, option


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        self.trip = self.store.create_trip("Private travel brief")

    def save(self, value):
        run = self.store.begin_run(self.trip, "local_composition")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0}, result=dossier([value]))

    def test_untrusted_text_cannot_terminate_script_data(self):
        attack = '</script><script>alert("untrusted")</script>'
        self.save(option(rationale=attack))
        output = write_preview(self.store, self.trip).read_text()
        self.assertNotIn(attack, output)
        embedded = output.split('<script type="application/json" id="proposal-data">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(embedded)["options"][0]["rationale"], attack)

    def test_snapshot_excludes_private_identity_and_policy_material(self):
        profile = self.store.profile()
        profile.update(passport_nationality="PrivateNationalityMarker", policy_text="PrivatePolicyMarker",
                       visit_date_flexibility="somewhat_flexible")
        self.store.save_profile(profile)
        self.save(option(evidence=[{"source_type": "profile_policy", "policy_excerpt": "PrivatePolicyMarker",
            "kind": "policy", "scope": "trip", "finding": "User-reported policy", "url": "",
            "checked_at": now(), "policy_digest": policy_digest(profile)}]))
        snapshot = preview_data(self.store, self.trip)
        serialized = json.dumps(snapshot)
        self.assertNotIn("PrivateNationalityMarker", serialized)
        self.assertNotIn("PrivatePolicyMarker", serialized)
        self.assertEqual(snapshot["date_flexibility"], "somewhat_flexible")
        self.assertEqual(snapshot["options"][0]["evidence"], [])

    def test_preview_preserves_unknowns_and_saved_state(self):
        self.save(option(total_cost=None, usable_hours=None, cost_complete=False))
        before = self.store.runs(self.trip)
        snapshot = preview_data(self.store, self.trip)
        output = write_preview(self.store, self.trip)
        self.assertIsNone(snapshot["options"][0]["total_cost"])
        self.assertTrue(output.is_file())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.runs(self.trip), before)
        self.assertEqual(self.store.feedback(self.trip), [])

    def test_activity_plan_and_pending_feedback_survive_reopening(self):
        self.save(option())
        option_id = self.store.latest_result(self.trip)["options"][0]["id"]
        plan = {"dates": "7 Nov 2026", "maps": [{"segments": [[[22.2, 114.2], [22.3, 114.3]]]}],
                "why": "</script><script>untrusted()</script>"}
        self.store.save_plans(self.trip, {option_id: plan})
        self.store.add_feedback(self.trip, "Prefer a shorter walk", {})
        reopened = Store(self.temp.name)
        self.addCleanup(reopened.db.close)
        snapshot = preview_data(reopened, self.trip)
        self.assertEqual(snapshot["options"][0]["plan"], plan)
        self.assertEqual(len(snapshot["pending_feedback"]), 1)
        self.assertTrue(snapshot["researched"])
        self.assertNotIn(plan["why"], write_preview(reopened, self.trip).read_text())

    def test_empty_store_renders_without_invented_proposals(self):
        snapshot = preview_data(self.store, None)
        self.assertEqual(snapshot["options"], [])
        self.assertIsNone(snapshot["trip"])
        self.assertFalse(snapshot["researched"])
        self.assertEqual(snapshot["pending_feedback"], [])
        self.assertTrue(write_preview(self.store, None).is_file())

    def test_incomplete_numeric_cost_and_explicit_destination_are_preserved(self):
        self.save(option(total_cost=5000, cost_complete=False, destination="Personal stop before London"))
        value = preview_data(self.store, self.trip)["options"][0]
        self.assertFalse(value["cost_complete"])
        self.assertEqual(value["total_cost"], 5000)
        self.assertEqual(value["destination"], "Personal stop before London")


if __name__ == "__main__":
    unittest.main()
