-- Applied as etl by `knesset migrate`. Idempotent: databases created before migrations
-- existed already have the ops tables from the Phase 0 init script.

create table if not exists ops.sync_state (
    table_name text primary key,
    watermark timestamptz,
    last_id bigint,
    state jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

create table if not exists ops.runs (
    run_id text primary key,
    step text not null,
    started_at timestamptz not null,
    finished_at timestamptz,
    status text not null,
    rows_in bigint,
    rows_out bigint,
    throughput double precision,
    cost_usd numeric(12, 4),
    error text,
    params jsonb
);
create index if not exists runs_step_started_at_idx on ops.runs (step, started_at desc);

create table if not exists ops.failures (
    id bigserial primary key,
    run_id text,
    step text not null,
    item_key text not null,
    error text,
    attempt int not null default 1,
    created_at timestamptz not null default now()
);
create index if not exists failures_step_item_key_idx on ops.failures (step, item_key);

-- Rows that disappeared from the source (weekly re-walk of small tables).
create table if not exists raw._deleted (
    table_name text not null,
    id bigint not null,
    doc jsonb,
    deleted_at timestamptz not null default now(),
    primary key (table_name, id, deleted_at)
);
