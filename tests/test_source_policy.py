"""Synthetic adapter attestations only: no live source is validated by these tests."""

from dataclasses import FrozenInstanceError, replace

import pytest

from src.etl.source_policy import (
    DatasetCandidate, DatasetDecision, assess_candidate, assess_capabilities,
)


AREA = "synthetic-area"


def attested(kind, **changes):
    # Synthetic complete adapter attestation, NOT evidence of real-world validity.
    candidate = DatasetCandidate(
        source_id=f"synthetic-{kind}", dataset_kind=kind, area_id=AREA,
        discovered_automatically=True, retrieved=True, content_validated=True,
        provenance_verified=True, coverage_verified=True, period_verified=True,
        units_verified=True, access_permitted=True, completeness="complete",
        snapshot_id=f"synthetic-{kind}-snapshot",
    )
    return replace(candidate, **changes)


@pytest.mark.parametrize("kind", [
    "boundary", "poi", "network", "official_statistics", "reviews", "legal_rules",
    "demand", "service_capacity", "trip_inputs", "costs",
])
def test_t11_complete_synthetic_attestation_is_eligible(kind):
    decision = assess_candidate(attested(kind), AREA)
    assert decision.included and decision.reasons == ()
    assert decision.to_dict() == {
        "source_id": f"synthetic-{kind}", "dataset_kind": kind, "area_id": AREA,
        "included": True, "reasons": (), "snapshot_id": f"synthetic-{kind}-snapshot",
    }
    with pytest.raises(FrozenInstanceError):
        decision.included = False


@pytest.mark.parametrize("field,value,reason", [
    ("discovered_automatically", False, "not_discovered"),
    ("retrieved", False, "fetch_failed"),
    ("retrieval_error", "HTTP 500", "fetch_failed"),
    ("content_validated", False, "validation_failed"),
    ("provenance_verified", False, "unknown_provenance"),
    ("coverage_verified", False, "unknown_coverage"),
    ("period_verified", False, "unknown_period"),
    ("units_verified", False, "unknown_units"),
    ("access_permitted", False, "access_not_permitted"),
    ("completeness", "partial", "incomplete"),
    ("completeness", "unknown", "incomplete"),
    ("snapshot_id", None, "missing_snapshot"),
])
@pytest.mark.parametrize("kind", ["official_statistics", "reviews", "network", "legal_rules", "demand"])
def test_t12_missing_attestation_excludes(kind, field, value, reason):
    decision = assess_candidate(attested(kind, **{field: value}), AREA)
    assert not decision.included and reason in decision.reasons


def test_t12_http_200_schema_probe_is_not_dataset_validation():
    # HTTP success alone cannot attest retrieval of usable data or content validity.
    probe_only = DatasetCandidate("synthetic-probe", "official_statistics", AREA,
                                  discovered_automatically=True)
    decision = assess_candidate(probe_only, AREA)
    assert not decision.included
    assert {"fetch_failed", "validation_failed", "incomplete", "missing_snapshot"} <= set(decision.reasons)


def test_t13_capabilities_do_not_substitute_reviews_or_optional_statistics():
    decisions = [assess_candidate(attested(kind), AREA) for kind in ("boundary", "poi", "network")]
    capabilities = assess_capabilities(decisions)
    assert capabilities["framework_map"] == {"available": True, "missing": []}
    assert capabilities["network_accessibility"]["available"]
    assert capabilities["quantified_deficit"]["missing"] == ["demand", "service_capacity"]
    assert capabilities["review_signals"]["missing"] == ["reviews"]
    assert "official_statistics" not in capabilities["framework_map"]["missing"]

    decisions.append(assess_candidate(attested("reviews"), AREA))
    assert assess_capabilities(decisions)["review_signals"]["available"]
    assert not assess_capabilities(decisions)["quantified_deficit"]["available"]


def test_t13_wrong_area_partial_data_and_multiple_providers():
    decisions = [assess_candidate(attested("boundary"), AREA),
                 assess_candidate(attested("poi", area_id="other"), AREA),
                 assess_candidate(attested("poi", source_id="partial-poi", completeness="partial"), AREA)]
    assert "area_mismatch" in decisions[1].reasons
    assert decisions[1].area_id == AREA
    assert assess_capabilities(decisions)["framework_map"]["missing"] == ["poi"]
    decisions.append(assess_candidate(attested("poi", source_id="second-provider"), AREA))
    assert assess_capabilities(decisions)["framework_map"]["available"]


def test_t13_legal_document_is_only_a_prerequisite_not_action_permission():
    decisions = [assess_candidate(attested(kind), AREA) for kind in
                 ("boundary", "poi", "network", "legal_rules")]
    caps = assess_capabilities(decisions)
    assert caps["intervention_screening_inputs"]["available"]
    assert caps["investment_comparison_inputs"]["missing"] == ["costs"]
    assert caps["trip_feasibility_inputs"]["missing"] == ["trip_inputs"]
    decisions.extend(assess_candidate(attested(kind), AREA) for kind in ("costs", "trip_inputs"))
    assert assess_capabilities(decisions)["investment_comparison_inputs"]["available"]
    assert assess_capabilities(decisions)["trip_feasibility_inputs"]["available"]
    # These are document/data prerequisites; no per-action legal authorization is assessed.


def test_unknown_kind_cannot_be_included():
    with pytest.raises(ValueError, match="Unsupported dataset_kind"):
        assess_candidate(attested("unknown"), AREA)


@pytest.mark.parametrize("field,value", [
    ("discovered_automatically", "true"), ("retrieved", "false"),
    ("content_validated", 1), ("provenance_verified", "yes"),
    ("coverage_verified", 0), ("period_verified", "true"),
    ("units_verified", "true"), ("access_permitted", 1),
])
def test_truthy_or_falsey_nonboolean_attestations_rejected(field, value):
    with pytest.raises(TypeError, match="must be bool"):
        attested("poi", **{field: value})


@pytest.mark.parametrize("field,value,error", [
    ("source_id", " ", ValueError), ("source_id", 7, TypeError),
    ("area_id", "", ValueError), ("area_id", None, TypeError),
    ("snapshot_id", " ", ValueError), ("snapshot_id", 3, TypeError),
    ("completeness", "COMPLETE", ValueError), ("completeness", None, TypeError),
])
def test_invalid_attestation_fields_rejected(field, value, error):
    with pytest.raises(error):
        attested("poi", **{field: value})


@pytest.mark.parametrize("target,error", [(" ", ValueError), (None, TypeError), (4, TypeError)])
def test_invalid_target_area_rejected(target, error):
    with pytest.raises(error):
        assess_candidate(attested("poi"), target)
    if target is not None:  # None means infer area for capabilities.
        with pytest.raises(error):
            assess_capabilities([], target)


def test_cross_area_decisions_cannot_be_aggregated():
    local = assess_candidate(attested("boundary"), AREA)
    foreign = assess_candidate(attested("poi", area_id="other"), "other")
    for target in (None, AREA):
        with pytest.raises(ValueError, match="target area"):
            assess_capabilities([local, foreign], target)
    with pytest.raises(ValueError, match="target area"):
        assess_capabilities([foreign], AREA)
    assert assess_capabilities([], AREA)["framework_map"] == {
        "available": False, "missing": ["boundary", "poi"]}
    assert assess_capabilities([local], AREA)["framework_map"]["missing"] == ["poi"]


@pytest.mark.parametrize("changes,error", [
    ({"included": "false"}, TypeError),
    ({"dataset_kind": "invented"}, ValueError),
    ({"area_id": ""}, ValueError),
    ({"reasons": ("validation_failed",)}, ValueError),
    ({"snapshot_id": None}, ValueError),
    ({"reasons": []}, TypeError),
])
def test_manually_constructed_included_decisions_fail_closed(changes, error):
    fields = dict(source_id="synthetic", dataset_kind="poi", area_id=AREA,
                  included=True, reasons=(), snapshot_id="synthetic-snapshot")
    fields.update(changes)
    with pytest.raises(error):
        DatasetDecision(**fields)
