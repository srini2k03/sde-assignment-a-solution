"""Discover the files in a drop and ingest each one exactly once.

Idempotency: every file is registered in ``ingested_files`` under
(path inside the drop, SHA-256 of its bytes). A file whose (path, hash) is
already registered is skipped, so re-running ``ingest`` changes nothing.
Each file is processed in its own transaction together with its registry row.
A crash therefore leaves every file either fully ingested or not at all,
and a re-run resumes where the previous one stopped.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .db import transaction
from .events import ingest_event_file
from .leads import ingest_lead_file
from .pii import PiiCodec
from .schemas import Catalogue


class DropError(ValueError):
    pass


@dataclass(frozen=True)
class DropFile:
    kind: str                  # 'leads' | 'events'
    path: Path
    rel_path: str              # posix path relative to the drop root
    workflow_id: str | None = None
    batch_id: str | None = None


def discover(drop: Path) -> list[DropFile]:
    """Files of one drop in processing order: lead files by (workflow, filename), then events by filename.

    Leads and events are independent at ingest time (linking happens at report
    time), so their relative order does not affect any output.
    """
    if not drop.is_dir():
        raise DropError(f"drop directory not found: {drop}")
    leads_dir, events_dir = drop / "leads", drop / "events"
    if not leads_dir.is_dir() and not events_dir.is_dir():
        raise DropError(f"{drop} has neither a leads/ nor an events/ directory")

    files: list[DropFile] = []
    if leads_dir.is_dir():
        for wf_dir in sorted(p for p in leads_dir.iterdir() if p.is_dir()):
            for path in sorted(wf_dir.glob("*.csv")):
                files.append(DropFile("leads", path, path.relative_to(drop).as_posix(),
                                      workflow_id=wf_dir.name, batch_id=path.stem))
    if events_dir.is_dir():
        for path in sorted(events_dir.glob("*.jsonl")):
            files.append(DropFile("events", path, path.relative_to(drop).as_posix()))
    return files


def ingest_drops(conn: sqlite3.Connection, codec: PiiCodec, catalogue: Catalogue,
                 drops: list[Path], log: Callable[[str], None] = print) -> None:
    """Ingest the given drops in the given order."""
    for drop in drops:
        for f in discover(Path(drop)):
            _ingest_file(conn, codec, catalogue, f, log)


def _ingest_file(conn: sqlite3.Connection, codec: PiiCodec, catalogue: Catalogue,
                 f: DropFile, log: Callable[[str], None]) -> None:
    data = f.path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    if f.kind == "leads" and f.workflow_id not in catalogue.workflows:
        # Not registered, so it is picked up once a schema for it exists.
        print(f"warning: skipping {f.path}: no schema for workflow {f.workflow_id!r}",
              file=sys.stderr)
        return

    with transaction(conn):
        known = {row[0] for row in conn.execute(
            "SELECT sha256 FROM ingested_files WHERE rel_path = ?", (f.rel_path,))}
        if sha in known:
            log(f"skip    {f.rel_path} (already ingested)")
            return
        if known:
            print(f"warning: {f.rel_path} was ingested before with different content; "
                  "ingesting this version as a new file", file=sys.stderr)

        file_id = conn.execute(
            "INSERT INTO ingested_files (kind, rel_path, sha256, source_path, workflow_id, "
            "batch_id, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f.kind, f.rel_path, sha, str(f.path), f.workflow_id, f.batch_id,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ).lastrowid

        if f.kind == "leads":
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DropError(f"{f.path} is not valid UTF-8: {exc}") from exc
            s = ingest_lead_file(conn, codec, catalogue.workflows[f.workflow_id],
                                 file_id, f.batch_id, text)
            log(f"ingest  {f.rel_path}: {s.rows} rows, {s.created} new leads, "
                f"{s.merged} merged, {s.rejected} rejected, {s.warnings} field warnings")
        else:
            s = ingest_event_file(conn, codec, file_id, data)
            log(f"ingest  {f.rel_path}: {s.lines} lines, {s.stored} stored, "
                f"{s.duplicates} duplicates, {s.malformed} malformed")
