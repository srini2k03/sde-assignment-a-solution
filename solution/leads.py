"""Ingest one lead CSV file: validate rows, record rejections, create or merge leads."""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass

from .pii import PiiCodec
from .schemas import WorkflowSchema


@dataclass
class LeadFileStats:
    rows: int = 0
    rejected: int = 0
    created: int = 0
    merged: int = 0
    warnings: int = 0


def _is_blank(cells: list[str]) -> bool:
    return all(not c.strip() for c in cells)


def ingest_lead_file(conn: sqlite3.Connection, codec: PiiCodec, schema: WorkflowSchema,
                     file_id: int, batch_id: str, text: str) -> LeadFileStats:
    """Process every row of one already-registered lead file (caller owns the transaction)."""
    stats = LeadFileStats()
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None:  # completely empty file
        return stats
    columns = schema.map_columns(header)

    row_number = 0
    for cells in reader:
        if _is_blank(cells):
            continue  # blank rows are not counted anywhere
        row_number += 1
        stats.rows += 1
        parsed = schema.parse_row(columns, cells)

        if parsed.reject_reason is not None:
            stats.rejected += 1
            conn.execute(
                "INSERT INTO lead_rows (file_id, row_number, outcome, reject_reason) "
                "VALUES (?, ?, 'rejected', ?)",
                (file_id, row_number, parsed.reject_reason),
            )
            continue

        lead_id, outcome = _upsert_lead(conn, codec, schema, file_id, batch_id, parsed.values)
        stats.warnings += parsed.field_warnings
        if outcome == "created":
            stats.created += 1
        else:
            stats.merged += 1
        conn.execute(
            "INSERT INTO lead_rows (file_id, row_number, outcome, lead_id, field_warnings) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_id, row_number, outcome, lead_id, parsed.field_warnings),
        )
    return stats


def _split_fields(schema: WorkflowSchema, values: dict[str, str | None]
                  ) -> tuple[str | None, dict[str, str | None], dict[str, str | None]]:
    """-> (external_ref, non-PII attributes, PII fields to encrypt)."""
    ref_name = schema.reference.name
    attributes: dict[str, str | None] = {}
    pii: dict[str, str | None] = {}
    for f in schema.fields:
        if f.name == ref_name:
            continue
        (pii if f.pii or f.type == "phone" else attributes)[f.name] = values.get(f.name)
    return values.get(ref_name), attributes, pii


def _upsert_lead(conn: sqlite3.Connection, codec: PiiCodec, schema: WorkflowSchema,
                 file_id: int, batch_id: str, values: dict[str, str | None]) -> tuple[int, str]:
    """Create the lead for this identity, or merge this row into the existing one.

    Merge rule: a later row overwrites only the fields where its value is
    non-null (last write wins). batch_id is never touched after creation.

    Defaults are applied when the state is written. That is equivalent to the
    spec's "defaults after merging": a default only fills a null, a later
    non-null value overwrites it either way, and a later null never overwrites.
    """
    phone = values[schema.identity.name]
    assert phone is not None  # a non-rejected row always has a valid identity phone
    phone_hash = codec.hash_phone(phone)

    existing = conn.execute(
        "SELECT lead_id, external_ref, attributes_json, pii_enc FROM leads "
        "WHERE workflow_id = ? AND phone_hash = ?",
        (schema.workflow_id, phone_hash),
    ).fetchone()

    if existing is None:
        merged = schema.apply_defaults(values)
        ref, attributes, pii = _split_fields(schema, merged)
        cur = conn.execute(
            "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id, first_file_id, "
            "external_ref, attributes_json, pii_enc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (schema.workflow_id, phone_hash, phone[-4:], batch_id, file_id, ref,
             json.dumps(attributes, sort_keys=True), codec.encrypt(pii)),
        )
        return cur.lastrowid, "created"

    lead_id, old_ref, attributes_json, pii_enc = existing
    current = {schema.reference.name: old_ref, **json.loads(attributes_json),
               **codec.decrypt(pii_enc)}
    for name, value in values.items():
        if value is not None:
            current[name] = value
    ref, attributes, pii = _split_fields(schema, schema.apply_defaults(current))
    conn.execute(
        "UPDATE leads SET external_ref = ?, attributes_json = ?, pii_enc = ? WHERE lead_id = ?",
        (ref, json.dumps(attributes, sort_keys=True), codec.encrypt(pii), lead_id),
    )
    return lead_id, "merged"
