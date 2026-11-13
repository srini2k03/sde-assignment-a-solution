"""Shared fixtures: build tiny drops on disk, run the CLI, read the reports."""
from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from solution.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"
GENERATOR = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"

MOTOR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry", "premium_amount",
                "preferred_language"]


@pytest.fixture(autouse=True)
def _fixed_pii_key(monkeypatch):
    # A fixed key keeps tests independent of key files; the key-file path has its own test.
    monkeypatch.setenv("AU_PII_KEY", "test-only-key")


def write_leads(drop: Path, workflow: str, batch: str, header: list[str], rows: list[list[str]]) -> None:
    path = drop / "leads" / workflow / f"{batch}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_events(drop: Path, name: str, events: list[dict | str]) -> None:
    path = drop / "events" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ev(event_id: str, event_type: str, call_id: str, at: str, phone: str | None = "9876543210",
       workflow: str = "motor_renewal", **payload) -> dict:
    e = {"event_id": event_id, "event_type": event_type, "workflow_id": workflow,
         "call_id": call_id, "occurred_at": at}
    if phone is not None:
        e["phone"] = phone
    e["payload"] = payload
    return e


class Pipeline:
    """Runs the real CLI against one database file."""

    def __init__(self, tmp: Path, name: str = "ledger.db"):
        self.db = tmp / name
        self.tmp = tmp

    def ingest(self, *drops: Path) -> None:
        assert main(["--db", str(self.db), "--schemas", str(SCHEMAS), "ingest",
                     *map(str, drops)]) == 0

    def report(self, out: str = "reports") -> dict[str, str]:
        out_dir = self.tmp / out
        assert main(["--db", str(self.db), "--schemas", str(SCHEMAS), "report",
                     "--out", str(out_dir)]) == 0
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(out_dir.iterdir())}

    def lead_report(self) -> list[dict]:
        return list(csv.DictReader(io.StringIO(self.report()["lead_report.csv"])))

    def quality(self) -> dict:
        return json.loads(self.report()["data_quality.json"])


@pytest.fixture
def pipeline(tmp_path) -> Pipeline:
    return Pipeline(tmp_path)


def generate(seed: int, out: Path, scale: str = "small") -> Path:
    subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", scale,
                    "--out", str(out)], check=True, capture_output=True)
    return out
