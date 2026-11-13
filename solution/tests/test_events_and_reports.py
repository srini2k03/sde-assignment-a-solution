"""Events, linking and derived facts, checked through the report files."""
import csv
import io

from solution.tests.conftest import MOTOR_HEADER, ev, write_events, write_leads

PHONE = "9876543210"


def one_lead(drop, phone=PHONE, ref="P1"):
    write_leads(drop, "motor_renewal", "batch_001", MOTOR_HEADER, [["Asha", phone, ref, "", "", ""]])


def lead_row(pipeline, ref="P1"):
    return next(r for r in pipeline.lead_report() if r["external_ref"] == ref)


def daily(pipeline):
    return list(csv.DictReader(io.StringIO(pipeline.report()["daily_overview.csv"])))


# ------------------------------------------------------------------ effective disposition
def test_override_beats_later_set(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00+05:30"),
        ev("e2", "call.connected", "c1", "2026-10-01T10:00:10+05:30"),
        ev("e3", "disposition.override", "c1", "2026-10-01T09:00:00+05:30", phone=None,
           disposition_code="renewed", overridden_by="sup", reason="qa"),
        ev("e4", "disposition.set", "c1", "2026-10-01T11:00:00+05:30", disposition_code="not_interested"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert row["current_disposition"] == "renewed"
    assert row["ever_success"] == "true"
    assert row["bucket"] == "completed"


def test_latest_set_wins_and_unknown_codes_ignored(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="callback_requested"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:06:00Z", disposition_code="payment_link_sent"),
        ev("e4", "disposition.set", "c1", "2026-10-01T10:07:00Z", disposition_code="RENEWD"),
        ev("e5", "disposition.override", "c1", "2026-10-01T10:08:00Z", phone=None,
           disposition_code="Renewed"),        # case-sensitive: unknown
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert row["current_disposition"] == "payment_link_sent"
    assert row["bucket"] == "in_progress_dialed"
    assert pipeline.quality()["unknown_disposition_events"] == 2


def test_latest_override_wins_with_event_id_tiebreak(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="renewed"),
        ev("o_b", "disposition.override", "c1", "2026-10-02T10:00:00Z", phone=None,
           disposition_code="callback_requested"),
        ev("o_a", "disposition.override", "c1", "2026-10-02T15:30:00+05:30", phone=None,
           disposition_code="language_barrier"),   # same instant as o_b; o_b is greater
    ])
    pipeline.ingest(d)
    assert lead_row(pipeline)["current_disposition"] == "callback_requested"


def test_current_disposition_is_from_most_recent_call_that_has_one(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="callback_requested"),
        ev("e3", "call.initiated", "c2", "2026-10-02T10:00:00Z"),   # later call, no disposition
        ev("e4", "call.ended", "c2", "2026-10-02T10:00:30Z", duration_sec=0, end_reason="no_answer"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert row["current_disposition"] == "callback_requested"
    assert row["last_call_at"] == "2026-10-02T15:30:00+05:30"
    assert row["total_calls"] == "2"


# ------------------------------------------------------------------------- buckets
def test_bucket_yet_to_reach_out(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        # events of a call without call.initiated do not make a call
        ev("e1", "call.connected", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="renewed"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert (row["bucket"], row["total_calls"], row["last_call_at"], row["ever_success"]) == \
        ("yet_to_reach_out", "0", "", "false")


def test_bucket_telephony_failed_only_if_every_call_failed(tmp_path, pipeline):
    d = tmp_path / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "", "", ""], ["B", "9876543211", "P2", "", "", ""]])
    write_events(d, "events_0001.jsonl", [
        ev("a1", "call.initiated", "ca1", "2026-10-01T10:00:00Z", phone="9876543210"),
        ev("a2", "call.telephony_failed", "ca1", "2026-10-01T10:00:05Z", phone="9876543210", reason="sip_503"),
        ev("a3", "call.initiated", "ca2", "2026-10-02T10:00:00Z", phone="9876543210"),
        ev("a4", "call.telephony_failed", "ca2", "2026-10-02T10:00:05Z", phone="9876543210", reason="sip_503"),
        ev("b1", "call.initiated", "cb1", "2026-10-01T10:00:00Z", phone="9876543211"),
        ev("b2", "call.telephony_failed", "cb1", "2026-10-01T10:00:05Z", phone="9876543211", reason="x"),
        ev("b3", "call.initiated", "cb2", "2026-10-02T10:00:00Z", phone="9876543211"),
        ev("b4", "call.ended", "cb2", "2026-10-02T10:00:40Z", phone="9876543211", duration_sec=0, end_reason="busy"),
    ])
    pipeline.ingest(d)
    assert lead_row(pipeline, "P1")["bucket"] == "in_progress_telephony_failed"
    assert lead_row(pipeline, "P2")["bucket"] == "in_progress_dialed"


def test_completed_beats_telephony_failed(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "call.telephony_failed", "c1", "2026-10-01T10:00:01Z", reason="x"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="wrong_number"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert row["bucket"] == "completed"
    assert row["ever_success"] == "false"


def test_non_terminal_disposition_stays_in_progress(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),
        ev("e2", "call.connected", "c1", "2026-10-01T10:00:09Z"),
        ev("e3", "disposition.set", "c1", "2026-10-01T10:05:00Z", disposition_code="callback_requested"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert (row["bucket"], row["connected_calls"]) == ("in_progress_dialed", "1")


# ------------------------------------------------------------------- linking & quality
def test_orphans_duplicates_and_malformed(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z", phone="+91 98765 43210"),
        ev("e1", "call.initiated", "c1", "2026-10-01T11:00:00Z"),                  # duplicate id
        ev("e2", "call.initiated", "c9", "2026-10-01T10:00:00Z", phone="9999999999"),  # unknown number
        ev("e3", "call.initiated", "c8", "2026-10-01T10:00:00Z", phone="N/A"),     # invalid phone
        ev("e4", "call.initiated", "c7", "2026-10-01T10:00:00Z", workflow="policy_feedback"),
        ev("e5", "disposition.override", "c_unknown", "2026-10-01T10:00:00Z", phone=None,
           disposition_code="renewed"),                                            # call has no linked events
        ev("e6", "disposition.set", "c9", "2026-10-01T10:05:00Z", phone="9999999999",
           disposition_code="RENEWD"),                                             # orphan, not unknown
        '{"event_id": "e7", "event_type": "call.init',                              # truncated
        "[1, 2, 3]",                                                                # not an object
        '{"event_type": "call.initiated"}',                                         # no event_id
        '{"event_id": "", "event_type": "call.initiated"}',                         # empty event_id
        "",                                                                         # empty: not counted
    ])
    pipeline.ingest(d)
    q = pipeline.quality()
    assert q["events_total"] == 11
    assert q["malformed_events"] == 4
    assert q["duplicate_events_dropped"] == 1
    assert q["orphan_events"] == 5
    assert q["unknown_disposition_events"] == 0
    row = lead_row(pipeline)
    assert row["last_call_at"] == "2026-10-01T15:30:00+05:30"   # first copy of e1 wins


def test_multiple_initiated_for_one_call_use_earliest(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e2", "call.initiated", "c1", "2026-10-01T20:00:00Z"),    # 2 Oct IST
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z"),    # 1 Oct IST, earliest
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert (row["total_calls"], row["last_call_at"]) == ("1", "2026-10-01T15:30:00+05:30")
    assert [r["date"] for r in daily(pipeline)] == ["2026-10-01"]


def test_daily_overview_uses_ist_dates_and_rates(tmp_path, pipeline):
    d = tmp_path / "d"
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, [
        ["A", "9876543210", "P1", "", "", ""], ["B", "9876543211", "P2", "", "", ""],
        ["C", "9876543212", "P3", "", "", ""]])
    events = [
        # 2026-10-01 20:00Z = 2026-10-02 01:30 IST
        ev("a1", "call.initiated", "ca1", "2026-10-01T20:00:00Z", phone="9876543210"),
        ev("a2", "call.connected", "ca1", "2026-10-01T20:00:10Z", phone="9876543210"),
        ev("a3", "disposition.set", "ca1", "2026-10-01T20:05:00Z", phone="9876543210", disposition_code="renewed"),
        ev("b1", "call.initiated", "cb1", "2026-10-02 09:00:00", phone="9876543211"),   # naive = IST
        ev("c1", "call.initiated", "cc1", "2026-10-02T09:00:00+05:30", phone="9876543212"),
        ev("c2", "call.initiated", "cc2", "2026-10-02T10:00:00+05:30", phone="9876543212"),
        ev("c3", "call.connected", "cc2", "2026-10-02T10:00:05+05:30", phone="9876543212"),
        ev("c4", "call.initiated", "cc3", "2026-10-01T12:00:00+05:30", phone="9876543212"),
    ]
    write_events(d, "events_0001.jsonl", events)
    pipeline.ingest(d)
    assert daily(pipeline) == [
        {"workflow_id": "motor_renewal", "date": "2026-10-01", "total_calls": "1",
         "connected_calls": "0", "connectivity_rate": "0.0000", "success_leads": "0"},
        {"workflow_id": "motor_renewal", "date": "2026-10-02", "total_calls": "4",
         "connected_calls": "2", "connectivity_rate": "0.5000", "success_leads": "1"},
    ]


def test_connectivity_rate_rounds_half_up(tmp_path, pipeline):
    # 1/3 -> 0.3333, 2/3 -> 0.6667; 1/16 = 0.0625 exactly; 1/32 = 0.03125 -> 0.0313 (half up)
    d = tmp_path / "d"
    rows, events = [], []
    for i in range(32):
        phone = f"98765432{i:02d}"
        rows.append([f"N{i}", phone, f"P{i}", "", "", ""])
        events.append(ev(f"i{i:02d}", "call.initiated", f"c{i}", "2026-10-01T10:00:00Z", phone=phone))
    events.append(ev("conn", "call.connected", "c0", "2026-10-01T10:00:05Z", phone="9876543200"))
    write_leads(d, "motor_renewal", "batch_001", MOTOR_HEADER, rows)
    write_events(d, "events_0001.jsonl", events)
    pipeline.ingest(d)
    assert daily(pipeline)[0]["connectivity_rate"] == "0.0313"


def test_override_links_through_call_to_lead(tmp_path, pipeline):
    d = tmp_path / "d"
    one_lead(d)
    write_events(d, "events_0001.jsonl", [
        ev("e1", "call.initiated", "c1", "2026-10-01T10:00:00Z", phone="09876543210"),
        ev("e2", "disposition.set", "c1", "2026-10-01T10:05:00Z", phone="91-98765-43210",
           disposition_code="callback_requested"),
        ev("e3", "disposition.override", "c1", "2026-10-03T10:00:00Z", phone=None,
           disposition_code="not_interested", overridden_by="supervisor_1", reason="qa_review"),
    ])
    pipeline.ingest(d)
    row = lead_row(pipeline)
    assert (row["bucket"], row["current_disposition"]) == ("completed", "not_interested")
    assert pipeline.quality()["orphan_events"] == 0
