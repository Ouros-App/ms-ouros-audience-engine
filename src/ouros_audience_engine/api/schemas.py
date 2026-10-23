from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
