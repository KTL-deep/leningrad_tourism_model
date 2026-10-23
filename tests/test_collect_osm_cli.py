"""Offline CLI integration using explicitly synthetic boundaries/OSM records."""

from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace

from scripts import collect_osm
from src.data_models import StudyArea
from src.etl.adapters.overpass_inventory import OverpassError
from src.etl.osm_parsing import parse_overpass
from src.storage import SnapshotStore


GEOMETRY = {"type": "Polygon", "coordinates": [[[30, 59], [30.01, 59], [30.01, 59.01], [30, 59.01], [30, 59]]]}
NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def raw_inventory():
    return json.dumps({"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-10-19T00:00:00Z"}, "elements": [
        {"type": "node", "id": 1, "lat": 59.001, "lon": 30.001, "tags": {"amenity": "cafe", "capacity": "500"}},
        {"type": "node", "id": 2, "lat": 59.002, "lon": 30.003},
        {"type": "way", "id": 3, "nodes": [1, 2], "tags": {"highway": "footway", "oneway": "-1"}},
        {"type": "relation", "id": 4, "members": [], "tags": {"historic": "castle"}},
        {"type": "count", "id": 0, "tags": {"nodes": "2", "ways": "1", "relations": "1", "total": "4"}},
    ]}).encode()


class SyntheticBoundary:
    def __init__(self, store):
        self.store = store

    def collect(self, query):
        area = StudyArea(query, GEOMETRY)
        snapshot = self.store.save_snapshot("osm_nominatim", area.area_id, "https://example.org/synthetic-boundary",
                                             b'{"synthetic":true}', "synthetic-v1", "application/json", "complete", NOW)
        return SimpleNamespace(study_area=area, snapshot=snapshot, observed_at=NOW, from_cache=True)


class SyntheticInventory:
    def __init__(self, store):
        self.store = store

    def collect(self, area):
        raw = raw_inventory()
        snapshot = self.store.save_snapshot("osm_overpass_inventory", area.area_id, "https://example.org/synthetic-osm",
                                             raw, "synthetic-v1", "application/json", "complete", NOW)
        return SimpleNamespace(snapshot=snapshot, inventory=parse_overpass(raw, area),
                               query_bbox=(59, 30, 59.01, 30.01), from_cache=True,
                               observed_at=NOW, attribution="Synthetic test, not empirical data")


def run_cli(tmp_path):
    store = SnapshotStore(tmp_path)
    code = collect_osm.main(["Synthetic Region", "--store", str(tmp_path)],
                            boundary_adapter=SyntheticBoundary(store), inventory_adapter=SyntheticInventory(store))
    return code, store


def test_exports_are_provenanced_and_cannot_enable_routing_or_deficit(tmp_path, capsys):
    code, store = run_cli(tmp_path)
    report = json.loads(capsys.readouterr().out)
    assert code == 0 and report["inventory_status"] == "succeeded"
    assert report["analysis_performed"] is False and report["routing_ready"] is False
    assert report["counts"]["poi"] == 2 and report["counts"]["network"] == 1
    inputs = report["input_assessment"]["input_readiness"]
    assert inputs["framework_map"]["available"]
    assert not inputs["network_accessibility"]["available"]
    assert not inputs["quantified_deficit"]["available"]
    manifest = store.get_run(report["input_assessment"]["manifest"]["run_id"])
    for role, artifact in report["exports"].items():
        assert artifact["snapshot_id"] in manifest.snapshot_ids
        saved = store.get_snapshot(artifact["snapshot_id"])
        layer = json.loads(store.read_content(saved.snapshot_id))
        assert layer["provenance"]["source_snapshot_id"] == report["inventory_snapshot"]["snapshot_id"]
        assert layer["provenance"]["recommendations"] is False
        assert not os.path.samefile(artifact["file"], store.root / "blobs" / f"{saved.content_hash}.bin")
    poi_layer = json.loads(store.read_content(report["exports"]["poi"]["snapshot_id"]))
    assert poi_layer["features"][0]["properties"]["capacity"] is None


def test_edited_export_does_not_corrupt_store_and_is_not_overwritten(tmp_path, capsys):
    _, store = run_cli(tmp_path)
    report = json.loads(capsys.readouterr().out)
    export = report["exports"]["poi"]
    from pathlib import Path
    Path(export["file"]).write_bytes(b"user-edited")
    assert json.loads(store.read_content(export["snapshot_id"]))["type"] == "FeatureCollection"
    code, _ = run_cli(tmp_path)
    failure = json.loads(capsys.readouterr().out)
    assert code == 2 and failure["status"] == "failed"
    assert Path(export["file"]).read_bytes() == b"user-edited"


def test_source_outage_is_partial_not_zero_inventory(tmp_path, capsys):
    class UnavailableInventory:
        def collect(self, area):
            raise OverpassError("http_error", {"http_status": 500})

    store = SnapshotStore(tmp_path)
    code = collect_osm.main(["Synthetic Region", "--store", str(tmp_path)],
                            boundary_adapter=SyntheticBoundary(store), inventory_adapter=UnavailableInventory())
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["status"] == "partial"
    assert report["boundary_status"] == "succeeded" and report["inventory_status"] == "failed"
    assert "counts" not in report  # Failure is NOT an empirical zero count.
    assert report["inventory_error"]["details"]["http_status"] == 500
    assert not report["input_assessment"]["input_readiness"]["framework_map"]["available"]
    assert store.get_run(report["input_assessment"]["manifest"]["run_id"])
