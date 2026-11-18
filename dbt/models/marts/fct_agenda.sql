select
    a.agenda_id,
    a.agenda_number,
    a.knesset_num,
    a.agenda_name,
    a.classification_desc,
    a.subtype_desc,
    a.status_id,
    s.status_desc,
    a.initiator_person_id,
    a.minister_person_id,
    a.gov_recommendation_desc,
    a.president_decision_date,
    a.committee_id,
    a.recommend_committee_id,
    a.leading_agenda_id
from {{ ref('stg_agenda') }} a
left join {{ ref('stg_status') }} s using (status_id)
