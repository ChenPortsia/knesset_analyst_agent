select status_id, status_desc, type_id, type_desc, is_active
from {{ ref('stg_status') }}
