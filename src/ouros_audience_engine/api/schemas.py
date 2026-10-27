from datetime import datetime
from math import isfinite
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
)

from ouros_audience_engine.adapters.bitmap_codec import MAX_FARM_OWNER_ID


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0, le=MAX_FARM_OWNER_ID)
    user_role: Literal["FARM_OWNER"]
    request_id: UUID


class ResolveResponse(BaseModel):
    user_id: int
    tip_ids: list[int]
    evaluated_at: datetime
    snapshot_info: dict[str, int]
    source_status: Literal["CURRENT", "STALE"]


class UserAudienceEntry(BaseModel):
    key: str
    version: str
    tip_ids: list[int]
    snapshot_version: int


class UserAudiencesResponse(BaseModel):
    user_id: int
    audiences: list[UserAudienceEntry]
    source_status: Literal["CURRENT", "STALE"]


class TipObservationWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    signals: dict[str, StrictBool | StrictInt | StrictFloat] = Field(min_length=1, max_length=32)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value

    @field_validator("signals")
    @classmethod
    def require_finite_numbers(
        cls, values: dict[str, StrictBool | StrictInt | StrictFloat]
    ) -> dict[str, StrictBool | StrictInt | StrictFloat]:
        if any(isinstance(value, float) and not isfinite(value) for value in values.values()):
            raise ValueError("signal numbers must be finite")
        return values


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
