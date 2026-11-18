select
    {{ j('Id', 'int') }}            as committee_session_id,
    {{ j('Number', 'int') }}        as session_number,
    {{ j('KnessetNum', 'int') }}    as knesset_num,
    {{ j('TypeID', 'int') }}        as type_id,
    {{ j('TypeDesc', 'he') }}       as type_desc,
    {{ j('CommitteeID', 'int') }}   as committee_id,
    {{ j('StatusID', 'int') }}      as status_id,
    {{ j('StatusDesc', 'he') }}     as status_desc,
    {{ j('Location', 'he') }}       as location,
    {{ j('SessionUrl', 'text') }}   as session_url,
    {{ j('BroadcastUrl', 'text') }} as broadcast_url,
    {{ j('StartDate', 'ts') }}      as start_date,
    {{ j('FinishDate', 'ts') }}     as finish_date,
    {{ j('Note', 'he') }}           as note,
    last_updated
from {{ source('raw', 'kns_committeesession') }}
