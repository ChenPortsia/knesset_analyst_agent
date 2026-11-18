select
    {{ j('Id', 'int') }}         as faction_id,
    {{ j('Name', 'he') }}        as faction_name,
    {{ j('KnessetNum', 'int') }} as knesset_num,
    {{ j('StartDate', 'ts') }}   as start_date,
    {{ j('FinishDate', 'ts') }}  as finish_date,
    {{ j('IsCurrent', 'bool') }} as is_current,
    last_updated
from {{ source('raw', 'kns_faction') }}
