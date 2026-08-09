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

-- The country as ISO 3166 alpha-2, worked out from whatever the board wrote.
--
-- `country` is kept exactly as published, because that is the honest record and it
-- is what gets displayed. But the boards disagree: the Netherlands arrives as "NL",
-- "The Netherlands" and "Netherlands" from three different sites, so filtering on
-- the text would split one country into three and hide most of it behind whichever
-- spelling was picked. The code is the thing to filter on.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS country_code text;

CREATE INDEX IF NOT EXISTS positions_country_code_idx ON positions (country_code);


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

-- Words too common in this corpus to distinguish anything, measured rather than
-- listed by hand.
--
-- A full-text query ORing its words is only useful if those words are informative.
-- "positions using PyTorch or TensorFlow" must become `pytorch | tensorflow`: leave
-- "positions" and "using" in and every advert matches, because every advert contains
-- them. Postgres's ts_rank has no notion of document frequency and cannot work this
-- out for itself.
--
-- Counted from the corpus with ts_stat, so it needs no hand-written list, covers
-- Swedish and German and Dutch as readily as English, and changes as the data does.
CREATE TABLE IF NOT EXISTS stopwords (
    word  text PRIMARY KEY,
    ndoc  integer NOT NULL,   -- adverts containing it
    share real    NOT NULL    -- as a fraction of all adverts
);

ALTER TABLE stopwords OWNER TO positions;


-- Each advert split into pieces, one vector per piece.
--
-- positions.embedding covers only the first ~1,500 characters, because that is all
-- multilingual-e5-base reads. The average advert here is 5,976 characters and 89% of
-- them are over 3,000, so roughly the first quarter of a typical advert is
-- searchable by meaning and the rest is not.
--
-- Worse, much of that quarter is the same for every posting from an employer -- "The
-- University of Antwerp is a dynamic, forward-thinking European university…" -- so
-- the budget is partly spent on text that cannot distinguish anything.
--
-- Chunking fixes both. A position is then scored by its best-matching piece rather
-- than by its opening, and the boilerplate simply becomes a chunk that never matches
-- anything, with no need to detect it.
CREATE TABLE IF NOT EXISTS position_chunks (
    source      text NOT NULL,
    source_id   text NOT NULL,
    chunk_index integer NOT NULL,
    text        text NOT NULL,     -- exactly what was embedded, for debugging
    embedding   vector(768),
    embedded_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (source, source_id, chunk_index),
    FOREIGN KEY (source, source_id)
        REFERENCES positions (source, source_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS position_chunks_embedding_idx
    ON position_chunks USING hnsw (embedding vector_cosine_ops);

-- Who may use the web interface.
--
-- The password itself is never stored. What is kept is scrypt(password, salt):
-- scrypt is a key-derivation function, deliberately slow and memory-hungry, so a
-- stolen table cannot be turned back into passwords at any useful rate. It is in
-- the standard library, so this costs no dependency.
--
-- Each user gets their own random salt, which is why two people choosing the same
-- password still store different bytes.
CREATE TABLE IF NOT EXISTS users (
    username      text PRIMARY KEY,
    password_hash bytea NOT NULL,
    salt          bytea NOT NULL,
    is_admin      boolean NOT NULL DEFAULT false,
    active        boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login    timestamptz
);

-- One row per signed-in browser.
--
-- The cookie holds only a random token; everything else about the session lives
-- here. That is what makes it revocable: delete the row and the next request from
-- that browser is signed out, with nothing to do on the browser's side.
CREATE TABLE IF NOT EXISTS sessions (
    token      text PRIMARY KEY,
    username   text NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    ip         text,
    user_agent text
);

CREATE INDEX IF NOT EXISTS sessions_username_idx ON sessions (username);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx   ON sessions (expires_at);

-- Anyone may fill in the registration form; nobody may use the result until an
-- administrator sets active. So `active` is the pending flag as well as the
-- off switch, and an unapproved account is a queue entry rather than a way in.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email text;

-- What one person may spend of the owner's API credit in a day. Only the written
-- answer costs anything, so only that is counted; searching is free and unlimited.
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_ask_limit integer NOT NULL DEFAULT 50;

-- One row per question asked, which is what the admin page reports.
--
-- The question text is kept: without it the page can say how much someone spent
-- but not what they were doing, which is the thing worth knowing when a bill or a
-- pattern looks wrong.
CREATE TABLE IF NOT EXISTS activity (
    id                bigserial PRIMARY KEY,
    username          text NOT NULL REFERENCES users (username) ON DELETE CASCADE,
    at                timestamptz NOT NULL DEFAULT now(),
    endpoint          text NOT NULL,      -- 'search' costs nothing, 'ask' calls a model
    question          text,
    results           integer,
    model             text,
    prompt_tokens     integer,
    completion_tokens integer,
    ms                integer
);

CREATE INDEX IF NOT EXISTS activity_who_idx ON activity (username, at DESC);
CREATE INDEX IF NOT EXISTS activity_at_idx  ON activity (at DESC);

ALTER TABLE positions OWNER TO positions;
ALTER TABLE position_chunks OWNER TO positions;
ALTER TABLE users OWNER TO positions;
ALTER TABLE sessions OWNER TO positions;
ALTER TABLE activity OWNER TO positions;
