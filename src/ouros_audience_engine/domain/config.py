from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class WaterAudienceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    version: str = Field(min_length=1, max_length=32)
    enabled: bool = False
    subject: Literal["farm"]
    member_id_space: Literal["farm_owner_user_id"]
    source: Literal["postgres"]
    metric: Literal["normalized_water_consumption"]
    period: Literal["last_complete_month"]
    baseline: Literal["previous_consecutive_months"]
    activation_pct: float = Field(gt=0, le=10000)
    resolution_pct: float = Field(ge=0, le=10000)
    min_baseline_samples: int = Field(ge=1, le=24)
    max_observation_age_days: int = Field(ge=1, le=365)
    unknown_policy: Literal["preserve_previous_membership"]
    first_run_push: Literal[False] = False

    @model_validator(mode="after")
    def validate_hysteresis(self) -> "WaterAudienceConfig":
        if self.activation_pct <= self.resolution_pct:
            raise ValueError("activation_pct must be greater than resolution_pct")
        return self


class AudienceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audiences: list[WaterAudienceConfig]

    @model_validator(mode="after")
    def unique_keys(self) -> "AudienceConfig":
        keys = [audience.key for audience in self.audiences]
        if len(keys) != len(set(keys)):
            raise ValueError("audience keys must be unique")
        return self


def load_audience_config(path: str | Path) -> AudienceConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "audiences" not in raw:
        raise ValueError("audience configuration must contain an audiences list")
    return AudienceConfig.model_validate(raw)
