# Knesset Data Platform — Build Plan

A local database of all Knesset parliamentary data (structured tables + full text from Word documents), enriched with entities, topics and sentiment, refreshed daily. On top of it: a **multi-agent** system (an orchestrator + specialist agents, each with its own tools; key specialists also served over the **A2A protocol**) for analysis, served through a **web chat UI** to a small group of invited users. Every step is logged and every agent run is traced.

Work phase by phase. Each phase ends with (a) its acceptance checks and (b) a notebook for exploring and testing its output. Don't start the next phase until both are done. Keep code simple, short and readable: small modules, no premature abstraction, no frameworks beyond those listed.

---

## 0. Working rules for Claude Code (copy into `CLAUDE.md`)

- Do one phase at a time. At the end of each phase:
  - run the tests, `ruff` and the phase notebook headless;
  - update `PROGRESS.md` (what was built, measured numbers, open issues);
  - commit, and tag `phase-N`;
  - **stop and report to the user** before starting the next phase.
- **Stop and ask the user** before:
  - any paid LLM or embedding run projected above $5;
  - any destructive database operation (drop, truncate, reset);
  - changing a decision recorded in §13.
- Tests never call knesset.gov.il or paid APIs. Use the recorded fixtures in `tests/fixtures/`. Live checks live in notebooks and the CLI only.
- **Dev scope:** text, embedding and enrichment steps accept `--knessets 25,26` (default from `.env` `SCOPE_KNESSETS`), so development iterates on a small slice. The full archive runs only when the user asks.
- **Secrets** live in `.env` only (gitignored). Never log API keys, passwords or full user emails.
- **Simple over clever:** plain functions, typed, short modules. Add a dependency only if it removes real code.

---

## 1. Context and constraints

- **Development machine:** MacBook Pro M1 Max, 32 GB, macOS. Its Apple GPU (`mps`) does the heavy one-time work: the embedding backfill and local NLP models.
- **Runtime target (after development):** either
  - a home server — a Windows 11 laptop (Intel Core Ultra 7 155U, 32 GB, ~750 GB free, integrated GPU only), running WSL2 + Docker Desktop, always on; or
  - GCP — a small VM in `me-west1` (Tel Aviv).

  Code must run unchanged on macOS, Linux and WSL2.
- **Portability rules:**
  - Everything runs in Docker Compose.
  - No macOS-only tools: no `textutil`, no `launchd`.
  - The compute device is auto-detected (`mps` → `cuda` → `cpu`).
  - All config comes from `.env`.
- **Network:** the Knesset API answers **Israeli IPs only** (foreign IPs get `303 → /maintenance-page-geo`, then an HTML 200). Detect the redirect and fail loudly. On GCP, test it first; if blocked, ingestion stays at home and the database is synced up.
- **No PDFs.** Only Word-family files (`.docx`, `.doc`, `.rtf`). PDFs are registered as `skipped_pdf` and never downloaded. No downloaded file is ever kept: download to a temp file, extract text, store it, delete the file.
- **Efficiency:** parallel but polite loading, `COPY` writes, incremental refresh, hash-based skips.
- **Observability:** structured logs for every pipeline step; traces for every LLM call, agent step and agent-to-agent handoff; all stored locally.
- **Multi-user:** a few invited users. Login required, per-user limits, nothing but the chat UI exposed.

## 2. Stack

| Concern | Choice |
| --- | --- |
| Python env | Python 3.12, `uv` |
| Containers | Docker Compose: `db`, `phoenix`, `app` (pipeline + CLI), `ui`, `scheduler` |
| Database | Postgres 17 + pgvector (`pgvector/pgvector:pg17`), named volume |
| HTTP | `httpx[http2]` async |
| DB driver | `asyncpg` (bulk loads), `psycopg` (agents, UI, notebooks) |
| Transformations | `dbt-postgres` |
| Text extraction | LibreOffice headless (`soffice --headless --convert-to "txt:Text (encoded):UTF8"`, so Hebrew comes out as UTF-8) in the `app` image; `python-docx` fast path for `.docx` |
| Embeddings | `sentence-transformers`, `BAAI/bge-m3`, fp16 on GPU, 1024 dims stored as `halfvec` |
| Local NLP | Hebrew NER and sentiment models from Hugging Face (DictaBERT family or heBERT). Verify availability and license, pick in Phase 5 by testing on labeled samples. |
| LLMs | A hosted API by default, model set per agent in `.env`. Ollama optional on the Mac only. Structured outputs validated with `pydantic`. |
| Agents | LangGraph (supervisor + specialist subgraphs) |
| Agent interop | A2A protocol via the official A2A Python SDK (pin the current version; follow the current spec for agent cards and transports) |
| UI | Chainlit, with password login (argon2-hashed) or Google OAuth, and its Postgres data layer for chat history and feedback |
| Code quality | `pytest`, `ruff` (lint + format), type hints; recorded API fixtures so tests never call knesset.gov.il |
| Logging | `structlog` → JSON lines in `logs/` + `ops.runs` |
| Tracing | Arize Phoenix (self-hosted, Postgres storage) via OpenTelemetry + OpenInference LangChain instrumentation |
| Notebooks | JupyterLab + `jupytext` pairing |
| Evaluation | RAGAS + hand-written gold set |
| Scheduling | `scheduler` container running `supercronic` (cron in Docker) |
| Remote access | Tailscale (private network) or Cloudflare Tunnel + Access. Only the UI port is reachable. |
| CLI | `typer` |

## 3. Repository layout

```
knesset-data/
├── PLAN.md
├── CLAUDE.md                # working rules (§0)
├── PROGRESS.md              # per-phase status, decisions, measured numbers
├── docker-compose.yml       # db, phoenix, app, ui, scheduler, agent-analyst, agent-researcher
├── docker-compose.gpu.yml   # optional override for local GPU runs on the Mac (native python)
├── Dockerfile               # app image: python + libreoffice
├── pyproject.toml
├── .env.example
├── Makefile                 # up, sync, dbt, extract, chunk, embed, enrich, ui, eval, notebooks, backup, restore
├── src/knesset/
│   ├── config.py            # settings + device detection
│   ├── log.py               # structlog, run_id, step timing helper
│   ├── tracing.py           # OpenTelemetry → Phoenix
│   ├── odata.py             # API client
│   ├── sync.py              # full + incremental loads
│   ├── documents.py         # registry + Word extraction
│   ├── chunks.py            # speaker-aware chunking
│   ├── embed.py
│   ├── enrich/              # structured links, NER, sentiment, LLM labels, summaries
│   ├── search.py            # hybrid retrieval + neighbor expansion
│   ├── tools/               # tool functions grouped by agent (sql.py, research.py, summary.py, verify.py, common.py)
│   ├── agents/              # state.py, orchestrator.py, analyst.py, researcher.py, summarizer.py, verifier.py, graph.py, prompts/
│   ├── a2a/                 # A2A servers (one per exposed specialist), client adapter, agent cards
│   ├── ui/                  # chainlit app.py, auth.py, rendering.py, limits.py, public/rtl.css
│   ├── nb.py                # notebook helpers
│   └── cli.py               # `knesset sync|extract|chunk|embed|enrich|ask|status|daily|backup|restore`
├── sql/                     # DDL, roles, indexes
├── dbt/
├── notebooks/
├── tests/                   # pytest; fixtures/ holds recorded API responses and sample .doc/.docx files
├── eval/                    # gold set, labeled chunks, runner, reports
├── logs/                    # gitignored
└── deploy/                  # home-server (WSL2) and GCP guides + scripts
```

## 4. Source API facts (measured live 17 Nov 2026 — design around these)

- Base: `https://knesset.gov.il/OdataV4/ParliamentInfo`, schema at `/$metadata` (OData 4.0). No auth. Read-only.
- Lobbyist sets `V_Lobbyists`, `V_LobbyistsClients` are declared in that metadata but served from `https://knesset.gov.il/OdataV4/Lobbyist`.
- 46 + 2 sets, ~3.4M rows. Biggest: `KNS_PlenumVoteResult` 1.95M, `KNS_DocumentCommitteeSession` 202k, `KNS_BillInitiator` 169k, `KNS_PlmSessionItem` 169k, `KNS_DocumentBill` 112k, `KNS_CommitteeSession` 109k.
- Integer key `Id` everywhere. Most sets have `LastUpdatedDate`. Without it: `KNS_ItemType`, `KNS_CmtSiteCode`, `KNS_MkSiteCode`, `KNS_BroadcastCommitteSession`, lobbyist sets.
- **Page size hard-capped at 100.** Page latency ~0.3–0.8 s. 8 concurrent requests completed in ~0.6–0.9 s with no errors (~10–13 pages/s) in a short burst, but a sustained load trips the WAF (HTTP 473 / 481) within ~90 s; the sustainable rate is ~2 requests/s (one at a time). The server also closes each HTTP/2 connection after 2,000 streams.
- **No `$skip`** (500 at offset 50,000), **no `@odata.nextLink`** (unreliable), **no `$expand` for loading** (children silently capped at 100 per parent).
- `$apply` and date functions like `year()` → **HTTP 473** (WAF). `$search` is ignored. `/$count` → 404; use `?$count=true&$top=0`.
- Works: `$filter` (comparisons, `contains`, ISO dates), `$orderby`, `$select`, `$count=true`, `Set(Id)`.
- `KNS_DocumentQuerie` 404s. Call `KNS_DocumentQuery`: bare JSON array, camelCase keys, datetimes without offset (Israel local time), ignores `$count`.
- `KNS_DocumentIsraelLaw` has 0 rows.
- `KNS_Document*` rows hold `FilePath` URLs on `fs.knesset.gov.il`, `GroupTypeDesc` and `ApplicationDesc` (DOC / PDF / XLS …). One parent often has several files (DOC + PDF of the same protocol).
- Polymorphic `ItemID` + `ItemTypeID` in `KNS_CmtSessionItem` / `KNS_PlmSessionItem` (1 query, 2 bill, 4 agenda, 5 plenum session, 12 committee session, 6000 secondary law, 6003 Israel law). `KNS_PlenumVote.ItemID` has no type column.
- Coverage: sessions from 1948, per-MK votes from 1982, queries from 2013.
- Reference (MIT): `github.com/zomer-g/ckan-version-tracker` — `app/services/knesset_db.py` and `app/services/knesset_tables_meta.py` (Hebrew table descriptions; copy the dict with attribution).

## 5. Database design

- `raw` — per entity set: `id int pk, doc jsonb, last_updated timestamptz, loaded_at timestamptz`.
- `ops` —
  - `sync_state`
  - `runs(run_id, step, started_at, finished_at, status, rows_in, rows_out, throughput, cost_usd, error, params jsonb)`
  - `failures`
- `text` —
  - `documents(doc_id pk, source_table, parent_type, parent_id, knesset_num, committee_id, doc_date, group_type, file_type, url, status /* pending|done|failed|skipped_pdf|skipped_other|superseded */, attempts, error, content_hash, text, n_chars, extracted_at)`
  - `chunks(chunk_id pk, doc_id, ord, text, n_tokens, chunk_hash unique, speaker_raw, person_id, meta jsonb, tsv tsvector generated ('simple'), embedding halfvec(1024))`, `unique(doc_id, ord)`
  - `chunk_enrichment(chunk_id pk, version, sentiment, sentiment_label, stance, stance_target, speech_act, topics text[], keywords text[], summary, model, created_at)`
  - `chunk_entities(chunk_id, type, surface, normalized, ref_table, ref_id, confidence, source)`
  - `chunk_items(chunk_id, item_type_id, item_id)`
  - `topics(topic_id, name_he, name_en, description, parent_id)`
  - `document_summaries(doc_id pk, summary, key_points jsonb, main_topics, participants, model, version)`
  - view `v_chunks` (chunk + enrichment + entity arrays + doc/session fields)
- `marts` — dbt output.
- `app` —
  - `users(user_id, email, display_name, role /* admin|member */, daily_question_limit, monthly_budget_usd, active)`
  - `usage(user_id, day, questions, tokens_in, tokens_out, cost_usd)`
  - `feedback(id, user_id, thread_id, message_id, trace_id, rating, comment, created_at)`
  - Chainlit data-layer tables for threads, steps and elements, in the same database.
- Indexes after bulk loads:
  - GIN: `tsv`, `topics`, `keywords`
  - HNSW (`halfvec_cosine_ops`): `embedding`
  - btree: `chunks(doc_id, ord)`, `chunk_entities(type, ref_id)`, `chunk_entities(normalized)`, `chunk_items(item_type_id, item_id)`, `documents(status)`, `documents(parent_type, parent_id)`
- Roles:
  - `etl` — read/write.
  - `agent_ro` — SELECT on `marts`, `text`; 15 s statement timeout.
  - `ui_app` — read/write on `app` only.

## 6. Observability

- **Logs:**
  - `structlog` JSON lines to `logs/<step>/<date>.jsonl`, rotated daily, kept 90 days.
  - Every line carries `run_id`, `step` and the item key. In the UI it also carries `user_id` and `thread_id`.
  - One `ops.runs` row per pipeline step; per-item errors go to `ops.failures`.
- **Traces (Phoenix, `localhost:6006`):**
  - Every LLM call, tool call, agent step and **handoff between agents** is a span.
  - One user question = one trace, with a child span per agent turn. Attributes: `user_id`, `thread_id`, `agent`, `run_id`, tokens, cost.
  - Pipeline steps are spans too.
- **Cost tracking:** per run, per user and per agent, in `ops.runs`, `app.usage`, and the ops/usage notebooks.
- **Security:** Phoenix and Jupyter are bound to localhost or Tailscale only, never exposed publicly.

## 7. Multi-agent design

A **supervisor (orchestrator) graph** in LangGraph delegates to specialist agents. Each specialist is its own subgraph with its own system prompt, its own tools, and its own model setting (`src/knesset/agents/agents.yaml`). Agents communicate only through the shared state and explicit handoffs, never by calling each other's tools.

### Agents and their tools

| Agent | Role | Tools (only these) |
| --- | --- | --- |
| **Orchestrator** | Understands the question, decides what to do, delegates, merges findings, writes the final answer in the user's language | `plan(question)` (writes a task plan to state), `delegate(agent, task, context)`, `ask_user(question)` (clarification), `resolve_entity(name)`, `finish(answer)` |
| **Analyst** (structured data) | Answers anything countable or factual from `marts` | `describe_schema(topic)`, `sql_query(sql)`, `resolve_entity(name)`, `make_chart(data, spec)` (returns a chart spec for the UI) |
| **Researcher** (text) | Finds what was said, by whom, where | `search_text(query, filters, k, neighbors)`, `get_document(doc_id, around_chunk, window)`, `facets(by, filters)`, `resolve_entity(name)` |
| **Summarizer** | Broad themes, trends and comparisons over many documents | `facets(by, filters)`, `summarize(filters, question, max_items)` (map-reduce over document or chunk summaries), `get_document_summary(doc_id)` |
| **Verifier** | Checks the draft answer: every claim backed by a citation or SQL result, numbers match, nothing invented | `get_chunks(chunk_ids, neighbors)`, `rerun_sql(sql)`, `verdict(status, issues)` |

`resolve_entity` is shared: it maps names and aliases to person, faction, committee, bill or law ids, so every filter uses ids.

### Shared state (`agents/state.py`)

- `question`, `user_id`, `thread_id`, `ui_filters` (Knesset, committee, dates from the UI sidebar)
- `plan`: a list of tasks `{id, agent, instruction, status, depends_on}`
- `findings`: a list `{task_id, agent, claim, evidence: [{type: chunk|sql|doc_summary, ref, url}], confidence}`
- `artifacts`: SQL queries, tables, chart specs
- `draft_answer`, `verifier_report`
- `budget`: steps used and remaining, tokens used, cost used
- `messages`: for chat history

### Flow

1. The orchestrator writes a plan. Simple questions get a one-task plan.
2. It delegates tasks, running independent ones in parallel (e.g. Analyst + Researcher). A dependent task waits, e.g. the Analyst first resolves the vote ids, then the Researcher searches the debate before that vote.
3. Each specialist returns structured `findings`, not free text.
4. The orchestrator drafts the answer from the findings only.
5. The Verifier checks the draft. On `fail`, the orchestrator gets the issues and can re-delegate **once**, then answers with explicit caveats.
6. The final answer has citations (chunk URLs, document links, SQL shown on demand) and any charts or tables.

### Guardrails

- Max 12 agent steps and max 2 re-delegations per question, a per-question token and cost cap, and a 150 s overall timeout (90 s until 24 Nov 2026). When a limit is hit, answer with what's known and say so.
- SQL runs only as `agent_ro`: SELECT only, auto `LIMIT 200`.
- Agents never see other users' data. Chat history is per thread.
- **A2A-ready contract:** every specialist exposes one entry point `run(task: AgentTask) -> AgentResult`, where:
  - `AgentTask` = instruction + context + filters + budget;
  - `AgentResult` = findings + artifacts + usage;
  - both are pydantic models, JSON-serializable.

  The orchestrator calls specialists only through an `AgentClient` interface with two implementations: `LocalClient` (in-process) and `A2AClient` (remote, Phase 8). Which one each agent uses is set in `.env` (`AGENT_<NAME>_MODE=local|a2a`).

## 8. UI (Chainlit)

- **Login:** password auth from `app.users`, or Google OAuth restricted to invited emails. Roles: `admin` and `member`.
- **Chat:** streamed answer, Hebrew right-to-left support via `public/rtl.css`, auto-detected per message direction.
- **Agent steps panel:** each agent's turn is shown as a collapsible Chainlit Step (agent name, task, tool calls, timing), so users see how the answer was built.
- **Sources:** citations are clickable side elements showing the matched chunk with its before and after neighbors highlighted, document metadata and a link to the original file on the Knesset site.
- **SQL and data:** generated SQL in a collapsible code block, result tables (paged), charts rendered with Plotly from the Analyst's chart specs, CSV download of results.
- **Filters sidebar (chat settings):** Knesset number, committee, date range and document type. These are passed to the agents as `ui_filters`.
- **Feedback:** thumbs up/down + an optional comment on each answer, stored in `app.feedback` with the Phoenix `trace_id`.
- **History:** per-user threads stored in Postgres; resume past conversations.
- **Limits:** per-user daily question cap and monthly budget, checked before each run. A friendly message when a limit is reached. The admin gets an alert when total monthly cost passes a threshold.
- **Admin view (admin role only):** usage per user, cost, feedback list with links to traces.
- **Privacy:** a short notice on the login page that questions are stored and traced.

## 9. Notebooks (one per phase, required)

Helpers in `src/knesset/nb.py`:
- `q(sql)`, `show_doc`, `show_chunk(chunk_id, neighbors=1)`, `search`, `facets`
- `runs`, `failures`
- `ask(question, user="notebook")` — runs the full multi-agent graph and returns state
- `trace_link`

Every notebook runs top to bottom, has parameter cells at the top, and is paired with jupytext. `make notebooks` executes them headless.

| Notebook | Contents |
| --- | --- |
| `00_setup` | Connections, roles, Phoenix reachable, device detected, sample log + trace |
| `01_odata_load` | Counts vs live `$count`, `sync_state`, throughput, raw sampling, resume test |
| `02_marts` | Browse marts, dbt test results, sample analyses |
| `03_documents` | Status by Knesset and type, **PDF-only gap report**, failures, random reader, text-quality checks |
| `04_chunks_embeddings` | Chunk sizes, a document's chunks in order, speaker match rate, vector vs keyword vs hybrid, neighbor expansion, throughput |
| `05_enrichment` | Entity, topic, sentiment, stance and speech-act distributions; filter playground; quality vs the labeled sample; cost per 1,000 chunks; labeling cell |
| `06_tools` | Call every tool directly with sample inputs; latency per tool; check outputs against expected shapes |
| `07_agents` | Run each specialist alone on a task; run the full graph; inspect plan, findings, verifier report, handoffs; trace links; compare models per agent |
| `08_a2a` | Fetch agent cards, call the remote Analyst and Researcher with a raw A2A client, stream a task, run the same questions local vs A2A and compare latency, cost and answers; one trace across services |
| `09_usage` | UI users, questions, cost per user and agent, feedback with trace links |
| `10_eval` | Latest report, failures by agent, local vs A2A comparison, run comparison |
| `11_ops` | Runs history, durations, failures, log tail by run_id, nightly health |
| `12_deploy` | Post-restore checks on the target host: row counts match, search and agents work, backups listed |

## 10. Phases

### Phase 0 — Scaffold and observability
- `uv` project, `Dockerfile` (Python + LibreOffice), `docker-compose.yml` (db, phoenix, app, ui placeholder, scheduler), `Makefile`, `.env.example`, `sql/000_init.sql`.
- `config.py` with device detection, `log.py`, `tracing.py`, `knesset status`.
- `pytest` + `ruff` configured, `CLAUDE.md` (from §0), `PROGRESS.md`, and a GitHub Actions workflow that builds the images and runs tests on Linux.
- GPU-heavy commands (`embed`, local NLP) can also run natively on the Mac (`uv run knesset embed`) against the Dockerized database, because Docker on macOS can't use the Apple GPU.
- **Done when:** `make up && knesset status` works on a clean clone (macOS, and Linux/WSL2 via CI or a test container), a test span shows in Phoenix, and notebook 00 runs.

### Phase 1 — Structured load
- `odata.py`
  - `get_metadata()` parses `$metadata` into `[(set_name, url_name, base_url, columns, has_last_updated)]`, applying the overrides in §4.
  - One shared `httpx.AsyncClient` (HTTP/2) and one global `Semaphore(CONCURRENCY=8)`.
  - Backoff on 5xx / 473 / timeouts. Halve concurrency on 473.
  - Detect the geo-block redirect and raise a clear error.
  - Record a small set of real responses into `tests/fixtures/` once, including the quirky `KNS_DocumentQuery` and lobbyist endpoints, for offline tests.
- `sync.py`
  - **Full load:** Id-range partitions (8 for tables > 100k rows). Keyset paging `Id gt last and Id le hi`, ordered by `Id`, `$top=100`. Buffer 10 pages, then `COPY` to temp + upsert. Checkpoint per partition. All tables run concurrently under the global semaphore.
  - **Incremental:** `LastUpdatedDate gt watermark - 48h`. Tables without it are re-walked fully.
  - **Deletes:** weekly re-walk of small tables into `raw._deleted`.
- **Done when:**
  - The full load takes under ~4.5 h. (Was ~1 h; changed 18 Nov 2026: the Knesset WAF tolerates only ~2 requests/s from one IP, see PROGRESS.md.)
  - Counts match the live `$count` within 0.5%.
  - A kill-and-restart resumes without duplicates.
  - An incremental run takes minutes.
  - Notebook 01 runs.

### Phase 2 — dbt models
- Staging casts, Hebrew normalization, timezone handling.
- Marts:
  - `dim_person` (with name variants), `fct_person_position`, `dim_faction`, `dim_committee`
  - `fct_committee_session`, `fct_plenum_session`, `fct_session_item`
  - `fct_bill`, `fct_vote`, `fct_vote_result` (with faction at vote time)
  - `fct_query`, `fct_agenda`, `dim_knesset`, `dim_status`, `dim_item_type`, `dim_ministry`
- Tests on keys, relationships and accepted values. Bilingual `schema.yml` descriptions (they feed the Analyst prompt).
- **Done when:** `dbt build` passes and notebook 02 runs.

### Phase 3 — Document text (Word only)
- **Registry:** union all `KNS_Document*` rows into `text.documents` with parent ids, Knesset, committee and date.
- **File selection per parent and document type:**
  - Prefer `.docx` > `.doc` > `.rtf`. Mark the others `superseded`.
  - PDF-only parents → `skipped_pdf`. Non-Word files → `skipped_other`.
  - Confirm the format with the magic bytes after download.
- **Priority:**
  1. committee protocols, Knessets 25–26
  2. plenum protocols, Knessets 25–26
  3. bill texts, Knessets 25–26
  4. older Knessets, newest first
- **Extraction:**
  - Async download (concurrency 8) to a temp file, deleted in `finally`.
  - `.docx` via `python-docx`. `.doc` and `.rtf` via LibreOffice headless in a process pool (4–6 workers, with a separate user profile directory per worker to avoid lock conflicts).
  - Clean: normalize whitespace and Hebrew punctuation, drop repeated headers and footers, keep speaker lines.
  - Store `text`, `n_chars`, `content_hash`. Max 3 attempts.
- **Updates:** protocols are often published days or weeks after the session, and documents are sometimes replaced. The registry is rebuilt from the synced `KNS_Document*` rows on every run:
  - new rows → `pending`;
  - rows whose `LastUpdatedDate` or URL changed → re-extract;
  - if the new `content_hash` differs, the document's chunks, embeddings and enrichment are invalidated and rebuilt.
- **Done when:**
  - ≥ 98% of priority-1 Word documents are done.
  - A sample of 20 reads correctly.
  - The PDF gap is quantified.
  - Notebook 03 runs.
- Review the gap with the user.

### Phase 4 — Chunking and embeddings
- **Chunking:** speaker-turn chunking for protocols (~500 tokens, ~60 overlap, never mid-sentence); paragraph windows for other documents. Contiguous `ord` per document; bge-m3 tokenizer.
- **Speaker resolution:** match to `dim_person` using the protocol's own attendance list (the header section listing committee members and invitees) first, then name variants and positions active on the session date. Store `person_id` + confidence.
- **Hebrew keyword search caveat:** Postgres has no Hebrew stemmer, and Hebrew attaches prefixes to words (ו, ה, ב, ל, מ, ש, כ), so `'simple'` keyword search misses inflected forms. Mitigations:
  - also index a prefix-stripped copy of the text in a second tsvector;
  - add `pg_trgm` on entity and name fields for fuzzy lookup;
  - rely on the vector side for recall.

  Measure the effect in notebook 04.
- **Embedding:** bge-m3 on the auto-detected device, fp16 on GPU, normalized, batch 32–64 sorted by length. Skip by `chunk_hash`. Benchmark 1,000 chunks first. **Run the backfill natively on the Mac.** HNSW after the bulk embed.
- **Done when:** priority-1 chunks are embedded, the speaker match rate is reported, and notebook 04 runs.

### Phase 5 — Enrichment
1. **Structured links:** session items → `chunk_items`; speaker → person entity + faction and party at that date.
2. **Local models (on the Mac for the backfill):** Hebrew NER → entities linked to marts ids (exact / alias / fuzzy, with confidence); sentiment score and label. Models chosen by testing on the labeled sample.
3. **LLM structured extraction (priority set first):**
   - Send several consecutive chunks of the same document per call (e.g. 5), with one shared context window, to cut repeated context tokens. Returns a list of results, one per chunk. pydantic-validated output per chunk: `topics` (1–3 from `text.topics`), `keywords`, `stance` + `stance_target`, `speech_act`, Hebrew one-sentence `summary`, named bills and laws.
   - Batch API where available. Benchmark 500 chunks and report the projected cost; **the user confirms before the large run.**
4. **Topic taxonomy:** seeded from `KNS_IsraelLawClassificiation` and committee domains, ~30–60 topics in two levels. Reviewed by the user in notebook 05.
5. **Document summaries:** map-reduce over chunk summaries.
6. **Quality:** ~150 user-labeled chunks in `eval/labeled_chunks.jsonl`. Precision and recall per field.
- Every output records `version` + `model`.
- **Done when:** tiers 1–2 cover all chunks, tier 3 covers the priority set, quality and cost are reported, and notebook 05 runs.

### Phase 6 — Retrieval layer and tools
- `search.py`
  - Hybrid: vector top-50 + keyword top-50, merged with reciprocal rank fusion.
  - Filters: Knesset, committee, dates, person, faction, entity, topic, sentiment range, stance, speech act, bill/item.
  - **Neighbor expansion:** `neighbors=0|1|2` fetches `ord ± n` in one query, merges overlapping windows, marks `hit` / `before` / `after`. Default `1` for protocols.
  - Returns `url`, `doc_id`, `chunk_id`.
- `tools/`: implement every tool in §7 as a plain, typed, unit-tested Python function first (no LLM), then wrap them as LangChain tools. Each tool:
  - returns compact JSON with an `evidence` list;
  - truncates long text;
  - logs and traces its call.
- **Done when:** unit tests pass, every tool returns correctly shaped output in notebook 06, and hybrid search with neighbors takes under 300 ms on Knessets 25–26.

### Phase 7 — Multi-agent system
- Build each specialist subgraph and test it alone (notebook 07) before wiring the supervisor.
- Then the orchestrator: plan → delegate (parallel where independent) → collect findings → draft → verify → finish, with the guardrails in §7.
- Prompts live in `agents/prompts/*.md` (versioned). Hebrew and English few-shot examples per agent; the Analyst gets 10–15 question→SQL pairs.
- `knesset ask "…"` CLI streams the agent steps.
- **Done when:**
  - Each specialist passes its own smoke tasks.
  - The full graph answers 10 mixed questions with citations within the limits.
  - The verifier catches at least the planted errors in a small test.
  - Every handoff is visible in Phoenix.
  - Notebook 07 runs.

### Phase 8 — A2A layer
- Wrap the **Analyst** and **Researcher** as A2A servers using the official A2A Python SDK. Each one:
  - runs as its own docker compose service (`agent-analyst`, `agent-researcher`);
  - serves an agent card at the spec's well-known path, describing its skills, input and output;
  - accepts a task whose structured data part is `AgentTask`, and returns `AgentResult` as a structured artifact;
  - supports streaming progress updates.
- `A2AClient` implements the same `AgentClient` interface as `LocalClient`. The orchestrator code doesn't change; only `AGENT_<NAME>_MODE` does.
- **Auth:** a bearer token per agent from `.env`. Agents listen only on the internal docker network unless explicitly published.
- **Tracing:** propagate the W3C `traceparent` header on every A2A call, so one question remains one trace in Phoenix across services.
- **Interop check:** call each agent from a generic A2A client (not our orchestrator) to prove it works for outside callers.
- **Done when:**
  - Both agents serve valid agent cards.
  - The full graph gives the same answers in `local` and `a2a` mode on the 10 smoke questions.
  - Latency and cost overhead are measured.
  - Traces are stitched across services.
  - Notebook 08 runs.

### Phase 9 — UI
- Build the Chainlit app as described in §8: login, chat with streaming, agent steps, sources with neighbors, SQL/tables/charts, filters sidebar, feedback, history, limits, admin view, RTL.
- Passwords are stored argon2-hashed. `knesset user add|disable|set-limit` CLI for the admin.
- `ui` service in docker compose, port 8000, bound to localhost by default.
- **Done when:**
  - Two test users can log in and chat with separate histories.
  - Limits block correctly.
  - Feedback links to traces.
  - Hebrew renders right to left.
  - Agent steps show which agents ran (local or A2A).
  - Notebook 09 shows usage.

### Phase 10 — Evaluation
- `eval/gold.yaml`, ~50 questions in Hebrew and English: 15 SQL, 15 text, 10 filtered/summarized, 10 combined (needing more than one agent).
- Claude Code drafts candidate questions and expected answers from the data (SQL answers computed, text answers with the chunk ids that support them). **The user reviews and approves every item** before it counts.
- Metrics:
  - end to end: answer correctness, RAGAS faithfulness / relevancy / context precision-recall, latency, cost
  - per agent: Analyst SQL execution accuracy; Researcher retrieval recall@k; Summarizer coverage; Verifier catch rate on planted errors
  - routing: the expected agents were used, the expected filters applied
  - comparisons: neighbors 0 vs 1; local vs A2A mode
- `make eval` writes a dated report and tags the traces with the eval run id.
- **Done when:** a baseline report exists and notebook 10 runs.

### Phase 11 — Daily refresh
- The `scheduler` container (supercronic) runs `knesset daily` at 03:30. Steps:
  1. incremental sync
  2. `dbt build`
  3. rebuild the document registry
  4. extract new or changed Word documents
  5. chunk + embed
  6. enrichment tiers 1–3
  7. document summaries
- On a CPU-only host, embedding and NER of the daily increment run on CPU (the volume is small).
- Each step is logged and traced; a failed step doesn't block independent ones; the run ends with a summary. The admin gets an alert (email or Telegram) on failure.
- **Done when:** two consecutive nightly runs succeed and new sessions are searchable the next morning (notebook 11).

### Phase 12 — Migration, deployment and remote access
- **Backup and restore:** `make backup` (`pg_dump -Fc` of the knesset DB + the Phoenix DB + `.env` template) and `make restore`. Test a restore on a fresh machine. Vectors and enrichments move with the dump; nothing is recomputed.
- **Option A — home server (Windows laptop):**
  - `deploy/home-server.md`:
    - WSL2 + Docker Desktop (start at login);
    - `.wslconfig` giving WSL ~20 GB RAM and enough swap;
    - power plan: never sleep, lid-close does nothing;
    - BIOS "power on after AC loss";
    - auto-login + Docker autostart.
  - Restore the dump. Tailscale on the host. Friends join the tailnet and see only the UI (Tailscale ACL limited to port 8000). Phoenix, Jupyter and the A2A agents stay internal.
  - Or Cloudflare Tunnel + Access with an email allowlist.
- **Option B — GCP:**
  - `deploy/gcp.md`: an `e2-standard-4`-class VM in `me-west1` with Docker, persistent disk, nightly snapshots, budget alert.
  - First test whether `knesset.gov.il` answers from that VM. If not, ingestion runs at home and pushes a nightly dump or incremental sync to GCP.
  - Expose the UI through Cloudflare Tunnel or Identity-Aware Proxy, never an open port.
- **Done when:**
  - The restore is tested.
  - The chosen host runs the nightly job, the UI and the A2A agents.
  - Two friends can log in remotely.
  - Backups run weekly.
  - Notebook 12 runs.

## 11. Efficiency rules

- One shared HTTP client and one global concurrency limit (≤ 8 parallel requests to knesset.gov.il).
- Keyset paging on `Id` only. No `$skip`, no `$expand` for loading.
- `COPY` + upsert. Indexes after bulk loads.
- Checkpoint every batch; every step is resumable and idempotent.
- Skip by hash and version for extraction, embedding and enrichment.
- Run cheap tiers on everything, expensive tiers on the priority set first. Always report the projected cost before a large paid run.
- Heavy GPU work happens once, on the Mac; runtime hosts handle only increments.
- Agents: findings as compact JSON, truncated tool outputs, per-question budgets, a cheaper model where it suffices (set per agent).

## 12. Out of scope for now

- PDF-only documents (revisit after the Phase 3 gap report).
- Research center (ממ"מ) documents (not in the API; separate scraper later).
- Broadcast video and audio.
- Public internet access to the UI (invited users only).

## 13. Decisions log (record in `PROGRESS.md` when made)

| Decision | Default until decided | Decide by |
| --- | --- | --- |
| LLM provider and model per agent | One hosted provider; a cheaper model for the Analyst and Verifier, a stronger one for the Orchestrator and Summarizer | Start of Phase 5 |
| Embedding model | bge-m3 locally on the Mac | Phase 4 benchmark |
| NER / sentiment models | Pick the best on the labeled sample | Phase 5 |
| Topic taxonomy | Seed list, user-reviewed | Phase 5, before tier 3 |
| Handle PDF-only documents? | No | After the Phase 3 gap report |
| Which agents run over A2A | Analyst + Researcher | Phase 8 |
| UI auth method | Password (argon2) | Phase 9 |
| Runtime host | Windows home server | Phase 12 |
