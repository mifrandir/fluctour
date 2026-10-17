import copy
import json
import unittest

from fluctour.personalization import annotate_options, planning_context
from fluctour.store import DEFAULT_PROFILE


def profile(**changes):
    return copy.deepcopy(DEFAULT_PROFILE) | {
        "home_airport": "HKG", "work_start": "09:00", "work_end": "17:00",
        "visit_date_flexibility": "somewhat_flexible",
        "passport_nationality": "PRIVATE_PASSPORT", "residency": "PRIVATE_RESIDENCY",
        "policy_text": "PRIVATE_RAW_POLICY", "strava_secret": "PRIVATE_STRAVA_SECRET",
        "policy_rules": {"allowances": {"LON": 2, "NYC": 1}, "carrier": "CX", "cabin": "business",
                         "triangle_personal_legs": 1, "comparison": "strictly_less",
                         "provenance": "user_reported", "written_policy_verified": False,
                         "benefit_year": None, "used": None},
    } | changes


class PersonalizationTests(unittest.TestCase):
    def test_leisure_trip_uses_known_home_work_and_flexibility_without_business_benefit(self):
        context = planning_context(profile(), {"destination": "Barcelona"})
        self.assertEqual(context["home_airport"], "HKG")
        self.assertEqual(context["work_schedule"]["start"], "09:00")
        self.assertEqual(context["work_schedule"]["end"], "17:00")
        self.assertEqual(context["work_schedule"]["workdays"], [0, 1, 2, 3, 4])
        self.assertEqual(context["date_flexibility"], "somewhat_flexible")
        self.assertFalse(context["work_schedule"]["calendar_confirmed"])
        self.assertFalse(context["work_schedule"]["timing_confirmed"])
        self.assertIsNone(context["employer_benefit"])
        self.assertIsNone(context["anchor_city"])
        self.assertFalse(any("allowance" in item or "triangle" in item.lower() for item in context["facts"] + context["blockers"]))

    def test_business_anchor_applies_only_recorded_allowance_with_unknown_usage_and_leave(self):
        for anchor, city, annual in (("LON", "London", 2), ("NYC", "New York", 1)):
            context = planning_context(profile(), {"business_anchor": anchor})
            self.assertEqual(context["anchor_city"], city)
            self.assertEqual(context["employer_benefit"]["annual_allowance"], annual)
            self.assertIsNone(context["employer_benefit"]["used"])
            self.assertIsNone(context["employer_benefit"]["allowance_remaining"])
            self.assertIsNone(context["employer_benefit"]["reset_date"])
            self.assertEqual(context["leave_status"], "business_work_dates_unconfirmed")
            self.assertIsNone(context["personal_leave_days"])
            self.assertTrue(context["employer_benefit"]["triangle_rule_known"])
            self.assertEqual(context["employer_approval"], "not_verified")

    def test_recorded_year_usage_and_dates_are_required_for_remaining_allowance(self):
        saved = profile(calendar_confirmed=True, timing_confirmed=True, visit_date_flexibility="flexible")
        saved["policy_rules"].update(benefit_year={"start": "2027-01-01", "end": "2027-12-31"},
                                      used={"LON": 1, "NYC": 0})
        request = {"business_anchor": "LON", "start_date": "2027-06-11", "end_date": "2027-06-14"}
        context = planning_context(saved, request)
        self.assertEqual(context["employer_benefit"]["allowance_remaining"], 1)
        self.assertTrue(context["work_schedule"]["calendar_confirmed"])
        self.assertTrue(context["work_schedule"]["timing_confirmed"])
        self.assertEqual(context["date_flexibility"], "flexible")
        self.assertEqual(context["leave_status"], "business_work_dates_unconfirmed")
        self.assertIsNone(context["employer_benefit"]["reset_date"])
        for changed in ({"start_date": "", "end_date": ""},
                        {"start_date": "2028-01-01", "end_date": "2028-01-03"}):
            self.assertIsNone(planning_context(saved, request | changed)["employer_benefit"]["allowance_remaining"])
        saved["policy_rules"]["used"]["LON"] = 2
        self.assertEqual(planning_context(saved, request)["employer_benefit"]["allowance_remaining"], 0)
        saved["policy_rules"]["reset_date"] = "2028-01-01"
        self.assertEqual(planning_context(saved, request)["employer_benefit"]["reset_date"], "2028-01-01")

    def test_unstructured_or_partial_policy_never_becomes_a_known_triangle_rule(self):
        context = planning_context(profile(policy_rules={}), {"business_anchor": "LON"})
        self.assertIsNone(context["employer_benefit"]["annual_allowance"])
        self.assertFalse(context["employer_benefit"]["triangle_rule_known"])
        saved = profile()
        del saved["policy_rules"]["comparison"]
        self.assertFalse(planning_context(saved, {"business_anchor": "LON"})["employer_benefit"]["triangle_rule_known"])

    def test_private_identity_raw_policy_and_secrets_are_not_in_local_ui_context(self):
        for anchor in ("", "LON", "NYC"):
            context = planning_context(profile(), {"business_anchor": anchor, "private_brief": "PRIVATE_BRIEF"})
            serialized = json.dumps(context)
            for secret in ("PRIVATE_PASSPORT", "PRIVATE_RESIDENCY", "PRIVATE_RAW_POLICY",
                           "PRIVATE_STRAVA_SECRET", "PRIVATE_BRIEF"):
                self.assertNotIn(secret, serialized)
        malformed = profile(home_airport="PRIVATE_BAD_AIRPORT", timezone="PRIVATE_BAD_ZONE",
                            visit_date_flexibility="PRIVATE_BAD_FLEX", work_start="PRIVATE_BAD_HOURS",
                            policy_rules={"provenance": "PRIVATE_BAD_POLICY"})
        self.assertNotIn("PRIVATE_", json.dumps(planning_context(malformed, {"business_anchor": "LON"})))

    def test_annotation_preserves_fares_and_payers_and_makes_business_leave_unknown(self):
        options = [{"id": "option-a", "total_cost": 1234, "legs": [{"paid_by": "unknown"}],
                    "assessment": {"status": "researched", "leave": {"days": 5, "dates": ["2027-06-11"],
                                                                                 "provisional": False}, "blockers": []}}]
        original = copy.deepcopy(options)
        annotated = annotate_options(options, profile(), {"business_anchor": "LON"})
        self.assertEqual(options, original)
        self.assertEqual(annotated[0]["total_cost"], 1234)
        self.assertEqual(annotated[0]["legs"], original[0]["legs"])
        self.assertIsNone(annotated[0]["assessment"]["leave"]["days"])
        self.assertEqual(annotated[0]["assessment"]["status"], "needs_checks")
        leisure = annotate_options(options, profile(), {})
        self.assertEqual(leisure[0]["assessment"], original[0]["assessment"])

    def test_invalid_anchor_and_malformed_usage_are_not_inferred(self):
        for anchor in ("DFW", "LAX", None, [], True):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                planning_context(profile(), {"business_anchor": anchor})
        saved = profile()
        saved["policy_rules"].update(benefit_year={"start": "2027-01-01", "end": "2027-12-31"},
                                      used={"LON": True}, recent_returns=["DFW", "LAX"])
        result = planning_context(saved, {"business_anchor": "LON", "start_date": "2027-06-11", "end_date": "2027-06-14"})
        self.assertIsNone(result["employer_benefit"]["used"])
        self.assertIsNone(result["employer_benefit"]["allowance_remaining"])


if __name__ == "__main__":
    unittest.main()
