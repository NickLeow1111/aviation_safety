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
    callsign          TEXT
);

-- The only query shape that matters: one aircraft's recent track.
CREATE INDEX IF NOT EXISTS aircraft_track_icao_time
    ON aircraft_track (icao24, observed_at DESC);

CREATE INDEX IF NOT EXISTS aircraft_track_time
    ON aircraft_track (observed_at DESC);

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
