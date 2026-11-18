select
    {{ j('Id', 'int') }}                    as bill_id,
    {{ j('KnessetNum', 'int') }}            as knesset_num,
    {{ j('Name', 'he') }}                   as bill_name,
    {{ j('TypeID', 'int') }}                as type_id,
    {{ j('TypeDesc', 'he') }}               as type_desc,
    {{ j('SubTypeID', 'int') }}             as subtype_id,
    {{ j('SubTypeDesc', 'he') }}            as subtype_desc,
    {{ j('PrivateNumber', 'int') }}         as private_number,
    {{ j('Number', 'int') }}                as bill_number,
    {{ j('CommitteeID', 'int') }}           as committee_id,
    {{ j('StatusID', 'int') }}              as status_id,
    {{ j('PostponementReasonDesc', 'he') }} as postponement_reason_desc,
    {{ j('PublicationDate', 'ts') }}        as publication_date,
    {{ j('PublicationSeriesDesc', 'he') }}  as publication_series_desc,
    {{ j('IsContinuationBill', 'bool') }}   as is_continuation_bill,
    {{ j('SummaryLaw', 'he') }}             as summary_law,
    last_updated
from {{ source('raw', 'kns_bill') }}
