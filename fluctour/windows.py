"""Calendar opportunities for research; no flight inventory is implied."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .planning import leave_required, validate_profile


def _local(day, clock, zone):
    # A UTC round trip resolves a nonexistent local clock time across a DST jump.
    return datetime.combine(day, clock, zone).astimezone(timezone.utc).astimezone(zone)


def _elapsed(moment, minutes, zone):
    return (moment.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(zone)


def suggest_windows(profile, start=None, horizon_days=60, min_nights=2, max_nights=6,
                    max_leave=2, limit=12):
    """Suggest diverse calendar windows within a bounded local-date horizon.

    Daytime departures assume leaving home at 09:00. After-work departures
    assume leaving at the configured work end. Return arrivals are placeholders
    at 18:00 in the home timezone, with transfer and recovery charged afterwards.
    All results are provisional until actual flights replace these assumptions.
    """
    validate_profile(profile)
    for name, value, lower, upper in (
            ("horizon_days", horizon_days, 1, 366),
            ("min_nights", min_nights, 1, 30),
            ("max_nights", max_nights, 1, 30),
            ("max_leave", max_leave, 0, 30),
            ("limit", limit, 1, 100)):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}.")
    if min_nights > max_nights:
        raise ValueError("min_nights must not exceed max_nights.")

    zone = ZoneInfo(profile["timezone"])
    if start is None:
        first = datetime.now(zone).date() + timedelta(days=1)
    elif isinstance(start, datetime):
        if start.tzinfo is None:
            raise ValueError("A start timestamp must include a UTC offset.")
        first = start.astimezone(zone).date()
    elif type(start) is date:
        first = start
    elif isinstance(start, str):
        first = date.fromisoformat(start)
    else:
        raise ValueError("start must be an ISO date, date, or aware datetime.")
    last = first + timedelta(days=horizon_days - 1)
    holidays = set(profile["holidays"])
    outbound_minutes = profile["outbound_transfer_minutes"] + profile["airport_buffer_minutes"]
    groups = defaultdict(list)
    for offset in range(horizon_days):
        day = first + timedelta(days=offset)
        clocks = [("daytime", time(9))]
        if day.weekday() in profile["workdays"] and day.isoformat() not in holidays:
            clocks.append(("after_work", time.fromisoformat(profile["work_end"])))
        for mode, clock in clocks:
            departure = _elapsed(_local(day, clock, zone), outbound_minutes, zone)
            # An after-midnight flight belongs to the following departure date.
            departure_day = departure.date()
            for nights in range(min_nights, max_nights + 1):
                return_day = departure_day + timedelta(days=nights)
                if return_day > last:
                    continue
                arrival = _local(return_day, time(18), zone)
                option = {"departure": departure.isoformat(), "return_arrival": arrival.isoformat()}
                leave = leave_required(option, profile)
                if leave["days"] is None or leave["days"] > max_leave:
                    continue
                holiday_dates = [
                    (day + timedelta(days=i)).isoformat()
                    for i in range((return_day - day).days + 1)
                    if (day + timedelta(days=i)).weekday() in profile["workdays"]
                    and (day + timedelta(days=i)).isoformat() in holidays
                ]
                groups[leave["days"]].append({
                    "departure_date": departure_day.isoformat(),
                    "earliest_departure": departure.isoformat(),
                    "return_date": return_day.isoformat(),
                    "assumed_return_arrival": arrival.isoformat(),
                    "nights": nights,
                    "mode": mode,
                    "leave_days": leave["days"],
                    "leave_dates": leave["dates"],
                    "leave_home_at": leave["leave_home_at"],
                    "ready_for_work_at": leave["ready_for_work_at"],
                    "holiday_dates": holiday_dates,
                    "provisional": True,
                    "calendar_confirmed": profile["calendar_confirmed"],
                    "timing_confirmed": profile["timing_confirmed"],
                    "availability": "not_checked",
                    "assumptions": [
                        "Calendar opportunity only; no flight, price or accommodation is verified.",
                        "Nights count home-calendar dates, not confirmed destination nights.",
                        "Daytime home departure is 09:00; after-work home departure is work end.",
                        "Return flight arrival is assumed at 18:00 in the home timezone.",
                    ],
                })

    selected = []
    used_dates = defaultdict(int)
    used_weeks = defaultdict(int)
    used_modes = defaultdict(int)

    def week(window):
        day = date.fromisoformat(window["departure_date"])
        return day - timedelta(days=day.weekday())

    def priority(window):
        return (used_dates[window["departure_date"]], used_weeks[week(window)],
                -window["nights"], -len(window["holiday_dates"]),
                used_modes[window["mode"]], window["departure_date"],
                window["earliest_departure"])

    # Round-robin leave allowances preserve both no-leave and longer-trip choices.
    while len(selected) < limit and any(groups.values()):
        for leave_days in sorted(groups):
            if not groups[leave_days] or len(selected) == limit:
                continue
            candidate = min(groups[leave_days], key=priority)
            groups[leave_days].remove(candidate)
            selected.append(candidate)
            used_dates[candidate["departure_date"]] += 1
            used_weeks[week(candidate)] += 1
            used_modes[candidate["mode"]] += 1
    return selected
