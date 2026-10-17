import json
import tempfile
import unittest

from fluctour.jobs import _public_context, enqueue, validate_request
from fluctour.server import dispatch, public_view
from fluctour.store import Store
from test_fluctour import dossier, option


REQUEST = {"destination": "London", "start_date": "2027-01-07",
           "end_date": "2027-01-10", "activities": ["hiking"]}


class PlanningContextIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)
        profile = self.store.profile()
        profile.update(home_airport="HKG", work_end="17:00",
                       passport_nationality="PRIVATE_PASSPORT", residency="PRIVATE_RESIDENCY",
                       policy_text="PRIVATE_EMPLOYER_PASSAGE", visit_date_flexibility="somewhat_flexible",
                       policy_rules={"allowances": {"LON": 2, "NYC": 1},
                                     "carrier": "CX", "cabin": "business",
                                     "triangle_personal_legs": 1, "comparison": "strictly_less"})
        self.store.save_profile(profile)

    def saved_trip(self, request):
        trip = self.store.create_trip("A private local request")
        result = dossier([option()]) | {"_planning_request": request}
        run = self.store.begin_run(trip, "source_import")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0}, result=result)
        enqueue(self.store, trip, request)
        return trip

    def test_business_leave_is_unknown_before_leave_filtering(self):
        business = self.saved_trip(REQUEST | {"business_anchor": "LON"})
        leisure = self.saved_trip(REQUEST)
        self.store.add_feedback(business, "", {"max_leave": 1})
        self.store.add_feedback(leisure, "", {"max_leave": 1})
        business_view = public_view(self.store, business)
        leisure_view = public_view(self.store, leisure)
        self.assertEqual(business_view["options"], [])
        self.assertEqual(len(leisure_view["options"]), 1)
        self.assertEqual(leisure_view["options"][0]["assessment"]["leave"]["days"], 1)
        self.assertEqual(business_view["planning_context"]["leave_status"], "business_work_dates_unconfirmed")

    def test_unfiltered_business_result_preserves_unknown_leave_and_allowance(self):
        trip = self.saved_trip(REQUEST | {"business_anchor": "LON"})
        data = public_view(self.store, trip)
        self.assertIsNone(data["options"][0]["assessment"]["leave"]["days"])
        self.assertIsNone(data["planning_context"]["employer_benefit"]["allowance_remaining"])
        self.assertEqual(data["planning_context"]["employer_benefit"]["annual_allowance"], 2)
        self.assertEqual(data["planning_context"]["employer_approval"], "not_verified")

    def test_public_prompt_omits_anchor_and_private_profile_but_keeps_public_destination(self):
        brief, public_profile = _public_context(self.store, REQUEST | {"business_anchor": "LON"})
        self.assertEqual(brief, _public_context(self.store, REQUEST)[0])
        public = json.loads(brief.split("\n", 1)[1])
        self.assertEqual(public["request"], REQUEST)
        self.assertEqual(public["home_airport"], "HKG")
        encoded = brief + json.dumps(public_profile)
        for private in ("business_anchor", "PRIVATE_PASSPORT", "PRIVATE_RESIDENCY", "PRIVATE_EMPLOYER_PASSAGE"):
            self.assertNotIn(private, encoded)

    def test_trip_switch_does_not_reuse_another_trips_business_context(self):
        london = self.saved_trip(REQUEST | {"business_anchor": "LON"})
        new_york = self.saved_trip(REQUEST | {"destination": "New York", "business_anchor": "NYC"})
        leisure = self.saved_trip(REQUEST)
        for trip, anchor, annual in ((london, "LON", 2), (new_york, "NYC", 1)):
            status, data = dispatch(self.store, "GET", "/api/trips/" + trip)
            self.assertEqual(status, 200)
            self.assertEqual(data["planning_context"]["business_anchor"], anchor)
            self.assertEqual(data["planning_context"]["employer_benefit"]["annual_allowance"], annual)
            for private in ("PRIVATE_PASSPORT", "PRIVATE_RESIDENCY", "PRIVATE_EMPLOYER_PASSAGE"):
                self.assertNotIn(private, json.dumps(data))
        personal = public_view(self.store, leisure)
        self.assertEqual(personal["planning_context"]["business_anchor"], "")
        self.assertIsNone(personal["planning_context"]["employer_benefit"])
        self.assertEqual(personal["options"][0]["assessment"]["leave"]["days"], 1)

    def test_explicit_public_via_city_is_forwarded_without_business_classification(self):
        for anchor, city in (("LON", "London"), ("NYC", "New York")):
            request = validate_request(REQUEST | {"destination": "Barcelona", "business_anchor": anchor,
                                                  "via_city": city})
            brief, _ = _public_context(self.store, request)
            public = json.loads(brief.split("\n", 1)[1])
            self.assertEqual(public["request"]["destination"], "Barcelona")
            self.assertEqual(public["request"]["via_city"], city)
            self.assertNotIn("business_anchor", public["request"])
            self.assertNotIn("PRIVATE_EMPLOYER_PASSAGE", brief)
        with self.assertRaises(ValueError):
            validate_request(REQUEST | {"business_anchor": "LON", "via_city": "New York"})
        with self.assertRaises(ValueError):
            validate_request(REQUEST | {"via_city": "PRIVATE_ARBITRARY_TEXT"})

    def test_local_business_anchor_without_public_via_requires_matching_saved_route(self):
        request = validate_request(REQUEST | {"business_anchor": "LON"})
        self.assertNotIn("via_city", request)
        trip = self.saved_trip(request)
        blockers = public_view(self.store, trip)["options"][0]["assessment"]["blockers"]
        self.assertIn("Saved flights do not include the selected London work visit.", blockers)
        legs = [{"id": "out", "origin": "HKG", "destination": "LHR", "departure": None,
                 "arrival": None, "carrier": "Unconfirmed", "cabin": "unknown", "paid_by": "unknown"},
                {"id": "back", "origin": "LHR", "destination": "HKG", "departure": None,
                 "arrival": None, "carrier": "Unconfirmed", "cabin": "unknown", "paid_by": "unknown"}]
        run = self.store.begin_run(trip, "source_import")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0},
                              result=dossier([option(legs=legs)]) | {"_planning_request": request})
        data = public_view(self.store, trip)
        self.assertNotIn("Saved flights do not include the selected London work visit.",
                         data["options"][0]["assessment"]["blockers"])
        self.assertIsNone(data["options"][0]["assessment"]["leave"]["days"])
        self.assertEqual(data["options"][0]["legs"], legs)

    def test_changed_public_request_labels_saved_options_as_outdated(self):
        trip = self.saved_trip(REQUEST)
        key = "job:" + trip
        state = json.loads(self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0])
        state["status"] = "completed"
        with self.store.db:
            self.store.db.execute("UPDATE settings SET value=? WHERE key=?", (json.dumps(state), key))
        enqueue(self.store, trip, REQUEST | {"destination": "Kyoto"})
        data = public_view(self.store, trip)
        self.assertEqual(data["job"]["request"]["destination"], "Kyoto")
        self.assertIn("Saved options belong to a different request; updated research is pending.",
                      data["options"][0]["assessment"]["blockers"])
        self.assertEqual(self.store.latest_result(trip)["_planning_request"]["destination"], "London")

    def test_filter_currency_comes_from_profile_even_when_option_currency_differs(self):
        trip = self.saved_trip(REQUEST)
        run = self.store.begin_run(trip, "source_import")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0},
                              result=dossier([option(currency="USD")]) | {"_planning_request": REQUEST})
        data = public_view(self.store, trip)
        self.assertEqual(data["options"][0]["currency"], "USD")
        self.assertEqual(data["planning_context"]["currency"], "HKD")

    def test_hard_total_cost_cap_does_not_accept_a_partial_trip_price(self):
        trip = self.saved_trip(REQUEST)
        run = self.store.begin_run(trip, "source_import")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0},
                              result=dossier([option(total_cost=5000, cost_complete=False)]) |
                              {"_planning_request": REQUEST})
        status, data = dispatch(self.store, "POST", "/api/trips/" + trip + "/feedback",
                                {"text": "max HKD8000"})
        self.assertEqual(status, 200)
        self.assertEqual(data["options"], [])
        self.assertEqual(self.store.latest_result(trip)["options"][0]["total_cost"], 5000)


if __name__ == "__main__":
    unittest.main()
