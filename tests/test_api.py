"""API integration tests.  Require a real PostgreSQL:

    TEST_DATABASE_URL=postgresql+psycopg2://... python -m pytest tests/test_api.py
"""
import os

import pytest

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="set TEST_DATABASE_URL to run API integration tests"
)

if DATABASE_URL:
    os.environ["DATABASE_URL"] = DATABASE_URL

TRACK_BODY = {
    "name": "Tianzhu Trail 2-Lap",
    "gun_time": "2026-10-01T08:00:00Z",
    "checkpoints": ["CP1", "CP2"],
    "finish": "FIN",
    "laps": 2,
    "segments": [
        {"min_seconds": 300, "max_seconds": 900},
        {"min_seconds": 300, "max_seconds": 900},
        {"min_seconds": 300, "max_seconds": 900},
    ],
}


@pytest.fixture()
def client():
    from sqlalchemy import text

    from app.db import Base, engine
    from app.main import app

    with engine.begin() as conn:
        Base.metadata.drop_all(bind=conn)
        conn.execute(text("DROP SEQUENCE IF EXISTS receive_seq_seq"))
        conn.execute(text("CREATE SEQUENCE receive_seq_seq START WITH 1 CACHE 1"))
        Base.metadata.create_all(bind=conn)

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def track_id(client):
    resp = client.post("/tracks", json=TRACK_BODY)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _reading(rid, chip="CHIP-001", point="CP1", time="2026-10-01T08:10:00Z"):
    return {"id": rid, "chip_id": chip, "point": point, "time": time}


# ---------------------------------------------------------------- tracks --

def test_create_and_get_track(client, track_id):
    resp = client.get(f"/tracks/{track_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gun_time"] == "2026-10-01T08:00:00Z"
    assert body["checkpoints"] == ["CP1", "CP2"]
    assert body["laps"] == 2
    assert len(body["segments"]) == 3


def test_track_validation(client):
    bad = dict(TRACK_BODY, segments=[{"min_seconds": 1, "max_seconds": 2}])
    assert client.post("/tracks", json=bad).status_code == 422

    bad = dict(TRACK_BODY, laps=0)
    assert client.post("/tracks", json=bad).status_code == 422

    bad = dict(TRACK_BODY, checkpoints=[])
    assert client.post("/tracks", json=bad).status_code == 422

    bad = dict(TRACK_BODY, gun_time="2026-10-01T08:00:00")  # naive datetime
    assert client.post("/tracks", json=bad).status_code == 422

    bad = dict(
        TRACK_BODY,
        segments=[
            {"min_seconds": 900, "max_seconds": 300},
            {"min_seconds": 300, "max_seconds": 900},
            {"min_seconds": 300, "max_seconds": 900},
        ],
    )
    assert client.post("/tracks", json=bad).status_code == 422

    bad = dict(TRACK_BODY, finish="CP1")
    assert client.post("/tracks", json=bad).status_code == 422


def test_get_missing_track_404(client):
    assert client.get("/tracks/nope").status_code == 404


# --------------------------------------------------------------- readings --

def test_reading_upload_idempotent_and_conflict(client):
    body = _reading("rd-1")
    r1 = client.post("/readings", json=body)
    assert r1.status_code == 201
    assert r1.json()["receive_seq"] == 1
    assert r1.json()["voided"] is False

    r2 = client.post("/readings", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    r3 = client.post("/readings", json=_reading("rd-1", time="2026-10-01T08:11:00Z"))
    assert r3.status_code == 409
    assert r3.json()["existing"]["id"] == "rd-1"


def test_reading_equivalent_timezone_is_idempotent(client):
    client.post("/readings", json=_reading("rd-tz"))
    resp = client.post("/readings", json=_reading("rd-tz", time="2026-10-01T10:10:00+02:00"))
    assert resp.status_code == 200


def test_void_flow(client):
    assert client.post("/voids", json={"reading_id": "ghost"}).status_code == 404

    client.post("/readings", json=_reading("rd-v"))
    r1 = client.post("/voids", json={"reading_id": "rd-v", "reason": "misread"})
    assert r1.status_code == 201
    r2 = client.post("/voids", json={"reading_id": "rd-v"})
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]

    assert client.get("/readings/rd-v").json()["voided"] is True


# ----------------------------------------------------------- adjudication --

def _upload_full_chain(client, chip="CHIP-001"):
    data = [
        ("rd-01", "CP1", "2026-10-01T08:10:00Z"),
        ("rd-02", "CP2", "2026-10-01T08:20:00Z"),
        ("rd-03", "FIN", "2026-10-01T08:30:00Z"),
        ("rd-04", "CP1", "2026-10-01T08:40:00Z"),
        ("rd-05", "CP2", "2026-10-01T08:50:00Z"),
        ("rd-06", "FIN", "2026-10-01T09:00:00Z"),
    ]
    for rid, point, t in data:  # deliberately uploaded in scrambled order
        pass
    for rid, point, t in [data[5], data[2], data[0], data[3], data[1], data[4]]:
        resp = client.post(
            "/readings", json=_reading(rid, chip=chip, point=point, time=t)
        )
        assert resp.status_code == 201


def test_publish_finish_and_versioning(client, track_id):
    _upload_full_chain(client)

    # wrong expected version for a chip with no adjudication yet
    resp = client.post(
        f"/tracks/{track_id}/chips/CHIP-001/adjudications",
        json={"expected_version": 1},
    )
    assert resp.status_code == 409
    assert resp.json()["current_version"] == 0

    resp = client.post(
        f"/tracks/{track_id}/chips/CHIP-001/adjudications",
        json={"expected_version": 0},
    )
    assert resp.status_code == 201, resp.text
    v1 = resp.json()
    assert v1["version"] == 1
    assert v1["status"] == "finished"
    assert v1["elapsed_seconds"] == 3600.0
    assert v1["laps_completed"] == 2
    assert [r["id"] for r in v1["adopted_readings"]] == [
        "rd-01", "rd-02", "rd-03", "rd-04", "rd-05", "rd-06",
    ]
    assert v1["track_snapshot"]["gun_time"] == "2026-10-01T08:00:00Z"

    # stale expected version now conflicts
    resp = client.post(
        f"/tracks/{track_id}/chips/CHIP-001/adjudications",
        json={"expected_version": 0},
    )
    assert resp.status_code == 409
    assert resp.json()["current_version"] == 1

    # late backfill + void must NOT rewrite v1, only the next version
    client.post(
        "/readings",
        json=_reading("rd-00", point="CP1", time="2026-10-01T08:05:00Z"),
    )
    client.post("/readings", json=_reading("rd-09", point="CP1", time="2026-10-01T08:11:00Z"))
    client.post("/voids", json={"reading_id": "rd-09", "reason": "misread"})

    resp = client.post(
        f"/tracks/{track_id}/chips/CHIP-001/adjudications",
        json={"expected_version": 1},
    )
    assert resp.status_code == 201
    v2 = resp.json()
    assert v2["version"] == 2
    # rd-00 sorts before rd-01 in the id tie-break -> adopted by v2
    assert v2["adopted_readings"][0]["id"] == "rd-00"
    assert [r["id"] for r in v2["voided_readings"]] == ["rd-09"]
    assert "rd-09" not in [r["id"] for r in v2["excluded_readings"]]

    # v1 is immutable
    resp = client.get(f"/tracks/{track_id}/chips/CHIP-001/adjudications/1")
    assert resp.status_code == 200
    again = resp.json()
    assert again["adopted_readings"][0]["id"] == "rd-01"
    assert again["voided_readings"] == []
    assert again["receive_seq_high"] == v1["receive_seq_high"]

    # listing shows both versions in order
    resp = client.get(f"/tracks/{track_id}/chips/CHIP-001/adjudications")
    assert [v["version"] for v in resp.json()] == [1, 2]


def test_publish_dnf(client, track_id):
    client.post("/readings", json=_reading("rd-20", chip="CHIP-002", point="CP1"))
    client.post(
        "/readings",
        json=_reading("rd-21", chip="CHIP-002", point="CP2", time="2026-10-01T08:20:00Z"),
    )
    resp = client.post(
        f"/tracks/{track_id}/chips/CHIP-002/adjudications",
        json={"expected_version": 0},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "dnf"
    assert body["points_passed"] == 2
    assert body["total_points"] == 6
    assert body["laps_completed"] == 0
    assert body["elapsed_seconds"] is None


def test_publish_unknown_track_404(client):
    resp = client.post(
        "/tracks/nope/chips/CHIP-001/adjudications", json={"expected_version": 0}
    )
    assert resp.status_code == 404


def test_get_missing_adjudication_404(client, track_id):
    resp = client.get(f"/tracks/{track_id}/chips/CHIP-001/adjudications/99")
    assert resp.status_code == 404


# ----------------------------------------------------------- concurrency --
#
# Regression tests for the receive-seq high-water race: an event that has
# claimed its receive_seq but not yet committed must never be covered by a
# published high-water mark it did not participate in.

def test_publish_boundary_waits_for_inflight_reading(client, track_id):
    import threading
    import time
    from datetime import datetime, timezone

    from sqlalchemy import text

    from app.db import SessionLocal
    from app.main import publish_adjudication
    from app.models import CHIP_LOCK_SQL, Reading
    from app.schemas import AdjudicationCreate

    chip = "CHIP-INFLIGHT"
    # Simulate an in-flight POST /readings: per-chip lock held, row
    # inserted (receive_seq claimed), transaction still open.
    s1 = SessionLocal()
    s1.execute(text(CHIP_LOCK_SQL), {"chip_id": chip})
    s1.add(
        Reading(
            id="rd-inflight",
            chip_id=chip,
            point="CP1",
            time=datetime(2026, 10, 1, 8, 10, tzinfo=timezone.utc),
        )
    )
    s1.flush()
    seq = s1.execute(
        text("SELECT receive_seq FROM readings WHERE id = 'rd-inflight'")
    ).scalar_one()

    published = {}

    def do_publish():
        s2 = SessionLocal()
        try:
            published["body"] = publish_adjudication(
                track_id, chip, AdjudicationCreate(expected_version=0), s2
            )
        finally:
            s2.close()

    t = threading.Thread(target=do_publish)
    t.start()
    time.sleep(0.5)
    # The publish must wait for the in-flight reading instead of fixing a
    # high-water mark that would silently cover it.
    assert t.is_alive(), "publish must wait for the in-flight reading"
    s1.commit()
    t.join(timeout=10)
    s1.close()
    assert not t.is_alive(), "publish must complete once the reading commits"

    body = published["body"]
    assert body["receive_seq_high"] >= seq
    inputs = (
        [r["id"] for r in body["adopted_readings"]]
        + [r["id"] for r in body["excluded_readings"]]
        + [r["id"] for r in body["voided_readings"]]
    )
    assert "rd-inflight" in inputs


def test_publish_boundary_waits_for_inflight_void(client, track_id):
    import threading
    import time

    from sqlalchemy import text

    from app.db import SessionLocal
    from app.main import publish_adjudication
    from app.models import CHIP_LOCK_SQL, VoidEvent
    from app.schemas import AdjudicationCreate

    chip = "CHIP-INFLIGHT-VOID"
    client.post("/readings", json=_reading("rd-target", chip=chip, point="CP1"))

    # In-flight POST /voids: lock held, void event inserted, not committed.
    s1 = SessionLocal()
    s1.execute(text(CHIP_LOCK_SQL), {"chip_id": chip})
    s1.add(VoidEvent(reading_id="rd-target", reason="misread"))
    s1.flush()
    seq = s1.execute(
        text("SELECT receive_seq FROM void_events WHERE reading_id = 'rd-target'")
    ).scalar_one()

    published = {}

    def do_publish():
        s2 = SessionLocal()
        try:
            published["body"] = publish_adjudication(
                track_id, chip, AdjudicationCreate(expected_version=0), s2
            )
        finally:
            s2.close()

    t = threading.Thread(target=do_publish)
    t.start()
    time.sleep(0.5)
    assert t.is_alive(), "publish must wait for the in-flight void event"
    s1.commit()
    t.join(timeout=10)
    s1.close()
    assert not t.is_alive()

    body = published["body"]
    assert body["receive_seq_high"] >= seq
    assert [r["id"] for r in body["voided_readings"]] == ["rd-target"]
    assert "rd-target" not in [r["id"] for r in body["adopted_readings"]]


def test_event_after_boundary_lands_above_high_water(client, track_id):
    chip = "CHIP-BOUNDARY"
    client.post("/readings", json=_reading("rd-a", chip=chip, point="CP1"))
    resp = client.post(
        f"/tracks/{track_id}/chips/{chip}/adjudications",
        json={"expected_version": 0},
    )
    assert resp.status_code == 201
    high = resp.json()["receive_seq_high"]

    # Committed after the boundary: must receive a seq above the published
    # high-water mark and stay out of v1.
    late = client.post(
        "/readings",
        json=_reading("rd-late", chip=chip, point="CP1", time="2026-10-01T08:11:00Z"),
    )
    assert late.status_code == 201
    assert late.json()["receive_seq"] > high

    v1 = client.get(f"/tracks/{track_id}/chips/{chip}/adjudications/1").json()
    assert v1["receive_seq_high"] == high
    ids_v1 = [
        r["id"]
        for r in v1["adopted_readings"] + v1["excluded_readings"] + v1["voided_readings"]
    ]
    assert "rd-late" not in ids_v1

    resp = client.post(
        f"/tracks/{track_id}/chips/{chip}/adjudications",
        json={"expected_version": 1},
    )
    assert resp.status_code == 201
    v2 = resp.json()
    assert v2["receive_seq_high"] >= late.json()["receive_seq"]
    ids_v2 = [
        r["id"]
        for r in v2["adopted_readings"] + v2["excluded_readings"] + v2["voided_readings"]
    ]
    assert "rd-late" in ids_v2


def test_concurrent_ingest_and_publish_keep_atomic_boundary(client, track_id):
    """Hammer one chip with concurrent uploads, voids and publishes; every
    published version must satisfy: committed events with receive_seq <=
    receive_seq_high are exactly the version's input set."""
    import threading

    chip = "CHIP-RACE"
    errors = []
    voids = []  # (reading_id, receive_seq) of committed void events
    lock = threading.Lock()
    stop = threading.Event()

    def uploader(n):
        try:
            for i in range(12):
                rid = f"race-{n}-{i}"
                r = client.post(
                    "/readings",
                    json=_reading(rid, chip=chip, point="CP1"),
                )
                assert r.status_code == 201, r.text
                if i % 4 == 3:
                    v = client.post(
                        "/voids", json={"reading_id": rid, "reason": "misread"}
                    )
                    assert v.status_code == 201, v.text
                    with lock:
                        voids.append((rid, v.json()["receive_seq"]))
        except Exception as exc:  # noqa: BLE001 - surfaced below
            with lock:
                errors.append(exc)

    def publisher():
        expected = 0
        try:
            while not stop.is_set():
                r = client.post(
                    f"/tracks/{track_id}/chips/{chip}/adjudications",
                    json={"expected_version": expected},
                )
                if r.status_code == 201:
                    expected += 1
                elif r.status_code == 409:
                    expected = r.json()["current_version"]
                else:
                    raise AssertionError(
                        f"unexpected publish status {r.status_code}: {r.text}"
                    )
        except Exception as exc:  # noqa: BLE001 - surfaced below
            with lock:
                errors.append(exc)

    workers = [threading.Thread(target=uploader, args=(n,)) for n in range(3)]
    pub = threading.Thread(target=publisher)
    pub.start()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    stop.set()
    pub.join(timeout=30)
    assert not pub.is_alive()
    assert not errors

    readings = client.get(f"/chips/{chip}/readings").json()
    versions = client.get(f"/tracks/{track_id}/chips/{chip}/adjudications").json()
    assert len(versions) >= 2, "the race should have produced several versions"
    for v in versions:
        high = v["receive_seq_high"]
        included = (
            {r["id"] for r in v["adopted_readings"]}
            | {r["id"] for r in v["excluded_readings"]}
            | {r["id"] for r in v["voided_readings"]}
        )
        voided_ids = {r["id"] for r in v["voided_readings"]}
        for e in readings:
            if e["receive_seq"] <= high:
                assert e["id"] in included, (v["version"], e["id"])
            else:
                assert e["id"] not in included, (v["version"], e["id"])
        for rid, vseq in voids:
            if vseq <= high:
                assert rid in voided_ids, (v["version"], rid)
            else:
                assert rid not in voided_ids, (v["version"], rid)
