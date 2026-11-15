"""Domain rules for minute-level A-weighted noise uploads and hourly results.

A *submission* is one CSV upload for one UTC hour (``YYYY-MM-DDTHH:00Z``).
Every row carries a minute index (0-59), a positive integer revision number and
either a finite dB value or an explicit "invalid" marker (empty value cell).
Revisions are strictly monotonic per minute:

* higher revision than the stored one            -> accepted, replaces it
* identical revision *and* identical payload     -> accepted, no-op (idempotent retry)
* identical revision with a different payload   -> whole submission rejected
* lower revision than the stored one             -> whole submission rejected

A submission is all-or-nothing: no row of a rejected file is applied.

An hourly result exists only when all 60 minutes hold a valid dB value:

    Leq = 10 * log10( sum(10 ** (L_i / 10)) / 60 )

The result is bound to the exact set of minute revisions it was computed from.
Any later accepted revision touching an hour without a complete valid set
retracts the published result; subsequent corrections can restore it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

MINUTES_PER_HOUR = 60
MAX_MINUTE = MINUTES_PER_HOUR - 1
MAX_CSV_BYTES = 1024 * 1024
# Values stay finite and far from overflowing the 10**(L/10) energy sum.
MAX_ABS_DB = 1000.0
MAX_REVISION = 2**63 - 1

INTEGER_RE = re.compile(r"^[0-9]+$")
NUMBER_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)

INVALID_MARKERS = frozenset(
    {"", "invalid", "n/a", "na", "null", "none", "void", "missing"}
)
VALID_MARKERS = frozenset({"valid", "ok"})
REVISION_HEADERS = frozenset(
    {
        "revision",
        "rev",
        "revision_number",
        "revision_no",
        "ver",
        "version",
    }
)
VALUE_HEADERS = frozenset(
    {"value_db", "db", "db_value", "leq", "level", "value"}
)


class RuleError(Exception):
    """A user-facing validation failure."""


@dataclass(frozen=True)
class Payload:
    """The value carried by a (minute, revision) pair."""

    invalid: bool
    db_value: float | None


@dataclass(frozen=True)
class ParsedRow:
    minute: int
    revision: int
    payload: Payload


@dataclass(frozen=True)
class ParsedUpload:
    hour: str
    rows: tuple[ParsedRow, ...]


def parse_hour(value: str) -> str:
    """Validate a UTC hour label and return it normalised to ...T..:00Z.

    Accepts ISO-8601-ish spellings (``Z``, explicit offsets, a space instead of
    ``T``, optional seconds) as long as they denote a whole hour.  Offsets are
    converted to UTC so one instant always has one label.
    """
    text = (value or "").strip()
    if not text:
        raise RuleError("hour is required (UTC, formatted YYYY-MM-DDTHH:00Z)")
    normalised = text.replace(" ", "T", 1)
    if normalised.endswith("Z") or normalised.endswith("z"):
        normalised = normalised[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError as exc:
        raise RuleError(
            "hour must be an ISO-8601 UTC hour such as 2026-11-15T09:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise RuleError("hour must be a whole hour (mm:ss must be 00:00)")
    parsed = parsed.astimezone(timezone.utc)
    if not 1970 <= parsed.year <= 9998:
        raise RuleError("hour is out of the supported range")
    return parsed.strftime("%Y-%m-%dT%H:00Z")


def parse_revision(text: str, line: int) -> int:
    raw = (text or "").strip()
    if not INTEGER_RE.match(raw):
        raise RuleError(
            f"line {line}: revision must be a positive integer, got {raw!r}"
        )
    revision = int(raw)
    if revision < 1:
        raise RuleError(f"line {line}: revision must be >= 1")
    if revision > MAX_REVISION:
        raise RuleError(
            f"line {line}: revision must be <= {MAX_REVISION}"
        )
    return revision


def parse_value(text: str, line: int) -> Payload:
    raw = (text or "").strip()
    if raw.lower() in INVALID_MARKERS:
        return Payload(invalid=True, db_value=None)
    if not NUMBER_RE.match(raw):
        if raw.lower() in VALID_MARKERS:
            raise RuleError(
                f"line {line}: a row marked valid must carry a dB value"
            )
        raise RuleError(
            f"line {line}: value must be a finite dB number or an invalid "
            f"marker (empty cell), got {raw!r}"
        )
    number = float(raw)
    if not math.isfinite(number):
        raise RuleError(f"line {line}: value must be finite, got {raw!r}")
    if abs(number) > MAX_ABS_DB:
        raise RuleError(
            f"line {line}: value must be within ±{MAX_ABS_DB:g} dB, "
            f"got {raw!r}"
        )
    return Payload(invalid=False, db_value=number)


def _split_csv_line(text: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if quoted:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    current.append('"')
                    index += 2
                    continue
                quoted = False
                index += 1
                continue
            current.append(char)
            index += 1
            continue
        if char == '"':
            quoted = True
            index += 1
            continue
        if char == ",":
            fields.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quoted:
        raise RuleError("malformed CSV: unterminated quoted field")
    fields.append("".join(current))
    return fields


def _csv_records(text: str) -> list[tuple[int, list[str]]]:
    """Return (1-based logical line number, fields) for every non-blank row."""
    decoded = text.lstrip("﻿")
    records: list[tuple[int, list[str]]] = []
    pending = ""
    start_line = 0
    for line_no, raw_line in enumerate(decoded.splitlines(), start=1):
        if pending:
            pending += "\n" + raw_line
        else:
            pending = raw_line
            start_line = line_no
        if pending.count('"') % 2 == 1:
            continue  # quoted field continues on the next line
        fields = _split_csv_line(pending)
        if any(cell.strip() for cell in fields):
            records.append((start_line, fields))
        pending = ""
    if pending.strip():
        raise RuleError(f"line {start_line}: unterminated quoted field")
    return records


def _resolve_columns(header: Sequence[str]) -> tuple[int, int, int, int | None]:
    try:
        minute_col = header.index("minute")
    except ValueError as exc:  # pragma: no cover - guarded by caller
        raise RuleError("header must include a minute column") from exc
    revision_col = next(
        (i for i, name in enumerate(header) if name in REVISION_HEADERS), None
    )
    if revision_col is None:
        raise RuleError("header must include a revision column")
    value_col = next(
        (i for i, name in enumerate(header) if name in VALUE_HEADERS), None
    )
    if value_col is None:
        raise RuleError("header must include a value/dB column")
    hour_col = header.index("hour") if "hour" in header else None
    return minute_col, revision_col, value_col, hour_col


def parse_upload(raw: bytes, hour_hint: str | None = None) -> ParsedUpload:
    """Parse and validate a CSV submission.

    Accepted layouts (header optional):
        minute,revision,value
        hour,minute,revision,value
        minute,revision,value,note

    The ``hour`` column, when present, must match ``hour_hint`` (or the hour the
    file itself declares when no hint was given).
    """
    if not raw or not raw.strip():
        raise RuleError("uploaded file is empty")
    if len(raw) > MAX_CSV_BYTES:
        raise RuleError(f"uploaded file exceeds {MAX_CSV_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuleError("uploaded file must be UTF-8 text") from exc

    records = _csv_records(text)
    if not records:
        raise RuleError("uploaded file contains no data rows")

    lookahead = [cell.strip().lower() for cell in records[0][1]]
    if "minute" in lookahead and REVISION_HEADERS.intersection(lookahead):
        header = lookahead
        columns = _resolve_columns(header)
        records = records[1:]
        if not records:
            raise RuleError("uploaded file contains a header but no data rows")
    else:
        columns = (0, 1, 2, None)

    minute_col, revision_col, value_col, hour_col = columns
    file_hour: str | None = None
    hint = parse_hour(hour_hint) if hour_hint and hour_hint.strip() else None
    rows: list[ParsedRow] = []
    seen: dict[int, int] = {}

    for line, fields in records:
        if len(fields) <= max(minute_col, revision_col, value_col):
            raise RuleError(
                f"line {line}: expected at least 3 columns "
                "(minute, revision, value)"
            )
        if hour_col is not None and hour_col < len(fields):
            row_hour = parse_hour(fields[hour_col])
            if file_hour is None:
                file_hour = row_hour
            elif row_hour != file_hour:
                raise RuleError(
                    f"line {line}: rows in one file must share a single hour"
                )
        minute_text = fields[minute_col].strip()
        if not INTEGER_RE.match(minute_text):
            raise RuleError(
                f"line {line}: minute must be a whole number 0-59 "
                f"(one row per minute), got {minute_text!r}"
            )
        minute = int(minute_text)
        if minute > MAX_MINUTE:
            raise RuleError(f"line {line}: minute must be 0-59, got {minute}")
        if minute in seen:
            raise RuleError(
                f"line {line}: duplicate minute {minute} "
                f"(first seen on line {seen[minute]}); one entry per minute"
            )
        seen[minute] = line
        revision = parse_revision(fields[revision_col], line)
        payload = parse_value(fields[value_col], line)
        rows.append(ParsedRow(minute, revision, payload))

    hour = hint or file_hour
    if hour is None:
        raise RuleError(
            "hour is required: provide it in the form or add an hour column"
        )
    if hint is not None and file_hour is not None and hint != file_hour:
        raise RuleError(
            f"rows declare hour {file_hour} but the submission targets {hint}"
        )

    return ParsedUpload(hour=hour, rows=tuple(rows))


def round_db(value: float) -> float:
    """Round to the storage precision (0.01 dB)."""
    return round(value, 2)


def payload_key(payload: Payload) -> str:
    """Canonical comparison key used for idempotent retries.

    Values are compared after the same 0.01 dB rounding that is persisted, so
    a retry of "50.001" against a recorded "50.00" is genuinely the same
    content rather than a spurious conflict.
    """
    if payload.invalid:
        return "invalid"
    assert payload.db_value is not None
    stored = round_db(payload.db_value) + 0.0  # + 0.0 folds -0.0 into 0.0
    return "value:" + stored.hex()


def compute_leq(values: Sequence[float]) -> float:
    """Energy-average 60 minute levels into an hourly Leq."""
    if len(values) != MINUTES_PER_HOUR:
        raise RuleError("hourly level needs exactly 60 minute values")
    total = sum(10.0 ** (value / 10.0) for value in values)
    return 10.0 * math.log10(total / MINUTES_PER_HOUR)
