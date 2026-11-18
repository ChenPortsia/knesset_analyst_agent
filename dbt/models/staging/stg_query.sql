select
    {{ j('Id', 'int') }}               as query_id,
    {{ j('Number', 'int') }}           as query_number,
    {{ j('KnessetNum', 'int') }}       as knesset_num,
    {{ j('Name', 'he') }}              as query_name,
    {{ j('TypeID', 'int') }}           as type_id,
    {{ j('TypeDesc', 'he') }}          as type_desc,
    {{ j('StatusID', 'int') }}         as status_id,
    {{ j('PersonID', 'int') }}         as person_id,
    {{ j('GovMinistryID', 'int') }}    as ministry_id,
    {{ j('SubmitDate', 'ts') }}        as submit_date,
    {{ j('ReplyMinisterDate', 'ts') }} as reply_minister_date,
    {{ j('ReplyDatePlanned', 'ts') }}  as reply_date_planned,
    last_updated
from {{ source('raw', 'kns_query') }}
