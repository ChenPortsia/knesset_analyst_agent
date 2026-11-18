select
    knesset_num,
    min(knesset_name)  as knesset_name,
    min(plenum_start)  as start_date,
    case when bool_or(plenum_finish is null) then null else max(plenum_finish) end as finish_date,
    bool_or(is_current) as is_current,
    count(*)           as n_sessions
from {{ ref('stg_knesset_dates') }}
group by knesset_num
