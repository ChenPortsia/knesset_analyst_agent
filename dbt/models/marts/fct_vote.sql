with results as (
    select
        vote_id,
        count(*) filter (where result_desc = 'בעד')  as n_for,
        count(*) filter (where result_desc = 'נגד')  as n_against,
        count(*) filter (where result_desc = 'נמנע') as n_abstain,
        count(*) filter (where result_desc = 'נוכח') as n_present,
        count(*)                                     as n_results
    from {{ ref('stg_plenum_vote_result') }}
    group by vote_id
)

select
    v.vote_id,
    v.vote_datetime,
    v.plenum_session_id,
    v.item_id,
    v.ordinal,
    v.vote_title,
    v.vote_subject,
    v.vote_method_desc,
    v.vote_status_desc,
    v.is_no_confidence,
    v.for_option_desc,
    v.against_option_desc,
    coalesce(r.n_for, 0)     as n_for,
    coalesce(r.n_against, 0) as n_against,
    coalesce(r.n_abstain, 0) as n_abstain,
    coalesce(r.n_present, 0) as n_present,
    coalesce(r.n_results, 0) as n_results
from {{ ref('stg_plenum_vote') }} v
left join results r using (vote_id)
