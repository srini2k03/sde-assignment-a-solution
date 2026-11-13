"""Build the five report files from the database.

All aggregation happens in SQL: derive.sql builds the derived facts (linking,
calls, effective dispositions, buckets) as TEMP tables, and the queries below
aggregate them. Python only formats rows and writes files. Each file is written to a
temporary name and then renamed, so a crash never leaves a half-written
report behind.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from pathlib import Path

from .db import read_sql, run_script, transaction

REPORT_FILES = ("batch_summary.csv", "daily_overview.csv", "lead_report.csv",
                "rejections.csv", "data_quality.json")

BATCH_SUMMARY = """
SELECT workflow_id, batch_id,
       COUNT(*)                                          AS total_leads,
       SUM(bucket = 'yet_to_reach_out')                  AS yet_to_reach_out,
       SUM(bucket = 'in_progress_telephony_failed')      AS in_progress_telephony_failed,
       SUM(bucket = 'in_progress_dialed')                AS in_progress_dialed,
       SUM(bucket = 'completed')                         AS completed
FROM lead_facts
GROUP BY workflow_id, batch_id
ORDER BY workflow_id, batch_id
"""

# connectivity_rate is rounded half-up to 4 decimals in exact integer arithmetic:
#   q = floor(connected * 10000 / total + 1/2) = (2 * connected * 10000 + total) / (2 * total)
DAILY_OVERVIEW = """
WITH daily AS (
    SELECT l.workflow_id, c.call_date,
           COUNT(*)          AS total_calls,
           SUM(c.connected)  AS connected_calls,
           COUNT(DISTINCT CASE WHEN c.is_success = 1 THEN c.lead_id END) AS success_leads
    FROM calls c
    JOIN leads l ON l.lead_id = c.lead_id
    GROUP BY l.workflow_id, c.call_date
)
SELECT workflow_id, call_date, total_calls, connected_calls,
       printf('%d.%04d', q / 10000, q % 10000) AS connectivity_rate,
       success_leads
FROM (SELECT *, (2 * connected_calls * 10000 + total_calls) / (2 * total_calls) AS q FROM daily)
ORDER BY workflow_id, call_date
"""

LEAD_REPORT = """
SELECT workflow_id, external_ref, batch_id, phone_masked, bucket, total_calls, connected_calls,
       COALESCE(last_call_at, '')        AS last_call_at,
       COALESCE(current_disposition, '') AS current_disposition,
       CASE WHEN ever_success = 1 THEN 'true' ELSE 'false' END AS ever_success
FROM lead_facts
ORDER BY workflow_id, external_ref, batch_id, phone_masked, lead_id
"""

REJECTIONS = """
SELECT f.workflow_id, f.batch_id, r.row_number, r.reject_reason
FROM lead_rows r
JOIN ingested_files f ON f.file_id = r.file_id
WHERE r.outcome = 'rejected'
ORDER BY f.workflow_id, f.batch_id, r.row_number, f.file_id
"""

DATA_QUALITY = """
SELECT
    (SELECT COUNT(*) FROM lead_rows)                                   AS lead_rows_total,
    (SELECT COUNT(*) FROM lead_rows WHERE outcome = 'rejected')        AS lead_rows_rejected,
    (SELECT COUNT(*) FROM lead_rows WHERE outcome = 'merged')          AS duplicate_lead_rows_merged,
    (SELECT COALESCE(SUM(field_warnings), 0) FROM lead_rows)           AS field_warnings,
    (SELECT COUNT(*) FROM events) + (SELECT COUNT(*) FROM event_rejects) AS events_total,
    (SELECT COUNT(*) FROM event_rejects WHERE kind = 'malformed')      AS malformed_events,
    (SELECT COUNT(*) FROM event_rejects WHERE kind = 'duplicate')      AS duplicate_events_dropped,
    (SELECT COUNT(*) FROM events) - (SELECT COUNT(*) FROM linked_events) AS orphan_events,
    (SELECT COUNT(*) FROM unknown_disposition_events)                  AS unknown_disposition_events
"""

REJECTIONS_BY_REASON = """
SELECT reject_reason, COUNT(*) FROM lead_rows
WHERE outcome = 'rejected'
GROUP BY reject_reason
ORDER BY reject_reason
"""

HEADERS = {
    "batch_summary.csv": ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                          "in_progress_telephony_failed", "in_progress_dialed", "completed"],
    "daily_overview.csv": ["workflow_id", "date", "total_calls", "connected_calls",
                           "connectivity_rate", "success_leads"],
    "lead_report.csv": ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
                        "total_calls", "connected_calls", "last_call_at", "current_disposition",
                        "ever_success"],
    "rejections.csv": ["workflow_id", "batch_id", "row_number", "reason"],
}


def build_reports(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {filename: file content} for all five reports."""
    # One read transaction, so all five files see the same snapshot.
    with transaction(conn):
        run_script(conn, read_sql("derive.sql"))

        out = {
            "batch_summary.csv": _csv(HEADERS["batch_summary.csv"], conn.execute(BATCH_SUMMARY)),
            "daily_overview.csv": _csv(HEADERS["daily_overview.csv"], conn.execute(DAILY_OVERVIEW)),
            "lead_report.csv": _csv(HEADERS["lead_report.csv"], conn.execute(LEAD_REPORT)),
            "rejections.csv": _csv(HEADERS["rejections.csv"], conn.execute(REJECTIONS)),
        }

        cur = conn.execute(DATA_QUALITY)
        names = [d[0] for d in cur.description]
        q = dict(zip(names, cur.fetchone()))
        by_reason = dict(conn.execute(REJECTIONS_BY_REASON).fetchall())
        quality = {
            "lead_rows_total": q["lead_rows_total"],
            "lead_rows_rejected": q["lead_rows_rejected"],
            "rejections_by_reason": by_reason,
            "duplicate_lead_rows_merged": q["duplicate_lead_rows_merged"],
            "field_warnings": q["field_warnings"],
            "events_total": q["events_total"],
            "malformed_events": q["malformed_events"],
            "duplicate_events_dropped": q["duplicate_events_dropped"],
            "orphan_events": q["orphan_events"],
            "unknown_disposition_events": q["unknown_disposition_events"],
        }
        out["data_quality.json"] = json.dumps(quality, indent=2) + "\n"
    return out


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow("" if v is None else v for v in row)
    return buf.getvalue()


def write_reports(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in build_reports(conn).items():
        target = out_dir / name
        tmp = out_dir / f".{name}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, target)
        written.append(target)
    return written
