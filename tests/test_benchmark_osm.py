import hashlib
import json
import sys
import types

import pytest

from scripts import benchmark_osm
from scripts import _benchmark_worker
from src.data_models.study_area import StudyArea


def polygon():
    return {"type": "Polygon", "coordinates": [[[30, 60], [30.01, 60],
            [30.01, 60.01], [30, 60.01], [30, 60]]]}


def feature(identifier, lon=30.001):
    return {"type": "Feature", "id": identifier,
            "geometry": {"type": "Point", "coordinates": [lon, 60.001]},
            "properties": {"tags": {"name": "museum"}}}


def test_feature_summary_order_independent_and_content_sensitive(tmp_path):
    path = tmp_path / "features"
    first, second = feature("node/1"), feature("node/2")
    path.write_text("\n".join(map(json.dumps, (first, second))) + "\n", encoding="utf-8")
    original = benchmark_osm.summarize_features(path)
    path.write_text("\n".join(map(json.dumps, (second, first))) + "\n", encoding="utf-8")
    assert benchmark_osm.summarize_features(path) == original
    second["properties"]["tags"]["name"] = "park"
    path.write_text("\n".join(map(json.dumps, (second, first))) + "\n", encoding="utf-8")
    assert benchmark_osm.summarize_features(path)["sha256"] != original["sha256"]


@pytest.mark.parametrize("lines", [
    [feature("node/1"), feature("node/1")],
    ['{"type":"Feature","id":"node/1","id":"node/2","geometry":{"type":"Point","coordinates":[30,60]}}'],
    ['{"type":"Feature","id":"node/1","geometry":{"type":"Point","coordinates":[NaN,60]}}'],
    [feature("node/1", float("inf"))],
])
def test_feature_summary_rejects_duplicates_and_nonfinite(tmp_path, lines):
    path = tmp_path / "features"
    path.write_text("\n".join(json.dumps(line) if isinstance(line, dict) else line for line in lines),
                    encoding="utf-8")
    with pytest.raises(ValueError):
        benchmark_osm.summarize_features(path)


def test_offline_fresh_process_and_exclusive_report(tmp_path):
    osmium = pytest.importorskip("osmium")
    pytest.importorskip("psutil")
    original = tmp_path / "fixture.pbf"
    with osmium.SimpleWriter(str(original)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30.001, 60.001),
                                                tags={"tourism": "museum"}))
    cached = tmp_path / ("fixture.osm.pbf." + benchmark_osm.sha256_file(original))
    original.rename(cached)
    area = tmp_path / "area.json"
    area.write_text(json.dumps(StudyArea("fixture", polygon()).to_dict()), encoding="utf-8")
    report = tmp_path / "report.json"
    arguments = ["--pbf", str(cached), "--area", str(area), "--output", str(report),
                 "--work-dir", str(tmp_path / "artifacts"), "--repetitions", "2"]
    assert benchmark_osm.main(arguments) == 0
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["source"]["sha256"] == hashlib.sha256(cached.read_bytes()).hexdigest()
    assert len(result["runs"]) == 2 and result["comparisons"][0]["matches_reference"]
    assert result["runs"][0]["counts"]["poi"] == 1
    assert result["runs"][0]["layers"]["poi"]["features"][0][0] == "node/1"
    assert result["runs"][0]["artifact_dir"] != result["runs"][1]["artifact_dir"]
    with pytest.raises(SystemExit):
        benchmark_osm.main(arguments)

    native_report = tmp_path / "native.json"
    assert benchmark_osm.main(["--pbf", str(cached), "--area", str(area), "--output", str(native_report),
                               "--mode", "native", "--compare-report", str(report)]) == 0
    comparison = json.loads(native_report.read_text(encoding="utf-8"))["comparisons"][0]
    assert comparison["matches_reference"]
    assert comparison["feature_mismatches"]["poi"]["changed_ids"] == []


def test_native_opt_in_and_rust_bridge_api(tmp_path, monkeypatch):
    import src.etl.osm_extract_parsing as parsing

    seen = []

    def fake_parser(*args, **kwargs):
        seen.append(kwargs)
        return {}, {}, None

    monkeypatch.setattr(parsing, "extract_layers", fake_parser)
    _benchmark_worker.baseline("pbf", "area", tmp_path)
    _benchmark_worker.native("pbf", "area", tmp_path)
    assert seen == [{}, {"native_tag_filter": True}]

    bridge = types.ModuleType("benchmarks.osm_rust_bridge")

    def fake_rust(*args, **kwargs):
        seen.append(kwargs)
        return {}, {}, None

    bridge.extract_layers_rust = fake_rust
    monkeypatch.setitem(sys.modules, "benchmarks.osm_rust_bridge", bridge)
    _benchmark_worker.rust("pbf", "area", tmp_path, tmp_path / "rust")
    assert seen[-1] == {"executable": tmp_path / "rust"}


def test_compare_detects_same_count_different_semantics():
    first = {"layers": {"poi": {"features": [["node/1", "a"]]},
                        "network": {"features": []}},
             "counts": {"poi": 1}, "source_timestamp": None}
    second = {"layers": {"poi": {"features": [["node/1", "b"]]},
                         "network": {"features": []}},
              "counts": {"poi": 1}, "source_timestamp": None}
    result = benchmark_osm.compare_runs(first, second)
    assert not result["matches_reference"]
    assert result["feature_mismatches"]["poi"]["changed_ids"] == ["node/1"]
