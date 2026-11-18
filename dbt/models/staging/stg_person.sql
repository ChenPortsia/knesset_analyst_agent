select
    {{ j('Id', 'int') }}          as person_id,
    {{ j('FirstName', 'he') }}    as first_name,
    {{ j('LastName', 'he') }}     as last_name,
    {{ j('GenderID', 'int') }}    as gender_id,
    {{ j('GenderDesc', 'text') }} as gender_desc,
    {{ j('Email', 'text') }}      as email,
    {{ j('IsCurrent', 'bool') }}  as is_current,
    last_updated
from {{ source('raw', 'kns_person') }}
