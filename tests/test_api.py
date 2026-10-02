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
        # Fail fast instead of hanging if a test ever leaks an open txn.
        conn.execute(text("SET lock_timeout = '5s'"))
        conn.execute(text("SET statement_timeout = '15s'"))
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


# ----------------------------------------------------- concurrency boundary

# The receive sequence comes from a non-transactional PostgreSQL sequence: an
# in-flight POST /readings or POST /voids has claimed its receive_seq before
# commit.  A concurrent publish must not observe the claimed value as its
# high-water mark while the row is still invisible -- otherwise the event is
# trapped below a published high water it never participated in.
#
# These tests drive the same building blocks the HTTP handlers use
# (lock_chip_events + the shared sequence + sessions) from two threads: an
# event transaction stays open holding the chip lock while the publish thread
# runs.

from datetime import datetime as _dt, timezone as _tz  # noqa: E402

from sqlalchemy import select as _select, text as _text  # noqa: E402

from app.db import SessionLocal as _SessionLocal  # noqa: E402
from app import main as _main  # noqa: E402
from app.main import chip_lock_key  # noqa: E402
from app.models import Reading as _Reading, VoidEvent as _VoidEvent  # noqa: E402


@pytest.fixture(autouse=True)
def _fault_injection_disable_chip_locks(monkeypatch):
    """With DISABLE_CHIP_LOCKS=1 the per-chip advisory lock becomes a no-op,
    reproducing the pre-fix racy behavior so the boundary tests below prove
    they actually fail without the serialization."""
    if os.environ.get("DISABLE_CHIP_LOCKS"):
        monkeypatch.setattr(_main, "lock_chip_events", lambda db, chip_id: None)

RECEIVE_SEQ_CURRVAL = _text("SELECT currval('receive_seq_seq')")
RECEIVE_SEQ_HIGH_WATER = _text("SELECT last_value, is_called FROM receive_seq_seq")


def _open_inflight_event(kind: str, chip: str, reading_id: str):
    """Lock the chip and insert an event, but leave the transaction open.

    The inserted row has already claimed its receive_seq (flush) while still
    being invisible to other transactions.  The lock call goes through the
    app module on purpose, so the DISABLE_CHIP_LOCKS fault injection turns
    this into the exact pre-fix racy sequence.
    """
    db = _SessionLocal()
    _main.lock_chip_events(db, chip)
    if kind == "reading":
        db.add(
            _Reading(
                id=reading_id,
                chip_id=chip,
                point="CP1",
                time=_dt(2026, 10, 1, 8, 10, 0, tzinfo=_tz.utc),
            )
        )
    else:
        db.add(_VoidEvent(reading_id=reading_id, reason="in-flight void"))
    db.flush()
    return db


def _publish_boundary(chip: str, out: list):
    """Same lock -> high-water -> committed input-set sequence as the handler."""
    db = _SessionLocal()
    try:
        _main.lock_chip_events(db, chip)
        row = db.execute(RECEIVE_SEQ_HIGH_WATER).one()
        high = int(row[0]) if row[1] else 0
        visible = db.scalars(
            _select(_Reading).where(
                _Reading.chip_id == chip, _Reading.receive_seq <= high
            )
        ).all()
        out.append((high, [r.id for r in visible]))
    finally:
        db.close()


def test_publish_waits_for_inflight_reading_then_includes_it(client, track_id):
    import threading

    inflight = _open_inflight_event("reading", "CHIP-RACE", "rd-race-1")
    inflight_seq = inflight.scalar(RECEIVE_SEQ_CURRVAL)

    out: list = []
    t = threading.Thread(target=_publish_boundary, args=("CHIP-RACE", out))
    try:
        t.start()
        t.join(timeout=3)
        assert t.is_alive(), (
            "publish must block while a same-chip event txn is in flight, "
            "but it observed the high-water mark immediately"
        )

        inflight.commit()
        inflight = None  # type: ignore[assignment]
        t.join(timeout=5)
        assert not t.is_alive(), "publish stayed blocked after the event committed"

        high, visible_ids = out[0]
        assert inflight_seq <= high
        assert visible_ids == ["rd-race-1"]
    finally:
        if inflight is not None:
            inflight.rollback()
            inflight.close()
        t.join(timeout=5)


def test_event_committing_after_boundary_keeps_seq_above_high(client, track_id):
    import threading

    # Publish holds the boundary first; a same-chip event insert must wait
    # and then land strictly above the published high-water mark.
    started = threading.Event()
    release = threading.Event()
    high_out: list = []

    def publish():
        db = _SessionLocal()
        try:
            _main.lock_chip_events(db, "CHIP-RACE2")
            row = db.execute(RECEIVE_SEQ_HIGH_WATER).one()
            high_out.append(int(row[0]) if row[1] else 0)
            started.set()
            release.wait(timeout=10)
            db.commit()
        finally:
            db.close()

    t = threading.Thread(target=publish)
    late_done = threading.Event()

    def late_event_commit():
        db = _SessionLocal()
        try:
            _main.lock_chip_events(db, "CHIP-RACE2")
            db.add(
                _Reading(
                    id="rd-race-2",
                    chip_id="CHIP-RACE2",
                    point="CP1",
                    time=_dt(2026, 10, 1, 8, 10, 0, tzinfo=_tz.utc),
                )
            )
            db.commit()
        finally:
            db.close()
            late_done.set()

    te = threading.Thread(target=late_event_commit)
    try:
        t.start()
        assert started.wait(timeout=5)
        te.start()
        # The insert cannot even proceed while the publish boundary is open.
        assert not late_done.wait(timeout=1)

        release.set()
        t.join(timeout=5)
        assert not t.is_alive()
        te.join(timeout=5)
        assert late_done.is_set()

        db = _SessionLocal()
        try:
            seq = db.scalar(
                _select(_Reading.receive_seq).where(_Reading.id == "rd-race-2")
            )
        finally:
            db.close()
        assert seq > high_out[0]
    finally:
        release.set()
        t.join(timeout=10)
        te.join(timeout=10)


def test_publish_waits_for_inflight_void_then_excludes_reading(client, track_id):
    import threading

    resp = client.post(
        "/readings",
        json=_reading("rd-void-race", chip="CHIP-RACE3", point="CP1"),
    )
    assert resp.status_code == 201
    reading_seq = resp.json()["receive_seq"]

    inflight = _open_inflight_event("void", "CHIP-RACE3", "rd-void-race")
    inflight_void_seq = inflight.scalar(RECEIVE_SEQ_CURRVAL)

    holder: dict = {}

    def publish():
        holder["resp"] = client.post(
            f"/tracks/{track_id}/chips/CHIP-RACE3/adjudications",
            json={"expected_version": 0},
        )

    t = threading.Thread(target=publish)
    try:
        t.start()
        t.join(timeout=3)
        assert t.is_alive(), "publish must block while the void txn is in flight"

        inflight.commit()
        inflight = None  # type: ignore[assignment]
        t.join(timeout=10)
        assert not t.is_alive(), "publish stayed blocked after the void committed"

        body = holder["resp"]
        assert body.status_code == 201, body.text
        payload = body.json()
        assert payload["receive_seq_high"] >= inflight_void_seq > reading_seq
        assert [r["id"] for r in payload["voided_readings"]] == ["rd-void-race"]
        assert payload["adopted_readings"] == []
    finally:
        if inflight is not None:
            inflight.rollback()
            inflight.close()
        t.join(timeout=10)


def test_unrelated_chip_lock_is_not_blocked(client, track_id):
    import threading

    # A held lock on CHIP-A must not block locking CHIP-B.
    held = threading.Event()
    release = threading.Event()

    def hold_chip_a():
        db = _SessionLocal()
        try:
            _main.lock_chip_events(db, "CHIP-A")
            held.set()
            release.wait(timeout=5)
            db.commit()
        finally:
            db.close()

    t = threading.Thread(target=hold_chip_a)
    t.start()
    assert held.wait(timeout=5)

    db = _SessionLocal()
    try:
        conn = db.connection()
        conn.execute(_text("SELECT pg_advisory_xact_lock(CAST(:k AS BIGINT))"), {"k": chip_lock_key("CHIP-B")})
        got = threading.Event()
        got.set()
        assert got.wait(timeout=2), "CHIP-B lock was blocked by CHIP-A"
        db.rollback()
    finally:
        db.close()
        release.set()
        t.join(timeout=5)
