# Submission

## Candidate
- **Name:** srini2k03
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~5h: ~1.5h writing the spec and plan, ~1.5h building with
  Claude Code and checking the results, ~2h reviewing and understanding the code for the walkthrough

## How to run
```bash
# from a clean clone, Python 3.11+
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# generate data (not committed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# ingest drop by drop (or both at once: ingest data/drop_1 data/drop_2)
python -m solution --db ledger.db ingest data/drop_1
python -m solution --db ledger.db ingest data/drop_2

# write the five reports
python -m solution --db ledger.db report --out reports/

# tests (generates its own data into pytest temp dirs, never into the repo)
pytest
```
- `--db` defaults to `ledger.db`; `--schemas` defaults to `assignment-a-data/schemas/`.
- PII key: set `AU_PII_KEY` to manage it yourself. Otherwise a random key is created once at
  `ledger.db.key` (gitignored, mode 0600) and reused. **Keep the key with the DB.** Without it,
  phones can't be matched and names can't be decrypted.
- Start over: `rm ledger.db ledger.db.key`.

## Design

**Architecture.** Two commands, with a strict split between *facts* and *derived facts*:

```
drop dir ──► ingest ──► facts (SQLite)                        report ──► 5 files
             • file registry (path + sha256)                    derive.sql (TEMP tables):
             • lead rows + outcome                                linked_events → orphans
             • merged lead state (PII hashed/encrypted)            call_owner (override linking)
             • event ledger (append-only)                          unknown dispositions
             • rejected event lines                                calls → lead_facts → buckets
                                                                  + SQL aggregation queries
```

`ingest` decides only what can be decided from the data seen so far, in arrival order: row
validation, lead merging, event dedupe. Everything that depends on *other* data (does this event
have a lead? which call does this override belong to? what bucket is this lead in?) is
re-derived from the stored facts on every `report`. So a lead arriving in `drop_2` links
events from `drop_1` with no special casing, and step-by-step ingestion gives the same
reports as ingesting everything at once.

**Database schema** (`solution/schema.sql`):

| Table | Key / constraints | Purpose |
|---|---|---|
| `ingested_files` | PK `file_id`; `UNIQUE(rel_path, sha256)`; CHECK `kind IN ('leads','events')`; lead files must have `workflow_id`+`batch_id` | Idempotency registry and provenance. `rel_path` is the path inside the drop (`leads/motor_renewal/batch_001.csv`), so a copied/renamed drop folder is still recognised. |
| `lead_rows` | PK `(file_id, row_number)`; CHECK `outcome IN ('created','merged','rejected')`; CHECK rejected ⇔ `reject_reason` set ⇔ `lead_id` null; rejected rows have 0 warnings; FK → `ingested_files`, `leads` | One row per non-blank CSV data row. Source of `rejections.csv`, `lead_rows_*`, `duplicate_lead_rows_merged`, `field_warnings`. No PII. |
| `leads` | PK `lead_id`; `UNIQUE(workflow_id, phone_hash)` = lead identity; CHECKs on hash length and `phone_last4` digits; `attributes_json` must be valid JSON | Current merged state. `batch_id` = first valid row's file stem. `external_ref` = schema's reference field. Non-PII fields in `attributes_json`; PII fields (name, canonical phone) Fernet-encrypted in `pii_enc`. |
| `events` | PK `event_id`; `UNIQUE(file_id, line_number)`; CHECK `phone_status IN ('valid','invalid','missing')` and valid ⇔ `phone_hash` set; utc/ist both set or both null | **The interaction ledger.** One row per distinct event_id, including events that are orphans *today*. Typed columns (`event_type`, `call_id`, `occurred_at_utc`, `occurred_at_ist`, `disposition_code`, `phone_hash`) plus `payload_json`. The raw phone is never stored. |
| `event_rejects` | PK `(file_id, line_number)`; CHECK `kind IN ('malformed','duplicate')`; duplicate ⇔ `event_id` set | Lines that never reached the ledger. Raw line text is deliberately not kept (it may contain a phone). |
| `disposition_catalogue` | PK `(workflow_id, code)`; 0/1 CHECKs | Re-synced from `dispositions.csv` on every command (it's configuration, not data). |
| `meta` | PK `key` | PII key fingerprint and schema version. |

Indexes: `events(workflow_id, phone_hash)` for phone linking, `events(call_id)` for override
linking, `lead_rows(lead_id)`; plus the indexes implied by the PK/UNIQUE constraints.

Timestamps are stored twice, as fixed-width UTC text (`2026-10-05T13:20:21.000000Z`) for ordering
and as IST text (`2026-10-05T18:50:21+05:30`) for report dates and `last_call_at`. Text
avoids float/epoch confusion and stays readable when someone inspects the DB.

**Ledger design.** `events` (plus `event_rejects`, `lead_rows`, `ingested_files`) is append-only,
enforced by `BEFORE UPDATE`/`BEFORE DELETE` triggers that `RAISE(ABORT)`. An override is just
another row. The effective disposition is computed at read time ("latest override, else latest
set"), so the original AI disposition is never edited. `leads` is the only mutable table
(merges), and a trigger freezes its identity columns and `batch_id`.

**PII.** Fields marked `"pii": true` never reach disk in plaintext:
- Phone: `HMAC-SHA256(k_hash, "+91XXXXXXXXXX")` in `leads.phone_hash` and `events.phone_hash`.
  Event phones are normalised with the same rules first, so `91-98765-43210` and
  `(+91) 9876543210` hash identically. It is keyed (not plain SHA-256) because there are only
  ~4×10⁹ Indian mobile numbers, so an unkeyed hash could be brute-forced in minutes. Events with
  an invalid phone keep `phone_status='invalid'` and a NULL hash.
- `phone_last4` is kept in clear for `phone_masked` (same exposure as the report itself).
- Name (and the canonical phone, so ops could still dial) are Fernet-encrypted (AES-128-CBC +
  HMAC, authenticated) in `leads.pii_enc`.
- Keys: one master secret from `AU_PII_KEY` or a generated `<db>.key` file. Two sub-keys are
  derived from it with HMAC-based labels: one for hashing, one for encryption. A fingerprint of
  the master key is stored in `meta`, so running with the wrong key fails loudly instead of
  silently turning every event into an orphan.
- `rejections.csv` contains only workflow, batch, row number and reason.
- A test scans every text/blob column of every table for any 10-digit phone or customer name
  from the input.

**Idempotency.** Each file is registered by `(rel_path, sha256)` and processed in one
transaction (`BEGIN IMMEDIATE … COMMIT`) together with its registry row. Re-ingesting the same
file is a no-op. A crash mid-file rolls back that file only, and a re-run resumes with it. All
`data_quality.json` numbers are `COUNT`s over stored rows, never mutable counters, so they can't
drift.

**Incremental equivalence.** Order-dependent decisions (which row creates a lead, which copy of an
event_id is first) only depend on the global order *drops → files by name → rows/lines*, which
is the same whether drops arrive one per run or together. Everything else is derived at report
time from the full current state. Tests assert byte-identical reports for both modes on four
seeds.

**Linking.** At report time (`derive.sql`):
1. non-override events join `leads` on `(workflow_id, phone_hash)`;
2. `call_owner` = for each `call_id`, the lead its phone-linked events point to;
3. `disposition.override` rows join `call_owner` on `call_id`;
4. every stored event not in that set is an orphan. A late lead fixes steps 1–3 by
   itself.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| A required field that is present but invalid rejects the row: `invalid_phone` for phone fields, `invalid_<field_name>` for any other type. | The spec only defines the phone case. No current schema has a required date/decimal/enum, but the rule had to be consistent. | Treat it like an optional field (null + warning), but a required field would then be null. |
| The identity phone is the first `phone` field in the schema and must be `required`; each schema needs exactly one non-PII `reference` field (checked at load). | A lead needs an identity and an `external_ref`. Fail fast on a broken schema. | Hard-code `phone`/`policy_number`, which breaks new workflows. |
| A rejected row never affects an existing lead, even if its phone matches. | Rejected rows "are not leads"; letting them merge would leak bad data into good leads. | Merge the valid parts. |
| Two source columns mapping to the same field: the first (left-most) wins. Two fields sharing an alias: the earlier field in the schema wins. | Deterministic, and matches reading order. | Last wins; error out. |
| Header normalisation follows the spec literally: lower → trim → collapse `_`/whitespace runs (no second trim). | Exact spec. `_phone` → ` phone` won't match; I didn't see this in the data. | Trim again after collapsing. |
| Blank rows (all cells empty/whitespace, including a fully empty line) are skipped and do not consume a row number. | Spec. | |
| Dates: exactly `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY` (one consistent separator) or `DD-Mon-YYYY` with a 3-letter English month (case-insensitive, own month table). 2-digit day/month required. | The four listed formats, literally. `5/10/2026` or `05-October-2026` → invalid → warning. | Lenient 1-digit parts / full month names. |
| Decimals: optional `₹`/`Rs`/`Rs.`/`INR` prefix (case-insensitive) with any amount of space after it; commas removed wherever they appear (`1,23,456`); then must be digits with optional `.digits`. Any minus sign → invalid. `Decimal` + `ROUND_HALF_UP` to 2 places (`10.005` → `10.01`). | Exact money arithmetic, no float error (`2.675` stays `2.68`). Indian digit grouping is valid. | Banker's rounding; reject odd comma positions. |
| Enum values must match a code case-insensitively (`EN` ok; `Hindi`/`english`/`tamil` invalid), stored lower-case. | Values are codes, not labels. | Map language names to codes. |
| Defaults are applied when the merged lead is written. | Equivalent to "after merging": a default only fills a null, a later non-null overwrites it either way, and a later null never overwrites. Tested. | Store raw nulls and apply defaults in every reader. |
| `external_ref` also follows last-non-null-wins on merge. | It's a normal schema field. In the data, duplicates always carry the same reference. | Freeze it like `batch_id`. |
| `connectivity_rate` is computed in integer arithmetic in SQL with half-up rounding to 4 decimals: `(2·c·10000 + t) / (2·t)`. | Exact, no floats. | Python `Decimal`. |
| Malformed = not UTF-8, not valid JSON, valid JSON that isn't an object, or `event_id` missing/empty/non-string. Empty or whitespace-only lines are not counted at all. | Spec says "non-empty line". An id that isn't a non-empty string can't be deduped. | Coerce numeric ids to strings. |
| Duplicate `event_id` with different content: the first-seen copy wins; later copies are counted as duplicates even if they differ. | Spec order (dedupe before anything else). The ledger is append-only, so there's nothing to "update". | Last copy wins; flag conflicting duplicates. |
| A non-override event with no `workflow_id`, or a missing/invalid phone → orphan. An override with no `call_id`, or whose call has no phone-linked events → orphan. Overrides link only through `call_id` (any phone on them is ignored). | Spec. | |
| If one `call_id` were linked to two different leads, the override goes to the earliest-created lead. | Deterministic; never happens in the data. | Treat as orphan. |
| Orphan is checked before unknown disposition: an unlinked event with an unknown code counts only as an orphan. Both counts reflect the final state. | Spec processing order + "final state". | |
| Unknown disposition = a linked event whose `event_type` starts with `disposition.` and whose code (exact, case-sensitive) is not in the catalogue for the lead's workflow; also when the code is missing. Ignored for everything else. | Catalogue codes are identifiers. | Case-insensitive match. |
| Ties on `occurred_at` (effective disposition) and on initiated time (latest/most-recent call): break by `event_id`, taking the greatest. | Deterministic. | |
| Several `call.initiated` events for one call: the earliest (`occurred_at`, then smallest `event_id`) defines the call's time and date; it is still one call. | A call starts once. | Latest. |
| Events of a call without a linked `call.initiated` (or whose initiated has no parseable time) don't make a call; its `call.connected` etc. are ignored. | Spec: a call counts only if `call.initiated` is present. | |
| An event with an unparseable/missing `occurred_at` is still stored and counted (orphan/unknown as usual), but ignored wherever a time is needed (call start, disposition ordering). | Don't drop data silently; don't invent a time. | Treat as malformed. |
| Timestamps: anything Python's `datetime.fromisoformat` accepts. Naive = IST. Fractional seconds are kept for ordering and dropped in `last_call_at`. | Covers the three formats in the data plus other ISO variants. | Strict regex for the three formats. |
| Event `phone` given as a JSON number is accepted as its digits. Other non-string types count as missing. | Harmless leniency. | Treat as invalid. |
| `batch_summary.csv` has one row per `(workflow_id, batch_id)` with ≥ 1 lead. A batch file where every row was rejected (or that only merged into earlier leads) gets no row; its rejections still appear in `rejections.csv`. | Leads belong to their first batch; a 0-lead row adds nothing. | Emit zero rows for every ingested file. |
| `rejections_by_reason` lists only reasons that occurred, keys sorted alphabetically. JSON uses `indent=2` and ends with a newline. | Stable diffs. | Always list every possible reason. |
| `lead_report.csv` ties on `external_ref` are sorted by `batch_id`, then `phone_masked`, then creation order. `rejections.csv` row numbers sort numerically, with file order as the last tie-break. | Fully deterministic output. | |
| CSV output: `\n` line endings, minimal quoting (Python `csv`), empty string for missing values, booleans `true`/`false`. | Contract. | |
| Idempotency key is `(path inside the drop, sha256)`. The same path with **different** content is ingested as a new file and a warning is printed (rows from both versions count). Identical content at another path is a different file. | A corrected re-upload shouldn't vanish silently, but it also can't "replace" history in an append-only ledger. | Refuse changed files; key on hash only. |
| Within a drop: lead files sorted by (workflow folder, filename), then event files by filename. Lead files for a workflow with no schema are skipped with a warning and *not* registered, so they're picked up once a schema exists. | Spec order. Leads vs events order doesn't matter because linking happens at report time. | Fail the whole drop. |
| Lead files must be UTF-8 (a BOM is tolerated). Otherwise ingest stops with an error and that file's transaction rolls back. | Spec says files are UTF-8. A wrong encoding is an upstream problem worth stopping for. | Decode with replacement characters. |
| The disposition catalogue is read from the schemas dir on every command, so reports always use the current catalogue. | Configuration, not history. | Snapshot the catalogue at ingest. |
| Kept the repository public, although the README asks for a private one. | My choice. The repo contains only code and docs: no generated data, databases or keys. | A private repo with reviewer access, as the README suggests. |

## Failure modes considered
- **Re-runs / partial runs:** per-file transactions + `(rel_path, sha256)` registry. A crash
  leaves each file fully in or fully out, and re-running resumes. Re-ingesting anything already
  ingested is a no-op (tested on row counts and report bytes).
- **Concurrent runs:** `BEGIN IMMEDIATE` takes the SQLite write lock per file, with a 30 s busy
  timeout. Two ingests of the same drop serialise and the second skips everything.
- **Dirty leads:** every format in the generator plus BOMs, short rows, extra/renamed/re-ordered
  columns, Devanagari digits in phones, absurdly long numbers.
- **Dirty events:** truncated lines, non-object JSON, invalid UTF-8, missing/empty ids,
  duplicates within and across drops, missing phones/call_ids/timestamps, unknown event types.
- **Wrong PII key:** detected through the stored key fingerprint; the command exits 1.
- **Lost key file:** *not recoverable*. Hashes can't be recomputed and names can't be
  decrypted. That is the point of encryption at rest, but it means the key must be backed up
  with the DB.
- **Reports half-written:** each file is written to a temp name and atomically renamed.
- **Not handled:**
  - a changed file with the same path is *added*, not reconciled (no "replace batch" semantics);
  - `payload_json` is stored as received, so PII inside a payload (not in the spec) would be
    stored as-is;
  - no file-size streaming (each file is read into memory; fine for 5k-line files);
  - ingesting drops in a different order gives different (but spec-consistent) results, since
    "first row wins" is order-dependent by definition;
  - no key rotation command.

## Testing
`pytest` runs **153 tests in ~15 s**:
- **Normalisers** (`test_normalize.py`): every valid/invalid phone format from the spec, plus
  10-digit numbers starting with `91`, `91`+remainder starting 0–5, 11 digits not starting with
  0, and non-ASCII digits. All four date formats, case-insensitive months, impossible dates, junk.
  Each currency prefix with/without space, Indian commas, negatives, half-up rounding, junk.
  Enums. Timestamps (Z, offset, naive = IST, other offsets) and IST date boundaries
  (`18:29:59Z` vs `18:30:00Z`, `20:00Z` → next IST day).
- **Schemas/rows** (`test_schemas.py`): header normalisation, aliases on reordered headers,
  first-column-wins, rejection order (one reason, schema order), warnings, defaults,
  `invalid_<field>`, schema validation.
- **Lead ingest** (`test_leads_ingest.py`): blank rows and row numbering, merge semantics
  (first batch kept, non-null overwrite, invalid/blank never overwrite, same-file and cross-drop
  duplicates), defaults after merge, rejected rows don't touch leads, warnings on merged rows,
  BOM + `₹`, numeric sort of rejections.
- **Events & derived facts** (`test_events_and_reports.py`): override beats set, latest wins,
  unknown/case-mismatched codes ignored, tie-breaks, current disposition from the latest call that
  has one, all four buckets including "every call telephony-failed", orphans of every kind,
  malformed lines, duplicates, multiple `call.initiated`, daily overview with IST dates, half-up
  rate rounding, override linking.
- **Storage** (`test_storage.py`): UPDATE/DELETE fail on all append-only tables; lead identity is
  frozen; no plaintext phone/name in any column of any table; wrong key refused; key file created
  once with 0600.
- **Pipeline** (`test_pipeline.py`): idempotent re-ingest (row counts + report bytes, also after
  drop 2, also from a copied folder); incremental == one-shot on seeds 1/7/42/123; an
  orphan-then-lead case; the output contract (file set, key order, `\n` endings).
- **Independent cross-check:** `tests/reference_impl.py` is a separate, plain-Python, no-DB
  implementation of the whole spec (it re-implements every parser instead of importing them). All
  five outputs must match the SQL pipeline **byte for byte** on seeds 1/7/42/123, after drop 1
  only, and on a hand-written edge-case drop. I mutation-checked it: breaking the
  telephony-failed bucket rule or the "earliest initiated" rule makes it fail.
- **Not tested:** `--scale large` (too slow for the suite; I ran it by hand, see Results),
  concurrent writers, and crash injection mid-file (relies on SQLite transactions).

## Results
Timings on a small cloud container (Python 3.13, SQLite 3.53):

| Dataset | ingest drop_1 | ingest drop_2 | report | total |
|---|---|---|---|---|
| `--scale small`, seed 42 (~2k lead rows, ~8.7k event lines) | 0.4 s | 0.2 s | 0.3 s | < 1 s |
| `--scale large`, seed 42 (~144k lead rows, ~633k event lines) | 55 s (both drops) | | 7.6 s | ~63 s |

The first version of the report used nested SQL views; on the large set they re-evaluated the
linking for every use and the report didn't finish in 2 minutes. Staging the derivation into
indexed TEMP tables (`derive.sql`) brought it to ~8 s with identical output.

Seed 42, small, `data_quality.json` after both drops:
```json
{
  "lead_rows_total": 2057,
  "lead_rows_rejected": 108,
  "rejections_by_reason": {
    "invalid_phone": 42,
    "missing_required:customer_name": 25,
    "missing_required:order_id": 13,
    "missing_required:phone": 14,
    "missing_required:policy_number": 14
  },
  "duplicate_lead_rows_merged": 57,
  "field_warnings": 122,
  "events_total": 8745,
  "malformed_events": 8,
  "duplicate_events_dropped": 237,
  "orphan_events": 462,
  "unknown_disposition_events": 18
}
```
Observations:
- **Late linking matters a lot.** After `drop_1` alone, 1,123 events are orphans; after
  `drop_2`, 462. **661 events** belonged to leads first uploaded in `batch_003`. A pipeline that
  decided "orphan" at ingest time would under-report those leads permanently.
- 38 duplicate events cross from drop 1 into drop 2, so dedupe must be global, not per file.
- Timestamps are mixed: 3,788 `Z`, 2,986 `+05:30`, 1,726 naive. Calls in the evening (UTC
  date ≠ IST date) land on the next IST day.
- Events for leads that were *rejected* (bad phone, missing name/ref) are counted as orphans, as
  the spec says. 152 stored events have an invalid phone, 70 (all overrides) have none.
- Buckets: 816 completed, 526 dialed, 38 telephony-failed-only, 512 not yet reached.

## Stretch
**(a) Report aggregation in SQL.** All derivation and aggregation is SQL: `derive.sql` (linking,
orphans, call_owner, effective disposition with `ROW_NUMBER()` windows, per-call flags, per-lead
facts and buckets) and the five report queries in `report.py` (`GROUP BY`, `COUNT(DISTINCT …)`,
integer half-up rounding via `printf`). Python only writes the rows out. Stages are TEMP tables
inside one read transaction, so all five files come from the same snapshot.

**(b) Running this for 100k leads/day.**
- **Database:** move to Postgres. The ledger becomes a table partitioned by event date, with
  `REVOKE UPDATE, DELETE`, and the same `UNIQUE(event_id)` / `ON CONFLICT DO NOTHING` dedupe.
  100k leads/day ≈ 0.5M events/day, which is small for Postgres.
- **Scheduling:** an orchestrator (Airflow/Temporal/cron + a lock) triggers `ingest` when a drop
  lands (object-store event or a `_SUCCESS` marker), then `report`. Uploads are immutable objects
  keyed by content hash, which is what the file registry already assumes. Reports are produced on
  a schedule (e.g. 07:00 IST) and also on demand.
- **Failures/retries:** ingest is already safe to retry (per-file transactions + hash registry).
  Retry with backoff; after N failures quarantine the file and alert, don't block the rest of
  the drop. Store a per-file error record so on-call can see *why*.
- **Backfills:** because reports are pure functions of the stored facts, a backfill is "ingest the
  missing files, re-run report". Late leads link automatically. To regenerate a past day's
  report "as of" then, filter facts by `ingested_at`. For rule changes (new bucket rule), only
  `derive.sql` changes; nothing stored needs migrating.
- **Incremental derivation at scale:** re-deriving everything per report is fine up to tens of
  millions of events. Beyond that, keep `call_owner`/`calls`/`lead_facts` as tables refreshed
  only for leads/calls touched since the last run (a watermark on `events.rowid` plus the new
  leads), and keep the full re-derive as a nightly consistency check.
- **Monitoring:** alert on data-quality ratios, not absolute counts: reject rate, orphan rate,
  unknown-disposition rate, duplicate rate per drop vs. a trailing baseline. Track ingest lag
  (drop landed → reports ready). A sudden orphan spike usually means a key or phone-format
  problem upstream.
- **Keys:** keep the master key in a KMS/secret manager, never in a file. Rotation: version the key
  (`key_id` column). New writes use the new key; a background job adds re-hashed phones for old
  rows (a new hash column or a shadow table, since the ledger is append-only) and re-encrypts
  `pii_enc`. Linking uses the current key version once the backfill completes. Access to
  decryption is restricted to the service that actually dials.
- **Retention:** PII deletion requests can be handled by destroying a per-lead data key (crypto
  shredding) without touching the append-only ledger.

## If I had another day
1. Per-file error quarantine instead of aborting the run on one bad lead file (e.g. non-UTF-8).
2. A `verify` command: re-derive facts from scratch and compare with the stored lead state (merge
   replay), and check the key fingerprint and registry hashes against the files on disk.
3. Stream large files instead of reading them whole; batch event inserts with `executemany` and
   in-memory dedupe to cut large-scale ingest from ~55 s to well under 30 s.
4. Explicit "replace batch" semantics for corrected re-uploads (tombstone the old file's rows
   rather than adding to them).
5. Key rotation command and a payload allowlist per event type (so PII can't slip in via payload).
6. Property-based tests (Hypothesis) for the normalisers, plus crash-injection tests for ingest.
