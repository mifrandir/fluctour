import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit

from fluctour import strava
from fluctour.server import Handler
from fluctour.store import Store
from test_server import FakeSocket


CLIENT_SECRET = "private-application-secret"
AUTH_CODE = "private-one-use-auth-code"
ACCESS_TOKEN = "private-access-token"
REFRESH_TOKEN = "private-refresh-token"


class StravaServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.db.close)

    def request(self, path="/api/strava", method="GET", payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        values = {"Host": "localhost:8765"}
        if method == "POST":
            values.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        values.update(headers or {})
        request = f"{method} {path} HTTP/1.1\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in values.items()) + "\r\n"
        connection = FakeSocket(request.encode("ascii") + body)
        server = SimpleNamespace(server_port=8765, data_root=Path(self.temp.name), preview_lock=Lock())
        Handler(connection, ("127.0.0.1", 12345), server)
        head, response = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), head, response

    def begin(self, host="localhost:8765"):
        code, _, response = self.request("/api/strava/configure", "POST", {
            "client_id": "12345", "client_secret": CLIENT_SECRET}, headers={"Host": host})
        self.assertEqual(code, 200)
        code, _, response = self.request("/api/strava/connect", "POST", {}, headers={"Host": host})
        self.assertEqual(code, 200)
        query = parse_qs(urlsplit(json.loads(response)["url"]).query)
        return query["state"][0], query["redirect_uri"][0]

    def callback(self, state, *, scope="activity:read_all", host="localhost:8765", code=AUTH_CODE):
        path = "/api/strava/callback?" + urlencode({"state": state, "code": code, "scope": scope})
        return self.request(path, headers={"Host": host, "Origin": "https://www.strava.com",
                                           "Sec-Fetch-Site": "cross-site"})

    def tokens(self):
        # Strava can omit scope from token JSON; the callback supplies the grant.
        return {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
                "expires_at": int(time.time()) + 21600, "athlete": {"id": 101}}

    def assert_no_secrets(self, data, *, include_code=True):
        rendered = data if isinstance(data, bytes) else json.dumps(data).encode()
        values = [CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN]
        if include_code:
            values.append(AUTH_CODE)
        for secret in values:
            self.assertNotIn(secret.encode(), rendered)
        self.assertNotIn(self.temp.name.encode(), rendered)

    def test_provider_callback_accepts_cross_site_navigation_and_exact_granted_scope(self):
        for host in ("localhost:8765", "127.0.0.1:8765"):
            with self.subTest(host=host):
                state, redirect = self.begin(host)
                with patch("fluctour.strava._request", return_value=self.tokens()), patch(
                        "fluctour.strava.exchange_callback", wraps=strava.exchange_callback) as exchange:
                    code, head, response = self.callback(state, host=host, scope="read,activity:read_all")
                self.assertEqual(code, 303)
                self.assertEqual(exchange.call_args.args[1:], (state, AUTH_CODE, redirect))
                self.assertEqual(exchange.call_args.kwargs, {"scope": "read,activity:read_all"})
                self.assertEqual(redirect, "http://" + host + "/api/strava/callback")
                self.assertIn(b"Location: /?strava=connected\r\n", head)
                self.assertIn(b"Referrer-Policy: no-referrer", head)
                self.assertIn(b"Cache-Control: no-store", head)
                self.assertEqual(response, b"")
                self.assertNotIn(state.encode(), head)
                self.assert_no_secrets(head)

    def test_callback_token_query_is_absent_from_redirect_body_and_access_logs(self):
        state, _ = self.begin()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch(
                "fluctour.strava._request", return_value=self.tokens()):
            code, head, response = self.callback(state)
        self.assertEqual(code, 303)
        combined = head + response + stdout.getvalue().encode() + stderr.getvalue().encode()
        self.assert_no_secrets(combined)
        self.assertNotIn(state.encode(), combined)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_cross_site_configuration_and_other_mutations_are_rejected(self):
        paths = ("/api/strava/configure", "/api/strava/connect", "/api/strava/sync",
                 "/api/strava/disconnect", "/api/strava/callback")
        payload = {"client_id": "12345", "client_secret": CLIENT_SECRET}
        with patch("fluctour.strava._request") as network:
            for path in paths:
                for headers in ({"Origin": "https://www.strava.com", "Sec-Fetch-Site": "cross-site"},
                                {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
                    with self.subTest(path=path, headers=headers):
                        code, _, response = self.request(path, "POST", payload, headers=headers)
                        self.assertEqual(code, 403)
                        self.assert_no_secrets(response)
            network.assert_not_called()
        self.assertFalse(strava.status(self.store)["configured"])

    def test_callback_exception_does_not_extend_to_similar_paths_or_ordinary_gets(self):
        for path in ("/api/strava", "/api/strava/callback/", "/api/strava/configure"):
            with self.subTest(path=path):
                code, _, _ = self.request(path, headers={"Sec-Fetch-Site": "cross-site"})
                self.assertEqual(code, 403)

    def test_invalid_or_replayed_state_fails_before_token_exchange(self):
        state, _ = self.begin()
        with patch("fluctour.strava._request") as network:
            code, head, response = self.callback("wrong-state")
            self.assertEqual(code, 400)
            self.assertNotIn(b"Location:", head)
            self.assert_no_secrets(response)
            self.assertIn("state", json.loads(response)["error"])
            network.assert_not_called()
        with patch("fluctour.strava._request", return_value=self.tokens()) as network:
            self.assertEqual(self.callback(state)[0], 303)
            self.assertEqual(self.callback(state)[0], 400)
            self.assertEqual(network.call_count, 1)

    def test_callback_evil_host_is_rejected_even_for_valid_state(self):
        state, _ = self.begin()
        with patch("fluctour.strava.exchange_callback") as exchange:
            for host in ("evil.example:8765", "localhost.evil.example:8765", "localhost:9999"):
                with self.subTest(host=host):
                    self.assertEqual(self.callback(state, host=host)[0], 403)
            exchange.assert_not_called()

    def test_callback_cannot_move_between_local_hosts_after_authorization(self):
        state, _ = self.begin("localhost:8765")
        with patch("fluctour.strava._request") as network:
            code, _, response = self.callback(state, host="127.0.0.1:8765")
            self.assertEqual(code, 400)
            self.assert_no_secrets(response)
            network.assert_not_called()

    def test_declined_or_missing_scope_does_not_contact_token_endpoint(self):
        for scope in ("", "read", "activity:read"):
            with self.subTest(scope=scope):
                state, _ = self.begin()
                with patch("fluctour.strava._request") as network:
                    code, _, response = self.callback(state, scope=scope)
                    self.assertEqual(code, 400)
                    self.assert_no_secrets(response)
                    network.assert_not_called()
        state, _ = self.begin()
        with patch("fluctour.strava.exchange_callback") as exchange:
            code, _, response = self.request("/api/strava/callback?" + urlencode({
                "state": state, "error": AUTH_CODE}), headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(code, 400)
            self.assert_no_secrets(response)
            exchange.assert_not_called()

    def test_configuration_and_connected_status_never_return_secret_material(self):
        code, head, response = self.request("/api/strava/configure", "POST", {
            "client_id": "12345", "client_secret": CLIENT_SECRET})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(response)["connection"]["configured"])
        self.assert_no_secrets(head + response)
        state, _ = self.begin()
        with patch("fluctour.strava._request", return_value=self.tokens()):
            self.assertEqual(self.callback(state)[0], 303)
        with patch("fluctour.strava._request") as network:
            code, head, response = self.request()
            network.assert_not_called()
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(response)["connection"]["connected"])
        self.assert_no_secrets(head + response)
        self.assertIn(b"Cache-Control: no-store", head)
        self.assertIn(b"Referrer-Policy: no-referrer", head)
        self.assertNotIn(b"Access-Control-Allow-Origin", head)

    def test_provider_failures_expose_safe_messages_and_retry_flags(self):
        state, _ = self.begin()
        opener = MagicMock()
        opener.open.side_effect = HTTPError("https://www.strava.com/oauth/token", 503,
                                           CLIENT_SECRET + AUTH_CODE, {}, None)
        with patch("fluctour.strava.build_opener", return_value=opener):
            code, head, response = self.callback(state)
        self.assertEqual(code, 400)
        self.assertIn("temporarily unavailable", json.loads(response)["error"])
        self.assert_no_secrets(head + response)
        with patch("fluctour.strava.authorization_url", side_effect=strava.StravaError(
                "A Strava operation is running; try again shortly.", "busy", True)):
            code, _, response = self.request("/api/strava/connect", "POST", {})
        self.assertEqual(code, 400)
        self.assert_no_secrets(response)
        result = json.loads(response)
        self.assertEqual(result["code"], "busy")
        self.assertTrue(result["retryable"])

    def test_unexpected_callback_failure_uses_generic_error_without_private_context(self):
        state, _ = self.begin()
        with patch("fluctour.strava.exchange_callback", side_effect=RuntimeError(
                CLIENT_SECRET + ACCESS_TOKEN + self.temp.name)):
            code, head, response = self.callback(state)
        self.assertEqual(code, 500)
        self.assertEqual(json.loads(response), {"error": "Unable to complete the local request."})
        self.assert_no_secrets(head + response)


if __name__ == "__main__":
    unittest.main()
