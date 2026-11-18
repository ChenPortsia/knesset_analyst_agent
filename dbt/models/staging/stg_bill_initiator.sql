select
    {{ j('Id', 'int') }}           as bill_initiator_id,
    {{ j('BillID', 'int') }}       as bill_id,
    {{ j('PersonID', 'int') }}     as person_id,
    {{ j('IsInitiator', 'bool') }} as is_initiator,
    {{ j('Ordinal', 'int') }}      as ordinal,
    last_updated
from {{ source('raw', 'kns_billinitiator') }}
