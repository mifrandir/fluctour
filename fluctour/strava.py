"""Read-only Strava OAuth and bounded local activity imports.

Register an application at https://www.strava.com/settings/api before configuring
its client ID and secret. Credentials, tokens and one-use OAuth state remain in
the private state directory, separate from profiles and external research.

Official contracts:
https://developers.strava.com/docs/authentication/
https://developers.strava.com/docs/reference/#api-Activities-getLoggedInAthleteActivities

The browser server supplies its exact loopback callback URL and the callback's
granted ``scope`` to exchange_callback. Public functions return safe status data
or an authorization URL. StravaError carries a safe message, code and retryable
flag; upstream response bodies and credentials are never exposed.
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


REGISTRATION_URL = "https://www.strava.com/settings/api"
SCOPE = "activity:read_all"
TIMEOUT = 20
PER_PAGE = 200
MAX_PAGES = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ATHLETE_BINDING_KEY = "strava_history_athlete_id"


class StravaError(ValueError):
    def __init__(self, message, code="strava_error", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _root(store):
    root = Path(store.root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


@contextmanager
def _locked(store):
    root = _root(store)
    try:
        fd = os.open(root / "strava.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StravaError("A Strava operation is running; try again shortly.", "busy", True) from None
            try:
                yield root
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        raise StravaError("Cannot access private Strava storage.", "storage_error") from None


def _load(root):
    try:
        fd = os.open(root / "strava.json", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(fd, "r") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise StravaError("Private Strava storage is invalid.", "storage_error")
            os.fchmod(handle.fileno(), 0o600)
            value = json.loads(handle.read(65537))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise StravaError("Private Strava storage is invalid; configure it again.", "storage_error") from None


def _save(root, data):
    fd, temporary = tempfile.mkstemp(prefix=".strava-", dir=root)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / "strava.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _status(data):
    return {
        "configured": bool(data.get("client_id") and data.get("client_secret")),
        "connected": bool(data.get("access_token") and data.get("refresh_token")
                          and not data.get("needs_reconnect")),
        "needs_reconnect": bool(data.get("needs_reconnect", False)),
        "last_sync_at": data.get("last_sync_at"),
        "synced_count": data.get("synced_count", 0),
        "partial": data.get("partial", False),
        "registration_url": REGISTRATION_URL,
    }


def status(store):
    """Read local connection status without contacting Strava or exposing secrets."""
    # Atomic replacement lets readers observe a complete snapshot during a sync.
    try:
        return _status(_load(_root(store)))
    except OSError:
        raise StravaError("Cannot access private Strava storage.", "storage_error") from None


def configure(store, client_id, client_secret):
    """Save registered application credentials locally and invalidate old tokens."""
    if (type(client_id) not in (str, int) or not str(client_id).isascii()
            or not str(client_id).isdigit() or not 1 <= len(str(client_id)) <= 20
            or int(client_id) <= 0):
        raise StravaError("Enter the numeric client ID from your registered Strava application.", "invalid_client")
    if (not isinstance(client_secret, str) or not 8 <= len(client_secret.strip()) <= 256
            or any(ord(char) < 33 or ord(char) > 126 for char in client_secret.strip())):
        raise StravaError("Enter the client secret from your registered Strava application.", "invalid_client")
    with _locked(store) as root:
        data = {"client_id": str(client_id), "client_secret": client_secret.strip()}
        _save(root, data)
        return _status(data)


def _redirect(value):
    if not isinstance(value, str):
        raise StravaError("Strava requires this server's exact loopback callback URL.", "invalid_redirect")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http"
                 and parsed.hostname in ("127.0.0.1", "localhost")
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and not parsed.username and not parsed.password
                 and parsed.path == "/api/strava/callback" and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise StravaError("Strava requires this server's exact loopback callback URL.", "invalid_redirect")
    return value


def authorization_url(store, redirect_uri):
    """Create a ten-minute, single-use OAuth transaction for this server callback."""
    redirect_uri = _redirect(redirect_uri)
    with _locked(store) as root:
        data = _load(root)
        if not _status(data)["configured"]:
            raise StravaError("Configure your registered Strava application first.", "not_configured")
        state = secrets.token_urlsafe(32)
        data["pending_oauth"] = {"state_digest": hashlib.sha256(state.encode()).hexdigest(),
                                 "expires_at": time.time() + 600, "redirect_uri": redirect_uri}
        _save(root, data)
        return "https://www.strava.com/oauth/authorize?" + urlencode({
            "client_id": data["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "approval_prompt": "force", "scope": SCOPE, "state": state})


def _request(url, *, form=None, access_token=None, basic=None, empty_ok=False):
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    request = Request(url, data=body, headers=headers)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise StravaError("Strava returned too much data; try a smaller import.", "response_too_large")
        if empty_ok and not payload:
            return {}
        return json.loads(payload)
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 429:
            raise StravaError("Strava rate limit reached; try again later.", "rate_limited", True) from None
        if code in (401, 403):
            raise StravaError("Strava access was rejected; reconnect your account.", "authorization_rejected") from None
        if 500 <= code <= 599:
            raise StravaError("Strava is temporarily unavailable; try again later.", "provider_unavailable", True) from None
        raise StravaError("Strava rejected the request; check the application settings and reconnect.", "provider_rejected") from None
    except (URLError, TimeoutError, OSError):
        raise StravaError("Cannot reach Strava; try again later.", "network_error", True) from None
    except StravaError:
        raise
    except (ValueError, UnicodeError):
        raise StravaError("Strava returned an invalid response; try again later.", "invalid_response", True) from None


def _tokens(data, response):
    if not isinstance(response, dict):
        raise StravaError("Strava returned invalid authorization data; reconnect.", "invalid_tokens")
    for key in ("access_token", "refresh_token"):
        if (not isinstance(response.get(key), str) or not 1 <= len(response[key]) <= 1024
                or any(ord(char) < 33 or ord(char) > 126 for char in response[key])):
            raise StravaError("Strava returned invalid authorization data; reconnect.", "invalid_tokens")
    expiry = response.get("expires_at")
    if type(expiry) is not int or expiry <= time.time():
        raise StravaError("Strava returned an expired authorization; reconnect.", "invalid_tokens")
    for key in ("access_token", "refresh_token", "expires_at"):
        data[key] = response[key]
    data["needs_reconnect"] = False


def _scopes(value):
    return set(value.replace(",", " ").split()) if isinstance(value, str) else set()


def _bind_athlete(store, data, response):
    athlete = response.get("athlete")
    athlete_id = athlete.get("id") if isinstance(athlete, dict) else None
    if type(athlete_id) is not int or athlete_id <= 0:
        raise StravaError("Strava did not identify the authorized athlete; reconnect.", "invalid_athlete")
    athlete_id = str(athlete_id)
    with store.db:
        existing = store.db.execute("SELECT value FROM settings WHERE key=?", (ATHLETE_BINDING_KEY,)).fetchone()
        if existing is not None and existing[0] != athlete_id:
            raise StravaError("This local history is linked to another Strava athlete. Reconnect that account or use a separate local history.",
                              "account_mismatch")
        store.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (ATHLETE_BINDING_KEY, athlete_id))
    data["athlete_id"] = athlete_id


def exchange_callback(store, state, code, redirect_uri, *, scope=None):
    """Consume validated OAuth state before exchanging a one-use code."""
    redirect_uri = _redirect(redirect_uri)
    with _locked(store) as root:
        data = _load(root)
        pending = data.get("pending_oauth", {})
        digest = hashlib.sha256(state.encode()).hexdigest() if isinstance(state, str) else ""
        if (not pending or not hmac.compare_digest(digest, pending.get("state_digest", ""))
                or pending.get("expires_at", 0) <= time.time()):
            raise StravaError("Strava authorization expired or its state is invalid; connect again.", "invalid_state")
        del data["pending_oauth"]
        _save(root, data)
        if pending.get("redirect_uri") != redirect_uri:
            raise StravaError("Strava callback does not match the server that began authorization.", "invalid_redirect")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048:
            raise StravaError("Strava authorization was declined or its code is missing; connect again.", "missing_code")
        if scope is not None and SCOPE not in _scopes(scope):
            raise StravaError("Allow read access to all activities on Strava to use this connection.", "missing_scope")
        response = _request("https://www.strava.com/oauth/token", form={
            "client_id": data["client_id"], "client_secret": data["client_secret"],
            "code": code, "grant_type": "authorization_code"})
        granted = _scopes(response.get("scope")) if isinstance(response, dict) else set()
        if scope is None and SCOPE not in granted:
            raise StravaError("Strava did not confirm the required activity permission; connect again.", "missing_scope")
        if granted and SCOPE not in granted:
            raise StravaError("Strava did not grant the required activity permission; connect again.", "missing_scope")
        _tokens(data, response)
        _bind_athlete(store, data, response)
        data["scope"] = SCOPE
        _save(root, data)
        return _status(data)


def _refresh(root, data):
    try:
        response = _request("https://www.strava.com/oauth/token", form={
            "client_id": data["client_id"], "client_secret": data["client_secret"],
            "refresh_token": data["refresh_token"], "grant_type": "refresh_token"})
        _tokens(data, response)
        _save(root, data)
    except StravaError as error:
        if error.code in ("authorization_rejected", "provider_rejected", "invalid_tokens"):
            data["needs_reconnect"] = True
            _save(root, data)
        raise


def sync_activities(store):
    """Import up to 1,000 recent activities; partial imports are explicit."""
    from .activities import import_activities

    with _locked(store) as root:
        data = _load(root)
        if not _status(data)["connected"]:
            raise StravaError("Connect your Strava account before syncing activities.", "not_connected")
        binding = store.db.execute("SELECT value FROM settings WHERE key=?", (ATHLETE_BINDING_KEY,)).fetchone()
        if binding is None or not data.get("athlete_id") or binding[0] != data["athlete_id"]:
            raise StravaError("The Strava athlete does not match this local history; reconnect its account.", "account_mismatch")
        if data.get("expires_at", 0) <= time.time() + 60:
            _refresh(root, data)
        found, partial, failure = [], False, None
        for page in range(1, MAX_PAGES + 1):
            url = "https://www.strava.com/api/v3/athlete/activities?" + urlencode({
                "page": page, "per_page": PER_PAGE})
            try:
                try:
                    batch = _request(url, access_token=data["access_token"])
                except StravaError as error:
                    if error.code != "authorization_rejected":
                        raise
                    _refresh(root, data)
                    batch = _request(url, access_token=data["access_token"])
                if (not isinstance(batch, list) or len(batch) > PER_PAGE
                        or any(not isinstance(item, dict) for item in batch)):
                    raise StravaError("Strava returned invalid activities; try again later.", "invalid_response", True)
                found.extend(batch)
                if len(batch) < PER_PAGE:
                    break
                if page == MAX_PAGES:
                    partial = True
            except StravaError as error:
                if error.code == "authorization_rejected":
                    data["needs_reconnect"] = True
                    _save(root, data)
                if not found:
                    raise
                partial, failure = True, error
                break
        fields = {"id", "sport_type", "type", "distance", "moving_time", "total_elevation_gain",
                  "start_date", "start_date_local", "start_latlng", "name", "location_city", "location_country"}
        compact = [{key: value for key, value in item.items() if key in fields} for item in found]
        try:
            imported = import_activities(store, json.dumps(compact), format="json")
        except ValueError:
            raise StravaError("Strava activity data could not be imported; existing history is preserved.", "invalid_activities") from None
        data.update(last_sync_at=datetime.now(timezone.utc).isoformat(),
                    synced_count=len(found), partial=partial)
        _save(root, data)
        last_import = imported.get("last_import", {})
        result = _status(data) | {"fetched_count": len(found),
                                  "added_count": last_import.get("added_count", 0),
                                  "updated_count": last_import.get("updated_count", 0),
                                  "duplicate_count": last_import.get("duplicate_count", 0),
                                  "retryable": bool(failure and failure.retryable),
                                  "error_code": failure.code if failure else None}
        return result


def disconnect(store, *, revoke=False, clear_credentials=False):
    """Forget local tokens; revoke at Strava only when explicitly requested."""
    with _locked(store) as root:
        data = _load(root)
        if revoke and data.get("refresh_token"):
            _request("https://www.strava.com/oauth/revoke", form={
                "token": data["refresh_token"], "token_type_hint": "refresh_token"},
                basic=f"{data['client_id']}:{data['client_secret']}", empty_ok=True)
        retained = {} if clear_credentials else {key: data[key] for key in
                                                ("client_id", "client_secret") if key in data}
        _save(root, retained)
        return _status(retained)
