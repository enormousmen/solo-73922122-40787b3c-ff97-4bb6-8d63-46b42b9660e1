"""Unit tests for CSV parsing, revision semantics and the Leq formula."""

from __future__ import annotations

import math

import pytest

from app import rules
from app.rules import RuleError


HOUR = "2026-11-15T09:00Z"


def parse(text: str, hour: str | None = HOUR):
    return rules.parse_upload(text.encode("utf-8"), hour)


def test_parse_basic_rows_and_header():
    parsed = parse("minute,revision,value\n0,1,50.5\n1,1,51\n", HOUR)
    assert parsed.hour == HOUR
    assert parsed.rows[0].minute == 0
    assert parsed.rows[0].revision == 1
    assert parsed.rows[0].payload.db_value == 50.5
    assert parsed.rows[1].payload.db_value == 51.0


def test_parse_without_header():
    parsed = parse("0,1,50\n1,2,55\n", HOUR)
    assert len(parsed.rows) == 2
    assert parsed.rows[1].revision == 2


def test_blank_value_means_invalid():
    parsed = parse("minute,revision,value\n0,1,50\n1,1,\n", HOUR)
    assert parsed.rows[1].payload.invalid is True
    assert parsed.rows[1].payload.db_value is None


@pytest.mark.parametrize(
    "marker",
    ["", "invalid", "INVALID", "n/a", "null", "missing"],
)
def test_invalid_markers(marker):
    parsed = parse(f"0,1,{marker}\n", HOUR)
    assert parsed.rows[0].payload.invalid is True


@pytest.mark.parametrize(
    "bad", ["50x", "valid", "NaN", "inf", "1e400", "2000"]
)
def test_bad_values_rejected(bad):
    with pytest.raises(RuleError):
        parse(f"0,1,{bad}\n", HOUR)


def test_negative_level_is_accepted_but_absurd_is_not():
    assert parse("0,1,-5\n", HOUR).rows[0].payload.db_value == -5.0
    with pytest.raises(RuleError):
        parse("0,1,-1000.5\n", HOUR)


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "x", ""])
def test_bad_revisions_rejected(bad):
    with pytest.raises(RuleError):
        parse(f"0,{bad},50\n", HOUR)


@pytest.mark.parametrize("bad", ["60", "-1", "1.0", "x"])
def test_bad_minutes_rejected(bad):
    with pytest.raises(RuleError):
        parse(f"{bad},1,50\n", HOUR)


def test_duplicate_minute_rejected():
    with pytest.raises(RuleError) as exc:
        parse("0,1,50\n0,2,51\n", HOUR)
    assert "duplicate minute" in str(exc.value)


def test_hour_hint_required_without_hour_column():
    with pytest.raises(RuleError):
        parse("0,1,50\n", None)


def test_hour_column_in_file():
    rows = ["hour,minute,revision,value"]
    for minute in (0, 1):
        rows.append(f"{HOUR},{minute},1,50")
    parsed = parse("\n".join(rows) + "\n", None)
    assert parsed.hour == HOUR


def test_hour_column_must_match_hint():
    rows = ["hour,minute,revision,value", "2026-11-15T08:00Z,0,1,50"]
    with pytest.raises(RuleError) as exc:
        parse("\n".join(rows) + "\n", HOUR)
    assert "targets" in str(exc.value)


def test_hour_normalisation():
    assert rules.parse_hour("2026-11-15T09:00:00Z") == HOUR
    assert rules.parse_hour("2026-11-15 09:00+00:00") == HOUR
    assert rules.parse_hour("2026-11-15T11:00+02:00") == HOUR


def test_hour_must_be_whole_hour():
    with pytest.raises(RuleError):
        rules.parse_hour("2026-11-15T09:30Z")


def test_quoted_fields_with_commas():
    parsed = parse('minute,revision,value,note\n0,1,50,"ok, fine"\n', HOUR)
    assert parsed.rows[0].payload.db_value == 50


def test_crlf_and_bom():
    raw = "﻿minute,revision,value\r\n0,1,50\r\n"
    parsed = rules.parse_upload(raw.encode("utf-8"), HOUR)
    assert parsed.rows[0].payload.db_value == 50
    assert parsed.rows[0].minute == 0


def test_empty_and_non_utf8_rejected():
    with pytest.raises(RuleError):
        rules.parse_upload(b"", HOUR)
    with pytest.raises(RuleError):
        rules.parse_upload(b"\xff\xfe", HOUR)


def test_oversized_rejected():
    with pytest.raises(RuleError):
        rules.parse_upload(b"0,1,50\n" + b"x" * (1024 * 1024 + 1), HOUR)


def test_leq_matches_definition():
    values = [40.0 + (minute % 7) for minute in range(60)]
    expected = 10 * math.log10(sum(10 ** (v / 10) for v in values) / 60)
    assert rules.compute_leq(values) == pytest.approx(expected)
    assert rules.compute_leq([60.0] * 60) == pytest.approx(60.0)


def test_payload_key_distinguishes_values():
    invalid = rules.Payload(invalid=True, db_value=None)
    a = rules.Payload(invalid=False, db_value=50.0)
    b = rules.Payload(invalid=False, db_value=50.02)
    c = rules.Payload(invalid=False, db_value=50.0)
    assert rules.payload_key(invalid) != rules.payload_key(a)
    assert rules.payload_key(a) == rules.payload_key(c)
    assert rules.payload_key(a) != rules.payload_key(b)
