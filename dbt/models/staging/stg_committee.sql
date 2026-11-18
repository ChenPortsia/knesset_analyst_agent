select
    {{ j('Id', 'int') }}                as committee_id,
    {{ j('Name', 'he') }}               as committee_name,
    {{ j('CategoryID', 'int') }}        as category_id,
    {{ j('CategoryDesc', 'he') }}       as category_desc,
    {{ j('KnessetNum', 'int') }}        as knesset_num,
    {{ j('CommitteeTypeID', 'int') }}   as committee_type_id,
    {{ j('CommitteeTypeDesc', 'he') }}  as committee_type_desc,
    {{ j('AdditionalTypeDesc', 'he') }} as additional_type_desc,
    {{ j('ParentCommitteeID', 'int') }} as parent_committee_id,
    {{ j('StartDate', 'ts') }}          as start_date,
    {{ j('FinishDate', 'ts') }}         as finish_date,
    {{ j('IsCurrent', 'bool') }}        as is_current,
    last_updated
from {{ source('raw', 'kns_committee') }}
