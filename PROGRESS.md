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

## Phase 2 — dbt models ✅ (tag `phase-2`)

**Built**
- `dbt/` project (dbt-core 1.12, dbt-postgres 1.11); run with `knesset dbt <args>` (`make dbt` = build), which passes the `.env` connection to `dbt/profiles.yml` and logs one `ops.runs` row. Anonymous usage stats off.
- Sources: 20 `raw.*` tables (`models/sources.yml`), described in English plus the Hebrew descriptions from `src/knesset/tables_meta.py` (copied verbatim from zomer-g/ckan-version-tracker with its MIT notice; excluded from ruff).
- Staging: 20 ephemeral `stg_*` models (inlined into the marts, so no extra schema or grants), built with two macros:
  - `j(col, type)`: typed jsonb extraction. Source datetimes carry an Israel offset; `::timestamp` keeps the wall-clock time, so every mart timestamp is Israel local time.
  - `he_norm(expr)`: drops direction marks and niqqud, maps geresh/gershayim and curly quotes to ASCII, NBSP → space, collapses whitespace.
- Marts (16 tables in `marts`, readable by `agent_ro`): `dim_knesset`, `dim_status`, `dim_item_type`, `dim_ministry`, `dim_faction`, `dim_committee`, `dim_person` (name variants in both orders, incl. spellings from vote results; MK terms from faction memberships), `fct_person_position`, `fct_committee_session`, `fct_plenum_session`, `fct_session_item` (committee + plenum items, key `c:`/`p:` + id), `fct_bill` (status, `is_passed`, initiators/joiners), `fct_vote` (for/against/abstain counts), `fct_vote_result` (faction at vote time: the membership covering the vote date, latest start wins), `fct_query`, `fct_agenda`.
- `schema.yml`: bilingual ("English | עברית") descriptions for every mart and column (they feed the Analyst prompt in Phase 7). 48 tests: unique/not_null keys and accepted values fail the build; tests on source references (relationships, not_null foreign keys) and the guessed vote-result labels only warn.
- `tests/test_dbt.py`: builds the project on synthetic raw rows and checks faction at vote time (incl. a vote on a membership's last day), vote counts, Israel wall-clock timestamps, Hebrew normalization, name variants, `is_passed`, session-item keys, `dim_knesset`. Found and fixed: `dim_knesset.finish_date` took the max of past periods for the current Knesset.
- Notebook `02_marts`: row counts, dbt test results, faction-match rate (overall and per year), the real vote-result labels and bill statuses, sample analyses (bills per Knesset, top initiators, committee activity, queries per ministry, faction cohesion). Runs end to end on the synthetic marts.

**Real data (Mac, 18 Nov 2026)**
- `make dbt`: 16 models + 48 tests in 22 s; `PASS=61 WARN=3 ERROR=0`. Notebook 02 runs headless.
- Rows: `fct_vote_result` 1,953,822 · `fct_session_item` 249,778 · `fct_committee_session` 109,289 · `fct_bill` 60,291 · `fct_query` 42,824 · `fct_vote` 36,144 · `fct_person_position` 24,080 · `fct_agenda` 22,072 · `fct_plenum_session` 8,799 · `dim_person` 1,235 · `dim_faction` 556 · `dim_committee` 2,905 · `dim_ministry` 922 · `dim_status` 82 · `dim_knesset` 27 · `dim_item_type` 8.
- Faction at vote time matched for 100% of vote results, every year 1982–2026. Faction cohesion in Knesset 25: 99.3–100% of for/against votes follow the faction majority.
- The warnings, and what was done:
  1. Vote-result labels are בעד (7), נגד (8), נמנע (9), נוכח (6, present, did not vote), הצביע (11, voted, secret ballot), לא נכח/ אינו נוכח (10). Accepted values corrected; `fct_vote.n_present` added.
  2. 39,226 session items use item types missing from `KNS_ItemType`: 11 (28,043 items, mostly committee general topics), 9 (11,120 plenum topics), 950 (63 oral question slots). Added to `dim_item_type` with descriptions inferred from item names, flagged `is_inferred`.
  3. 3,763 vote results reference votes missing from `KNS_PlenumVote`: a source gap; stays a warning.
- `is_passed` now also counts status 6030 "פורסם ברשומות" (1,054 bills) besides 118 "התקבלה בקריאה שלישית" (7,752).
- Rebuild with the fixes: `PASS=63 WARN=1 ERROR=0` in 22 s; the one warning is the source gap in item 3.

**Open issues**
- 885 bills have no `knesset_num`.
- `dim_knesset` has 27 rows for Knessets 1–26: one extra `KnessetNum` value in `KNS_KnessetDates` to look at.

## Phase 3 — Document text (Word only) ✅ (tag `phase-3`)

**Built**
- `sql/002_text_documents.sql`: `text.documents` (PLAN §5 columns plus `source_updated`, `priority`, `group_type_id`, `detected_type`), indexes on status/priority and parent.
- `documents.py`:
  - **Registry** (`knesset registry`): one row per `KNS_Document*` row (7 source tables; `doc_id` = `<parent_type>:<source id>`), with Knesset, committee and date from the parent row. Selection: among files with the same name (same document, different formats) docx > doc > rtf, the rest `superseded`; a PDF is `superseded` when its parent and document type have a Word file, else `skipped_pdf`; other formats `skipped_other`. Several different Word files of one parent and type are all kept (e.g. attachments): a refinement of the plan's "per parent and document type". Priority 1 committee protocols, 2 plenum protocols (document type contains "פרוטוקול"), 3 bills, all in the scope Knessets; 4 the rest. Re-runs keep `done`/`failed` rows unless the URL or `LastUpdatedDate` changed, which resets them to `pending` with 0 attempts.
  - **Extraction** (`knesset extract [--knessets 25,26|all] [--priority N] [--limit N]`): batches of 200 pending documents by priority, Knesset (newest first) and date. Download through the WAF-aware client; format confirmed by magic bytes (a ".doc" that is really a PDF becomes `skipped_pdf`; an HTML page is a failed attempt); .docx with python-docx (body paragraphs and tables in order); .doc/.rtf with LibreOffice headless (`txt:Text (encoded):UTF8`), `SOFFICE_WORKERS` (default 4) slots, each with its own user profile, in a temp dir always removed. Cleaning: NFC, direction marks removed, maqaf/geresh/gershayim/NBSP normalized, whitespace collapsed, page numbers and repeated short lines (5+ times; running headers and footers) dropped, speaker lines (ending with ':') kept. Stores `text`, `n_chars`, `content_hash` (sha256), `detected_type`. Max 3 attempts, each failure in `ops.failures` with its attempt number. A geo-block aborts the run.
- `make registry`; `make extract ARGS=...` runs in the app container.
- Tests (31 total, 6 new): cleaning, magic bytes, .docx order, a real LibreOffice .doc conversion (skipped where LibreOffice is missing, as on the CI test runner), registry selection and change detection, extraction end to end against a fake file server (404 retried → `failed` after 3 attempts, PDF disguised as .doc, no temp files left).
- Notebook `03_documents`: status by priority/type, the ≥ 98% priority-1 check, the PDF-only gap report, failures, text quality (length, broken characters, Hebrew share), and 20 random protocols to read.
- Fixed: the logger kept a reference to the `sys.stderr` of the moment `configure()` ran (a closed stream after a CLI test); it now looks it up on each use.

**Live findings**
- Registry on the real data (11 s): 373,934 files; 185,937 pending (Word), 86,168 skipped_pdf, 54,322 skipped_other, 47,507 superseded.
- First trial batch: 47 of 50 downloads got **HTTP 406** because the client sent the API's `Accept: application/json`; fixed by sending `Accept: */*` for files (the fake server in the tests now answers 406 to a JSON Accept). The 47 documents keep 2 attempts.

- Priority 1 (committee protocols, Knessets 25–26): **9,377 / 9,377 done (100%)** in 47 min (≈ 3.3 docs/s, no WAF answers); one LibreOffice timeout succeeded on the rerun. 9,376 of the ".doc" files are really .docx (python-docx path).
- Text quality: median 53,514 chars, 1 document under 500 chars, 0 with U+FFFD, Hebrew share ≥ 0.9 in all (1.0 in 9,371). Samples read correctly: headers, attendance lists (table rows as `name | – | role`) and the Knesset's own `<< נושא >>` markup, which Phase 4 can use for speaker/topic boundaries.
- Fixed: plenum protocols are filed as "דברי הכנסת", not "פרוטוקול", so priority 2 was empty (they were in 4).
- PDF gap over all Knessets: committee protocols 30,925 PDF-only parents vs 62,324 with Word (33%), plus 32,036 scanned TIF protocol files (old Knessets); see notebook 03 for the per-Knesset table.

**Acceptance**

| Check | Target | Result |
|---|---|---|
| Priority-1 Word documents done | ≥ 98% | 9,377 / 9,377 (100%) |
| Sample of 20 reads correctly | user check | confirmed by the user |
| PDF gap quantified | report | notebook 03 + the table below |
| Notebook 03 | runs headless | ✓ |

**PDF gap, Knessets 25–26 (reviewed with the user, 18 Nov 2026)**: no committee or plenum protocol is PDF-only. PDF-only parents (vs with Word): committee background material 2,992 (1,017), bill texts for 1st reading 714 (0), for 2nd/3rd reading 606 (0), published laws 572 (0), ministers' answers to queries 629 (12), committee decisions 752 (644), drafts for committee discussion 678 (297), secondary-legislation filings 1,212 (45) and 724 (47), research-center documents 643 (1, out of scope §12). Bill texts after the early stage and written query answers are PDF-only. **Decision: keep "no PDFs" (§13 default) for now**; revisit in Phase 7 if the agents need bill wording or query answers.

**After the tag (dev scope completed)**
- Priority 2 (plenum protocols, 25–26): 421 done. Priority 3 (bills, 25–26): 7,615 done in 26 min; 1 file 404s on the server.
- Priority 4 in 25–26: 6,430 done in 21 min, 43 failed attempts in three groups, fixed: .docx files python-docx cannot open (broken internal references, `word/NULL`) or finds no body text in (text boxes) now fall back to LibreOffice; NUL and other control characters are removed in cleaning (Postgres rejects NUL). Rerun picks them up.

**Notes**
- Chunk/embedding invalidation when a document's `content_hash` changes belongs to Phase 4 (no chunks exist yet).
- Whether `fs.knesset.gov.il` sits behind the same WAF as the API is unknown; the client handles 473/481 either way.

## Changes to the plan

- 18 Nov 2026, approved by the user: Phase 1 full-load target changed from < ~1 h to < ~4.5 h (WAF-limited to ~2 requests/s). PLAN.md §4 and §10 updated.

## Decisions (PLAN §13)

- Handle PDF-only documents? **No** (decided 18 Nov 2026 after the Phase 3 gap report; revisit in Phase 7 if bill wording or query answers are needed). Other decisions: defaults still apply.
