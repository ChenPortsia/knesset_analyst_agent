-- Runs once, on the first start of an empty db volume (docker-entrypoint-initdb.d).
-- Passwords come from the db container environment (.env) via psql \getenv.
\set ON_ERROR_STOP on
\getenv etl_password ETL_PASSWORD
\getenv agent_ro_password AGENT_RO_PASSWORD
\getenv ui_app_password UI_APP_PASSWORD
\getenv phoenix_password PHOENIX_DB_PASSWORD

create role etl login password :'etl_password';
create role agent_ro login password :'agent_ro_password';
create role ui_app login password :'ui_app_password';
create role phoenix login password :'phoenix_password';
alter role agent_ro set statement_timeout = '15s';
alter role agent_ro set default_transaction_read_only = on;

create database phoenix owner phoenix;

create extension if not exists vector;
create extension if not exists pg_trgm;

create schema raw authorization etl;
create schema ops authorization etl;
create schema text authorization etl;
create schema marts authorization etl;
create schema app authorization ui_app;

grant usage on schema marts, text to agent_ro;
alter default privileges for role etl in schema marts, text grant select on tables to agent_ro;
grant usage on schema app to etl;
alter default privileges for role ui_app in schema app grant select on tables to etl;

set role etl;

create table ops.sync_state (
    table_name text primary key,
    watermark timestamptz,
    last_id bigint,
    state jsonb not null default '{}',
    updated_at timestamptz not null default now()
);

create table ops.runs (
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
create index on ops.runs (step, started_at desc);

create table ops.failures (
    id bigserial primary key,
    run_id text,
    step text not null,
    item_key text not null,
    error text,
    attempt int not null default 1,
    created_at timestamptz not null default now()
);
create index on ops.failures (step, item_key);

reset role;
