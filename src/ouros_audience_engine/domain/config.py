from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictBool, model_validator


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


class RuleClause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "between", "outside"]
    value: StrictBool | FiniteFloat
    upper: FiniteFloat | None = None

    @model_validator(mode="after")
    def validate_comparison(self) -> "RuleClause":
        numeric = not isinstance(self.value, bool)
        if self.operator in {"between", "outside"}:
            if not numeric or self.upper is None:
                raise ValueError("between/outside require numeric value and upper bounds")
            if self.value > self.upper:
                raise ValueError("lower bound must not exceed upper bound")
        elif self.upper is not None:
            raise ValueError("upper is only valid for between/outside")
        elif self.operator not in {"eq", "ne"} and not numeric:
            raise ValueError("ordered comparisons require a numeric value")
        return self


class RuleBranch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    all: list[RuleClause] = Field(min_length=1, max_length=32)


class TipAudienceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    version: str = Field(min_length=1, max_length=32)
    enabled: bool = False
    subject: Literal["farm"]
    member_id_space: Literal["farm_owner_user_id"]
    source: Literal["postgres"]
    metric: Literal["tip_observation_rule"]
    tip_id: int = Field(gt=0, le=2_147_483_647)
    any_of: list[RuleBranch] = Field(min_length=1, max_length=16)
    max_observation_age_days: int = Field(ge=1, le=365)
    unknown_policy: Literal["preserve_previous_membership"]
    first_run_push: Literal[False] = False

    @model_validator(mode="after")
    def validate_signal_types(self) -> "TipAudienceConfig":
        signal_types: dict[str, bool] = {}
        for branch in self.any_of:
            for clause in branch.all:
                is_boolean = isinstance(clause.value, bool)
                previous = signal_types.setdefault(clause.field, is_boolean)
                if previous != is_boolean:
                    raise ValueError(f"signal {clause.field} has conflicting value types")
        if len(signal_types) > 32:
            raise ValueError("tip audience supports at most 32 signal fields")
        return self

    @property
    def required_signals(self) -> dict[str, bool]:
        return {
            clause.field: isinstance(clause.value, bool)
            for branch in self.any_of
            for clause in branch.all
        }


class DailyWaterMeterAuditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    version: str = Field(min_length=1, max_length=32)
    enabled: bool = False
    subject: Literal["farm"]
    member_id_space: Literal["farm_owner_user_id"]
    source: Literal["postgres"]
    metric: Literal["daily_water_meter_audit"]
    tip_id: int = Field(gt=0, le=2_147_483_647)
    max_reading_age_hours: int = Field(ge=1, le=168)
    max_observation_age_days: int = Field(ge=1, le=7)
    unknown_policy: Literal["preserve_previous_membership"]
    first_run_push: Literal[False] = False



AudienceDefinition = Annotated[
    WaterAudienceConfig | TipAudienceConfig | DailyWaterMeterAuditConfig,
    Field(discriminator="metric"),
]


class AudienceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audiences: list[AudienceDefinition]

    @model_validator(mode="after")
    def unique_keys(self) -> "AudienceConfig":
        keys = [audience.key for audience in self.audiences]
        if len(keys) != len(set(keys)):
            raise ValueError("audience keys must be unique")
        return self
