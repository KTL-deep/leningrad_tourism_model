"""Small, JSON-serializable provenance contracts (schema version 1)."""

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import math
import re


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Invalid record ID: {value!r}")
    return value


def _version(version: int) -> None:
    if type(version) is not int or version != 1:
        raise ValueError(f"Unsupported schema version: {version!r}")


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _number(value: float | None, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError(f"{name} must be finite")


class KnowledgeStatus(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceSnapshot:
    snapshot_id: str
    source_id: str
    area_id: str
    url: str
    content_hash: str
    byte_length: int
    parser_version: str
    media_type: str
    completeness: str
    retrieved_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        for identifier in (self.snapshot_id, self.source_id, self.area_id):
            valid_id(identifier)
        for name in ("url", "parser_version", "media_type", "completeness"):
            _text(getattr(self, name), name)
        if self.completeness not in ("complete", "partial", "unknown"):
            raise ValueError("Invalid snapshot completeness")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("Invalid content hash")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise ValueError("Invalid byte length")
        if not isinstance(self.retrieved_at, datetime) or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")

    def to_dict(self) -> dict:
        return {**asdict(self), "retrieved_at": self.retrieved_at.isoformat()}


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    snapshot_id: str
    locator: str
    unit: str
    status: KnowledgeStatus | str
    value: float | None
    method: str | None = None
    lower: float | None = None
    upper: float | None = None
    reason: str | None = None
    excerpt: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        valid_id(self.evidence_id)
        valid_id(self.snapshot_id)
        _text(self.locator, "locator")
        _text(self.unit, "unit")
        for name in ("method", "reason", "excerpt"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if not isinstance(self.status, (str, KnowledgeStatus)):
            raise ValueError("Invalid knowledge status")
        try:
            status = KnowledgeStatus(self.status)
        except ValueError as exc:
            raise ValueError("Invalid knowledge status") from exc
        object.__setattr__(self, "status", status)
        for name in ("value", "lower", "upper"):
            _number(getattr(self, name), name)
        if status is KnowledgeStatus.UNKNOWN:
            if self.value is not None or self.lower is not None or self.upper is not None:
                raise ValueError("Unknown evidence cannot have numeric values")
            _text(self.reason, "reason")
        else:
            if self.value is None:
                raise ValueError("Known evidence needs a numeric value")
            if status in (KnowledgeStatus.DERIVED, KnowledgeStatus.ESTIMATED):
                _text(self.method, "method")
            if status is KnowledgeStatus.ESTIMATED and (self.lower is None or self.upper is None):
                raise ValueError("Estimates need an interval")
            if (self.lower is None) != (self.upper is None):
                raise ValueError("Interval requires both bounds")
            if self.lower is not None and not self.lower <= self.value <= self.upper:
                raise ValueError("Value must lie within interval")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    area_id: str
    code_version: str
    config_hash: str
    started_at: datetime
    snapshot_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    status: str
    limitations: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _version(self.schema_version)
        valid_id(self.run_id)
        valid_id(self.area_id)
        _text(self.code_version, "code_version")
        _text(self.status, "status")
        if self.status not in ("pending", "running", "succeeded", "partial", "failed", "skipped"):
            raise ValueError("Invalid run status")
        if not isinstance(self.config_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.config_hash):
            raise ValueError("config_hash must be a lowercase SHA-256 digest")
        if not isinstance(self.started_at, datetime) or self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        for ids in (self.snapshot_ids, self.evidence_ids):
            if not isinstance(ids, tuple):
                raise ValueError("References must be tuples")
            for identifier in ids:
                valid_id(identifier)
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate run references")
        if not isinstance(self.limitations, tuple):
            raise ValueError("limitations must be a tuple")
        for limitation in self.limitations:
            _text(limitation, "limitation")

    def to_dict(self) -> dict:
        return {**asdict(self), "started_at": self.started_at.isoformat()}
