-- One row per MK per vote, with the MK's faction at the time of the vote: the faction
-- membership (person_to_position with a faction) whose period covers the vote date;
-- if periods overlap, the one that started last.
with memberships as (
    select person_id, faction_id, faction_name, start_date, finish_date
    from {{ ref('stg_person_to_position') }}
    where faction_id is not null
)

select distinct on (r.vote_result_id)
    r.vote_result_id,
    r.vote_id,
    r.person_id,
    r.vote_datetime,
    r.result_code,
    r.result_desc,
    r.plenum_session_id,
    r.item_id,
    m.faction_id,
    m.faction_name
from {{ ref('stg_plenum_vote_result') }} r
left join memberships m
    on m.person_id = r.person_id
   and r.vote_datetime >= m.start_date
   and r.vote_datetime < coalesce(m.finish_date + interval '1 day', 'infinity')
order by r.vote_result_id, m.start_date desc nulls last
