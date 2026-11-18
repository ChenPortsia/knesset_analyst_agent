select ministry_id, ministry_name, is_active, category_name, gov_id
from {{ ref('stg_gov_ministry') }}
