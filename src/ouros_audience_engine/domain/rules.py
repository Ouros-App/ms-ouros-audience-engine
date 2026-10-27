from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from ouros_audience_engine.domain.audience import Evaluation, MeasurementPeriod, RuleOutcome
from ouros_audience_engine.domain.config import RuleClause, TipAudienceConfig


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
        latest_key = _month_key(latest)
        evaluated_utc = evaluated_at.astimezone(UTC)
        if latest_key != _previous_month_key((evaluated_utc.year, evaluated_utc.month)):
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="MISSING_LATEST_PERIOD")

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


class TipObservationRule:
    """Evaluates a tip's OR-of-AND conditions against one fresh farm observation."""

    def __init__(self, config: TipAudienceConfig) -> None:
        self.config = config

    def evaluate(
        self,
        signals: Mapping[str, object],
        *,
        observed_at: datetime,
        evaluated_at: datetime,
    ) -> RuleOutcome:
        if observed_at.tzinfo is None or evaluated_at.tzinfo is None:
            raise ValueError("observation and evaluation timestamps must be timezone-aware")
        watermark = observed_at.astimezone(UTC)
        evaluated_utc = evaluated_at.astimezone(UTC)
        if watermark > evaluated_utc:
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="FUTURE_OBSERVATION")
        if evaluated_utc - watermark > timedelta(days=self.config.max_observation_age_days):
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="STALE_OBSERVATION")

        has_unknown = False
        for branch in self.config.any_of:
            branch_values = [_evaluate_clause(clause, signals) for clause in branch.all]
            if all(value is True for value in branch_values):
                return RuleOutcome(Evaluation.ACTIVE, watermark, reason="RULE_MATCHED")
            if not any(value is False for value in branch_values):
                has_unknown = True
        if has_unknown:
            return RuleOutcome(Evaluation.UNKNOWN, watermark, reason="MISSING_OR_INVALID_SIGNAL")
        return RuleOutcome(Evaluation.RESOLVED, watermark, reason="NO_RULE_MATCH")


def _evaluate_clause(
    clause: RuleClause,
    signals: Mapping[str, object],
) -> bool | None:
    if clause.field not in signals:
        return None
    actual = signals[clause.field]
    expected = clause.value
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            return None
        left: Decimal | bool = actual
        right: Decimal | bool = expected
    else:
        if isinstance(actual, bool) or not isinstance(actual, Decimal | int | float):
            return None
        try:
            left = Decimal(str(actual))
            right = Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return None
        if not left.is_finite():
            return None

    if clause.operator == "eq":
        return left == right
    if clause.operator == "ne":
        return left != right
    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        return None
    if clause.operator == "lt":
        return left < right
    if clause.operator == "lte":
        return left <= right
    if clause.operator == "gt":
        return left > right
    if clause.operator == "gte":
        return left >= right
    if clause.operator in {"between", "outside"}:
        if clause.upper is None:
            return None
        upper = Decimal(str(clause.upper))
        if clause.operator == "between":
            return right <= left <= upper
        return left < right or left > upper
    raise ValueError(f"unsupported rule operator: {clause.operator}")


def _previous_month_key(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    return (year - 1, 12) if month == 1 else (year, month - 1)
