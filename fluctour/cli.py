"""Command-line adapter for persisted travel planning."""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .planning import overnight, rank_options
from .research import budget_state, fingerprint, research
from .store import DEFAULT_PROFILE, Store


def parser():
    root = argparse.ArgumentParser(prog="fluctour", description="Local travel research with durable Codex sessions.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", default=os.environ.get("FLUCTOUR_DATA_DIR", ".fluctour"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize private local state; no accounts or servers required.")
    commands.add_parser("doctor", help="Check local prerequisites without starting a model session.")
    profile = commands.add_parser("profile", help="Show preferences or set one field using a JSON value.")
    profile.add_argument("key", nargs="?", choices=list(DEFAULT_PROFILE))
    profile.add_argument("value", nargs="?")
    policy = commands.add_parser("policy", help="Import employer travel policy as UTF-8 text.")
    policy.add_argument("file", type=Path)
    new = commands.add_parser("new", help="Save a travel brief without starting research.")
    new.add_argument("brief")
    new.add_argument("--token-budget", type=int, default=150000)
    new.add_argument("--max-sessions", type=int, default=6)
    commands.add_parser("list", help="List local travel briefs.")
    run = commands.add_parser("research", help="Start/resume bounded research in fresh Codex sessions.")
    run.add_argument("trip")
    run.add_argument("--steps", type=int, default=3)
    run.add_argument("--timeout", type=int, default=900, help="Seconds per stage.")
    run.add_argument("--refresh", action="store_true")
    public = commands.add_parser("public-research", help="Research a public-only brief without loading the personal profile.")
    public.add_argument("brief", nargs="?")
    public.add_argument("--trip")
    public.add_argument("--steps", type=int, default=3)
    public.add_argument("--timeout", type=int, default=900)
    public.add_argument("--refresh", action="store_true")
    public.add_argument("--show", action="store_true", help="Read existing public research without starting inference.")
    public.add_argument("--for-trip", help="Attach results to this local private trip after research.")
    windows = commands.add_parser("windows", help="Find leave-efficient calendar opportunities without inference.")
    windows.add_argument("--start")
    windows.add_argument("--days", type=int, default=60)
    windows.add_argument("--max-leave", type=int, default=2)
    windows.add_argument("--min-nights", type=int, default=2)
    windows.add_argument("--max-nights", type=int, default=6)
    triangle = commands.add_parser("triangle-check", help="Compare a Cathay triangle against its business roundtrip quote.")
    triangle.add_argument("file", type=Path)
    attach = commands.add_parser("attach-public", help="Combine public findings with private policy locally.")
    attach.add_argument("trip")
    attach.add_argument("public_trip")
    attach.add_argument("--personal-leg", type=int, choices=[0, 1, 2], default=1)
    recovery = commands.add_parser("recover", help="Validate and recover a completed Codex response from a failed import.")
    recovery.add_argument("trip")
    preview = commands.add_parser("preview", help="Create a local browser preview of saved proposals.")
    preview.add_argument("trip", nargs="?")
    server = commands.add_parser("serve", help="Run the local trip-planning application.")
    server.add_argument("--port", type=int, default=8765)
    for name in ("show", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("trip")
        sub.add_argument("--json", action="store_true")
        if name == "show":
            sub.add_argument("--evidence", action="store_true")
    feedback = commands.add_parser("feedback", help="Immediately filter saved alternatives; never starts a model.")
    feedback.add_argument("trip")
    feedback.add_argument("text", nargs="?", default="")
    feedback.add_argument("--max-cost", type=float)
    feedback.add_argument("--max-leave", type=float)
    feedback.add_argument("--no-overnight", action="store_true", default=None)
    feedback.add_argument("--allow-overnight", action="store_true")
    feedback.add_argument("--departure-date")
    feedback.add_argument("--clear", action="store_true", help="Clear accumulated local filters.")
    limits = commands.add_parser("limits", help="Explicitly change a job's cumulative allowance.")
    limits.add_argument("trip")
    limits.add_argument("--token-budget", type=int, required=True)
    limits.add_argument("--max-sessions", type=int, required=True)
    return root


def _feedback_constraints(store, item):
    from .preferences import interpret_feedback
    currency = store.profile()["currency"]
    interpreted = interpret_feedback(item["text"], currency)
    effective = interpreted["constraints"] | item["constraints"]
    row = store.db.execute("SELECT value FROM settings WHERE key=?", ("feedback_context:" + str(item["id"]),)).fetchone()
    source_currency = json.loads(row[0]).get("currency") if row else None
    if source_currency is None and "max_cost" in item["constraints"]:
        for requirement in interpreted["requirements"]:
            if requirement.get("currency") and "amount" in requirement:
                cap = math.nextafter(requirement["amount"], -math.inf) if requirement.get("strict") else requirement["amount"]
                if cap == item["constraints"]["max_cost"]:
                    source_currency = requirement["currency"]
                    break
    currency_changed = bool(source_currency and source_currency != currency and "max_cost" in effective)
    if currency_changed:
        effective.pop("max_cost", None)
    return effective, interpreted, source_currency if currency_changed else None


def constraints_for(store, trip_id):
    constraints = {}
    for item in store.feedback(trip_id):
        effective, _, _ = _feedback_constraints(store, item)
        if effective.get("clear"):
            constraints = {}
        constraints.update({k: v for k, v in effective.items() if k != "clear"})
    return constraints


def view(store, trip_id):
    from .preferences import interpret_feedback
    from .jobs import job_state
    from .personalization import planning_context
    trip = store.trip(trip_id)
    result = store.latest_result(trip_id)
    constraints = constraints_for(store, trip_id)
    planning_request = (job_state(store, trip_id) or {}).get("request", {})
    options = rank_options(result, store.profile(), constraints, planning_request=planning_request)
    if result and planning_request:
        saved_request = result.get("_planning_request")
        public_part = lambda request: {k: v for k, v in request.items() if k != "business_anchor"}
        mismatch = ("Saved options belong to a different request; updated research is pending."
                    if isinstance(saved_request, dict) and public_part(saved_request) != public_part(planning_request)
                    else "The saved options have not been matched to this request."
                    if saved_request is None else None)
        if mismatch:
            for option in options:
                option["assessment"]["blockers"].append(mismatch)
                option["assessment"]["status"] = "needs_checks"
    if result and result.get("_profile") != fingerprint(store.profile(), "", []):
        for option in options:
            option["assessment"]["blockers"].append("Profile changed since research; policy and feasibility need review.")
            option["assessment"]["status"] = "needs_checks"
    applied, pending, activity_changes = [], [], []
    for item in store.feedback(trip_id):
        _, interpreted, changed_currency = _feedback_constraints(store, item)
        outstanding = item["id"] > (result or {}).get("_feedback_id", 0)
        if outstanding:
            pending.extend(requirement["text"] for requirement in interpreted["unapplied"])
            activity_changes.extend({"activity": requirement["activity"], "source_text": requirement["source_text"],
                                     "status": "pending"} for requirement in interpreted["requirements"]
                                    if requirement.get("kind") == "activity")
        for requirement in interpreted["requirements"]:
            if requirement["status"] != "applied":
                continue
            values = requirement["constraints"]
            if changed_currency and "max_cost" in values:
                if outstanding:
                    pending.append(requirement["source_text"])
                continue
            overridden = any(key in item["constraints"] and (
                item["constraints"][key] > value if key in ("max_cost", "max_leave")
                else item["constraints"][key] != value) for key, value in values.items())
            if overridden:
                if outstanding:
                    pending.append(requirement["source_text"])
                continue
            active = all(key == "clear" or (key in constraints and (
                constraints[key] <= value if key in ("max_cost", "max_leave") else constraints[key] == value))
                         for key, value in values.items())
            if active:
                applied.append(requirement["source_text"])
        if changed_currency and not any(requirement.get("currency") for requirement in interpreted["requirements"]):
            pending.append(f"Saved {changed_currency} cost cap needs rechecking after the currency change.")
    effects = {"applied": list(dict.fromkeys(applied)), "pending": list(dict.fromkeys(pending)),
               "recognized_activity_changes": activity_changes}
    return {"trip": trip, "research": result, "constraints": constraints,
            "options": options, "pending_feedback": effects["pending"], "feedback_effects": effects,
            "planning_context": planning_context(store.profile(), planning_request),
            "budget": budget_state(store, trip_id)}


def show(store, trip_id, as_json=False, evidence=False):
    data = view(store, trip_id)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"{trip_id} — {data['trip']['brief']}")
    if not data["research"]:
        print(f"No research yet. Run: python -m fluctour research {trip_id}")
        return
    print(data["research"]["summary"])
    print("Saved stages: " + ", ".join(data["research"].get("_stages", [])))
    for text in data["pending_feedback"]:
        print(f"Pending research feedback: {text}")
    print("Ranking: policy failures last, then fewer leave days, then cost and usable time.")
    for index, option in enumerate(data["options"], 1):
        assessment = option["assessment"]
        leave = assessment["leave"]
        print(f"\n{index}. [{option['id']}] {option['title']}")
        print(f"   {option['departure'] or 'Departure time unknown'} → home {option['return_arrival'] or 'return time unknown'}")
        for leg in option.get("legs", []):
            print(f"   {leg['origin']} → {leg['destination']} | {leg['carrier']} {leg['cabin']} | {leg['paid_by']} pays")
        cost = f"{option['currency']} {option['total_cost']:,.0f}" if option["total_cost"] is not None else "Cost unknown"
        leave_label = f"{leave['days']} leave days" if leave["days"] is not None else "Leave unknown"
        hours = f"{option['usable_hours']:g} usable hours" if option["usable_hours"] is not None else "Usable time unknown"
        print(f"   {cost} | {leave_label}{' (provisional)' if leave['provisional'] else ''} | {hours}")
        print(f"   {option['rationale']}")
        if leave["leave_home_at"]:
            print(f"   Leave for airport: {leave['leave_home_at']}; ready for work: {leave['ready_for_work_at']}")
        print("   Overnight flights: " + {True: "yes", False: "no", None: "unknown"}[overnight(option)])
        for tradeoff in option["tradeoffs"]:
            print(f"   Trade-off: {tradeoff}")
        for blocker in assessment["blockers"]:
            print(f"   Needs checking: {blocker}")
        if evidence:
            for item in option["evidence"]:
                print(f"   {item['kind']} ({item['checked_at']}): {item['finding']}\n     {item['url']}")
    if not data["options"]:
        print("\nNo researched alternative matches these filters. Saved research remains available.")
        print("Clear filters or run research --refresh to expand the alternatives.")
    for task in data["research"]["remaining_work"]:
        print(f"Remaining: {task}")
    print("\nPrices require a fresh check before purchase. No bookings have been made.")


def main(argv=None):
    args = parser().parse_args(argv)
    # Local policy, prompts and event logs may contain personal information.
    os.umask(0o077)
    store = Store(args.data_dir)
    try:
        if args.command == "init":
            print(f"Local state: {store.root}\nLater deployment: https://fluctour.miltz.me")
            print("No server, app, email or notifications enabled. Preferences are editable with profile.")
            print("Home airport is unknown. Calendar and travel-time defaults are provisional.")
        elif args.command == "doctor":
            binary = shutil.which("codex")
            print(f"Python: {sys.version.split()[0]} | Codex: {binary or 'missing'} | State: {store.root}")
            if binary:
                result = subprocess.run([binary, "login", "status"], capture_output=True, text=True, timeout=15)
                print("Codex authentication: " + ("available" if result.returncode == 0 else "run codex login"))
            print("No research session started.")
        elif args.command == "profile":
            profile = store.profile()
            if args.key is not None:
                if args.value is None:
                    raise ValueError("Supply a JSON value: profile home_airport '\"HKG\"'")
                profile[args.key] = json.loads(args.value)
                store.save_profile(profile)
            print(json.dumps(profile, ensure_ascii=False, indent=2))
        elif args.command == "policy":
            if args.file.suffix.lower() not in (".txt", ".md", ".rst"):
                raise ValueError("Import UTF-8 .txt/.md/.rst passages; PDF extraction is not supported yet.")
            if args.file.stat().st_size > 64000:
                raise ValueError("Policy file is too large; use relevant travel passages.")
            profile = store.profile()
            profile["policy_text"] = args.file.read_text(encoding="utf-8")
            profile["policy_confirmed"] = False
            store.save_profile(profile)
            print("Policy saved locally. Confirm applicability with: profile policy_confirmed true")
        elif args.command == "new":
            print(store.create_trip(args.brief, args.token_budget, args.max_sessions))
        elif args.command == "list":
            for trip in store.trips():
                print(f"{trip['id']}  {trip['brief']}")
        elif args.command == "windows":
            from .windows import suggest_windows
            print(json.dumps(suggest_windows(store.profile(), start=args.start,
                horizon_days=args.days, min_nights=args.min_nights, max_nights=args.max_nights,
                max_leave=args.max_leave), indent=2))
        elif args.command == "triangle-check":
            from .policy import assess_triangle
            if args.file.stat().st_size > 120000:
                raise ValueError("Triangle quote file is too large.")
            print(json.dumps(assess_triangle(json.loads(args.file.read_text()), store.profile()), indent=2))
        elif args.command == "attach-public":
            from .service import attach_public_research
            attach_public_research(store, args.trip, args.public_trip, args.personal_leg)
            show(store, args.trip)
        elif args.command == "research":
            research(store, args.trip, args.steps, args.refresh, args.timeout)
            show(store, args.trip)
        elif args.command == "public-research":
            if bool(args.brief) == bool(args.trip):
                raise ValueError("Supply either a public-only brief or --trip ID.")
            if args.show and not args.trip:
                raise ValueError("--show requires an existing --trip ID.")
            public_store = Store(store.root / "public-research")
            try:
                trip_id = args.trip or public_store.create_trip(args.brief)
                if args.for_trip:
                    store.trip(args.for_trip)
                    public_store.set_budget_owner(trip_id, args.for_trip)
                print(f"Public research trip: {trip_id}")
                if not args.show:
                    research(public_store, trip_id, args.steps, args.refresh, args.timeout)
                if args.for_trip:
                    from .service import attach_public_research
                    attach_public_research(store, args.for_trip, trip_id)
                    show(store, args.for_trip)
                else:
                    show(public_store, trip_id)
            finally:
                public_store.db.close()
        elif args.command == "recover":
            from .research import recover
            recover(store, args.trip)
            show(store, args.trip)
        elif args.command == "preview":
            from .preview import write_preview
            trip_id = args.trip
            if not trip_id:
                trip_id = next((t["id"] for t in store.trips() if store.latest_result(t["id"])), None)
            if trip_id is None:
                raise ValueError("No saved proposals yet. Create research before generating a preview.")
            path = write_preview(store, trip_id)
            print(f"Open this file in your browser: {path}")
        elif args.command == "serve":
            from .server import serve
            store.db.close()
            serve(args.data_dir, args.port)
        elif args.command == "show":
            show(store, args.trip, args.json, args.evidence)
        elif args.command == "status":
            store.trip(args.trip)
            data = {"budget": budget_state(store, args.trip), "runs": [
                {k: v for k, v in r.items() if k != "result"} for r in store.runs(args.trip)]}
            print(json.dumps(data, indent=2))
        elif args.command == "feedback":
            constraints = {k: getattr(args, k) for k in ("max_cost", "max_leave", "no_overnight",
                           "departure_date") if getattr(args, k) is not None}
            if args.no_overnight and args.allow_overnight:
                raise ValueError("Choose either --no-overnight or --allow-overnight.")
            if args.allow_overnight:
                constraints["no_overnight"] = False
            if args.clear:
                constraints["clear"] = True
            for key in ("max_cost", "max_leave"):
                if key in constraints and (not math.isfinite(constraints[key]) or constraints[key] < 0):
                    raise ValueError(f"{key} must be a nonnegative finite number.")
            if args.departure_date:
                date.fromisoformat(args.departure_date)
            if not args.text and not constraints:
                raise ValueError("Supply feedback text or a filter.")
            store.add_feedback(args.trip, args.text, constraints)
            show(store, args.trip)
            if args.text:
                print("Feedback saved. Free-text interpretation happens on the next research run; filters apply now.")
        elif args.command == "limits":
            store.trip(args.trip)
            if args.token_budget <= 0 or not 1 <= args.max_sessions <= 30:
                raise ValueError("Use a positive token budget and 1–30 sessions.")
            with store.db:
                store.db.execute("UPDATE trips SET budget=?,max_sessions=? WHERE id=?",
                                 (args.token_budget, args.max_sessions, args.trip))
            print(json.dumps(budget_state(store, args.trip), indent=2))
        return 0
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        print(f"fluctour: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nResearch interrupted; prior checkpoints are saved.", file=sys.stderr)
        return 130
    finally:
        store.db.close()
