"""Offline views use immutable store provenance, not exported filesystem paths."""

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.data_models.contracts import RunManifest
from src.presentation.results import list_runs, load_run_view
from src.storage.snapshots import SnapshotStore


NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _save(store, source, content, *, area="area"):
    return store.save_snapshot(source, area, "urn:test:synthetic", json.dumps(content).encode(),
                               "test-v1", "application/json", "partial", retrieved_at=NOW)


def _fixture(tmp_path, *, enriched=False, tamper=None, matching_changes=None, poi_features=None):
    store = SnapshotStore(tmp_path)
    poi = _save(store, "geofabrik_poi", {"type": "FeatureCollection",
                                        "features": [] if poi_features is None else poi_features})
    network = _save(store, "geofabrik_network", {"type": "FeatureCollection", "features": []})
    exports = {key: {"snapshot_id": snapshot.snapshot_id, "file": "does-not-exist.geojson"}
               for key, snapshot in (("poi", poi), ("network", network))}
    if tamper in ("foreign", "not-in-run"):
        substitute = _save(store, "geofabrik_poi", {"type": "FeatureCollection", "features": [1]},
                           area="other" if tamper == "foreign" else "area")
        exports["poi"]["snapshot_id"] = substitute.snapshot_id
    collection = {"stage": "territory_collection", "query": "Synthetic query",
                  "study_area": {"area_id": "area", "name": "Synthetic"}, "exports": exports}
    cfg = _save(store, "territory_collection_config", collection)
    ids = [poi.snapshot_id, network.snapshot_id, cfg.snapshot_id]
    enrichment = None
    if enriched:
        parent_content = store.read_content(cfg.snapshot_id)
        store.save_run(RunManifest("parent", "area", "test", hashlib.sha256(parent_content).hexdigest(),
                                   NOW, tuple(ids), (), "partial"))
        report = {"artifact_kind": "advisory_heritage_matching", "area_id": "area",
                  "source_run_id": "parent", "osm_poi_snapshot_id": poi.snapshot_id,
                  "identifier_links": [], "review_candidates": [], "excluded_review_candidates": []}
        if matching_changes is not None:
            matching_changes(report)
        if tamper == "matching_poi":
            report["osm_poi_snapshot_id"] = network.snapshot_id
        match = _save(store, "heritage_matching", report)
        ids.append(match.snapshot_id)
        enrichment = {"stage": "official_registry_enrichment", "source_run_id": "parent",
                      "area_id": "area", "heritage_matching": {"snapshot_id": match.snapshot_id}}
        cfg = _save(store, "registry_enrichment_config", enrichment)
        ids.append(cfg.snapshot_id)
    content = store.read_content(cfg.snapshot_id)
    run = RunManifest("child" if enriched else "parent", "area", "test", hashlib.sha256(content).hexdigest(),
                      NOW, tuple(ids), (), "partial")
    store.save_run(run)
    return store, run, collection, enrichment, poi


@pytest.mark.parametrize("enriched", [False, True])
def test_load_and_list_partial_runs(tmp_path, enriched):
    store, run, collection, enrichment, poi = _fixture(tmp_path, enriched=enriched)
    result = load_run_view(tmp_path, run.run_id)
    assert result["run"] == run.to_dict()
    assert result["collection"] == collection
    assert result["enrichment"] == enrichment
    assert result["poi"]["features"] == []
    assert result["network"]["features"] == []
    assert (result["matching"] is not None) == enriched
    assert any(s["content_hash"] == poi.content_hash and s["retrieved_at"] for s in result["sources"])
    listing = list_runs(tmp_path)
    assert listing["errors"] == []
    selected = next(item for item in listing["runs"] if item["run_id"] == run.run_id)
    assert selected["status"] == "partial"
    assert selected["stage"] == ("official_registry_enrichment" if enriched else "territory_collection")


@pytest.mark.parametrize("tamper", ["foreign", "not-in-run", "matching_poi"])
def test_rejects_invalid_export_or_matching_reference(tmp_path, tamper):
    _, run, *_ = _fixture(tmp_path, enriched=tamper == "matching_poi", tamper=tamper)
    with pytest.raises(ValueError):
        load_run_view(tmp_path, run.run_id)


def test_corrupt_and_missing_blob_fail_load_but_listing_remains_metadata_only(tmp_path):
    store, run, _, _, poi = _fixture(tmp_path)
    blob = store.root / "blobs" / (poi.content_hash + ".bin")
    blob.write_bytes(b"corrupt")
    assert len(list_runs(tmp_path)["runs"]) == 1
    with pytest.raises(ValueError):
        load_run_view(tmp_path, run.run_id)
    blob.unlink()
    with pytest.raises(OSError):
        load_run_view(tmp_path, run.run_id)


def test_listing_empty_bad_manifest_and_unsupported_run(tmp_path):
    assert list_runs(tmp_path) == {"runs": [], "errors": []}
    store, run, *_ = _fixture(tmp_path)
    (store.root / "runs" / "bad.json").write_text("{bad", encoding="utf-8")
    unsupported = RunManifest("other", "area", "test", "0" * 64, NOW, (), (), "partial")
    store.save_run(unsupported)
    listing = list_runs(tmp_path)
    assert [item["run_id"] for item in listing["runs"]] == [run.run_id]
    assert len(listing["errors"]) == 1


def test_listing_reports_corrupt_config_without_hashing_large_nonconfig_blobs(tmp_path):
    store, run, *_ = _fixture(tmp_path)
    cfg = next(store.get_snapshot(sid) for sid in run.snapshot_ids
               if store.get_snapshot(sid).source_id == "territory_collection_config")
    (store.root / "blobs" / (cfg.content_hash + ".bin")).write_bytes(b"broken")
    listing = list_runs(tmp_path)
    assert listing["runs"] == []
    assert len(listing["errors"]) == 1


def test_rejects_invalid_geojson_features(tmp_path):
    store = SnapshotStore(tmp_path)
    poi = _save(store, "geofabrik_poi", {"type": "FeatureCollection", "features": [42]})
    network = _save(store, "geofabrik_network", {"type": "FeatureCollection", "features": []})
    cfg = _save(store, "territory_collection_config", {
        "stage": "territory_collection", "query": "query", "study_area": {"area_id": "area", "name": "Area"},
        "exports": {"poi": {"snapshot_id": poi.snapshot_id}, "network": {"snapshot_id": network.snapshot_id}}})
    run = RunManifest("test", "area", "test", hashlib.sha256(store.read_content(cfg.snapshot_id)).hexdigest(),
                      NOW, (poi.snapshot_id, network.snapshot_id, cfg.snapshot_id), (), "partial")
    store.save_run(run)
    with pytest.raises(ValueError, match="features"):
        load_run_view(tmp_path, run.run_id)


@pytest.mark.parametrize("change", [
    lambda report: report.pop("identifier_links"),
    lambda report: report.update(review_candidates={}),
    lambda report: report.update(excluded_review_candidates=[{"evidence": "bad"}]),
    lambda report: report.update(identifier_links=[{"evidence": {"warnings": [42]}}]),
    lambda report: report.update(review_candidates=[{"evidence": {}, "warnings": "bad"}]),
    lambda report: report.update(warnings=[False]),
])
def test_hash_consistent_malformed_matching_rejected(tmp_path, change):
    _, run, *_ = _fixture(tmp_path, enriched=True, matching_changes=change)
    with pytest.raises(ValueError, match="matching"):
        load_run_view(tmp_path, run.run_id)


def test_valid_matching_entries_and_feature_properties(tmp_path):
    feature = {"type": "Feature", "id": "node/1", "geometry": None,
               "properties": {"tags": {"tourism": "museum"}, "categories": ["heritage"]}}
    def matching_entries(report):
        report["identifier_links"] = [{"evidence": {"warnings": ["advisory"]}}]
        report["review_candidates"] = [{"evidence": {}}]
        report["excluded_review_candidates"] = [{"evidence": {}}]
        report["warnings"] = ["advisory"]

    _, run, *_ = _fixture(tmp_path, enriched=True, matching_changes=matching_entries,
                          poi_features=[feature])
    view = load_run_view(tmp_path, run.run_id)
    assert view["poi"]["features"] == [feature]
    assert view["matching"]["identifier_links"][0]["evidence"]["warnings"] == ["advisory"]


@pytest.mark.parametrize("features", [
    [{"type": "Feature", "properties": {}, "geometry": None}],
    [{"type": "Feature", "id": " ", "properties": {}, "geometry": None}],
    [{"type": "Feature", "id": "same", "properties": {}, "geometry": None}] * 2,
    [{"type": "Feature", "id": "one", "properties": {"tags": []}, "geometry": None}],
    [{"type": "Feature", "id": "one", "properties": {"categories": [1]}, "geometry": None}],
    [{"type": "Feature", "id": "one", "properties": {"categories": "wrong"}, "geometry": None}],
])
def test_hash_consistent_malformed_feature_rejected(tmp_path, features):
    _, run, *_ = _fixture(tmp_path, poi_features=features)
    with pytest.raises(ValueError, match="feature"):
        load_run_view(tmp_path, run.run_id)


@pytest.mark.parametrize("key", ["snapshot_ids", "evidence_ids", "limitations"])
def test_manifest_string_instead_of_array_reported(tmp_path, key):
    store, run, *_ = _fixture(tmp_path)
    path = store.root / "runs" / (run.run_id + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record[key] = "wrong"
    path.write_text(json.dumps(record), encoding="utf-8")
    listing = list_runs(tmp_path)
    assert listing["runs"] == []
    assert len(listing["errors"]) == 1
