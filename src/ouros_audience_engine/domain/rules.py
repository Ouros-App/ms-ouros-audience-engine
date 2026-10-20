from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ouros_audience_engine.domain.audience import Evaluation, MeasurementPeriod, RuleOutcome


def _month_key(period: MeasurementPeriod) -> tuple[int, int]:
    return (period.period_start.year, period.period_start.month)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


class WaterConsumptionRule:
    """Evaluates monthly meter deltas; unknown data never implies resolution."""

    def __init__(
        self,
        activation_pct: Decimal = Decimal("25"),
        resolution_pct: Decimal = Decimal("15"),
        min_baseline_samples: int = 3,
        max_observation_age_days: int = 45,
    ) -> None:
        if activation_pct <= resolution_pct:
            raise ValueError("activation_pct must exceed resolution_pct")
        if min_baseline_samples < 1 or max_observation_age_days < 1:
            raise ValueError("sample count and freshness age must be positive")
        self.activation_pct = activation_pct
        self.resolution_pct = resolution_pct
        self.min_baseline_samples = min_baseline_samples
        self.max_observation_age_days = max_observation_age_days

    def evaluate(
        self,
        periods: list[MeasurementPeriod],
        *,
        was_member: bool,
        previous_watermark: datetime | None,
        evaluated_at: datetime,
    ) -> RuleOutcome:
        if evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if not periods:
            return RuleOutcome(Evaluation.UNKNOWN, None, reason="NO_MEASUREMENTS")

        ordered = sorted(periods, key=lambda item: item.period_start)
        latest = ordered[-1]
        watermark = latest.source_watermark
        if watermark.tzinfo is None:
            return RuleOutcome(Evaluation.UNKNOWN, None, reason="INVALID_WATERMARK")
        if latest.consumed < 0 or any(period.consumed < 0 for period in ordered):
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="NEGATIVE_CONSUMPTION")
        if evaluated_at.astimezone(UTC) - watermark.astimezone(UTC) > timedelta(
            days=self.max_observation_age_days
        ):
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="STALE_OBSERVATION")
        if previous_watermark is not None:
            if previous_watermark.tzinfo is None:
                return RuleOutcome(
                    Evaluation.UNKNOWN, watermark, reason="INVALID_PREVIOUS_WATERMARK"
                )
            if watermark.astimezone(UTC) <= previous_watermark.astimezone(UTC):
                return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="NO_NEW_MEASUREMENT")

        latest_key = _month_key(latest)
        baseline = ordered[:-1]
        if len(baseline) < self.min_baseline_samples:
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="INSUFFICIENT_BASELINE")

        expected_key = latest_key
        selected: list[MeasurementPeriod] = []
        for period in reversed(baseline):
            expected_key = _previous_month_key(expected_key)
            if _month_key(period) != expected_key:
                break
            selected.append(period)
            if len(selected) == self.min_baseline_samples:
                break
        if len(selected) != self.min_baseline_samples:
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="NONCONSECUTIVE_BASELINE")

        baseline_value = _median([period.consumed for period in selected])
        if baseline_value <= 0:
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="ZERO_BASELINE")

        increase = (latest.consumed - baseline_value) * Decimal(100) / baseline_value
        if was_member:
            result = Evaluation.ACTIVE if increase >= self.resolution_pct else Evaluation.RESOLVED
        else:
            result = Evaluation.ACTIVE if increase >= self.activation_pct else Evaluation.RESOLVED
        return RuleOutcome(result, watermark, increase)


def _previous_month_key(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    return (year - 1, 12) if month == 1 else (year, month - 1)
