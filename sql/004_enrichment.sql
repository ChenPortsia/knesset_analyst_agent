-- Enrichment of chunks (PLAN.md §5, Phase 5). Rows go with their chunk (on delete cascade),
-- so re-chunking a changed document invalidates its enrichment.

create table if not exists text.chunk_items (
    chunk_id bigint not null references text.chunks on delete cascade,
    item_type_id int not null,          -- marts.dim_item_type
    item_id bigint not null,
    primary key (chunk_id, item_type_id, item_id)
);
create index if not exists chunk_items_item_idx on text.chunk_items (item_type_id, item_id);

create table if not exists text.chunk_entities (
    chunk_id bigint not null references text.chunks on delete cascade,
    type text not null,                 -- person, faction, committee, bill, law, org, place, …
    surface text not null,              -- as written in the chunk
    normalized text,
    ref_table text,                     -- marts table the entity resolved to, if any
    ref_id bigint,
    confidence real,
    source text not null                -- speaker, ner, llm
);
create index if not exists chunk_entities_chunk_idx on text.chunk_entities (chunk_id);
create index if not exists chunk_entities_ref_idx on text.chunk_entities (type, ref_id);
create index if not exists chunk_entities_normalized_idx on text.chunk_entities (normalized);

create table if not exists text.topics (
    topic_id int primary key,
    name_he text not null,
    name_en text,
    description text,
    parent_id int references text.topics
);

create table if not exists text.chunk_enrichment (
    chunk_id bigint primary key references text.chunks on delete cascade,
    version text not null,
    sentiment real,                     -- -1 … 1
    sentiment_label text,
    stance text,
    stance_target text,
    speech_act text,
    topics text[],
    keywords text[],
    summary text,
    model text,
    created_at timestamptz not null default now()
);
create index if not exists chunk_enrichment_topics_idx on text.chunk_enrichment using gin (topics);
create index if not exists chunk_enrichment_keywords_idx on text.chunk_enrichment using gin (keywords);

create table if not exists text.document_summaries (
    doc_id text primary key references text.documents on delete cascade,
    summary text,
    key_points jsonb,
    main_topics text[],
    participants text[],
    model text,
    version text,
    created_at timestamptz not null default now()
);
