"""Durable application state; Codex conversation history is never authoritative."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


DEFAULT_PROFILE = {
    "timezone": "Asia/Hong_Kong",
    "home_airport": None,
    "currency": "HKD",
    "work_start": "09:00",
    "work_end": "18:00",
    "workdays": [0, 1, 2, 3, 4],
    "holidays": [],
    "calendar_confirmed": False,
    "outbound_transfer_minutes": 90,
    "airport_buffer_minutes": 180,
    "home_transfer_minutes": 90,
    "recovery_minutes": 0,
    "timing_confirmed": False,
    "budget": None,
    "max_leave": None,
    "passport_nationality": None,
    "residency": None,
    "policy_text": "",
    "policy_confirmed": False,
    "monthly_token_budget": 500000,
    "policy_rules": {},
    "visit_date_flexibility": None,
}


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger_root = self.root
        while (self.ledger_root.parent / "state.sqlite").is_file():
            self.ledger_root = self.ledger_root.parent
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS trips(
            id TEXT PRIMARY KEY, brief TEXT NOT NULL, created_at TEXT NOT NULL,
            budget INTEGER NOT NULL, max_sessions INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, stage TEXT NOT NULL,
            status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
            thread_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
            cached_tokens INTEGER, result TEXT, error TEXT);
          CREATE TABLE IF NOT EXISTS feedback(
            id INTEGER PRIMARY KEY, trip_id TEXT NOT NULL, created_at TEXT NOT NULL,
            text TEXT NOT NULL, constraints TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name in ("observed_input_tokens", "observed_output_tokens", "observed_cached_tokens"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('profile', ?)",
                        (json.dumps(DEFAULT_PROFILE),))
        self.db.commit()
        with self.ledger() as ledger:
            for row in self.db.execute("SELECT * FROM runs"):
                self._ledger_write(ledger, dict(row), replace=False)

    @contextmanager
    def ledger(self):
        connection = self.db if self.ledger_root == self.root else sqlite3.connect(
            self.ledger_root / "state.sqlite", timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS usage_ledger(
                id TEXT PRIMARY KEY, data_root TEXT NOT NULL, trip_id TEXT NOT NULL,
                stage TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                owner_trip_id TEXT)""")
            if "owner_trip_id" not in {r[1] for r in connection.execute("PRAGMA table_info(usage_ledger)")}:
                connection.execute("ALTER TABLE usage_ledger ADD COLUMN owner_trip_id TEXT")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_ledger)")}
            for name in ("observed_input_tokens", "observed_output_tokens", "observed_cached_tokens"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE usage_ledger ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if connection is not self.db:
                connection.close()

    def _ledger_write(self, connection, row, replace=True):
        operation = "REPLACE" if replace else "IGNORE"
        mapping = connection.execute("SELECT value FROM settings WHERE key=?",
            ("budget_owner:" + str(self.root) + ":" + row["trip_id"],)).fetchone()
        owner = json.loads(mapping[0]) if mapping else row["trip_id"]
        connection.execute(f"""INSERT OR {operation} INTO usage_ledger
            (id,data_root,trip_id,stage,status,started_at,input_tokens,output_tokens,cached_tokens,owner_trip_id,
             observed_input_tokens,observed_output_tokens,observed_cached_tokens)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            row["id"], str(self.root), row["trip_id"], row["stage"], row["status"], row["started_at"],
            row["input_tokens"], row["output_tokens"], row["cached_tokens"], owner,
            row.get("observed_input_tokens", 0), row.get("observed_output_tokens", 0), row.get("observed_cached_tokens", 0)))

    def set_budget_owner(self, trip_id, owner_trip_id):
        self.trip(trip_id)
        with self.ledger() as ledger:
            if not ledger.execute("SELECT 1 FROM trips WHERE id=?", (owner_trip_id,)).fetchone():
                raise ValueError("Budget owner must be a trip in the main local store.")
            key = "budget_owner:" + str(self.root) + ":" + trip_id
            existing = ledger.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if existing and json.loads(existing[0]) != owner_trip_id:
                raise ValueError("Public research already belongs to another trip's cumulative budget.")
            ledger.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(owner_trip_id)))
            ledger.execute("UPDATE usage_ledger SET owner_trip_id=? WHERE data_root=? AND trip_id=?",
                           (owner_trip_id, str(self.root), trip_id))

    def job_usage(self, trip_id):
        own_trip = self.trip(trip_id)
        with self.ledger() as ledger:
            mapping = ledger.execute("SELECT value FROM settings WHERE key=?",
                ("budget_owner:" + str(self.root) + ":" + trip_id,)).fetchone()
            owner = json.loads(mapping[0]) if mapping else trip_id
            limit_trip = ledger.execute("SELECT * FROM trips WHERE id=?", (owner,)).fetchone()
            rows = [dict(r) for r in ledger.execute(
                "SELECT * FROM usage_ledger WHERE COALESCE(owner_trip_id,trip_id)=?", (owner,))]
        return rows, dict(limit_trip) if limit_trip else own_trip

    def monthly_usage(self, month):
        with self.ledger() as ledger:
            rows = [dict(r) for r in ledger.execute(
                "SELECT * FROM usage_ledger WHERE started_at LIKE ?", (month + "%",))]
            row = ledger.execute("SELECT value FROM settings WHERE key='profile'").fetchone()
            limit = json.loads(row[0]).get("monthly_token_budget", DEFAULT_PROFILE["monthly_token_budget"])
        return rows, limit

    def profile(self):
        return DEFAULT_PROFILE | json.loads(self.db.execute(
            "SELECT value FROM settings WHERE key='profile'").fetchone()[0])

    def save_profile(self, profile):
        # Import lazily to keep domain validation separate from persistence.
        from .planning import validate_profile
        validate_profile(profile)
        with self.db:
            self.db.execute("UPDATE settings SET value=? WHERE key='profile'",
                            (json.dumps(profile),))

    def create_trip(self, brief, budget=150000, max_sessions=6):
        if not brief.strip() or len(brief) > 6000:
            raise ValueError("Brief must contain 1–6000 characters.")
        if budget <= 0 or not 1 <= max_sessions <= 30:
            raise ValueError("Use a positive token budget and 1–30 sessions.")
        trip_id = uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO trips VALUES(?,?,?,?,?)", (
                trip_id, brief, now(), budget, max_sessions))
        return trip_id

    def trip(self, trip_id):
        row = self.db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown trip: {trip_id}")
        return dict(row)

    def trips(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM trips ORDER BY created_at DESC")]

    def runs(self, trip_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM runs WHERE trip_id=? ORDER BY started_at, rowid", (trip_id,))]

    def feedback(self, trip_id):
        return [dict(r) | {"constraints": json.loads(r["constraints"])} for r in
                self.db.execute("SELECT * FROM feedback WHERE trip_id=? ORDER BY id", (trip_id,))]

    def add_feedback(self, trip_id, text, constraints):
        self.trip(trip_id)
        if len(text) > 4000:
            raise ValueError("Feedback must be at most 4000 characters.")
        with self.db:
            self.db.execute("INSERT INTO feedback(trip_id,created_at,text,constraints) VALUES(?,?,?,?)",
                            (trip_id, now(), text, json.dumps(constraints)))

    def begin_run(self, trip_id, stage):
        run_id = uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs(id,trip_id,stage,status,started_at) VALUES(?,?,?,?,?)",
                            (run_id, trip_id, stage, "running", now()))
        with self.ledger() as ledger:
            self._ledger_write(ledger, dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()))
        return run_id

    def finish_run(self, run_id, status, usage, result=None, error=None, thread_id=None):
        row = dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        row.update(status=status, input_tokens=usage.get("input_tokens"),
                   output_tokens=usage.get("output_tokens"), cached_tokens=usage.get("cached_input_tokens"))
        with self.ledger() as ledger:
            self._ledger_write(ledger, row)
        with self.db:
            self.db.execute("""UPDATE runs SET status=?,ended_at=?,input_tokens=?,output_tokens=?,
                cached_tokens=?,result=?,error=?,thread_id=COALESCE(?,thread_id) WHERE id=?""", (
                status, now(), usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cached_input_tokens"), json.dumps(result) if result is not None else None,
                error, thread_id, run_id))

    def record_usage(self, run_id, usage):
        """Persist cumulative observed usage without treating an unfinished tail as free."""
        values = [usage.get(key, 0) for key in ("input_tokens", "output_tokens", "cached_input_tokens")]
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Observed usage must use nonnegative integer counts.")
        with self.db:
            self.db.execute("""UPDATE runs SET observed_input_tokens=MAX(observed_input_tokens,?),
                observed_output_tokens=MAX(observed_output_tokens,?),
                observed_cached_tokens=MAX(observed_cached_tokens,?) WHERE id=?""", (*values, run_id))
        row = dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        with self.ledger() as ledger:
            self._ledger_write(ledger, row)

    def record_thread(self, run_id, thread_id):
        with self.db:
            self.db.execute("UPDATE runs SET thread_id=? WHERE id=?", (thread_id, run_id))

    def save_progress(self, run_id, progress):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)",
                            ("progress:" + run_id, json.dumps(progress)))

    def progress(self, run_id):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", ("progress:" + run_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def plans(self, trip_id):
        self.trip(trip_id)
        row = self.db.execute("SELECT value FROM settings WHERE key=?", ("plans:" + trip_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_plans(self, trip_id, plans):
        self.trip(trip_id)
        if not isinstance(plans, dict):
            raise ValueError("Activity plans must be keyed by option ID.")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("plans:" + trip_id, json.dumps(plans, allow_nan=False)))

    def latest_result(self, trip_id):
        row = self.db.execute("""SELECT result FROM runs WHERE trip_id=? AND status='completed'
            ORDER BY started_at DESC, rowid DESC LIMIT 1""", (trip_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @contextmanager
    def worker_lock(self):
        # OS locks release on crashes; serialize runs across all trips for budget accounting.
        import fcntl
        with (self.ledger_root / "worker.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another research worker is running. Its progress is saved.") from None
            try:
                with self.db:
                    self.db.execute("""UPDATE runs SET status='interrupted',ended_at=?,
                        error='Worker stopped before saving a result; usage is unknown.'
                        WHERE status='running'""", (now(),))
                with self.ledger() as ledger:
                    ledger.execute("UPDATE usage_ledger SET status='interrupted' WHERE status='running'")
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
