import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from fluctour import strava
from fluctour.activities import activity_summary
from fluctour.store import Store


CALLBACK = "http://127.0.0.1:8765/api/strava/callback"
SECRET = "local-client-secret"


def tokens(**changes):
    return {"access_token": "access-token-private", "refresh_token": "refresh-token-private",
            "expires_at": int(time.time()) + 21600, "scope": "read,activity:read_all",
            "athlete": {"id": 101}, **changes}


def activity(number=1):
    return {"id": number, "sport_type": "Hike", "distance": 7000,
            "moving_time": 7200, "total_elevation_gain": 400,
            "start_date": "2026-10-01T01:00:00Z", "name": "A private hike",
            "start_latlng": [22.227, 114.239], "map": {"summary_polyline": "private-route"}}


class StravaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state")

    def tearDown(self):
        self.store.db.close()
        self.directory.cleanup()

    def configure(self):
        return strava.configure(self.store, "12345", SECRET)

    def state(self, callback=CALLBACK):
        url = strava.authorization_url(self.store, callback)
        return parse_qs(urlsplit(url).query)["state"][0]

    def connect(self):
        self.configure()
        state = self.state()
        with patch("fluctour.strava._request", return_value=tokens()):
            return strava.exchange_callback(self.store, state, "single-use-code", CALLBACK,
                                             scope="read,activity:read_all")

    def read_private(self):
        return json.loads((self.store.root / "strava.json").read_text())

    def write_private(self, data):
        (self.store.root / "strava.json").write_text(json.dumps(data))

    def test_status_is_offline_and_never_returns_credentials_or_tokens(self):
        with patch("fluctour.strava._request") as network:
            self.assertFalse(strava.status(self.store)["configured"])
            self.configure()
            network.assert_not_called()
        self.connect()
        response = strava.status(self.store)
        self.assertTrue(response["configured"])
        self.assertTrue(response["connected"])
        rendered = json.dumps(response)
        for secret in (SECRET, "access-token-private", "refresh-token-private", "client_secret"):
            self.assertNotIn(secret, rendered)
        self.assertNotIn(SECRET, json.dumps(self.store.profile()))

    def test_credentials_and_state_permissions_are_private(self):
        self.connect()
        self.assertEqual(os.stat(self.store.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.store.root / "strava.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.store.root / "strava.lock").st_mode & 0o777, 0o600)

    def test_status_remains_readable_while_a_mutating_operation_holds_the_lock(self):
        self.connect()
        with strava._locked(self.store):
            snapshot = strava.status(self.store)
            self.assertTrue(snapshot["connected"])
            with self.assertRaises(strava.StravaError) as caught:
                strava.authorization_url(self.store, CALLBACK)
            self.assertEqual(caught.exception.code, "busy")

    def test_authorization_requests_only_read_scope_and_random_expiring_state(self):
        self.configure()
        first = strava.authorization_url(self.store, CALLBACK)
        params = parse_qs(urlsplit(first).query)
        self.assertEqual(urlsplit(first).netloc, "www.strava.com")
        self.assertEqual(params["scope"], ["activity:read_all"])
        self.assertEqual(params["redirect_uri"], [CALLBACK])
        self.assertGreaterEqual(len(params["state"][0]), 40)
        pending = self.read_private()["pending_oauth"]
        self.assertNotEqual(pending["state_digest"], params["state"][0])
        self.assertGreater(pending["expires_at"], time.time() + 590)
        self.assertNotEqual(params["state"][0], self.state())
        self.assertNotIn(SECRET, first)

    def test_invalid_credentials_are_rejected(self):
        for client_id, secret in ((True, SECRET), ("-1", SECRET), ("a", SECRET),
                                  ("123", ""), ("123", "unsafe\nsecret")):
            with self.subTest(client_id=client_id), self.assertRaises(strava.StravaError):
                strava.configure(self.store, client_id, secret)

    def test_only_exact_loopback_callback_urls_are_accepted(self):
        self.configure()
        for callback in ("https://evil.example/api/strava/callback", "http://127.0.0.2:8765/api/strava/callback",
                         "http://127.0.0.1.evil.example:8765/api/strava/callback", "http://localhost/api/strava/callback",
                         CALLBACK + "?redirect=evil", CALLBACK + "#fragment", None, "http://user@localhost:8765/api/strava/callback"):
            with self.subTest(callback=callback), self.assertRaises(strava.StravaError):
                self.state(callback)
        state = self.state()
        with patch("fluctour.strava._request") as network:
            with self.assertRaises(strava.StravaError):
                strava.exchange_callback(self.store, state, "code", CALLBACK.replace("8765", "8766"))
            network.assert_not_called()

    def test_state_is_expiring_one_use_and_csrf_checked_before_network(self):
        self.configure()
        state = self.state()
        with patch("fluctour.strava._request", return_value=tokens()) as network:
            with self.assertRaises(strava.StravaError):
                strava.exchange_callback(self.store, "wrong-state", "code", CALLBACK)
            network.assert_not_called()
            strava.exchange_callback(self.store, state, "code", CALLBACK)
            with self.assertRaises(strava.StravaError):
                strava.exchange_callback(self.store, state, "code", CALLBACK)
            self.assertEqual(network.call_count, 1)
        state = self.state()
        data = self.read_private()
        data["pending_oauth"]["expires_at"] = time.time() - 1
        self.write_private(data)
        with patch("fluctour.strava._request") as network, self.assertRaises(strava.StravaError):
            strava.exchange_callback(self.store, state, "code", CALLBACK)
        network.assert_not_called()

    def test_declined_scope_or_missing_code_does_not_connect(self):
        self.configure()
        for code, scope in (("code", "read,activity:read"), (None, "activity:read_all")):
            state = self.state()
            with patch("fluctour.strava._request") as network, self.assertRaises(strava.StravaError):
                strava.exchange_callback(self.store, state, code, CALLBACK, scope=scope)
            network.assert_not_called()
            self.assertNotIn("pending_oauth", self.read_private())
        state = self.state()
        with patch("fluctour.strava._request", return_value=tokens(scope="read")):
            with self.assertRaises(strava.StravaError):
                strava.exchange_callback(self.store, state, "code", CALLBACK)
        self.assertFalse(strava.status(self.store)["connected"])

    def test_callback_scope_can_supply_grant_when_token_response_omits_it(self):
        self.configure()
        state = self.state()
        response = tokens()
        del response["scope"]
        with patch("fluctour.strava._request", return_value=response):
            self.assertTrue(strava.exchange_callback(self.store, state, "code", CALLBACK,
                                                      scope="activity:read_all")["connected"])

    def test_cross_account_reconnect_cannot_silently_merge_activity_histories(self):
        self.connect()
        with patch("fluctour.strava._request", return_value=[activity()]):
            strava.sync_activities(self.store)
        strava.disconnect(self.store, clear_credentials=True)
        self.configure()
        state = self.state()
        with patch("fluctour.strava._request", return_value=tokens(athlete={"id": 202})):
            with self.assertRaises(strava.StravaError) as caught:
                strava.exchange_callback(self.store, state, "other-account-code", CALLBACK,
                                         scope="activity:read_all")
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertFalse(strava.status(self.store)["connected"])
        self.assertEqual(activity_summary(self.store)["count"], 1)
        self.assertEqual(self.store.db.execute("SELECT value FROM settings WHERE key=?",
                                              (strava.ATHLETE_BINDING_KEY,)).fetchone()[0], "101")
        state = self.state()
        with patch("fluctour.strava._request", return_value=tokens()):
            self.assertTrue(strava.exchange_callback(self.store, state, "original-account-code", CALLBACK,
                                                     scope="activity:read_all")["connected"])

    def test_expired_access_refreshes_and_persists_rotated_refresh_token(self):
        self.connect()
        data = self.read_private()
        data["expires_at"] = 0
        self.write_private(data)
        response = tokens(access_token="new-access", refresh_token="new-refresh")
        with patch("fluctour.strava._request", side_effect=[response, [activity()]]) as network:
            result = strava.sync_activities(self.store)
        self.assertEqual(network.call_args_list[0].kwargs["form"]["grant_type"], "refresh_token")
        self.assertEqual(network.call_args_list[1].kwargs["access_token"], "new-access")
        self.assertEqual(self.read_private()["refresh_token"], "new-refresh")
        self.assertEqual(result["added_count"], 1)
        self.assertEqual(activity_summary(self.store)["count"], 1)
        self.assertNotIn("new-refresh", json.dumps(result))

    def test_rejected_refresh_requires_reconnect_and_exposes_no_provider_body(self):
        self.connect()
        data = self.read_private()
        data["expires_at"] = 0
        self.write_private(data)
        with patch("fluctour.strava._request", side_effect=strava.StravaError(
                "Strava access was rejected; reconnect.", "authorization_rejected")):
            with self.assertRaises(strava.StravaError):
                strava.sync_activities(self.store)
        self.assertTrue(strava.status(self.store)["needs_reconnect"])
        self.assertFalse(strava.status(self.store)["connected"])

    def test_sync_paginates_and_merges_idempotently_without_route_payloads(self):
        self.connect()
        batch = [activity(i) for i in range(1, 201)]
        with patch("fluctour.strava._request", side_effect=[batch, [activity(201)]]) as network:
            result = strava.sync_activities(self.store)
        self.assertEqual(result["fetched_count"], 201)
        self.assertEqual(result["added_count"], 201)
        self.assertFalse(result["partial"])
        self.assertIn("page=2", network.call_args_list[1].args[0])
        with patch("fluctour.strava._request", side_effect=[batch, [activity(201)]]):
            second = strava.sync_activities(self.store)
        self.assertEqual(second["duplicate_count"], 201)
        self.assertEqual(activity_summary(self.store)["count"], 201)
        saved = self.store.db.execute("SELECT value FROM settings WHERE key='activity_history'").fetchone()[0]
        self.assertNotIn("private-route", saved)
        self.assertNotIn("access-token-private", saved)

    def test_sync_cap_and_midstream_failure_are_explicit_partial_results(self):
        self.connect()
        with patch("fluctour.strava._request", side_effect=[
                [activity(i + page * 200) for i in range(1, 201)] for page in range(5)]) as network:
            result = strava.sync_activities(self.store)
        self.assertEqual(network.call_count, 5)
        self.assertEqual(result["fetched_count"], 1000)
        self.assertTrue(result["partial"])
        with patch("fluctour.strava._request", side_effect=[
                [activity(i) for i in range(1, 201)],
                strava.StravaError("Strava rate limit reached.", "rate_limited", True)]):
            result = strava.sync_activities(self.store)
        self.assertTrue(result["partial"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error_code"], "rate_limited")
        self.assertEqual(result["fetched_count"], 200)

    def test_one_unauthorized_get_refreshes_once_and_retries_same_page(self):
        self.connect()
        with patch("fluctour.strava._request", side_effect=[
                strava.StravaError("Reconnect.", "authorization_rejected"),
                tokens(access_token="refreshed-access"), [activity()]]) as network:
            result = strava.sync_activities(self.store)
        self.assertEqual(network.call_count, 3)
        self.assertEqual(network.call_args_list[0].args[0], network.call_args_list[2].args[0])
        self.assertEqual(result["fetched_count"], 1)

    def test_disconnect_is_local_unless_revocation_is_explicit(self):
        self.connect()
        with patch("fluctour.strava._request") as network:
            result = strava.disconnect(self.store)
            network.assert_not_called()
        self.assertTrue(result["configured"])
        self.assertFalse(result["connected"])
        self.connect()
        with patch("fluctour.strava._request", return_value={}) as network:
            result = strava.disconnect(self.store, revoke=True, clear_credentials=True)
        self.assertEqual(network.call_args.args[0], "https://www.strava.com/oauth/revoke")
        self.assertFalse(result["configured"])
        self.assertEqual(self.read_private(), {})

    def test_http_timeout_request_headers_and_error_redaction(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'[]'
        with patch("fluctour.strava.build_opener", return_value=opener):
            result = strava._request("https://www.strava.com/api/v3/athlete/activities", access_token="secret-token")
        self.assertEqual(result, [])
        req = opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], strava.TIMEOUT)
        for error, code, retryable in (
                (HTTPError("https://www.strava.com", 429, SECRET, {}, None), "rate_limited", True),
                (HTTPError("https://www.strava.com", 401, SECRET, {}, None), "authorization_rejected", False),
                (HTTPError("https://www.strava.com", 503, SECRET, {}, None), "provider_unavailable", True),
                (URLError(SECRET), "network_error", True)):
            opener.open.side_effect = error
            with patch("fluctour.strava.build_opener", return_value=opener), self.assertRaises(strava.StravaError) as caught:
                strava._request("https://www.strava.com/oauth/token", form={"client_secret": SECRET})
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_provider_redirects_are_not_followed_with_credentials(self):
        self.assertIsNone(strava._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_state_file_symlink_is_rejected_without_reading_target(self):
        target = Path(self.directory.name) / "other.json"
        target.write_text(json.dumps({"client_id": "123", "client_secret": SECRET}))
        (self.store.root / "strava.json").symlink_to(target)
        with self.assertRaises(strava.StravaError):
            strava.status(self.store)


if __name__ == "__main__":
    unittest.main()
