-- NER bookkeeping (Phase 5): which chunks the NER model has read. Entities go to
-- text.chunk_entities with source 'ner'; a re-chunked chunk is read again (on delete cascade).
create table if not exists text.chunk_ner (
    chunk_id bigint primary key references text.chunks on delete cascade,
    model text not null,
    n_entities int not null,
    done_at timestamptz not null default now()
);
