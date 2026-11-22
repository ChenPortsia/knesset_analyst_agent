-- Protocol structure from the documents' own markup (Phase 5): agenda sections with word
-- counts per speaker, and the attendance list. Rebuilt by `knesset structure`.

create table if not exists text.sections (
    doc_id text not null references text.documents on delete cascade,
    ord int not null,                   -- 0 = opening, before the first agenda marker
    tag text,                           -- נושא, הצח or הלסי; null for an untitled opening
    title text,
    item_type_id int,                   -- the session item the title matches, if any
    item_id bigint,
    n_words int not null,
    n_turns int not null,
    primary key (doc_id, ord)
);
create index if not exists sections_item_idx on text.sections (item_type_id, item_id);

create table if not exists text.section_speakers (
    doc_id text not null,
    ord int not null,
    speaker_raw text not null,
    role text,                          -- protocol tag: יור, דובר, אורח, …
    person_id bigint,                   -- as resolved on the chunks
    n_words int not null,
    n_turns int not null,
    primary key (doc_id, ord, speaker_raw),
    foreign key (doc_id, ord) references text.sections on delete cascade
);
create index if not exists section_speakers_person_idx on text.section_speakers (person_id);

create table if not exists text.participants (
    doc_id text not null references text.documents on delete cascade,
    ord int not null,                   -- position in the list
    name text not null,
    grp text,                           -- the list heading: חברי הוועדה, מוזמנים, …
    description text,                   -- role and organization as written
    organization text,                  -- last comma-separated part of the description
    person_id bigint,                   -- dim_person, for exact or member matches
    primary key (doc_id, ord)
);
create index if not exists participants_person_idx on text.participants (person_id);
create index if not exists participants_org_idx on text.participants (organization);
