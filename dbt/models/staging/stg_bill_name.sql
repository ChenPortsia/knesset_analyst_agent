select
    {{ j('Id', 'int') }}                 as bill_name_id,
    {{ j('BillID', 'int') }}             as bill_id,
    {{ j('Name', 'he') }}                as bill_name,
    {{ j('NameHistoryTypeDesc', 'he') }} as name_history_type_desc,
    last_updated
from {{ source('raw', 'kns_billname') }}
