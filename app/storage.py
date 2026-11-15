"""SQLite persistence for submissions, minute readings and hourly results.

Consistency model
-----------------
* Every accepted submission is applied inside a single ``BEGIN IMMEDIATE``
  transaction.  SQLite serialises writers across processes, so two service
  processes sharing one database file can never interleave two submissions.
* The minute readings and the hourly result they produce are written in that
  same transaction, so a commit publishes both effects together or neither.
* Readers use a read transaction (a WAL snapshot) so a query observes the
  database as of a single commit: never a half-applied batch and never a
  result computed from readings the client cannot see.

The database uses WAL journaling so readers never block the submitting writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

from . import rules
from .rules import MINUTES_PER_HOUR, ParsedUpload, Payload

WRITE_LOCK_TIMEOUT_SECONDS = 30.0
BUSY_RETRY_WINDOW_SECONDS = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hour        TEXT    NOT NULL,
    outcome     TEXT    NOT NULL CHECK (outcome IN ('applied', 'noop')),
    row_count   INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS minutes (
    hour          TEXT    NOT NULL,
    minute        INTEGER NOT NULL CHECK (minute BETWEEN 0 AND 59),
    revision      INTEGER NOT NULL CHECK (revision >= 1),
    is_invalid    INTEGER NOT NULL CHECK (is_invalid IN (0, 1)),
    db_value      REAL,
    payload_key   TEXT    NOT NULL,
    submission_id INTEGER NOT NULL REFERENCES submissions(id),
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (hour, minute),
    CHECK ((is_invalid = 1 AND db_value IS NULL)
        OR (is_invalid = 0 AND db_value IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_minutes_hour ON minutes(hour);

CREATE TABLE IF NOT EXISTS hourly_results (
    hour            TEXT PRIMARY KEY,
    leq_db          REAL NOT NULL,
    revisions_json  TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id),
    computed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS result_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hour            TEXT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('published', 'retracted')),
    leq_db          REAL,
    revisions_json  TEXT,
    submission_id   INTEGER NOT NULL REFERENCES submissions(id),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_result_events_hour
    ON result_events(hour, id);
"""


class Conflict(Exception):
    """Submission rejected because of a revision rule violation."""

    def __init__(self, errors: Sequence[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def database_path() -> Path:
    return Path(os.environ.get("NOISE_DB_PATH", "/data/noise.sqlite3"))


def _open() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path,
        timeout=WRITE_LOCK_TIMEOUT_SECONDS,
        isolation_level=None,  # explicit transaction control
    )
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"PRAGMA busy_timeout = {int(WRITE_LOCK_TIMEOUT_SECONDS * 1000)}"
    )
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _retry_busy(action: Callable[[], object]) -> object:
    deadline = time.monotonic() + BUSY_RETRY_WINDOW_SECONDS
    delay = 0.01
    while True:
        try:
            return action()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def init_db() -> None:
    """Create the schema; safe to call from several processes at once."""

    def action() -> None:
        conn = _open()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    _retry_busy(action)


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """A consistent point-in-time view spanning multiple SELECTs."""
    conn = _open()
    try:
        conn.execute("BEGIN")
        # Touch a table so the WAL read snapshot is taken here; every later
        # SELECT in this transaction observes the same commit.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _run_write(action: Callable[[sqlite3.Connection], object]) -> object:
    def attempt() -> object:
        conn = _open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                outcome = action(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return outcome
        finally:
            conn.close()

    return _retry_busy(attempt)


def _fingerprint(rows: Sequence[sqlite3.Row]) -> str:
    parts = [
        f"{row['minute']}:{row['revision']}:{row['payload_key']}" for row in rows
    ]
    return "|".join(sorted(parts))


def _refresh_hour_result(
    conn: sqlite3.Connection, hour: str, submission_id: int
) -> tuple[str, float | None, str | None]:
    """Bring the hourly result in line with the minute rows of ``hour``.

    Runs inside the submission transaction, so the result and the readings it
    was derived from become visible in the same commit.  Returns
    ``(state, leq, computed_at)`` where state is 'published', 'retracted' or
    'unavailable'.
    """
    rows = conn.execute(
        "SELECT minute, revision, is_invalid, db_value, payload_key "
        "FROM minutes WHERE hour = ? ORDER BY minute",
        (hour,),
    ).fetchall()
    present = {row["minute"] for row in rows}
    missing = [m for m in range(MINUTES_PER_HOUR) if m not in present]
    invalid = [row["minute"] for row in rows if row["is_invalid"]]
    existing = conn.execute(
        "SELECT * FROM hourly_results WHERE hour = ?", (hour,)
    ).fetchone()

    if missing or invalid:
        if existing is not None:
            conn.execute("DELETE FROM hourly_results WHERE hour = ?", (hour,))
            conn.execute(
                "INSERT INTO result_events "
                "(hour, action, leq_db, revisions_json, submission_id, created_at) "
                "VALUES (?, 'retracted', NULL, NULL, ?, ?)",
                (hour, submission_id, utc_now()),
            )
            return "retracted", None, None
        return "unavailable", None, None

    leq = round(rules.compute_leq([row["db_value"] for row in rows]), 2)
    revisions = {str(row["minute"]): row["revision"] for row in rows}
    revisions_json = json.dumps(
        revisions, sort_keys=True, separators=(",", ":")
    )
    fingerprint = _fingerprint(rows)

    if (
        existing is not None
        and existing["fingerprint"] == fingerprint
        and existing["leq_db"] == leq
    ):
        # Identical revision set and identical levels: byte-for-byte the same
        # published result, so leave it (and its computed_at) untouched.
        return "published", existing["leq_db"], existing["computed_at"]

    now = utc_now()
    conn.execute(
        """
        INSERT INTO hourly_results
            (hour, leq_db, revisions_json, fingerprint, submission_id, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(hour) DO UPDATE SET
            leq_db = excluded.leq_db,
            revisions_json = excluded.revisions_json,
            fingerprint = excluded.fingerprint,
            submission_id = excluded.submission_id,
            computed_at = excluded.computed_at
        """,
        (hour, leq, revisions_json, fingerprint, submission_id, now),
    )
    conn.execute(
        "INSERT INTO result_events "
        "(hour, action, leq_db, revisions_json, submission_id, created_at) "
        "VALUES (?, 'published', ?, ?, ?, ?)",
        (hour, leq, revisions_json, submission_id, now),
    )
    return "published", leq, now


def submit_hour(parsed: ParsedUpload) -> dict:
    """Apply one upload atomically.

    Returns a result dict.  Raises :class:`Conflict` when any row violates the
    revision rules; in that case nothing has been written.
    """
    hour = parsed.hour

    def action(conn: sqlite3.Connection) -> dict:
        rows = conn.execute(
            "SELECT minute, revision, payload_key FROM minutes WHERE hour = ?",
            (hour,),
        ).fetchall()
        current = {row["minute"]: row for row in rows}

        errors: list[str] = []
        applied: list[dict] = []
        unchanged: list[dict] = []
        pending: list[tuple[Payload, int, int]] = []  # (payload, minute, revision)

        for row in parsed.rows:
            stored = current.get(row.minute)
            incoming_key = rules.payload_key(row.payload)
            if stored is None:
                applied.append({"minute": row.minute, "revision": row.revision})
                pending.append((row.payload, row.minute, row.revision))
                continue
            if row.revision > stored["revision"]:
                applied.append(
                    {
                        "minute": row.minute,
                        "revision": row.revision,
                        "previous_revision": stored["revision"],
                    }
                )
                pending.append((row.payload, row.minute, row.revision))
                continue
            if row.revision == stored["revision"]:
                if incoming_key == stored["payload_key"]:
                    unchanged.append(
                        {"minute": row.minute, "revision": row.revision}
                    )
                    continue
                errors.append(
                    f"minute {row.minute}: revision {row.revision} was already "
                    "recorded with different content"
                )
                continue
            errors.append(
                f"minute {row.minute}: revision {row.revision} is older than the "
                f"recorded revision {stored['revision']}"
            )

        if errors:
            raise Conflict(errors)

        outcome = "applied" if pending else "noop"
        cursor = conn.execute(
            "INSERT INTO submissions (hour, outcome, row_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            (hour, outcome, len(parsed.rows), utc_now()),
        )
        submission_id = int(cursor.lastrowid)
        now = utc_now()
        for payload, minute, revision in pending:
            conn.execute(
                """
                INSERT INTO minutes
                    (hour, minute, revision, is_invalid, db_value,
                     payload_key, submission_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hour, minute) DO UPDATE SET
                    revision = excluded.revision,
                    is_invalid = excluded.is_invalid,
                    db_value = excluded.db_value,
                    payload_key = excluded.payload_key,
                    submission_id = excluded.submission_id,
                    updated_at = excluded.updated_at
                """,
                (
                    hour,
                    minute,
                    revision,
                    1 if payload.invalid else 0,
                    None if payload.invalid else rules.round_db(payload.db_value),
                    rules.payload_key(payload),
                    submission_id,
                    now,
                ),
            )

        state, leq, computed_at = _refresh_hour_result(conn, hour, submission_id)
        snapshot = _read_hour(conn, hour)
        return {
            "status": "unchanged" if outcome == "noop" else "accepted",
            "outcome": outcome,
            "submission_id": submission_id,
            "applied": applied,
            "unchanged": unchanged,
            "hour_result_state": state,
            "result": snapshot,
        }

    return _run_write(action)


def _minute_list(rows: Sequence[sqlite3.Row]) -> list[dict]:
    return [
        {
            "minute": row["minute"],
            "revision": row["revision"],
            "invalid": bool(row["is_invalid"]),
            "db": None if row["is_invalid"] else row["db_value"],
        }
        for row in rows
    ]


def _read_hour(conn: sqlite3.Connection, hour: str) -> dict:
    """Read minutes and hourly result from one snapshot.

    Must be called inside a transaction (read snapshot or submission
    transaction) so the two reads belong to the same commit.
    """
    rows = conn.execute(
        "SELECT minute, revision, is_invalid, db_value FROM minutes "
        "WHERE hour = ? ORDER BY minute",
        (hour,),
    ).fetchall()
    result = conn.execute(
        "SELECT leq_db, revisions_json, submission_id, computed_at "
        "FROM hourly_results WHERE hour = ?",
        (hour,),
    ).fetchone()
    last_event = conn.execute(
        "SELECT action, leq_db, submission_id, created_at FROM result_events "
        "WHERE hour = ? ORDER BY id DESC LIMIT 1",
        (hour,),
    ).fetchone()

    present = {row["minute"] for row in rows}
    missing = [m for m in range(MINUTES_PER_HOUR) if m not in present]
    invalid = [row["minute"] for row in rows if row["is_invalid"]]
    revision_map = {str(row["minute"]): row["revision"] for row in rows}

    published = result is not None
    computed_from = None
    if published:
        computed_from = json.loads(result["revisions_json"])

    payload = {
        "hour": hour,
        "status": "published" if published else "unavailable",
        "leq_db": result["leq_db"] if published else None,
        "computed_at": result["computed_at"] if published else None,
        "submission_id": result["submission_id"] if published else None,
        "computed_from_revisions": computed_from,
        "minute_revisions": revision_map,
        "minutes": _minute_list(rows),
        "invalid_minutes": invalid,
        "missing_minutes": missing,
        "minutes_recorded": len(rows),
        "valid_minutes": (
            MINUTES_PER_HOUR - len(invalid) - len(missing)
        ),
        "has_data": bool(rows),
        "last_event": (
            {
                "action": last_event["action"],
                "leq_db": last_event["leq_db"],
                "submission_id": last_event["submission_id"],
                "created_at": last_event["created_at"],
            }
            if last_event is not None
            else None
        ),
    }
    # The stored result is the authoritative set of revisions it used; it is
    # written in the same transaction as the readings, so agreement is the
    # consistency invariant.
    if published and computed_from != revision_map:
        raise RuntimeError(
            f"inconsistent state for hour {hour}: stored result revisions "
            "differ from recorded readings"
        )
    return payload


def get_hour(hour: str) -> dict:
    with read_snapshot() as conn:
        return _read_hour(conn, hour)


def list_hours() -> list[dict]:
    with read_snapshot() as conn:
        rows = conn.execute(
            "SELECT hour, COUNT(*) AS n FROM minutes GROUP BY hour ORDER BY hour"
        ).fetchall()
        summaries = []
        for row in rows:
            result = conn.execute(
                "SELECT leq_db FROM hourly_results WHERE hour = ?",
                (row["hour"],),
            ).fetchone()
            summaries.append(
                {
                    "hour": row["hour"],
                    "minutes_recorded": row["n"],
                    "status": (
                        "published" if result is not None else "unavailable"
                    ),
                    "leq_db": result["leq_db"] if result is not None else None,
                }
            )
        return summaries
