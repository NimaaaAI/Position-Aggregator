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

-- What kind of post this is: phd, postdoc, professor and so on.
--
-- An array rather than a single value, because plenty of adverts genuinely offer
-- both -- "PhD or postdoctoral position", "Several fully funded PhD / Post-Doctoral
-- Positions". Filed as one or the other they would vanish from half the searches
-- they belong in.
--
-- This exists because ranking cannot supply it. Every AI-related advert scores
-- between 0.82 and 0.85 cosine similarity whatever the job, since the subject is
-- the whole document and the job type is one word in a title. Counted by hand,
-- 52 positions have both "PhD" and something AI-related in the title; a search for
-- exactly that surfaced 7 of them in the top 40. A column can be filtered; a hope
-- cannot.
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS position_type text[];

CREATE INDEX IF NOT EXISTS positions_type_idx ON positions USING gin (position_type);


-- Full-text search, to sit alongside the vector search.
--
-- Embeddings capture meaning, which is exactly why they are bad at strings that
-- have none: "MSCA", "ERC", a grant code, a project acronym. Those need matching
-- literally, and Postgres does that without any extension.
--
-- The 'simple' configuration, not 'english': it does no stemming, which is what we
-- want here. The adverts arrive in Swedish, German, French, Dutch and Norwegian, so
-- English stemming rules would be wrong for most of them -- and an acronym should
-- be matched exactly rather than stemmed at all.
--
-- Generated and stored, so it is maintained by the database and cannot fall out of
-- step with the text it indexes.
ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'simple',
            coalesce(title, '') || ' ' ||
            coalesce(employer, '') || ' ' ||
            coalesce(city, '') || ' ' ||
            coalesce(country, '') || ' ' ||
            coalesce(description, '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS positions_tsv_idx ON positions USING gin (tsv);

ALTER TABLE positions OWNER TO positions;
