"""Ingest one JSON Lines event file into the append-only ledger.

Per line, at ingest time:
  1. malformed (not JSON / not an object / no usable event_id) -> event_rejects
  2. duplicate event_id (seen in any earlier line or drop)      -> event_rejects
  3. otherwise                                                  -> events (the ledger)

Orphan and unknown-disposition are *not* decided here. They depend on which
leads exist, and a lead can arrive in a later drop, so they are derived at
report time by the SQL views. That keeps step-by-step ingestion and
all-at-once ingestion equivalent.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .normalize import normalize_phone, parse_timestamp
from .pii import PiiCodec


@dataclass
class EventFileStats:
    lines: int = 0
    stored: int = 0
    malformed: int = 0
    duplicates: int = 0


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def parse_event_line(raw: bytes) -> dict | None:
    """Decoded JSON object for a line, or None if the line is malformed."""
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(event, dict) or _str_or_none(event.get("event_id")) is None:
        return None
    return event


def ledger_row(event: dict, codec: PiiCodec) -> dict:
    """Map a parsed event to ledger columns. The raw phone never leaves this function."""
    raw_phone = event.get("phone")
    if isinstance(raw_phone, int) and not isinstance(raw_phone, bool):
        raw_phone = str(raw_phone)
    if not isinstance(raw_phone, str) or raw_phone.strip() == "":
        phone_status, phone_hash = "missing", None
    else:
        canonical = normalize_phone(raw_phone)
        phone_status = "valid" if canonical else "invalid"
        phone_hash = codec.hash_phone(canonical) if canonical else None

    ts = parse_timestamp(event.get("occurred_at"))
    payload = event.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    return {
        "event_id": event["event_id"],
        "event_type": _str_or_none(event.get("event_type")),
        "workflow_id": _str_or_none(event.get("workflow_id")),
        "call_id": _str_or_none(event.get("call_id")),
        "occurred_at_raw": event.get("occurred_at") if isinstance(event.get("occurred_at"), str) else None,
        "occurred_at_utc": ts.utc if ts else None,
        "occurred_at_ist": ts.ist if ts else None,
        "phone_status": phone_status,
        "phone_hash": phone_hash,
        "disposition_code": code if isinstance(code, str) else None,
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload is not None else None,
    }


_INSERT_EVENT = """
INSERT INTO events (event_id, file_id, line_number, event_type, workflow_id, call_id,
                    occurred_at_raw, occurred_at_utc, occurred_at_ist, phone_status,
                    phone_hash, disposition_code, payload_json)
VALUES (:event_id, :file_id, :line_number, :event_type, :workflow_id, :call_id,
        :occurred_at_raw, :occurred_at_utc, :occurred_at_ist, :phone_status,
        :phone_hash, :disposition_code, :payload_json)
ON CONFLICT (event_id) DO NOTHING
"""
# ON CONFLICT(event_id) rather than INSERT OR IGNORE: OR IGNORE would also
# swallow CHECK/NOT NULL violations and count them as "duplicates".


def ingest_event_file(conn: sqlite3.Connection, codec: PiiCodec, file_id: int,
                      data: bytes) -> EventFileStats:
    """Process every line of one already-registered event file (caller owns the transaction)."""
    stats = EventFileStats()
    for line_number, raw in enumerate(data.split(b"\n"), start=1):
        raw = raw.rstrip(b"\r")
        if not raw.strip():
            continue  # empty lines are not events and are not counted
        stats.lines += 1
        event = parse_event_line(raw)
        if event is None:
            stats.malformed += 1
            conn.execute("INSERT INTO event_rejects (file_id, line_number, kind) "
                         "VALUES (?, ?, 'malformed')", (file_id, line_number))
            continue
        row = ledger_row(event, codec)
        cur = conn.execute(_INSERT_EVENT, {**row, "file_id": file_id, "line_number": line_number})
        if cur.rowcount == 1:
            stats.stored += 1
        else:  # first-seen copy wins; this one is only recorded as a duplicate
            stats.duplicates += 1
            conn.execute("INSERT INTO event_rejects (file_id, line_number, kind, event_id) "
                         "VALUES (?, ?, 'duplicate', ?)", (file_id, line_number, row["event_id"]))
    return stats
