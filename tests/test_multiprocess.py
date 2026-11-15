"""Multi-process tests: two uvicorn servers share one SQLite database.

These tests start two real service processes (the same command Docker runs)
against the same database file and verify:

* concurrent submissions from both processes never interleave: the hourly
  result always matches the exact set of minute readings committed with it;
* a reader polling during writes never observes a stale hourly value or a
  half-applied batch;
* rejecting a conflicting submission in one process does not disturb reads
  served by the other.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
HOUR = "2026-11-15T09:00Z"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, deadline: float = 45.0) -> None:
    end = time.monotonic() + deadline
    last: Exception | None = None
    while time.monotonic() < end:
        try:
            if httpx.get(url, timeout=1.0).status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - retry any startup error
            last = exc
        time.sleep(0.1)
    raise RuntimeError(f"service at {url} did not become ready: {last}")


class _Server:
    """A uvicorn worker that exits when its parent closes stdin.

    The worker watches stdin (see ``tests/serve_worker.py``) instead of relying
    on signals, because some sandboxes deny a parent the right to signal its
    children.  Signals remain as a fallback.
    """

    def __init__(self, port: int, env: dict[str, str]):
        self.port = port
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "serve_worker.py"), str(port)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
        )
        self.stopped = False

    def shutdown(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            pass
        for signal_call in (self.proc.terminate, self.proc.kill):
            try:
                signal_call()
                self.proc.wait(timeout=5)
                return
            except (ProcessLookupError, PermissionError):
                continue
            except subprocess.TimeoutExpired:
                continue


@pytest.fixture(scope="module")
def two_servers(tmp_path_factory):
    db = tmp_path_factory.mktemp("shared-db") / "noise.sqlite3"
    env = dict(os.environ)
    env["NOISE_DB_PATH"] = str(db)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("VIRTUAL_ENV", None)
    ports = [_free_port(), _free_port()]
    servers = []
    try:
        for port in ports:
            servers.append(_Server(port, env))
        urls = [f"http://127.0.0.1:{port}" for port in ports]
        for url in urls:
            _wait_ready(url + "/healthz")
        yield urls, db
    finally:
        for server in servers:
            server.shutdown()


def csv_for(rows) -> str:
    out = ["minute,revision,value"]
    for minute, revision, value in rows:
        out.append(f"{minute},{revision},{'' if value is None else value}")
    return "\n".join(out) + "\n"


def post_upload(url: str, rows, hour=HOUR):
    return httpx.post(
        url + "/api/upload",
        data={"hour": hour, "text": csv_for(rows)},
        timeout=120.0,
    )


def db_snapshot(db: Path, hour: str = HOUR) -> dict:
    """Ground truth read directly from the shared file."""
    conn = sqlite3.connect(db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        minutes = conn.execute(
            "SELECT minute, revision, is_invalid, db_value FROM minutes "
            "WHERE hour = ? ORDER BY minute",
            (hour,),
        ).fetchall()
        result = conn.execute(
            "SELECT leq_db, revisions_json FROM hourly_results WHERE hour = ?",
            (hour,),
        ).fetchone()
        conn.execute("COMMIT")
        return {
            "minutes": {
                row["minute"]: (row["revision"], row["is_invalid"], row["db_value"])
                for row in minutes
            },
            "leq": None if result is None else result["leq_db"],
            "revisions": (
                None
                if result is None
                else json.loads(result["revisions_json"])
            ),
        }
    finally:
        conn.close()


def expected_leq(values):
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / 60)


def test_concurrent_uploads_from_two_processes_stay_atomic(two_servers):
    urls, db = two_servers

    # Both processes race to create the complete 60-minute base revision.
    halves = [
        [(m, 1, 45.0 + (m % 5)) for m in range(30)],
        [(m, 1, 45.0 + (m % 5)) for m in range(30, 60)],
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda pair: post_upload(urls[pair[0]], pair[1]),
                list(enumerate(halves)),
            )
        )
    assert [r.status_code for r in responses] == [200, 200], [
        r.text for r in responses
    ]
    base = db_snapshot(db)
    assert len(base["minutes"]) == 60
    assert base["leq"] is not None

    values = {m: 45.0 + (m % 5) for m in range(60)}

    # Now both processes push disjoint single-minute revisions at the same
    # time while a reader continuously polls the other process.
    writes = []
    for minute in range(60):
        revision = 2
        value = 60.0 + minute * 0.1
        values[minute] = value
        writes.append((minute, revision, value))

    stop = {"flag": False}
    observed: list[dict] = []
    violations: list[str] = []

    def reader():
        while not stop["flag"]:
            body = httpx.get(urls[1] + "/api/hours/" + HOUR, timeout=60.0).json()
            observed.append(body)
            if body["status"] == "published":
                rows = {row["minute"]: row for row in body["minutes"]}
                if len(rows) != 60:
                    violations.append("published result with missing minutes")
                if body["computed_from_revisions"] != body["minute_revisions"]:
                    violations.append(
                        "result revisions differ from visible readings"
                    )
                values_now = [rows[m]["db"] for m in range(60)]
                if not all(v is not None for v in values_now):
                    violations.append("published result with non-numeric value")
                else:
                    recomputed = round(expected_leq(values_now), 2)
                    if abs(recomputed - body["leq_db"]) > 0.011:
                        violations.append(
                            f"stale/half batch: leq {body['leq_db']} != "
                            f"{recomputed} for visible readings"
                        )
                count = body["minutes_recorded"]
                if count != 60:
                    violations.append(f"visible minute count {count}")

    reader_pool = ThreadPoolExecutor(max_workers=1)
    future = reader_pool.submit(reader)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda item: post_upload(urls[item[0]], [item[1]]),
                    [(i % 2, row) for i, row in enumerate(writes)],
                )
            )
    finally:
        time.sleep(0.2)
        stop["flag"] = True
        future.result(timeout=120)
        reader_pool.shutdown(wait=True)

    assert all(r.status_code == 200 for r in responses), [
        r.text for r in responses if r.status_code != 200
    ]
    assert len(observed) >= 10, f"reader only sampled {len(observed)} times"
    assert not violations, violations[:5]

    final = db_snapshot(db)
    assert final["minutes"] == {
        m: (2, 0, round(v, 2)) for m, v in values.items()
    }
    assert final["leq"] == pytest.approx(
        round(expected_leq([values[m] for m in range(60)]), 2), abs=0.011
    )
    assert final["revisions"] == {str(m): 2 for m in range(60)}

    # Cross-process agreement: both processes report the identical result.
    left = httpx.get(urls[0] + "/api/hours/" + HOUR).json()
    right = httpx.get(urls[1] + "/api/hours/" + HOUR).json()
    assert left == right


def test_conflict_in_one_process_does_not_corrupt_other(two_servers):
    urls, db = two_servers
    hour = "2026-11-15T11:00Z"
    rows = [(m, 1, 50.0) for m in range(60)]
    assert post_upload(urls[0], rows, hour=hour).status_code == 200
    before = httpx.get(urls[1] + "/api/hours/" + hour).json()

    # Process B sees the same state and rejects a conflicting retry...
    bad = [(0, 1, 52.0)]
    response = post_upload(urls[1], bad, hour=hour)
    assert response.status_code == 409
    assert "different content" in response.text

    # ...while process A still serves the committed result unchanged.
    after = httpx.get(urls[0] + "/api/hours/" + hour).json()
    assert after == before

    # A valid retry through process A is accepted after B's rejection.
    assert post_upload(urls[0], rows, hour=hour).status_code == 200
    noop = httpx.get(urls[1] + "/api/hours/" + hour).json()
    assert noop["computed_at"] == before["computed_at"]


def test_cross_process_retraction_and_restore(two_servers):
    urls, db = two_servers
    hour = "2026-11-15T12:00Z"
    rows = [(m, 1, 50.0) for m in range(60)]
    assert post_upload(urls[0], rows, hour=hour).status_code == 200

    assert post_upload(urls[1], [(m, 2, None) for m in (10, 20)], hour=hour).status_code == 200
    retracted = httpx.get(urls[0] + "/api/hours/" + hour).json()
    assert retracted["status"] == "unavailable"
    assert retracted["invalid_minutes"] == [10, 20]
    assert retracted["leq_db"] is None

    assert post_upload(urls[0], [(10, 3, 50.0)], hour=hour).status_code == 200
    partial = httpx.get(urls[1] + "/api/hours/" + hour).json()
    assert partial["status"] == "unavailable"
    assert partial["invalid_minutes"] == [20]

    assert post_upload(urls[1], [(20, 3, 50.0)], hour=hour).status_code == 200
    restored = httpx.get(urls[0] + "/api/hours/" + hour).json()
    assert restored["status"] == "published"
    assert restored["leq_db"] == pytest.approx(50.0, abs=0.01)
    assert restored["invalid_minutes"] == []


def test_simultaneous_identical_uploads_apply_once(two_servers):
    """The same file sent twice at once: one applies, the other is a no-op."""
    urls, db = two_servers
    hour = "2026-11-15T13:00Z"
    rows = [(m, 1, 55.0) for m in range(60)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda url: post_upload(url, rows, hour=hour),
                urls,
            )
        )
    assert all(r.status_code == 200 for r in responses), [
        r.text for r in responses
    ]
    bodies = [r.json() for r in responses]
    assert sorted(body["outcome"] for body in bodies) == ["applied", "noop"]
    snapshot = db_snapshot(db, hour)
    assert len(snapshot["minutes"]) == 60
    assert all(value[0] == 1 for value in snapshot["minutes"].values())
    assert snapshot["leq"] == pytest.approx(55.0, abs=0.01)


def test_simultaneous_disjoint_revisions_both_apply(two_servers):
    """Two batches touching different minutes must not lose each other's writes."""
    urls, db = two_servers
    hour = "2026-11-15T14:00Z"
    assert post_upload(
        urls[0], [(m, 1, 50.0) for m in range(60)], hour=hour
    ).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda item: post_upload(urls[item[0]], item[1], hour=hour),
                [(0, [(5, 2, None)]), (1, [(9, 2, None)])],
            )
        )
    assert [r.status_code for r in responses] == [200, 200], [
        r.text for r in responses
    ]
    result = httpx.get(urls[0] + "/api/hours/" + hour).json()
    assert result["invalid_minutes"] == [5, 9]
    assert result["status"] == "unavailable"
    snapshot = db_snapshot(db, hour)
    assert snapshot["minutes"][5][0] == 2
    assert snapshot["minutes"][9][0] == 2
