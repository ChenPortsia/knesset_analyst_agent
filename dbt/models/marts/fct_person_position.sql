select
    pp.person_to_position_id,
    pp.person_id,
    pp.position_id,
    p.position_desc,
    pp.knesset_num,
    pp.start_date,
    pp.finish_date,
    pp.faction_id,
    pp.faction_name,
    pp.committee_id,
    pp.committee_name,
    pp.ministry_id,
    pp.ministry_name,
    pp.government_num,
    pp.duty_desc,
    pp.is_current
from {{ ref('stg_person_to_position') }} pp
left join {{ ref('stg_position') }} p using (position_id)
