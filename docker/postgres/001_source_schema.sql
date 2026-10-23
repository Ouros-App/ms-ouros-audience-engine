CREATE TABLE public.farms (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    area_property NUMERIC NOT NULL CHECK (area_property > 0),
    region VARCHAR(50) NOT NULL,
    chickens_now INTEGER NOT NULL DEFAULT 0 CHECK (chickens_now >= 0),
    id_address INTEGER NOT NULL DEFAULT 1,
    id_enterprise INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE public.farm_owners (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    password TEXT NOT NULL DEFAULT 'fixture-only',
    email VARCHAR(50) NOT NULL,
    id_farm INTEGER NOT NULL REFERENCES public.farms(id)
);

CREATE TABLE public.water_registries (
    id SERIAL PRIMARY KEY,
    registration_date TIMESTAMP NOT NULL,
    start_hydrometer NUMERIC NOT NULL CHECK (start_hydrometer > 0),
    end_hydrometer NUMERIC NOT NULL CHECK (end_hydrometer >= start_hydrometer),
    id_farm INTEGER NOT NULL REFERENCES public.farms(id)
);

CREATE TABLE public.tips (
    id SERIAL PRIMARY KEY,
    tip TEXT NOT NULL CHECK (length(tip) > 0)
);
