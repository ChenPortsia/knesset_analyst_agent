select
    {{ j('Id', 'int') }}                   as agenda_id,
    {{ j('Number', 'int') }}               as agenda_number,
    {{ j('KnessetNum', 'int') }}           as knesset_num,
    {{ j('Name', 'he') }}                  as agenda_name,
    {{ j('ClassificationDesc', 'he') }}    as classification_desc,
    {{ j('SubTypeDesc', 'he') }}           as subtype_desc,
    {{ j('StatusID', 'int') }}             as status_id,
    {{ j('InitiatorPersonID', 'int') }}    as initiator_person_id,
    {{ j('GovRecommendationDesc', 'he') }} as gov_recommendation_desc,
    {{ j('PresidentDecisionDate', 'ts') }} as president_decision_date,
    {{ j('CommitteeID', 'int') }}          as committee_id,
    {{ j('RecommendCommitteeID', 'int') }} as recommend_committee_id,
    {{ j('MinisterPersonID', 'int') }}     as minister_person_id,
    {{ j('LeadingAgendaID', 'int') }}      as leading_agenda_id,
    last_updated
from {{ source('raw', 'kns_agenda') }}
