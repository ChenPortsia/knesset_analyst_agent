select
    committee_id, committee_name, knesset_num, category_desc, committee_type_desc,
    additional_type_desc, parent_committee_id, start_date, finish_date, is_current
from {{ ref('stg_committee') }}
