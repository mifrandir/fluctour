import copy
import json
import unittest
from datetime import datetime

from fluctour.planning import leave_required
from fluctour.store import DEFAULT_PROFILE
from fluctour.windows import suggest_windows


class WindowTests(unittest.TestCase):
    def profile(self, **changes):
        return copy.deepcopy(DEFAULT_PROFILE) | {
            "calendar_confirmed": True, "timing_confirmed": True,
        } | changes

    def test_after_work_departure_includes_commute_and_airport_buffer(self):
        profile = self.profile()
        windows = suggest_windows(profile, "2026-10-16", horizon_days=3,
                                  min_nights=2, max_nights=2, max_leave=0)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window["mode"], "after_work")
        self.assertEqual(window["earliest_departure"], "2026-10-16T22:30:00+08:00")
        self.assertEqual(window["leave_home_at"], "2026-10-16T18:00:00+08:00")
        self.assertEqual(window["leave_dates"], [])
        self.assertTrue(window["provisional"])
        self.assertEqual(window["availability"], "not_checked")
        json.dumps(windows)

    def test_holiday_bridge_uses_actual_calendar_and_diverse_leave_buckets(self):
        profile = self.profile(holidays=["2026-10-19"])
        windows = suggest_windows(profile, "2026-10-15", horizon_days=30, limit=12)
        self.assertEqual({w["leave_days"] for w in windows}, {0, 1, 2})
        self.assertGreaterEqual(len({w["departure_date"] for w in windows}), 6)
        self.assertTrue(any(w["leave_days"] == 0 and w["nights"] == 3
                            and "2026-10-19" in w["holiday_dates"] for w in windows))

    def test_nonstandard_workweek_and_recovery_are_charged(self):
        profile = self.profile(workdays=[1, 2, 3, 4, 5], recovery_minutes=15 * 60)
        windows = suggest_windows(profile, "2026-10-16", horizon_days=7,
                                  min_nights=2, max_nights=2, max_leave=3, limit=100)
        friday = next(w for w in windows if w["departure_date"] == "2026-10-16"
                      and w["mode"] == "after_work")
        self.assertEqual(friday["leave_dates"], ["2026-10-17"])
        monday = next(w for w in windows if w["return_date"] == "2026-10-19"
                     and w["mode"] == "daytime")
        self.assertIn("2026-10-20", monday["leave_dates"])
        for window in windows:
            calculated = leave_required({"departure": window["earliest_departure"],
                                         "return_arrival": window["assumed_return_arrival"]}, profile)
            self.assertEqual(window["leave_dates"], calculated["dates"])

    def test_dst_uses_home_timezone_offsets(self):
        profile = self.profile(timezone="America/New_York", outbound_transfer_minutes=60,
                               airport_buffer_minutes=120)
        windows = suggest_windows(profile, "2026-10-30", horizon_days=3,
                                  min_nights=2, max_nights=2, max_leave=0)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["earliest_departure"], "2026-10-30T21:00:00-04:00")
        self.assertEqual(windows[0]["assumed_return_arrival"], "2026-11-01T18:00:00-05:00")

    def test_cross_midnight_outbound_is_reported_on_actual_flight_date(self):
        profile = self.profile(work_end="22:00")
        windows = suggest_windows(profile, "2026-10-16", horizon_days=4,
                                  min_nights=2, max_nights=2, max_leave=1, limit=100)
        window = next(w for w in windows if w["mode"] == "after_work")
        self.assertEqual(window["departure_date"], "2026-10-17")
        self.assertEqual(window["earliest_departure"], "2026-10-17T02:30:00+08:00")
        self.assertEqual(window["leave_home_at"], "2026-10-16T22:00:00+08:00")

    def test_bounds_and_start_types(self):
        for kwargs in ({"horizon_days": 367}, {"horizon_days": True}, {"min_nights": 0},
                       {"max_nights": 31}, {"min_nights": 4, "max_nights": 2},
                       {"max_leave": -1}, {"limit": 0}, {"start": datetime(2026, 10, 16)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                suggest_windows(self.profile(), **kwargs)
        self.assertEqual(suggest_windows(self.profile(), "2026-10-16", horizon_days=1), [])


if __name__ == "__main__":
    unittest.main()
