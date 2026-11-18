-- Name variants: the person's own names plus the spellings used in per-MK vote results,
-- in both orders ("first last" and "last first"), for entity resolution.
with names as (
    select person_id, first_name, last_name from {{ ref('stg_person') }}
    union
    select distinct person_id, first_name, last_name from {{ ref('stg_plenum_vote_result') }}
),

variants as (
    select person_id, array_agg(distinct v order by v) as name_variants
    from names
    cross join lateral (values
        (concat_ws(' ', first_name, last_name)),
        (concat_ws(' ', last_name, first_name))
    ) as t (v)
    where v <> ''
    group by person_id
),

terms as (  -- faction membership means the person served as an MK in that Knesset
    select person_id, array_agg(distinct knesset_num order by knesset_num) as mk_knessets
    from {{ ref('stg_person_to_position') }}
    where faction_id is not null and knesset_num is not null
    group by person_id
)

select
    p.person_id,
    p.first_name,
    p.last_name,
    concat_ws(' ', p.first_name, p.last_name) as full_name,
    p.gender_desc,
    p.is_current,
    coalesce(v.name_variants, '{}') as name_variants,
    coalesce(t.mk_knessets, '{}')   as mk_knessets,
    t.mk_knessets[1]                as first_knesset,
    t.mk_knessets[cardinality(t.mk_knessets)] as last_knesset
from {{ ref('stg_person') }} p
left join variants v using (person_id)
left join terms t using (person_id)
