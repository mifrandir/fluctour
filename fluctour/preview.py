"""Browser snapshots with sourced activity plans and local application controls."""

import json
from pathlib import Path
from urllib.parse import urlparse

from .cli import view
from .store import now
from .jobs import job_state


def preview_data(store, trip_id):
    snapshot = view(store, trip_id) if trip_id else {"options": [], "trip": None}
    profile = store.profile()
    plans = store.plans(trip_id) if trip_id else {}
    options = []
    for option in snapshot["options"]:
        evidence = []
        for item in option["evidence"]:
            if item["source_type"] == "profile_policy" or item["kind"] == "policy":
                continue
            parsed = urlparse(item["url"])
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                evidence.append({k: item[k] for k in ("kind", "scope", "url", "checked_at", "finding")})
        options.append({k: option[k] for k in (
            "id", "title", "destination", "rationale", "legs", "total_cost", "cost_complete", "currency",
            "usable_hours", "tradeoffs", "assessment")} | {"evidence": evidence, "plan": plans.get(option["id"])})
    return {"generated_at": now(), "home_airport": profile["home_airport"],
            "date_flexibility": profile["visit_date_flexibility"],
            "allowances": profile["policy_rules"].get("allowances", {}), "options": options,
            "trip": ({k: snapshot["trip"][k] for k in ("id", "brief")} if trip_id else None),
            "trips": [{"id": t["id"], "brief": t["brief"]} for t in store.trips()],
            "pending_feedback": snapshot.get("pending_feedback", []),
            "feedback_effects": snapshot.get("feedback_effects", {}),
            "budget": snapshot.get("budget", {}), "planning_context": snapshot.get("planning_context", {}),
            "constraints": snapshot.get("constraints", {}), "researched": bool(snapshot.get("research")),
            "job": job_state(store, trip_id) if trip_id else None}


def write_preview(store, trip_id):
    data = json.dumps(preview_data(store, trip_id), ensure_ascii=False)
    # Script data cannot terminate its enclosing element, even for untrusted research text.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = Path(__file__).with_name("preview.html").read_text()
    directory = store.root / "preview"
    directory.mkdir(exist_ok=True, mode=0o700)
    output = directory / "index.html"
    assets = Path(__file__).with_name("assets")
    template = template.replace("__FLUCTOUR_MAP_CSS__", (assets / "leaflet.css").read_text())
    template = template.replace("__FLUCTOUR_MAP_JS__", (assets / "leaflet.js").read_text())
    output.write_text(template.replace("__FLUCTOUR_DATA__", data))
    output.chmod(0o600)
    return output
