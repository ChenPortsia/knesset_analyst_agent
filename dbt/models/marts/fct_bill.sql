with initiators as (
    select
        bill_id,
        array_agg(person_id order by ordinal) filter (where is_initiator)     as initiator_ids,
        array_agg(person_id order by ordinal) filter (where not is_initiator) as joiner_ids
    from {{ ref('stg_bill_initiator') }}
    group by bill_id
),

names as (
    select bill_id, count(*) as n_previous_names
    from {{ ref('stg_bill_name') }}
    group by bill_id
)

select
    b.bill_id,
    b.knesset_num,
    b.bill_name,
    b.type_desc,
    b.subtype_desc,
    b.private_number,
    b.bill_number,
    b.committee_id,
    b.status_id,
    s.status_desc,
    coalesce(s.status_desc like 'התקבלה בקריאה שלישית%' or s.status_desc = 'פורסם ברשומות', false)
        as is_passed,
    b.publication_date,
    b.publication_series_desc,
    b.postponement_reason_desc,
    b.is_continuation_bill,
    b.summary_law,
    coalesce(i.initiator_ids, '{}') as initiator_ids,
    coalesce(i.joiner_ids, '{}')    as joiner_ids,
    coalesce(n.n_previous_names, 0) as n_previous_names
from {{ ref('stg_bill') }} b
left join {{ ref('stg_status') }} s using (status_id)
left join initiators i using (bill_id)
left join names n using (bill_id)
