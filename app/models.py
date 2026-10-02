import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

# One shared, monotonically increasing "receive sequence" for every incoming
# event (readings and void events).  The sequence itself is created at
# startup (CREATE SEQUENCE IF NOT EXISTS ...) so it survives create_all and
# is shared by both event tables.
RECEIVE_SEQ_DEFAULT = text("nextval('receive_seq_seq')")
RECEIVE_SEQ_DDL = "CREATE SEQUENCE IF NOT EXISTS receive_seq_seq START WITH 1 CACHE 1"
RECEIVE_SEQ_HIGH_WATER_SQL = "SELECT last_value, is_called FROM receive_seq_seq"

# Per-chip advisory lock (transaction-scoped) serializing event ingestion
# with adjudication publishes.  Sequence values are non-transactional: a
# reading/void INSERT claims its receive_seq at nextval() time but only
# becomes visible at COMMIT, so without this lock a concurrent publish
# could read a high-water mark that already covers a still-uncommitted
# event, and that event would later commit *inside* the published
# high-water without ever having participated in the adjudication.
# Taking this lock before the INSERT (and before fixing the high-water
# mark at publish time) makes lock order == receive_seq order == commit
# order per chip, so the published boundary is atomic.
CHIP_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(:chip_id, 0))"


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Track(Base):
    """Immutable race track.  There is deliberately no update/delete path."""

    __tablename__ = "tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    gun_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    checkpoints: Mapped[list] = mapped_column(JSON, nullable=False)
    finish: Mapped[str] = mapped_column(String(200), nullable=False)
    laps: Mapped[int] = mapped_column(Integer, nullable=False)
    segments: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Reading(Base):
    """A chip reading.  The client-supplied id is globally unique."""

    __tablename__ = "readings"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    chip_id: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    point: Mapped[str] = mapped_column(String(200), nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    receive_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=RECEIVE_SEQ_DEFAULT, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class VoidEvent(Base):
    """Append-only void event marking a previously uploaded reading as a
    misread.  A reading can be voided at most once; re-voiding is idempotent."""

    __tablename__ = "void_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reading_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("readings.id"), unique=True, nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    receive_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=RECEIVE_SEQ_DEFAULT, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class AdjudicationHead(Base):
    """Concurrency control row: one per (track, chip).  Locked FOR UPDATE
    during publish so version assignment is atomic."""

    __tablename__ = "adjudication_heads"

    track_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tracks.id"), primary_key=True
    )
    chip_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Adjudication(Base):
    """One immutable published adjudication version."""

    __tablename__ = "adjudications"
    __table_args__ = (
        UniqueConstraint("track_id", "chip_id", "version", name="uq_adjudication_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    track_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tracks.id"), nullable=False, index=True
    )
    chip_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    receive_seq_high: Mapped[int] = mapped_column(BigInteger, nullable=False)
    track_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
