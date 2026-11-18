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

## Phase 1 — Structured load ✅ (tag `phase-1`)

**Built**
- `odata.py`: `parse_metadata()` → `EntitySet(name, url_name, base_url, columns, has_last_updated)` with the §4 overrides (`KNS_DocumentQuerie` → `KNS_DocumentQuery`, lobbyist sets → `/OdataV4/Lobbyist`). One HTTP/2 `httpx.AsyncClient`, one global semaphore (`ODATA_CONCURRENCY`, default 8), retries with jittered backoff on 5xx / WAF answers (473, 481) / timeouts, concurrency halved once per WAF episode, `GeoBlockedError` on the `maintenance-page-geo` redirect or an HTML answer. Handles the bare-array, camelCase `KNS_DocumentQuery`.
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
- Tests: 24 offline (fake OData server via `httpx.MockTransport`, synthetic `tests/fixtures/metadata.xml`), incl. partitioned full load, crash + resume without duplicates, incremental filter and watermark, deletes and the mass-delete guard. DB tests use a throwaway `knesset_test` database and skip if Postgres is down; CI's test job runs them against a Postgres service.
- Notebook `01_odata_load`: counts vs live `$count` (asserts ≤ 0.5%), sync state, throughput, raw samples, kill-and-restart resume test on `KNS_Bill`, timed incremental run.
- Fix: `log.run` now records cancellation / Ctrl-C as `failed` (was `ok`).

**Acceptance (measured on the Mac, 17–18 Nov 2026)**

| Check | Target | Measured |
|---|---|---|
| Full load | < ~4.5 h (was ~1 h, changed with the user's approval) | 4 h 21 min, 48/48 tables, 3,320,486 rows (run `504e42bd30a8`) |
| Counts vs live `$count` | within 0.5% | 0 of 48 sets off; 3,397,137 raw rows; largest gap `KNS_DocumentBill` 0.28%, measured just before the incremental run that fetched 308 of those rows |
| Kill and restart | resumes, no duplicates | `KNS_Bill` cancelled after 20 s, resumed in 339 s: 60,291 rows = 60,291 distinct = 60,291 live |
| Incremental | minutes | 2.5 min, 501 rows written (308 `KNS_DocumentBill`, 193 `KNS_PlmSessionItem`) |
| Notebook 01 | runs headless | ✓ |
| Tests | offline | 24 passed, incl. the recorded live fixtures (`tests/fixtures/live/`) |

**Live findings**
- First full load on the Mac (run `86d4e91b1dbf`, 84 s): 16 tables done, 32 failed mid-walk on **HTTP 481**, a second non-standard firewall status besides 473. Fix: 481 handled like 473 (retry, halve concurrency), firewall backoff 10 s → 120 s with 8 attempts, at most one halving per 30 s. Checkpoints kept the partial progress.
- **Full load completed** (Mac, run `504e42bd30a8`, started at `ODATA_CONCURRENCY=4`, resuming the first attempt's checkpoints): **48/48 tables, 3,320,486 rows written in 4 h 21 min**, 0 failures. `KNS_PlenumVoteResult` 1,945,822 rows (§4: 1.95M).
  - The WAF answered 481 after ~90 s at concurrency 4 (≈ 15 req/s); concurrency fell to 2, then 1 within 3 min, and stayed at 1 with no further 481s. Sustained rate ≈ 2.1 requests/s (33,627 requests), the same pace as the reference implementation. The WAF limits requests per time window, so the < 1 h target is not reachable from one IP.
  - The server closes each HTTP/2 connection after 2,000 streams (`ConnectionTerminated last_stream_id:1999`); retried transparently, about every 5 min.
  - Fixed: the per-table `requests` in `table_synced` counted all concurrent requests; it now counts each table's own (a context-variable counter per table task).
- Compared with the reference `zomer-g/ckan-version-tracker` (`app/services/knesset_db.py`, `knesset_http.py`): same keyset paging, 10-page batches, 48 h overlap, endpoint overrides, and the same incremental literal (`LastUpdatedDate gt <UTC>Z and Id gt X`), which runs in their production; `Id gt` filtering works on `KNS_DocumentQuery` there too. The difference is pacing: they walk one table and one request at a time (≈ 2 pages/s, ≈ 5 h for a full load) and never meet the WAF. Copied their 90 s / 20 s connect timeout.
- `knesset_tables_meta.py` (Hebrew table descriptions) is to be copied with attribution in Phase 2, for the dbt docs and the Analyst prompt.
- From the recorded fixtures: the Lobbyist service supports `$count` (830 lobbyists); `KNS_DocumentQuery` is a bare array with camelCase keys and offset-less datetimes, as in §4.

**Open issues**
- Concurrency never climbs back after a WAF halving; harmless for the daily increment, revisit only if runs get slow.
- `KNS_BroadcastCommitteSession` (109k rows, no `LastUpdatedDate`) is re-walked on each incremental run; the whole incremental still took 2.5 min.
- Deletes (`knesset sync --deletes`) are built and tested offline but not yet run live; Phase 11 schedules them weekly.

## Changes to the plan

- 18 Nov 2026, approved by the user: Phase 1 full-load target changed from < ~1 h to < ~4.5 h (WAF-limited to ~2 requests/s). PLAN.md §4 and §10 updated.

## Decisions (PLAN §13)

None changed yet; defaults apply.
