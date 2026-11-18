select
    {{ j('Id', 'int') }}                   as vote_id,
    {{ j('VoteDateTime', 'ts') }}          as vote_datetime,
    {{ j('SessionID', 'int') }}            as plenum_session_id,
    {{ j('ItemID', 'int') }}               as item_id,
    {{ j('Ordinal', 'int') }}              as ordinal,
    {{ j('VoteMethodDesc', 'he') }}        as vote_method_desc,
    {{ j('VoteStatusDesc', 'he') }}        as vote_status_desc,
    {{ j('VoteTitle', 'he') }}             as vote_title,
    {{ j('VoteSubject', 'he') }}           as vote_subject,
    {{ j('IsNoConfidenceInGov', 'bool') }} as is_no_confidence,
    {{ j('ForOptionDesc', 'he') }}         as for_option_desc,
    {{ j('AgainstOptionDesc', 'he') }}     as against_option_desc,
    last_updated
from {{ source('raw', 'kns_plenumvote') }}
