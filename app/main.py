"""FastAPI service for hourly equivalent noise levels."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import rules, storage
from .rules import RuleError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(storage.init_db)
    yield


app = FastAPI(
    title="Hourly Noise Level Service",
    version="1.0.0",
    description=(
        "Versioned per-minute A-weighted levels are energy-averaged into an "
        "hourly Leq (10*log10(sum(10**(L/10))/60)) once all 60 minutes are "
        "valid. Every upload is applied atomically."
    ),
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"}
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}


def _error(status: int, message: str, **extra) -> JSONResponse:
    body = {"ok": False, "status": status, "error": message}
    body.update(extra)
    return JSONResponse(body, status_code=status)


def _degraded(exc: sqlite3.OperationalError) -> JSONResponse:
    """Report a transient database failure without implying a rejected batch."""
    return _error(
        503,
        "database busy; the submission was not applied, please retry: "
        + str(exc),
        retryable=True,
    )


async def _read_structured_body(request: Request) -> tuple[bytes, str | None]:
    """Extract ``(csv_bytes, hour)`` from a JSON or text/csv request body."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuleError("request body is not valid JSON")
        if not isinstance(body, dict):
            raise RuleError("JSON body must be an object")
        csv_text = body.get("csv")
        if not isinstance(csv_text, str):
            raise RuleError("JSON body must include a 'csv' string")
        body_hour = body.get("hour")
        if body_hour is not None and not isinstance(body_hour, str):
            raise RuleError("JSON 'hour' must be a string")
        return csv_text.encode("utf-8"), body_hour
    if "text/csv" in content_type or "text/plain" in content_type:
        return await request.body(), None
    raise RuleError(
        "provide a CSV file (multipart 'file'), a 'text' form field, a JSON "
        '{"hour","csv"} body, or a text/csv request body'
    )


@app.post("/api/upload")
async def upload(
    request: Request,
    file: UploadFile | None = File(default=None),
    hour: str | None = Form(default=None),
    text: str | None = Form(default=None),
) -> JSONResponse:
    """Apply one CSV submission atomically.

    ``file`` (multipart) or ``text`` (form field) carries the CSV; ``hour`` is
    the UTC hour label.  Any rule violation rejects the entire submission and
    leaves the database untouched.
    """
    try:
        if file is not None:
            raw = await file.read(rules.MAX_CSV_BYTES + 1)
        elif text is not None and text.strip():
            raw = text.encode("utf-8")
        else:
            raw, structured_hour = await _read_structured_body(request)
            if hour is None or not hour.strip():
                hour = structured_hour
        if hour is None or not hour.strip():
            hour = request.query_params.get("hour")
        parsed = rules.parse_upload(raw, hour)
    except RuleError as exc:
        return _error(400, "submission rejected: " + str(exc))

    checksum = hashlib.sha256(raw).hexdigest()
    try:
        outcome = await run_in_threadpool(storage.submit_hour, parsed)
    except storage.Conflict as exc:
        return _error(
            409,
            "submission rejected: revision conflicts; no rows were applied",
            errors=exc.errors,
            hour=parsed.hour,
            checksum=checksum,
        )
    except sqlite3.OperationalError as exc:
        return _degraded(exc)

    return JSONResponse(
        {
            "ok": True,
            "hour": parsed.hour,
            "checksum": checksum,
            **outcome,
        },
        status_code=200,
    )


@app.get("/api/hours")
def hours() -> JSONResponse:
    try:
        return JSONResponse({"ok": True, "hours": storage.list_hours()})
    except sqlite3.OperationalError as exc:
        return _degraded(exc)


@app.get("/api/hours/{hour}")
def hour_result(hour: str) -> JSONResponse:
    try:
        hour = rules.parse_hour(hour)
    except RuleError as exc:
        return _error(400, str(exc))
    try:
        return JSONResponse({"ok": True, **storage.get_hour(hour)})
    except sqlite3.OperationalError as exc:
        return _degraded(exc)
