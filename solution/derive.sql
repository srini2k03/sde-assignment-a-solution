-- Report-time derivation. Runs inside the report's read transaction and builds
-- TEMP tables in stages; each stage is computed once and indexed for the next.
-- TEMP tables live only for this connection and never touch the ledger.
--
-- Nothing here is persisted: every report reflects everything ingested so far,
-- however and in whatever order it was ingested.

DROP TABLE IF EXISTS temp.linked_events;
DROP TABLE IF EXISTS temp.call_owner;
DROP TABLE IF EXISTS temp.unknown_disposition_events;
DROP TABLE IF EXISTS temp.calls;
DROP TABLE IF EXISTS temp.lead_facts;

-- 1. Non-override events link to a lead by (workflow_id, phone_hash).
CREATE TEMP TABLE linked_events AS
SELECT l.lead_id, l.workflow_id AS lead_workflow_id, e.*
FROM events e
JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
WHERE e.event_type IS NOT 'disposition.override';

-- 2. The lead that owns each call: the lead its phone-linked events point to.
--    (If one call_id were ever split across leads, the earliest-created lead wins.)
CREATE TEMP TABLE call_owner AS
SELECT call_id, MIN(lead_id) AS lead_id
FROM linked_events
WHERE call_id IS NOT NULL
GROUP BY call_id;
CREATE UNIQUE INDEX temp.idx_call_owner ON call_owner (call_id);

-- 3. Overrides carry no phone; they link through their call_id.
INSERT INTO linked_events
SELECT o.lead_id, l.workflow_id, e.*
FROM events e
JOIN call_owner o ON o.call_id = e.call_id
JOIN leads l ON l.lead_id = o.lead_id
WHERE e.event_type = 'disposition.override';
CREATE INDEX temp.idx_linked_call ON linked_events (lead_id, call_id);

-- Everything in `events` that is not in `linked_events` is an orphan.

-- 4. Linked disposition.* events whose code is not in the lead's workflow
--    catalogue (exact, case-sensitive). They are counted and otherwise ignored.
CREATE TEMP TABLE unknown_disposition_events AS
SELECT le.event_id
FROM linked_events le
WHERE substr(le.event_type, 1, 12) = 'disposition.'
  AND NOT EXISTS (SELECT 1 FROM disposition_catalogue c
                  WHERE c.workflow_id = le.lead_workflow_id AND c.code = le.disposition_code);

-- 5. One row per counted call: a (lead, call_id) with a linked call.initiated.
CREATE TEMP TABLE calls AS
WITH initiated AS (
    SELECT lead_id, call_id, event_id, occurred_at_utc, occurred_at_ist,
           ROW_NUMBER() OVER (PARTITION BY lead_id, call_id
                              ORDER BY occurred_at_utc, event_id) AS rn   -- earliest wins
    FROM linked_events
    WHERE event_type = 'call.initiated' AND call_id IS NOT NULL AND occurred_at_utc IS NOT NULL
),
flags AS (
    SELECT lead_id, call_id,
           MAX(event_type = 'call.connected')        AS connected,
           MAX(event_type = 'call.telephony_failed') AS telephony_failed
    FROM linked_events
    WHERE call_id IS NOT NULL
    GROUP BY lead_id, call_id
),
dispositions AS (
    -- Effective disposition: latest override, else latest set (ties: greatest event_id).
    SELECT le.lead_id, le.call_id, le.disposition_code, c.is_terminal, c.is_success,
           ROW_NUMBER() OVER (PARTITION BY le.lead_id, le.call_id
                              ORDER BY le.event_type = 'disposition.override' DESC,
                                       le.occurred_at_utc DESC, le.event_id DESC) AS rn
    FROM linked_events le
    JOIN disposition_catalogue c
      ON c.workflow_id = le.lead_workflow_id AND c.code = le.disposition_code
    WHERE le.event_type IN ('disposition.set', 'disposition.override')
      AND le.call_id IS NOT NULL AND le.occurred_at_utc IS NOT NULL
)
SELECT i.lead_id,
       i.call_id,
       i.event_id                       AS initiated_event_id,
       i.occurred_at_utc                AS initiated_at_utc,
       i.occurred_at_ist                AS initiated_at_ist,
       substr(i.occurred_at_ist, 1, 10) AS call_date,          -- IST date
       f.connected,
       f.telephony_failed,
       d.disposition_code               AS effective_disposition,
       COALESCE(d.is_terminal, 0)       AS is_terminal,
       COALESCE(d.is_success, 0)        AS is_success
FROM initiated i
JOIN flags f ON f.lead_id = i.lead_id AND f.call_id = i.call_id
LEFT JOIN dispositions d ON d.lead_id = i.lead_id AND d.call_id = i.call_id AND d.rn = 1
WHERE i.rn = 1;
CREATE INDEX temp.idx_calls_lead ON calls (lead_id);

-- 6. One row per lead with everything the lead report and buckets need.
CREATE TEMP TABLE lead_facts AS
WITH ranked AS (
    SELECT c.*,
           ROW_NUMBER() OVER (PARTITION BY lead_id
                              ORDER BY initiated_at_utc DESC, initiated_event_id DESC) AS recency,
           ROW_NUMBER() OVER (PARTITION BY lead_id
                              ORDER BY effective_disposition IS NULL,
                                       initiated_at_utc DESC, initiated_event_id DESC) AS disp_recency
    FROM calls c
),
per_lead AS (
    SELECT lead_id,
           COUNT(*)              AS total_calls,
           SUM(connected)        AS connected_calls,
           SUM(telephony_failed) AS failed_calls,
           MAX(is_terminal)      AS any_terminal,
           MAX(is_success)       AS ever_success,
           MAX(CASE WHEN recency = 1 THEN initiated_at_ist END)           AS last_call_at,
           MAX(CASE WHEN disp_recency = 1 THEN effective_disposition END) AS current_disposition
    FROM ranked
    GROUP BY lead_id
)
SELECT l.lead_id, l.workflow_id, l.external_ref, l.batch_id,
       '+91******' || l.phone_last4   AS phone_masked,
       COALESCE(p.total_calls, 0)     AS total_calls,
       COALESCE(p.connected_calls, 0) AS connected_calls,
       p.last_call_at,
       p.current_disposition,
       COALESCE(p.ever_success, 0)    AS ever_success,
       CASE
           WHEN COALESCE(p.total_calls, 0) = 0 THEN 'yet_to_reach_out'
           WHEN p.any_terminal = 1             THEN 'completed'
           WHEN p.failed_calls = p.total_calls THEN 'in_progress_telephony_failed'
           ELSE 'in_progress_dialed'
       END AS bucket
FROM leads l
LEFT JOIN per_lead p ON p.lead_id = l.lead_id;
