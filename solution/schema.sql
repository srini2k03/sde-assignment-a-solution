-- SQLite schema for the interaction ledger.
--
-- Write side (filled by `ingest`):
--   ingested_files  one row per input file; the idempotency registry
--   lead_rows       one row per non-blank CSV data row and what happened to it
--   leads           current merged state of each lead (the only mutable table)
--   events          THE interaction ledger: every distinct event, append-only
--   event_rejects   lines that never reached the ledger (malformed / duplicate)
-- Reference data (re-synced from assignment-a-data/schemas on every command):
--   disposition_catalogue
-- Read side: derive.sql rebuilds every derived fact (linking, calls, buckets)
-- from these tables at report time, so late-arriving leads and overrides link
-- automatically and nothing derived is ever stored.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingested_files (
    file_id      INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    rel_path     TEXT NOT NULL,          -- path inside the drop, e.g. leads/motor_renewal/batch_001.csv
    sha256       TEXT NOT NULL CHECK (length(sha256) = 64),
    source_path  TEXT NOT NULL,          -- where it was read from (informational only)
    workflow_id  TEXT,                   -- leads files only
    batch_id     TEXT,                   -- leads files only (file stem)
    ingested_at  TEXT NOT NULL,
    UNIQUE (rel_path, sha256),
    CHECK ((kind = 'leads') = (workflow_id IS NOT NULL AND batch_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id          INTEGER PRIMARY KEY,
    workflow_id      TEXT NOT NULL,
    phone_hash       TEXT NOT NULL CHECK (length(phone_hash) = 64),  -- HMAC-SHA256 of +91XXXXXXXXXX
    phone_last4      TEXT NOT NULL CHECK (phone_last4 GLOB '[0-9][0-9][0-9][0-9]'),
    batch_id         TEXT NOT NULL,      -- batch of the first valid row; never changes
    first_file_id    INTEGER NOT NULL REFERENCES ingested_files (file_id),
    external_ref     TEXT NOT NULL,      -- value of the schema's "reference" field
    attributes_json  TEXT NOT NULL CHECK (json_valid(attributes_json)),  -- other non-PII fields
    pii_enc          BLOB NOT NULL,      -- Fernet-encrypted JSON of the PII fields
    UNIQUE (workflow_id, phone_hash)     -- lead identity
);

CREATE TABLE IF NOT EXISTS lead_rows (
    file_id         INTEGER NOT NULL REFERENCES ingested_files (file_id),
    row_number      INTEGER NOT NULL CHECK (row_number >= 1),
    outcome         TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reject_reason   TEXT,
    lead_id         INTEGER REFERENCES leads (lead_id),
    field_warnings  INTEGER NOT NULL DEFAULT 0 CHECK (field_warnings >= 0),
    PRIMARY KEY (file_id, row_number),
    CHECK ((outcome = 'rejected') = (reject_reason IS NOT NULL)),
    CHECK ((outcome = 'rejected') = (lead_id IS NULL)),
    CHECK (outcome <> 'rejected' OR field_warnings = 0)
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    file_id           INTEGER NOT NULL REFERENCES ingested_files (file_id),
    line_number       INTEGER NOT NULL CHECK (line_number >= 1),
    event_type        TEXT,
    workflow_id       TEXT,
    call_id           TEXT,
    occurred_at_raw   TEXT,              -- as received (never contains PII)
    occurred_at_utc   TEXT,              -- fixed-width UTC, sortable; NULL if unparseable
    occurred_at_ist   TEXT,              -- same instant in IST, whole seconds
    phone_status      TEXT NOT NULL CHECK (phone_status IN ('valid', 'invalid', 'missing')),
    phone_hash        TEXT CHECK (phone_hash IS NULL OR length(phone_hash) = 64),
    disposition_code  TEXT,
    payload_json      TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
    UNIQUE (file_id, line_number),
    CHECK ((phone_status = 'valid') = (phone_hash IS NOT NULL)),
    CHECK ((occurred_at_utc IS NULL) = (occurred_at_ist IS NULL))
);

CREATE TABLE IF NOT EXISTS event_rejects (
    file_id      INTEGER NOT NULL REFERENCES ingested_files (file_id),
    line_number  INTEGER NOT NULL CHECK (line_number >= 1),
    kind         TEXT NOT NULL CHECK (kind IN ('malformed', 'duplicate')),
    event_id     TEXT,                   -- set for duplicates; raw lines are not kept (may hold PII)
    PRIMARY KEY (file_id, line_number),
    CHECK ((kind = 'duplicate') = (event_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS disposition_catalogue (
    workflow_id  TEXT NOT NULL,
    code         TEXT NOT NULL,
    label        TEXT NOT NULL,
    is_terminal  INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    is_success   INTEGER NOT NULL CHECK (is_success IN (0, 1)),
    PRIMARY KEY (workflow_id, code)
);

CREATE INDEX IF NOT EXISTS idx_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (call_id);
CREATE INDEX IF NOT EXISTS idx_lead_rows_lead ON lead_rows (lead_id);

-- ------------------------------------------------------------ append-only
-- The ledger and every audit table can only grow. `leads` is the one table
-- allowed to change (merges), and even there the identity columns are frozen.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;

CREATE TRIGGER IF NOT EXISTS event_rejects_no_update BEFORE UPDATE ON event_rejects
BEGIN SELECT RAISE(ABORT, 'event_rejects is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_rejects_no_delete BEFORE DELETE ON event_rejects
BEGIN SELECT RAISE(ABORT, 'event_rejects is append-only'); END;

CREATE TRIGGER IF NOT EXISTS lead_rows_no_update BEFORE UPDATE ON lead_rows
BEGIN SELECT RAISE(ABORT, 'lead_rows is append-only'); END;
CREATE TRIGGER IF NOT EXISTS lead_rows_no_delete BEFORE DELETE ON lead_rows
BEGIN SELECT RAISE(ABORT, 'lead_rows is append-only'); END;

CREATE TRIGGER IF NOT EXISTS ingested_files_no_update BEFORE UPDATE ON ingested_files
BEGIN SELECT RAISE(ABORT, 'ingested_files is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ingested_files_no_delete BEFORE DELETE ON ingested_files
BEGIN SELECT RAISE(ABORT, 'ingested_files is append-only'); END;

CREATE TRIGGER IF NOT EXISTS leads_identity_frozen
BEFORE UPDATE OF workflow_id, phone_hash, phone_last4, batch_id, first_file_id ON leads
BEGIN SELECT RAISE(ABORT, 'lead identity and batch_id are immutable'); END;
CREATE TRIGGER IF NOT EXISTS leads_no_delete BEFORE DELETE ON leads
BEGIN SELECT RAISE(ABORT, 'leads cannot be deleted'); END;
