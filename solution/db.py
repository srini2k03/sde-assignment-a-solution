"""SQLite connection handling, schema setup and reference-data sync."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from typing import Iterator

from .pii import PiiCodec
from .schemas import Catalogue

SCHEMA_VERSION = "1"


class KeyMismatchError(RuntimeError):
    pass


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None: autocommit mode, so the only transactions are the
    # explicit ones opened by transaction() below. No hidden implicit BEGINs.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(read_sql("schema.sql"))
    return conn


def run_script(conn: sqlite3.Connection, sql: str) -> None:
    """Execute a multi-statement SQL script inside the caller's transaction.

    (sqlite3's executescript() would COMMIT the open transaction first.)
    """
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip() and not all(
            l.strip().startswith("--") or not l.strip() for l in statement.splitlines()):
        raise ValueError(f"incomplete SQL statement at end of script: {statement[:80]!r}")


def read_sql(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text("utf-8")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing unit of work (BEGIN IMMEDIATE takes the write lock up front)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def check_key(conn: sqlite3.Connection, codec: PiiCodec) -> None:
    """Remember the PII key fingerprint on first use; refuse a different key later.

    With the wrong key every phone hash would differ, so nothing would link and
    every event would silently become an orphan. Failing loudly is better.
    """
    with transaction(conn):
        rows = dict(conn.execute("SELECT key, value FROM meta"))
        stored = rows.get("pii_key_fingerprint")
        if stored is None:
            conn.execute("INSERT INTO meta VALUES ('pii_key_fingerprint', ?)", (codec.fingerprint,))
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        elif stored != codec.fingerprint:
            raise KeyMismatchError(
                "the PII key does not match the one this database was created with "
                "(check AU_PII_KEY or the <db>.key file)"
            )


def sync_catalogue(conn: sqlite3.Connection, catalogue: Catalogue) -> None:
    """Replace the disposition catalogue with the current CSV contents.

    The catalogue is configuration, not ledger data, so reports always use the
    current file. It lives in a real table (not a TEMP one) because the
    persistent views join against it.
    """
    with transaction(conn):
        conn.execute("DELETE FROM disposition_catalogue")
        conn.executemany(
            "INSERT INTO disposition_catalogue VALUES (?, ?, ?, ?, ?)",
            [(d.workflow_id, d.code, d.label, int(d.is_terminal), int(d.is_success))
             for d in catalogue.dispositions],
        )
