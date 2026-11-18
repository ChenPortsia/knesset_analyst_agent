select item_type_id, item_type_desc, source_table
from {{ ref('stg_item_type') }}
