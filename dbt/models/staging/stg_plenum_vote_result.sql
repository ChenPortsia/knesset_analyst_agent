select
    {{ j('Id', 'int') }}         as vote_result_id,
    {{ j('VoteID', 'int') }}     as vote_id,
    {{ j('MkId', 'int') }}       as person_id,
    {{ j('VoteDate', 'ts') }}    as vote_datetime,
    {{ j('ResultCode', 'int') }} as result_code,
    {{ j('ResultDesc', 'he') }}  as result_desc,
    {{ j('FirstName', 'he') }}   as first_name,
    {{ j('LastName', 'he') }}    as last_name,
    {{ j('SessionID', 'int') }}  as plenum_session_id,
    {{ j('ItemID', 'int') }}     as item_id,
    last_updated
from {{ source('raw', 'kns_plenumvoteresult') }}
