from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    audience_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    snapshot_version: int = Field(ge=1)
    bitmap_format: Literal["pyroaring-portable"]
    bitmap_format_version: Literal["1"]
    member_id_space: Literal["farm_owner_user_id"]
    rule_version: str = Field(min_length=1, max_length=32)
    object_key: str = Field(min_length=1, max_length=512)
    member_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at: datetime
    source_watermark: datetime | None
