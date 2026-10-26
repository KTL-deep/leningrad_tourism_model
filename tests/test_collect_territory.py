import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import osmium
import pytest

from scripts.collect_territory import main
from src.etl.adapters.nominatim_boundary import ADAPTER_VERSION, BoundaryError, _select
from src.etl.adapters.official_statistics import DiscoveryResult, SOURCE_ID as STATS_SOURCE
from src.etl.source_policy import DatasetCandidate
from src.storage import SnapshotStore


QUERY = "Sample, Leningrad region"


def _fixture(tmp_path):
    store = SnapshotStore(tmp_path / "analysis")
    polygon = {"type": "Polygon", "coordinates": [[[30, 60], [30.002, 60],
                [30.002, 60.002], [30, 60.002], [30, 60]]]}
    raw = json.dumps([{"category": "boundary", "type": "administrative", "osm_type": "relation",
                       "osm_id": 123, "display_name": QUERY, "address": {"state": "Leningrad region"},
                       "geojson": polygon}]).encode()
    area = _select(raw, QUERY)
    boundary = store.save_snapshot("osm_nominatim", area.area_id,
                                   "https://nominatim.openstreetmap.org/search", raw,
                                   ADAPTER_VERSION, "application/json", "complete")
    geocoder = SimpleNamespace(collect=lambda _: SimpleNamespace(study_area=area, snapshot=boundary,
                               from_cache=True, observed_at=datetime.now(timezone.utc), attribution="OSM"))
    pbf = tmp_path / "observations.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        for identifier, lon, lat, tags in [(1, 30.001, 60.001, {"tourism": "museum"}),
                                            (2, 30.018, 60.001, {"tourism": "museum"}),
                                            (3, 30.0005, 60.0005, {}),
                                            (4, 30.0015, 60.0005, {}),
                                            (5, 30.0015, 60.0015, {}),
                                            (6, 30.0005, 60.0015, {})]:
            writer.add_node(osmium.osm.mutable.Node(id=identifier,
                            location=osmium.osm.Location(lon, lat), tags=tags))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[3, 4, 5, 6, 3]))
        writer.add_relation(osmium.osm.mutable.Relation(
            id=11, tags={"type": "multipolygon", "leisure": "park"},
            members=[("w", 10, "outer")]))
    manifest = {"url": "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf",
                "resolved_url": "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf",
                "sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(), "size": pbf.stat().st_size,
                "received_at": "2026-01-01T00:00:00+00:00"}
    extract = SimpleNamespace(collect=lambda _: (pbf, manifest, True,
                              (59.99, 29.99, 60.02, 30.03), {"sha256": "a" * 64}))
    return store, geocoder, extract, pbf


def _run(tmp_path, capsys, geocoder, extract, stats, *flags):
    code = main([QUERY, "--store", str(tmp_path / "analysis"), "--offline", *flags],
                boundary_adapter=geocoder, extract_adapter=extract, statistics_adapter=stats)
    return code, json.loads(capsys.readouterr().out)


def test_scoped_collection_exclusion_is_not_zero_or_ready(tmp_path, capsys):
    store, geocoder, extract, pbf = _fixture(tmp_path)
    stats = SimpleNamespace(discover=lambda _: DiscoveryResult("excluded", "cache_missing", (), (), ()))
    first_code, first = _run(tmp_path, capsys, geocoder, extract, stats)
    assert first_code == 0 and first["status"] == "partial"
    assert first["collection_status"] == "succeeded" and first["required_spatial_inputs_collected"]
    assert not first["analysis_performed"] and first["recommendations"] == []
    assert first["statistics_discovery"]["reason"] == "cache_missing"
    assert first["statistics_discovery"]["statistics"] == []
    assert first["input_assessment"]["manifest"]["status"] == "failed"
    assert not first["input_assessment"]["input_readiness"]["framework_map"]["available"]
    assert store.get_run(first["run_manifest_id"]).status == "partial"
    assert first["run_manifest_id"] != first["input_assessment"]["manifest"]["run_id"]
    poi = json.loads(Path(first["exports"]["poi"]["file"]).read_text(encoding="utf-8"))
    assert {"node/1", "node/2", "relation/11"} <= {item["id"] for item in poi["features"]}
    assert poi["provenance"]["source_snapshot_id"] == first["source_snapshot_id"]
    assert store.get_snapshot(first["scope_snapshot_id"]).area_id == first["area_id"]
    assert store.get_snapshot(first["boundary_snapshot_id"]).area_id == first["area_id"]
    ids = store.get_run(first["run_manifest_id"]).snapshot_ids
    assert {first["scope_snapshot_id"], first["source_snapshot_id"],
            first["statistics_discovery"]["discovery_snapshot_id"],
            first["poi_duplicate_audit"]["snapshot_id"]} <= set(ids)
    assert "territory_collection_config" in {store.get_snapshot(s).source_id for s in ids}
    assert pbf.read_bytes() and not (tmp_path / "analysis" / "observations.pbf").exists()
    second_code, second = _run(tmp_path, capsys, geocoder, extract, stats)
    assert second_code == 0 and second["exports"] == first["exports"]
    assert second["attempt_id"] != first["attempt_id"]
    assert len(list((tmp_path / "analysis" / "logs").glob("collection-*.json"))) == 2


def test_included_statistics_are_bound_but_poi_remains_partial(tmp_path, capsys):
    store, geocoder, extract, _ = _fixture(tmp_path)

    def discover(area):
        raw = b"municipality,jurisdiction,period,metric,unit,value\nSample,region,2024,visits,persons,7\n"
        snapshot = store.save_snapshot(STATS_SOURCE, area.area_id,
                                       "https://78.rosstat.gov.ru/data.csv", raw,
                                       "official-statistics-discovery-v1", "text/csv", "complete")
        row = {"municipality": area.name, "jurisdiction": "Leningrad region",
               "period": 2024, "metric": "visits", "unit": "persons", "value": 7,
               "attribution": "Петростат", "license": "CC BY 4.0",
               "source_url": snapshot.url, "snapshot_id": snapshot.snapshot_id, "area_id": area.area_id}
        candidate = DatasetCandidate(STATS_SOURCE, "official_statistics", area.area_id,
                                     discovered_automatically=True, retrieved=True,
                                     content_validated=True, provenance_verified=True,
                                     coverage_verified=True, period_verified=True,
                                     units_verified=True, access_permitted=True,
                                     completeness="complete", snapshot_id=snapshot.snapshot_id)
        return DiscoveryResult("included", None, (row,), (snapshot.snapshot_id,), (), candidate=candidate)

    code, result = _run(tmp_path, capsys, geocoder, extract, SimpleNamespace(discover=discover))
    assert code == 0 and result["statistics_discovery"]["statistics"][0]["value"] == 7
    assert any(item["dataset_kind"] == "official_statistics" and item["included"]
               for item in result["input_assessment"]["datasets"])
    assert not any(item["dataset_kind"] == "poi" and item["included"]
                   for item in result["input_assessment"]["datasets"])
    assert result["statistics_discovery"]["snapshot_ids"][0] in store.get_run(result["run_manifest_id"]).snapshot_ids


def test_required_failure_persists_attempt_without_fake_run(tmp_path, capsys):
    store, _, extract, _ = _fixture(tmp_path)

    def fail(_):
        raise BoundaryError("cache_missing")

    code, result = _run(tmp_path, capsys, SimpleNamespace(collect=fail), extract, None)
    assert code == 2 and result["error"] == "cache_missing"
    assert not result["required_spatial_inputs_collected"]
    assert "counts" not in result and "recommendations" not in result
    assert not list((store.root / "runs").glob("*.json"))
    journal = json.loads((store.root / "logs" / f"collection-{result['attempt_id']}.json").read_text())
    assert journal["status"] == "failed" and journal["result"]["error"] == "cache_missing"


def test_optional_adapter_error_is_explicit(tmp_path, capsys):
    _, geocoder, extract, _ = _fixture(tmp_path)

    def broken(_):
        raise RuntimeError("broken adapter")

    code, result = _run(tmp_path, capsys, geocoder, extract, SimpleNamespace(discover=broken))
    assert code == 0 and result["statistics_discovery"]["reason"] == "adapter_error"
    assert result["statistics_discovery"]["error_type"] == "RuntimeError"


def test_extract_failure_keeps_attempt_without_success_counts(tmp_path, capsys):
    store, geocoder, _, _ = _fixture(tmp_path)

    def fail(_):
        raise RuntimeError("extract unavailable")

    code, result = _run(tmp_path, capsys, geocoder, SimpleNamespace(collect=fail), None)
    assert code == 2 and result["status"] == "failed" and "counts" not in result
    assert result["details"] == "extract unavailable"
    assert not list((store.root / "runs").glob("*.json"))
    assert json.loads((store.root / "logs" / f"collection-{result['attempt_id']}.json").read_text())["status"] == "failed"


def test_offline_default_adapters_never_issue_http(tmp_path, capsys):
    class NoNetwork:
        def get(self, *args, **kwargs):
            raise AssertionError("HTTP must be disabled")

    code = main([QUERY, "--offline", "--store", str(tmp_path / "analysis")], session=NoNetwork())
    result = json.loads(capsys.readouterr().out)
    assert code == 2 and result["error"] == "cache_miss"
    assert not list((tmp_path / "analysis" / "runs").glob("*.json"))


@pytest.mark.parametrize("flaw", ["foreign", "corrupt", "partial", "invalid_status",
                                  "nan", "contradiction", "ineligible", "wrong_source"])
def test_invalid_optional_metadata_isolated_from_spatial_run(tmp_path, capsys, flaw):
    store, geocoder, extract, _ = _fixture(tmp_path)

    def discover(area):
        sid_area = "other_area" if flaw == "foreign" else area.area_id
        source = "other_source" if flaw == "wrong_source" else STATS_SOURCE
        snapshot = store.save_snapshot(source, sid_area, "https://78.rosstat.gov.ru/data.csv",
                                       b"source", "official-statistics-discovery-v1", "text/csv",
                                       "partial" if flaw == "partial" else "complete")
        if flaw == "corrupt":
            (store.root / "blobs" / f"{snapshot.content_hash}.bin").write_bytes(b"tampered")
        candidate = DatasetCandidate(STATS_SOURCE, "official_statistics", area.area_id,
                                     discovered_automatically=True, retrieved=True,
                                     content_validated=True, provenance_verified=True,
                                     coverage_verified=True, period_verified=True,
                                     units_verified=True, access_permitted=flaw != "ineligible",
                                     completeness="complete", snapshot_id=snapshot.snapshot_id)
        row = {"area_id": area.area_id, "snapshot_id": snapshot.snapshot_id,
               "source_url": snapshot.url, "municipality": area.name,
               "jurisdiction": "Leningrad region", "period": 2024,
               "value": float("nan") if flaw == "nan" else 7, "metric": "visits",
               "unit": "persons", "attribution": "Петростат", "license": "CC BY 4.0"}
        return DiscoveryResult("excluded" if flaw == "contradiction" else
                               "nonsense" if flaw == "invalid_status" else "included",
                               "unavailable" if flaw == "contradiction" else None,
                               (row,), (snapshot.snapshot_id,), (), candidate=candidate)

    code, result = _run(tmp_path, capsys, geocoder, extract, SimpleNamespace(discover=discover))
    assert code == 0 and result["collection_status"] == "succeeded"
    assert result["required_spatial_inputs_collected"] and result["status"] == "partial"
    excluded = result["statistics_discovery"]
    assert excluded["status"] == "excluded" and excluded["reason"] == "invalid_discovery_result"
    assert excluded["error_type"] in ("ValueError", "TypeError", "FileNotFoundError")
    assert excluded["snapshot_ids"] == excluded["statistics"] == []
    assert not any(item["dataset_kind"] == "official_statistics" and item["included"]
                   for item in result["input_assessment"]["datasets"])
    run = store.get_run(result["run_manifest_id"])
    assert run.status == "partial"
    assert {store.get_snapshot(sid).source_id for sid in run.snapshot_ids}.isdisjoint(
        {"other_source"})
    assert json.loads(store.read_content(excluded["discovery_snapshot_id"]))["snapshot_ids"] == []
    assert json.loads((store.root / "logs" / f"collection-{result['attempt_id']}.json").read_text())["status"] == "succeeded"
