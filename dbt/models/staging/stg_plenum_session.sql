select
    {{ j('Id', 'int') }}                as plenum_session_id,
    {{ j('Number', 'int') }}            as session_number,
    {{ j('KnessetNum', 'int') }}        as knesset_num,
    {{ j('Name', 'he') }}               as session_name,
    {{ j('StartDate', 'ts') }}          as start_date,
    {{ j('FinishDate', 'ts') }}         as finish_date,
    {{ j('IsSpecialMeeting', 'bool') }} as is_special_meeting,
    last_updated
from {{ source('raw', 'kns_plenumsession') }}
