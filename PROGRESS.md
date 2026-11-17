# Progress

## Phase 0 — Scaffold and observability ✅ (tag `phase-0`)

**Built**
- `uv` project (Python 3.12), `pyproject.toml` with ruff + pytest config, `uv.lock`.
- `Dockerfile`: python:3.12-slim-trixie + LibreOffice Writer (headless) + tzdata + supercronic v0.2.33; deps installed with `uv sync --locked`.
- `docker-compose.yml`: `db` (pgvector/pgvector:pg17, named volume `pgdata`), `phoenix` (arizephoenix/phoenix:version-21.1.0, Postgres storage in its own `phoenix` database), `app` (idle; `docker compose exec app knesset …`), `ui` (static placeholder on :8000), `scheduler` (supercronic, `deploy/crontab`, Israel time). Every port binds to 127.0.0.1. db and phoenix have healthchecks, so `make up` (`--wait`) returns only when they are ready.
- `sql/000_init.sql` (runs on first start of an empty volume): roles `etl`, `agent_ro` (read-only, 15 s statement timeout), `ui_app`, `phoenix`; extensions `vector`, `pg_trgm`; schemas `raw`, `ops`, `text`, `marts` (owned by etl), `app` (owned by ui_app); `ops.sync_state`, `ops.runs`, `ops.failures`. Passwords come from `.env` via psql `\getenv`.
- `src/knesset/`: `config.py` (settings from `.env`, device detection mps → cuda → cpu, cpu when torch is absent), `db.py`, `log.py` (structlog JSON lines to `logs/<step>/<date>.jsonl`, 90-day pruning, `run()` helper → run_id on every line, one span and one `ops.runs` row per step), `tracing.py` (Phoenix OTLP/HTTP, trace check, trace links), `nb.py` (`q`, `runs`, `failures`, `trace_link`), `cli.py` (`knesset status`).
- `knesset status` checks db + pgvector + schemas + roles, `agent_ro` login, Phoenix health, device and scope, then emits a test span and confirms Phoenix received it. Exit code 1 on any failure.
- Tests (8, offline), notebook `00_setup`, GitHub Actions (`test` job: ruff + pytest; `stack` job: `make up`, `make status`, native `uv run knesset status`, `make notebooks`).

**Measured (Linux container session)**
- `knesset status`: all checks ok; test span visible in Phoenix with its child span (`/redirects/traces/<id>` opens it).
- pytest: 8 passed in 0.3 s. ruff: clean. Notebook 00 runs headless.

**Notes and open issues**
- Phoenix 21 turns auth on by default, which silently rejected spans with 401. Set `PHOENIX_ENABLE_AUTH=false`, acceptable because Phoenix is bound to localhost / Tailscale only (PLAN §6). `knesset status` now fails if the test span does not arrive.
- The app image could not be built inside this dev session (its network blocks Debian mirrors and GitHub release downloads). The CI `stack` job verifies it on a clean Linux clone: image build, all 5 services healthy, `make status` in the container, native `uv run knesset status`, notebook 00 — green on run 3. macOS (M1, Docker Compose v2.6.0): `make status` and native `uv run knesset status` all ok, trace received.
- CI found two bugs, both fixed: supercronic must be started by absolute path (it re-execs `argv[0]`); containers ran as root and left root-owned files in the bind-mounted `logs/`, breaking native runs on Linux/WSL2 — app containers now run as the host user (`HOST_UID`/`HOST_GID`, exported by the Makefile).
- supercronic is downloaded per `TARGETARCH` without a checksum pin (one checksum per arch); revisit if needed.
- `sql/000_init.sql` only runs on an empty volume. Later DDL (Phase 1+) will need a small apply step (`knesset db migrate`), not a volume reset.
- `make up` no longer uses `--wait`: Compose v2.6 waits forever on services without a healthcheck. `depends_on: service_healthy` already holds app/ui/scheduler until db and phoenix are ready. Docker Desktop on the Mac (Compose v2.6.0, 2022) should be updated.
- Device shows `cpu` natively on the Mac because torch is not installed yet; the `gpu` extra (torch → `mps`) is added in Phase 4.
- The trace link printed inside the container uses the internal host `phoenix:6006`; open the same path on `localhost:6006`. Fix with a separate public URL setting when the UI needs links (Phase 9).
- `docker-compose.gpu.yml` deferred to Phase 4 (native `uv run knesset embed` against the Dockerized db already works since ports are on localhost).

## Phase 1 — Structured load 🟡 code done, live run pending (no tag yet)

**Built**
- `odata.py`: `parse_metadata()` → `EntitySet(name, url_name, base_url, columns, has_last_updated)` with the §4 overrides (`KNS_DocumentQuerie` → `KNS_DocumentQuery`, lobbyist sets → `/OdataV4/Lobbyist`). One HTTP/2 `httpx.AsyncClient`, one global semaphore (`ODATA_CONCURRENCY`, default 8), retries with jittered backoff on 5xx / 473 / timeouts, concurrency halved on 473, `GeoBlockedError` on the `maintenance-page-geo` redirect or an HTML answer. Handles the bare-array, camelCase `KNS_DocumentQuery`.
- `sync.py`:
  - **Full load:** 8 Id-range partitions for tables > 100k rows (last one open-ended), keyset pages `Id gt last [and Id le hi]` by Id, `$top=100`; every 10 pages: `COPY` to a temp table → upsert (only changed docs) → per-partition checkpoint, all in one transaction. All tables run concurrently under the global semaphore. A restart resumes from the checkpoints; the primary key makes duplicates impossible.
  - **Incremental:** `LastUpdatedDate gt (watermark − 48h)` with keyset paging; tables without `LastUpdatedDate` are re-walked.
  - **Deletes** (`--deletes`): tables ≤ 100k rows, live Ids vs raw; missing rows move to `raw._deleted`. Refuses to delete > 10% (and > 10 rows) of a table in one go.
  - Stops a walk on a page with no new Ids, so an endpoint that ignores `$filter` can't loop forever.
  - One `ops.runs` row per sync (`rows_in` = HTTP requests, `rows_out` = rows written), per-table failures in `ops.failures`, a `table_synced` log line per table (mode, rows, requests, seconds).
  - Naive datetimes are read as Israel time (session timezone `Asia/Jerusalem`).
- `raw.<set lowercased>(id bigint pk, doc jsonb, last_updated timestamptz, loaded_at)`, created on demand.
- Migrations: `knesset migrate` applies `sql/NNN_*.sql` as etl (tracked in `ops.migrations`); `status` and `sync` run it automatically. `001_ops_raw.sql` holds the ops tables (moved out of `000_init.sql`, idempotent for existing volumes) and `raw._deleted`. Checked on an existing Phase 0 volume and on a fresh one.
- CLI: `knesset sync [--tables a,b] [--full] [--deletes]`, `knesset migrate`, `knesset record-fixtures`. Make: `sync`, `sync-full`, `sync-deletes`, `fixtures`, `migrate`.
- Tests: 23 offline (fake OData server via `httpx.MockTransport`, synthetic `tests/fixtures/metadata.xml`), incl. partitioned full load, crash + resume without duplicates, incremental filter and watermark, deletes and the mass-delete guard. DB tests use a throwaway `knesset_test` database and skip if Postgres is down; CI's test job runs them against a Postgres service.
- Notebook `01_odata_load`: counts vs live `$count` (asserts ≤ 0.5%), sync state, throughput, raw samples, kill-and-restart resume test on `KNS_CommitteeSession`, timed incremental run.
- Fix: `log.run` now records cancellation / Ctrl-C as `failed` (was `ok`).

**Pending (needs an Israeli IP — run on the Mac)**
1. `make fixtures` → commit `tests/fixtures/live/`; `test_live_fixtures_parse` then runs instead of skipping.
2. `make sync` — the full load (target < ~1 h).
3. `uv run jupytext --to ipynb --execute notebooks/01_odata_load.py` — counts, resume test, incremental timing.
4. Record the measured numbers here, then tag `phase-1`.

**Live findings**
- First full load on the Mac (run `86d4e91b1dbf`, 84 s): 16 tables done, 32 failed mid-walk on **HTTP 481**, a second non-standard firewall status besides 473. Fix: 481 handled like 473 (retry, halve concurrency), firewall backoff 10 s → 120 s with 8 attempts, at most one halving per 30 s. Checkpoints kept the partial progress.

**Unverified against the live API (written from §4)**
- The incremental literal `LastUpdatedDate gt 2026-11-15T08:00:00Z`. If the server rejects it, the incremental run fails per table with HTTP 400 and the fix is the literal format.
- Whether `KNS_DocumentQuery` honours `$filter` / `$orderby` / `$top` (the walk is safe either way).
- `$count` on the Lobbyist service.

## Decisions (PLAN §13)

None changed yet; defaults apply.
