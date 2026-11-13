"""Database guarantees: append-only ledger, no plaintext PII, key handling."""
import csv
import re
import sqlite3
from contextlib import contextmanager

import pytest

from solution.cli import main
from solution.normalize import normalize_phone
from solution.tests.conftest import SCHEMAS, generate


@contextmanager
def raw_db(path):
    # Autocommit, so a failed statement never leaves a transaction (and a lock) behind.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("storage")
    data = generate(7, tmp / "data")
    db = tmp / "ledger.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AU_PII_KEY", "test-only-key")
        assert main(["--db", str(db), "--schemas", str(SCHEMAS), "ingest",
                     str(data / "drop_1"), str(data / "drop_2")]) == 0
    return db, data


@pytest.mark.parametrize("table", ["events", "event_rejects", "lead_rows", "ingested_files"])
def test_audit_tables_are_append_only(ingested, table):
    with raw_db(ingested[0]) as conn:
        _assert_append_only(conn, table)


def _assert_append_only(conn, table):
    col = conn.execute(f"SELECT name FROM pragma_table_info('{table}') LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET {col} = {col}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] > 0


def test_lead_identity_and_batch_are_frozen(ingested):
    with raw_db(ingested[0]) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE leads SET batch_id = 'batch_999'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM leads")


def test_no_plaintext_pii_anywhere_in_the_database(ingested):
    db, data = ingested
    phones, names = set(), set()
    for path in data.glob("drop_*/leads/*/*.csv"):
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for key, value in row.items():
                    k = (key or "").lower()
                    if k in ("phone", "mobile no", "contact") and value and normalize_phone(value):
                        phones.add(normalize_phone(value)[3:])  # the 10 significant digits
                    if k in ("customer_name", "insured name", "name") and value.strip():
                        names.add(value.strip())
    for path in data.glob("drop_*/events/*.jsonl"):
        for raw in re.findall(r'"phone": "([^"]*)"', path.read_text("utf-8")):
            if normalize_phone(raw):
                phones.add(normalize_phone(raw)[3:])
    assert len(phones) > 1000 and len(names) > 100

    with raw_db(db) as conn:
        _assert_no_pii(conn, phones, names)


def _assert_no_pii(conn, phones, names):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    digit_runs = re.compile(r"[0-9]{10}")
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
        for row in conn.execute(f"SELECT * FROM {table}"):
            for col, value in zip(cols, row):
                if value is None or isinstance(value, (int, float)):
                    continue
                text = value.decode("latin-1") if isinstance(value, bytes) else value
                for run in digit_runs.findall(text):
                    assert run not in phones, f"plaintext phone in {table}.{col}"
                assert not any(n in text for n in names), f"plaintext name in {table}.{col}"


def test_events_with_invalid_phone_are_stored_without_the_phone(ingested):
    with raw_db(ingested[0]) as conn:
        n = conn.execute("SELECT COUNT(*) FROM events WHERE phone_status = 'invalid' "
                         "AND phone_hash IS NULL").fetchone()[0]
    assert n > 0


def test_wrong_key_is_refused(ingested, monkeypatch, capsys):
    monkeypatch.setenv("AU_PII_KEY", "a-different-key")
    assert main(["--db", str(ingested[0]), "--schemas", str(SCHEMAS), "report",
                 "--out", str(ingested[0].parent / "r")]) == 1
    assert "PII key does not match" in capsys.readouterr().err


def test_key_file_generated_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("AU_PII_KEY")
    db = tmp_path / "x.db"
    (tmp_path / "d" / "events").mkdir(parents=True)
    (tmp_path / "d" / "events" / "events_0001.jsonl").write_text("")
    args = ["--db", str(db), "--schemas", str(SCHEMAS), "ingest", str(tmp_path / "d")]
    assert main(args) == 0
    key = (tmp_path / "x.db.key").read_text()
    assert (tmp_path / "x.db.key").stat().st_mode & 0o077 == 0     # not group/world readable
    assert main(args) == 0
    assert (tmp_path / "x.db.key").read_text() == key
