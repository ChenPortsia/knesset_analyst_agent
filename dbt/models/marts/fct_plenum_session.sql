with items as (
    select plenum_session_id, count(*) as n_items
    from {{ ref('stg_plm_session_item') }}
    group by plenum_session_id
),

votes as (
    select plenum_session_id, count(*) as n_votes
    from {{ ref('stg_plenum_vote') }}
    group by plenum_session_id
)

select
    s.plenum_session_id,
    s.session_number,
    s.knesset_num,
    s.session_name,
    s.start_date,
    s.finish_date,
    s.is_special_meeting,
    coalesce(i.n_items, 0) as n_items,
    coalesce(v.n_votes, 0) as n_votes
from {{ ref('stg_plenum_session') }} s
left join items i using (plenum_session_id)
left join votes v using (plenum_session_id)
