select
    {{ j('Id', 'int') }}              as plm_session_item_id,
    {{ j('PlenumSessionID', 'int') }} as plenum_session_id,
    {{ j('ItemID', 'int') }}          as item_id,
    {{ j('ItemTypeID', 'int') }}      as item_type_id,
    {{ j('Ordinal', 'int') }}         as ordinal,
    {{ j('StatusID', 'int') }}        as status_id,
    {{ j('Name', 'he') }}             as item_name,
    {{ j('IsDiscussion', 'int') }}    as is_discussion,
    last_updated
from {{ source('raw', 'kns_plmsessionitem') }}
