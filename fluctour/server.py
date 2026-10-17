"""Loopback-only application server for persisted local trip planning."""

import json
import math
import re
import signal
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, current_thread, main_thread
from urllib.parse import parse_qs, urlsplit

from .cli import view
from .store import Store, now
from .jobs import enqueue, job_state, validate_request, Runner


MAX_BODY = 16384
TRIP_ID = re.compile(r"[a-f0-9]{12}\Z")


def public_view(store, trip_id):
    """Expose travel decisions without the profile, raw dossier or policy evidence."""
    data = view(store, trip_id)
    plans = store.plans(trip_id)
    options = []
    for item in data["options"]:
        option = {key: item[key] for key in (
            "id", "title", "destination", "rationale", "departure", "return_arrival",
            "legs", "total_cost", "cost_complete", "currency", "usable_hours",
            "tradeoffs", "assessment") if key in item}
        evidence = []
        for source in item.get("evidence", []):
            if source.get("source_type") == "profile_policy" or source.get("kind") == "policy":
                continue
            url = urlsplit(source.get("url", ""))
            if url.scheme == "https" and url.hostname and not url.username and not url.password:
                evidence.append({key: source[key] for key in (
                    "kind", "scope", "url", "checked_at", "finding") if key in source})
        option["evidence"] = evidence
        option["plan"] = plans.get(item["id"])
        options.append(option)
    return {"trip": {key: data["trip"][key] for key in ("id", "brief", "created_at")},
            "researched": bool(data["research"]), "options": options,
            "constraints": data["constraints"], "pending_feedback": data["pending_feedback"],
            "feedback_effects": data["feedback_effects"],
            "budget": data["budget"], "planning_context": data["planning_context"],
            "job": job_state(store, trip_id)}


def validate_feedback(payload):
    if not isinstance(payload, dict) or set(payload) - {"text", "constraints"}:
        raise ValueError("Use text and constraints fields only.")
    text = payload.get("text", "")
    constraints = payload.get("constraints", {})
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("Feedback must be text of at most 4000 characters.")
    allowed = {"max_cost", "max_leave", "no_overnight", "departure_date", "clear"}
    if not isinstance(constraints, dict) or set(constraints) - allowed:
        raise ValueError("Unknown feedback constraint.")
    for key, value in constraints.items():
        if key in {"max_cost", "max_leave"}:
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("Cost and leave limits must be finite nonnegative numbers.")
        elif key in {"no_overnight", "clear"}:
            if type(value) is not bool:
                raise ValueError("Overnight and clear constraints must be booleans.")
        elif not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Departure date must use YYYY-MM-DD.")
        else:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError("Departure date must be a valid calendar date.") from None
    if not text.strip() and not constraints:
        raise ValueError("Provide feedback or a constraint.")
    return text, constraints


def dispatch(store, method, path, payload=None, *, origin="http://localhost:8765"):
    """Handle API operations without sockets or model inference."""
    if path.startswith("/api/strava"):
        from . import strava
        from .activities import activity_summary
        try:
            if path == "/api/strava" and method == "GET":
                return 200, {"connection": strava.status(store), "history": activity_summary(store)}
            if method == "POST" and path == "/api/strava/configure":
                if not isinstance(payload, dict) or set(payload) != {"client_id", "client_secret"}:
                    return 400, {"error": "Enter your Strava application client ID and secret."}
                return 200, {"connection": strava.configure(store, payload["client_id"], payload["client_secret"])}
            if method == "POST" and path == "/api/strava/connect":
                return 200, {"url": strava.authorization_url(store, origin + "/api/strava/callback")}
            if method == "POST" and path == "/api/strava/sync":
                return 200, {"connection": strava.sync_activities(store), "history": activity_summary(store)}
            if method == "POST" and path == "/api/strava/disconnect":
                return 200, {"connection": strava.disconnect(store)}
            return 404, {"error": "Not found."}
        except strava.StravaError as error:
            return 400, {"error": str(error), "code": error.code, "retryable": error.retryable}
    if path == "/api/trips":
        if method == "GET":
            return 200, {"trips": [{key: trip[key] for key in ("id", "brief", "created_at")} |
                                   {"researched": bool(store.latest_result(trip["id"]))}
                                   for trip in store.trips()]}
        if method == "POST":
            if (not isinstance(payload, dict) or not {"brief"} <= set(payload) <= {"brief", "research"}
                    or not isinstance(payload["brief"], str)):
                return 400, {"error": "Provide a travel brief as text."}
            try:
                request = validate_request(payload["research"]) if "research" in payload else None
            except ValueError as error:
                return 400, {"error": str(error)}
            try:
                trip_id = store.create_trip(payload["brief"].strip())
            except ValueError:
                return 400, {"error": "Brief must contain 1–6000 characters."}
            if request is not None:
                try:
                    enqueue(store, trip_id, request)
                except Exception as error:
                    # A rejected enqueue must not leave an unreturned draft behind.
                    with store.db:
                        store.db.execute("DELETE FROM trips WHERE id=?", (trip_id,))
                    if isinstance(error, ValueError):
                        return 400, {"error": str(error)}
                    raise
            return 201, public_view(store, trip_id)
        return 405, {"error": "Method not allowed."}
    parts = path.split("/")
    if (len(parts) not in (4, 5) or parts[:3] != ["", "api", "trips"]
            or not TRIP_ID.fullmatch(parts[3]) or (len(parts) == 5 and parts[4] not in {"feedback", "research", "harness", "cancel"})):
        return 404, {"error": "Not found."}
    trip_id = parts[3]
    try:
        store.trip(trip_id)
    except ValueError:
        return 404, {"error": "Trip not found."}
    if len(parts) == 4 and method == "GET":
        return 200, public_view(store, trip_id)
    if len(parts) == 5 and parts[4] == "harness" and method == "GET":
        from .harness import inspect_harness
        return 200, inspect_harness(store, trip_id)
    if len(parts) == 5 and parts[4] == "cancel" and method == "POST":
        if not isinstance(payload, dict) or payload:
            return 400, {"error": "Cancellation does not accept additional fields."}
        from .jobs import request_cancel
        try:
            request_cancel(store, trip_id)
        except ValueError as error:
            return 400, {"error": str(error)}
        return 202, public_view(store, trip_id)
    if len(parts) == 5 and parts[4] == "research" and method == "POST":
        try:
            if not isinstance(payload, dict) or set(payload) - {"research", "brief", "refresh"}:
                raise ValueError("Use research, refresh and an optional private brief only.")
            if "refresh" in payload and type(payload["refresh"]) is not bool:
                raise ValueError("Refresh must be a boolean.")
            if "brief" in payload and (not isinstance(payload["brief"], str)
                    or not 1 <= len(payload["brief"].strip()) <= 6000):
                raise ValueError("Brief must contain 1–6000 characters.")
            request = payload.get("research")
            if request is None:
                request = (job_state(store, trip_id) or {}).get("request")
            if request is None:
                raise ValueError("Choose a destination, dates and activities to start planning.")
            if "refresh" in payload:
                enqueue(store, trip_id, request, refresh=payload["refresh"])
            else:
                enqueue(store, trip_id, request)
            if "brief" in payload:
                with store.db:
                    store.db.execute("UPDATE trips SET brief=? WHERE id=?", (payload["brief"].strip(), trip_id))
            return 202, public_view(store, trip_id)
        except ValueError as error:
            return 400, {"error": str(error)}
    if len(parts) == 5 and parts[4] == "feedback" and method == "POST":
        try:
            text, constraints = validate_feedback(payload)
            from .preferences import derive_public_request_update, interpret_feedback
            feedback_currency = store.profile()["currency"]
            interpreted = interpret_feedback(text, feedback_currency)
        except ValueError as error:
            return 400, {"error": str(error)}
        with store.db:
            feedback = store.db.execute("INSERT INTO feedback(trip_id,created_at,text,constraints) VALUES(?,?,?,?)",
                (trip_id, now(), text, json.dumps(interpreted["constraints"] | constraints)))
            store.db.execute("INSERT INTO settings VALUES(?,?)", ("feedback_context:" + str(feedback.lastrowid), json.dumps({
                "currency": feedback_currency, "derived_constraints": interpreted["constraints"],
                "explicit_constraints": constraints})))
        queued = False
        queue_error = None
        state = job_state(store, trip_id)
        if state and state.get("request"):
            updated = derive_public_request_update(state["request"], interpreted)
            if updated["changed"]:
                if state["status"] in {"queued", "researching", "cancelling"}:
                    queue_error = "Activity changes are saved locally; revise the request after the current research stops."
                else:
                    try:
                        state = enqueue(store, trip_id, updated["request"])
                        queued = state["status"] == "queued"
                    except ValueError:
                        queue_error = "Activity changes are saved locally; research could not be queued yet."
        data = public_view(store, trip_id)
        data["feedback_effects"].update(research_queued=queued, research_error=queue_error)
        return 200, data
    return 405, {"error": "Method not allowed."}


def trusted_request(headers, port, *, callback_navigation=False):
    """Host and browser-origin checks prevent websites accessing the local service."""
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    if port == 80:
        hosts |= {"localhost", "127.0.0.1"}
    host = headers.get("Host", "").lower()
    if host not in hosts:
        return False
    if callback_navigation:
        return True
    if headers.get("Sec-Fetch-Site", "") not in ("", "none", "same-origin"):
        return False
    origin = headers.get("Origin")
    return origin is None or origin.lower() == "http://" + host


class Handler(BaseHTTPRequestHandler):
    server_version = "Fluctour"
    sys_version = ""

    def log_message(self, format, *args):
        # Briefs and request URLs can contain private travel information.
        pass

    def reply(self, status, data, content_type="application/json; charset=utf-8"):
        if content_type.startswith("application/json"):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def process(self):
        self.close_connection = True
        callback = self.command == "GET" and urlsplit(self.path).path == "/api/strava/callback"
        if not trusted_request(self.headers, self.server.server_port, callback_navigation=callback):
            self.reply(403, {"error": "This service accepts same-origin local requests only."})
            return
        payload = None
        if self.command == "POST":
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.reply(415, {"error": "Use application/json."})
                return
            size = self.headers.get("Content-Length", "")
            if self.headers.get("Transfer-Encoding") or not size.isascii() or not size.isdigit():
                self.reply(400, {"error": "A valid Content-Length is required."})
                return
            if len(size) > 5 or int(size) > MAX_BODY:
                self.reply(413, {"error": "Request exceeds 16384 bytes."})
                return
            try:
                raw = self.rfile.read(int(size))
                if len(raw) != int(size):
                    raise ValueError("Incomplete body")
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {"error": "Invalid JSON body."})
                return
        store = None
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc:
                self.reply(400, {"error": "Use a local request path."})
                return
            store = Store(self.server.data_root)
            if callback:
                from . import strava
                query = parse_qs(target.query)
                try:
                    if "error" in query:
                        raise strava.StravaError("Strava access was declined. You can reconnect when ready.")
                    strava.exchange_callback(store, query.get("state", [""])[0],
                        query.get("code", [""])[0], "http://" + self.headers["Host"] + "/api/strava/callback",
                        scope=query.get("scope", [""])[0])
                except strava.StravaError as error:
                    self.reply(400, {"error": str(error)})
                    return
                self.send_response(303)
                self.send_header("Location", "/?strava=connected")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if target.path == "/" and self.command == "GET":
                from .preview import write_preview
                selected = parse_qs(target.query).get("trip", [None])[0]
                trips = store.trips()
                if selected is not None and not any(trip["id"] == selected for trip in trips):
                    self.reply(404, {"error": "Trip not found."})
                    return
                if selected is None:
                    selected = next((trip["id"] for trip in trips if store.latest_result(trip["id"])),
                                    trips[0]["id"] if trips else None)
                with self.server.preview_lock:
                    html = write_preview(store, selected).read_bytes()
                self.reply(200, html, "text/html; charset=utf-8")
                return
            status, data = dispatch(store, self.command, target.path, payload, origin="http://" + self.headers["Host"])
            if self.command == "POST" and status < 300:
                runner = getattr(self.server, "job_runner", None)
                if runner is not None:
                    if target.path.endswith("/cancel"):
                        runner.cancel(target.path.split("/")[3])
                    else:
                        runner.wake()
            self.reply(status, data)
        except Exception:
            self.reply(500, {"error": "Unable to complete the local request."})
        finally:
            if store is not None:
                store.db.close()

    do_GET = process
    do_POST = process
    do_PUT = process
    do_DELETE = process
    do_PATCH = process


def serve(root, port=8765):
    """Serve the local application until interrupted; state stays in the given root."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.data_root = Path(root).resolve()
        server.preview_lock = Lock()
        server.job_runner = Runner(server.data_root)
        previous_term = None
        if current_thread() is main_thread():
            previous_term = signal.getsignal(signal.SIGTERM)
            def stop_server(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_server)
        try:
            server.job_runner.wake()
            print(f"Fluctour: http://localhost:{port}/", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                server.job_runner.close()
            finally:
                if previous_term is not None:
                    signal.signal(signal.SIGTERM, previous_term)
