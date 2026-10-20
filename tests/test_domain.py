from datetime import UTC, date, datetime
from decimal import Decimal

from ouros_audience_engine.domain.audience import Evaluation, MeasurementPeriod
from ouros_audience_engine.domain.rules import WaterConsumptionRule


def periods(values: list[str], first_month: date = date(2026, 1, 1)) -> list[MeasurementPeriod]:
    result = []
    year, month = first_month.year, first_month.month
    for value in values:
        result.append(
            MeasurementPeriod(
                farm_id=1,
                period_start=date(year, month, 1),
                consumed=Decimal(value),
                source_watermark=datetime(year, month, 28, tzinfo=UTC),
            )
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    return result


def test_new_activation_uses_three_prior_complete_months_and_hysteresis() -> None:
    rule = WaterConsumptionRule(activation_pct=Decimal("25"), resolution_pct=Decimal("15"))
    outcome = rule.evaluate(
        periods(["100", "100", "100", "126"]),
        was_member=False,
        previous_watermark=None,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.ACTIVE
    assert outcome.percentage_above_baseline == Decimal("26")


def test_active_membership_is_not_resolved_without_a_new_measurement() -> None:
    rule = WaterConsumptionRule()
    data = periods(["100", "100", "100", "110"])
    outcome = rule.evaluate(
        data,
        was_member=True,
        previous_watermark=data[-1].source_watermark,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN


def test_resolution_requires_new_valid_period_and_uses_lower_hysteresis_threshold() -> None:
    rule = WaterConsumptionRule(activation_pct=Decimal("25"), resolution_pct=Decimal("15"))
    outcome = rule.evaluate(
        periods(["100", "100", "100", "114"]),
        was_member=True,
        previous_watermark=datetime(2026, 4, 1, tzinfo=UTC),
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.RESOLVED


def test_insufficient_or_nonconsecutive_history_is_unknown() -> None:
    rule = WaterConsumptionRule(min_baseline_samples=3)
    outcome = rule.evaluate(
        periods(["100", "100", "100", "130"], first_month=date(2025, 1, 1))[::2],
        was_member=False,
        previous_watermark=None,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN


def test_stale_observation_is_unknown_instead_of_resolution() -> None:
    rule = WaterConsumptionRule(max_observation_age_days=45)
    outcome = rule.evaluate(
        periods(["100", "100", "100", "110"]),
        was_member=True,
        previous_watermark=None,
        evaluated_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN
