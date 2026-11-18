-- Items discussed in committee and plenum sessions. item_id points to a different table per
-- item_type_id (1 query, 2 bill, 4 agenda, 5 plenum session, 12 committee session,
-- 6000 secondary law, 6003 Israel law); see dim_item_type.source_table.
with items as (
    select
        'committee' as session_type, cmt_session_item_id as source_item_id,
        committee_session_id as session_id, item_id, item_type_id, ordinal, status_id, item_name
    from {{ ref('stg_cmt_session_item') }}
    union all
    select
        'plenum', plm_session_item_id,
        plenum_session_id, item_id, item_type_id, ordinal, status_id, item_name
    from {{ ref('stg_plm_session_item') }}
)

select
    left(i.session_type, 1) || ':' || i.source_item_id as session_item_key,
    i.session_type,
    i.source_item_id,
    i.session_id,
    i.item_id,
    i.item_type_id,
    t.item_type_desc,
    i.ordinal,
    i.status_id,
    i.item_name
from items i
left join {{ ref('stg_item_type') }} t using (item_type_id)
