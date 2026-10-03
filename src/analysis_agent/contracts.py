"""Versioned first-slice contracts. No business rules or private field names."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkbenchError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class RequiredFact(Contract):
    column: str = Field(min_length=1)
    entity: dict[str, Any] = Field(default_factory=dict)
    time: dict[str, str] | str | None = None
    allow_bounded: bool = False


class ResultSpec(Contract):
    schema_version: Literal["result_spec@1"] = "result_spec@1"
    requirements: list[RequiredFact] = Field(default_factory=list)


class DailyProtocol(Contract):
    dates: list[str] = Field(min_length=1, max_length=366)
    review_only: list[str] = Field(default_factory=list)
    cutoff_parameter: str = "as_of"

    @model_validator(mode="after")
    def ordered_dates(self):
        from datetime import date
        if self.dates != sorted(set(self.dates)) or any(date.fromisoformat(d).isoformat()!=d for d in self.dates):
            raise ValueError("Daily dates must be ordered unique ISO dates")
        if not set(self.review_only)<=set(self.dates):raise ValueError("Review dates must belong to schedule")
        return self


class RunRequest(Contract):
    project_id: str = Field(min_length=1, max_length=100)
    source_ref: str
    skill_ref: str
    question: str = Field(min_length=1, max_length=8000)
    parameters: dict[str, Any]
    consistency: Literal["live", "frozen"] = "live"
    daily_protocol: DailyProtocol | None = None
    tool_budget: int = Field(default=12, ge=1, le=30)
    report_budget: int = Field(default=3, ge=1, le=20)
    required_fact_columns: list[str] = Field(default_factory=list)
    result_spec: ResultSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def exclusive_result_contract(cls, value):
        if isinstance(value, dict) and value.get("result_spec") is not None and "required_fact_columns" in value:
            raise ValueError("result_spec and required_fact_columns are mutually exclusive")
        return value

    @model_serializer(mode="wrap")
    def serialize_contract(self, handler):
        value = handler(self)
        if self.daily_protocol is None:value.pop("daily_protocol",None)
        if self.result_spec is not None:
            value.pop("required_fact_columns", None)
        else:
            value.pop("result_spec", None)
            if "required_fact_columns" not in self.model_fields_set:
                value.pop("required_fact_columns", None)
        return value


class ToolSpec(Contract):
    ref: str
    capability: str
    description: str
    input_schema: dict[str, Any]
    output_schema_version: str = "table_result@1"
    timeout_seconds: int = 15
    max_result_bytes: int = 1_100_000
    side_effects: Literal["none", "external"]
    retry: Literal["never", "new_action_allowed"]
    consistency: Literal["live"] = "live"


class SkillRelease(Contract):
    ref: str
    instructions: str
    data_contract: str
    tools: tuple[str, ...]
    privacy: Literal["public", "private"] = "public"
    attachments: dict[str, str] = Field(default_factory=dict)


class TableResult(Contract):
    rows: list[dict[str, Any]]
    units: dict[str, str]
    time_range: dict[str, str]
    entity_keys: list[str]
    completeness: Literal["complete_for_query", "bounded", "unknown"]
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class EvidenceRef(Contract):
    evidence_id: str
    row: int = Field(ge=0)
    column: str


class Fact(Contract):
    kind: Literal["observation", "derived"] = "observation"
    refs: list[EvidenceRef] = Field(min_length=1, max_length=2)
    operation: Literal["cell", "subtract", "ratio"] = "cell"
    value: str | int | float | bool | None
    unit: str
    entity: dict[str, Any]
    time_range: dict[str, str]
    label: str = Field(max_length=1000)


class EvidenceInput(Contract):
    evidence_id: str
    row: int = Field(ge=0)
    column: str
    entity: dict[str, Any]
    time_range: dict[str, str]


class FactV2(Contract):
    schema_version: Literal["fact@2"] = "fact@2"
    kind: Literal["observation", "derived"] = "observation"
    inputs: list[EvidenceInput] = Field(min_length=1, max_length=2)
    operation: Literal["cell", "subtract", "ratio", "pct_change", "pp_change"] = "cell"
    value: str | int | float | bool | None
    unit: str
    note: str = Field(default="", max_length=4000)


class Report(Contract):
    result_status: Literal["complete", "partial", "insufficient_evidence"]
    title: str = Field(min_length=1, max_length=500)
    facts: list[FactV2 | Fact]
    hypotheses: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)


class Adapter(Protocol):
    """Trusted registered code; never imported from a request-supplied path."""
    ref: str
    data_contract: str
    privacy: str
    tools: dict[str, ToolSpec]

    def catalog(self) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, bool]: ...
    def validate_task(self, parameters: dict[str, Any]) -> None: ...
    def validate_call(self, tool_ref: str, args: dict[str, Any], parameters: dict[str, Any]) -> None: ...
    def execute(self, tool_ref: str, args: dict[str, Any], parameters: dict[str, Any]) -> TableResult: ...


class Registry:
    def __init__(self, adapters: list[Adapter], skills: list[SkillRelease]):
        for adapter in adapters:
            for spec in adapter.tools.values():
                ToolSpec.model_validate(spec.model_dump(mode="json") if isinstance(spec, ToolSpec) else spec)
        self.adapters = {a.ref: a for a in adapters}
        self.skills = {s.ref: s for s in skills}
        if len(self.adapters) != len(adapters) or len(self.skills) != len(skills):
            raise ValueError("Duplicate release reference")

    def resolve(self, request: RunRequest):
        adapter = self.adapters.get(request.source_ref)
        skill = self.skills.get(request.skill_ref)
        if adapter is None or skill is None:
            raise WorkbenchError("not_found", "Unregistered source or Skill", 404)
        if request.consistency == "frozen":
            from .snapshots import Snapshot
            if not isinstance(getattr(adapter, "snapshot", None), Snapshot):
                raise WorkbenchError("unsupported", "Frozen requires a registered queryable snapshot", 422)
            adapter.snapshot.manager.open(adapter.snapshot.key)
        if not adapter.capabilities().get(request.consistency, False):
            raise WorkbenchError("unsupported", "Requested consistency is unsupported", 422)
        if skill.data_contract != adapter.data_contract or any(t not in adapter.tools for t in skill.tools):
            raise WorkbenchError("incompatible", "Skill and data/tool contracts are incompatible", 422)
        if adapter.privacy != skill.privacy:
            raise WorkbenchError("incompatible", "Skill privacy does not match source", 422)
        adapter.validate_task(request.parameters)
        return adapter, skill
