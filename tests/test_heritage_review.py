"""Blind packet selection, provenance and exclusive publication."""

import copy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from scripts.prepare_heritage_review import main
from src.data_models import RunManifest
from src.etl.heritage_review import digest, prepare_review
from src.etl.heritage_validation import REVIEW_BASIS, validate_heritage_benchmark
from src.storage import SnapshotStore


def fixtures():
    records = [{"source_record_id": f"r{i}", "name": f"official {i}"} for i in range(12)]
    features = [{"id": f"node/{i}", "properties": {"name": f"osm {i}"}} for i in range(12)]
    def entry(r, p, status):
        return {"source_record_id": f"r{r}", "poi_id": f"node/{p}", "status": status}
    report = {"artifact_kind": "advisory_heritage_matching", "schema_version": 1,
              "algorithm_version": "v4", "source_run_id": "run1", "area_id": "area",
              "official_registry_snapshot_id": "registry", "osm_poi_snapshot_id": "poi",
              "identifier_links": [entry(i, i, "identifier_link") for i in range(4)],
              "review_candidates": [entry(i, i, "name_only_unlocated_candidate") for i in range(4, 8)] +
                                   [entry(2, 9, "name_only_unlocated_candidate")],
              "excluded_review_candidates": [{**entry(i, i, "excluded_name_candidate"),
                                              "reason": "osm_non_object_subject_role",
                                              "evidence": {"role": "information"}} for i in range(8, 12)]}
    bundle = {"matching_snapshot_id": "matching", "matching_sha256": "a" * 64,
              "report": report, "registry": {"area_id": "area", "records": records},
              "poi": {"type": "FeatureCollection", "features": features}}
    benchmark = {"schema_version": 1, "cases": [
        {"source_record_id": "r0", "poi_id": "node/1"}]}
    return bundle, benchmark


def test_deterministic_disjoint_blind_and_quota():
    bundle, benchmark = fixtures()
    original = copy.deepcopy((bundle, benchmark))
    result = prepare_review([bundle], benchmark, seed="fixed", per_stratum=3)
    assert (bundle, benchmark) == original
    shuffled = copy.deepcopy(bundle)
    for channel in ("identifier_links", "review_candidates", "excluded_review_candidates"):
        shuffled["report"][channel].reverse()
    shuffled["registry"]["records"].reverse()
    shuffled["poi"]["features"].reverse()
    assert result == prepare_review([shuffled], benchmark, seed="fixed", per_stratum=3)
    cases = result["blind_packet"]["cases"]
    assert len(cases) <= 9
    assert len({c["source_record_id"] for c in cases}) == len(cases)
    assert len({c["poi_id"] for c in cases}) == len(cases)
    assert all(c["source_record_id"] != "r0" and c["poi_id"] != "node/1" for c in cases)
    assert result["manifest"]["blind_packet_sha256"] == digest(result["blind_packet"])
    assert all(set(c) == {"case_id", "source_run_id", "source_record_id", "poi_id",
                              "official_registry_snapshot_id", "osm_poi_snapshot_id",
                              "official_record", "osm_feature"} for c in cases)
    assert [c["case_id"] for c in cases] == [f"H{i:02d}" for i in range(1, len(cases) + 1)]
    assert all(c["label"] is None and c["rationale"] == "" and c["evidence"] == []
               for c in result["benchmark_template"]["cases"])
    with pytest.raises(ValueError):
        validate_heritage_benchmark(result["benchmark_template"])


def test_zero_stratum_and_global_endpoint_exclusion():
    bundle, benchmark = fixtures()
    benchmark["cases"].extend({"source_record_id": f"r{i}", "poi_id": f"node/{i}"}
                               for i in range(8, 12))
    result = prepare_review([bundle], benchmark, seed="x", per_stratum=4)
    stats = {s["channel"]: s for s in result["manifest"]["strata"]}
    assert stats["excluded_review_candidates"] == {"source_run_id": "run1",
        "channel": "excluded_review_candidates", "eligible": 0, "selected": 0, "shortfall": 4}
    assert stats["identifier_links"]["eligible"] == 2
    assert stats["identifier_links"]["shortfall"] == 2


def test_reordered_bundles_identical_and_global_sample_endpoints():
    first, benchmark = fixtures()
    second = copy.deepcopy(first)
    second["report"]["source_run_id"] = "run2"
    second["matching_snapshot_id"] = "matching2"
    result = prepare_review([first, second], benchmark, seed="fixed", per_stratum=2)
    assert result == prepare_review([second, first], benchmark, seed="fixed", per_stratum=2)
    cases = result["blind_packet"]["cases"]
    assert len({c["source_record_id"] for c in cases}) == len(cases)
    assert len({c["poi_id"] for c in cases}) == len(cases)
    assert len(result["manifest"]["strata"]) == 6


@pytest.mark.parametrize("change", [
    lambda b: b["report"]["review_candidates"].append(copy.deepcopy(b["report"]["identifier_links"][0])),
    lambda b: b["report"]["excluded_review_candidates"][0].update(poi_id="foreign"),
    lambda b: b["registry"]["records"].append(copy.deepcopy(b["registry"]["records"][0])),
])
def test_invalid_original_references_and_duplicate_outputs(change):
    bundle, benchmark = fixtures()
    change(bundle)
    with pytest.raises(ValueError):
        prepare_review([bundle], benchmark, seed="x")


@pytest.mark.parametrize("seed,quota", [("", 4), ("x", 0), ("x", 21), ("x", True)])
def test_invalid_selection_parameters(seed, quota):
    bundle, benchmark = fixtures()
    with pytest.raises(ValueError):
        prepare_review([bundle], benchmark, seed=seed, per_stratum=quota)


def test_cli_store_and_exclusive_output(tmp_path):
    bundle, benchmark = fixtures()
    benchmark.update(benchmark_id="development", selection_protocol="developer selected",
                     review_basis=REVIEW_BASIS)
    benchmark["cases"][0].update(case_id="C01", source_run_id="run1",
                                  official_registry_snapshot_id="registry",
                                  osm_poi_snapshot_id="poi", label="unresolved",
                                  rationale="independent review", evidence=["original sources"])
    store = SnapshotStore(tmp_path / "store")
    def save(source, document):
        return store.save_snapshot(source, "area", "urn:test:" + source,
                                   json.dumps(document).encode(), "test-v1", "application/json")
    registry = save("mkrf_egrkn", bundle["registry"])
    poi = save("geofabrik_poi", bundle["poi"])
    store.save_run(RunManifest("run1", "area", "test", "a" * 64, datetime.now(timezone.utc),
                               (poi.snapshot_id,), (), "succeeded"))
    bundle["report"].update(official_registry_snapshot_id=registry.snapshot_id,
                            osm_poi_snapshot_id=poi.snapshot_id)
    benchmark["cases"][0].update(official_registry_snapshot_id=registry.snapshot_id,
                                  osm_poi_snapshot_id=poi.snapshot_id)
    matching = save("heritage_matching", bundle["report"])
    path = tmp_path / "development.json"
    path.write_text(json.dumps(benchmark), encoding="utf-8")
    output = tmp_path / "out"
    args = ["--store", str(store.root), "--matching-snapshot-id", matching.snapshot_id,
            "--exclude-benchmark", str(path), "--output-dir", str(output)]
    assert main(args) == 0
    assert main(args) == 2
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    completion = json.loads((output / "complete.json").read_text(encoding="utf-8"))
    assert manifest["matching_snapshots"][0]["sha256"] == matching.content_hash
    for filename, sha in completion["artifact_raw_sha256"].items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == sha
    assert manifest["excluded_development_benchmark_sha256"] == digest(benchmark)
    bad = copy.deepcopy(bundle["report"])
    bad["identifier_links"][0]["source_record_id"] = "foreign"
    foreign = save("heritage_matching", bad)
    bad_args = args.copy()
    bad_args[bad_args.index(matching.snapshot_id)] = foreign.snapshot_id
    bad_args[-1] = str(tmp_path / "bad")
    assert main(bad_args) == 2
    assert not (tmp_path / "bad").exists()
