import io
import json
import tempfile
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from fluctour.server import Handler, dispatch, trusted_request
from fluctour.planning import policy_digest
from fluctour.store import Store, now
from test_fluctour import dossier, option


class FakeSocket:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, mode, *args):
        return self.request

    def sendall(self, data):
        self.output.extend(data)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)

    def request(self, path="/api/trips", method="GET", body=b"", headers=None):
        values = {"Host": "localhost:8765"}
        if method == "POST":
            values.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        values.update(headers or {})
        raw = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in values.items()) + "\r\n"
        connection = FakeSocket(raw.encode("ascii") + body)
        server = SimpleNamespace(server_port=8765, data_root=Path(self.temp.name), preview_lock=Lock())
        Handler(connection, ("127.0.0.1", 32123), server)
        head, response = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), head, response

    def test_create_persists_and_returns_private_safe_view_without_inference(self):
        status, result = dispatch(self.store, "POST", "/api/trips", {"brief": " Walk the coast "})
        self.assertEqual(status, 201)
        trip_id = result["trip"]["id"]
        self.assertEqual(result["trip"]["brief"], "Walk the coast")
        self.assertEqual(result["options"], [])
        self.assertFalse(result["researched"])
        reopened = Store(self.temp.name)
        try:
            self.assertEqual(reopened.trip(trip_id)["brief"], "Walk the coast")
            self.assertEqual(reopened.runs(trip_id), [])
        finally:
            reopened.db.close()
        status, listed = dispatch(self.store, "GET", "/api/trips")
        self.assertEqual(status, 200)
        self.assertEqual(listed["trips"][0]["id"], trip_id)

    def test_create_rejects_invalid_data_without_state_changes(self):
        for payload in (None, [], {}, {"brief": " "}, {"brief": 3}, {"brief": "a" * 6001},
                        {"brief": "Trip", "run_research": True}):
            with self.subTest(payload=repr(payload)[:70]):
                self.assertEqual(dispatch(self.store, "POST", "/api/trips", payload)[0], 400)
        self.assertEqual(self.store.trips(), [])

    def test_feedback_filters_persist_and_text_stays_pending(self):
        trip_id = self.store.create_trip("Coastal hiking")
        path = f"/api/trips/{trip_id}/feedback"
        status, data = dispatch(self.store, "POST", path, {"text": "Prefer quiet paths", "constraints": {
            "max_cost": 8000, "max_leave": 2, "no_overnight": True, "departure_date": "2027-03-19"}})
        self.assertEqual(status, 200)
        self.assertEqual(data["constraints"]["max_cost"], 8000)
        self.assertEqual(data["pending_feedback"], ["Prefer quiet paths"])
        self.assertEqual(len(self.store.feedback(trip_id)), 1)
        dispatch(self.store, "POST", path, {"constraints": {"clear": True}})
        self.assertEqual(dispatch(self.store, "GET", f"/api/trips/{trip_id}")[1]["constraints"], {})
        self.assertEqual(self.store.runs(trip_id), [])

    def test_invalid_feedback_has_no_partial_mutation(self):
        trip_id = self.store.create_trip("Trip")
        invalid = [{"text": 5}, {"text": "a" * 4001}, {"text": ""}, {"constraints": []},
                   {"constraints": {"max_cost": -1}}, {"constraints": {"max_leave": True}},
                   {"constraints": {"max_cost": float("inf")}},
                   {"constraints": {"max_cost": float("nan")}},
                   {"constraints": {"no_overnight": 1}}, {"constraints": {"clear": "true"}},
                   {"constraints": {"departure_date": "2027-02-30"}},
                   {"constraints": {"departure_date": "20270319"}},
                   {"constraints": {"departure_date": None}},
                   {"constraints": {"private_policy": "invented"}}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(dispatch(self.store, "POST", f"/api/trips/{trip_id}/feedback", payload)[0], 400)
        self.assertEqual(self.store.feedback(trip_id), [])

    def test_dossier_policy_and_profile_are_not_exported(self):
        trip_id = self.store.create_trip("Trip")
        profile = self.store.profile()
        profile.update(passport_nationality="SECRET_NATIONALITY", residency="SECRET_RESIDENCY",
                       policy_text="SECRET_POLICY")
        self.store.save_profile(profile)
        evidence = [{"kind": "policy", "scope": "trip", "source_type": "profile_policy",
                     "url": "", "finding": "SECRET_POLICY", "policy_excerpt": "SECRET_POLICY",
                     "checked_at": now(), "policy_digest": policy_digest(profile)},
                    {"kind": "destination", "scope": "trip", "source_type": "official",
                     "url": "https://example.org/hike", "finding": "Marked trail", "checked_at": now()}]
        run = self.store.begin_run(trip_id, "discovery")
        self.store.finish_run(run, "completed", {"input_tokens": 0, "output_tokens": 0},
                              result=dossier([option(evidence=evidence)]))
        status, data = dispatch(self.store, "GET", f"/api/trips/{trip_id}")
        serialized = json.dumps(data)
        self.assertEqual(status, 200)
        for marker in ("SECRET_NATIONALITY", "SECRET_RESIDENCY", "SECRET_POLICY", "policy_excerpt", "_profile"):
            self.assertNotIn(marker, serialized)
        self.assertEqual(data["options"][0]["evidence"][0]["finding"], "Marked trail")
        self.assertNotIn("research", data)

    def test_private_files_and_unknown_trip_are_not_served(self):
        for path in ("/state.sqlite", "/.fluctour/state.sqlite", "/../state.sqlite", "/api/profile",
                     "/api/trips/aaaaaaaaaaaa", "/api/trips/not-an-id", "/api/trips/aaaaaaaaaaaa/other"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_host_and_origin_prevent_cross_site_access(self):
        valid = {"Host": "localhost:8765", "Origin": "http://localhost:8765", "Sec-Fetch-Site": "same-origin"}
        self.assertTrue(trusted_request(valid, 8765))
        for headers in ({"Host": "evil.example:8765"}, {"Host": "localhost:80"},
                        {"Host": "localhost:8765", "Origin": "null"},
                        {"Host": "localhost:8765", "Origin": "https://localhost:8765"},
                        {"Host": "localhost:8765", "Origin": "http://evil.example"},
                        {"Host": "localhost:8765", "Sec-Fetch-Site": "cross-site"},
                        {"Host": "localhost:8765", "Sec-Fetch-Site": "same-site"}):
            with self.subTest(headers=headers):
                self.assertFalse(trusted_request(headers, 8765))
                self.assertEqual(self.request(headers=headers)[0], 403)

    def test_json_post_contract_and_size_limit(self):
        status, head, raw = self.request(method="POST", body=b'{"brief":"Hike and swim"}')
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["trip"]["brief"], "Hike and swim")
        self.assertIn(b"Cache-Control: no-store", head)
        self.assertNotIn(b"Access-Control-Allow-Origin", head)
        cases = [({"Content-Type": "text/plain"}, b'{}', 415),
                 ({}, b'{invalid', 400), ({}, b'\xff', 400),
                 ({"Content-Length": "16385"}, b'', 413),
                 ({"Transfer-Encoding": "chunked"}, b'{}', 400),
                 ({"Content-Length": "-1"}, b'', 400)]
        for headers, body, expected in cases:
            self.assertEqual(self.request(method="POST", body=body, headers=headers)[0], expected)
        self.assertEqual(len(self.store.trips()), 1)

    def test_errors_do_not_expose_sql_or_state_paths(self):
        status, _, raw = self.request("/api/trips/aaaaaaaaaaaa")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(raw), {"error": "Trip not found."})
        self.assertNotIn(self.temp.name.encode(), raw)


if __name__ == "__main__":
    unittest.main()
