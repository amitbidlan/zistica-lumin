"""Tests for POST /v1/labels (Slice 3 PR C — labels endpoint)."""

from __future__ import annotations


def test_post_label_inserts_row(client, db):
    r = client.post(
        "/v1/labels",
        json={
            "trace_id": "trace-x",
            "span_id": "span-y",
            "field": "tool_params",
            "label": "good",
            "category": "false_positive",
            "notes": "rule fired but the action was actually fine",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "good"
    assert body["id"].startswith("lbl_")

    # Row in DB
    rows = db.fetchall_dict("SELECT * FROM labels WHERE id = ?", [body["id"]])
    assert len(rows) == 1
    assert rows[0]["label"] == "good"
    assert rows[0]["category"] == "false_positive"
    assert rows[0]["labeled_by"] == "dashboard"


def test_post_label_rejects_bad_label(client):
    r = client.post(
        "/v1/labels",
        json={"trace_id": "t", "field": "input", "label": "purple"},
    )
    assert r.status_code == 400


def test_post_label_rejects_bad_field(client):
    r = client.post(
        "/v1/labels",
        json={"trace_id": "t", "field": "garbage_field", "label": "bad"},
    )
    assert r.status_code == 400


def test_post_label_with_minimal_body(client, db):
    """All fields except label and field are optional."""
    r = client.post(
        "/v1/labels",
        json={"field": "input", "label": "bad"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "bad"
