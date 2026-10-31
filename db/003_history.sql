-- History for the live-traffic features.
--
-- Two tables, both additive: nothing here drops or rewrites the event store.
--
--   aircraft_track  one row per aircraft per poll — the raw positions that make
--                   motion visible at all. Pruned by age, not by hand.
--   track_event     manoeuvres read out of those positions (go-arounds, holds,
--                   unstable approaches), so the question "how many go-arounds at
--                   WSSS this week" stops depending on how long the server has
--                   been running.
--
-- No foreign keys to safety_event: a track is observation, not a reported event,
-- and the two are deliberately separate tables.

CREATE TABLE IF NOT EXISTS aircraft_track (
    id                BIGSERIAL PRIMARY KEY,
    icao24            TEXT             NOT NULL,
    observed_at       TIMESTAMPTZ      NOT NULL,
    lat               DOUBLE PRECISION NOT NULL,
    lon               DOUBLE PRECISION NOT NULL,
    altitude_ft       DOUBLE PRECISION,
    ground_speed_kt   DOUBLE PRECISION,
    track_deg         DOUBLE PRECISION,
    vertical_rate_fpm DOUBLE PRECISION,
    on_ground         BOOLEAN          NOT NULL DEFAULT FALSE,
    callsign          TEXT,
    -- ICAO type designator (A320, B789). Only the community feed reports it, so it is
    -- null on OpenSky-only fixes; the dashboard reads the newest non-null per aircraft.
    aircraft_type     TEXT
);

-- Existing installs predate the column; adding it is additive and safe to repeat.
ALTER TABLE aircraft_track ADD COLUMN IF NOT EXISTS aircraft_type TEXT;

-- The only query shape that matters: one aircraft's recent track.
CREATE INDEX IF NOT EXISTS aircraft_track_icao_time
    ON aircraft_track (icao24, observed_at DESC);

CREATE INDEX IF NOT EXISTS aircraft_track_time
    ON aircraft_track (observed_at DESC);

-- What each airframe is. Type designators arrive only from the community feed and only
-- on some fixes, so they are remembered per aircraft rather than per position: an
-- OpenSky fix for the same aircraft must not erase what we already know about it.
CREATE TABLE IF NOT EXISTS aircraft_meta (
    icao24        TEXT PRIMARY KEY,
    aircraft_type TEXT,
    callsign      TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS track_event (
    id           BIGSERIAL PRIMARY KEY,
    icao24       TEXT             NOT NULL,
    callsign     TEXT,
    kind         TEXT             NOT NULL,
    detail       TEXT             NOT NULL,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    altitude_ft  DOUBLE PRECISION,
    observed_at  TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS track_event_kind_time
    ON track_event (kind, observed_at DESC);

CREATE INDEX IF NOT EXISTS track_event_aircraft_time
    ON track_event (icao24, observed_at DESC);

-- ---------------------------------------------------------------------------
-- Audit columns on the event table.
--
-- A detection that turns out to be wrong is never deleted: it is marked. The row keeps
-- its original payload, its provenance and its metrics, so a later reader can check the
-- re-judgement instead of having to trust it. Active views filter on invalidated_at.
-- ---------------------------------------------------------------------------
ALTER TABLE safety_event ADD COLUMN IF NOT EXISTS detector TEXT;
ALTER TABLE safety_event ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE safety_event ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;
ALTER TABLE safety_event ADD COLUMN IF NOT EXISTS invalidated_by TEXT;

CREATE INDEX IF NOT EXISTS safety_event_detector_time
    ON safety_event (detector, event_time DESC);

CREATE INDEX IF NOT EXISTS safety_event_active_time
    ON safety_event (event_time DESC) WHERE invalidated_at IS NULL;
