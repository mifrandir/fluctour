import math
import unittest

from fluctour.preferences import derive_public_request_update, interpret_feedback


class PreferenceTests(unittest.TestCase):
    def test_explicit_filters_combine_with_original_text_evidence(self):
        text = "no overnight flights; max 2 days leave; under HKD8000"
        result = interpret_feedback(text, "HKD")
        self.assertEqual(result["constraints"]["max_leave"], 2)
        self.assertTrue(result["constraints"]["no_overnight"])
        self.assertEqual(result["constraints"]["max_cost"], math.nextafter(8000, -math.inf))
        self.assertEqual(result["unapplied"], [])
        for item in result["requirements"]:
            self.assertEqual(item["status"], "applied")
            self.assertEqual(item["kind"], "filter")
            self.assertEqual(text[slice(*item["span"])], item["source_text"])

    def test_unknown_private_requirements_remain_unapplied(self):
        text = "my employer requires PRIVATE_COMPANY_APPROVAL; no overnight flights; I'd prefer fewer crowds"
        result = interpret_feedback(text, "HKD")
        self.assertEqual(result["constraints"], {"no_overnight": True})
        self.assertEqual(len(result["unapplied"]), 2)
        self.assertIn("PRIVATE_COMPANY_APPROVAL", result["unapplied"][0]["text"])
        self.assertEqual(result["unapplied"][1]["text"], "I'd prefer fewer crowds")

    def test_negated_or_unsupported_coordination_never_creates_positive_activity_intent(self):
        request = {"destination": "Kyoto", "activities": []}
        for text in ("Do not include hiking and swimming", "Don't include hiking, swimming",
                     "don't apply no overnight flights and max 2 days leave",
                     "only if work permits hiking and swimming"):
            with self.subTest(text=text):
                result = interpret_feedback(text, "HKD")
                self.assertEqual(result["constraints"], {})
                self.assertEqual(result["requirements"], [])
                self.assertTrue(result["unapplied"])
                self.assertFalse(derive_public_request_update(request, result)["changed"])

    def test_typed_nonfilter_requirements_are_explicitly_pending(self):
        result = interpret_feedback("more hiking; 7-12 km hikes; avoid places already visited; only verified availability; liked Strava activity 123456", "HKD")
        self.assertEqual(result["constraints"], {})
        self.assertEqual([item["kind"] for item in result["requirements"]], [
            "activity", "hike_distance", "avoid_visited", "availability", "liked_activity"])
        self.assertEqual(len(result["unapplied"]), 5)
        self.assertTrue(all(item["status"] == "pending" for item in result["requirements"]))
        distance = result["requirements"][1]
        self.assertEqual((distance["minimum_km"], distance["maximum_km"]), (7, 12))
        self.assertTrue(distance["confirmed_by_user"])
        liked = result["requirements"][4]
        self.assertEqual(liked["activity_id"], "123456")
        self.assertTrue(liked["confirmed_by_user"])
        self.assertFalse(liked["activity_verified"])

    def test_public_update_contains_only_validated_request_and_activity_enums(self):
        request = {"destination": "Kyoto", "start_date": "2027-04-10", "end_date": "2027-04-13", "activities": ["food"]}
        interpretation = interpret_feedback("more hiking and swimming; PRIVATE_EMPLOYER_POLICY; under HKD8000; 7–12 km hikes", "HKD")
        result = derive_public_request_update(request, interpretation)
        self.assertTrue(result["changed"])
        self.assertEqual(result["request"]["activities"], ["food", "hiking", "swimming"])
        self.assertEqual(result["applied_activities"], ["hiking", "swimming"])
        self.assertEqual(set(result["request"]), {"destination", "start_date", "end_date", "activities"})
        self.assertNotIn("PRIVATE_EMPLOYER_POLICY", str(result))
        self.assertNotIn("max_cost", str(result))
        self.assertNotIn("7–12", str(result))
        same = derive_public_request_update(result["request"], interpretation)
        self.assertFalse(same["changed"])
        self.assertEqual(interpretation["requirements"][0]["status"], "pending")

    def test_no_implicit_hiking_preferences_from_past_activity_references(self):
        request = {"destination": "Kyoto", "activities": []}
        result = derive_public_request_update(request, interpret_feedback("liked Strava activity 42", "HKD"))
        self.assertFalse(result["changed"])
        self.assertEqual(result["request"]["activities"], [])

    def test_other_currencies_ambiguous_dollar_and_locale_numbers_are_not_guessed(self):
        different = interpret_feedback("under USD8000", "HKD")
        self.assertEqual(different["constraints"], {})
        self.assertEqual(different["requirements"][0]["currency"], "USD")
        self.assertEqual(different["requirements"][0]["status"], "pending")
        for text in ("under $8000", "under HKD8.000", "under HKD8,00", "under HKD-20", "under HKDnan", "under 8, 000"):
            with self.subTest(text=text):
                result = interpret_feedback(text, "HKD")
                self.assertEqual(result["constraints"], {})
                self.assertTrue(result["unapplied"])
        self.assertEqual(interpret_feedback("max HKD8,000.50", "HKD")["constraints"], {"max_cost": 8000.5})

    def test_conflicts_stay_pending_and_multiple_caps_use_strictest(self):
        conflict = interpret_feedback("no overnight flights and allow overnight flights; max 2 days leave", "HKD")
        self.assertEqual(conflict["constraints"], {"max_leave": 2})
        self.assertEqual(len(conflict["unapplied"]), 2)
        self.assertEqual(interpret_feedback("max 3 days leave; max 2 days leave", "HKD")["constraints"], {"max_leave": 2})
        self.assertEqual(interpret_feedback("max HKD8000; max HKD9000", "HKD")["constraints"], {"max_cost": 8000})

    def test_date_constraints_reset_and_negation_are_not_misread(self):
        self.assertEqual(interpret_feedback("depart 2027-04-10", "HKD")["constraints"], {"departure_date": "2027-04-10"})
        self.assertEqual(interpret_feedback("clear filters", "HKD")["constraints"], {"clear": True})
        self.assertEqual(interpret_feedback("clear filters; max 2 days leave", "HKD")["constraints"], {})
        self.assertEqual(interpret_feedback("depart 2027-04-10; depart 2027-04-11", "HKD")["constraints"], {})
        for text in ("depart 2027-02-30", "not under HKD8000", "I don't want no overnight flights", "12-7 km hikes"):
            with self.subTest(text=text):
                self.assertEqual(interpret_feedback(text, "HKD")["constraints"], {})
                self.assertTrue(interpret_feedback(text, "HKD")["unapplied"])

    def test_bounds_empty_input_and_invalid_public_updates(self):
        self.assertEqual(interpret_feedback("", "HKD"), {"constraints": {}, "requirements": [], "unapplied": []})
        for text, currency in ((5, "HKD"), ("a" * 4001, "HKD"), ("\ud800", "HKD"), ("more hiking", "usd"), ("x", None)):
            with self.subTest(text=str(text)[:20]), self.assertRaises(ValueError):
                interpret_feedback(text, currency)
        for interpretation in (None, {}, {"requirements": "hiking"}):
            with self.assertRaises(ValueError):
                derive_public_request_update({"destination": "Kyoto"}, interpretation)
        self.assertEqual(derive_public_request_update({"destination": "Kyoto"}, {
            "requirements": [{"kind": "activity", "intent": "include", "activity": "PRIVATE_TEXT"}]
        })["request"]["activities"], [])


if __name__ == "__main__":
    unittest.main()
