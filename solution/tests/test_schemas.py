import pytest

from solution.schemas import SchemaError, load_catalogue, normalize_header
from solution.tests.conftest import SCHEMAS

CAT = load_catalogue(SCHEMAS)
MOTOR = CAT.workflows["motor_renewal"]
FEEDBACK = CAT.workflows["policy_feedback"]


@pytest.mark.parametrize("raw,expected", [
    ("Insured Name", "insured name"),
    ("  Mobile   No ", "mobile no"),
    ("policy_number", "policy number"),
    ("Policy__No", "policy no"),
    ("Expiry_ Date", "expiry date"),
    ("ORDER\tID", "order id"),
])
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


def test_aliases_and_reordered_columns_map_to_fields():
    header = ["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium",
              "agent_notes"]
    assert MOTOR.map_columns(header) == {
        "preferred_language": 0, "customer_name": 1, "policy_number": 2, "phone": 3,
        "policy_expiry": 4, "premium_amount": 5,
    }  # agent_notes is ignored


def test_feedback_aliases():
    header = ["Order ID", "Name", "Contact", "Product", "Order Date", "Language", "agent_notes"]
    cols = FEEDBACK.map_columns(header)
    assert cols == {"order_id": 0, "customer_name": 1, "phone": 2, "product": 3,
                    "order_date": 4, "preferred_language": 5}


def test_first_matching_column_wins():
    cols = MOTOR.map_columns(["phone", "Mobile", "customer_name"])
    assert cols["phone"] == 0


def _row(**over):
    base = {"customer_name": "Asha Rao", "phone": "9876543210", "policy_number": "MOT/1",
            "policy_expiry": "2026-11-01", "premium_amount": "₹1,000", "preferred_language": "hi"}
    base.update(over)
    header = list(base)
    return MOTOR.parse_row(MOTOR.map_columns(header), list(base.values()))


def test_valid_row_is_normalised():
    r = _row()
    assert r.reject_reason is None and r.field_warnings == 0
    assert r.values == {"customer_name": "Asha Rao", "phone": "+919876543210",
                        "policy_number": "MOT/1", "policy_expiry": "2026-11-01",
                        "premium_amount": "1000.00", "preferred_language": "hi"}


def test_rejection_reason_follows_schema_order():
    # customer_name comes before phone in the schema, so it wins.
    assert _row(customer_name="  ", phone="N/A").reject_reason == "missing_required:customer_name"
    assert _row(phone="N/A", policy_number="").reject_reason == "invalid_phone"
    assert _row(phone="", policy_number="").reject_reason == "missing_required:phone"
    assert _row(policy_number="").reject_reason == "missing_required:policy_number"


def test_missing_column_counts_as_missing_value():
    r = MOTOR.parse_row(MOTOR.map_columns(["customer_name", "phone"]), ["Asha", "9876543210"])
    assert r.reject_reason == "missing_required:policy_number"


def test_short_row_counts_as_missing_value():
    header = ["customer_name", "phone", "policy_number"]
    r = MOTOR.parse_row(MOTOR.map_columns(header), ["Asha", "9876543210"])
    assert r.reject_reason == "missing_required:policy_number"


def test_optional_invalid_fields_become_null_with_one_warning_each():
    r = _row(policy_expiry="31/02/2026", premium_amount="N/A", preferred_language="Hindi")
    assert r.reject_reason is None
    assert r.field_warnings == 3
    assert r.values["policy_expiry"] is None
    assert r.values["premium_amount"] is None
    assert r.values["preferred_language"] is None


def test_optional_missing_is_not_a_warning():
    r = _row(policy_expiry="", premium_amount=" ", preferred_language="")
    assert r.field_warnings == 0


def test_rejected_rows_report_no_warnings():
    r = _row(policy_expiry="TBD", phone="12345")
    assert r.reject_reason == "invalid_phone"
    assert r.field_warnings == 0


def test_defaults():
    assert MOTOR.apply_defaults({"preferred_language": None})["preferred_language"] == "en"
    assert MOTOR.apply_defaults({"preferred_language": "ta"})["preferred_language"] == "ta"


def test_required_non_phone_invalid_gets_invalid_field_reason(tmp_path):
    schema = tmp_path / "s"
    schema.mkdir()
    (schema / "dispositions.csv").write_text("workflow_id,code,label,is_terminal,is_success\n")
    (schema / "wf.json").write_text("""{"workflow_id": "wf", "fields": [
        {"name": "phone", "type": "phone", "required": true},
        {"name": "ref", "type": "string", "required": true, "reference": true},
        {"name": "due", "type": "date", "required": true}]}""")
    wf = load_catalogue(schema).workflows["wf"]
    r = wf.parse_row({"phone": 0, "ref": 1, "due": 2}, ["9876543210", "R1", "soon"])
    assert r.reject_reason == "invalid_due"


def test_schema_without_reference_field_is_refused(tmp_path):
    (tmp_path / "dispositions.csv").write_text("workflow_id,code,label,is_terminal,is_success\n")
    (tmp_path / "wf.json").write_text(
        '{"workflow_id": "wf", "fields": [{"name": "phone", "type": "phone", "required": true}]}')
    with pytest.raises(SchemaError):
        load_catalogue(tmp_path)


def test_catalogue_flags():
    d = {(x.workflow_id, x.code): x for x in CAT.dispositions}
    assert d[("motor_renewal", "renewed")].is_terminal and d[("motor_renewal", "renewed")].is_success
    assert not d[("motor_renewal", "callback_requested")].is_terminal
    assert d[("policy_feedback", "refused")].is_terminal and not d[("policy_feedback", "refused")].is_success
