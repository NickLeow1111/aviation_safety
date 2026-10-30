/***************************************************************************************
 Tables  : opensky_usage, document, document_chunk
 Purpose : live ADS-B budget tracking, and the corpus the assistant is grounded in.

 opensky_usage exists because the OpenSky API is metered by bounding-box area, not
 by call count. Recording every request against a daily budget is the difference
 between a demo that runs for a month and one that is banned by lunchtime.

 document / document_chunk hold whatever a user uploaded, chunked and indexed with
 Postgres full-text search. No embeddings: the configured model gateway has no
 embeddings endpoint, and for a corpus this size a tsvector index answers the same
 questions without sending anything to a third party.
***************************************************************************************/

CREATE TABLE IF NOT EXISTS opensky_usage (
    day         DATE        NOT NULL,
    mode        TEXT        NOT NULL CHECK (mode IN ('anonymous', 'authenticated')),
    requests    INTEGER     NOT NULL DEFAULT 0,
    credits     INTEGER     NOT NULL DEFAULT 0,
    last_call   TIMESTAMPTZ,
    PRIMARY KEY (day, mode)
);

COMMENT ON TABLE opensky_usage IS
    'One row per day and credential mode. Credits are what OpenSky bills; requests are what we spent them on.';

CREATE TABLE IF NOT EXISTS document (
    id          BIGSERIAL PRIMARY KEY,
    filename    TEXT        NOT NULL,
    sha256      TEXT        NOT NULL UNIQUE,
    kind        TEXT        NOT NULL CHECK (kind IN ('text', 'table', 'other')),
    bytes       INTEGER     NOT NULL DEFAULT 0,
    row_count   INTEGER,
    meta        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN document.sha256 IS
    'Content hash: re-uploading the same file is a no-op, not a second copy in the corpus.';

CREATE TABLE IF NOT EXISTS document_chunk (
    id          BIGSERIAL PRIMARY KEY,
    document_id BIGINT      NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    ordinal     INTEGER     NOT NULL,
    content     TEXT        NOT NULL,
    -- Generated, so the index can never drift from the text it indexes.
    tsv         TSVECTOR    GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS document_chunk_tsv_idx ON document_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS document_chunk_document_idx ON document_chunk (document_id);

-- The map's live traffic and the assistant's citations both key off events, so
-- keep an index for the per-place aggregates the assistant quotes.
CREATE INDEX IF NOT EXISTS safety_event_place_time_idx ON safety_event (place_icao, event_time DESC);
