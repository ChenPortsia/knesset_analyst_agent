select
    {{ j('Id', 'int') }}        as status_id,
    {{ j('Desc', 'he') }}       as status_desc,
    {{ j('TypeID', 'int') }}    as type_id,
    {{ j('TypeDesc', 'he') }}   as type_desc,
    {{ j('IsActive', 'bool') }} as is_active,
    last_updated
from {{ source('raw', 'kns_status') }}
