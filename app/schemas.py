from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator


def _aware_utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        raise ValueError("must be a timezone-aware UTC datetime (e.g. 2026-10-01T08:00:00Z)")
    return v.astimezone(timezone.utc)


class SegmentSchema(BaseModel):
    min_seconds: int = Field(gt=0)
    max_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must be <= max_seconds")
        return self


class TrackCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gun_time: datetime
    checkpoints: list[str] = Field(min_length=1)
    finish: str = Field(min_length=1, max_length=200)
    laps: int = Field(ge=1)
    segments: list[SegmentSchema]

    @field_validator("gun_time")
    @classmethod
    def gun_time_must_be_aware(cls, v: datetime) -> datetime:
        return _aware_utc(v)

    @field_validator("checkpoints")
    @classmethod
    def checkpoints_must_be_valid(cls, v: list[str]) -> list[str]:
        for c in v:
            if not c or not c.strip():
                raise ValueError("checkpoint names must be non-empty")
            if len(c) > 200:
                raise ValueError("checkpoint names must be at most 200 characters")
        if len(set(v)) != len(v):
            raise ValueError("checkpoint names must be unique")
        return v

    @model_validator(mode="after")
    def check_track(self):
        if self.finish in set(self.checkpoints):
            raise ValueError("finish point must be different from every checkpoint")
        expected = len(self.checkpoints) + 1
        if len(self.segments) != expected:
            raise ValueError(
                f"segments must contain exactly len(checkpoints)+1 = {expected} "
                "intervals: start->first checkpoint, one per adjacent pair, "
                "and last checkpoint->finish"
            )
        return self


class ReadingCreate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    chip_id: str = Field(min_length=1, max_length=200)
    point: str = Field(min_length=1, max_length=200)
    time: datetime

    @field_validator("time")
    @classmethod
    def time_must_be_aware(cls, v: datetime) -> datetime:
        return _aware_utc(v)


class VoidCreate(BaseModel):
    reading_id: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class AdjudicationCreate(BaseModel):
    expected_version: int = Field(ge=0)
