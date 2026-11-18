select
    {{ j('Id', 'int') }}         as position_id,
    {{ j('Description', 'he') }} as position_desc,
    {{ j('GenderID', 'int') }}   as gender_id,
    last_updated
from {{ source('raw', 'kns_position') }}
