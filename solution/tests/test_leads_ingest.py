"""Lead ingestion through the real pipeline: rows, rejections, duplicates and merging."""
import csv
import io
import json
import sqlite3

from solution.pii import PiiCodec
from solution.tests.conftest import MOTOR_HEADER, write_leads

CODEC = PiiCodec(b"test-only-key")


def lead_state(db) -> dict[str, dict]:
    """policy_number -> merged fields (decrypting PII) plus batch_id."""
    conn = sqlite3.connect(db, isolation_level=None)
    out = {}
    for ref, batch, attrs, pii in conn.execute(
            "SELECT external_ref, batch_id, attributes_json, pii_enc FROM leads"):
        out[ref] = {"batch_id": batch, **json.loads(attrs), **CODEC.decrypt(pii)}
    conn.close()
    return out


def rejections(reports) -> list[list[str]]:
    return list(csv.reader(io.StringIO(reports["rejections.csv"])))[1:]


def test_blank_rows_skipped_and_row_numbers_count_data_rows(tmp_path, pipeline):
    drop = tmp_path / "drop"
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "", "", ""],
        ["", "", "", "", "", ""],                 # blank
        [" ", "  ", "", "", "", " "],            # whitespace-only = blank
        ["B", "N/A", "P2", "", "", ""],           # row 2 -> rejected
        ["", "9876543211", "P3", "", "", ""],     # row 3 -> rejected
    ])
    pipeline.ingest(drop)
    reports = pipeline.report()
    assert rejections(reports) == [
        ["motor_renewal", "batch_001", "2", "invalid_phone"],
        ["motor_renewal", "batch_001", "3", "missing_required:customer_name"],
    ]
    q = json.loads(reports["data_quality.json"])
    assert q["lead_rows_total"] == 3
    assert q["lead_rows_rejected"] == 2
    assert q["rejections_by_reason"] == {"invalid_phone": 1, "missing_required:customer_name": 1}


def test_duplicate_merge_keeps_first_batch_and_overwrites_non_null(tmp_path, pipeline):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    write_leads(d1, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["Asha Rao", "9876543210", "P1", "2026-11-01", "1000", ""],
        ["Asha R.", "+91 98765 43210", "P1", "", "₹1,200", "N/A"],   # same-file duplicate
    ])
    write_leads(d2, "motor_renewal", "batch_003",
                ["Language", "Insured Name", "Mobile No", "Policy No", "Expiry Date"], [
        ["TA", "", "09876543210", "P1", "05-Dec-2026"],  # missing name -> rejected, no effect
        ["TA", "Asha Final", "09876543210", "P1", "TBD"],
    ])
    pipeline.ingest(d1, d2)
    state = lead_state(pipeline.db)
    assert list(state) == ["P1"]
    lead = state["P1"]
    assert lead["batch_id"] == "batch_001"            # from the first valid row, permanently
    assert lead["customer_name"] == "Asha Final"      # last non-null write wins
    assert lead["phone"] == "+919876543210"
    assert lead["policy_expiry"] == "2026-11-01"      # later blank / invalid never overwrite
    assert lead["premium_amount"] == "1200.00"
    assert lead["preferred_language"] == "ta"

    q = pipeline.quality()
    assert q["lead_rows_total"] == 4
    assert q["lead_rows_rejected"] == 1
    assert q["duplicate_lead_rows_merged"] == 2
    # 'N/A' language on row 2 and 'TBD' date on the last row: warnings count on merged rows too.
    assert q["field_warnings"] == 2


def test_default_applied_after_merge(tmp_path, pipeline):
    drop = tmp_path / "d"
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "", "", ""],        # language null
        ["A", "9876543210", "P1", "", "", "mr"],      # later explicit value
        ["B", "9876543211", "P2", "", "", ""],        # never set
        ["C", "9876543212", "P3", "", "", "kn"],
        ["C", "9876543212", "P3", "", "", ""],        # later null does not reset to default
    ])
    pipeline.ingest(drop)
    state = lead_state(pipeline.db)
    assert state["P1"]["preferred_language"] == "mr"
    assert state["P2"]["preferred_language"] == "en"
    assert state["P3"]["preferred_language"] == "kn"


def test_rejected_row_never_touches_existing_lead(tmp_path, pipeline):
    drop = tmp_path / "d"
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "", "500", ""],
        ["Z", "9876543210", "", "", "999", ""],       # same phone, but rejected
    ])
    pipeline.ingest(drop)
    assert lead_state(pipeline.db)["P1"]["premium_amount"] == "500.00"
    assert pipeline.quality()["duplicate_lead_rows_merged"] == 0


def test_same_phone_in_different_workflows_is_two_leads(tmp_path, pipeline):
    drop = tmp_path / "d"
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", "9876543210", "P1", "", "", ""]])
    write_leads(drop, "policy_feedback", "batch_001", ["customer_name", "phone", "order_id"],
                [["A", "9876543210", "O1"]])
    pipeline.ingest(drop)
    rows = pipeline.lead_report()
    assert [(r["workflow_id"], r["external_ref"]) for r in rows] == [
        ("motor_renewal", "P1"), ("policy_feedback", "O1")]


def test_batch_with_only_rejected_rows_has_no_summary_row(tmp_path, pipeline):
    drop = tmp_path / "d"
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [["A", "9876543210", "P1", "", "", ""]])
    write_leads(drop, "motor_renewal", "batch_002", MOTOR_HEADER, [["A", "123", "P2", "", "", ""]])
    pipeline.ingest(drop)
    reports = pipeline.report()
    assert reports["batch_summary.csv"].splitlines()[1:] == ["motor_renewal,batch_001,1,1,0,0,0"]
    assert rejections(reports) == [["motor_renewal", "batch_002", "1", "invalid_phone"]]


def test_utf8_bom_and_rupee_sign(tmp_path, pipeline):
    path = tmp_path / "d" / "leads" / "motor_renewal" / "batch_001.csv"
    path.parent.mkdir(parents=True)
    path.write_bytes("﻿customer_name,phone,policy_number,premium_amount\r\n"
                     "A,9876543210,P1,\"₹12,345.678\"\r\n".encode("utf-8"))
    pipeline.ingest(tmp_path / "d")
    assert lead_state(pipeline.db)["P1"]["premium_amount"] == "12345.68"


def test_rejections_sorted_numerically(tmp_path, pipeline):
    rows = [["A", "9876543210", f"P{i}", "", "", ""] for i in range(1, 12)]
    rows[1][1] = rows[10][1] = "bad"    # rows 2 and 11 rejected
    write_leads(tmp_path / "d", "motor_renewal", "batch_001", MOTOR_HEADER, rows)
    pipeline.ingest(tmp_path / "d")
    assert [r[2] for r in rejections(pipeline.report())] == ["2", "11"]
