INSERT INTO public.water_registries (registration_date, start_hydrometer, end_hydrometer, id_farm)
SELECT TIMESTAMP '2026-10-30 12:00:00', 1582, 1682, 2
WHERE NOT EXISTS (
    SELECT 1 FROM public.water_registries
    WHERE id_farm = 2 AND registration_date = TIMESTAMP '2026-10-30 12:00:00'
);
