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
    previous_version: int = Field(ge=0)
    bootstrap: bool
    transitions: dict[str, str]
    transition_sha256: dict[str, str]


class TransitionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    audience_key: str
    snapshot_version: int = Field(ge=1)
    previous_version: int = Field(ge=0)
    added_key: str
    resolved_key: str
    access_revoked_key: str
    added_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_revoked_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_object_key: str | None
    previous_sha256: str | None
    member_id_space: Literal["farm_owner_user_id"]
    rule_version: str
    evaluated_at: datetime
    source_watermark: datetime | None
