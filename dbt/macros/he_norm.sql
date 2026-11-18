{#
  Hebrew text normalization: drop direction marks (LRM/RLM/ALM) and niqqud,
  map Hebrew geresh/gershayim and curly quotes to ASCII ' and ", NBSP to space,
  collapse whitespace, trim; empty → null.
#}
{% macro he_norm(expr) -%}
  nullif(btrim(regexp_replace(
    translate(
      regexp_replace({{ expr }}, '[\u200e\u200f\u061c\u0591-\u05c7]', '', 'g'),
      '׳״‘’“”' || chr(160),
      chr(39) || chr(34) || chr(39) || chr(39) || chr(34) || chr(34) || ' '
    ),
    '\s+', ' ', 'g')), '')
{%- endmacro %}
