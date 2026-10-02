import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .adjudication import Reading as ChainReading
from .adjudication import Segment, adjudicate
from .db import Base, engine, get_db
from .models import (
    CHIP_LOCK_SQL,
    RECEIVE_SEQ_DDL,
    RECEIVE_SEQ_HIGH_WATER_SQL,
    Adjudication,
    AdjudicationHead,
    Reading,
    Track,
    VoidEvent,
)
from .schemas import AdjudicationCreate, ReadingCreate, TrackCreate, VoidCreate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wait for the database to come up (compose healthcheck usually makes
    # this a no-op), then create the shared receive sequence and the schema.
    last_err: Exception | None = None
    for _ in range(60):
        try:
            with engine.begin() as conn:
                conn.execute(text(RECEIVE_SEQ_DDL))
                Base.metadata.create_all(bind=conn)
            break
        except Exception as exc:  # pragma: no cover - depends on env timing
            last_err = exc
            time.sleep(1)
    else:  # pragma: no cover
        raise RuntimeError(f"database not reachable: {last_err}")
    yield


app = FastAPI(
    title="Trail Race Chip Timing Adjudication",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# serialization helpers
# --------------------------------------------------------------------------

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def track_snapshot(track: Track) -> dict:
    return {
        "id": track.id,
        "name": track.name,
        "gun_time": iso_z(track.gun_time),
        "checkpoints": list(track.checkpoints),
        "finish": track.finish,
        "laps": track.laps,
        "segments": list(track.segments),
    }


def track_out(track: Track) -> dict:
    return {**track_snapshot(track), "created_at": iso_z(track.created_at)}


def reading_out(reading: Reading, voided: bool) -> dict:
    return {
        "id": reading.id,
        "chip_id": reading.chip_id,
        "point": reading.point,
        "time": iso_z(reading.time),
        "receive_seq": reading.receive_seq,
        "voided": voided,
        "created_at": iso_z(reading.created_at),
    }


def void_out(void: VoidEvent) -> dict:
    return {
        "id": void.id,
        "reading_id": void.reading_id,
        "reason": void.reason,
        "receive_seq": void.receive_seq,
        "created_at": iso_z(void.created_at),
    }


def adjudication_out(adj: Adjudication) -> dict:
    return {
        "id": adj.id,
        "track_id": adj.track_id,
        "chip_id": adj.chip_id,
        "version": adj.version,
        "receive_seq_high": adj.receive_seq_high,
        **adj.result,
        "track_snapshot": adj.track_snapshot,
        "created_at": iso_z(adj.created_at),
    }


def _is_voided(db: Session, reading_id: str) -> bool:
    return (
        db.scalar(
            select(VoidEvent.id).where(VoidEvent.reading_id == reading_id)
        )
        is not None
    )


def _lock_chip(db: Session, chip_id: str) -> None:
    """Serialize event ingestion and adjudication publishing per chip.

    The lock is transaction-scoped: an event insert holds it until commit,
    so once a publisher acquires it, every event of this chip whose
    receive_seq was already assigned is also committed and visible to the
    publisher's next statements; any event of this chip committed later
    necessarily claimed its receive_seq after the publisher read the
    sequence high-water mark, and therefore lands above it.
    """
    db.execute(text(CHIP_LOCK_SQL), {"chip_id": chip_id})


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return {
        "service": "trail-race-chip-timing-adjudication",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}


# --------------------------------------------------------------------------
# tracks (immutable)
# --------------------------------------------------------------------------

@app.post("/tracks", status_code=201)
def create_track(payload: TrackCreate, db: Session = Depends(get_db)):
    track = Track(
        name=payload.name,
        gun_time=payload.gun_time,
        checkpoints=payload.checkpoints,
        finish=payload.finish,
        laps=payload.laps,
        segments=[s.model_dump() for s in payload.segments],
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return track_out(track)


@app.get("/tracks")
def list_tracks(db: Session = Depends(get_db)):
    tracks = db.scalars(select(Track).order_by(Track.created_at, Track.id)).all()
    return [track_out(t) for t in tracks]


@app.get("/tracks/{track_id}")
def get_track(track_id: str, db: Session = Depends(get_db)):
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track '{track_id}' not found")
    return track_out(track)


# --------------------------------------------------------------------------
# readings (idempotent upload, conflict on divergent content)
# --------------------------------------------------------------------------

def _reading_echo_or_conflict(
    db: Session, existing: Reading, payload: ReadingCreate, response: Response
):
    same = (
        existing.chip_id == payload.chip_id
        and existing.point == payload.point
        and existing.time.astimezone(timezone.utc) == payload.time
    )
    if same:
        response.status_code = 200
        return reading_out(existing, voided=_is_voided(db, existing.id))
    return JSONResponse(
        status_code=409,
        content={
            "detail": f"reading id '{payload.id}' already exists with different content",
            "existing": reading_out(existing, voided=_is_voided(db, existing.id)),
        },
    )


@app.post("/readings")
def create_reading(
    payload: ReadingCreate, response: Response, db: Session = Depends(get_db)
):
    existing = db.get(Reading, payload.id)
    if existing is not None:
        return _reading_echo_or_conflict(db, existing, payload, response)
    # Hold the per-chip lock across the INSERT (which claims the receive
    # sequence number) until commit, so a concurrent publish of this chip
    # either sees this reading committed or fixes its high-water mark
    # below this reading's receive_seq.
    _lock_chip(db, payload.chip_id)
    reading = Reading(
        id=payload.id, chip_id=payload.chip_id, point=payload.point, time=payload.time
    )
    db.add(reading)
    try:
        db.commit()
    except IntegrityError:
        # lost a race against a concurrent insert of the same id
        db.rollback()
        existing = db.get(Reading, payload.id)
        return _reading_echo_or_conflict(db, existing, payload, response)
    db.refresh(reading)
    response.status_code = 201
    return reading_out(reading, voided=False)


@app.get("/readings/{reading_id}")
def get_reading(reading_id: str, db: Session = Depends(get_db)):
    reading = db.get(Reading, reading_id)
    if reading is None:
        raise HTTPException(
            status_code=404, detail=f"reading '{reading_id}' not found"
        )
    return reading_out(reading, voided=_is_voided(db, reading_id))


@app.get("/chips/{chip_id}/readings")
def list_chip_readings(chip_id: str, db: Session = Depends(get_db)):
    readings = db.scalars(
        select(Reading)
        .where(Reading.chip_id == chip_id)
        .order_by(Reading.receive_seq)
    ).all()
    return [reading_out(r, voided=_is_voided(db, r.id)) for r in readings]


# --------------------------------------------------------------------------
# void events (append-only)
# --------------------------------------------------------------------------

@app.post("/voids")
def create_void(
    payload: VoidCreate, response: Response, db: Session = Depends(get_db)
):
    reading = db.get(Reading, payload.reading_id)
    if reading is None:
        raise HTTPException(
            status_code=404, detail=f"reading '{payload.reading_id}' not found"
        )
    existing = db.scalar(
        select(VoidEvent).where(VoidEvent.reading_id == payload.reading_id)
    )
    if existing is not None:
        response.status_code = 200
        return void_out(existing)
    # Same per-chip boundary discipline as readings: lock before the
    # INSERT claims its receive_seq, hold until commit.
    _lock_chip(db, reading.chip_id)
    void = VoidEvent(reading_id=payload.reading_id, reason=payload.reason)
    db.add(void)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(VoidEvent).where(VoidEvent.reading_id == payload.reading_id)
        )
        response.status_code = 200
        return void_out(existing)
    db.refresh(void)
    response.status_code = 201
    return void_out(void)


# --------------------------------------------------------------------------
# adjudications (immutable, optimistic-concurrency versioned)
# --------------------------------------------------------------------------

@app.post("/tracks/{track_id}/chips/{chip_id}/adjudications", status_code=201)
def publish_adjudication(
    track_id: str,
    chip_id: str,
    payload: AdjudicationCreate,
    db: Session = Depends(get_db),
):
    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(status_code=404, detail=f"track '{track_id}' not found")

    # Ensure the head row exists, then lock it: publishes for the same
    # (track, chip) are serialized, and the version check + insert is atomic.
    db.execute(
        pg_insert(AdjudicationHead)
        .values(track_id=track_id, chip_id=chip_id, current_version=0)
        .on_conflict_do_nothing()
    )
    head = db.execute(
        select(AdjudicationHead)
        .where(
            AdjudicationHead.track_id == track_id,
            AdjudicationHead.chip_id == chip_id,
        )
        .with_for_update()
    ).scalar_one()

    if head.current_version != payload.expected_version:
        current_version = head.current_version
        db.rollback()
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"version conflict: expected_version={payload.expected_version} "
                    f"but current_version={current_version}"
                ),
                "current_version": current_version,
            },
        )

    # Atomically fix the receive-sequence high-water mark.  Taking the
    # per-chip advisory lock first makes the boundary consistent: every
    # reading/void event of this chip with receive_seq <= high was
    # committed before this point (its transaction held this lock until
    # commit), so it is visible to the queries below and joins this
    # version's input set; every event of this chip committed afterwards
    # claims its receive_seq only after we read the sequence, so it lands
    # above high and can only affect future versions.  The track snapshot,
    # the high-water mark and the adjudication inputs therefore describe
    # one and the same atomic boundary.
    _lock_chip(db, chip_id)
    row = db.execute(text(RECEIVE_SEQ_HIGH_WATER_SQL)).one()
    high = int(row[0]) if row[1] else 0

    readings = db.scalars(
        select(Reading)
        .where(Reading.chip_id == chip_id, Reading.receive_seq <= high)
        .order_by(Reading.receive_seq)
    ).all()
    voids = db.scalars(
        select(VoidEvent)
        .join(Reading, Reading.id == VoidEvent.reading_id)
        .where(Reading.chip_id == chip_id, VoidEvent.receive_seq <= high)
    ).all()
    voided_by_id = {v.reading_id: v for v in voids}
    valid = [r for r in readings if r.id not in voided_by_id]

    result = adjudicate(
        gun_time=track.gun_time,
        checkpoints=list(track.checkpoints),
        finish=track.finish,
        laps=track.laps,
        segments=[Segment(**s) for s in track.segments],
        readings=[ChainReading(id=r.id, point=r.point, time=r.time) for r in valid],
    )
    adopted_ids = {link.reading_id for link in result.chain}

    result_json = {
        "status": "finished" if result.finished else "dnf",
        "laps_completed": result.laps_completed,
        "points_passed": result.points_passed,
        "total_points": result.total_points,
        "elapsed_seconds": result.elapsed_seconds,
        "adopted_readings": [
            {
                "id": link.reading_id,
                "point": link.point,
                "time": iso_z(link.time),
                "position": link.position,
                "lap": link.lap,
            }
            for link in result.chain
        ],
        "excluded_readings": [
            {"id": r.id, "point": r.point, "time": iso_z(r.time)}
            for r in valid
            if r.id not in adopted_ids
        ],
        "voided_readings": [
            {
                "id": r.id,
                "point": r.point,
                "time": iso_z(r.time),
                "void_event_id": voided_by_id[r.id].id,
                "reason": voided_by_id[r.id].reason,
            }
            for r in readings
            if r.id in voided_by_id
        ],
    }

    adj = Adjudication(
        track_id=track_id,
        chip_id=chip_id,
        version=payload.expected_version + 1,
        receive_seq_high=high,
        track_snapshot=track_snapshot(track),
        result=result_json,
    )
    db.add(adj)
    head.current_version = adj.version
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return JSONResponse(
            status_code=409,
            content={"detail": "concurrent adjudication publish, please retry"},
        )
    db.refresh(adj)
    return adjudication_out(adj)


@app.get("/tracks/{track_id}/chips/{chip_id}/adjudications")
def list_adjudications(track_id: str, chip_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Adjudication)
        .where(Adjudication.track_id == track_id, Adjudication.chip_id == chip_id)
        .order_by(Adjudication.version)
    ).all()
    return [adjudication_out(r) for r in rows]


@app.get("/tracks/{track_id}/chips/{chip_id}/adjudications/{version}")
def get_adjudication(
    track_id: str, chip_id: str, version: int, db: Session = Depends(get_db)
):
    row = db.scalar(
        select(Adjudication).where(
            Adjudication.track_id == track_id,
            Adjudication.chip_id == chip_id,
            Adjudication.version == version,
        )
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"adjudication version {version} not found for chip '{chip_id}'",
        )
    return adjudication_out(row)
