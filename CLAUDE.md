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
- `make extract ARGS="--priority 1"` — download + extract Word files in the app container (LibreOffice); needs an Israeli IP
- `make chunk` — chunk extracted documents and resolve speakers (native)
- `make speakers` — re-resolve chunk speakers to `dim_person` in place (embeddings untouched)
- `make embed ARGS="--limit 1000"` — bge-m3 embeddings on the local GPU (native, `embed` extra); `--index` builds HNSW
- `make test` (DB tests need `make up`), `make lint`, `make notebooks`
