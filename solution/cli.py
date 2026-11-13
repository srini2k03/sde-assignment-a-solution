"""Command line interface.

    python -m solution [--db ledger.db] [--schemas DIR] ingest DROP [DROP ...]
    python -m solution [--db ledger.db] [--schemas DIR] report --out DIR
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import KeyMismatchError, check_key, connect, sync_catalogue
from .ingest import DropError, ingest_drops
from .pii import PiiCodec, load_master_key
from .report import write_reports
from .schemas import SchemaError, load_catalogue

# Default schemas location relative to this package, so the command works from any cwd.
DEFAULT_SCHEMAS = Path(__file__).resolve().parent.parent / "assignment-a-data" / "schemas"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m solution",
                                description="Interaction ledger & reporting pipeline")
    p.add_argument("--db", default="ledger.db", help="SQLite database file (default: ledger.db)")
    p.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS,
                   help="directory with <workflow>.json schemas and dispositions.csv "
                        f"(default: {DEFAULT_SCHEMAS})")
    sub = p.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="ingest one or more drop directories, in the order given")
    ing.add_argument("drops", nargs="+", type=Path, metavar="DROP")

    rep = sub.add_parser("report", help="write the five report files")
    rep.add_argument("--out", type=Path, required=True, help="output directory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalogue = load_catalogue(args.schemas)
        codec = PiiCodec(load_master_key(args.db))
        conn = connect(args.db)
        try:
            check_key(conn, codec)
            sync_catalogue(conn, catalogue)
            if args.command == "ingest":
                ingest_drops(conn, codec, catalogue, args.drops)
            else:
                for path in write_reports(conn, args.out):
                    print(f"wrote {path}")
        finally:
            conn.close()
    except (SchemaError, DropError, KeyMismatchError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
