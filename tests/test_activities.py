import json
import tempfile
import unittest

from fluctour.activities import MAX_BYTES, activity_summary, import_activities
from fluctour.store import Store


def activity(activity_id=1, **updates):
    return {"id": activity_id, "start_date": "2026-06-12T01:00:00Z", "sport_type": "Hike",
            "distance": 8500, "total_elevation_gain": 400, "moving_time": 10800,
            "name": "Coastal ridge", "location_city": "Hong Kong", "location_country": "Hong Kong",
            "start_latlng": [22.245612, 114.238912], **updates}


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)

    def load(self):
        row = self.store.db.execute("SELECT value FROM settings WHERE key='activity_history'").fetchone()
        return row[0] if row else None

    def test_empty_summary_has_unknown_preferences_and_no_assumed_history(self):
        summary = activity_summary(self.store)
        self.assertFalse(summary["has_history"])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["date_range"])
        self.assertEqual(summary["inferred"], [])
        self.assertEqual(summary["recent"], [])
        self.assertIsNone(summary["by_type"]["Hike"]["distance_m"]["max"])
        self.assertIsNone(self.load())

    def test_json_api_units_statistics_source_and_persistence(self):
        rows = [activity(1), activity(2, distance=15000, total_elevation_gain=900, moving_time=18000),
                activity(3, sport_type="Walk", distance=None, moving_time=None),
                activity(4, sport_type="TrailRun", distance=6000),
                activity(5, sport_type="Swim", distance=1200, total_elevation_gain=0),
                activity(6, sport_type="Run", distance=5000)]
        summary = import_activities(self.store, json.dumps(rows), format="json")
        self.assertTrue(summary["has_history"])
        self.assertEqual(summary["count"], 6)
        hike = summary["by_type"]["Hike"]
        self.assertEqual(hike["count"], 2)
        self.assertEqual(hike["distance_m"]["median"], 11750)
        self.assertEqual(hike["distance_m"]["total"], 23500)
        self.assertEqual(hike["elevation_m"]["max"], 900)
        self.assertEqual(summary["by_type"]["Walk"]["distance_m"]["missing"], 1)
        self.assertIsNone(summary["by_type"]["Walk"]["distance_m"]["min"])
        self.assertEqual(summary["by_type"]["Swim"]["elevation_m"]["min"], 0)
        self.assertEqual(summary["other_types"], {"Run": 1})
        self.assertEqual(summary["inferred"], [])
        self.assertEqual(summary["provenance"][0]["source"], "strava_api_json")
        self.assertEqual(summary["last_import"]["added_count"], 6)
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(activity_summary(reopened)["count"], 6)
        finally:
            reopened.db.close()

    def test_local_dates_preferred_and_recent_has_source_names_and_locations(self):
        summary = import_activities(self.store, json.dumps([activity(start_date_local="2026-06-11T23:30:00Z")]), "json")
        self.assertEqual(summary["date_range"]["first"], "2026-06-11")
        self.assertEqual(summary["recent"][0], {"id": "1", "name": "Coastal ridge", "date": "2026-06-11",
                         "type": "Hike", "distance_m": 8500, "elevation_m": 400,
                         "location": "Hong Kong, Hong Kong"})
        raw = self.load()
        self.assertNotIn("22.245612", raw)
        self.assertNotIn("114.238912", raw)
        self.assertNotIn("23:30", raw)
        self.assertEqual(summary["coarse_regions"][0]["cell"], "22..24 latitude, 114..116 longitude")

    def test_json_discards_credentials_notes_and_routes(self):
        rows = [activity(access_token="PRIVATE_TOKEN", athlete={"id": 2},
                         description="PRIVATE_NOTE", private_note="PRIVATE_PRIVATE_NOTE",
                         map={"summary_polyline": "PRIVATE_ROUTE"})]
        summary = import_activities(self.store, json.dumps(rows), "json")
        combined = self.load() + json.dumps(summary)
        for value in ("PRIVATE_TOKEN", "PRIVATE_NOTE", "PRIVATE_ROUTE", "PRIVATE_PRIVATE_NOTE"):
            self.assertNotIn(value, combined)

    def test_repeated_import_is_idempotent_and_partial_rows_preserve_metrics(self):
        original = json.dumps([activity()])
        import_activities(self.store, original, "json")
        repeated = import_activities(self.store, original, "json")
        self.assertEqual(repeated["count"], 1)
        self.assertEqual(repeated["last_import"]["duplicate_count"], 1)
        self.assertEqual(len(repeated["provenance"]), 1)
        partial = {"id": 1, "start_date": "2026-06-12T01:00:00Z", "type": "Hike"}
        result = import_activities(self.store, json.dumps([partial]), "json")
        self.assertEqual(result["by_type"]["Hike"]["distance_m"]["max"], 8500)
        self.assertEqual(result["recent"][0]["name"], "Coastal ridge")
        self.assertEqual(result["count"], 1)
        changed = import_activities(self.store, json.dumps([activity(distance=9000)]), "json")
        self.assertEqual(changed["last_import"]["updated_count"], 1)
        self.assertEqual(changed["by_type"]["Hike"]["distance_m"]["max"], 9000)

    def test_missing_ids_have_stable_identity_and_distinct_session_times(self):
        rows = [activity(None), activity(None, start_date="2026-06-12T15:00:00Z")]
        result = import_activities(self.store, json.dumps(rows), "json")
        self.assertEqual(result["count"], 2)
        repeated = import_activities(self.store, json.dumps(rows), "json")
        self.assertEqual(repeated["count"], 2)
        self.assertEqual(repeated["last_import"]["duplicate_count"], 2)

    def test_csv_explicit_units_and_english_export_date(self):
        text = ('Activity ID,Activity Date,Activity Name,Activity Type,Distance (km),Elevation Gain (ft),Moving Time (HH:MM:SS),Location City,Location Country\n'
                '100,"Jun 12, 2026, 7:30:00 AM",Mountain day,Hike,8.5,1000,03:30:00,Barcelona,Spain\n')
        result = import_activities(self.store, text)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["by_type"]["Hike"]["distance_m"]["median"], 8500)
        self.assertAlmostEqual(result["by_type"]["Hike"]["elevation_m"]["median"], 304.8)
        self.assertEqual(result["by_type"]["Hike"]["moving_time_s"]["median"], 12600)
        self.assertEqual(result["recent"][0]["location"], "Barcelona, Spain")
        self.assertEqual(result["last_import"]["source"], "strava_csv")

    def test_csv_unitless_export_metrics_require_explicit_units(self):
        for field in ("Distance", "Moving Time", "Elevation Gain"):
            text = f"Activity Date,Activity Type,{field}\n2026-06-12,Hike,123\n"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "units are unknown"):
                import_activities(self.store, text)
        self.assertIsNone(self.load())

    def test_csv_rejects_locale_numbers_invalid_clock_and_ambiguous_columns(self):
        cases = [('Distance (km)', '"1,5"'), ('Distance (km)', '-4'), ('Distance (km)', 'NaN'),
                 ('Distance (km)', '=1+1'), ('Moving Time (HH:MM:SS)', '01:90:00')]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                import_activities(self.store, f"Activity Date,Activity Type,{field}\n2026-06-12,Hike,{value}\n")
        with self.assertRaises(ValueError):
            import_activities(self.store, "Activity Date,Activity Type,Distance (m),Distance (km)\n2026-06-12,Hike,10,1\n")
        self.assertIsNone(self.load())

    def test_invalid_later_row_rejects_entire_import_without_changing_history(self):
        import_activities(self.store, json.dumps([activity()]), "json")
        before = self.load()
        with self.assertRaises(ValueError):
            import_activities(self.store, json.dumps([activity(2), activity(3, distance=-10)]), "json")
        self.assertEqual(self.load(), before)
        with self.assertRaises(ValueError):
            import_activities(self.store, "Activity Date,Activity Type,Distance (m)\n2026-06-12,Hike,5\ninvalid,Walk,6\n")
        self.assertEqual(self.load(), before)

    def test_bad_json_types_and_metrics_reject_atomically(self):
        cases = ["{}", "invalid", '[null]', json.dumps({"access_token": "secret"}),
                 json.dumps([activity(distance=True)]), json.dumps([activity(distance="8.5")]),
                 json.dumps([activity(distance=float("nan"))]), json.dumps([activity(distance=10**300)]),
                 json.dumps([activity(start_latlng=[91, 0])]), json.dumps([activity(start_latlng=[float("nan"), 0])]),
                 json.dumps([activity(start_latlng=[True, 2])]), json.dumps([activity(id="$(command)")]),
                 json.dumps([activity(name="\ud800")]), json.dumps([activity(sport_type="\ud800")]),
                 json.dumps([activity(start_date="not a date")])]
        for text in cases:
            with self.subTest(text=text[:70]), self.assertRaises(ValueError):
                import_activities(self.store, text, "json")
        self.assertIsNone(self.load())

    def test_bounds_utf8_and_format_validation(self):
        for text, format in ((" " * (MAX_BYTES + 1), "json"), (b"\xff", "csv"),
                             (json.dumps([{}] * 10001), "json"), ("[]", "xml"), (5, "json")):
            with self.subTest(format=format), self.assertRaises(ValueError):
                import_activities(self.store, text, format)
        self.assertIsNone(self.load())

    def test_csv_missing_metrics_are_unknown_and_names_are_data(self):
        result = import_activities(self.store, 'Activity Date,Activity Type,Activity Name\n2026-06-12,Walk,"=SUM(1,2)"\n')
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["recent"][0]["name"], "=SUM(1,2)")
        self.assertIsNone(result["by_type"]["Walk"]["distance_m"]["total"])
        self.assertEqual(result["inferred"], [])

    def test_recent_is_bounded_and_no_cities_inferred_from_coordinates(self):
        rows = [activity(index, start_date=f"2026-06-{index + 1:02d}T01:00:00Z",
                         location_city=None, location_country=None) for index in range(12)]
        result = import_activities(self.store, json.dumps(rows), "json")
        self.assertEqual(len(result["recent"]), 10)
        self.assertEqual(result["recent"][0]["date"], "2026-06-12")
        self.assertIsNone(result["recent"][0]["location"])


if __name__ == "__main__":
    unittest.main()
