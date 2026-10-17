import json
import tempfile
import unittest
from unittest.mock import patch

from fluctour.research import INSTRUCTIONS
from fluctour.server import dispatch
from fluctour.store import Store


class HarnessApiTests(unittest.TestCase):
    def test_inspection_is_read_only_and_excludes_private_profile_and_brief(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            try:
                profile = store.profile()
                profile.update(policy_text="PRIVATE_POLICY_MARKER", passport_nationality="PRIVATE_ID_MARKER")
                store.save_profile(profile)
                trip = store.create_trip("PRIVATE_BRIEF_MARKER")
                before = store.db.total_changes
                with patch("fluctour.research.subprocess.Popen", side_effect=AssertionError("No inference")):
                    status, data = dispatch(store, "GET", f"/api/trips/{trip}/harness")
                self.assertEqual(status, 200)
                self.assertEqual(data["current_templates"]["instructions"], INSTRUCTIONS)
                self.assertEqual(store.db.total_changes, before)
                for secret in ("PRIVATE_POLICY_MARKER", "PRIVATE_ID_MARKER", "PRIVATE_BRIEF_MARKER", root):
                    self.assertNotIn(secret, json.dumps(data))
                self.assertEqual(store.runs(trip), [])
                self.assertEqual(dispatch(store, "GET", "/api/trips/aaaaaaaaaaaa/harness")[0], 404)
                self.assertEqual(dispatch(store, "POST", f"/api/trips/{trip}/harness", {})[0], 405)
            finally:
                store.db.close()
