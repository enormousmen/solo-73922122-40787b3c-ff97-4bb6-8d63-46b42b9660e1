"""End-to-end tests over the HTTP API: revisions, atomicity, JSON queries."""

from __future__ import annotations

import math

import pytest

from tests.conftest import csv_rows, full_hour

HOUR = "2026-11-15T09:00Z"


def upload(client, rows, hour=HOUR, header=True):
    return client.post(
        "/api/upload",
        data={"hour": hour, "text": csv_rows(rows, header=header)},
    )


def leq(values):
    return 10 * math.log10(sum(10 ** (v / 10) for v in values) / 60)


def test_upload_full_hour_publishes_result(client):
    rows = full_hour(value=50.0)
    response = upload(client, rows)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "applied"
    assert body["hour_result_state"] == "published"
    assert body["result"]["leq_db"] == pytest.approx(50.0, abs=0.01)
    assert body["result"]["status"] == "published"
    assert len(body["result"]["minutes"]) == 60

    query = client.get(f"/api/hours/{HOUR}").json()
    assert query["leq_db"] == pytest.approx(leq([50.0] * 60), abs=0.005)
    assert query["invalid_minutes"] == []
    assert query["missing_minutes"] == []
    assert query["minutes_recorded"] == 60
    assert all(str(i) in query["computed_from_revisions"] for i in range(60))
    assert query["computed_from_revisions"] == query["minute_revisions"]


def test_partial_hour_has_no_result(client):
    response = upload(client, [(0, 1, 50.0), (1, 1, 51.0)])
    body = response.json()
    assert body["hour_result_state"] == "unavailable"
    assert body["result"]["status"] == "unavailable"
    assert body["result"]["leq_db"] is None
    assert body["result"]["missing_minutes"] == list(range(2, 60))


def test_leq_uses_distinct_minute_values(client):
    values = [40 + (minute % 13) * 1.5 for minute in range(60)]
    upload(client, [(m, 1, v) for m, v in enumerate(values)])
    result = client.get(f"/api/hours/{HOUR}").json()
    assert result["leq_db"] == pytest.approx(leq(values), abs=0.005)


def test_higher_revision_replaces_value_and_result(client):
    upload(client, full_hour(value=50.0))
    first = client.get(f"/api/hours/{HOUR}").json()

    response = upload(client, [(7, 2, 70.0)])
    body = response.json()
    assert response.status_code == 200
    assert body["applied"][0]["minute"] == 7
    assert body["applied"][0]["previous_revision"] == 1
    assert body["result"]["minutes"][7]["db"] == 70.0
    assert body["result"]["minutes"][7]["revision"] == 2
    assert body["result"]["leq_db"] != first["leq_db"]
    expected = leq([50.0] * 59 + [70.0])
    assert body["result"]["leq_db"] == pytest.approx(expected, abs=0.005)


def test_same_revision_same_content_is_idempotent(client):
    upload(client, full_hour(value=50.0))
    before = client.get(f"/api/hours/{HOUR}").json()
    response = upload(client, full_hour(value=50.0))
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "noop"
    assert body["status"] == "unchanged"
    assert body["applied"] == []
    assert len(body["unchanged"]) == 60
    assert body["hour_result_state"] == "published"
    after = client.get(f"/api/hours/{HOUR}").json()
    assert after["computed_at"] == before["computed_at"]
    assert after["leq_db"] == before["leq_db"]
    assert after["submission_id"] == before["submission_id"]


def test_same_revision_different_content_rejects_whole_upload(client):
    upload(client, full_hour(value=50.0))
    before = client.get(f"/api/hours/{HOUR}").json()
    rows = [(0, 2, 55.0), (1, 1, 59.0), (2, 1, 50.0)]
    response = upload(client, rows)
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert any("different content" in e for e in body["errors"])
    after = client.get(f"/api/hours/{HOUR}").json()
    assert after["minute_revisions"] == before["minute_revisions"]
    assert after["minutes"][0]["db"] == 50.0  # row 0 must not have applied
    assert after["minutes"][1]["db"] == 50.0
    assert after["leq_db"] == before["leq_db"]


def test_older_revision_rejects_whole_upload(client):
    upload(client, [(0, 3, 50.0), (1, 3, 50.0)])
    response = upload(client, [(0, 4, 60.0), (1, 2, 45.0)])
    assert response.status_code == 409
    assert any("older" in e for e in response.json()["errors"])
    result = client.get(f"/api/hours/{HOUR}").json()
    assert result["minutes"][0]["revision"] == 3
    assert result["minutes"][0]["db"] == 50.0
    assert result["minutes"][1]["revision"] == 3


def test_invalid_revision_retracts_result(client):
    upload(client, full_hour(value=50.0))
    assert client.get(f"/api/hours/{HOUR}").json()["status"] == "published"

    response = upload(client, [(5, 2, None)])
    body = response.json()
    assert body["hour_result_state"] == "retracted"
    assert body["result"]["status"] == "unavailable"
    assert body["result"]["leq_db"] is None
    assert body["result"]["invalid_minutes"] == [5]
    assert body["result"]["missing_minutes"] == []
    assert body["result"]["last_event"]["action"] == "retracted"

    query = client.get(f"/api/hours/{HOUR}").json()
    assert query["leq_db"] is None
    assert query["invalid_minutes"] == [5]


def test_missing_and_invalid_minutes_listed(client):
    rows = [
        (m, 1, None if m == 7 else 50.0)
        for m in range(60)
        if m not in (3, 4)
    ]
    response = upload(client, rows)
    result = response.json()["result"]
    assert result["invalid_minutes"] == [7]
    assert result["missing_minutes"] == [3, 4]
    assert result["status"] == "unavailable"


def test_correction_restores_result(client):
    upload(client, full_hour(value=50.0))
    upload(client, [(5, 2, None), (8, 2, None)])
    assert client.get(f"/api/hours/{HOUR}").json()["status"] == "unavailable"

    response = upload(client, [(5, 3, 50.0)])
    assert response.json()["result"]["status"] == "unavailable"
    assert response.json()["result"]["invalid_minutes"] == [8]

    response = upload(client, [(8, 3, 55.0)])
    body = response.json()
    assert body["hour_result_state"] == "published"
    assert body["result"]["status"] == "published"
    assert body["result"]["invalid_minutes"] == []
    assert body["result"]["leq_db"] == pytest.approx(
        leq([50.0] * 59 + [55.0]), abs=0.005
    )


def test_retracted_hour_remembered_in_events(client):
    upload(client, full_hour())
    upload(client, [(0, 2, None)])
    query = client.get(f"/api/hours/{HOUR}").json()
    assert query["last_event"]["action"] == "retracted"
    assert query["last_event"]["leq_db"] is None


def test_query_unknown_hour_is_empty(client):
    body = client.get(f"/api/hours/{HOUR}").json()
    assert body["status"] == "unavailable"
    assert body["has_data"] is False
    assert body["minutes"] == []
    assert body["missing_minutes"] == list(range(60))


def test_invalid_query_hour_rejected(client):
    response = client.get("/api/hours/not-a-hour")
    assert response.status_code == 400


def test_duplicate_minute_rejects_upload(client):
    response = upload(client, [(0, 1, 50.0), (0, 2, 51.0)])
    assert response.status_code == 400
    assert client.get(f"/api/hours/{HOUR}").json()["minutes"] == []


def test_hours_index(client):
    upload(client, [(0, 1, 50.0)])
    upload(client, full_hour(), hour="2026-11-15T10:00Z")
    body = client.get("/api/hours").json()
    assert [h["hour"] for h in body["hours"]] == [
        "2026-11-15T09:00Z",
        "2026-11-15T10:00Z",
    ]
    assert body["hours"][0]["status"] == "unavailable"
    assert body["hours"][1]["status"] == "published"


def test_csv_file_upload(client):
    content = csv_rows(full_hour()).encode("utf-8")
    response = client.post(
        "/api/upload",
        data={"hour": HOUR},
        files={"file": ("levels.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "published"


def test_json_body_upload(client):
    response = client.post(
        "/api/upload",
        json={"hour": HOUR, "csv": csv_rows(full_hour())},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["leq_db"] == pytest.approx(50.0, abs=0.01)


def test_hour_column_only_upload(client):
    rows = ["hour,minute,revision,value"]
    for minute in range(60):
        rows.append(f"{HOUR},{minute},1,50")
    response = client.post(
        "/api/upload",
        content="\n".join(rows) + "\n",
        headers={"content-type": "text/csv"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["hour"] == HOUR


def test_upload_requires_payload(client):
    response = client.post("/api/upload", data={"hour": HOUR})
    assert response.status_code == 400


def test_index_page_and_health(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "小时等效声级" in page.text
    assert client.get("/healthz").json() == {"ok": True}


def test_rejects_conflicting_hour_column(client):
    rows = ["hour,minute,revision,value", f"2026-11-15T08:00Z,0,1,50"]
    response = client.post(
        "/api/upload",
        data={"hour": HOUR, "text": "\n".join(rows)},
    )
    assert response.status_code == 400
