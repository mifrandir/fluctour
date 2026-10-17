"""Local composition of public research and private travel preferences."""

import copy
import hashlib
import json

from .planning import policy_digest, validate_result
from .research import fingerprint
from .store import Store, now


def attach_public_research(store, trip_id, public_trip_id, personal_leg_index=1):
    """Attach public findings locally; no profile or employer facts leave the machine."""
    trip = store.trip(trip_id)
    if type(personal_leg_index) is not int or not 0 <= personal_leg_index <= 2:
        raise ValueError("Personal leg index must be 0, 1 or 2.")
    public = Store(store.root / "public-research")
    try:
        public.trip(public_trip_id)
        result = public.latest_result(public_trip_id)
    finally:
        public.db.close()
    if result is None:
        raise ValueError("Public research has no completed checkpoint.")
    source_fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    profile = store.profile()
    existing = store.latest_result(trip_id)
    if existing and (existing.get("_public_reference"), existing.get("_source_fingerprint"),
                     existing.get("_profile"), existing.get("_personal_leg_index")) == (
            public_trip_id, source_fingerprint, fingerprint(profile, "", []), personal_leg_index):
        return existing
    result = copy.deepcopy({k: v for k, v in result.items() if not k.startswith("_")})
    for option in result["options"]:
        legs = option["legs"]
        if len(legs) == 3:
            for index, leg in enumerate(legs):
                leg["paid_by"] = "personal" if index == personal_leg_index else "company"
            option["unresolved"].append(
                "Payer allocation is a local proposal, not employer approval. Obtain one joint quote for the two company-paid Cathay business legs and a comparable Cathay business roundtrip; require the joint total to be strictly lower.")
        if profile["policy_text"]:
            option["evidence"].append({
                "kind": "policy", "scope": "trip", "source_type": "profile_policy", "url": "",
                "policy_digest": policy_digest(profile), "policy_excerpt": profile["policy_text"][:2000],
                "checked_at": now(), "finding": "User-reported benefit rules; annual usage, reset dates, written ticketing terms and trip approval remain unknown.",
            })
            option["policy_status"] = "unknown"
    validate_result(result)
    feedback = [{"text": f["text"], "constraints": f["constraints"]} for f in store.feedback(trip_id)]
    result.update(_context=fingerprint(profile, trip["brief"], feedback),
                  _profile=fingerprint(profile, "", []), _feedback_id=0,
                  _stages=["public_reference"], _public_reference=public_trip_id,
                  _source_fingerprint=source_fingerprint, _personal_leg_index=personal_leg_index)
    with store.worker_lock():
        run = store.begin_run(trip_id, "local_composition")
        store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
                         result=result)
    return result
