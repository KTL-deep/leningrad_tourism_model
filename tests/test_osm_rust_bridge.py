import json
import os
from pathlib import Path

import osmium
import pytest

from benchmarks.osm_rust_bridge import extract_layers_rust, read_candidates
from src.data_models.study_area import StudyArea
from src.etl.osm_extract_parsing import extract_layers


def polygon(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south],
            [east, north], [west, north], [west, south]]]}


def test_candidate_bridge_merges_tags_and_rejects_conflicts(tmp_path):
    path = tmp_path / "candidates.ndjson"
    path.write_text("\n".join(json.dumps(item) for item in [
        {"type": "way", "id": 8, "nodes": [1, 2], "coordinates": [[30, 60], [30.1, 60.1]], "tags": {"highway": "path"}},
        {"type": "node", "id": 1, "lon": 30, "lat": 60, "tags": {"tourism": "museum"}},
    ]) + "\n", encoding="utf-8")
    # Rust emits nodes first; bridge also accepts a POI encountered after a support ref.
    nodes, ways = read_candidates(path)
    assert nodes[1][1] == {"tourism": "museum"}
    assert ways[8][0] == [1, 2]
    path.write_text(path.read_text(encoding="utf-8") + json.dumps(
        {"type": "node", "id": 1, "lon": 30.2, "lat": 60, "tags": {}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate candidate node"):
        read_candidates(path)
    path.write_text('{"type":"node","id":1,"lon":30,"lat":60,"tags":{}}\n'
                    '{"type":"way","id":2,"nodes":[1],"coordinates":[[30.2,60]],"tags":{}}\n')
    with pytest.raises(ValueError, match="conflicting shared node"):
        read_candidates(path)


def test_candidate_rejects_bad_schema_and_duplicates(tmp_path):
    path = tmp_path / "candidates.ndjson"
    path.write_text('{"type":"node","id":1,"lon":30,"lat":60,"tags":{},"id":2}\n')
    with pytest.raises(ValueError, match="duplicate candidate JSON key"):
        read_candidates(path)
    path.write_text('{"type":"way","id":2,"nodes":[1],"coordinates":[[NaN,60]],"tags":{}}\n')
    with pytest.raises(ValueError, match="nonfinite JSON"):
        read_candidates(path)


def test_shared_node_coordinates_use_exact_source_lattice(tmp_path):
    path = tmp_path / "lattice.ndjson"
    path.write_text(json.dumps({"type": "node", "id": 1, "lon": 300000001 * 1e-7,
                               "lat": 60, "tags": {"tourism": "museum"}}) + "\n" + json.dumps({
        "type": "way", "id": 2, "nodes": [1], "coordinates": [[300000001 / 1e7, 60]],
        "tags": {"highway": "path"}}) + "\n", encoding="utf-8")
    nodes, _ = read_candidates(path)
    assert nodes[1][0][0] == 300000001 / 1e7


@pytest.mark.parametrize("scope", ["polygon", "hole", "multipart"])
def test_rust_matches_baseline_for_crossing_and_enclosing_ways(tmp_path, scope):
    executable = os.environ.get("OSM_RUST_PRESELECT")
    if not executable or not Path(executable).is_file():
        pytest.skip("Rust executable unavailable; set OSM_RUST_PRESELECT to built executable")
    pbf = tmp_path / "source.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-10-21T20:21:46Z")
    coords = [(29.9, 59.9), (30.1, 59.9), (30.1, 60.1), (29.9, 60.1),
              (29.98, 60), (30.02, 60), (30.0000001, 60.0000001)]
    ids = [14_000_000_001 + n for n in range(len(coords))]
    with osmium.SimpleWriter(str(pbf), header=header) as writer:
        for n, (lon, lat) in enumerate(coords):
            writer.add_node(osmium.osm.mutable.Node(id=ids[n], location=osmium.osm.Location(lon, lat),
                                                    tags={"tourism": "museum"} if n == 6 else {}))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=ids[:4] + ids[:1], tags={"leisure": "park"}))
        writer.add_way(osmium.osm.mutable.Way(id=11, nodes=ids[4:6], tags={"highway": "path"}))
        writer.add_way(osmium.osm.mutable.Way(id=12, nodes=[ids[4], ids[6]], tags={"highway": "service"}))
    geometry = polygon(29.99, 59.99, 30.01, 60.01)
    if scope == "hole":
        geometry["coordinates"].append(polygon(29.999, 59.999, 30.001, 60.001)["coordinates"][0])
    elif scope == "multipart":
        geometry = {"type": "MultiPolygon", "coordinates": [
            polygon(29.99, 59.99, 29.995, 60.01)["coordinates"],
            polygon(30.005, 59.99, 30.01, 60.01)["coordinates"],
        ]}
    area = StudyArea("inside", geometry)
    baseline, counts, timestamp = extract_layers(pbf, area, tmp_path / "baseline")
    rust, rust_counts, rust_timestamp = extract_layers_rust(pbf, area, tmp_path / "rust", executable=executable)
    assert rust_counts == counts
    assert rust_timestamp == timestamp
    for role in ("poi", "network"):
        normalized = lambda path: sorted(
            (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()), key=lambda item: item["id"])
        assert normalized(rust[role]) == normalized(baseline[role])


def test_rust_explicitly_rejects_incomplete_source_instead_of_claiming_equivalence(tmp_path):
    executable = os.environ.get("OSM_RUST_PRESELECT")
    if not executable or not Path(executable).is_file():
        pytest.skip("Rust executable unavailable; set OSM_RUST_PRESELECT to built executable")
    pbf = tmp_path / "incomplete.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30, 60),
                                              tags={"tourism": "museum"}))
        writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[1, 999], tags={"highway": "path"}))
    area = StudyArea("inside", polygon(29.99, 59.99, 30.01, 60.01))
    _, counts, _ = extract_layers(pbf, area, tmp_path / "baseline")
    assert counts["missing_way_locations"] == 1
    with pytest.raises(RuntimeError, match="missing node"):
        extract_layers_rust(pbf, area, tmp_path / "rust", executable=executable)
    assert not (tmp_path / "rust" / "network.features").exists()
