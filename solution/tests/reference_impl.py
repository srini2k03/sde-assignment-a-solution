"""Deliberately simple reference implementation of the whole spec, used only by tests.

It reads the raw drop folders and computes the five outputs in memory: no
database, no hashing, no encryption, and none of the pipeline's modules
(every parser is re-implemented here on purpose). If the SQL pipeline and this
file agree byte-for-byte on several random seeds, a bug would have to be
written twice, in two different styles, to slip through.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


# ------------------------------------------------------------------ parsers
def phone(raw):
    d = "".join(ch for ch in raw if ch in "0123456789")
    if len(d) == 12 and d[:2] == "91":
        d = d[2:]
    if len(d) == 11 and d[0] == "0":
        d = d[1:]
    return "+91" + d if len(d) == 10 and d[0] in "6789" else None


def a_date(raw):
    s = raw.strip()
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
            y, m, d = s.split("-")
        elif re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", s):
            d, m, y = s.split("/")
        elif re.fullmatch(r"[0-9]{2}-[0-9]{2}-[0-9]{4}", s):
            d, m, y = s.split("-")
        elif re.fullmatch(r"[0-9]{2}-[A-Za-z]{3}-[0-9]{4}", s):
            d, mon, y = s.split("-")
            if mon.lower() not in MONTHS:
                return None
            m = MONTHS.index(mon.lower()) + 1
        else:
            return None
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def money(raw):
    s = raw.strip()
    low = s.lower()
    for prefix in ("₹", "rs.", "rs", "inr"):
        if low.startswith(prefix):
            s = s[len(prefix):].lstrip()
            break
    s = s.replace(",", "")
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", s):
        return None
    return str(Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def when(raw):
    """-> aware IST datetime, or None."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def header_key(h):
    return re.sub(r"[\s_]+", " ", h.lower().strip())


# ------------------------------------------------------------------- runner
def run(drops, schemas_dir):
    schemas_dir = Path(schemas_dir)
    schemas = {}
    for p in sorted(schemas_dir.glob("*.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        schemas[s["workflow_id"]] = s
    catalogue = {}
    with open(schemas_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            catalogue[(r["workflow_id"], r["code"])] = (r["is_terminal"] == "true",
                                                       r["is_success"] == "true")

    leads = {}          # (wf, phone) -> {"batch":..., "values": {...}}
    rejections = []     # (wf, batch, row, reason)
    seen_files = set()
    q = dict(lead_rows_total=0, lead_rows_rejected=0, duplicate_lead_rows_merged=0,
             field_warnings=0, events_total=0, malformed_events=0, duplicate_events_dropped=0)
    events = {}         # event_id -> event dict (first copy)

    for drop in map(Path, drops):
        lead_files = []
        if (drop / "leads").is_dir():
            for wf_dir in sorted((drop / "leads").iterdir()):
                for f in sorted(wf_dir.glob("*.csv")):
                    lead_files.append((wf_dir.name, f))
        for wf, f in lead_files:
            data = f.read_bytes()
            key = (f.relative_to(drop).as_posix(), data)
            if key in seen_files:
                continue
            seen_files.add(key)
            schema = schemas[wf]
            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
            if not rows:
                continue
            col_of = {}
            for i, h in enumerate(rows[0]):
                for fld in schema["fields"]:
                    names = [header_key(fld["name"])] + [header_key(a) for a in fld.get("aliases", [])]
                    if header_key(h) in names:
                        col_of.setdefault(fld["name"], i)
                        break
            n = 0
            for cells in rows[1:]:
                if all(c.strip() == "" for c in cells):
                    continue
                n += 1
                q["lead_rows_total"] += 1
                vals, reason, warns = {}, None, 0
                for fld in schema["fields"]:
                    i = col_of.get(fld["name"])
                    raw = cells[i].strip() if i is not None and i < len(cells) else ""
                    if raw == "":
                        vals[fld["name"]] = None
                        if fld.get("required") and not reason:
                            reason = "missing_required:" + fld["name"]
                        continue
                    t = fld["type"]
                    if t == "phone":
                        v = phone(raw)
                    elif t == "date":
                        v = a_date(raw)
                    elif t == "decimal":
                        v = money(raw)
                    elif t == "enum":
                        v = raw.lower() if raw.lower() in [x.lower() for x in fld["values"]] else None
                    else:
                        v = raw
                    vals[fld["name"]] = v
                    if v is None:
                        if fld.get("required"):
                            if not reason:
                                reason = "invalid_phone" if t == "phone" else "invalid_" + fld["name"]
                        else:
                            warns += 1
                if reason:
                    q["lead_rows_rejected"] += 1
                    rejections.append((wf, f.stem, n, reason))
                    continue
                q["field_warnings"] += warns
                ident = (wf, vals["phone"])
                if ident in leads:
                    q["duplicate_lead_rows_merged"] += 1
                    for k, v in vals.items():
                        if v is not None:
                            leads[ident]["values"][k] = v
                else:
                    leads[ident] = {"batch": f.stem, "values": dict(vals)}

        ev_files = sorted((drop / "events").glob("*.jsonl")) if (drop / "events").is_dir() else []
        for f in ev_files:
            data = f.read_bytes()
            key = (f.relative_to(drop).as_posix(), data)
            if key in seen_files:
                continue
            seen_files.add(key)
            for raw in data.split(b"\n"):
                if raw.strip() == b"":
                    continue
                q["events_total"] += 1
                try:
                    e = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    e = None
                if not isinstance(e, dict) or not isinstance(e.get("event_id"), str) or e["event_id"] == "":
                    q["malformed_events"] += 1
                    continue
                if e["event_id"] in events:
                    q["duplicate_events_dropped"] += 1
                    continue
                events[e["event_id"]] = e

    # ---- link events (final state) --------------------------------------
    def s(x):
        return x if isinstance(x, str) and x != "" else None

    def phone_of(e):
        p = e.get("phone")
        if isinstance(p, int) and not isinstance(p, bool):
            p = str(p)
        return phone(p) if isinstance(p, str) else None

    linked = {}  # event_id -> lead identity
    for eid, e in events.items():
        if e.get("event_type") != "disposition.override":
            ident = (s(e.get("workflow_id")), phone_of(e))
            if ident in leads:
                linked[eid] = ident
    owner = {}
    for eid, ident in linked.items():
        cid = s(events[eid].get("call_id"))
        if cid is not None:
            owner.setdefault(cid, []).append(ident)
    lead_order = {ident: i for i, ident in enumerate(leads)}  # creation order
    owner = {cid: min(ids, key=lead_order.get) for cid, ids in owner.items()}
    for eid, e in events.items():
        if e.get("event_type") == "disposition.override":
            cid = s(e.get("call_id"))
            if cid in owner:
                linked[eid] = owner[cid]

    unknown = 0
    usable = []
    for eid, ident in linked.items():
        e = events[eid]
        et = e.get("event_type")
        code = e["payload"].get("disposition_code") if isinstance(e.get("payload"), dict) else None
        if isinstance(et, str) and et.startswith("disposition."):
            if not isinstance(code, str) or (ident[0], code) not in catalogue:
                unknown += 1
                continue
        usable.append((ident, e, code))

    # ---- calls --------------------------------------------------------------
    calls = {}  # (ident, call_id) -> dict
    for ident, e, code in usable:
        cid = s(e.get("call_id"))
        if cid is None:
            continue
        c = calls.setdefault((ident, cid), {"init": None, "connected": False, "failed": False,
                                            "set": None, "override": None})
        et = e.get("event_type")
        t = when(e.get("occurred_at"))
        if et == "call.connected":
            c["connected"] = True
        elif et == "call.telephony_failed":
            c["failed"] = True
        elif et == "call.initiated" and t is not None:
            if c["init"] is None or (t, e["event_id"]) < c["init"]:
                c["init"] = (t, e["event_id"])
        elif et in ("disposition.set", "disposition.override") and t is not None:
            slot = "set" if et == "disposition.set" else "override"
            if c[slot] is None or (t, e["event_id"]) > c[slot][:2]:
                c[slot] = (t, e["event_id"], code)

    per_lead = {ident: [] for ident in leads}
    for (ident, cid), c in calls.items():
        if c["init"] is None:
            continue
        best = c["override"] or c["set"]
        code = best[2] if best else None
        term, succ = catalogue.get((ident[0], code), (False, False))
        per_lead[ident].append({"t": c["init"][0], "eid": c["init"][1], "connected": c["connected"],
                                "failed": c["failed"], "code": code, "terminal": term,
                                "success": succ})

    # ---- outputs ------------------------------------------------------------
    def out_csv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue()

    lead_rows, batch, daily = [], {}, {}
    for ident, lead in leads.items():
        wf, ph = ident
        cl = per_lead[ident]
        if not cl:
            bucket = "yet_to_reach_out"
        elif any(c["terminal"] for c in cl):
            bucket = "completed"
        elif all(c["failed"] for c in cl):
            bucket = "in_progress_telephony_failed"
        else:
            bucket = "in_progress_dialed"
        by_time = sorted(cl, key=lambda c: (c["t"], c["eid"]))
        last = by_time[-1]["t"].strftime("%Y-%m-%dT%H:%M:%S+05:30") if by_time else ""
        with_disp = [c for c in by_time if c["code"]]
        current = with_disp[-1]["code"] if with_disp else ""
        ref_field = next(f["name"] for f in schemas[wf]["fields"] if f.get("reference"))
        lead_rows.append([wf, lead["values"][ref_field], lead["batch"], "+91******" + ph[-4:],
                          bucket, len(cl), sum(c["connected"] for c in cl), last, current,
                          "true" if any(c["success"] for c in cl) else "false"])
        b = batch.setdefault((wf, lead["batch"]), {"yet_to_reach_out": 0, "in_progress_telephony_failed": 0,
                                                    "in_progress_dialed": 0, "completed": 0})
        b[bucket] += 1
        for c in cl:
            d = daily.setdefault((wf, c["t"].date().isoformat()), {"n": 0, "conn": 0, "succ": set()})
            d["n"] += 1
            d["conn"] += c["connected"]
            if c["success"]:
                d["succ"].add(ph)

    lead_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    out = {}
    out["batch_summary.csv"] = out_csv(
        ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out", "in_progress_telephony_failed",
         "in_progress_dialed", "completed"],
        [[wf, b, sum(v.values()), v["yet_to_reach_out"], v["in_progress_telephony_failed"],
          v["in_progress_dialed"], v["completed"]] for (wf, b), v in sorted(batch.items())])
    out["daily_overview.csv"] = out_csv(
        ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
        [[wf, d, v["n"], v["conn"],
          str((Decimal(v["conn"]) / Decimal(v["n"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
          len(v["succ"])] for (wf, d), v in sorted(daily.items())])
    out["lead_report.csv"] = out_csv(
        ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
         "connected_calls", "last_call_at", "current_disposition", "ever_success"], lead_rows)
    out["rejections.csv"] = out_csv(["workflow_id", "batch_id", "row_number", "reason"],
                                    sorted(rejections, key=lambda r: (r[0], r[1], r[2])))
    reasons = {}
    for r in rejections:
        reasons[r[3]] = reasons.get(r[3], 0) + 1
    quality = {
        "lead_rows_total": q["lead_rows_total"],
        "lead_rows_rejected": q["lead_rows_rejected"],
        "rejections_by_reason": dict(sorted(reasons.items())),
        "duplicate_lead_rows_merged": q["duplicate_lead_rows_merged"],
        "field_warnings": q["field_warnings"],
        "events_total": q["events_total"],
        "malformed_events": q["malformed_events"],
        "duplicate_events_dropped": q["duplicate_events_dropped"],
        "orphan_events": len(events) - len(linked),
        "unknown_disposition_events": unknown,
    }
    out["data_quality.json"] = json.dumps(quality, indent=2) + "\n"
    return out
