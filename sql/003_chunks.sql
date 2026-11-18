-- Chunks with keyword and vector search (PLAN.md §5, Phase 4).

-- Hebrew attaches prefixes (ו ה ב ל מ ש כ) to words, and Postgres has no Hebrew stemmer.
-- he_strip removes as many leading prefix letters (up to 3) as leave 3+ letters, so every
-- inflected form maps to the same string (ובכנסת, הכנסת, כנסת → נסת). It is applied to both
-- the text and the query, so the over-stripping cancels out.
create or replace function text.he_strip(t text) returns text
language sql immutable parallel safe as $$
    select regexp_replace(t, '(^|[^א-ת])[ובלמשהכ]{1,3}([א-ת]{3,})', '\1\2', 'g')
$$;

alter table text.documents add column if not exists chunked_hash text;  -- content_hash chunks were built from

create table if not exists text.chunks (
    chunk_id bigserial primary key,
    doc_id text not null references text.documents on delete cascade,
    ord int not null,                     -- contiguous within a document
    text text not null,
    n_tokens int not null,                -- bge-m3 tokens
    chunk_hash text not null,             -- sha256 of text; identical chunks share embeddings
    speaker_raw text,                     -- dominant speaker as written in the protocol
    speaker_role text,                    -- protocol tag: יור, דובר, אורח, קריאה, …
    person_id bigint,                     -- marts.dim_person, when resolved
    speaker_confidence real,
    meta jsonb not null default '{}',     -- kind (header/turns/paragraphs), all speakers in the chunk
    tsv tsvector generated always as (to_tsvector('simple', text)) stored,
    tsv_stripped tsvector generated always as (to_tsvector('simple', text.he_strip(text))) stored,
    embedding halfvec(1024),
    unique (doc_id, ord)
);
create index if not exists chunks_hash_idx on text.chunks (chunk_hash);
create index if not exists chunks_person_idx on text.chunks (person_id);
create index if not exists chunks_tsv_idx on text.chunks using gin (tsv);
create index if not exists chunks_tsv_stripped_idx on text.chunks using gin (tsv_stripped);
-- The HNSW index on embedding is built after the bulk embed: `knesset embed --index`.
