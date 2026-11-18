with items as (
    select committee_session_id, count(*) as n_items
    from {{ ref('stg_cmt_session_item') }}
    group by committee_session_id
)

select
    s.committee_session_id,
    s.session_number,
    s.knesset_num,
    s.committee_id,
    c.committee_name,
    s.type_desc,
    s.status_desc,
    s.location,
    s.start_date,
    s.finish_date,
    s.session_url,
    s.broadcast_url,
    s.note,
    coalesce(i.n_items, 0) as n_items
from {{ ref('stg_committee_session') }} s
left join {{ ref('stg_committee') }} c using (committee_id)
left join items i using (committee_session_id)
