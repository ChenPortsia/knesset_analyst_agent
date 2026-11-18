{#
  Typed extraction from raw.<set>.doc (the OData row as jsonb).
  Source datetimes are Israel local time with an offset ("2024-07-28T21:41:35+03:00");
  ::timestamp keeps that wall-clock time and drops the offset, so every mart timestamp
  is Israel local time (timestamp without time zone).
#}
{% macro j(col, type='text') -%}
  {%- set v = "doc->>'" ~ col ~ "'" -%}
  {%- if type == 'int' -%} ({{ v }})::bigint
  {%- elif type == 'bool' -%} ({{ v }})::boolean
  {%- elif type == 'ts' -%} ({{ v }})::timestamp
  {%- elif type == 'date' -%} ({{ v }})::date
  {%- elif type == 'he' -%} {{ he_norm(v) }}
  {%- else -%} nullif(btrim({{ v }}), '')
  {%- endif -%}
{%- endmacro %}
