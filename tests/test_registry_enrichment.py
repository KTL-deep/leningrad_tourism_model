"""Offline integration checks for enriching immutable territory collections."""

from datetime import datetime, timedelta, timezone
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
from src.etl.heritage_locality import PARSER_VERSION as LOCALITY_VERSION, SOURCE_ID as LOCALITY_SOURCE
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


def locality_case(base, *, count=1, point=True, address="г. Гатчина, улица Соборная", located=False):
    store, area, parent, raw, _, _ = base
    features = [{"type": "Feature", "id": f"node/{i+1}",
                 "geometry": {"type": "Point", "coordinates": [30.1, 59.1]} if point else None,
                 "properties": {"osm_type": "node", "osm_id": i+1, "tags": {"name": f"Дом {i}"}}}
                for i in range(count)]
    poi = save(store, "geofabrik_poi", area, {"type": "FeatureCollection", "features": features,
                                               "provenance": {"source_snapshot_id": raw.snapshot_id}})
    config = save(store, "territory_collection_config", area, {
        "stage": "territory_collection", "study_area": area.to_dict(),
        "exports": {"poi": {"snapshot_id": poi.snapshot_id}}})
    run = RunManifest("localityparent", area.area_id, parent.code_version, parent.config_hash,
                      parent.started_at, (raw.snapshot_id, poi.snapshot_id, config.snapshot_id), (), "partial")
    store.save_run(run)
    heritage, oopt = adapters(store, area)
    original = heritage.behavior

    def collect():
        result = original()
        registry = dict(result.registry)
        registry["records"] = [{"source_record_id": f"official:{i}", "reg_number": None,
                                "name": f"Дом {i}", "address": address,
                                "region_name": "Ленинградская область", "spatial_status": "unlocated",
                                "geometry": {"type": "Point", "coordinates": [30.1, 59.1]} if located else None}
                               for i in range(count)]
        result.registry = registry
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, registry)
        return result

    heritage.behavior = collect
    return store, area, run, heritage, oopt


def fake_resolution(store, area, locality, region, *, defect=None):
    foreign = StudyArea("Gatchina locality", {"type": "Polygon", "coordinates": [
        [[30, 59], [30.5, 59], [30.5, 59.5], [30, 59.5], [30, 59]]]})
    raw = json.dumps([{"osm_type": "relation", "osm_id": 1, "name": locality,
                       "category": "boundary", "type": "administrative", "address": {
                           "country_code": "ru", "state": region, "city": locality},
                       "geojson": foreign.request_geometry}]).encode()
    original = store.save_snapshot("osm_nominatim", foreign.area_id, "https://nominatim.openstreetmap.org/search",
                                   raw, "other" if defect == "original_parser" else "nominatim-boundary-v1",
                                   "text/plain" if defect == "original_media" else "application/json",
                                   "partial" if defect == "original_completeness" else "complete",
                                   retrieved_at=(datetime.now(timezone.utc) + timedelta(hours=1)
                                                 if defect == "future_retrieval" else None))
    rebound = store.save_snapshot("other" if defect == "source" else LOCALITY_SOURCE,
                                  foreign.area_id if defect == "area" else area.area_id, original.url,
                                   raw, LOCALITY_VERSION,
                                   "text/plain" if defect == "rebound_media" else "application/json",
                                   "complete" if defect == "rebound_completeness" else "partial",
                                  retrieved_at=original.retrieved_at)
    context = {"context_version": 1, "status": "source_reported", "source_snapshot_id": rebound.snapshot_id,
               "original_snapshot_id": original.snapshot_id,
               "raw_sha256": "0" * 64 if defect == "hash" else rebound.content_hash,
               "locality_name": locality, "region_name": region,
               "query": f"{locality}, {region}, Россия", "bbox": [30, 59, 30.5, 59.5],
               "guard_m": 5000, "interpretation": "locality_envelope_not_heritage_geometry_or_legal_boundary"}
    checked_at = datetime.now(timezone.utc)
    if defect == "future_checked":
        checked_at += timedelta(hours=1)
    elif defect == "naive_checked":
        checked_at = checked_at.replace(tzinfo=None)
    elif defect == "early_checked":
        checked_at = original.retrieved_at - timedelta(seconds=1)
    return SimpleNamespace(context=context, snapshot=rebound, from_cache=True,
                           checked_at=checked_at), original


def test_locality_context_rebound_and_deduplicated(base):
    store, area, run, heritage, oopt = locality_case(base, count=2)
    calls = []

    class Resolver:
        def resolve(self, given_area, locality, region):
            calls.append((given_area, locality, region))
            resolution, original = fake_resolution(store, area, locality, region)
            self.original = original
            return resolution

    resolver = Resolver()
    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=resolver)
    summary = result["address_locality_context"]
    assert len(calls) == 1 and summary["status"] == "collected_context"
    rebound = summary["snapshot_ids"][0]
    child = store.get_run(result["run_manifest_id"])
    assert rebound in child.snapshot_ids and resolver.original.snapshot_id not in child.snapshot_ids
    assert store.get_snapshot(rebound).retrieved_at == resolver.original.retrieved_at
    report = json.loads(store.read_content(result["heritage_matching"]["snapshot_id"]))
    assert report["address_locality_context"] == stable_locality_summary(summary)
    assert len(report["review_candidates"]) == 2
    assert all(c["evidence"]["official_address_locality_context"]["source_snapshot_id"] == rebound
               for c in report["review_candidates"])
    assert store.get_run(run.run_id) == run


def stable_locality_summary(summary):
    return {**summary, "queries": [
        {key: value for key, value in query.items() if key not in ("from_cache", "checked_at")}
        for query in summary["queries"]]}


def test_matching_artifact_ignores_runtime_cache_observation(base):
    store, area, run, heritage, oopt = locality_case(base)
    observations = []

    class Resolver:
        def resolve(self, given_area, locality, region):
            resolution, original = fake_resolution(store, area, locality, region)
            self.original = original
            resolution.from_cache = bool(observations)
            resolution.checked_at = datetime.now(timezone.utc)
            observations.append((resolution.from_cache, resolution.checked_at))
            return resolution

    resolver = Resolver()
    first = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                 oopt_adapter=oopt, locality_resolver=resolver)
    second = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=resolver)
    assert first["heritage_matching"]["snapshot_id"] == second["heritage_matching"]["snapshot_id"]
    assert first["configuration_snapshot_id"] != second["configuration_snapshot_id"]
    assert first["address_locality_context"]["queries"][0]["from_cache"] is False
    assert second["address_locality_context"]["queries"][0]["from_cache"] is True
    assert first["address_locality_context"]["queries"][0]["checked_at"] != (
        second["address_locality_context"]["queries"][0]["checked_at"])
    report = json.loads(store.read_content(first["heritage_matching"]["snapshot_id"]))
    assert report["address_locality_context"] == stable_locality_summary(first["address_locality_context"])
    assert all("checked_at" not in query and "from_cache" not in query
               for query in report["address_locality_context"]["queries"])
    assert first["heritage_matching"]["address_locality_context"] == first["address_locality_context"]
    assert second["heritage_matching"]["address_locality_context"] == second["address_locality_context"]
    assert store.get_run(run.run_id) == run
    assert resolver.original.snapshot_id not in store.get_run(first["run_manifest_id"]).snapshot_ids
    for result in (first, second):
        saved = json.loads(store.read_content(result["configuration_snapshot_id"]))
        assert saved["address_locality_context"] == result["address_locality_context"]
        assert store.verify_content(result["heritage_matching"]["snapshot_id"]).content_hash == (
            result["heritage_matching"]["sha256"])


def test_locality_case_variants_share_one_budget_slot(base):
    store, area, run, heritage, oopt = locality_case(base, count=2)
    original = heritage.behavior

    def varied():
        result = original()
        document = dict(result.registry)
        document["records"] = [dict(record) for record in document["records"]]
        document["records"][1]["address"] = "г. гатчина, улица Соборная"
        document["records"][1]["region_name"] = "ленинградская область"
        result.registry = document
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, document)
        return result

    heritage.behavior = varied
    calls = []

    class Resolver:
        def resolve(self, given_area, locality, region):
            calls.append((locality, region))
            return fake_resolution(store, area, locality, region)[0]

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver(), max_locality_lookups=1)
    summary = result["address_locality_context"]
    assert len(calls) == 1 and len(summary["queries"]) == 1
    assert summary["status"] == "collected_context"
    assert summary["queries"][0]["source_record_ids"] == ["official:0", "official:1"]


@pytest.mark.parametrize("defect", ["area", "hash", "source", "offline", "rebound_media",
                                     "rebound_completeness", "original_parser", "original_media",
                                     "original_completeness", "early_checked", "future_checked",
                                     "naive_checked", "future_retrieval"])
def test_locality_failure_isolated(base, defect):
    store, area, run, heritage, oopt = locality_case(base)

    class Resolver:
        def resolve(self, given_area, locality, region):
            if defect == "offline":
                error = ValueError("cache_miss")
                error.code = "cache_miss"
                raise error
            return fake_resolution(store, area, locality, region, defect=defect)[0]

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver())
    assert result["sources"]["heritage"]["status"] == "collected_partial"
    assert result["address_locality_context"]["status"] == "partial"
    assert result["address_locality_context"]["queries"][0]["reason"] == (
        "cache_miss" if defect == "offline" else "locality_error")
    assert not result["address_locality_context"]["snapshot_ids"]
    report = json.loads(store.read_content(result["heritage_matching"]["snapshot_id"]))
    assert len(report["review_candidates"]) == 1


@pytest.mark.parametrize("change", [{"located": True}, {"point": False}])
def test_no_locality_lookup_without_eligible_candidate(base, change):
    store, area, run, heritage, oopt = locality_case(base, **change)

    class Resolver:
        def resolve(self, *args):
            pytest.fail("unexpected lookup")

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver())
    assert result["address_locality_context"]["status"] == "not_needed"


def test_unsupported_address_recorded_once_without_lookup(base):
    store, area, run, heritage, oopt = locality_case(base, count=2, address="улица Соборная")
    original = heritage.behavior

    def duplicate_candidate():
        result = original()
        result.registry["records"][1]["name"] = result.registry["records"][0]["name"]
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, result.registry)
        return result

    heritage.behavior = duplicate_candidate

    class Resolver:
        def resolve(self, *args):
            pytest.fail("unsupported address must not cause lookup")

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver())
    summary = result["address_locality_context"]
    assert summary["status"] == "partial" and summary["queries"] == []
    assert summary["unresolved_records"] == [
        {"source_record_id": f"official:{i}", "reason": "unsupported_or_ambiguous_address_locality"}
        for i in range(2)]
    report = json.loads(store.read_content(result["heritage_matching"]["snapshot_id"]))
    assert len(report["review_candidates"]) == 2 and report["address_locality_context"] == summary


def test_collected_query_and_unresolved_address_stays_partial(base):
    store, area, run, heritage, oopt = locality_case(base, count=2)
    original = heritage.behavior

    def mixed():
        result = original()
        result.registry["records"][1]["address"] = "улица Соборная"
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, result.registry)
        return result

    heritage.behavior = mixed

    class Resolver:
        def resolve(self, given_area, locality, region):
            return fake_resolution(store, area, locality, region)[0]

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver())
    summary = result["address_locality_context"]
    assert summary["status"] == "partial"
    assert len(summary["snapshot_ids"]) == 1
    assert summary["unresolved_records"] == [
        {"source_record_id": "official:1", "reason": "unsupported_or_ambiguous_address_locality"}]


def test_locality_budget_and_provider_denial(base):
    store, area, run, heritage, oopt = locality_case(base, count=3)
    original = heritage.behavior

    def distinct():
        result = original()
        for i, record in enumerate(result.registry["records"]):
            record["address"] = f"г. Место {'АБВ'[i]}, улица Соборная"
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, result.registry)
        return result

    heritage.behavior = distinct
    calls = []

    class Resolver:
        def resolve(self, *args):
            calls.append(args)
            error = ValueError("rate_limited")
            error.code = "rate_limited"
            raise error

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage, oopt_adapter=oopt,
                                  locality_resolver=Resolver(), max_locality_lookups=2)
    assert len(calls) == 1
    assert [q["reason"] for q in result["address_locality_context"]["queries"]] == [
        "rate_limited", "provider_unavailable", "provider_unavailable"]
    for bad in (-1, 9, True, 1.0):
        with pytest.raises(ValueError, match="invalid_locality_lookup_budget"):
            enrich_territory_run(store, run.run_id, heritage_adapter=heritage,
                                 oopt_adapter=oopt, max_locality_lookups=bad)


def test_zero_budget_never_calls_resolver(base):
    store, area, run, heritage, oopt = locality_case(base)

    class Resolver:
        def resolve(self, *args):
            pytest.fail("budget must prevent lookup")

    result = enrich_territory_run(store, run.run_id, heritage_adapter=heritage, oopt_adapter=oopt,
                                  locality_resolver=Resolver(), max_locality_lookups=0)
    assert result["address_locality_context"]["queries"][0]["reason"] == "budget_exhausted"


def test_locality_conflict_excludes_name_candidate_without_removing_identifier(base):
    store, area, run, heritage, oopt = locality_case(base, count=2)
    original = heritage.behavior

    def identified():
        result = original()
        result.registry["records"][1]["reg_number"] = "123456789012345"
        result.registry_snapshot = save(store, HERITAGE_SOURCE_ID, area, result.registry)
        return result

    heritage.behavior = identified
    parent_poi = next(sid for sid in run.snapshot_ids if store.get_snapshot(sid).source_id == "geofabrik_poi")
    features = json.loads(store.read_content(parent_poi))["features"]
    features[1]["properties"]["tags"]["ref:egrokn"] = "123456789012345"
    poi = save(store, "geofabrik_poi", area, {"type": "FeatureCollection", "features": features,
                                               "provenance": {"source_snapshot_id": base[3].snapshot_id}})
    config = save(store, "territory_collection_config", area, {
        "stage": "territory_collection", "study_area": area.to_dict(),
        "exports": {"poi": {"snapshot_id": poi.snapshot_id}}})
    revised = RunManifest("identifiedparent", area.area_id, run.code_version, run.config_hash,
                          run.started_at, (base[3].snapshot_id, poi.snapshot_id, config.snapshot_id), (), "partial")
    store.save_run(revised)

    class Resolver:
        def resolve(self, given_area, locality, region):
            resolution, _ = fake_resolution(store, area, locality, region)
            resolution.context["bbox"] = [32, 60, 32.1, 60.1]
            return resolution

    result = enrich_territory_run(store, revised.run_id, heritage_adapter=heritage,
                                  oopt_adapter=oopt, locality_resolver=Resolver())
    report = json.loads(store.read_content(result["heritage_matching"]["snapshot_id"]))
    assert len(report["identifier_links"]) == 1
    assert len(report["excluded_review_candidates"]) == 1
    assert report["excluded_review_candidates"][0]["reason"] == "unlocated_address_locality_conflict"
