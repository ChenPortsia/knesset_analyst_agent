select faction_id, faction_name, knesset_num, start_date, finish_date, is_current
from {{ ref('stg_faction') }}
