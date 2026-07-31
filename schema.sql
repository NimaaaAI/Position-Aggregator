-- One table holding every job ad, plus its embedding.
--
--   psql -d positions -f schema.sql
--
-- Safe to re-run: everything is IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS positions (
    -- Which board and its own id for the ad. Two boards can each have an ad
    -- numbered 250476, so the key is the pair, not the number alone.
    source        text NOT NULL,
    source_id     text NOT NULL,
    url           text NOT NULL,

    -- From the JSON-LD block in the page.
    title         text,
    employer      text,
    city          text,
    country       text,
    street        text,
    postcode      text,
    industry      text,
    posted_at     timestamptz,
    closes_at     timestamptz,
    summary       text,            -- the ~155 character blurb

    -- From the page body.
    description   text,            -- the full advert, ~16,000 characters

    -- What we actually hand the embedding model: title, employer, location and
    -- the opening of the description. Kept so that when a search result looks
    -- wrong you can see exactly what was compared, and so the embedder can skip
    -- anything whose text has not changed.
    embed_text    text,
    embedding     vector(768),     -- 768 is fixed by multilingual-e5-base

    -- Bookkeeping.
    html_file     text,            -- which downloaded file this came from
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),

    -- Set when the ad drops out of the sitemap, which means it has closed.
    -- Recorded rather than deleted: the history is worth keeping, and you can
    -- choose whether to include closed ads in a search.
    closed_at     timestamptz,

    extracted_at  timestamptz,
    embedded_at   timestamptz,

    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS positions_country_idx  ON positions (country);
CREATE INDEX IF NOT EXISTS positions_closes_idx   ON positions (closes_at);
CREATE INDEX IF NOT EXISTS positions_open_idx     ON positions (closed_at)
    WHERE closed_at IS NULL;

-- Not needed at 1,886 rows -- scanning them all takes microseconds -- but it
-- costs one line now and means nothing has to change as this grows.
CREATE INDEX IF NOT EXISTS positions_embedding_idx
    ON positions USING hnsw (embedding vector_cosine_ops);

ALTER TABLE positions OWNER TO positions;
