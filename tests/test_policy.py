import copy
import json
import unittest
from datetime import datetime, timezone

from fluctour.policy import assess_triangle


NOW = datetime(2026, 10, 15, 4, tzinfo=timezone.utc)


def leg(leg_id, origin, destination, departure, arrival, carrier="CX", cabin="business"):
    return {"id": leg_id, "origin": origin, "destination": destination,
            "departure": departure, "arrival": arrival, "carrier": carrier, "cabin": cabin}


def quote(amount, **extra):
    return {"amount": amount, "currency": "HKD", "checked_at": "2026-10-15T11:00:00+08:00",
            "source_url": "https://www.cathaypacific.com/quote/example", **extra}


def request(personal="b"):
    attendance = {"start": "2026-11-02T09:00:00+00:00", "end": "2026-11-04T17:00:00+00:00"}
    return {
        "business_destination": "LON", "business_attendance": attendance,
        "legs": [
            leg("a", "HKG", "LHR", "2026-11-01T00:30:00+08:00", "2026-11-01T07:30:00+00:00"),
            leg("b", "LHR", "LAX", "2026-11-05T12:00:00+00:00", "2026-11-05T15:30:00-08:00"),
            leg("c", "LAX", "HKG", "2026-11-08T11:00:00-08:00", "2026-11-09T19:00:00+08:00"),
        ],
        "personal_leg_id": personal,
        "company_quote": quote("35000.10", leg_ids=[i for i in ("a", "b", "c") if i != personal],
                               ticketing="joint", carrier="CX", cabin="business"),
        "baseline_quote": quote("40000.20", carrier="CX", cabin="business",
                                business_attendance=copy.deepcopy(attendance), legs=[
                                    leg("base-a", "HKG", "LHR", "2026-11-01T00:30:00+08:00",
                                        "2026-11-01T07:30:00+00:00"),
                                    leg("base-b", "LHR", "HKG", "2026-11-05T12:00:00+00:00",
                                        "2026-11-06T08:00:00+08:00"),
                                ]),
        "personal_quote": quote("4000.00", leg_ids=[personal]),
    }


def profile(**rules):
    return {"policy_rules": {"provenance": "user_reported", "written_policy_verified": False,
                             "benefit_year": None, "used": None, **rules}}


class TrianglePolicyTests(unittest.TestCase):
    def assess(self, data=None, personal="b", policy=None):
        return assess_triangle(data if data is not None else request(personal),
                               policy if policy is not None else profile(), as_of=NOW)

    def test_any_one_leg_can_be_personally_paid(self):
        for personal in ("a", "b", "c"):
            with self.subTest(personal=personal):
                result = self.assess(personal=personal)
                self.assertEqual(result["comparison"], "conditional_pass")
                self.assertEqual(result["company_total"], "35000.10")
                self.assertEqual(result["personal_total"], "4000.00")
                self.assertEqual(result["savings"], "5000.10")
                self.assertEqual(result["employer_approval"], "not_verified")
                json.dumps(result)

    def test_equal_and_higher_totals_fail_strict_comparison(self):
        for amount, savings in (("40000.20", "0.00"), ("40000.21", "-0.01")):
            with self.subTest(amount=amount):
                data = request()
                data["company_quote"]["amount"] = amount
                result = self.assess(data)
                self.assertEqual(result["comparison"], "fails_strict_inequality")
                self.assertEqual(result["savings"], savings)

    def test_decimal_comparison_retains_precision(self):
        data = request()
        data["company_quote"]["amount"] = "9999999999999999999999999.10"
        data["baseline_quote"]["amount"] = "9999999999999999999999999.11"
        self.assertEqual(self.assess(data)["savings"], "0.01")

    def test_company_quote_must_cover_exact_other_two_legs_jointly(self):
        for changes in ({"leg_ids": ["a"]}, {"leg_ids": ["a", "b"]},
                        {"leg_ids": ["a", "a"]}, {"leg_ids": ["a", "c", "b"]},
                        {"ticketing": "separate"}, {"ticketing": None}):
            with self.subTest(changes=changes):
                data = request()
                data["company_quote"].update(changes)
                result = self.assess(data)
                self.assertEqual(result["comparison"], "needs_quotes")
                self.assertIsNone(result["company_total"])

    def test_cross_currency_quotes_do_not_compare(self):
        for name in ("company_quote", "baseline_quote", "personal_quote"):
            with self.subTest(name=name):
                data = request()
                data[name]["currency"] = "USD"
                result = self.assess(data)
                self.assertEqual(result["comparison"], "needs_quotes")
                self.assertIsNone(result["savings"])

    def test_company_and_baseline_require_cathay_business(self):
        for location in ("company_quote", "baseline_quote", "company_leg", "baseline_leg"):
            for changes in ({"carrier": "BA"}, {"cabin": "economy"}):
                with self.subTest(location=location, changes=changes):
                    data = request()
                    target = (data["legs"][0] if location == "company_leg" else
                              data["baseline_quote"]["legs"][0] if location == "baseline_leg"
                              else data[location])
                    target.update(changes)
                    self.assertNotEqual(self.assess(data)["comparison"], "conditional_pass")

    def test_personal_leg_can_use_another_carrier_and_cabin(self):
        data = request()
        data["legs"][1].update(carrier="AA", cabin="economy")
        result = self.assess(data)
        self.assertEqual(result["comparison"], "conditional_pass")
        self.assertEqual(result["personal_leg"]["carrier"], "AA")

    def test_triangle_topology_and_absolute_chronology(self):
        variants = []
        for index, field, value in ((0, "origin", "SZX"), (2, "destination", "SZX"),
                                    (1, "origin", "LGW"), (2, "id", "a"),
                                    (1, "departure", "2026-11-01T01:00:00+00:00"),
                                    (2, "arrival", "2026-11-08T10:00:00+08:00"),
                                    (0, "departure", "2026-11-01T00:30:00")):
            data = request()
            data["legs"][index][field] = value
            variants.append(data)
        for data in variants:
            with self.subTest(legs=data["legs"]):
                self.assertEqual(self.assess(data)["comparison"], "invalid_itinerary")
        data = request()
        data["personal_leg_id"] = "missing"
        self.assertEqual(self.assess(data)["comparison"], "invalid_itinerary")
        # The westbound arrival has an earlier local clock time on the same day.
        data = request()
        data["legs"][1]["arrival"] = "2026-11-05T10:00:00-08:00"
        self.assertEqual(self.assess(data)["comparison"], "conditional_pass")

    def test_business_attendance_must_match_baseline_and_both_itineraries(self):
        variants = []
        data = request()
        data["baseline_quote"]["business_attendance"]["end"] = "2026-11-03T17:00:00+00:00"
        variants.append(data)
        data = request()
        data["baseline_quote"]["legs"][1]["departure"] = "2026-11-03T12:00:00+00:00"
        variants.append(data)
        data = request()
        data["baseline_quote"]["legs"][0]["destination"] = "JFK"
        variants.append(data)
        for data in variants:
            self.assertEqual(self.assess(data)["comparison"], "needs_quotes")
        data = request()
        data["legs"][1]["departure"] = "2026-11-03T12:00:00+00:00"
        self.assertEqual(self.assess(data)["comparison"], "invalid_itinerary")

    def test_missing_stale_future_and_unsourced_quotes_cannot_compare(self):
        for name in ("company_quote", "baseline_quote"):
            for changes in (None, {"checked_at": "2026-10-14T20:00:00+00:00"},
                            {"checked_at": "2026-10-15T05:00:00+00:00"},
                            {"source_url": ""}, {"source_url": "https://user:secret@example.com"},
                            {"checked_at": "2026-10-15T04:00:00"}):
                with self.subTest(name=name, changes=changes):
                    data = request()
                    if changes is None:
                        del data[name]
                    else:
                        data[name].update(changes)
                    result = self.assess(data)
                    self.assertEqual(result["comparison"], "needs_quotes")
                    self.assertIsNone(result["savings"])
        data = request()
        data["company_quote"]["checked_at"] = "2025-01-01T00:00:00+00:00"
        data["as_of"] = "2025-01-01T00:00:00+00:00"
        self.assertEqual(self.assess(data)["comparison"], "needs_quotes")

    def test_invalid_amounts_do_not_pass(self):
        for amount in (0, -1, "NaN", "Infinity", "-Infinity", "unknown", True, None):
            with self.subTest(amount=amount):
                data = request()
                data["company_quote"]["amount"] = amount
                self.assertEqual(self.assess(data)["comparison"], "needs_quotes")

    def test_unknown_usage_and_reset_are_not_inferred_from_recent_trips(self):
        result = self.assess(policy=profile(recent_returns=["DFW", "LAX"]))
        self.assertEqual(result["allowance_remaining"], {"LON": None, "NYC": None})
        self.assertTrue(any("unknown" in item for item in result["blockers"]))
        self.assertTrue(any("user-reported" in item for item in result["blockers"]))

    def test_explicit_benefit_year_and_usage_allow_remaining_calculation(self):
        policy = profile(benefit_year={"start": "2026-07-01", "end": "2027-06-30"},
                         used={"LON": 1, "NYC": 0})
        result = self.assess(policy=policy)
        self.assertEqual(result["allowance_remaining"], {"LON": 1, "NYC": 1})
        policy["policy_rules"]["used"]["LON"] = 2
        result = self.assess(policy=policy)
        self.assertEqual(result["allowance_remaining"]["LON"], 0)
        self.assertTrue(any("exhausted" in item for item in result["blockers"]))
        policy["policy_rules"]["benefit_year"] = {"start": "2025-07-01", "end": "2026-06-30"}
        self.assertEqual(self.assess(policy=policy)["allowance_remaining"], {"LON": None, "NYC": None})

    def test_personal_fare_is_optional_and_is_not_allocated_from_company_quote(self):
        data = request()
        del data["personal_quote"]
        result = self.assess(data)
        self.assertEqual(result["comparison"], "conditional_pass")
        self.assertIsNone(result["personal_total"])
        self.assertTrue(any("personal cost is unknown" in item for item in result["blockers"]))
        data["personal_quote"] = quote("4000", leg_ids=["b"], checked_at="2026-10-14T04:00:00+00:00")
        self.assertEqual(self.assess(data)["comparison"], "needs_quotes")

    def test_malformed_json_shapes_produce_blockers(self):
        for data in (None, [], {}, {"business_destination": []}, request() | {"legs": None}):
            with self.subTest(data=data):
                self.assertEqual(assess_triangle(data, {}, as_of=NOW)["comparison"], "invalid_itinerary")


if __name__ == "__main__":
    unittest.main()
