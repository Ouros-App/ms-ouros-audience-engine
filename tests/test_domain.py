from datetime import UTC, date, datetime
from decimal import Decimal

from ouros_audience_engine.domain.audience import Evaluation, MeasurementPeriod
from ouros_audience_engine.domain.config import (
    AudienceConfig,
    RuleBranch,
    RuleClause,
    TipAudienceConfig,
)
from ouros_audience_engine.domain.rules import TipObservationRule, WaterConsumptionRule
from ouros_audience_engine.domain.tip_audience_catalog import default_tip_audiences


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
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.ACTIVE
    assert outcome.percentage_above_baseline == Decimal("26")


def test_membership_re_evaluates_same_latest_complete_month() -> None:
    rule = WaterConsumptionRule()
    outcome = rule.evaluate(
        periods(["100", "100", "100", "110"]),
        was_member=True,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.RESOLVED


def test_resolution_uses_lower_hysteresis_threshold() -> None:
    rule = WaterConsumptionRule(activation_pct=Decimal("25"), resolution_pct=Decimal("15"))
    outcome = rule.evaluate(
        periods(["100", "100", "100", "114"]),
        was_member=True,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.RESOLVED


def test_nonconsecutive_baseline_is_unknown() -> None:
    rule = WaterConsumptionRule(min_baseline_samples=3)
    data = periods(["100", "100", "100", "100", "130"])
    del data[2]
    outcome = rule.evaluate(
        data,
        was_member=False,
        evaluated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN
    assert outcome.reason == "NONCONSECUTIVE_BASELINE"


def test_missing_latest_complete_month_is_unknown() -> None:
    rule = WaterConsumptionRule()
    outcome = rule.evaluate(
        periods(["100", "100", "100"], first_month=date(2026, 1, 1)),
        was_member=False,
        evaluated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN
    assert outcome.reason == "MISSING_LATEST_PERIOD"


def test_stale_observation_is_unknown_instead_of_resolution() -> None:
    rule = WaterConsumptionRule(max_observation_age_days=45)
    outcome = rule.evaluate(
        periods(["100", "100", "100", "110"]),
        was_member=True,
        evaluated_at=datetime(2026, 6, 30, tzinfo=UTC),
    )
    assert outcome.evaluation is Evaluation.UNKNOWN


def tip_audience(**overrides: object) -> TipAudienceConfig:
    values: dict[str, object] = {
        "key": "tip_13_environment",
        "version": "1",
        "enabled": True,
        "subject": "farm",
        "member_id_space": "farm_owner_user_id",
        "source": "postgres",
        "metric": "tip_observation_rule",
        "tip_id": 13,
        "max_observation_age_days": 1,
        "unknown_policy": "preserve_previous_membership",
        "first_run_push": False,
        "any_of": [
            RuleBranch(
                all=[
                    RuleClause(field="air_temp_c", operator="gte", value=Decimal("30")),
                    RuleClause(
                        field="relative_humidity_pct",
                        operator="gt",
                        value=Decimal("70"),
                    ),
                ]
            ),
            RuleBranch(all=[RuleClause(field="alarm_active", operator="eq", value=True)]),
        ],
    }
    values.update(overrides)
    return TipAudienceConfig.model_validate(values)


def test_tip_rule_uses_three_valued_logic_for_branches_and_freshness() -> None:
    config = tip_audience()
    rule = TipObservationRule(config)
    observed_at = datetime(2026, 10, 1, tzinfo=UTC)
    evaluated_at = datetime(2026, 10, 1, 12, tzinfo=UTC)

    active = rule.evaluate(
        {"air_temp_c": Decimal("31"), "relative_humidity_pct": Decimal("75")},
        observed_at=observed_at,
        evaluated_at=evaluated_at,
    )
    resolved = rule.evaluate(
        {
            "air_temp_c": Decimal("25"),
            "relative_humidity_pct": Decimal("60"),
            "alarm_active": False,
        },
        observed_at=observed_at,
        evaluated_at=evaluated_at,
    )
    missing_signal = rule.evaluate(
        {"air_temp_c": Decimal("31")},
        observed_at=observed_at,
        evaluated_at=evaluated_at,
    )
    mismatched_signal = rule.evaluate(
        {"air_temp_c": "31", "relative_humidity_pct": Decimal("75"), "alarm_active": False},
        observed_at=observed_at,
        evaluated_at=evaluated_at,
    )
    stale = rule.evaluate(
        {"alarm_active": True},
        observed_at=datetime(2026, 9, 29, tzinfo=UTC),
        evaluated_at=evaluated_at,
    )

    assert active.evaluation is Evaluation.ACTIVE
    assert resolved.evaluation is Evaluation.RESOLVED
    assert missing_signal.evaluation is Evaluation.UNKNOWN
    assert mismatched_signal.evaluation is Evaluation.UNKNOWN
    assert stale.evaluation is Evaluation.UNKNOWN


def test_tip_rule_includes_range_boundaries() -> None:
    rule = TipObservationRule(
        tip_audience(
            any_of=[
                RuleBranch(
                    all=[
                        RuleClause(
                            field="water_temp_c",
                            operator="between",
                            value=Decimal("18"),
                            upper=Decimal("22"),
                        )
                    ]
                )
            ]
        )
    )
    observed_at = datetime(2026, 10, 1, tzinfo=UTC)

    lower = rule.evaluate(
        {"water_temp_c": Decimal("18")},
        observed_at=observed_at,
        evaluated_at=observed_at,
    )
    upper = rule.evaluate(
        {"water_temp_c": Decimal("22")},
        observed_at=observed_at,
        evaluated_at=observed_at,
    )
    outside = rule.evaluate(
        {"water_temp_c": Decimal("17.99")},
        observed_at=observed_at,
        evaluated_at=observed_at,
    )

    assert lower.evaluation is Evaluation.ACTIVE
    assert upper.evaluation is Evaluation.ACTIVE
    assert outside.evaluation is Evaluation.RESOLVED


def test_tip_catalog_covers_each_current_tip_once_and_starts_disabled() -> None:
    definitions = default_tip_audiences()

    assert len(definitions) == 32
    assert len({definition.tip_id for definition in definitions}) == 32
    assert {definition.tip_id for definition in definitions} == {
        97,
        98,
        99,
        100,
        101,
        102,
        103,
        104,
        105,
        106,
        107,
        108,
        109,
        110,
        111,
        112,
        113,
        114,
        115,
        116,
        117,
        118,
        119,
        120,
        121,
        122,
        123,
        124,
        125,
        126,
        127,
        128,
    }
    config = AudienceConfig(audiences=list(definitions))
    dumped_numeric_threshold = definitions[0].model_dump()["any_of"][1]["all"][0]["value"]
    assert len(config.audiences) == 32
    assert all(not definition.enabled for definition in definitions)
    assert isinstance(dumped_numeric_threshold, float)
