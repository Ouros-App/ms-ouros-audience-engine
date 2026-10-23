from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class Evaluation(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MeasurementPeriod:
    farm_id: int
    period_start: date
    consumed: Decimal
    source_watermark: datetime


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    evaluation: Evaluation
    source_watermark: datetime | None
    percentage_above_baseline: Decimal | None = None
    reason: str | None = None
