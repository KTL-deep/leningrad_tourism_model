"""Synthetic adapter attestations: integration contracts, not empirical validation."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from src.etl.source_policy import DatasetCandidate
from src.pipeline.input_assessment import prepare_inputs
from src.storage import SnapshotStore


NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def ready_candidate(store, kind, area_id="synthetic_area"):
    source_id = f"synthetic_{kind}"
    snapshot = store.save_snapshot(
        source_id=source_id, area_id=area_id,
        url=f"https://example.org/{kind}", content=b'{"synthetic":true}',
        parser_version="synthetic-v1", media_type="application/json",
        completeness="complete", retrieved_at=NOW,
    )
    return DatasetCandidate(
        source_id=source_id, dataset_kind=kind, area_id=area_id,
        discovered_automatically=True, retrieved=True, content_validated=True,
        provenance_verified=True, coverage_verified=True, period_verified=True,
        units_verified=True, access_permitted=True, completeness="complete",
        snapshot_id=snapshot.snapshot_id,
    )


def assess(store, candidates, **overrides):
    args = dict(area_id="synthetic_area", candidates=candidates, store=store,
                code_version="synthetic-code-v1", configuration={"profile": "test"},
                run_id="synthetic_run", started_at=NOW)
    args.update(overrides)
    return prepare_inputs(**args)


def test_missing_statistics_and_reviews_preserve_independent_inputs(tmp_path):
    store = SnapshotStore(tmp_path)
    base = [ready_candidate(store, kind) for kind in ("boundary", "poi", "network")]
    absent_statistics = DatasetCandidate("missing_stats", "official_statistics", "synthetic_area")
    absent_reviews = DatasetCandidate("missing_reviews", "reviews", "synthetic_area")
    result = assess(store, [*base, absent_statistics, absent_reviews])
    report = result.to_dict()
    assert result.manifest.status == "partial"
    assert report["input_readiness"]["framework_map"]["available"]
    assert report["input_readiness"]["network_accessibility"]["available"]
    assert not report["input_readiness"]["quantified_deficit"]["available"]
    assert not report["input_readiness"]["review_signals"]["available"]
    assert report["computed_tourism_results"] == []
    assert all(not d.included and d.reasons for d in result.decisions[-2:])
    assert any("missing_stats/official_statistics" in item for item in result.manifest.limitations)
    artifacts = [store.get_snapshot(identifier) for identifier in result.manifest.snapshot_ids]
    artifact = next(item for item in artifacts if item.source_id == "input_assessment")
    stored = json.loads(store.read_content(artifact.snapshot_id))
    assert stored["artifact_kind"] == "derived_input_assessment"
    assert stored["datasets"][-1]["included"] is False
    assert stored["adapter_attestations"][-1]["discovered_automatically"] is False
    assert assess(store, [*base, absent_statistics, absent_reviews]).manifest == result.manifest


def test_no_essential_inputs_cannot_enable_map(tmp_path):
    result = assess(SnapshotStore(tmp_path), [])
    assert result.manifest.status == "failed"
    assert not result.to_dict()["input_readiness"]["framework_map"]["available"]


@pytest.mark.parametrize("problem", ["foreign_area", "foreign_source", "partial", "missing", "corrupt"])
def test_attestations_cannot_override_stored_provenance(tmp_path, problem):
    store = SnapshotStore(tmp_path)
    candidate = ready_candidate(store, "poi")
    if problem == "foreign_area":
        foreign = ready_candidate(store, "poi", area_id="foreign_area")
        candidate = replace(candidate, snapshot_id=foreign.snapshot_id)
    elif problem == "foreign_source":
        other = ready_candidate(store, "network")
        candidate = replace(candidate, snapshot_id=other.snapshot_id)
    elif problem == "partial":
        partial = store.save_snapshot("synthetic_poi", "synthetic_area", "https://example.org/poi",
                                      b'{"synthetic":true}', "synthetic-v1", "application/json", "partial")
        candidate = replace(candidate, snapshot_id=partial.snapshot_id)
    elif problem == "missing":
        candidate = replace(candidate, snapshot_id="missing")
    elif problem == "corrupt":
        snapshot = store.get_snapshot(candidate.snapshot_id)
        (tmp_path / "blobs" / f"{snapshot.content_hash}.bin").write_bytes(b"corrupt")
    with pytest.raises((ValueError, FileNotFoundError)):
        assess(store, [candidate])
    assert not (tmp_path / "runs" / "synthetic_run.json").exists()


def test_duplicate_source_kind_not_silently_overwritten(tmp_path):
    store = SnapshotStore(tmp_path)
    candidate = ready_candidate(store, "poi")
    with pytest.raises(ValueError, match="Duplicate"):
        assess(store, [candidate, candidate])


@pytest.mark.parametrize("area_id", ["synthetic_gatchina", "synthetic_vyborg"])
def test_same_input_contract_for_two_areas_without_city_report(tmp_path, area_id):
    store = SnapshotStore(tmp_path)
    candidates = [ready_candidate(store, kind, area_id) for kind in ("boundary", "poi", "network")]
    result = assess(store, candidates, area_id=area_id)
    assert result.to_dict()["input_readiness"]["network_accessibility"]["available"]
    assert result.manifest.status == "partial"
    assert not any(item.dataset_kind == "official_statistics" for item in result.decisions)
    assert store.get_run(result.manifest.run_id) == result.manifest


def test_review_reports_do_not_supply_demand_capacity_or_legal_permission(tmp_path):
    store = SnapshotStore(tmp_path)
    candidates = [ready_candidate(store, kind) for kind in ("boundary", "poi", "network", "reviews")]
    result = assess(store, candidates)
    inputs = result.to_dict()["input_readiness"]
    assert inputs["review_signals"]["available"]
    assert not inputs["quantified_deficit"]["available"]
    assert set(inputs["quantified_deficit"]["missing"]) == {"demand", "service_capacity"}
    assert not inputs["intervention_screening_inputs"]["available"]
    assert "legal_rules" in inputs["intervention_screening_inputs"]["missing"]
    assert result.to_dict()["computed_tourism_results"] == []


def test_supporting_artifacts_are_bound_to_run_but_do_not_enable_capabilities(tmp_path):
    store = SnapshotStore(tmp_path)
    artifact = store.save_snapshot("derived_network", "synthetic_area", "urn:synthetic:network",
                                    b'{}', "test-v1", "application/json", "partial")
    result = assess(store, [], supporting_snapshot_ids=(artifact.snapshot_id,))
    assert artifact.snapshot_id in result.manifest.snapshot_ids
    assert not result.to_dict()["input_readiness"]["network_accessibility"]["available"]
    assert store.get_run(result.manifest.run_id) == result.manifest


def test_foreign_supporting_artifact_is_not_accepted(tmp_path):
    store = SnapshotStore(tmp_path)
    artifact = store.save_snapshot("derived_network", "foreign_area", "urn:synthetic:network",
                                    b'{}', "test-v1", "application/json", "partial")
    with pytest.raises(ValueError, match="Supporting snapshot area"):
        assess(store, [], supporting_snapshot_ids=(artifact.snapshot_id,))
