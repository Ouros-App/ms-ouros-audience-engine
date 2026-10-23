INSERT INTO public.farms (id, name, area_property, region) VALUES
    (1, 'QA farm eligible', 10, 'QA'),
    (2, 'QA farm control', 12, 'QA');

INSERT INTO public.farm_owners (id, name, email, id_farm) VALUES
    (1, NULL, 'owner1@example.invalid', 1),
    (2, NULL, 'owner2@example.invalid', 1),
    (3, NULL, 'owner3@example.invalid', 2);

INSERT INTO public.tips (id, tip) VALUES
    (1, 'QA fixture tip: review monthly water use.'),
    (2, 'QA fixture tip: inspect equipment readings.');

INSERT INTO public.water_registries (registration_date, start_hydrometer, end_hydrometer, id_farm)
SELECT
    month_start + INTERVAL '27 days',
    1000 + COALESCE(SUM(consumption) OVER (
        PARTITION BY id_farm ORDER BY month_start ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ), 0),
    1000 + SUM(consumption) OVER (
        PARTITION BY id_farm ORDER BY month_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ),
    id_farm
FROM (
    VALUES
        (1, DATE '2026-05-01', 100::numeric),
        (1, DATE '2026-06-01', 100::numeric),
        (1, DATE '2026-07-01', 100::numeric),
        (1, DATE '2026-08-01', 100::numeric),
        (1, DATE '2026-09-01', 130::numeric),
        (2, DATE '2026-05-01', 100::numeric),
        (2, DATE '2026-06-01', 102::numeric),
        (2, DATE '2026-07-01', 99::numeric),
        (2, DATE '2026-08-01', 101::numeric),
        (2, DATE '2026-09-01', 100::numeric)
) AS monthly(id_farm, month_start, consumption)
ORDER BY id_farm, month_start;

INSERT INTO public.audience_snapshot_refs (audience_key, rule_version)
VALUES ('high_water_consumption', '1')
ON CONFLICT (audience_key) DO NOTHING;

INSERT INTO public.tip_audience_refs (tip_id, audience_key, enabled) VALUES
    (1, 'high_water_consumption', TRUE),
    (2, 'high_water_consumption', FALSE);

SELECT setval(pg_get_serial_sequence('public.farms', 'id'), 2, TRUE);
SELECT setval(pg_get_serial_sequence('public.farm_owners', 'id'), 3, TRUE);
SELECT setval(pg_get_serial_sequence('public.tips', 'id'), 2, TRUE);
SELECT setval(pg_get_serial_sequence('public.water_registries', 'id'), 10, TRUE);
