select
    {{ j('Id', 'int') }}          as knesset_dates_id,
    {{ j('KnessetNum', 'int') }}  as knesset_num,
    {{ j('Name', 'he') }}         as knesset_name,
    {{ j('Assembly', 'int') }}    as assembly,
    {{ j('Plenum', 'int') }}      as plenum,
    {{ j('PlenumStart', 'ts') }}  as plenum_start,
    {{ j('PlenumFinish', 'ts') }} as plenum_finish,
    {{ j('IsCurrent', 'bool') }}  as is_current,
    last_updated
from {{ source('raw', 'kns_knessetdates') }}
