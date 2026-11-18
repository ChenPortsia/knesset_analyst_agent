select
    {{ j('Id', 'int') }}          as ministry_id,
    {{ j('Name', 'he') }}         as ministry_name,
    {{ j('IsActive', 'bool') }}   as is_active,
    {{ j('CategoryName', 'he') }} as category_name,
    {{ j('GovID', 'int') }}       as gov_id,
    last_updated
from {{ source('raw', 'kns_govministry') }}
