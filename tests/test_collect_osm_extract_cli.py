import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

from scripts.collect_osm_extract import main
from src.data_models.study_area import StudyArea
from src.storage import SnapshotStore


def test_cli_export_offline_and_modified_file(tmp_path, capsys):
    study_area = StudyArea("sample", {"type": "Polygon", "coordinates": [
        [[30, 60], [30.01, 60], [30.01, 60.01], [30, 60.01], [30, 60]]]})
    store = SnapshotStore(tmp_path)
    boundary_snapshot = store.save_snapshot("osm_nominatim", study_area.area_id, "https://example.org/boundary",
                                            b"boundary", "v1", "application/json", "complete")
    boundary = SimpleNamespace(study_area=study_area, snapshot=boundary_snapshot)
    pbf = tmp_path / "extract.pbf"
    pbf.write_bytes(b"small valid adapter-verified test PBF")
    adapter = SimpleNamespace(collect=lambda _: (tmp_path / "extract.pbf", {
        "url": "source", "resolved_url": "source", "sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(),
        "size": pbf.stat().st_size, "received_at": "2026-01-01T00:00:00+00:00"}, True,
        (60, 30, 60.01, 30.01), {"sha256": "1" * 64}))

    def parse(pbf, area, directory):
        assert pbf != tmp_path / "extract.pbf"
        assert pbf.read_bytes() == (tmp_path / "extract.pbf").read_bytes()
        paths = {role: directory / (role + ".features") for role in ("poi", "network")}
        for path in paths.values():
            path.write_text('{"type":"Feature","geometry":null,"properties":{}}\n')
        return paths, {"poi": 1, "network": 1}, None

    def run():
        result = main(["sample", "--store", str(tmp_path), "--offline"],
                      boundary_adapter=SimpleNamespace(collect=lambda _: boundary),
                      extract_adapter=adapter, parser=parse)
        return result, json.loads(capsys.readouterr().out)

    code, output = run()
    assert code == 0 and output["status"] == "partial"
    path = output["exports"]["poi"]["file"]
    assert json.loads(Path(path).read_text())["provenance"]["source_snapshot_id"] == output["source_snapshot_id"]
    assert json.loads(Path(path).read_text())["provenance"]["run_manifest_id"] is None
    manifest = store.get_run(output["run_manifest_id"])
    assert manifest.status == "failed"  # Partial inventory does not enable a framework map.
    assert {boundary_snapshot.snapshot_id, output["source_snapshot_id"],
             *(item["snapshot_id"] for item in output["exports"].values())} <= set(manifest.snapshot_ids)
    raw = store.get_snapshot(output["source_snapshot_id"])
    assert raw.retrieved_at.isoformat() == "2026-01-01T00:00:00+00:00"
    assert raw.completeness == "partial"
    assessment_id = next(identifier for identifier in manifest.snapshot_ids
                         if store.get_snapshot(identifier).source_id == "input_assessment")
    configuration = json.loads(store.read_content(assessment_id))["configuration"]
    assert configuration["extract_manifest"]["resolved_url"] == "source"
    assert configuration["extract_manifest"]["sha256"] == raw.content_hash
    assert not output["input_assessment"]["input_readiness"]["framework_map"]["available"]
    network_decision = next(item for item in output["input_assessment"]["datasets"]
                            if item["dataset_kind"] == "network")
    assert "unknown_coverage" in network_decision["reasons"]
    assert run()[1]["exports"] == output["exports"]
    Path(path).write_text("edited")
    store.verify_content(output["exports"]["poi"]["snapshot_id"])
    code, output = run()
    assert code == 2 and output["error"] == "modified_export"


def test_cli_rejects_mismatched_pbf_without_run(tmp_path, capsys):
    area = StudyArea("sample", {"type": "Polygon", "coordinates": [
        [[30, 60], [30.01, 60], [30.01, 60.01], [30, 60.01], [30, 60]]]})
    store = SnapshotStore(tmp_path)
    boundary = store.save_snapshot("osm_nominatim", area.area_id, "https://example.org", b"boundary",
                                   "v1", "application/json", "complete")
    pbf = tmp_path / "extract.pbf"
    pbf.write_bytes(b"wrong")
    code = main(["sample", "--store", str(tmp_path), "--offline"],
                boundary_adapter=SimpleNamespace(collect=lambda _: SimpleNamespace(study_area=area, snapshot=boundary)),
                extract_adapter=SimpleNamespace(collect=lambda _: (pbf, {
                    "url": "source", "resolved_url": "source", "sha256": "0" * 64,
                    "size": 5, "received_at": "2026-01-01T00:00:00+00:00"}, True,
                    (60, 30, 60.01, 30.01), {"sha256": "1" * 64})))
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not list((tmp_path / "runs").glob("*.json"))


def test_parser_failure_keeps_raw_snapshot_without_publishing_run(tmp_path, capsys):
    area = StudyArea("sample", {"type": "Polygon", "coordinates": [
        [[30, 60], [30.01, 60], [30.01, 60.01], [30, 60.01], [30, 60]]]})
    store = SnapshotStore(tmp_path)
    boundary = store.save_snapshot("osm_nominatim", area.area_id, "https://example.org", b"boundary",
                                   "v1", "application/json", "complete")
    pbf = tmp_path / "extract.pbf"
    pbf.write_bytes(b"invalid PBF")

    def fail_parse(*args):
        raise RuntimeError("invalid PBF format")

    code = main(["sample", "--store", str(tmp_path), "--offline"],
                boundary_adapter=SimpleNamespace(collect=lambda _: SimpleNamespace(study_area=area, snapshot=boundary)),
                extract_adapter=SimpleNamespace(collect=lambda _: (pbf, {
                    "url": "source", "resolved_url": "source", "sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(),
                    "size": pbf.stat().st_size, "received_at": "2026-01-01T00:00:00+00:00"}, True,
                    (60, 30, 60.01, 30.01), {"sha256": "1" * 64})), parser=fail_parse)
    assert code == 2 and json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not list((tmp_path / "runs").glob("*.json"))
    assert not list(tmp_path.rglob(".pending-*"))
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2
