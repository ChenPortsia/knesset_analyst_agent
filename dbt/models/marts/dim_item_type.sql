-- KNS_ItemType lacks some ids that session items use; those are added here with
-- descriptions inferred from the item names (marked is_inferred).
select item_type_id, item_type_desc, source_table, false as is_inferred
from {{ ref('stg_item_type') }}
union all
select v.item_type_id, v.item_type_desc, null, true
from (values
    (9, 'נושא לדיון במליאה (לא מתועד ב-KNS_ItemType; הוסק מהנתונים)'),
    (11, 'נושא לדיון בוועדה (לא מתועד ב-KNS_ItemType; הוסק מהנתונים)'),
    (950, 'שאילתות ותשובות בעל פה במליאה (לא מתועד ב-KNS_ItemType; הוסק מהנתונים)')
) as v (item_type_id, item_type_desc)
where v.item_type_id not in (select item_type_id from {{ ref('stg_item_type') }})
