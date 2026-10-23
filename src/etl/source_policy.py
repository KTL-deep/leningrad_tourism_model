"""Fail-closed eligibility of adapter-attested source snapshots.

This policy consumes attestations; it does not fetch or validate source content.
In particular, an eligible legal-rules document is not permission for any action.
"""

from dataclasses import asdict, dataclass


DATASET_KINDS = frozenset({
    "boundary", "poi", "network", "official_statistics", "reviews",
    "legal_rules", "demand", "service_capacity", "trip_inputs", "costs",
})


def _nonempty_string(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a nonempty string")
    if not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _kind(value: str) -> None:
    _nonempty_string(value, "dataset_kind")
    if value not in DATASET_KINDS:
        raise ValueError(f"Unsupported dataset_kind: {value}")


@dataclass
class DatasetCandidate:
    source_id: str
    dataset_kind: str
    area_id: str
    discovered_automatically: bool = False
    retrieved: bool = False
    content_validated: bool = False
    provenance_verified: bool = False
    coverage_verified: bool = False
    period_verified: bool = False
    units_verified: bool = False
    access_permitted: bool = False
    completeness: str = "unknown"
    retrieval_error: str | None = None
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.source_id, "source_id")
        _nonempty_string(self.area_id, "area_id")
        _kind(self.dataset_kind)
        for field in (
            "discovered_automatically", "retrieved", "content_validated",
            "provenance_verified", "coverage_verified", "period_verified",
            "units_verified", "access_permitted",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be bool")
        if not isinstance(self.completeness, str):
            raise TypeError("completeness must be complete, partial or unknown")
        if self.completeness not in ("complete", "partial", "unknown"):
            raise ValueError("completeness must be complete, partial or unknown")
        if self.retrieval_error is not None and not isinstance(self.retrieval_error, str):
            raise TypeError("retrieval_error must be a string or None")
        if self.snapshot_id is not None:
            _nonempty_string(self.snapshot_id, "snapshot_id")


@dataclass(frozen=True)
class DatasetDecision:
    source_id: str
    dataset_kind: str
    area_id: str
    included: bool
    reasons: tuple[str, ...]
    snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty_string(self.source_id, "source_id")
        _nonempty_string(self.area_id, "area_id")
        _kind(self.dataset_kind)
        if type(self.included) is not bool:
            raise TypeError("included must be bool")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reasons
        ):
            raise TypeError("reasons must be a tuple of nonempty strings")
        if self.included and self.reasons:
            raise ValueError("included decision cannot have exclusion reasons")
        if not self.included and not self.reasons:
            raise ValueError("excluded decision needs a reason")
        if self.snapshot_id is not None:
            _nonempty_string(self.snapshot_id, "snapshot_id")
        if self.included and self.snapshot_id is None:
            raise ValueError("included decision needs a snapshot_id")

    def to_dict(self) -> dict:
        return asdict(self)


def assess_candidate(candidate: DatasetCandidate, area_id: str) -> DatasetDecision:
    """Include only complete, area-matched snapshots with every adapter check attested."""
    _nonempty_string(area_id, "area_id")
    if not isinstance(candidate, DatasetCandidate):
        raise TypeError("candidate must be a DatasetCandidate")
    candidate.__post_init__()  # A mutable candidate may have changed since creation.

    reasons = []
    if candidate.area_id != area_id:
        reasons.append("area_mismatch")
    if not candidate.discovered_automatically:
        reasons.append("not_discovered")
    if not candidate.retrieved or candidate.retrieval_error:
        reasons.append("fetch_failed")
    for field, reason in (
        ("content_validated", "validation_failed"),
        ("provenance_verified", "unknown_provenance"),
        ("coverage_verified", "unknown_coverage"),
        ("period_verified", "unknown_period"),
        ("units_verified", "unknown_units"),
        ("access_permitted", "access_not_permitted"),
    ):
        if not getattr(candidate, field):
            reasons.append(reason)
    if candidate.completeness != "complete":
        reasons.append("incomplete")
    if not candidate.snapshot_id:
        reasons.append("missing_snapshot")
    return DatasetDecision(candidate.source_id, candidate.dataset_kind, area_id,
                           not reasons, tuple(reasons), candidate.snapshot_id)


CAPABILITY_REQUIREMENTS = {
    "framework_map": ("boundary", "poi"),
    "network_accessibility": ("boundary", "poi", "network"),
    "quantified_deficit": ("boundary", "poi", "network", "demand", "service_capacity"),
    "review_signals": ("poi", "reviews"),
    "intervention_screening_inputs": ("boundary", "poi", "network", "legal_rules"),
    "investment_comparison_inputs": ("boundary", "poi", "network", "legal_rules", "costs"),
    "trip_feasibility_inputs": ("boundary", "poi", "network", "legal_rules", "trip_inputs"),
}


def assess_capabilities(decisions: list[DatasetDecision], area_id: str | None = None) -> dict:
    """Report available inputs, not computed results or per-action legal permission.

    Each decision belongs to one assessed territory; cross-territory aggregation
    is rejected rather than treating foreign records as local inputs.
    """
    if area_id is not None:
        _nonempty_string(area_id, "area_id")
    areas = set()
    for decision in decisions:
        if not isinstance(decision, DatasetDecision):
            raise TypeError("decisions must contain DatasetDecision records")
        areas.add(decision.area_id)
    if len(areas) > 1 or (area_id is not None and areas and area_id not in areas):
        raise ValueError("decisions must belong to the target area")
    included = {decision.dataset_kind for decision in decisions if decision.included}
    return {
        name: {"available": all(kind in included for kind in required),
               "missing": [kind for kind in required if kind not in included]}
        for name, required in CAPABILITY_REQUIREMENTS.items()
    }
