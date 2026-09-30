"""Unit tests for the pure adjudication algorithm (no database needed)."""
from datetime import datetime, timedelta, timezone

from app.adjudication import Reading, Segment, adjudicate

GUN = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)


def at(**kwargs) -> datetime:
    return GUN + timedelta(**kwargs)


def r(rid: str, point: str, **kwargs) -> Reading:
    return Reading(id=rid, point=point, time=at(**kwargs))


# Default track: CP1 -> CP2 -> FIN, 2 laps, every segment 300..900 seconds.
DEFAULT_KWARGS = dict(
    gun_time=GUN,
    checkpoints=["CP1", "CP2"],
    finish="FIN",
    laps=2,
    segments=[Segment(300, 900)] * 3,
)

FULL_CHAIN_READINGS = [
    r("rd-01", "CP1", seconds=600),
    r("rd-02", "CP2", seconds=1200),
    r("rd-03", "FIN", seconds=1800),
    r("rd-04", "CP1", seconds=2400),
    r("rd-05", "CP2", seconds=3000),
    r("rd-06", "FIN", seconds=3600),
]


def test_full_finish_chain():
    res = adjudicate(readings=FULL_CHAIN_READINGS, **DEFAULT_KWARGS)
    assert res.finished is True
    assert res.points_passed == 6
    assert res.total_points == 6
    assert res.laps_completed == 2
    assert res.elapsed_seconds == 3600.0
    assert [link.reading_id for link in res.chain] == [
        "rd-01", "rd-02", "rd-03", "rd-04", "rd-05", "rd-06",
    ]
    assert [link.lap for link in res.chain] == [1, 1, 1, 2, 2, 2]


def test_input_order_is_irrelevant():
    shuffled = list(reversed(FULL_CHAIN_READINGS))
    res = adjudicate(readings=shuffled, **DEFAULT_KWARGS)
    assert [link.reading_id for link in res.chain] == [
        "rd-01", "rd-02", "rd-03", "rd-04", "rd-05", "rd-06",
    ]


def test_dnf_when_chain_breaks():
    # No usable FIN reading for lap 1 -> chain stops after CP2 of lap 1.
    readings = [x for x in FULL_CHAIN_READINGS if x.id not in {"rd-03", "rd-06"}]
    res = adjudicate(readings=readings, **DEFAULT_KWARGS)
    assert res.finished is False
    assert res.points_passed == 2
    assert res.laps_completed == 0
    assert res.elapsed_seconds is None
    assert [link.reading_id for link in res.chain] == ["rd-01", "rd-02"]


def test_segment_window_enforced_too_fast():
    # CP1 only 60s after the gun (< min 300) -> no legal chain at all.
    res = adjudicate(readings=[r("rd-x", "CP1", seconds=60)], **DEFAULT_KWARGS)
    assert res.points_passed == 0
    assert res.finished is False


def test_segment_window_enforced_too_slow():
    # CP1 1000s after the gun (> max 900) -> not usable for position 1.
    res = adjudicate(readings=[r("rd-x", "CP1", seconds=1000)], **DEFAULT_KWARGS)
    assert res.points_passed == 0


def test_segment_window_bounds_are_inclusive():
    readings = [
        r("a", "CP1", seconds=300),   # exactly min
        r("b", "CP2", seconds=1200),  # exactly +max
        r("c", "FIN", seconds=1500),  # exactly +min
        r("d", "CP1", seconds=2400),  # exactly +max (finish -> next lap start)
        r("e", "CP2", seconds=2700),
        r("f", "FIN", seconds=3600),  # exactly +max
    ]
    res = adjudicate(readings=readings, **DEFAULT_KWARGS)
    assert res.finished is True
    assert [link.reading_id for link in res.chain] == ["a", "b", "c", "d", "e", "f"]


def test_tie_break_prefers_earliest_last_time():
    kwargs = dict(
        gun_time=GUN,
        checkpoints=["CP1"],
        finish="FIN",
        laps=1,
        segments=[Segment(60, 120), Segment(60, 120)],
    )
    readings = [
        r("cp", "CP1", seconds=100),
        r("fin-early", "FIN", seconds=180),
        r("fin-late", "FIN", seconds=200),
    ]
    res = adjudicate(readings=readings, **kwargs)
    assert res.finished is True
    assert [link.reading_id for link in res.chain] == ["cp", "fin-early"]


def test_tie_break_prefers_lexicographically_smaller_id_chain():
    kwargs = dict(
        gun_time=GUN,
        checkpoints=["CP1"],
        finish="FIN",
        laps=1,
        segments=[Segment(60, 120), Segment(60, 120)],
    )
    readings = [
        r("cp", "CP1", seconds=100),
        r("b-fin", "FIN", seconds=180),
        r("a-fin", "FIN", seconds=180),  # same time, smaller id
    ]
    res = adjudicate(readings=readings, **kwargs)
    assert [link.reading_id for link in res.chain] == ["cp", "a-fin"]


def test_tie_break_compares_whole_chain_not_single_reading():
    # Both candidate chains end with the same reading; the prefix decides.
    readings = FULL_CHAIN_READINGS + [
        r("rd-07", "CP2", seconds=1260),  # legal alternative for position 2
    ]
    res = adjudicate(readings=readings, **DEFAULT_KWARGS)
    assert res.finished is True
    # ("rd-01","rd-02",...) < ("rd-01","rd-07",...) so rd-02 wins.
    assert [link.reading_id for link in res.chain] == [
        "rd-01", "rd-02", "rd-03", "rd-04", "rd-05", "rd-06",
    ]


def test_longer_chain_beats_earlier_prefix():
    # "early" at CP1 cannot reach the only FIN reading (65 > max 60),
    # "late" can (55 within [40, 60]).  The longer chain must win even
    # though its first reading is later.
    kwargs = dict(
        gun_time=GUN,
        checkpoints=["CP1"],
        finish="FIN",
        laps=1,
        segments=[Segment(40, 60), Segment(40, 60)],
    )
    readings = [
        r("early", "CP1", seconds=45),
        r("late", "CP1", seconds=55),
        r("fin", "FIN", seconds=110),
    ]
    res = adjudicate(readings=readings, **kwargs)
    assert res.finished is True
    assert [link.reading_id for link in res.chain] == ["late", "fin"]


def test_unknown_points_are_never_adopted():
    readings = FULL_CHAIN_READINGS + [r("zz-9", "CP9", seconds=650)]
    res = adjudicate(readings=readings, **DEFAULT_KWARGS)
    assert [link.reading_id for link in res.chain] == [
        "rd-01", "rd-02", "rd-03", "rd-04", "rd-05", "rd-06",
    ]


def test_chain_is_capped_at_configured_laps():
    # One-lap track, but the chip produced two full laps of readings.
    kwargs = dict(
        gun_time=GUN,
        checkpoints=["CP1"],
        finish="FIN",
        laps=1,
        segments=[Segment(60, 600), Segment(60, 600)],
    )
    readings = [
        r("a", "CP1", seconds=300),
        r("b", "FIN", seconds=600),
        r("c", "CP1", seconds=900),
        r("d", "FIN", seconds=1200),
    ]
    res = adjudicate(readings=readings, **kwargs)
    assert res.finished is True
    assert res.points_passed == 2
    assert [link.reading_id for link in res.chain] == ["a", "b"]


def test_finish_point_is_start_of_next_lap():
    # The FIN->CP1 gap must satisfy segment[0] (start->first checkpoint).
    readings = [
        r("a", "CP1", seconds=600),
        r("b", "CP2", seconds=1200),
        r("c", "FIN", seconds=1800),
        r("d", "CP1", seconds=1800 + 901),  # 901s after FIN: outside [300, 900]
        r("e", "CP2", seconds=1800 + 901 + 600),
        r("f", "FIN", seconds=1800 + 901 + 1200),
    ]
    res = adjudicate(readings=readings, **DEFAULT_KWARGS)
    assert res.finished is False
    assert res.points_passed == 3  # lap 1 complete, lap 2 chain broken


def test_no_readings():
    res = adjudicate(readings=[], **DEFAULT_KWARGS)
    assert res.finished is False
    assert res.points_passed == 0
    assert res.chain == ()
