"""Exercise the production relation contract through the real PBF parser and store."""

import hashlib
import json
from types import SimpleNamespace

import osmium

from scripts.collect_osm_extract import main
from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


def test_cli_multipolygon_contract_and_explicit_legacy_mode(tmp_path, capsys):
    area = StudyArea("sample", {"type": "Polygon", "coordinates": [
        [[30, 60], [30.01, 60], [30.01, 60.01], [30, 60.01], [30, 60]]]})
    store = SnapshotStore(tmp_path / "store")
    boundary = store.save_snapshot("osm_nominatim", area.area_id, "https://example.org/boundary",
                                   b"boundary", "v1", "application/json", "complete")
    pbf = tmp_path / "fixture.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        for identifier, coordinate in enumerate([
            (29.9, 59.9), (30.1, 59.9), (30.1, 60.1), (29.9, 60.1),
        ], start=1):
            writer.add_node(osmium.osm.mutable.Node(id=identifier, location=osmium.osm.Location(*coordinate)))
        writer.add_node(osmium.osm.mutable.Node(
            id=5, location=osmium.osm.Location(30.005, 60.005), tags={"tourism": "museum"}))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2, 3]))
        writer.add_way(osmium.osm.mutable.Way(id=11, nodes=[3, 4, 1]))
        writer.add_relation(osmium.osm.mutable.Relation(
            id=20, members=[("w", 10, "outer"), ("w", 11, "outer")],
            tags={"type": "multipolygon", "leisure": "park"}))
    record = {"url": "https://example.org/fixture.pbf", "resolved_url": "https://example.org/fixture.pbf",
              "sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(), "size": pbf.stat().st_size,
              "received_at": "2026-10-23T00:00:00+00:00"}

    def run(*extra):
        code = main(["sample", "--store", str(store.root), "--offline", *extra],
                    boundary_adapter=SimpleNamespace(collect=lambda _: SimpleNamespace(study_area=area, snapshot=boundary)),
                    extract_adapter=SimpleNamespace(collect=lambda _: (
                        pbf, record, True, (60, 30, 60.01, 30.01), {"sha256": "1" * 64})))
        output = json.loads(capsys.readouterr().out)
        assert code == 0, output
        return output

    current = run()
    assert current["status"] == "partial" and not current["analysis_performed"]
    assert current["counts"]["poi"] == 2
    poi_snapshot_id = current["exports"]["poi"]["snapshot_id"]
    poi = json.loads(store.read_content(poi_snapshot_id))
    assert {feature["id"] for feature in poi["features"]} == {"node/5", "relation/20"}
    assert poi["provenance"]["algorithm_version"] == "geofabrik-node-way-multipolygon-observations-v2"
    assert poi["provenance"]["source_snapshot_id"] == current["source_snapshot_id"]
    assert poi["provenance"]["relation_quality"]["counts"]["relation_poi"] == 1
    assert poi["provenance"]["relation_quality"]["counts"]["relation_failure_scope"] == "source_extract"
    assert poi["provenance"]["relation_quality"]["deduplication_status"] == "not_performed"
    manifest = store.get_run(current["run_manifest_id"])
    assert manifest.status == "failed"  # Observations alone do not enable a complete map.
    assert {poi_snapshot_id, current["source_snapshot_id"]} <= set(manifest.snapshot_ids)
    assert not current["input_assessment"]["input_readiness"]["framework_map"]["available"]
    assert run()["exports"] == current["exports"]

    legacy = run("--node-way-only")
    assert legacy["counts"]["poi"] == 1
    raw = store.get_snapshot(current["source_snapshot_id"])
    legacy_raw = store.get_snapshot(legacy["source_snapshot_id"])
    assert raw.snapshot_id != legacy_raw.snapshot_id
    assert raw.content_hash == legacy_raw.content_hash
    assert legacy_raw.parser_version == "geofabrik-node-way-observations-v1"
    assert store.get_snapshot(poi_snapshot_id).completeness == "partial"
