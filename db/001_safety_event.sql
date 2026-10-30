/***************************************************************************************
 Table   : safety_event
 Target  : local PostgreSQL (POC). Mirrors the DM_TBL_SRG_* conventions where it can.
 Purpose : One row per safety event, whatever produced it.

 Why one table and not two
 -------------------------
 A mapped event can arrive from three places: a report somebody filed, a file
 somebody uploaded, or a detection the geometry made from ADS-B tracks. All three
 answer the same questions — what, when, where, how bad, says who — so they get
 the same shape. `source` and `provenance` keep them distinguishable without
 pushing the difference into a join every query has to remember.

 Two deliberate departures from DM_TBL_SRG_OCCURRENCES
 -----------------------------------------------------
 1. event_time is a TIMESTAMPTZ, not a DATE. A map that can only place an event
    on a day cannot answer "what happened between 09:00 and 10:00", and loss of
    separation is measured in seconds.
 2. lat/lon are first class. The DM table carries Location as free text ('WSSS');
    nothing in it can be drawn.
 ***************************************************************************************/

DROP TABLE IF EXISTS safety_event CASCADE;

CREATE TABLE safety_event (
    id            BIGSERIAL PRIMARY KEY,

    -- what and when
    event_time    TIMESTAMPTZ NOT NULL,
    event_type    TEXT        NOT NULL,
    severity      TEXT        NOT NULL DEFAULT 'unknown',

    -- where
    lat           DOUBLE PRECISION NOT NULL CHECK (lat  BETWEEN -90  AND 90),
    lon           DOUBLE PRECISION NOT NULL CHECK (lon  BETWEEN -180 AND 180),
    place_icao    TEXT,
    place_name    TEXT,
    altitude_ft   INTEGER,

    -- who
    callsigns     TEXT[] NOT NULL DEFAULT '{}',
    icao24        TEXT[] NOT NULL DEFAULT '{}',
    registration  TEXT,

    -- how the conclusion was reached: min separation, duration, threshold used
    metrics       JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- provenance: every row can be traced back to what produced it
    source        TEXT NOT NULL CHECK (source IN ('report', 'upload', 'derived')),
    provenance    TEXT NOT NULL,
    external_ref  TEXT,
    raw           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN safety_event.source IS
    'report = filed with the regulator; upload = user-supplied file; derived = computed from tracks';
COMMENT ON COLUMN safety_event.provenance IS
    'Human-readable sentence naming the source and every derived field, so a dot can always be questioned';
COMMENT ON COLUMN safety_event.metrics IS
    'Detection evidence (min_horizontal_nm, min_vertical_ft, duration_s, threshold) — empty for reports';

-- The map asks for a time window inside a box, newest first. Both indexes serve that.
CREATE INDEX safety_event_time_idx      ON safety_event (event_time DESC);
CREATE INDEX safety_event_type_time_idx ON safety_event (event_type, event_time DESC);
CREATE INDEX safety_event_place_idx     ON safety_event (place_icao);
CREATE INDEX safety_event_geo_idx       ON safety_event (lat, lon);
CREATE INDEX safety_event_source_idx    ON safety_event (source);

-- An upload that is re-run, or a detector that re-scans the same window, must not
-- double the map. external_ref is the producer's own identifier.
CREATE UNIQUE INDEX safety_event_producer_key
    ON safety_event (source, external_ref)
    WHERE external_ref IS NOT NULL;

-- What the map layer actually reads. Kept narrow so a viewport query stays cheap.
CREATE VIEW safety_event_map AS
SELECT id, event_time, event_type, severity, lat, lon,
       place_icao, place_name, altitude_ft, callsigns, registration, source
FROM safety_event;
