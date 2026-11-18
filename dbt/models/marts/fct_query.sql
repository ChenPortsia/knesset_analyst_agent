select
    q.query_id,
    q.query_number,
    q.knesset_num,
    q.query_name,
    q.type_desc,
    q.status_id,
    s.status_desc,
    q.person_id,
    q.ministry_id,
    m.ministry_name,
    q.submit_date,
    q.reply_minister_date,
    q.reply_date_planned
from {{ ref('stg_query') }} q
left join {{ ref('stg_status') }} s using (status_id)
left join {{ ref('stg_gov_ministry') }} m using (ministry_id)
