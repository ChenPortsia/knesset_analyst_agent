-- Document registry and extracted text (PLAN.md §5). Rebuilt from raw.kns_document* by
-- `knesset registry`; filled by `knesset extract`.
create table if not exists text.documents (
    doc_id text primary key,            -- '<parent_type>:<source row id>'
    source_table text not null,
    parent_type text not null,          -- committee_session, plenum_session, bill, agenda, query, secondary_law, israel_law
    parent_id bigint,
    knesset_num int,
    committee_id bigint,
    doc_date timestamp,                 -- Israel local time
    group_type_id int,
    group_type text,
    file_type text,                     -- docx, doc, rtf, pdf, ...
    url text not null,
    source_updated timestamptz,         -- LastUpdatedDate of the source row
    priority int not null default 4,    -- 1 committee protocols, 2 plenum protocols, 3 bills (scope Knessets), 4 rest
    status text not null default 'pending'
        check (status in ('pending', 'done', 'failed', 'skipped_pdf', 'skipped_other', 'superseded')),
    attempts int not null default 0,
    error text,
    detected_type text,                 -- format by magic bytes after download
    content_hash text,
    text text,
    n_chars int,
    extracted_at timestamptz,
    updated_at timestamptz not null default now()
);
create index if not exists documents_status_idx on text.documents (status, priority, knesset_num);
create index if not exists documents_parent_idx on text.documents (parent_type, parent_id);
