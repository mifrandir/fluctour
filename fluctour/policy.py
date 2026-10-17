"""Conservative Cathay triangle comparisons over supplied JSON evidence.

``assess_triangle(request, profile, *, as_of=None)`` accepts:

    {
      "business_destination": "LON" | "NYC",
      "business_attendance": {"start": aware_ISO_timestamp, "end": aware_ISO_timestamp},
      "legs": [leg, leg, leg],
      "personal_leg_id": "one of the three leg IDs",
      "company_quote": quote + {"leg_ids": [the other two IDs], "ticketing": "joint",
                                "carrier": "CX", "cabin": "business"},
      "baseline_quote": quote + {"legs": [leg, leg], "carrier": "CX", "cabin": "business",
                                 "business_attendance": {"start": ..., "end": ...}},
      "personal_quote": quote + {"leg_ids": [personal_leg_id]}  # optional
    }

A leg has ``id``, ``origin``, ``destination`` (uppercase airport codes),
``departure``, ``arrival`` (aware ISO timestamps), ``carrier`` and ``cabin``.
A quote has ``amount`` (prefer a decimal string), ``currency`` (three uppercase
letters), ``checked_at`` (aware ISO timestamp) and ``source_url`` (HTTPS).
Quotes must be no more than six hours old and cannot be dated in the future.
The company amount is the supplied JOINT two-leg total, never allocated from
individual fares. Amounts are returned as decimal strings, without conversion.
Baseline legs must form HKG-business-HKG and cover the exact attendance interval
declared by the request, as must the business stop in the triangle. This checks
attendance equivalence, not independent fare-rule or ticket comparability.

Annual allowance limits are two LON trips and one NYC trip. Remaining allowance
requires profile.policy_rules.benefit_year={start: ISO_date, end: ISO_date}
(inclusive) and policy_rules.used={LON: nonnegative_int, NYC: nonnegative_int}.
The attendance dates must fall inside that explicitly recorded benefit year.
No usage or reset date is inferred from previous itineraries or today's year.

Malformed itinerary data returns ``invalid_itinerary``; unavailable, stale or
invalid required quote data returns ``needs_quotes``. A valid price comparison
returns ``conditional_pass`` or ``fails_strict_inequality``. A conditional pass
is arithmetic only: allowance, written terms, supplier verification and employer
approval remain separate blockers. This function never approves or books travel.
``as_of`` is an aware datetime supplied by trusted calling code, not request JSON.
"""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from urllib.parse import urlparse


_BUSINESS_AIRPORTS = {
    "LON": {"LON", "LHR", "LGW", "LCY", "LTN", "STN", "SEN"},
    "NYC": {"NYC", "JFK", "EWR", "LGA"},
}
_ALLOWANCES = {"LON": 2, "NYC": 1}


def _instant(value, label):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError(f"{label} requires an aware ISO timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{label} requires an aware ISO timestamp.") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{label} requires a UTC offset.")
    return result


def _attendance(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} requires start and end timestamps.")
    start, end = [_instant(value.get(key), f"{label}.{key}") for key in ("start", "end")]
    if end <= start:
        raise ValueError(f"{label}.end must follow start.")
    return start, end


def _legs(value, count, label):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{label} must contain exactly {count} chronological connected legs.")
    parsed = []
    ids = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError(f"{label} legs must be objects.")
        leg_id = raw.get("id")
        if not isinstance(leg_id, str) or not leg_id.strip() or leg_id in ids:
            raise ValueError(f"{label} requires nonempty unique leg IDs.")
        ids.add(leg_id)
        for field in ("origin", "destination"):
            if not isinstance(raw.get(field), str) or not re.fullmatch(r"[A-Z]{3}", raw[field]):
                raise ValueError(f"{label} {field} requires a three-letter airport code.")
        if raw["origin"] == raw["destination"]:
            raise ValueError(f"{label} contains a leg with identical endpoints.")
        for field in ("carrier", "cabin"):
            if not isinstance(raw.get(field), str) or not raw[field].strip():
                raise ValueError(f"{label} requires each leg's {field}.")
        leg = raw | {"departure": _instant(raw.get("departure"), f"{label} departure"),
                     "arrival": _instant(raw.get("arrival"), f"{label} arrival")}
        if leg["arrival"] <= leg["departure"]:
            raise ValueError(f"{label} leg arrival must follow departure in absolute time.")
        if parsed and (parsed[-1]["destination"] != leg["origin"] or
                       parsed[-1]["arrival"] >= leg["departure"]):
            raise ValueError(f"{label} legs must be connected and chronological in absolute time.")
        parsed.append(leg)
    if parsed[0]["origin"] != "HKG" or parsed[-1]["destination"] != "HKG":
        raise ValueError(f"{label} must start and end at HKG.")
    return parsed


def _money(value, label):
    if type(value) not in (str, int, float) or len(str(value)) > 256:
        raise ValueError(f"{label} must be a positive finite decimal amount.")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label} must be a positive finite decimal amount.") from None
    if (not amount.is_finite() or amount <= 0 or len(amount.as_tuple().digits) > 200
            or abs(amount.as_tuple().exponent) > 1000):
        raise ValueError(f"{label} must be a positive finite decimal amount of bounded precision.")
    return amount


def _quote(value, label, now):
    if not isinstance(value, dict):
        raise ValueError(f"{label} is missing; a current sourced quote is required.")
    amount = _money(value.get("amount"), f"{label}.amount")
    currency = value.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{label} requires a three-letter currency.")
    checked = _instant(value.get("checked_at"), f"{label}.checked_at")
    if not timedelta(0) <= now - checked <= timedelta(hours=6):
        raise ValueError(f"{label} is stale or future-dated; obtain a current quote.")
    url = value.get("source_url")
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        valid_url = (parsed and parsed.scheme == "https" and parsed.hostname
                     and not parsed.username and not parsed.password)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError(f"{label} requires an HTTPS source_url without credentials.")
    return amount, currency


def _coverage(value, expected, label):
    supplied = value.get("leg_ids")
    if (not isinstance(supplied, list) or any(not isinstance(i, str) for i in supplied)
            or len(supplied) != len(expected) or set(supplied) != set(expected)):
        raise ValueError(f"{label} must cover exactly leg IDs {', '.join(expected)}.")


def _cathay_business(value, label):
    if value.get("carrier") != "CX" or value.get("cabin") != "business":
        raise ValueError(f"{label} must explicitly quote Cathay (CX) business class.")


def _allowance(profile, destination, attendance, blockers):
    remaining = {key: None for key in _ALLOWANCES}
    rules = profile.get("policy_rules") if isinstance(profile, dict) else None
    rules = rules if isinstance(rules, dict) else {}
    year, used = rules.get("benefit_year"), rules.get("used")
    if not isinstance(year, dict) or not isinstance(used, dict):
        blockers.append("Benefit-year boundaries and annual usage are unknown; remaining allowance is unknown.")
        return remaining, rules
    try:
        start, end = [date.fromisoformat(year[key]) for key in ("start", "end")]
        if end < start or (end - start).days > 366:
            raise ValueError
        if not (start <= attendance[0].date() <= attendance[1].date() <= end):
            blockers.append("Business attendance is outside the recorded benefit year; allowance is unknown.")
            return remaining, rules
    except (KeyError, TypeError, ValueError):
        blockers.append("Benefit-year boundaries are invalid; remaining allowance is unknown.")
        return remaining, rules
    for city, limit in _ALLOWANCES.items():
        count = used.get(city)
        if type(count) is int and count >= 0:
            remaining[city] = max(0, limit - count)
    if destination in remaining:
        if remaining[destination] is None:
            blockers.append(f"{destination} usage is unknown; remaining allowance is unknown.")
        elif remaining[destination] == 0:
            blockers.append(f"The recorded {destination} allowance is exhausted for this benefit year.")
    return remaining, rules


def assess_triangle(request, profile, *, as_of=None):
    """Compare supplied quotes conditionally; never infer approval or book travel."""
    now = as_of if as_of is not None else datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime.")
    result = {
        "comparison": "invalid_itinerary", "company_total": None, "baseline_total": None,
        "personal_total": None, "savings": None, "currency": None, "blockers": [],
        "allowance_remaining": {"LON": None, "NYC": None},
        "allowance_limits": dict(_ALLOWANCES), "employer_approval": "not_verified",
        "quote_verification": "supplied_not_independently_verified", "policy_basis": "user_reported",
        "personal_leg": None,
    }
    blockers = result["blockers"]
    try:
        if not isinstance(request, dict):
            raise ValueError("Triangle request must be an object.")
        destination = request.get("business_destination")
        if destination not in _BUSINESS_AIRPORTS:
            raise ValueError("Business destination must be LON or NYC.")
        attendance = _attendance(request.get("business_attendance"), "business_attendance")
        legs = _legs(request.get("legs"), 3, "triangle")
        if len({leg["origin"] for leg in legs}) != 3:
            raise ValueError("A triangle must visit two distinct stops before returning to HKG.")
        personal_id = request.get("personal_leg_id")
        if personal_id not in [leg["id"] for leg in legs]:
            raise ValueError("personal_leg_id must identify exactly one triangle leg.")
        company_ids = [leg["id"] for leg in legs if leg["id"] != personal_id]
        personal = next(leg for leg in legs if leg["id"] == personal_id)
        result["personal_leg"] = {key: personal[key] for key in
                                  ("id", "origin", "destination", "carrier", "cabin")}
        for leg in legs:
            if leg["id"] != personal_id:
                _cathay_business(leg, f"Company-paid leg {leg['id']}")
        stops = [(legs[i]["arrival"], legs[i + 1]["departure"]) for i in range(2)
                 if legs[i]["destination"] in _BUSINESS_AIRPORTS[destination]]
        if len(stops) != 1 or not (stops[0][0] <= attendance[0] < attendance[1] <= stops[0][1]):
            raise ValueError("The triangle business stop must cover the declared business attendance interval.")
    except (ValueError, TypeError) as error:
        blockers.append(str(error))
        return result

    remaining, rules = _allowance(profile, destination, attendance, blockers)
    result["allowance_remaining"] = remaining
    result["policy_basis"] = rules.get("provenance", "user_reported")
    if rules.get("written_policy_verified") is not True:
        blockers.append("Employer rule is user-reported; written policy and ticketing terms are not verified.")
    blockers.append("Employer approval for this trip is not verified; a price comparison is conditional only.")
    blockers.append("Supplier quotes, tax inclusion, fare rules and joint ticketability require independent verification.")

    result["comparison"] = "needs_quotes"
    company, baseline = request.get("company_quote"), request.get("baseline_quote")
    quotes = {}
    for key, raw in (("company", company), ("baseline", baseline)):
        try:
            amount, currency = _quote(raw, f"{key}_quote", now)
            _cathay_business(raw, f"{key}_quote")
            if key == "company":
                _coverage(raw, company_ids, "company_quote")
                if raw.get("ticketing") != "joint":
                    raise ValueError("company_quote requires a joint two-leg quote; per-leg fare allocation is invalid.")
            else:
                baseline_legs = _legs(raw.get("legs"), 2, "baseline")
                for leg in baseline_legs:
                    _cathay_business(leg, f"Baseline leg {leg['id']}")
                if baseline_legs[0]["destination"] not in _BUSINESS_AIRPORTS[destination]:
                    raise ValueError("Baseline must be a roundtrip to the same business destination.")
                baseline_attendance = _attendance(raw.get("business_attendance"), "baseline attendance")
                if baseline_attendance != attendance or not (
                        baseline_legs[0]["arrival"] <= attendance[0] < attendance[1]
                        <= baseline_legs[1]["departure"]):
                    raise ValueError("Baseline must cover equivalent business attendance dates.")
            quotes[key] = amount, currency
            result[f"{key}_total"] = str(amount)
        except (ValueError, TypeError) as error:
            blockers.append(str(error))

    personal_quote = request.get("personal_quote")
    personal_quote_invalid = False
    if personal_quote is not None:
        try:
            amount, currency = _quote(personal_quote, "personal_quote", now)
            _coverage(personal_quote, [personal_id], "personal_quote")
            quotes["personal"] = amount, currency
            result["personal_total"] = str(amount)
        except (ValueError, TypeError) as error:
            blockers.append(str(error))
            personal_quote_invalid = True
    else:
        blockers.append("The personally paid leg has no current quote; personal cost is unknown.")

    if "company" not in quotes or "baseline" not in quotes or personal_quote_invalid:
        return result
    currencies = {currency for _, currency in quotes.values()}
    if len(currencies) != 1:
        blockers.append("Quote currencies differ; no comparison or conversion is inferred.")
        return result
    result["currency"] = quotes["company"][1]
    company_amount, baseline_amount = quotes["company"][0], quotes["baseline"][0]
    with localcontext() as context:
        context.prec = 2200
        result["savings"] = str(baseline_amount - company_amount)
    result["comparison"] = ("conditional_pass" if company_amount < baseline_amount
                            else "fails_strict_inequality")
    if company_amount >= baseline_amount:
        blockers.append("Company-paid joint total must be strictly less than the Cathay business roundtrip baseline.")
    return result
