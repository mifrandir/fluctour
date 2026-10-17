"""Deterministic planning and conservative assessment of research evidence."""

import math
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def aware(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Flight and evidence timestamps must include a UTC offset.")
    return parsed


def validate_profile(profile):
    from .store import DEFAULT_PROFILE
    if set(profile) != set(DEFAULT_PROFILE):
        raise ValueError("Unknown or missing profile fields.")
    for key in ("timezone", "currency", "policy_text", "work_start", "work_end"):
        if not isinstance(profile[key], str):
            raise ValueError(f"{key} must be text.")
    try:
        ZoneInfo(profile["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Timezone must be an IANA identifier such as Asia/Hong_Kong.") from None
    if time.fromisoformat(profile["work_start"]) >= time.fromisoformat(profile["work_end"]):
        raise ValueError("Work start must precede work end; overnight shifts are not supported yet.")
    for key in ("work_start", "work_end"):
        if time.fromisoformat(profile[key]).tzinfo is not None:
            raise ValueError("Work hours use your profile timezone, without an offset.")
    if not isinstance(profile["workdays"], list) or any(
            type(day) is not int or not 0 <= day <= 6 for day in profile["workdays"]):
        raise ValueError("Workdays must be a list of weekday numbers, Monday=0.")
    if not isinstance(profile["holidays"], list) or any(not isinstance(d, str) for d in profile["holidays"]):
        raise ValueError("Holidays must be a list of ISO dates.")
    for day in profile["holidays"]:
        date.fromisoformat(day)
    for key in ("calendar_confirmed", "timing_confirmed", "policy_confirmed"):
        if type(profile[key]) is not bool:
            raise ValueError(f"{key} must be true or false.")
    for key in ("outbound_transfer_minutes", "airport_buffer_minutes", "home_transfer_minutes",
                "recovery_minutes", "monthly_token_budget"):
        if type(profile[key]) is not int or profile[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer.")
    for key in ("budget", "max_leave"):
        if profile[key] is not None and (type(profile[key]) not in (int, float)
                                       or not math.isfinite(profile[key]) or profile[key] < 0):
            raise ValueError(f"{key} must be a nonnegative number or null.")
    for key in ("home_airport", "passport_nationality", "residency"):
        if profile[key] is not None and not isinstance(profile[key], str):
            raise ValueError(f"{key} must be text or null.")
    if len(profile["currency"]) != 3 or not profile["currency"].isupper():
        raise ValueError("Currency must be a three-letter uppercase code.")
    if len(profile["policy_text"]) > 16000:
        raise ValueError("Policy text exceeds 16000 characters; import the relevant travel passages.")
    if not isinstance(profile["policy_rules"], dict):
        raise ValueError("Policy rules must be a JSON object.")
    if profile["visit_date_flexibility"] not in (None, "flexible", "somewhat_flexible", "fixed"):
        raise ValueError("Visit-date flexibility must be flexible, somewhat_flexible, fixed or null.")


def leave_required(option, profile):
    """Charge every workday overlapped by door-to-door absence, including recovery."""
    if (option.get("departure") is None or option.get("return_arrival") is None
            or any(s.get("paid_by") == "company" for s in option.get("legs", []))):
        return {"days": None, "dates": [], "leave_home_at": None,
                "ready_for_work_at": None, "provisional": True}
    zone = ZoneInfo(profile["timezone"])
    start = (aware(option["departure"]) - timedelta(minutes=
        profile["outbound_transfer_minutes"] + profile["airport_buffer_minutes"])).astimezone(zone)
    end = (aware(option["return_arrival"]) + timedelta(minutes=
        profile["home_transfer_minutes"] + profile["recovery_minutes"])).astimezone(zone)
    if end <= start or end - start > timedelta(days=366):
        raise ValueError("Trip must last between zero and 366 days.")
    days = []
    current = start.date()
    while current <= end.date():
        if current.weekday() in profile["workdays"] and current.isoformat() not in profile["holidays"]:
            work_start = datetime.combine(current, time.fromisoformat(profile["work_start"]), zone)
            work_end = datetime.combine(current, time.fromisoformat(profile["work_end"]), zone)
            if start < work_end and end > work_start:
                days.append(current.isoformat())
        current += timedelta(days=1)
    return {"days": len(days), "dates": days, "leave_home_at": start.isoformat(),
            "ready_for_work_at": end.isoformat(),
            "provisional": not (profile["calendar_confirmed"] and profile["timing_confirmed"])}


def evidence_fresh(evidence, at=None):
    at = at or datetime.now(timezone.utc)
    age = (at - aware(evidence["checked_at"])).total_seconds()
    hours = {"price": 6, "availability": 6, "entry": 168,
             "policy": 720, "schedule": 24, "destination": 720}[evidence["kind"]]
    return 0 <= age <= hours * 3600


def assess(option, profile, at=None):
    at = at or datetime.now(timezone.utc)
    leave = leave_required(option, profile)
    blockers = list(option["unresolved"])
    blockers.append("Supplier claims have not been independently verified for the exact flights, dates, travelers and room.")
    if leave["days"] is None:
        blockers.append("Flight times or confirmed business-work dates are missing; personal leave cannot be calculated.")
    if not profile["home_airport"]:
        blockers.append("Home airport has not been confirmed.")
    if leave["provisional"]:
        blockers.append("Leave estimate uses unconfirmed work-calendar or transfer assumptions.")
    if not profile["policy_text"] or not profile["policy_confirmed"]:
        blockers.append("Employer policy is unconfirmed.")
    if not profile["passport_nationality"] or not profile["residency"]:
        blockers.append("Passport nationality/residency are missing; entry eligibility is unconfirmed.")
    if option["policy_status"] != "pass":
        blockers.append(f"Employer-policy assessment: {option['policy_status']}.")
    if option["departure"] is not None and aware(option["departure"]) <= at:
        blockers.append("Departure is in the past.")
    if option["currency"] != profile["currency"]:
        blockers.append("Cost currency differs from profile; comparison needs a checked exchange rate.")
    if not option["cost_complete"] or option["total_cost"] is None:
        blockers.append("Total cost has missing components.")
    fresh = {(e["kind"], e["scope"]) for e in option["evidence"]
             if e["finding"].strip() and evidence_fresh(e, at) and (
                 e["source_type"] != "profile_policy" or policy_evidence_valid(e, profile))}
    for kind, scope in (("price", "trip"), ("availability", "outbound"),
                        ("availability", "return"), ("availability", "accommodation"),
                        ("entry", "trip"), ("policy", "trip"),
                        ("schedule", "outbound"), ("schedule", "return")):
        if (kind, scope) not in fresh:
            blockers.append(f"Missing or stale {scope} {kind} evidence.")
    return {"leave": leave, "blockers": list(dict.fromkeys(blockers)),
            "status": "needs_checks" if blockers else "researched",
            "booking_status": "not_booked"}


def rank_options(result, profile, constraints=None, at=None, *, planning_request=None):
    constraints = constraints or {}
    found = []
    for option in (result or {}).get("options", []):
        assessment = assess(option, profile, at)
        if planning_request and planning_request.get("business_anchor"):
            assessment["leave"].update(days=None, dates=[], provisional=True)
            assessment["blockers"].append("Business working dates are unconfirmed; personal leave remains unknown.")
            assessment["status"] = "needs_checks"
            anchor = planning_request["business_anchor"]
            airports = {"LON": {"LHR", "LGW", "LCY", "STN", "LTN", "SEN"},
                        "NYC": {"JFK", "EWR", "LGA"}}.get(anchor, set())
            route_airports = {leg.get(key) for leg in option.get("legs", []) for key in ("origin", "destination")}
            if not airports.intersection(route_airports):
                city = {"LON": "London", "NYC": "New York"}.get(anchor, "work city")
                assessment["blockers"].append(f"Saved flights do not include the selected {city} work visit.")
        cost_limit = constraints.get("max_cost", profile["budget"])
        leave_limit = constraints.get("max_leave", profile["max_leave"])
        if cost_limit is not None and (not option["cost_complete"] or option["total_cost"] is None or option["currency"] != profile["currency"] or
                                      option["total_cost"] > cost_limit):
            continue
        if leave_limit is not None and (assessment["leave"]["days"] is None or assessment["leave"]["days"] > leave_limit):
            continue
        if constraints.get("no_overnight") and overnight(option) is not False:
            continue
        if constraints.get("departure_date") and (option["departure"] is None or aware(option["departure"]).astimezone(
                ZoneInfo(profile["timezone"])).date().isoformat() != constraints["departure_date"]):
            continue
        found.append(option | {"assessment": assessment})
    # Never compare unlike currencies as though they were interchangeable.
    return sorted(found, key=lambda o: (o["policy_status"] == "fail",
        o["assessment"]["leave"]["days"] if o["assessment"]["leave"]["days"] is not None else math.inf,
        o["currency"] != profile["currency"], o["total_cost"] if o["total_cost"] is not None else math.inf,
        -(o["usable_hours"] or 0)))


def policy_digest(profile):
    return hashlib.sha256(profile["policy_text"].encode()).hexdigest()


def policy_evidence_valid(evidence, profile):
    return (evidence["source_type"] == "profile_policy" and evidence["kind"] == "policy"
            and evidence["policy_digest"] == policy_digest(profile)
            and bool(evidence["policy_excerpt"].strip())
            and evidence["policy_excerpt"] in profile["policy_text"])


def overnight(option):
    """Conservative red-eye classification from both flights' local clock times."""
    unknown = False
    segments = [(s["departure"], s["arrival"]) for s in option.get("legs", [])] or [
        (option["departure"], option["destination_arrival"]),
        (option["return_departure"], option["return_arrival"])]
    for departure_value, arrival_value in segments:
        if departure_value is None or arrival_value is None:
            unknown = True
            continue
        departure, arrival = aware(departure_value), aware(arrival_value)
        # Early local arrivals or departures require sleeping hours even without a date change.
        if departure.hour < 6 or arrival.hour < 6 or (departure.hour >= 20 and
                (arrival.date() > departure.date() or arrival.hour < departure.hour)):
            return True
    if option["overnight_flight"] is True:
        return True
    return None if unknown else False


def validate_result(result, *, home_airport=None):
    """Validate untrusted model output without an optional dependency."""
    import json
    from pathlib import Path
    schema_name = "research.activities.schema.json" if isinstance(result, dict) and "activity_plans" in result else "research.schema.json"
    schema = json.loads(Path(__file__).with_name(schema_name).read_text())

    def check(value, spec, path):
        if isinstance(spec["type"], list):
            if value is None and "null" in spec["type"]:
                return
            spec = spec | {"type": next(t for t in spec["type"] if t != "null")}
        expected = {"object": dict, "array": list, "string": str, "number": (int, float),
                    "boolean": bool}
        if not isinstance(value, expected[spec["type"]]) or (
                spec["type"] == "number" and (isinstance(value, bool) or not math.isfinite(value))):
            raise ValueError(f"Invalid {path}: expected {spec['type']}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"Invalid {path}: unexpected value")
        if isinstance(value, dict):
            if set(value) != set(spec["properties"]):
                raise ValueError(f"Invalid fields at {path}")
            for key, item in value.items():
                check(item, spec["properties"][key], f"{path}.{key}")
        elif isinstance(value, list):
            if len(value) > 40:
                raise ValueError(f"Too many items at {path}")
            for item in value:
                check(item, spec["items"], path + "[]")
        elif isinstance(value, str) and len(value) > 6000:
            raise ValueError(f"Text too long at {path}")

    check(result, schema, "research")
    if len(result["options"]) > 8:
        raise ValueError("Research must contain at most 8 alternatives.")
    ids = set()
    for option in result["options"]:
        if not option["id"] or option["id"] in ids:
            raise ValueError("Alternative IDs must be nonempty and unique.")
        ids.add(option["id"])
        leg_ids = set()
        last_arrival = None
        for leg in option["legs"]:
            if not leg["id"] or leg["id"] in leg_ids:
                raise ValueError("Flight leg IDs must be nonempty and unique within an option.")
            leg_ids.add(leg["id"])
            for field in ("origin", "destination"):
                if len(leg[field]) != 3 or not leg[field].isalpha() or not leg[field].isupper():
                    raise ValueError("Flight leg airports must be three-letter uppercase codes.")
            depart = aware(leg["departure"]) if leg["departure"] is not None else None
            arrive = aware(leg["arrival"]) if leg["arrival"] is not None else None
            if depart and arrive and depart >= arrive:
                raise ValueError("A flight leg must arrive after departure.")
            if depart and last_arrival and depart < last_arrival:
                raise ValueError("Flight legs overlap in time.")
            last_arrival = arrive
        legs = option["legs"]
        if legs:
            if home_airport and (legs[0]["origin"] != home_airport or legs[-1]["destination"] != home_airport):
                raise ValueError("Travel legs must start and finish at the confirmed home airport.")
            for previous, following in zip(legs, legs[1:]):
                if previous["destination"] != following["origin"]:
                    raise ValueError("Travel legs are disconnected; include airport changes as explicit supported ground-transfer legs.")
            for envelope, leg, field in (("departure", legs[0], "departure"),
                                         ("return_arrival", legs[-1], "arrival")):
                if option[envelope] is not None and leg[field] is not None and aware(option[envelope]) != aware(leg[field]):
                    raise ValueError("Trip boundary timestamps must match the first and last travel legs.")
            for leg in legs:
                for field in ("departure", "arrival"):
                    if leg[field] is None:
                        continue
                    timestamp = aware(leg[field])
                    if ((option["departure"] is not None and timestamp < aware(option["departure"]))
                            or (option["return_arrival"] is not None and timestamp > aware(option["return_arrival"]))):
                        raise ValueError("Travel-leg timestamps must fall within the trip boundaries.")
        times = [aware(option[k]) if option[k] is not None else None for k in
                 ("departure", "destination_arrival", "return_departure", "return_arrival")]
        known = [t for t in times if t is not None]
        if any(a >= b for a, b in zip(known, known[1:])) or (len(known) > 1 and
                known[-1] - known[0] > timedelta(days=366)):
            raise ValueError("Flight timestamps are out of order or exceed 366 days.")
        if (option["total_cost"] is not None and option["total_cost"] < 0) or (
                option["usable_hours"] is not None and option["usable_hours"] < 0):
            raise ValueError("Invalid cost or usable destination time.")
        if option["cost_complete"] and option["total_cost"] is None:
            raise ValueError("A complete trip cost requires a numeric total.")
        if times[1] and times[2] and option["usable_hours"] is not None and option["usable_hours"] > (
                times[2] - times[1]).total_seconds() / 3600:
            raise ValueError("Usable hours exceed the time at destination.")
        if len(option["currency"]) != 3 or not option["currency"].isalpha() or not option["currency"].isupper():
            raise ValueError("Option currency must be a three-letter uppercase code.")
        for evidence in option["evidence"]:
            if evidence["source_type"] == "public_url":
                url = urlparse(evidence["url"])
                if url.scheme != "https" or not url.hostname or url.username or url.password:
                    raise ValueError("Public evidence must use an HTTPS URL without credentials.")
                if evidence["policy_digest"] or evidence["policy_excerpt"]:
                    raise ValueError("Public evidence must not contain private policy material.")
            elif evidence["kind"] != "policy" or evidence["url"]:
                raise ValueError("Private policy evidence must use a policy digest and excerpt without a URL.")
            if not evidence["finding"].strip():
                raise ValueError("Evidence findings cannot be empty.")
            if aware(evidence["checked_at"]) > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("Evidence cannot be checked in the future.")
    plan_ids = set()
    for plan in result.get("activity_plans", []):
        if plan["option_id"] not in ids or plan["option_id"] in plan_ids:
            raise ValueError("Activity plans must refer to unique existing alternative IDs.")
        plan_ids.add(plan["option_id"])
        option = next(item for item in result["options"] if item["id"] == plan["option_id"])
        source_urls = {item["url"] for item in option["evidence"] if item["source_type"] == "public_url"}
        for link in plan["route_links"]:
            url = urlparse(link["url"])
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or link["url"] not in source_urls):
                raise ValueError("Activity route links must match HTTPS public evidence for the same alternative.")
    if "activity_plans" in result and plan_ids != ids:
        raise ValueError("Every alternative requires its own structured activity plan.")
    return result
