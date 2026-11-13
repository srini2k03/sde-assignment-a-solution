"""Workflow lead schemas and the disposition catalogue.

Loads ``<schemas_dir>/*.json`` (one lead schema per workflow) and
``<schemas_dir>/dispositions.csv``. It also owns the two pure, per-row rules
that depend on a schema: header mapping and row validation.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_phone, parse_date, parse_decimal, parse_enum

FIELD_TYPES = ("string", "phone", "date", "decimal", "enum")


class SchemaError(ValueError):
    """A schema or catalogue file is unusable."""


def normalize_header(header: str) -> str:
    """lower-case, trim, then collapse every run of ``_``/whitespace to one space."""
    return re.sub(r"[\s_]+", " ", header.lower().strip())


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: tuple[str, ...] = ()
    values: tuple[str, ...] = ()
    default: str | None = None

    def parse(self, raw: str) -> str | None:
        """Canonical value for a non-empty raw cell, or None if invalid."""
        if self.type == "string":
            return raw.strip()
        if self.type == "phone":
            return normalize_phone(raw)
        if self.type == "date":
            return parse_date(raw)
        if self.type == "decimal":
            return parse_decimal(raw)
        if self.type == "enum":
            return parse_enum(raw, list(self.values))
        raise AssertionError(f"unhandled field type {self.type}")  # guarded at load time


@dataclass
class ParsedRow:
    values: dict[str, str | None]
    reject_reason: str | None
    field_warnings: int


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[FieldSpec]
    _lookup: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # Normalised name/alias -> field name. Built in schema order with
        # setdefault, so if two fields ever shared an alias the earlier one wins.
        for f in self.fields:
            for label in (f.name, *f.aliases):
                self._lookup.setdefault(normalize_header(label), f.name)

    @property
    def identity(self) -> FieldSpec:
        """The phone field that identifies a lead (first field of type phone)."""
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference(self) -> FieldSpec:
        return next(f for f in self.fields if f.reference)

    def map_columns(self, header: list[str]) -> dict[str, int]:
        """Field name -> column index. Unmatched columns are ignored and,
        when two columns map to the same field, the first (left-most) wins."""
        columns: dict[str, int] = {}
        for idx, raw in enumerate(header):
            name = self._lookup.get(normalize_header(raw))
            if name is not None:
                columns.setdefault(name, idx)
        return columns

    def parse_row(self, columns: dict[str, int], cells: list[str]) -> ParsedRow:
        """Validate one non-blank data row.

        Fields are walked in schema order; the first failure is the row's only
        reject reason. Invalid optional fields become null and count as one
        warning each, but warnings are only reported for rows that survive.
        """
        values: dict[str, str | None] = {}
        reason: str | None = None
        warnings = 0
        for f in self.fields:
            idx = columns.get(f.name)
            raw = cells[idx].strip() if idx is not None and idx < len(cells) else ""
            if raw == "":
                values[f.name] = None
                if f.required and reason is None:
                    reason = f"missing_required:{f.name}"
                continue
            parsed = f.parse(raw)
            values[f.name] = parsed
            if parsed is None:
                if f.required:
                    if reason is None:
                        reason = "invalid_phone" if f.type == "phone" else f"invalid_{f.name}"
                else:
                    warnings += 1
        return ParsedRow(values, reason, 0 if reason else warnings)

    def apply_defaults(self, values: dict[str, str | None]) -> dict[str, str | None]:
        return {
            name: (value if value is not None or spec.default is None else spec.default)
            for name, value, spec in ((f.name, values.get(f.name), f) for f in self.fields)
        }


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


@dataclass
class Catalogue:
    """Everything loaded from the schemas directory."""

    workflows: dict[str, WorkflowSchema]
    dispositions: list[Disposition]


def _load_schema(path: Path) -> WorkflowSchema:
    doc = json.loads(path.read_text(encoding="utf-8"))
    try:
        workflow_id = doc["workflow_id"]
        fields = []
        for f in doc["fields"]:
            if f["type"] not in FIELD_TYPES:
                raise SchemaError(f"{path}: field {f['name']!r} has unknown type {f['type']!r}")
            default = f.get("default")
            if default is not None and f["type"] == "enum":
                default = default.lower()
            fields.append(FieldSpec(
                name=f["name"], type=f["type"], required=bool(f.get("required", False)),
                pii=bool(f.get("pii", False)), reference=bool(f.get("reference", False)),
                aliases=tuple(f.get("aliases", ())), values=tuple(f.get("values", ())),
                default=default,
            ))
    except (KeyError, TypeError) as exc:
        raise SchemaError(f"{path}: malformed schema ({exc})") from exc

    phones = [f for f in fields if f.type == "phone"]
    refs = [f for f in fields if f.reference]
    if not phones or not phones[0].required:
        raise SchemaError(f"{path}: the first phone field identifies a lead and must be required")
    if len(refs) != 1 or refs[0].pii:
        raise SchemaError(f"{path}: need exactly one non-PII field with \"reference\": true")
    return WorkflowSchema(workflow_id, fields)


def _parse_bool(value: str, where: str) -> bool:
    v = value.strip().lower()
    if v not in ("true", "false"):
        raise SchemaError(f"{where}: expected true/false, got {value!r}")
    return v == "true"


def load_catalogue(schemas_dir: Path) -> Catalogue:
    schemas_dir = Path(schemas_dir)
    workflows: dict[str, WorkflowSchema] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        schema = _load_schema(path)
        if schema.workflow_id in workflows:
            raise SchemaError(f"{path}: duplicate workflow_id {schema.workflow_id!r}")
        workflows[schema.workflow_id] = schema
    if not workflows:
        raise SchemaError(f"no *.json lead schemas found in {schemas_dir}")

    disp_path = schemas_dir / "dispositions.csv"
    dispositions = []
    with disp_path.open(newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            where = f"{disp_path}:{n}"
            dispositions.append(Disposition(
                workflow_id=row["workflow_id"].strip(), code=row["code"].strip(),
                label=row["label"].strip(),
                is_terminal=_parse_bool(row["is_terminal"], where),
                is_success=_parse_bool(row["is_success"], where),
            ))
    return Catalogue(workflows, dispositions)
