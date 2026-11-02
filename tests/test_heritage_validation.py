"""Offline curated-pair evaluation and immutable provenance tests."""

import copy
from datetime import datetime, timezone
import json

import pytest

from scripts.validate_heritage_matching import build_audit, main
from src.data_models import RunManifest
from src.etl.heritage_locality import INTERPRETATION, PARSER_VERSION, SOURCE_ID
from src.etl.heritage_validation import (REVIEW_BASIS, evaluate_heritage_benchmark,
                                         strict_json)
from src.storage import SnapshotStore


def case(identifier, label="supported", *, record=None, poi=None):
    return {"case_id": identifier, "source_run_id": "run1", "source_record_id": record or identifier,
            "poi_id": poi or "node/1", "official_registry_snapshot_id": "official",
            "osm_poi_snapshot_id": "poi", "label": label, "rationale": "manual review",
            "evidence": ["registry:record", "osm:feature"]}


def benchmark(cases):
    return {"schema_version": 1, "benchmark_id": "curated-v1", "selection_protocol": "developer selected",
            "review_basis": REVIEW_BASIS, "cases": cases}


def report(links=(), candidates=()):
    return {"schema_version": 1, "artifact_kind": "advisory_heritage_matching",
            "algorithm_version": "heritage-matching-v1", "source_run_id": "run1",
            "official_registry_snapshot_id": "official", "osm_poi_snapshot_id": "poi",
            "identifier_links": list(links), "review_candidates": list(candidates)}


def entry(record, status="identifier_link"):
    return {"source_record_id": record, "poi_id": "node/1", "status": status}


def excluded(record, reason="osm_non_object_subject_role"):
    return {**entry(record, "excluded_name_candidate"), "reason": reason,
            "evidence": {"osm_subject_role": "information_about_feature"}}


def test_counts_missing_unresolved_and_candidate_union_and_immutability():
    source = benchmark([case("a"), case("b", "contradicted"), case("c", "unresolved"),
                        case("d"), case("e", "supported")])
    proposals = [report([entry("a"), entry("b"), entry("c")],
                        [entry("e", "name_distance_review_candidate")])]
    original = copy.deepcopy((source, proposals))
    result = evaluate_heritage_benchmark(source, proposals)
    assert (source, proposals) == original
    assert result == evaluate_heritage_benchmark(source, proposals)
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["selection_protocol"] == "developer selected"
    assert result["evaluated_algorithm_versions"] == ["heritage-matching-v1"]
    assert result["sample_biased"] and not result["external_ground_truth"]
    links, retrieval = result["channels"]["identifier_links"], result["channels"]["candidate_retrieval"]
    assert (links["emitted_supported"], links["emitted_contradicted"], links["emitted_unresolved"],
            links["missed_supported"], links["supported_total"]) == (1, 1, 1, 2, 3)
    assert links["correspondence_rate_on_resolved_emitted_pairs"] == 0.5
    assert links["retrieval_rate_on_reviewed_supported_pairs"] == 1 / 3
    assert retrieval["emitted_supported"] == 2
    assert retrieval["retrieval_rate_on_reviewed_supported_pairs"] == 2 / 3
    assert result["cases"][3]["candidate_retrieval"] == {"present": False, "status": None}
    assert all(decision["exclusion"] is None for decision in result["cases"])


def test_excluded_supported_is_retrieval_miss_not_negative_and_unresolved_is_not_denominator():
    b = benchmark([case("x"), case("y", "unresolved")])
    r = report()
    r["excluded_review_candidates"] = [excluded("x"), excluded(
        "y", "unlocated_name_conflicts_with_explicit_registry_reference")]
    result = evaluate_heritage_benchmark(b, [r])
    for counts in result["channels"].values():
        assert counts["missed_supported"] == 1
        assert counts["missed_unresolved"] == 1
        assert counts["emitted_contradicted"] == 0
        assert counts["retrieval_rate_on_reviewed_supported_pairs"] == 0
        assert counts["correspondence_rate_on_resolved_emitted_pairs"] is None
    assert result["cases"][0]["exclusion"] == {"status": "excluded_name_candidate",
                                                "reason": "osm_non_object_subject_role"}
    assert result["cases"][0]["candidate_retrieval"] == {"present": False, "status": None}


def test_v4_locality_exclusion_is_not_a_ground_truth_negative():
    r = report()
    r["algorithm_version"] = "heritage-matching-v4"
    r["excluded_review_candidates"] = [excluded("x", "unlocated_address_locality_conflict")]
    result = evaluate_heritage_benchmark(benchmark([case("x")]), [r])
    assert result["cases"][0]["exclusion"]["reason"] == "unlocated_address_locality_conflict"
    for counts in result["channels"].values():
        assert counts["missed_supported"] == counts["supported_total"] == 1
        assert counts["emitted_contradicted"] == 0
        assert counts["retrieval_rate_on_reviewed_supported_pairs"] == 0


@pytest.mark.parametrize("change", [
    lambda r: r.update(excluded_review_candidates={}),
    lambda r: r["excluded_review_candidates"].append(excluded("x")),
    lambda r: r["review_candidates"].append(entry("x", "name_only_unlocated_candidate")),
    lambda r: r["identifier_links"].append(entry("x")),
    lambda r: r["excluded_review_candidates"][0].update(status="review_candidate"),
    lambda r: r["excluded_review_candidates"][0].update(reason="unknown"),
    lambda r: r["excluded_review_candidates"][0].update(evidence=None),
    lambda r: r["excluded_review_candidates"][0].update(source_record_id=""),
])
def test_malformed_or_overlapping_exclusions_fail_closed(change):
    r = report()
    r["excluded_review_candidates"] = [excluded("x")]
    change(r)
    with pytest.raises(ValueError):
        evaluate_heritage_benchmark(benchmark([case("x")]), [r])


def test_null_denominators():
    result = evaluate_heritage_benchmark(benchmark([case("x", "unresolved")]), [report()])
    for channel in result["channels"].values():
        assert channel["correspondence_rate_on_resolved_emitted_pairs"] is None
        assert channel["retrieval_rate_on_reviewed_supported_pairs"] is None


def test_algorithm_versions_are_sorted_and_unique_across_runs():
    b = benchmark([case("x")])
    b["cases"].append({**case("y"), "source_run_id": "run2"})
    first = report()
    first["algorithm_version"] = "v2"
    second = report()
    second.update(source_run_id="run2", algorithm_version="v1")
    assert evaluate_heritage_benchmark(b, [first, second])["evaluated_algorithm_versions"] == ["v1", "v2"]


@pytest.mark.parametrize("modify", [
    lambda b, r: b["cases"][0].update(label="invented"),
    lambda b, r: b["cases"][0].update(rationale=""),
    lambda b, r: b["cases"][0].update(evidence=[]),
    lambda b, r: b["cases"].append({**b["cases"][0], "case_id": "another"}),
    lambda b, r: r.append(copy.deepcopy(r[0])),
    lambda b, r: r[0].update(official_registry_snapshot_id="wrong"),
    lambda b, r: r[0]["review_candidates"].append(entry("x", "name_only_unlocated_candidate")),
    lambda b, r: r[0]["identifier_links"].append(entry("x", "ambiguous_identifier")),
    lambda b, r: b["cases"][0].update(source_run_id="unknown"),
])
def test_invalid_cases_and_outputs_rejected(modify):
    b, r = benchmark([case("x")]), [report([entry("x")])]
    modify(b, r)
    with pytest.raises(ValueError):
        evaluate_heritage_benchmark(b, r)


def test_nonfinite_duplicate_json_and_caps():
    for data in (b'{"x":NaN}', b'{"x":1e999}', b'{"x":-1e999}',
                 b'{"x":1,"x":2}', b" " * (64 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            strict_json(data)
    b = benchmark([case("x")])
    b["extra"] = float("inf")
    with pytest.raises(ValueError):
        evaluate_heritage_benchmark(b, [report()])
    with pytest.raises(ValueError):
        evaluate_heritage_benchmark(benchmark([]), [report()] * 101)


@pytest.fixture
def stored(tmp_path):
    store = SnapshotStore(tmp_path / "store")

    def save(source, document):
        return store.save_snapshot(source, "area", "urn:test:" + source,
                                   json.dumps(document).encode(), "test-v1", "application/json")

    registry = save("mkrf_egrkn", {"area_id": "area", "records": [{"source_record_id": "x"}]})
    poi = save("geofabrik_poi", {"type": "FeatureCollection", "features": [{"id": "node/1"}]})
    store.save_run(RunManifest("run1", "area", "test", "a" * 64, datetime.now(timezone.utc),
                               (poi.snapshot_id,), (), "succeeded"))
    b = benchmark([case("x")])
    b["cases"][0].update(official_registry_snapshot_id=registry.snapshot_id,
                          osm_poi_snapshot_id=poi.snapshot_id)
    r = report([entry("x")])
    r.update(official_registry_snapshot_id=registry.snapshot_id, osm_poi_snapshot_id=poi.snapshot_id,
             area_id="area")
    matching = save("heritage_matching", r)
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(b), encoding="utf-8")
    return store, path, matching, registry, poi


def test_cli_audit_exclusive_and_foreign_records(stored, tmp_path):
    store, path, matching, registry, poi = stored
    args = ["--benchmark", str(path), "--store", str(store.root),
            "--matching-snapshot-id", matching.snapshot_id, "--output", str(tmp_path / "out.json")]
    assert main(args) == 0
    result = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert result["audit"]["matching_snapshots"][0]["sha256"] == matching.content_hash
    assert {ref["sha256"] for ref in result["audit"]["input_snapshots"]} == {
        registry.content_hash, poi.content_hash}
    assert main(args) == 2  # Never replace an existing audit.
    document = json.loads(path.read_text(encoding="utf-8"))
    document["cases"][0]["source_record_id"] = "foreign"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="foreign_case_pair"):
        build_audit(path, store, [matching.snapshot_id])


def test_cli_rejects_large_and_nonfinite_benchmark(stored):
    store, path, matching, _, _ = stored
    path.write_bytes(b" " * (64 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="document_too_large"):
        build_audit(path, store, [matching.snapshot_id])
    path.write_bytes(b'{"cases":NaN}')
    with pytest.raises(ValueError, match="nonfinite_json"):
        build_audit(path, store, [matching.snapshot_id])


def test_cli_rejects_foreign_report_pairs_and_invalid_run_provenance(stored):
    store, path, _, registry, poi = stored

    def save(document):
        return store.save_snapshot("heritage_matching", "area", "urn:test:matching",
                                   json.dumps(document).encode(), "test-v1", "application/json")

    r = report([entry("foreign")])
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id)
    with pytest.raises(ValueError, match="foreign_report_pair"):
        build_audit(path, store, [save(r).snapshot_id])
    r["identifier_links"] = [entry("x")]
    r["source_run_id"] = "unknown"
    with pytest.raises((OSError, ValueError)):
        build_audit(path, store, [save(r).snapshot_id])


def test_cli_rejects_foreign_excluded_pair(stored):
    store, path, _, registry, poi = stored
    r = report()
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id, excluded_review_candidates=[excluded("foreign")])
    matching = store.save_snapshot("heritage_matching", "area", "urn:test:excluded",
                                   json.dumps(r).encode(), "test-v3", "application/json")
    with pytest.raises(ValueError, match="foreign_report_pair"):
        build_audit(path, store, [matching.snapshot_id])


def test_cli_accepts_provenanced_excluded_pair_without_counting_emission(stored):
    store, path, _, registry, poi = stored
    r = report()
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id, excluded_review_candidates=[excluded("x")])
    matching = store.save_snapshot("heritage_matching", "area", "urn:test:excluded",
                                   json.dumps(r).encode(), "test-v3", "application/json")
    audit = build_audit(path, store, [matching.snapshot_id])
    assert audit["cases"][0]["exclusion"]["reason"] == "osm_non_object_subject_role"
    assert audit["channels"]["candidate_retrieval"]["missed_supported"] == 1


def locality_report(stored, *, original=True, source=SOURCE_ID, area="area",
                    digest=True, time=True, summary=False):
    store, _, _, registry, poi = stored
    raw = json.dumps([{"osm_type": "relation", "osm_id": 12, "category": "boundary",
                       "type": "administrative", "name": "Петергоф", "addresstype": "city",
                       "address": {"city": "Петергоф", "state": "Санкт-Петербург",
                                   "country_code": "ru"},
                       "geojson": {"type": "Polygon", "coordinates": [
                           [[29, 59], [30, 59], [30, 60], [29, 59]]]}}], ensure_ascii=False).encode()
    url = "https://nominatim.openstreetmap.org/search?q=test"
    original_snapshot = store.save_snapshot("osm_nominatim", "foreign-city", url, raw,
                                            "nominatim-boundary-v1", "application/json", "complete")
    rebound = store.save_snapshot(source, area, url, raw, PARSER_VERSION, "application/json",
                                  "partial", retrieved_at=(original_snapshot.retrieved_at if time else None))
    context = {"context_version": 1, "source_snapshot_id": rebound.snapshot_id,
               "original_snapshot_id": original_snapshot.snapshot_id if original else "missing",
               "raw_sha256": rebound.content_hash if digest else "a" * 64,
               "locality_name": "Петергоф", "region_name": "Санкт-Петербург",
               "query": "Петергоф, Санкт-Петербург, Россия", "status": "source_reported",
               "bbox": [29, 59, 30, 60], "guard_m": 5000, "interpretation": INTERPRETATION}
    r = report()
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id,
             excluded_review_candidates=[excluded("x", "unlocated_address_locality_conflict")])
    r["excluded_review_candidates"][0]["evidence"]["official_address_locality_context"] = context
    r["excluded_review_candidates"][0]["evidence"]["address_locality_comparison"] = {
        "status": "outside_guarded_context_envelope"}
    if summary:
        r["address_locality_context"] = {"snapshot_ids": [rebound.snapshot_id]}
    matching = store.save_snapshot("heritage_matching", "area", "urn:test:locality:matching",
                                   json.dumps(r, ensure_ascii=False).encode(), "test-v4", "application/json")
    return matching, rebound, original_snapshot


def test_cli_audits_rebound_and_foreign_original_context(stored):
    store, path, _, _, _ = stored
    matching, rebound, original = locality_report(stored, summary=True)
    audit = build_audit(path, store, [matching.snapshot_id])
    refs = {ref["snapshot_id"]: ref["sha256"] for ref in audit["audit"]["input_snapshots"]}
    assert refs[rebound.snapshot_id] == refs[original.snapshot_id] == rebound.content_hash
    assert audit["channels"]["candidate_retrieval"]["missed_supported"] == 1


@pytest.mark.parametrize("change", [
    {"original": False}, {"source": "osm_nominatim"}, {"area": "foreign-city"},
    {"digest": False}, {"time": False},
])
def test_cli_rejects_invalid_locality_provenance(stored, change):
    store, path, _, _, _ = stored
    matching, _, _ = locality_report(stored, **change)
    with pytest.raises((ValueError, OSError)):
        build_audit(path, store, [matching.snapshot_id])


def test_cli_rejects_missing_summary_context(stored):
    store, path, _, registry, poi = stored
    r = report()
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id,
             address_locality_context={"snapshot_ids": ["missing"]})
    matching = store.save_snapshot("heritage_matching", "area", "urn:test:locality:summary",
                                   json.dumps(r).encode(), "test-v4", "application/json")
    with pytest.raises((ValueError, OSError)):
        build_audit(path, store, [matching.snapshot_id])
    output = path.parent / "rejected-audit.json"
    assert main(["--benchmark", str(path), "--store", str(store.root),
                 "--matching-snapshot-id", matching.snapshot_id, "--output", str(output)]) == 2
    assert not output.exists()


def test_cli_includes_summary_context_without_pair_evidence(stored):
    store, path, _, registry, poi = stored
    _, rebound, _ = locality_report(stored)
    r = report()
    r.update(area_id="area", official_registry_snapshot_id=registry.snapshot_id,
             osm_poi_snapshot_id=poi.snapshot_id,
             address_locality_context={"snapshot_ids": [rebound.snapshot_id]})
    matching = store.save_snapshot("heritage_matching", "area", "urn:test:summary-only",
                                   json.dumps(r).encode(), "test-v4", "application/json")
    refs = build_audit(path, store, [matching.snapshot_id])["audit"]["input_snapshots"]
    assert {ref["snapshot_id"] for ref in refs} >= {rebound.snapshot_id}


@pytest.mark.parametrize("kind", ["registry", "poi"])
def test_cli_rejects_nondict_source_entries(stored, kind):
    store, path, _, registry, poi = stored

    def save(source, document):
        return store.save_snapshot(source, "area", "urn:test:invalid:" + source,
                                   json.dumps(document).encode(), "test-v1", "application/json")

    if kind == "registry":
        registry = save("mkrf_egrkn", {"area_id": "area", "records": [None]})
    else:
        poi = save("geofabrik_poi", {"type": "FeatureCollection", "features": [None]})
        store.save_run(RunManifest("run2", "area", "test", "a" * 64, datetime.now(timezone.utc),
                                   (poi.snapshot_id,), (), "succeeded"))
    b = json.loads(path.read_text(encoding="utf-8"))
    b["cases"][0].update(source_run_id="run2" if kind == "poi" else "run1",
                          official_registry_snapshot_id=registry.snapshot_id,
                          osm_poi_snapshot_id=poi.snapshot_id)
    path.write_text(json.dumps(b), encoding="utf-8")
    r = report()
    r.update(source_run_id=b["cases"][0]["source_run_id"], area_id="area",
             official_registry_snapshot_id=registry.snapshot_id, osm_poi_snapshot_id=poi.snapshot_id)
    matching = save("heritage_matching", r)
    with pytest.raises(ValueError, match="invalid_registry_records|invalid_poi_features"):
        build_audit(path, store, [matching.snapshot_id])
