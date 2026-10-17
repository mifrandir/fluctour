"""Local interpretation of explicit feedback clauses; unsupported requests stay pending."""

import math
import re
from datetime import date

from .jobs import ACTIVITIES, validate_request


NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
SEPARATOR = re.compile(r"[;\n]+|,(?=\s*[A-Za-z])|\s+and\s+", re.IGNORECASE)
GROUP_SEPARATOR = re.compile(r"[;\n]+")


def _number(text):
    number = float(text.replace(",", ""))
    return number if math.isfinite(number) and number >= 0 else None


def _clauses(text, separator=SEPARATOR):
    start = 0
    for split in list(separator.finditer(text)) + [None]:
        end = split.start() if split else len(text)
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and (text[right - 1].isspace() or text[right - 1] in ".!"):
            right -= 1
        if left < right:
            yield text[left:right], [left, right]
        start = split.end() if split else end


def interpret_feedback(text, currency):
    """Apply only bounded, explicit clauses and retain original evidence for every decision."""
    return _interpret_feedback(text, currency, split_groups=True)


def _interpret_feedback(text, currency, split_groups):
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("Feedback must be text of at most 4000 characters.")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Feedback must contain valid UTF-8 text.") from None
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Use the profile's three-letter uppercase currency.")
    constraints, requirements, unapplied = {}, [], []
    pending_filters = []
    clauses = []
    for group, group_span in _clauses(text, GROUP_SEPARATOR):
        candidates = list(_clauses(group)) if split_groups else [(group, [0, len(group)])]
        if len(candidates) > 1 and all(_interpret_feedback(part, currency, split_groups=False)["requirements"]
                                       for part, _ in candidates):
            clauses.extend((part, [group_span[0] + span[0], group_span[0] + span[1]]) for part, span in candidates)
        else:
            clauses.append((group, group_span))
    def pending(item, reason):
        item.update(status="pending", reason=reason)
        requirements.append(item)
        unapplied.append({"text": item["source_text"], "span": item["span"], "reason": reason})
    for clause, span in clauses:
        item = {"source_text": clause, "span": span}
        lower = clause.casefold()
        value = None
        if lower in ("clear filters", "reset filters"):
            value = {"clear": True}
        elif lower in ("no overnight flights", "avoid overnight flights", "no red-eye flights", "no red eye flights"):
            value = {"no_overnight": True}
        elif lower in ("allow overnight flights", "overnight flights are okay", "overnight flights are ok"):
            value = {"no_overnight": False}
        elif match := re.fullmatch(rf"(?:max(?:imum)?|at most|no more than)\s+({NUMBER})\s+(?:days?\s+(?:of\s+)?leave|leave\s+days?)", clause, re.IGNORECASE):
            number = _number(match[1])
            if number is not None:
                value = {"max_leave": number}
        elif match := re.fullmatch(rf"(under|below|less than|max(?:imum)?(?: cost| budget)?|budget(?: of)?|at most|up to)\s+(?:([A-Za-z]{{3}})\s*)?({NUMBER})\s*([A-Za-z]{{3}})?", clause, re.IGNORECASE):
            before, after = (match[2] or "").upper(), (match[4] or "").upper()
            code = before or after or currency
            amount = _number(match[3])
            strict = match[1].casefold() in ("under", "below", "less than")
            if amount is not None:
                item.update(kind="cost_limit", currency=code, amount=amount, strict=strict)
                if before and after and before != after:
                    pending(item, "The clause contains conflicting currency codes.")
                    continue
                if code != currency:
                    pending(item, "A checked exchange rate is needed before filtering a different currency.")
                    continue
                if strict and amount == 0:
                    pending(item, "No nonnegative cost can be strictly below zero.")
                    continue
                value = {"max_cost": math.nextafter(amount, -math.inf) if strict else amount}
        elif match := re.fullmatch(r"(?:depart(?:ure)?(?: on)?|leave on)\s+(\d{4}-\d{2}-\d{2})", clause, re.IGNORECASE):
            try:
                date.fromisoformat(match[1])
                value = {"departure_date": match[1]}
            except ValueError:
                pass
        elif match := re.fullmatch(r"(?:(?:more|add|include)\s+)?(hiking|hikes|swimming|swims|food|culture|cycling|nature|beach|city|relaxing)", clause, re.IGNORECASE):
            activity = {"hikes": "hiking", "swims": "swimming"}.get(match[1].casefold(), match[1].casefold())
            item.update(kind="activity", activity=activity, intent="include")
            pending(item, "Explicit activity request; new research must be requested separately if saved alternatives do not cover it.")
            continue
        elif match := re.fullmatch(rf"({NUMBER})\s*[-–]\s*({NUMBER})\s*km\s+(?:hikes|hiking)", clause, re.IGNORECASE):
            minimum, maximum = _number(match[1]), _number(match[2])
            if minimum is not None and maximum is not None and minimum <= maximum:
                item.update(kind="hike_distance", minimum_km=minimum, maximum_km=maximum, confirmed_by_user=True)
                pending(item, "Explicit distance request retained locally; route distances have not been filtered or inferred from past activities.")
                continue
        elif lower in ("avoid places already visited", "avoid places i've already visited", "avoid places i have already visited"):
            item.update(kind="avoid_visited", avoid=True)
            pending(item, "Recorded activity locations are incomplete travel history; visited destinations need a local check.")
            continue
        elif lower in ("only verified availability", "only available flights", "only available hotels", "double check availability", "double-check availability"):
            item.update(kind="availability", require_supplier_check=True)
            pending(item, "Exact flights, travelers and rooms need a current supplier check; no availability claim or filter has been applied.")
            continue
        elif match := re.fullmatch(r"(?:i liked|liked|i enjoyed)\s+(?:strava\s+)?activity\s+(\d{1,30})", clause, re.IGNORECASE):
            item.update(kind="liked_activity", activity_id=match[1], confirmed_by_user=True, activity_verified=False)
            pending(item, "User-reported liked activity retained privately; the activity reference and relevant route details still need checking.")
            continue
        if value is not None:
            item.update(kind="filter", status="applied", constraints=value)
            pending_filters.append(item)
        else:
            unapplied.append({"text": clause, "span": span,
                              "reason": "This clause has not been interpreted or applied."})
    clear_mixed = len(clauses) > 1 and any("clear" in item["constraints"] for item in pending_filters)
    conflicts = {key for key in ("no_overnight", "departure_date")
                 if len({item["constraints"][key] for item in pending_filters if key in item["constraints"]}) > 1}
    for item in pending_filters:
        if clear_mixed or any(key in conflicts for key in item["constraints"]):
            pending(item, "Conflicting filters need clarification; submit filter resets separately.")
            continue
        for key, value in item["constraints"].items():
            constraints[key] = min(constraints[key], value) if key in ("max_cost", "max_leave") and key in constraints else value
        requirements.append(item)
    requirements.sort(key=lambda item: item["span"][0])
    unapplied.sort(key=lambda item: item["span"][0])
    return {"constraints": constraints, "requirements": requirements, "unapplied": unapplied}


def derive_public_request_update(request, interpretation):
    """Add explicitly requested activity enums; raw feedback never enters public research."""
    current = validate_request(request)
    if not isinstance(interpretation, dict) or not isinstance(interpretation.get("requirements"), list):
        raise ValueError("Provide interpreted local feedback.")
    requested = set()
    for item in interpretation["requirements"]:
        if (isinstance(item, dict) and item.get("kind") == "activity" and item.get("intent") == "include"
                and isinstance(item.get("activity"), str) and item["activity"] in ACTIVITIES):
            requested.add(item["activity"])
    updated = validate_request(current | {"activities": sorted(set(current["activities"]) | requested)})
    return {"request": updated, "changed": updated != current, "applied_activities": sorted(requested)}
