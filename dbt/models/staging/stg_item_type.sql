select
    {{ j('Id', 'int') }}         as item_type_id,
    {{ j('Desc', 'he') }}        as item_type_desc,
    {{ j('TableName', 'text') }} as source_table,
    last_updated
from {{ source('raw', 'kns_itemtype') }}
