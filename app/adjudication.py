"""Pure adjudication algorithm.

Given an immutable track definition and the set of valid (non-voided) chip
readings, select the best legal passing chain starting at the gun time:

* the chain is a prefix of the expected point sequence
  ``(checkpoints + [finish]) * laps`` — the finish point of one lap is the
  start of the next lap, so the segment intervals cycle with period
  ``len(checkpoints) + 1``;
* consecutive readings (and the gun time before the first one) must be
  separated by the segment's ``[min_seconds, max_seconds]`` interval
  (inclusive bounds);
* among all legal chains pick, in order:
  1. the one with the most passed points (longest chain),
  2. the one whose last reading is earliest,
  3. the lexicographically smallest tuple of reading IDs.

The dynamic program keeps, per expected position and per candidate reading at
that position, the lexicographically smallest ID-tuple of any chain ending
exactly with that reading.  For a fixed last reading that is optimal: the
final comparison only looks at (length, last time, ID tuple), and future
extensions only depend on the last reading, never on the prefix.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence


@dataclass(frozen=True)
class Reading:
    id: str
    point: str
    time: datetime  # timezone-aware UTC


@dataclass(frozen=True)
class Segment:
    min_seconds: int
    max_seconds: int


@dataclass(frozen=True)
class ChainLink:
    reading_id: str
    point: str
    time: datetime
    position: int  # 1-based index in the expected point sequence
    lap: int  # 1-based lap number


@dataclass(frozen=True)
class AdjudicationResult:
    finished: bool
    points_passed: int
    total_points: int
    laps_completed: int
    elapsed_seconds: Optional[float]
    chain: tuple[ChainLink, ...]


def adjudicate(
    *,
    gun_time: datetime,
    checkpoints: Sequence[str],
    finish: str,
    laps: int,
    segments: Sequence[Segment],
    readings: Sequence[Reading],
) -> AdjudicationResult:
    points_per_lap = list(checkpoints) + [finish]
    points_per_lap_count = len(points_per_lap)
    total_points = points_per_lap_count * laps

    by_point: dict[str, list[Reading]] = {}
    for r in readings:
        by_point.setdefault(r.point, []).append(r)
    for rs in by_point.values():
        rs.sort(key=lambda r: (r.time, r.id))

    # Entries (last_time, id tuple) — one per reachable last reading at the
    # previous expected position.
    prev: Optional[list[tuple[datetime, tuple[str, ...]]]] = None
    best_ids: tuple[str, ...] = ()
    best_time: Optional[datetime] = None
    best_len = 0

    for position in range(1, total_points + 1):
        idx = (position - 1) % points_per_lap_count
        point = points_per_lap[idx]
        seg = segments[idx]
        lo = timedelta(seconds=seg.min_seconds)
        hi = timedelta(seconds=seg.max_seconds)

        cur: list[tuple[datetime, tuple[str, ...]]] = []
        for r in by_point.get(point, ()):
            if position == 1:
                if gun_time + lo <= r.time <= gun_time + hi:
                    cur.append((r.time, (r.id,)))
            else:
                assert prev is not None
                earliest = r.time - hi
                latest = r.time - lo
                best_prefix: Optional[tuple[str, ...]] = None
                for prev_time, prev_ids in prev:
                    if earliest <= prev_time <= latest and (
                        best_prefix is None or prev_ids < best_prefix
                    ):
                        best_prefix = prev_ids
                if best_prefix is not None:
                    cur.append((r.time, best_prefix + (r.id,)))
        if not cur:
            break
        best_time, best_ids = min(cur, key=lambda c: (c[0], c[1]))
        best_len = position
        prev = cur

    id_to_reading = {r.id: r for r in readings}
    chain = []
    for i, rid in enumerate(best_ids):
        r = id_to_reading[rid]
        position = i + 1
        chain.append(
            ChainLink(
                reading_id=r.id,
                point=r.point,
                time=r.time,
                position=position,
                lap=(position - 1) // points_per_lap_count + 1,
            )
        )

    finished = best_len == total_points
    elapsed = (
        (best_time - gun_time).total_seconds()
        if finished and best_time is not None
        else None
    )
    return AdjudicationResult(
        finished=finished,
        points_passed=best_len,
        total_points=total_points,
        laps_completed=best_len // points_per_lap_count,
        elapsed_seconds=elapsed,
        chain=tuple(chain),
    )
