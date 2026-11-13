# Assignment A solution: interaction ledger & reporting pipeline

Design, decisions and results are in [`../SUBMISSION.md`](../SUBMISSION.md). This file is the
map of the code.

## Usage

Run from the repo root (Python 3.11+, `pip install -r requirements.txt`):

```bash
python -m solution --db ledger.db ingest data/drop_1
python -m solution --db ledger.db ingest data/drop_2
python -m solution --db ledger.db ingest data/drop_1 data/drop_2   # several drops, processed in the order given
python -m solution --db ledger.db report --out reports/
```

| Option | Default | Meaning |
|---|---|---|
| `--db PATH` | `ledger.db` (current dir) | SQLite database file. Created on first use. |
| `--schemas DIR` | `assignment-a-data/schemas/` (next to this package) | Lead schemas (`*.json`) and `dispositions.csv`. |
| `ingest DROP [DROP ...]` | | Ingest drop directories in the order given. Files already ingested are skipped. |
| `report --out DIR` | | Write the five report files into `DIR` (created if missing). |

PII key: taken from the `AU_PII_KEY` env var. If it is unset, a random key is generated once into
`<db>.key` (mode 0600, gitignored) and reused on later runs. Running against an existing DB
with a different key fails with an error instead of silently producing orphans.

Exit code `0` on success, `1` on bad input (missing drop, broken schema, wrong key).

Tests: `pytest` from the repo root.

## Module layout

| File | Responsibility |
|---|---|
| `__main__.py` | `python -m solution` entry point. |
| `cli.py` | Argument parsing; wires the catalogue, PII codec, DB, ingest and report together. |
| `normalize.py` | Pure value parsers: phone, date, decimal, enum, event timestamps (UTC/IST). |
| `schemas.py` | Loads workflow schemas and `dispositions.csv`; header mapping and per-row validation. |
| `pii.py` | Key loading/generation, HMAC phone hashing and Fernet encryption of PII. |
| `schema.sql` | Database tables, constraints, indexes and append-only triggers. |
| `db.py` | Connection setup, explicit transactions, key fingerprint check, catalogue sync. |
| `ingest.py` | Discovers drop files, idempotency registry (path + SHA-256), one transaction per file. |
| `leads.py` | One lead CSV: blank rows, row numbers, rejections, create/merge leads. |
| `events.py` | One JSONL file: malformed/duplicate detection and appending to the ledger. |
| `derive.sql` | Report-time derivation in SQL: linking, orphans, calls, effective dispositions, buckets. |
| `report.py` | Report queries (SQL aggregation) and writing the five files. |
| `tests/reference_impl.py` | Independent plain-Python implementation used to cross-check outputs. |
| `tests/test_*.py` | Unit and end-to-end tests (see `SUBMISSION.md` → Testing). |

Data flow: `ingest` stores **facts** only (lead rows and their outcome, merged lead state, raw
events). `report` rebuilds every **derived** fact from those tables each time (`derive.sql`).
That separation is what makes incremental and one-shot ingestion give identical reports.

## How to extend

| Likely request | Where to change |
|---|---|
| **New workflow** | Drop a `<workflow>.json` into the schemas dir and add its rows to `dispositions.csv`. No code change needed. Lead files go in `<drop>/leads/<workflow_id>/`. |
| **New field type** (e.g. `email`, `integer`) | Add a parser in `normalize.py`, add the type to `FIELD_TYPES` and to `FieldSpec.parse` in `schemas.py`. Tests go in `test_normalize.py`. |
| **New column in `lead_report.csv`** | Compute it in the `lead_facts` stage of `derive.sql`, select it in `LEAD_REPORT` and add it to `HEADERS` in `report.py`. Mirror it in `tests/reference_impl.py`. Lead attributes are in `leads.attributes_json` (`json_extract(attributes_json, '$.preferred_language')`). |
| **New report file** | Add a query + header in `report.py` and an entry in `build_reports`. |
| **New event type** (e.g. `call.voicemail`) | Events of any type are already stored. Use it in `derive.sql`: e.g. a new `MAX(event_type = 'call.voicemail')` flag in the `flags` CTE of `calls`. |
| **New/changed bucket rule** | The `CASE` at the end of `derive.sql` (`lead_facts`), plus any per-call flag it needs in `calls`. Mirror it in the reference implementation. |
| **Different effective-disposition rule** | The `dispositions` CTE `ORDER BY` in `derive.sql`. |
| **New data-quality counter** | `DATA_QUALITY` query and the `quality` dict in `report.py` (key order there is the output order). |
| **Switch to Postgres** | `db.py` (driver, `BEGIN` instead of `BEGIN IMMEDIATE`) and `schema.sql` (`BIGSERIAL` keys, `BYTEA`, `jsonb`, triggers become a PL/pgSQL function or simply `REVOKE UPDATE, DELETE`). `derive.sql` and the report queries are standard SQL apart from `printf` → `to_char`/`format`, `substr`, `MAX(boolean)` → `bool_or`, and `ON CONFLICT` already works. `TEMP` tables work the same. |
| **Re-key PII** | Read each `leads.pii_enc`, decrypt with the old key and re-hash/re-encrypt with the new one. The ledger is append-only, so events would get a new `phone_hash` column (or a shadow table) rather than an UPDATE; see `SUBMISSION.md` → production note. |
