select
    {{ j('Id', 'int') }}                 as cmt_session_item_id,
    {{ j('CommitteeSessionID', 'int') }} as committee_session_id,
    {{ j('ItemID', 'int') }}             as item_id,
    {{ j('ItemTypeID', 'int') }}         as item_type_id,
    {{ j('Ordinal', 'int') }}            as ordinal,
    {{ j('StatusID', 'int') }}           as status_id,
    {{ j('Name', 'he') }}                as item_name,
    last_updated
from {{ source('raw', 'kns_cmtsessionitem') }}
