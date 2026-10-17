"""Local planning context from explicitly saved preferences and structured rules.

This context is for the local UI and local assessment only. It must not be added
to a public research prompt. Business benefits apply only when request selects
business_anchor LON or NYC. No passport, raw policy passage, account credential,
activity history or arbitrary profile text is copied into the result.

Recorded usage follows policy.py's explicit benefit_year={start,end} and
used={LON:count,NYC:count} contract. Remaining allowance requires request dates
inside that recorded year. Neither resets nor prior usage are inferred.
"""

import copy
from datetime import date, time
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ANCHORS = {"LON": "London", "NYC": "New York"}
FLEXIBILITY = {"flexible": "Dates are flexible.", "somewhat_flexible": "Dates have some flexibility.",
               "fixed": "Dates are fixed."}


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _clock(value):
    if not isinstance(value, str) or len(value) > 15:
        return None
    try:
        clock = time.fromisoformat(value)
        return clock.isoformat(timespec="minutes") if clock.tzinfo is None else None
    except ValueError:
        return None


def _count(value):
    return value if type(value) is int and 0 <= value <= 100000 else None


def planning_context(profile, request):
    """Return allowlisted local facts; rule availability never implies approval."""
    if not isinstance(profile, dict) or not isinstance(request, dict):
        raise ValueError("Profile and planning request must be objects.")
    anchor = request.get("business_anchor", "")
    if not isinstance(anchor, str) or anchor not in ("", "LON", "NYC"):
        raise ValueError("Business anchor must be empty, LON or NYC.")
    airport = profile.get("home_airport")
    airport = airport if isinstance(airport, str) and re.fullmatch(r"[A-Z]{3}", airport) else None
    zone = profile.get("timezone")
    try:
        if not isinstance(zone, str):
            raise ValueError
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        zone = None
    start, end = _clock(profile.get("work_start")), _clock(profile.get("work_end"))
    if start is None or end is None or start >= end:
        start = end = None
    days = profile.get("workdays")
    days = sorted(set(days)) if isinstance(days, list) and all(
        type(day) is int and 0 <= day <= 6 for day in days) else None
    flexibility = profile.get("visit_date_flexibility")
    flexibility = flexibility if isinstance(flexibility, str) and flexibility in FLEXIBILITY else None
    context = {
        "home_airport": airport, "timezone": zone,
        "currency": profile.get("currency") if isinstance(profile.get("currency"), str)
            and re.fullmatch(r"[A-Z]{3}", profile["currency"]) else None,
        "work_schedule": {"start": start, "end": end, "workdays": days,
                          "calendar_confirmed": profile.get("calendar_confirmed") is True,
                          "timing_confirmed": profile.get("timing_confirmed") is True},
        "date_flexibility": flexibility, "business_anchor": anchor,
        "anchor_city": ANCHORS.get(anchor), "employer_benefit": None,
        "leave_status": "requires_itinerary", "personal_leave_days": None,
        "employer_approval": "not_verified", "facts": [], "blockers": [],
    }
    facts, blockers = context["facts"], context["blockers"]
    if airport:
        facts.append(f"Home airport: {airport}.")
    else:
        blockers.append("Home airport is unknown.")
    if start and end:
        facts.append(f"Saved work hours: {start}–{end}.")
    if flexibility:
        facts.append(FLEXIBILITY[flexibility])
    if not context["work_schedule"]["calendar_confirmed"]:
        blockers.append("Work calendar and employer holidays are unconfirmed.")
    if not context["work_schedule"]["timing_confirmed"]:
        blockers.append("Airport transfers, airport buffers and recovery assumptions are unconfirmed.")
    if not anchor:
        return context

    context["leave_status"] = "business_work_dates_unconfirmed"
    blockers.append("Business working dates are unconfirmed; personal leave remains unknown.")
    city = ANCHORS[anchor]
    rules = profile.get("policy_rules")
    rules = rules if isinstance(rules, dict) else {}
    allowances = rules.get("allowances")
    annual = _count(allowances.get(anchor)) if isinstance(allowances, dict) else None
    basis = rules.get("provenance")
    basis = basis if basis in ("user_reported", "written_policy") else "unknown"
    benefit = {
        "annual_allowance": annual, "used": None, "allowance_remaining": None,
        "benefit_year": None, "reset_date": None, "policy_basis": basis,
        "written_policy_verified": rules.get("written_policy_verified") is True,
        "flight_conditions": {
            "carrier": "CX" if rules.get("carrier") == "CX" else None,
            "cabin": "business" if rules.get("cabin") == "business" else None,
            "personal_leg_count": 1 if type(rules.get("triangle_personal_legs")) is int
                and rules["triangle_personal_legs"] == 1 else None,
            "comparison": "strictly_less" if rules.get("comparison") == "strictly_less" else None,
        },
    }
    conditions = benefit["flight_conditions"]
    benefit["triangle_rule_known"] = all(value is not None for value in conditions.values())
    year = rules.get("benefit_year")
    if isinstance(year, dict):
        first, last = _date(year.get("start")), _date(year.get("end"))
        if first and last and first <= last and (last - first).days <= 366:
            benefit["benefit_year"] = {"start": first.isoformat(), "end": last.isoformat()}
            used = rules.get("used")
            benefit["used"] = _count(used.get(anchor)) if isinstance(used, dict) else None
            trip_start, trip_end = _date(request.get("start_date")), _date(request.get("end_date"))
            if (annual is not None and benefit["used"] is not None and trip_start and trip_end
                    and first <= trip_start <= trip_end <= last):
                benefit["allowance_remaining"] = max(0, annual - benefit["used"])
            reset = _date(rules.get("reset_date"))
            if reset and reset > last:
                benefit["reset_date"] = reset.isoformat()
    context["employer_benefit"] = benefit
    facts.append(f"Business anchor: {city}.")
    if annual is not None:
        facts.append(f"Recorded {city} benefit: {annual} trips per benefit year.")
    else:
        blockers.append(f"The annual {city} benefit is unknown.")
    remaining = benefit["allowance_remaining"]
    if remaining is None:
        blockers.append(f"Remaining {city} allowance is unknown; usage, benefit year and applicable dates need confirmation.")
    elif remaining == 0:
        blockers.append(f"The recorded {city} allowance is exhausted for these dates.")
    if benefit["triangle_rule_known"]:
        facts.append("One triangle leg is personal. The joint Cathay business quote for the other two must be strictly below an equivalent business return.")
        blockers.append("Triangle eligibility requires current comparable joint and return quotes; no fares or savings are inferred.")
    else:
        blockers.append("Triangle flight conditions are unknown; do not assume employer payment.")
    if not benefit["written_policy_verified"]:
        blockers.append("Written employer policy and ticketing terms are unverified.")
    blockers.append("Employer approval for this itinerary is unverified.")
    return context


def annotate_options(options, profile, request):
    """Attach local context to UI options without inferring fares or company payers."""
    if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
        raise ValueError("Options must be a list of objects.")
    context = planning_context(profile, request)
    annotated = copy.deepcopy(options)
    for option in annotated:
        option["planning_context"] = copy.deepcopy(context)
        if context["business_anchor"] and isinstance(option.get("assessment"), dict):
            assessment = option["assessment"]
            if isinstance(assessment.get("leave"), dict):
                assessment["leave"].update(days=None, dates=[], provisional=True)
            blockers = assessment.setdefault("blockers", [])
            reason = "Business working dates are unconfirmed; personal leave remains unknown."
            if reason not in blockers:
                blockers.append(reason)
            assessment["status"] = "needs_checks"
    return annotated
