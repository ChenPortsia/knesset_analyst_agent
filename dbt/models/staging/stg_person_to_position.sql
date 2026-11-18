select
    {{ j('Id', 'int') }}             as person_to_position_id,
    {{ j('PersonID', 'int') }}       as person_id,
    {{ j('PositionID', 'int') }}     as position_id,
    {{ j('KnessetNum', 'int') }}     as knesset_num,
    {{ j('StartDate', 'ts') }}       as start_date,
    {{ j('FinishDate', 'ts') }}      as finish_date,
    {{ j('GovMinistryID', 'int') }}  as ministry_id,
    {{ j('GovMinistryName', 'he') }} as ministry_name,
    {{ j('DutyDesc', 'he') }}        as duty_desc,
    {{ j('FactionID', 'int') }}      as faction_id,
    {{ j('FactionName', 'he') }}     as faction_name,
    {{ j('GovernmentNum', 'int') }}  as government_num,
    {{ j('CommitteeID', 'int') }}    as committee_id,
    {{ j('CommitteeName', 'he') }}   as committee_name,
    {{ j('IsCurrent', 'bool') }}     as is_current,
    last_updated
from {{ source('raw', 'kns_persontoposition') }}
