# Working rules

The full plan is in `PLAN.md`. Status per phase is in `PROGRESS.md`.

- Do one phase at a time. At the end of each phase:
  - run the tests, `ruff` and the phase notebook headless;
  - update `PROGRESS.md` (what was built, measured numbers, open issues);
  - commit, and tag `phase-N`;
  - **stop and report to the user** before starting the next phase.
- **Stop and ask the user** before:
  - any paid LLM or embedding run projected above $5;
  - any destructive database operation (drop, truncate, reset);
  - changing a decision recorded in PLAN.md §13.
- Tests never call knesset.gov.il or paid APIs. Use the recorded fixtures in `tests/fixtures/`. Live checks live in notebooks and the CLI only.
- **Dev scope:** text, embedding and enrichment steps accept `--knessets 25,26` (default from `.env` `SCOPE_KNESSETS`), so development iterates on a small slice. The full archive runs only when the user asks.
- **Secrets** live in `.env` only (gitignored). Never log API keys, passwords or full user emails.
- **Simple over clever:** plain functions, typed, short modules. Add a dependency only if it removes real code.

## Commands

- `make up` — start db, phoenix, app, ui, scheduler (creates `.env` from `.env.example` if missing)
- `make status` — `knesset status` inside the app container; `uv run knesset status` runs it natively
- `make migrate` — apply new `sql/NNN_*.sql` (also run by `status` and `sync`)
- `make sync` / `sync-full` / `sync-deletes` / `fixtures` — OData load; needs an Israeli IP, so run natively on the Mac
- `make dbt` — `knesset dbt build` (marts in schema `marts`; any dbt command: `uv run knesset dbt <args>`)
- `make registry` — rebuild `text.documents` from the synced `KNS_Document*` rows
- `make extract ARGS="--priority 1"` — download + extract Word files in the app container (LibreOffice); needs an Israeli IP (`--redo docx,doc` re-extracts done files of those types)
- `make chunk` — chunk extracted documents and resolve speakers (native)
- `make speakers` — re-resolve chunk speakers to `dim_person` in place (embeddings untouched)
- `make embed ARGS="--limit 1000"` — bge-m3 embeddings on the local GPU (native, `embed` extra); `--index` builds HNSW
- `make warm` — load the HNSW index into Postgres shared buffers (after building it; kept across restarts)
- `uv run knesset samples [--per-type 3]` — sample documents per type + markup tag counts into `eval/samples/` (for reading)
- `uv run knesset links` — enrichment tier 1 (chunks → session items, speakers → person/faction)
- `uv run knesset structure` — protocol sections (agenda items, words per speaker) and attendance lists → `text.sections`, `text.section_speakers`, `text.participants`
- `make ner ARGS="--limit 2000"` — named entities in chunks (DictaBERT-NER, local GPU, `embed` extra) → `text.chunk_entities` source `ner`; resumable via `text.chunk_ner`
- `uv run --extra embed knesset search "…" [--k 5] [--keywords-only]` — hybrid search with timings (debug)
- `uv run --extra embed knesset ask "…" [--thread <id>]` — paid: ask the agents (LangGraph; streams steps, asks back if unclear, `--thread` continues a conversation; capped by `AGENT_MAX_COST_USD` per question); notebook 07 is capped at $5
- `uv run --extra embed --with "langgraph-cli[inmem]" langgraph dev` — the graph in LangGraph Studio (`langgraph.json`)
- `uv run --extra embed knesset eval --configs baseline,haiku-specialists [--max-usd 5] [--ids …]` — paid: the eval set (`eval/questions.yaml`) under model configurations (`eval/configs.yaml`), graded in code; answers + Markdown report in `logs/eval/`; OpenAI models need `--extra openai` and `OPENAI_API_KEY`
- `uv run knesset traces [--n 10]` — recent agent questions from Phoenix: path, verdict, cost, steps, answer
- `uv run knesset topics` — load `src/knesset/enrich/topics.yaml` into `text.topics`
- `uv run knesset label` — hand-label the gold set (`eval/labeled_chunks.jsonl`)
- `uv run knesset topics-teach --n 500 [--dry-run]` — paid: LLM teacher labels (Batch API, capped by `TOPIC_BUDGET_USD`); `topics-collect`, `topics-eval`, `topics-apply`
- `make test` (DB tests need `make up`), `make lint`, `make notebooks`
