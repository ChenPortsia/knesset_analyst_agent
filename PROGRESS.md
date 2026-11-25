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
- Rerun with the fixes: 15 more done. Left without text (28 of ≈ 23,890 Word documents in Knessets 25–26, 0.1%): 23 with no text in either python-docx or LibreOffice (scanned pages inside Word files), 3 corrupt .docx LibreOffice cannot load either, 1 file 404 on the server. They end as `failed` after their third attempt.

**Notes**
- Chunk/embedding invalidation when a document's `content_hash` changes belongs to Phase 4 (no chunks exist yet).
- Whether `fs.knesset.gov.il` sits behind the same WAF as the API is unknown; the client handles 473/481 either way.

## Phase 4 — Chunking and embeddings ✅ (tag `phase-4`)

**Built**
- `sql/003_chunks.sql`: `text.chunks` (PLAN §5 columns plus `speaker_role`, `speaker_confidence`, `tsv_stripped`), GIN on both tsvectors, btree on `chunk_hash` and `person_id`; `text.he_strip()`; `text.documents.chunked_hash`.
  - Deviation from §5: `chunk_hash` is indexed, not unique. Identical text occurs in several documents (boilerplate, duplicate files); the hash is used to embed it once.
- `chunks.py` (`knesset chunk [--knessets]`):
  - Speaker turns from the Knesset's tags (`<< דובר >> name: << דובר >>`; topic tags `<< נושא >>` are not speakers). Protocols without tags fall back to short `name:` lines; non-protocol documents never do (bills have `הגדרות:` lines).
  - Chunks pack whole turns up to ~500 tokens; a longer turn is split into sentence windows with ~60 tokens of overlap and its speaker line repeated in every piece. Other documents: sentence windows that keep paragraph breaks. Never mid-sentence. Contiguous `ord`. bge-m3 tokenizer (`tokenizers`, downloaded once).
  - Speakers: dominant speaker per chunk → `dim_person` by normalized name (titles, faction in parentheses and role after a dash removed): exact variant of a person active on the session date (confidence 1.0 if also in the protocol's attendance list, else 0.9); unique variant but not active (0.6); last name unique among people present and active (0.7); else unresolved (most invitees are not in `dim_person`).
  - Documents whose `content_hash` differs from `chunked_hash` are re-chunked (old chunks deleted in the same transaction) — the invalidation deferred from Phase 3.
- `embed.py` (`knesset embed [--knessets] [--limit] [--batch] [--index]`, native with the `embed` extra): bge-m3 via sentence-transformers on the detected device (fp16 on mps/cuda), normalized, `max_seq_length` 1024, shortest chunks first, one commit per batch (resumable), each distinct text embedded once and copied to duplicates. `--index` builds HNSW (`halfvec_cosine_ops`) after the bulk embed.
- Hebrew keyword search: `he_strip` removes up to 3 prefix letters while 3+ letters remain, applied to text and query alike (ובכנסת, הכנסת, כנסת → נסת), in `tsv_stripped`; plain `tsv` is kept.
- Tests (40 total, 7 new; word count stands in for the tokenizer, a fake encoder for bge-m3): chunking of protocols/untagged/other documents, overlap, names, attendance, all resolution paths, prefix search, chunk → re-chunk → embed (shared embeddings) → HNSW → nearest neighbour.
- Notebook `04_chunks_embeddings` (smoke-tested on test data with the vector part off).

**Live findings**
- `make chunk` (Knessets 25–26): 23,858 documents → 650,985 chunks in 26 min; 494,781 chunks with a speaker, 164,933 resolved (33%) — under investigation (titles, spelling variants; guests are mostly not in `dim_person`).
- Embedding benchmark: 104 chunks/s on mps, but on the 1,000 shortest chunks (the embedder goes shortest first), so not representative. The overnight backfill finished (a rerun found nothing left; GPU 93–100% busy at ~27 W).
- Backfill complete: 650,985 / 650,985 chunks embedded; HNSW built in 728 s after the shm fix.
- Speakers on the real chunks (first pass, 33% resolved): by tag, דובר 63.7%, דובר_המשך 48.5%, קריאה 39.9%, יור 88%, אורח 2.7%; tags הצח, הפסקה, סיום, הלסי, אחרי_כן (≈ 46k chunks) are structural markers, not speakers; "קריאה"/"קריאות" (≈ 19k) are anonymous calls; ministers with long titles failed (השר לביטחון לאומי איתמר בן גביר); many unresolved דובר names are committee legal advisors and officials, and most guests are invitees — not in `dim_person`. Fixed: structural tags are no longer speakers (new chunking) and are skipped in resolution; anonymous calls are not counted; new rule: an active person whose full first and last name both appear in the speaker string, unique → confidence 0.8.
- Speaker resolution moved to its own step (`knesset speakers`, also run by `knesset chunk` for new documents): it updates `person_id` / `speaker_confidence` in place, so better matching never re-chunks or loses embeddings.
- HNSW build failed with `DiskFull: could not resize shared memory segment`: Docker gives the db container 64 MB of `/dev/shm`, and a parallel build needs a segment of `maintenance_work_mem` (was 2 GB). Fixed: `shm_size: 2gb` on the db service, `maintenance_work_mem` 1 GB.

**Acceptance (Knessets 25–26, measured on the Mac 18–19 Nov 2026)**

| Check | Result |
|---|---|
| Priority-1 chunks embedded | 455,138 / 455,138 (100%); all 650,985 chunks embedded (overnight on mps, ≈ 13 chunks/s over the full backfill), HNSW in 728 s |
| Speaker match rate reported | by tag: יור 92.5%, דובר 67.0%, דובר_המשך 59.8%, קריאה 46.2% (incl. anonymous calls), אורח 2.7% (invitees, mostly not in `dim_person`); confidence 1.0: 46,818 chunks, 0.9: 109,796, 0.8: 16,666, 0.7: 4,139, 0.6: 2,583 |
| Notebook 04 | runs headless |

- Chunks: 494,783 speaker-turn, 114,121 paragraph, 42,081 header; median 488 / 466 / 483 tokens; 610,375 distinct texts.
- Keyword hits plain → prefix-stripped: מכסות לייבוא דבש 6 → 17, יוקר המחיה 3,407 → 4,976, החטופים בעזה 3,831 → 7,958, רפורמה משפטית 299 → 1,855.
- Vector search finds the relevant debates (beekeepers' protocol for the honey quota, hostage families' testimony); keyword ranking often puts attendance-list header chunks first; hybrid (RRF) balances the two.
- After the notebook: `<< מנהל >>` (a chair-change note) and `שאילתה` are structural tags too; titles with a comma ("שר הנגב, הגליל והחוסן הלאומי …") now match on the whole string. Run `make speakers` to apply (no re-chunking). Applied: 182,103 of 420,944 named-speaker chunks resolved (43%; ≈ 70% without guests, who are mostly not in `dim_person`).

**Open issues**
- A few chunks exceed the target: max 22,315 tokens, where a "sentence" has no sentence end (long tables or lists). bge-m3 reads the first 1,024 tokens of those; the rest is keyword-searchable only. Fix: split over-long sentences by words, then re-chunk just those documents.
- Many unresolved `דובר` speakers are committee legal advisors and officials (e.g. legal advisor ד"ר עידית חנוכה), and most guests are invitees: not in `dim_person`. A staff/guest list would be a later addition.
- Header chunks rank high in keyword search; Phase 6 search should down-weight or filter them.

## Phase 5 — Enrichment ✅ (tag `phase-5`)

**Plan change (user, 22 Nov 2026): structure + NER instead of topic labeling.** Hand labeling topics per chunk proved ambiguous (law fragments, short chunks, document types differ). Topics now come from the documents' own markup and metadata; the only model-based NLP is named-entity recognition. The taxonomy, LLM teacher, classifier and labeling page stay in the code (`topics`, `topics-teach`, `topics-eval`, `topics-apply`, `label`; tests kept) but were not run and nothing was spent.

**Document types (`knesset samples`, `eval/samples/`)**: committee and plenum protocols are speaker-turn transcripts; bills (preliminary bills: sponsor header, clauses, דברי הסבר), law texts, committee drafts and secondary legislation are legal wording; background material, decisions, conclusions and press releases are short written documents; queries and agenda motions are one chunk each. The current chunking fits all of them; only protocols get sections and participants.

**Extraction fixes found by reading the samples (all protocols re-extracted, re-chunked, re-embedded)**
- Speaker lines sit in Word content controls (`w:sdt`), which python-docx skipped: `docx_text` now reads every `w:t`, inline and block.
- `clean()` dropped short lines repeated 5+ times as running headers, which deleted a frequent speaker's tagged line (`<< יור >> … << יור >>` ends in `>>`, not `:`). Lines with `<<` are always kept.
- `extract --redo docx,doc` re-queues done documents of those types; unchanged text keeps its chunks and embeddings.
- The embedder sees chunk text without protocol markers (`embed.plain`); the stored text keeps them.
- WAF answers now pause every request for 60 s together (one `odata_paused` log line) instead of per-request backoff.
- Effect (Knessets 25–26, committee protocols): chair (`יור`) chunks 2,958 → 117,095 (99.9% resolved); plenum speakers 94.4% resolved. Protocols without any speaker tag: 2 of 9,377 committee, 0 of 421 plenum. Chunk sizes: median 467 tokens (committee), 460 (plenum); < 1.3% under 30 tokens. Re-embedding: 627,473 chunks in 14.4 h on the Mac GPU (12 chunks/s).

**Step 1 — links (`knesset links`)**: protocol chunks → session items by `<< נושא >>` titles; speakers → person and faction on the session date. After the fixes: 529,146 chunk ↔ item links, 329,666 person and 316,137 faction entities (was 182,104 / 172,520).

**Step 2 — protocol structure (`knesset structure`, 93 s, no model)**
- `text.sections`: protocols split at `<< נושא >>` / `<< הצח >>` / `<< הלסי >>`; a marker straight after a marker (< 20 words between) is one section and the נושא title wins; titles matched to session items. Words before a speaker line belong to whoever spoke last.
- `text.section_speakers`: words and turns per speaker per section (דובר and דובר_המשך merged), speakers resolved as on chunks. This answers "share of the session per agenda item / speaker / faction".
- `text.participants`: committee attendance lists — name, heading (חברי הוועדה, מוזמנים, …), description as written, organization (last comma part), person id for members and exact matches. The transcription service is skipped.
- Results: 9,798 protocols, 15,381 sections, 217,529 section speakers, 229,257 participants. Committee sections 95.8% titled, 90.9% linked to an item; plenum 99.9% / 70.2% (the rest is procedure, not items). Top organizations: משרד האוצר (3,958 sessions), משרד המשפטים, המשרד לביטחון לאומי, משרד הביטחון, משרד הבריאות.
- Caveat for the agent: filter by committee and session, not by a word in a title (a "תקציב" title search also returns unrelated bills).

**Step 3 — NER (`make ner`, DictaBERT-NER, CC BY 4.0)**
- `dicta-il/dictabert-ner` via the transformers pipeline (whole-word aggregation, stride 64) on the Mac GPU. Input: chunk text without speaker lines and markup; header chunks skipped. Kept labels: person, org, place (GPE/LOC), facility, event, product, work, language, misc; dates and titles dropped; score ≥ 0.5. Resumable through `text.chunk_ner`.
- Linking: a person links to `dim_person` when the full name (any first-name word with the last name; geresh ignored) or a last name alone — if it is nobody's first name — belongs to exactly one person holding a position at that date; `ביבי` is an alias. Parties link to faction ids on exact names. Prefix forms merge into the plain form when it was also found (בישראל → ישראל, בכנסת → הכנסת; the article ה is never stripped).
- Validated on three 2,000-chunk trials before the full run (each fixed: mid-word spans, the article stripped, prefix forms counted apart, last names and middle names unlinked, a first name linked as a last name). Final trial: 75.3% of people linked; unlinked are first names alone, shared last names (כהן, כץ, ביטון) and people outside the marts.
- Full run: 691,207 chunks in 4.7 h (≈ 40 chunks/s); 3,821,394 entities, 461,399 linked; 1,070,899 prefix forms merged.
- Known limit: a last name that is also a common first name of someone outside the marts can mislink (e.g. שירי → נאור שירי, where it may mean Shiri Bibas). The surface form is kept on every entity.

**Tests**: 59. Notebook `05_enrichment`: links, sections (shares per item in one session, speakers in its largest item), participants, entities per type, top names, linked spot check.

**Open**: section speakers and entities are not yet aggregated per faction in marts (the agent can join them); org entities beyond factions (ministries, committees, companies) keep their names without ids.

## Phase 6 — Retrieval layer and tools ✅ (tag `phase-6`)

**Search (`search.py`, `knesset search`)**
- Hybrid: bge-m3 vector top-50 (HNSW, `ef_search` 100, iterative scan) + keyword top-50 on the prefix-stripped `tsv_stripped` (`text.he_strip` on the query too), merged by reciprocal rank fusion (k = 60). Keywords only where torch is missing (the app container).
- Filters: Knessets, committees, dates, document types, documents, speaker (person), speaker's faction, people mentioned (NER), any mentioned name, session items. Header chunks are excluded by default. Topic / sentiment / stance / speech-act filters are not built: those labels were not produced (Phase 5 plan change).
- Identical text (the same background document attached to many sessions) counts once; `also_in` lists the other documents.
- Neighbors: `ord ± n` in one query (default 1 for protocols, 0 otherwise), overlapping windows merged, chunks marked hit / before / after; text without protocol markup; `url`, `doc_id`, `chunk_id` on every result. Runs as `agent_ro`.
- Memory: Postgres ran with Docker's default 128 MB of shared buffers while the HNSW index is 1.9 GB (chunks 14 GB), so any search past the hottest pages read from disk (1,000 neighbors: 13,016 pages read, 2.3 s). The db container now runs with `shared_buffers=3GB` (`DB_SHARED_BUFFERS`; Docker's VM has 8 GB on the Mac), `effective_cache_size=5GB`, and `pg_prewarm` preloaded (`make warm` loads the index once; autoprewarm restores it after restarts).
- Selective filters (committee, speaker, faction, mentions, entity, items, documents) matching ≤ 20k chunks compare with them directly; larger sets use the index with the filter applied as it scans. Tried and dropped: exact distances over a whole big committee (Finance, 50,551 chunks: 563–3,390 ms from disk) and filtering the index's 1,000 nearest overall (only 2–13 of them were Finance chunks). The search log line names the path (`vector_path` index / exact).
- Measured on the Mac (Knessets 25–26, notebook 06, index in memory):
  - hybrid with neighbors, 5 queries × 3: median 140 ms (target < 300 ms ✅); warm runs 80–150 ms (encode ≈ 25, connect ≈ 15–20, vector ≈ 20, keyword 15–100, neighbors ≈ 10–15 ms);
  - one large committee (Finance, 50,551 chunks): median 290 ms, max 302 ms (notebook limit 500 ms), mostly the filtered index scan (≈ 200 ms);
  - outliers of 0.8–1.8 s are keyword ranking on a query's first run: a very common word (תקציב) matches many chunks whose rows are not cached (the 14 GB table exceeds the 3 GB buffers). `facets` with a query shows the same (1.7–2 s).

**Tools (`tools/`)**: plain typed functions, each returning JSON with an `evidence` list, truncated text, and a log line + trace span per call (`tool_call`, `tool.<name>`).
- common: `resolve_entity` (person, faction, committee, ministry, bill → ids).
- research: `search_text`, `get_document`, `get_chunks`, `facets` (knesset, committee, month, doc type, speaker, faction, entity), `share_of_words` (words and turns per section, speaker, faction or committee, from `text.sections`; factions once per person and date — the first version timed out at 15 s; non-MKs apart from MKs without a faction).
- summary: `get_document_summary` (sections with shares, top speakers, organizations, top entities — from markup, no model), `summarize` (map-reduce over search hits with the agent's model, injected).
- sql: `describe_schema` (dbt schema.yml, bilingual), `sql_query` (one SELECT, `agent_ro`, read-only, 15 s, ≤ 200 rows), `make_chart` (Plotly figure spec).
- verify: `get_chunks`, `rerun_sql`, `verdict`.
- `tools/lc.py`: LangChain `StructuredTool`s per agent; bad filters and SQL errors go back to the agent as text.
- Orchestrator state tools (`plan`, `delegate`, `ask_user`, `finish`) come with the graph in Phase 7.

**Tests**: 79. Notebook `06_retrieval_tools` (passed on the Mac): latency over 5 queries × 3, one-committee latency, best hits, every tool with shape checks. Finance Committee share of words by faction: non-MKs and unidentified speakers 47.7%, יש עתיד 12.5%, יהדות התורה 9.8%, הליכוד 7.5%; MKs without a faction on the day 1.7%.

**Open**
- Keyword ranking of very common words on a cold cache (above): rank fewer matches, or an index that stores positions (RUM), if it matters in Phase 7.
- A connection per search (≈ 15–20 ms): the agents in Phase 7 should share a connection pool.
- `summarize` needs the agent's model (Phase 7).

## Phase 7 — Multi-agent system ✅ (tag `phase-7`)

**Built** (tested offline with a fake Claude client; three paid notebook-07 runs, below)
- `agents/contracts.py`: `AgentTask` → `AgentResult` (findings with evidence, artifacts, verdict, usage), pydantic, JSON — the A2A-ready contract.
- `agents/llm.py`: one tool loop on the Anthropic SDK (`beta.messages.create`): tools and a strict JSON output schema in the same request, prompt caching (`cache_control`), server-side refusal fallbacks (`fallbacks: "default"`), at most 8 model calls per loop (the last without tools); cost per call at list prices; a per-question `Budget` (cost, agent turns, wall time) shared by all agents.
- `agents/specialist.py`: analyst, researcher, summarizer, verifier = prompt + tools + model behind `run(task)`. A finding keeps only evidence its own tool calls returned in that task (invented ids are dropped and the claim becomes low confidence).
- `agents/client.py`: `AgentClient` / `LocalClient` (A2A in Phase 8).
- `agents/graph.py` (LangGraph): plan (Opus, may call `resolve_entity`; or asks the user to clarify) → delegate (independent tasks in parallel, dependent ones with their dependencies' findings as context; a `handoff.<agent>` span each) → draft (Opus, from findings only, numbered citations) → verify (verifier re-reads cited chunks and re-runs SQL) → on fail, plan again once, then answer with caveats → finish. Limits, refusals and unparsable answers end the run with what is known, said so.
- `agents/prompts/*.md`: shared rules; orchestrator (Hebrew + English examples), draft, analyst (13 question → SQL pairs on the real marts columns), researcher, summarizer, verifier.
- Tracing (Phoenix, PLAN §6): one trace per question. The `question` root carries user, thread, question, the path (`plan → delegate → draft → verify → finish`), answer, verdict, cost and steps; under it an `orchestrator.<step>` span per graph step, `handoff.<agent>` and `agent.<agent>` per delegation, `tool.<name>` per tool call, and every Claude call via OpenInference (prompt, response, tokens, latency). Specialists run in worker threads with a copy of the trace context, so their spans stay in the question's trace (a test checks it, and fails without the copy).
- Langfuse Cloud (the user's request): when `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set, the same spans also go to `{LANGFUSE_BASE_URL}/api/public/otel/v1/traces` (OTLP over HTTP, Basic auth). The question span sets `langfuse.user.id`, `langfuse.session.id` (thread), `input.value` and `output.value`. Endpoint and auth were written from memory (the docs could not be read from the cloud session); the first run on the Mac confirmed traces arrive in Langfuse. Tests clear the keys, so no test trace leaves the machine. Bug found by notebook 07: phoenix.otel's provider drops its own exporter when another processor is added (default `replace_default_processor=True`), so from the Langfuse change until the fix spans reached only Langfuse; fixed with `replace_default_processor=False` (a test fails without it).
- `knesset ask "…"`: streams plan, agent results and the verdict, then the answer, sources, cost, calls, steps, seconds and the Phoenix link.
- Limits (`.env`): `AGENT_MAX_COST_USD` 0.50 per question, `AGENT_MAX_STEPS` 12 agent turns, `AGENT_TIMEOUT_S` 90.
- Notebook `07_agents` (paid, capped at $5 in the notebook): each specialist alone, the verifier on one correct and two planted-error drafts, 10 mixed questions, traces in Phoenix.
- Tests: 87 (8 new: strict schema, invented evidence, specialist alone, full graph, re-plan then caveats, clarification, cost and step limits, the CLI).
- Fixed on the way: Claude Opus 5.5 / Sonnet 5.5 cache reads cost 5% of the input price (the topic-labeling cost code assumed 10%).
- First paid question (Mac): "כמה הצעות חוק עברו בכנסת ה-25?" → plan (1 analyst task) → SQL → verifier re-ran it → pass. Answer: 573, split into government 363 / private 199 / committee 11, with the SQL as source. $0.052, 7 calls, 4 steps, 24.7 s, trace in Phoenix. Fixes from it: the API key is passed from `.env` to the client; `knesset ask` keeps tool-call log lines in `logs/ask/` instead of the terminal.

**First notebook-07 run (Mac, $2.75)** — read back from the Langfuse export:
- 7/10 questions answered well with citations: 573 bills passed; Finance held the most committee sessions (1,154); the 2025 budget law passed 66–52 along coalition lines (faction table); about 5% of the Finance Committee's words went to the 2025 budget law (9% with the 2025 budget changes); Education got the most queries (176 of 1,580).
- 3/10 failed the same way: the summarizer hit the 90 s limit before any finding (Haredi draft, construction waste, hostages in the plenum). The notebook's citation check (≥ 80%) failed, and the failing cell lost the notebook output (results are now saved as they arrive, and `knesset traces` reads questions back from Phoenix).
- Cost by agent: summarizer $1.07 (68 calls: one Opus call per passage in its map-reduce tool), analyst $0.42, plan $0.40 (Opus, ~2.4 calls per question), researcher $0.37, draft $0.28 (Opus), verifier $0.20. Every call wrote the prompt cache, one-shot calls too.
- Changes (the user chose all four): (A) the summarizer reads passages itself in one loop — the per-passage map-reduce tool is gone; (B) only tool loops use the prompt cache; (C) plan and draft on Sonnet 5.5; (D) the timeout is 150 s.

**Second notebook-07 run ($3.70)**: answers were thorough and well cited, and the verifier caught real errors (an unsupported surname and title, uncited counts, testimony from another debate), but cost rose: 5/10 questions re-planned after a failed check, re-running the specialists with a growing context, and most still failed (4 hit the $0.50 cap, 1 the 150 s limit). Counts from `facets` / `share_of_words` had no citable evidence, and answers ran up to 35 sources.

**Changes after it (the user's choice)**
- Counts cite their SQL: `facets` and `share_of_words` return the statement they ran, values filled in, as `sql` evidence the verifier can re-run (`rerun_sql`); a test re-runs it and gets the same numbers.
- Shorter output: at most 6 findings per task, answers of about 250 words and at most 10 sources.
- Thinking again by issue kind: the verifier marks each issue `unsupported` or `missing`. Unsupported claims → the orchestrator revises the draft from the findings it has (one call, no new search). Missing information → it re-plans only for that, once, if at least $0.15 of the budget is left, then drafts and checks again (a second fail → revise).
- The verifier checks at most 8 key claims, re-reading the chunks in one call.
- SQL-only answers are checked in code: every number in the answer must appear in the cited queries' results (or the question, or be a rank ≤ 20); otherwise the verifier runs.
- A local reranker (`BAAI/bge-reranker-v2-m3`, cross-encoder, on the Mac GPU) re-orders the 20 best fused candidates; agents read the best 5 (`search_text` k default 5, at most 10). Plain `search()` stays without it (notebook-06 latency).
- The verifier runs on Claude Haiku 5.5 (no server-side refusal fallback is sent for it).

**Third notebook-07 run (≈ $1.49, all checks passed)** — acceptance:
- Each specialist passes its smoke task: analyst 573 bills ($0.012, 3 s); researcher three cost-of-living statements with speaker and date ($0.031); summarizer six cited themes on construction waste ($0.095).
- The verifier (Haiku 5.5) passes the correct draft and fails both planted errors (wrong number 1153 vs 573; the invented "most were opposition bills"), $0.0004 per check.
- 10/10 mixed questions answered with citations within the limits: SQL questions $0.03, 11–14 s (checked in code, no verifier call); text questions $0.09–0.20, 55–83 s; one $0.40 / 104 s (Finance time share: a re-plan for missing information, then a revision). 6/10 drafts were revised after the check, each saying what it removed or corrected.
- 10/10 traces in Phoenix (and Langfuse), handoffs included.
- Cost per run: $2.75 → $3.70 → ≈ $1.49 over the three runs (per text question ≈ $0.30 → $0.47 → $0.14).

**Open**
- The planner sometimes picks the summarizer (Opus) and sometimes the researcher (Sonnet) for the same question; the summarizer costs ~3× more. Moving it to Sonnet is the user's option (`AGENT_SUMMARIZER_MODEL`).
- Each `knesset ask` loads bge-m3 and the reranker (several seconds); a long-running server (Phase 9 UI) loads them once. A shared database connection pool is still to do.
- The verdict stays `fail` on revised answers (they are not checked again); the answer says what was changed.
- Fixed after the run: evidence URLs now come from the tool results (the summarizer once typed a malformed link).

**LangGraph refactor (24 Nov 2026, the user: "use all LangGraph capabilities")** — PLAN §5 already names LangGraph with specialist subgraphs; this makes the specialists real subgraphs.
- Models through LangChain: `agents/models.py` builds `ChatAnthropic` per agent (effort, server-side refusal fallback for Opus/Sonnet); a `BudgetCallback` charges every call and stops a call before it starts once a limit is hit. Any LangChain chat model plugs in through the factory (e.g. `ChatOpenAI` for a model matrix).
- Specialists are `create_agent` subgraphs (tools, `ProviderStrategy` structured output, `ModelCallLimitMiddleware` at 8 calls). The hand-written Anthropic tool loop is gone; evidence checks (`keep_seen`) are unchanged.
- Orchestrator graph: `Send` fan-out of ready tasks in parallel; `interrupt()` + `Command(resume=…)` to ask the user back; `Command(goto=…)` from verify; `RetryPolicy` on plan/draft/revise; budget and models in the runtime `Context`; streaming of node updates, custom agent events and the subgraphs' tool calls.
- Memory: `PostgresSaver` checkpoints every thread (`knesset ask "…" --thread <id>` continues a conversation; the planner sees the last 3 turns), `PostgresStore` keeps entities resolved per user across threads. Both in schema `ops` (the etl role cannot create schemas).
- `langgraph.json` for LangGraph Studio (`uv run --with "langgraph-cli[inmem]" langgraph dev`).
- Tests: 99 (agents rewritten on a fake LangChain chat model: full graph, revise, re-plan once, SQL check in code, interrupt/resume, conversation + Store in Postgres, limits, CLI with ask-back, one trace per question).
- First real run, `knesset ask` (Mac): 573 bills, the SQL checked in code, $0.044, 5 calls, 17 s.
- **Fourth notebook-07 run (≈ $1.49, all checks passed)**, the same as run 3:
  - Specialists: analyst 573 ($0.017); researcher three dated cost-of-living statements ($0.039); summarizer six themes on construction waste ($0.055).
  - The verifier passes the correct draft and fails both planted errors ($0.0005 each).
  - 9/10 questions answered with citations: SQL questions $0.04–0.05 in 13–20 s; text questions $0.10–0.38 in 49–93 s. Every text answer was revised after the check.
  - 10/10 traces.
- Fixes after the run:
  - The planner used its 6 calls on `resolve_entity` and gave no plan (the organizations question). Now a planner or specialist that runs out of calls gets one more call without tools and answers from the tool results, as the old loop did.
  - Answers listed up to 16 sources: the list now keeps only the sources the answer cites.
  - One revision left an editing note in the text: the revise prompt now asks for clean final text.
- Tests: 101.

**Eval set and model matrix (25 Nov 2026, the user's choice)**
- `eval/questions.yaml`: 16 questions, 8 SQL and 8 text, in Hebrew and English. A SQL question is right when the answer has every value of its reference query's first row, computed from the database at run time (no number typed in by hand). A text question scores the share of expected term groups it contains: speakers, organizations, key terms taken from the answers read in earlier runs.
- `eval/configs.yaml`: baseline (.env), haiku-specialists, all-haiku, luna-specialists (OpenAI `gpt-6-luna` through `ChatOpenAI`; `openai` extra, `OPENAI_API_KEY`). Any agent's model can be replaced by name.
- `knesset eval --configs … --max-usd 5`: runs every question under each configuration, never starting a question whose worst case passes the cap. It prints one line per answer and a summary per configuration (SQL right, text score, cited, $ total and per question, s per question). Answers go to `logs/eval/<time>.jsonl` and the report to `.md`.
- GPT-6 Luna list price $0.10 / $0.50 / $0.01 cached per million tokens, from price aggregators; OpenAI's own page could not be confirmed.
- First matrix run (Mac): baseline got 8/8 SQL questions right ($0.04–0.11 each, 12–38 s). It then crashed on the first text question: a researcher's final JSON came back cut off mid-string, and LangChain's `StructuredOutputValidationError` was not caught. Fixes:
  - A broken final JSON is treated like running out of calls: the agent keeps its tool results and gets one last call without tools (specialists and planner).
  - In the eval, one failing question is recorded with its error and the run goes on.
  - `--resume <jsonl>` adds to an earlier run and skips the answers already there; their cost counts toward the cap.
- **The full matrix (Mac, $3.23 for 64 answers)**, re-graded with the corrected term lists:

  | config | SQL right | text score | cited | $ total | $ / question | s / question |
  |---|---|---|---|---|---|---|
  | baseline | 8/8 | 1.00 | 16/16 | 1.99 | 0.124 | 55 |
  | haiku-specialists | 8/8 | 0.96 | 15/16 | 0.67 | 0.042 | 45 |
  | all-haiku | 7/8 (1 connection error) | 0.94 | 15/16 | 0.20 | 0.013 | 56 |
  | luna-specialists | 0/8 (all errors) | 0.69 | 6/16 | 0.36 | 0.023 | 38 |

  - Luna's analyst never ran: OpenAI rejects `make_chart`'s free-form object arguments, so every question the analyst touched failed on the first call. Its text answers (researcher, summarizer) were shorter (150 words vs 210–240) with fewer sources (5 vs 7–9); one hit the subgraph's recursion limit.
  - Haiku answers read well. The misses: on the labor-committee organizations question all-haiku counted NER mentions and left out government bodies, where the baseline used the attendance lists (`text.participants`), the better source. haiku-specialists answered construction waste in English without naming the committee chair.
  - Most text answers in every configuration were revised after the check (baseline 6/8, Haiku 7/8).
  - Agent tool errors seen in the logs: wrong column names (`sub_type_desc`, `knesset_num` on `fct_vote`, `publish_date`), `items` filters given as `["bill", id]`, an unknown filter `dates`. The agents recovered from each.
- Fixes after the run:
  - `make_chart` takes typed arguments (columns, rows, kind, x, y, series, title); a test checks that no tool has an open object argument.
  - `items` filters that are not integer pairs get a clear error with an example.
  - The specialist recursion limit is 10 × the model-call cap (the cap is the real limit).
  - `--resume` re-runs answers that failed with an error and re-grades the kept ones with the current `expect`.
  - Term lists accept the Hebrew name with the article and the English name ("הביטוח הלאומי", "National Insurance", "municipal", "dumping").
- **After the fixes (re-run of the 10 failed answers, ≈ $0.17; matrix total ≈ $3.40)**:

  | config | SQL right | text score | cited | $ / question | s / question |
  |---|---|---|---|---|---|
  | baseline | 8/8 | 1.00 | 16/16 | 0.124 | 55 |
  | haiku-specialists | 8/8 | 0.96 | 15/16 | 0.042 | 45 |
  | all-haiku | 8/8 | 0.94 | 16/16 | 0.013 | 57 |
  | luna-specialists | 8/8 | 0.81 | 15/16 | 0.033 | 51 |

  Luna's SQL questions now all pass. Its labor-organizations answer is still from the first run (the researcher hit the old recursion limit inside the task; that is not a run error, so `--resume` kept it).
- Decision (user, 25 Nov 2026): Haiku 5.5 for the analyst, researcher and summarizer; Sonnet 5.5 stays the orchestrator, Haiku the verifier (`config.py` defaults and `.env.example`; `sonnet-specialists` in the matrix keeps the earlier models for comparison).
- 10 harder questions (26 in all): a comparison across Knessets, multi-step SQL (top private-bill initiator, busiest month), two follow-ups asked in the same thread (`before`), cross-source text questions (Finance chair and the 2025 budget, coalition vs opposition on the judicial reform, Smotrich in 2025) and an unanswerable one (graded on an honest "not found").
- Tests: 111.

## Changes to the plan

- 24 Nov 2026, the user: the per-question agent timeout is 150 s (PLAN §7 said 90 s): text questions finished at 91–98 s in the first notebook-07 run.
- 22 Nov 2026, the user: topic labeling paused; topics from markup and metadata, NER as the only model step (Phase 5 above).
- 18 Nov 2026, approved by the user: Phase 1 full-load target changed from < ~1 h to < ~4.5 h (WAF-limited to ~2 requests/s). PLAN.md §4 and §10 updated.

## Decisions (PLAN §13)

- Verifier model (changed by the user, 24 Nov 2026): **Claude Haiku 5.5** instead of Sonnet 5.5; checking claims against cited chunks and SQL is a narrower task, and the planted-error test in notebook 07 is its quality check.
- Orchestrator model (changed by the user, 24 Nov 2026): plan and draft run on **Claude Sonnet 5.5**, not Opus 5.5, to cut cost (they were 25% of the first notebook-07 run). The summarizer stays on Opus 5.5.
- LLM provider and models (decided 19 Nov 2026, start of Phase 5): **Anthropic Claude**. Bulk enrichment (Phase 5 tier 3): **Claude Haiku 5.5** (`claude-haiku-5-5`) via the Batch API, after a 500-chunk benchmark and the user's confirmation of the projected cost (rough estimate ≈ $55 for the ≈ 440k distinct priority-1 chunks, ±50% until Hebrew token counts are measured). Not used in Phase 5 after the 22 Nov plan change (no paid enrichment ran); it stays the model if paid enrichment returns. Agents (Phase 7): **Claude Opus 5.5** (`claude-opus-5-5`) for the Orchestrator and Summarizer, **Claude Sonnet 5.5** (`claude-sonnet-5-5`) for the Analyst, Researcher and Verifier.
- Handle PDF-only documents? **No** (decided 18 Nov 2026 after the Phase 3 gap report; revisit in Phase 7 if bill wording or query answers are needed). Other decisions: defaults still apply.
