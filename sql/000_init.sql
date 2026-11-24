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
create extension if not exists pg_prewarm;  -- `make warm` loads the vector index into memory

create schema raw authorization etl;
create schema ops authorization etl;
create schema text authorization etl;
create schema marts authorization etl;
create schema app authorization ui_app;

grant usage on schema marts, text to agent_ro;
alter default privileges for role etl in schema marts, text grant select on tables to agent_ro;
grant usage on schema app to etl;
alter default privileges for role ui_app in schema app grant select on tables to etl;

-- Tables are created by migrations (sql/001_*.sql onward), applied by `knesset migrate`.
