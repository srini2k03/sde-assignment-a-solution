"""Whole-pipeline properties on generated data, plus the independent cross-check."""
import shutil
import sqlite3

import pytest

from solution.tests import reference_impl
from solution.tests.conftest import MOTOR_HEADER, SCHEMAS, Pipeline, ev, generate, write_events, write_leads

SEEDS = [1, 7, 42, 123]
TABLES = ["ingested_files", "leads", "lead_rows", "events", "event_rejects"]


@pytest.fixture(scope="module")
def datasets(tmp_path_factory):
    root = tmp_path_factory.mktemp("generated")
    return {seed: generate(seed, root / f"seed_{seed}") for seed in SEEDS}


def row_counts(db):
    conn = sqlite3.connect(db, isolation_level=None)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    conn.close()
    return counts


def test_reingesting_a_drop_changes_nothing(tmp_path, datasets):
    data = datasets[42]
    p = Pipeline(tmp_path)
    p.ingest(data / "drop_1")
    counts, reports = row_counts(p.db), p.report("r1")
    p.ingest(data / "drop_1")
    assert row_counts(p.db) == counts
    assert p.report("r2") == reports


def test_reingest_after_later_drop_changes_nothing(tmp_path, datasets):
    data = datasets[42]
    p = Pipeline(tmp_path)
    p.ingest(data / "drop_1", data / "drop_2")
    counts, reports = row_counts(p.db), p.report("r1")
    p.ingest(data / "drop_1")
    p.ingest(data / "drop_2", data / "drop_1")
    assert row_counts(p.db) == counts
    assert p.report("r2") == reports


def test_copied_drop_folder_is_recognised(tmp_path, datasets):
    # Idempotency is by (path inside the drop, content hash), not by absolute path.
    data = datasets[42]
    p = Pipeline(tmp_path)
    p.ingest(data / "drop_1")
    counts = row_counts(p.db)
    copy = tmp_path / "elsewhere" / "drop_1"
    shutil.copytree(data / "drop_1", copy)
    p.ingest(copy)
    assert row_counts(p.db) == counts


@pytest.mark.parametrize("seed", SEEDS)
def test_incremental_equals_all_at_once(tmp_path, datasets, seed):
    data = datasets[seed]
    step = Pipeline(tmp_path, "step.db")
    step.ingest(data / "drop_1")
    step.ingest(data / "drop_2")
    once = Pipeline(tmp_path, "once.db")
    once.ingest(data / "drop_1", data / "drop_2")
    assert step.report("step") == once.report("once")


@pytest.mark.parametrize("seed", SEEDS)
def test_matches_independent_reference_implementation(tmp_path, datasets, seed):
    data = datasets[seed]
    p = Pipeline(tmp_path)
    p.ingest(data / "drop_1")
    p.ingest(data / "drop_2")
    ours = p.report()
    expected = reference_impl.run([data / "drop_1", data / "drop_2"], SCHEMAS)
    assert sorted(ours) == sorted(expected)
    for name in expected:
        assert ours[name] == expected[name], f"{name} differs for seed {seed}"


def test_reference_also_matches_after_drop_1_only(tmp_path, datasets):
    data = datasets[1]
    p = Pipeline(tmp_path)
    p.ingest(data / "drop_1")
    assert p.report() == reference_impl.run([data / "drop_1"], SCHEMAS)


def test_orphan_links_once_its_lead_arrives(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", "9000000001", "P1", "", "", ""]])
    write_events(d1, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z", phone="+91 98765 43210"),
        ev("e2", "call.connected", "c1", "2026-10-01T10:00:05Z", phone="9876543210"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00Z", phone="9876543210",
           disposition_code="renewed"),
        ev("e4", "disposition.override", "c1", "2026-10-02T10:00:00Z", phone=None,
           disposition_code="already_renewed_elsewhere"),
    ])
    write_leads(d2, "motor_renewal", "batch_003", MOTOR_HEADER, [["B", "09876543210", "P2", "", "", ""]])

    p = Pipeline(tmp_path)
    p.ingest(d1)
    q = p.quality()
    assert q["orphan_events"] == 4
    assert [r["external_ref"] for r in p.lead_report()] == ["P1"]

    p.ingest(d2)
    q = p.quality()
    assert q["orphan_events"] == 0
    row = next(r for r in p.lead_report() if r["external_ref"] == "P2")
    assert (row["batch_id"], row["bucket"], row["total_calls"], row["connected_calls"],
            row["current_disposition"], row["ever_success"]) == \
        ("batch_003", "completed", "1", "1", "already_renewed_elsewhere", "false")
    assert p.report() == reference_impl.run([d1, d2], SCHEMAS)


def test_changed_file_with_same_path_is_ingested_as_new(tmp_path, capsys):
    d = tmp_path / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", "9876543210", "P1", "", "", ""]])
    p = Pipeline(tmp_path)
    p.ingest(d)
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", "9876543211", "P2", "", "", ""]])
    p.ingest(d)
    assert "different content" in capsys.readouterr().err
    assert [r["external_ref"] for r in p.lead_report()] == ["P1", "P2"]


def test_output_files_follow_the_contract(tmp_path, datasets):
    p = Pipeline(tmp_path)
    p.ingest(datasets[42] / "drop_1", datasets[42] / "drop_2")
    reports = p.report()
    assert set(reports) == {"batch_summary.csv", "daily_overview.csv", "lead_report.csv",
                            "rejections.csv", "data_quality.json"}
    assert list(__import__("json").loads(reports["data_quality.json"])) == [
        "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
        "duplicate_lead_rows_merged", "field_warnings", "events_total", "malformed_events",
        "duplicate_events_dropped", "orphan_events", "unknown_disposition_events"]
    for name, text in reports.items():
        assert "\r" not in text and text.endswith("\n"), name
    raw = (tmp_path / "reports" / "lead_report.csv").read_bytes()
    assert b"\r\n" not in raw


def test_reference_matches_on_hand_written_edge_cases(tmp_path):
    """Cases the generator never produces: ties, repeated call.initiated, odd linking."""
    d = tmp_path / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "31/02/2026", "Rs 10.005", "EN"],
        ["B", "9876543211", "P1", "", "", ""],            # same external_ref, other phone
        ["C", "9876543212", "P3", "", "", ""],
    ])
    write_events(d, "events_0001.jsonl", [
        ev("e2", "call.initiated", "c1", "2026-10-01T20:00:00Z"),
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),           # earlier copy wins
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="callback_requested"),
        ev("e4", "disposition.set", "c1", "2026-10-01T15:35:00+05:30", disposition_code="renewed"),  # tie
        ev("f1", "call.initiated", "c2", "2026-10-01T10:00:00Z", phone="9876543211"),
        ev("f0", "call.initiated", "c3", "2026-10-01T10:00:00Z", phone="9876543211"),  # same time
        ev("f2", "disposition.set", "c3", "2026-10-01T10:01:00Z", phone="9876543211", disposition_code="dnd"),
        ev("f3", "disposition.set", "c2", "2026-10-01T10:01:00Z", phone="9876543211",
           disposition_code="payment_link_sent"),
        ev("g1", "disposition.override", "c3", "2026-10-01T10:02:00Z", phone=None, disposition_code="bogus"),
        ev("h1", "call.initiated", "c4", "not a time", phone="9876543212"),
        ev("h2", "call.connected", "c4", "2026-10-01T10:00:00Z", phone="9876543212"),
        {"event_id": "h3", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "c5", "occurred_at": "2026-10-01T10:00:00Z", "phone": 9876543212, "payload": {}},
        {"event_id": "h4", "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "occurred_at": "2026-10-01T11:00:00Z", "phone": "9876543212", "payload": {}},  # no call_id
    ])
    p = Pipeline(tmp_path)
    p.ingest(d)
    assert p.report() == reference_impl.run([d], SCHEMAS)
