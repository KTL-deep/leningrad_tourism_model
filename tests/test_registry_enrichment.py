"""Offline integration checks for enriching immutable territory collections."""

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.enrich_territory import main
from src.data_models import RunManifest
from src.data_models.study_area import StudyArea
from src.etl.adapters.mkrf_registry import MkrfError
from src.etl.adapters.mkrf_registry import SOURCE_ID as HERITAGE_SOURCE_ID
from src.etl.adapters.lenobl_oopt import SOURCE_ID as OOPT_SOURCE_ID
from src.pipeline.registry_enrichment import enrich_territory_run
from src.storage import SnapshotStore


def save(store, source, area, document):
    return store.save_snapshot(source, area.area_id, "urn:test:" + source,
                               json.dumps(document).encode(), "test-v1", "application/json", "partial")


@pytest.fixture
def base(tmp_path):
    store = SnapshotStore(tmp_path / "store")
    area = StudyArea("Gatchina", {"type": "Polygon", "coordinates": [
        [[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]]},
        jurisdictions=("Ленинградская область",))
    raw = save(store, "geofabrik_extract", area, {"raw": True})
    poi = save(store, "geofabrik_poi", area, {"type": "FeatureCollection", "features": [],
                                                "provenance": {"source_snapshot_id": raw.snapshot_id}})
    config = save(store, "territory_collection_config", area, {
        "stage": "territory_collection", "study_area": area.to_dict(),
        "exports": {"poi": {"snapshot_id": poi.snapshot_id}}})
    run = RunManifest("parent", area.area_id, "test-v1", hashlib.sha256(b"config").hexdigest(),
                      datetime.now(timezone.utc), (raw.snapshot_id, poi.snapshot_id, config.snapshot_id),
                      (), "partial")
    store.save_run(run)
    return store, area, run, raw, poi, config


def adapters(store, area, *, heritage=None, oopt=None):
    def heritage_result():
        passport = save(store, HERITAGE_SOURCE_ID, area, {"passport": True})
        schema = save(store, HERITAGE_SOURCE_ID, area, {"schema": True})
        archive = save(store, HERITAGE_SOURCE_ID, area, {"archive": True})
        registry = {"area_id": area.area_id, "quality": {"retained_records": 0},
                    "source_versions": {"archive_url": archive.url}, "coverage": "partial",
                    "records": []}
        derived = save(store, HERITAGE_SOURCE_ID, area, registry)
        return SimpleNamespace(passport_snapshot=passport, schema_snapshot=schema,
                               archive_snapshot=archive, registry_snapshot=derived, registry=registry,
                               checked_at=datetime.now(timezone.utc), from_cache=True)

    def oopt_result():
        raw = save(store, OOPT_SOURCE_ID, area, {"raw": True})
        catalog = {"area_id": area.area_id, "source_coverage": "regional", "items": []}
        derived = save(store, OOPT_SOURCE_ID, area, catalog)
        return SimpleNamespace(raw_snapshot=raw, catalog_snapshot=derived, catalog=catalog,
                               checked_at=datetime.now(timezone.utc), from_cache=False)

    class Adapter:
        def __init__(self, behavior):
            self.behavior = behavior

        def collect(self, given_area):
            assert given_area == area
            if isinstance(self.behavior, Exception):
                raise self.behavior
            return self.behavior()

    return Adapter(heritage if heritage is not None else heritage_result), Adapter(oopt if oopt is not None else oopt_result)


def check_manifest(store, base, result):
    _, area, parent, raw, poi, config = base
    child = store.get_run(result["run_manifest_id"])
    assert child.area_id == area.area_id and child.status == result["status"]
    assert set(parent.snapshot_ids) <= set(child.snapshot_ids)
    assert store.get_run(parent.run_id) == parent
    for snapshot_id in child.snapshot_ids:
        snapshot = store.verify_content(snapshot_id)
        assert snapshot.area_id == area.area_id
        assert hashlib.sha256(store.read_content(snapshot_id)).hexdigest() == snapshot.content_hash
    configuration = store.verify_content(result["configuration_snapshot_id"])
    assert configuration.snapshot_id in child.snapshot_ids
    assert child.config_hash == configuration.content_hash
    assert json.loads(store.read_content(configuration.snapshot_id))["source_run_id"] == parent.run_id
    assert result["osm_source_received_at"] == raw.retrieved_at.isoformat()


def test_both_sources_and_matching(base):
    store, area, *_ = base
    heritage, oopt = adapters(store, area)
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["status"] == "partial"
    assert all(source["status"] == "collected_partial" for source in result["sources"].values())
    assert result["heritage_matching"]["status"] == "advisory_only"
    assert store.verify_content(result["heritage_matching"]["snapshot_id"]).content_hash == result["heritage_matching"]["sha256"]
    check_manifest(store, base, result)


@pytest.mark.parametrize("failed", ["heritage", "protected_areas"])
def test_one_source_fails_without_losing_other(base, failed):
    store, area, *_ = base
    heritage, oopt = adapters(store, area, **{("oopt" if failed == "protected_areas" else "heritage"): RuntimeError("offline")})
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["status"] == "partial"
    assert result["sources"][failed]["status"] == "unavailable"
    assert result["sources"][failed]["snapshot_ids"] == []
    other = "heritage" if failed == "protected_areas" else "protected_areas"
    assert result["sources"][other]["status"] == "collected_partial"
    assert set(result["sources"][other]["snapshot_ids"]) <= set(store.get_run(result["run_manifest_id"]).snapshot_ids)
    check_manifest(store, base, result)


def test_both_sources_unavailable_still_records_child(base):
    store, area, *_ = base
    heritage, oopt = adapters(store, area, heritage=RuntimeError("offline"), oopt=RuntimeError("offline"))
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["status"] == "failed"
    assert all(source["status"] == "unavailable" for source in result["sources"].values())
    assert result["heritage_matching"]["status"] == "unavailable"
    check_manifest(store, base, result)


@pytest.mark.parametrize("failed", ["heritage", "protected_areas"])
def test_malformed_derived_document_is_source_local(base, failed):
    store, area, *_ = base
    heritage, oopt = adapters(store, area)
    adapter = heritage if failed == "heritage" else oopt
    original = adapter.behavior

    def malformed():
        result = original()
        document = dict(result.registry if failed == "heritage" else result.catalog)
        document.pop("quality" if failed == "heritage" else "items")
        derived = save(store, HERITAGE_SOURCE_ID if failed == "heritage" else OOPT_SOURCE_ID, area, document)
        if failed == "heritage":
            result.registry_snapshot, result.registry = derived, document
        else:
            result.catalog_snapshot, result.catalog = derived, document
        return result

    adapter.behavior = malformed
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["sources"][failed]["status"] == "unavailable"
    assert result["sources"]["heritage" if failed == "protected_areas" else "protected_areas"]["status"] == "collected_partial"
    check_manifest(store, base, result)


def test_matching_failure_retains_sources(base, monkeypatch):
    store, area, *_ = base
    heritage, oopt = adapters(store, area)

    def fail(*args):
        raise ValueError("matching broken")

    monkeypatch.setattr("src.pipeline.registry_enrichment.match_heritage", fail)
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["heritage_matching"] == {"status": "unavailable", "reason": "matching_error", "error_type": "ValueError"}
    assert all(source["status"] == "collected_partial" for source in result["sources"].values())
    check_manifest(store, base, result)


@pytest.mark.parametrize("failed", ["heritage", "protected_areas"])
def test_unrelated_source_snapshot_rejected(base, failed):
    store, area, *_ = base
    heritage, oopt = adapters(store, area)
    adapter = heritage if failed == "heritage" else oopt
    original = adapter.behavior

    def unrelated():
        result = original()
        if failed == "heritage":
            result.passport_snapshot = save(store, "unrelated", area, {"passport": True})
        else:
            result.raw_snapshot = save(store, "unrelated", area, {"raw": True})
        return result

    adapter.behavior = unrelated
    result = enrich_territory_run(store, "parent", heritage_adapter=heritage, oopt_adapter=oopt)
    assert result["sources"][failed]["status"] == "unavailable"
    other = "heritage" if failed == "protected_areas" else "protected_areas"
    assert result["sources"][other]["status"] == "collected_partial"
    check_manifest(store, base, result)


@pytest.mark.parametrize("mutation", ["failed_run", "missing_poi", "foreign_poi", "foreign_raw", "missing_raw", "malformed_config"])
def test_invalid_parent_provenance_rejected(base, mutation):
    store, area, parent, raw, poi, config = base
    ids = list(parent.snapshot_ids)
    if mutation in ("failed_run", "missing_poi"):
        if mutation == "missing_poi":
            ids.remove(poi.snapshot_id)
    elif mutation in ("foreign_poi", "foreign_raw", "missing_raw"):
        foreign = StudyArea("Other", {"type": "Polygon", "coordinates": [
            [[32, 59], [33, 59], [33, 60], [32, 60], [32, 59]]]})
        if mutation == "foreign_poi":
            bad = save(store, "geofabrik_poi", foreign, {})
            document = {"stage": "territory_collection", "study_area": area.to_dict(),
                        "exports": {"poi": {"snapshot_id": bad.snapshot_id}}}
        else:
            source_id = save(store, "geofabrik_extract", foreign, {}).snapshot_id if mutation == "foreign_raw" else "missing"
            bad = save(store, "geofabrik_poi", area, {"type": "FeatureCollection", "features": [],
                                                       "provenance": {"source_snapshot_id": source_id}})
            document = {"stage": "territory_collection", "study_area": area.to_dict(),
                        "exports": {"poi": {"snapshot_id": bad.snapshot_id}}}
            ids.append(bad.snapshot_id)
        config = save(store, "territory_collection_config", area, document)
        ids[ids.index(parent.snapshot_ids[-1])] = config.snapshot_id
        ids.remove(poi.snapshot_id)
    else:
        config = save(store, "territory_collection_config", area, {"stage": "territory_collection"})
        ids[-1] = config.snapshot_id
    modified = RunManifest("invalidparent", area.area_id, "test-v1", parent.config_hash,
                           parent.started_at, tuple(ids), (), "failed" if mutation == "failed_run" else "partial")
    store.save_run(modified)
    heritage, oopt = adapters(store, area)
    with pytest.raises(ValueError):
        enrich_territory_run(store, modified.run_id, heritage_adapter=heritage, oopt_adapter=oopt)


def test_offline_cli_journal_and_missing_run(base, capsys):
    store, area, *_ = base
    heritage, oopt = adapters(store, area)
    args = ["--run-id", "parent", "--store", str(store.root), "--offline"]
    assert main(args, heritage_adapter=heritage, oopt_adapter=oopt) == 0
    result = json.loads(capsys.readouterr().out)
    check_manifest(store, base, result)
    assert json.loads(next((store.root / "logs").glob("registry-enrichment-*.json")).read_text())["status"] == "partial"
    assert main(["--run-id", "missing", "--store", str(store.root), "--offline"],
                heritage_adapter=heritage, oopt_adapter=oopt) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed" and failed["error_type"] == "FileNotFoundError"
    journal = store.root / "logs" / f"registry-enrichment-{failed['attempt_id']}.json"
    assert json.loads(journal.read_text()) == failed


def test_invalid_archive_preserves_oopt_and_journal(base, capsys):
    store, area, *_ = base
    heritage, oopt = adapters(store, area, heritage=MkrfError("invalid_archive"))
    assert main(["--run-id", "parent", "--store", str(store.root), "--offline"],
                heritage_adapter=heritage, oopt_adapter=oopt) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial"
    assert result["sources"]["heritage"] == {"status": "unavailable", "reason": "invalid_archive",
                                               "error_type": "MkrfError", "snapshot_ids": []}
    assert result["sources"]["protected_areas"]["status"] == "collected_partial"
    assert result["heritage_matching"] == {"status": "unavailable", "reason": "heritage_registry_unavailable"}
    child = store.get_run(result["run_manifest_id"])
    assert set(result["sources"]["protected_areas"]["snapshot_ids"]) <= set(child.snapshot_ids)
    assert json.loads((store.root / "logs" / f"registry-enrichment-{result['attempt_id']}.json").read_text()) == result
    check_manifest(store, base, result)
