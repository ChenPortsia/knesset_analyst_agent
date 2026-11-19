-- Topic labeling (Phase 5): teacher labels from the LLM, hand labels from the user,
-- and the classifier's topics on every chunk.

create table if not exists text.topic_labels (
    chunk_id bigint not null references text.chunks on delete cascade,
    source text not null,               -- llm (teacher) or user (hand-labeled evaluation set)
    topics int[] not null,              -- text.topics ids, 1–3 child topics
    model text,
    version text,                       -- taxonomy version the labels were made against
    created_at timestamptz not null default now(),
    primary key (chunk_id, source)
);

-- Every paid LLM batch, so a long batch can be collected later and spend is tracked.
create table if not exists ops.llm_batches (
    batch_id text primary key,
    step text not null,
    run_id text,
    model text not null,
    n_requests int not null,
    projected_usd numeric(14, 6),
    cost_usd numeric(14, 6),
    status text not null default 'submitted',   -- submitted, collected
    created_at timestamptz not null default now(),
    collected_at timestamptz
);

-- Topics on chunk_enrichment come from the classifier, not the per-chunk LLM pass.
alter table text.chunk_enrichment alter column version drop not null;
alter table text.chunk_enrichment add column if not exists topic_scores real[];
alter table text.chunk_enrichment add column if not exists topics_model text;
alter table text.chunk_enrichment add column if not exists topics_version text;
alter table ops.llm_batches add column if not exists request_map jsonb;  -- custom_id → item ids
alter table ops.runs alter column cost_usd type numeric(14, 6);  -- LLM batches cost fractions of a cent
