from typing import Literal

from ouros_audience_engine.domain.config import RuleBranch, RuleClause, TipAudienceConfig


def _clause(
    field: str,
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "between", "outside"],
    value: bool | float | int,
    upper: float | int | None = None,
) -> RuleClause:
    converted_value = value if isinstance(value, bool) else float(value)
    return RuleClause(
        field=field,
        operator=operator,
        value=converted_value,
        upper=float(upper) if upper is not None else None,
    )


def _branch(*clauses: RuleClause) -> RuleBranch:
    return RuleBranch(all=list(clauses))


def _tip(
    number: int,
    tip_id: int,
    key_suffix: str,
    max_age_days: int,
    *branches: RuleBranch,
) -> TipAudienceConfig:
    return TipAudienceConfig(
        key=f"tip_{number:02d}_{key_suffix}",
        version="1",
        enabled=False,
        subject="farm",
        member_id_space="farm_owner_user_id",
        source="postgres",
        metric="tip_observation_rule",
        tip_id=tip_id,
        any_of=list(branches),
        max_observation_age_days=max_age_days,
        unknown_policy="preserve_previous_membership",
        first_run_push=False,
    )


def default_tip_audiences() -> tuple[TipAudienceConfig, ...]:
    """POC rules for the 32 operational tips; inputs are farm-level observations."""
    return (
        _tip(
            1,
            109,
            "nipple_setup",
            1,
            _branch(_clause("nipple_setup_out_of_age_target", "eq", True)),
            _branch(_clause("nipple_flow_ml_min", "outside", 60, 90)),
        ),
        _tip(
            2,
            114,
            "water_line_flush",
            1,
            _branch(_clause("hours_since_flush", "gt", 24)),
            _branch(_clause("between_lot_sanitation_complete", "eq", False)),
        ),
        _tip(
            3,
            111,
            "water_quality",
            1,
            _branch(_clause("water_ph", "outside", 6, 6.8)),
            _branch(_clause("chlorine_initial_ppm", "outside", 2, 4)),
            _branch(_clause("chlorine_endpoint_ppm", "lt", 1.5)),
        ),
        _tip(
            4,
            121,
            "night_leak_test",
            1,
            _branch(_clause("night_static_test_flow_detected", "eq", True)),
        ),
        _tip(
            5,
            123,
            "water_temperature",
            1,
            _branch(_clause("water_temp_c", "outside", 18, 22)),
        ),
        _tip(
            6,
            103,
            "generator_test",
            8,
            _branch(_clause("generator_load_test_age_days", "gt", 7)),
        ),
        _tip(
            7,
            108,
            "fan_belts",
            8,
            _branch(_clause("fan_belt_deflection_cm", "outside", 1.5, 2)),
            _branch(_clause("fan_maintenance_overdue", "eq", True)),
        ),
        _tip(
            8,
            117,
            "peak_energy_cost",
            1,
            _branch(
                _clause("peak_grid_use", "eq", True),
                _clause("generator_available", "eq", True),
                _clause("peak_grid_cost_exceeds_generator_cost", "eq", True),
            ),
        ),
        _tip(
            9,
            125,
            "static_pressure",
            1,
            _branch(_clause("static_pressure_pa", "lt", 37)),
        ),
        _tip(
            10,
            104,
            "solar_panel_maintenance",
            1,
            _branch(
                _clause("dry_period", "eq", True),
                _clause("solar_cleaning_age_days", "gt", 14),
            ),
            _branch(_clause("solar_production_drop_alert", "eq", True)),
        ),
        _tip(
            11,
            122,
            "brooding_humidity",
            1,
            _branch(
                _clause("chick_age_days", "lte", 7),
                _clause("relative_humidity_pct", "lt", 50),
            ),
            _branch(
                _clause("chick_age_days", "lte", 7),
                _clause("relative_humidity_pct", "gt", 70),
            ),
        ),
        _tip(
            12,
            112,
            "minimum_ventilation",
            1,
            _branch(_clause("airflow_below_minimum", "eq", True)),
        ),
        _tip(
            13,
            97,
            "air_speed_by_age",
            1,
            _branch(
                _clause("broiler_age_days", "gt", 35),
                _clause("air_temp_c", "gt", 30),
                _clause("air_speed_m_s", "outside", 2.5, 3),
            ),
            _branch(
                _clause("broiler_age_days", "lt", 21),
                _clause("air_speed_m_s", "gt", 1),
            ),
        ),
        _tip(
            14,
            113,
            "cooling_pad_maintenance",
            8,
            _branch(
                _clause("pad_pump_running", "eq", True),
                _clause("external_relative_humidity_pct", "gt", 75),
            ),
            _branch(_clause("pad_reservoir_maintenance_overdue", "eq", True)),
        ),
        _tip(
            15,
            116,
            "air_quality_gases",
            1,
            _branch(
                _clause("chick_age_days", "lte", 7),
                _clause("ammonia_ppm", "gte", 20),
            ),
            _branch(
                _clause("chick_age_days", "lte", 7),
                _clause("carbon_monoxide_ppm", "gte", 10),
            ),
            _branch(
                _clause("chick_age_days", "lte", 7),
                _clause("carbon_dioxide_ppm", "gte", 3000),
            ),
        ),
        _tip(
            16,
            105,
            "preplacement_temperature",
            1,
            _branch(
                _clause("placement_hours_from_now", "between", 0, 36),
                _clause("floor_temp_c", "outside", 30, 32),
            ),
            _branch(
                _clause("placement_hours_from_now", "between", 0, 36),
                _clause("air_temp_c", "outside", 32, 34),
            ),
        ),
        _tip(
            17,
            107,
            "crop_fill",
            1,
            _branch(
                _clause("hours_since_placement", "between", 8, 12),
                _clause("crop_fill_pct", "lt", 80),
            ),
            _branch(
                _clause("hours_since_placement", "between", 12, 24),
                _clause("crop_fill_pct", "lt", 85),
            ),
            _branch(
                _clause("hours_since_placement", "gte", 24),
                _clause("crop_fill_pct", "lt", 95),
            ),
        ),
        _tip(
            18,
            98,
            "first_day_access",
            1,
            _branch(
                _clause("chick_age_hours", "lte", 24),
                _clause("paper_coverage_pct", "lt", 40),
            ),
            _branch(
                _clause("chick_age_hours", "lte", 24),
                _clause("feed_g_per_chick", "lt", 50),
            ),
            _branch(
                _clause("chick_age_hours", "lte", 24),
                _clause("chicks_per_tray", "gt", 100),
            ),
        ),
        _tip(
            19,
            124,
            "feed_quality",
            1,
            _branch(_clause("wet_feed_detected", "eq", True)),
            _branch(_clause("moldy_feed_detected", "eq", True)),
            _branch(_clause("feed_residue_quality_bad", "eq", True)),
        ),
        _tip(
            20,
            118,
            "feeder_access",
            1,
            _branch(_clause("birds_per_plate", "gt", 70)),
            _branch(_clause("feeder_height_out_of_target", "eq", True)),
        ),
        _tip(
            21,
            115,
            "silo_between_lots",
            30,
            _branch(
                _clause("between_flocks", "eq", True),
                _clause("silo_cleaning_complete", "eq", False),
            ),
            _branch(
                _clause("between_flocks", "eq", True),
                _clause("silo_drying_hours", "lt", 48),
            ),
        ),
        _tip(
            22,
            126,
            "darkling_beetle_control",
            1,
            _branch(_clause("darkling_beetle_detected", "eq", True)),
            _branch(_clause("bedding_target_streak_days", "lt", 8)),
        ),
        _tip(
            23,
            101,
            "litter_condition",
            1,
            _branch(_clause("litter_wet_or_compact", "eq", True)),
            _branch(_clause("litter_depth_cm", "gt", 3)),
            _branch(_clause("footpad_lesions_detected", "eq", True)),
        ),
        _tip(
            24,
            110,
            "vehicle_disinfection",
            1,
            _branch(_clause("vehicle_disinfection_complete", "eq", False)),
            _branch(_clause("disinfectant_age_days", "gt", 7)),
        ),
        _tip(
            25,
            100,
            "vaccination_water",
            1,
            _branch(
                _clause("hours_to_vaccination", "between", 0, 48),
                _clause("chlorination_active", "eq", True),
            ),
            _branch(_clause("vaccination_uptake_pct", "lt", 95)),
        ),
        _tip(
            26,
            102,
            "sanitary_downtime",
            30,
            _branch(_clause("sanitary_downtime_days", "lt", 15)),
        ),
        _tip(
            27,
            120,
            "weekly_weighing",
            8,
            _branch(_clause("weighing_age_days", "gt", 7)),
            _branch(_clause("weight_sample_below_minimum", "eq", True)),
            _branch(_clause("uniformity_pct", "lt", 85)),
            _branch(_clause("weight_cv_pct", "gte", 8)),
        ),
        _tip(
            28,
            128,
            "stocking_density",
            1,
            _branch(
                _clause("dark_house", "eq", False),
                _clause("bird_density_kg_m2", "gt", 28),
            ),
            _branch(
                _clause("dark_house", "eq", True),
                _clause("bird_density_kg_m2", "gt", 38),
            ),
        ),
        _tip(
            29,
            106,
            "daily_mortality",
            1,
            _branch(_clause("daily_mortality_pct", "gt", 0.1)),
        ),
        _tip(
            30,
            127,
            "lighting_program",
            1,
            _branch(
                _clause("chick_age_days", "lte", 5),
                _clause("daily_light_hours", "lt", 23),
            ),
            _branch(
                _clause("chick_age_days", "between", 7, 35),
                _clause("daily_dark_hours", "outside", 4, 6),
            ),
        ),
        _tip(
            31,
            119,
            "preloading_checklist",
            1,
            _branch(
                _clause("loading_planned", "eq", True),
                _clause("feed_withdrawal_hours", "outside", 8, 12),
            ),
            _branch(
                _clause("loading_planned", "eq", True),
                _clause("water_removed_too_early", "eq", True),
            ),
            _branch(
                _clause("loading_planned", "eq", True),
                _clause("loading_checklist_complete", "eq", False),
            ),
        ),
        _tip(
            32,
            99,
            "sensor_maintenance",
            30,
            _branch(_clause("sensor_cleaning_age_days", "gt", 14)),
            _branch(_clause("sensor_calibration_age_days", "gt", 30)),
            _branch(_clause("alarm_test_age_days", "gt", 7)),
        ),
    )
