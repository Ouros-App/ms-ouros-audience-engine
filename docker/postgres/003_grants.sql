GRANT CONNECT ON DATABASE audience TO audience_source, audience_metadata;
GRANT USAGE ON SCHEMA public TO audience_source, audience_metadata;
GRANT SELECT ON public.farms, public.farm_owners, public.water_registries TO audience_source;
GRANT SELECT ON public.tips, public.tip_audience_refs TO audience_metadata;
GRANT SELECT, INSERT, UPDATE ON public.audience_snapshot_refs TO audience_metadata;
GRANT INSERT, UPDATE, DELETE ON public.tip_audience_refs TO audience_metadata;
GRANT DELETE ON public.audience_snapshot_refs TO audience_metadata;
