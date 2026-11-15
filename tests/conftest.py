"""Shared fixtures: an isolated database per test and a test client."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_ENV = "NOISE_DB_PATH"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "noise.sqlite3"
    os.environ[DB_ENV] = str(path)
    from app import storage

    storage.init_db()
    yield path
    os.environ.pop(DB_ENV, None)


@pytest.fixture()
def client(db_path: Path):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def csv_rows(rows, header: bool = True) -> str:
    """Build a CSV from (minute, revision, value) tuples."""
    lines = ["minute,revision,value"] if header else []
    for minute, revision, value in rows:
        lines.append(f"{minute},{revision},{'' if value is None else value}")
    return "\n".join(lines) + "\n"


def full_hour(revision: int = 1, value=50.0, hour_rows=None) -> list[tuple]:
    if hour_rows is not None:
        return list(hour_rows)
    return [(minute, revision, value) for minute in range(60)]
