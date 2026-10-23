import json

import osmium
import pytest

from src.data_models.study_area import StudyArea
from src.etl.osm_extract_parsing import extract_layers


def polygon(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south],
            [east, north], [west, north], [west, south]]]}


def test_sparse_global_ids_and_enclosing_way(tmp_path, monkeypatch):
    indexes = []
    original = osmium.SimpleHandler.apply_file

    def track(self, *args, **kwargs):
        indexes.append(kwargs["idx"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(osmium.SimpleHandler, "apply_file", track)
    path = tmp_path / "enclosing.osm.pbf"
    ids = [14_000_000_001 + i for i in range(4)]
    coords = [(29.9, 59.9), (30.1, 59.9), (30.1, 60.1), (29.9, 60.1)]
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-10-21T20:21:46Z")
    with osmium.SimpleWriter(str(path), header=header) as writer:
        for identifier, coord in zip(ids, coords):
            writer.add_node(osmium.osm.mutable.Node(id=identifier, location=osmium.osm.Location(*coord)))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=ids + ids[:1], tags={"leisure": "park"}))
    cached = tmp_path / ("northwestern-fed-district.osm.pbf." + "a" * 64)
    path.rename(cached)
    area = StudyArea("inside park", polygon(29.99, 59.99, 30.01, 60.01))
    paths, counts, timestamp = extract_layers(cached, area, tmp_path / "features")
    assert timestamp == "2026-10-21T20:21:46Z"
    assert indexes == ["sparse_file_array"]
    assert counts["poi"] == 1 and counts["network"] == 0
    feature = json.loads(paths["poi"].read_text())
    assert feature["id"] == "way/10"
    assert feature["properties"]["location_precision"] == "approximate_way_centroid"


def test_analysis_geometry_matches_verified_collection_scope(tmp_path):
    path = tmp_path / "surrounding.osm.pbf"
    with osmium.SimpleWriter(str(path)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30.02, 60.02),
                                              tags={"tourism": "museum"}))
    area = StudyArea("small request", polygon(30, 60, 30.01, 60.01),
                     analysis_geometry=polygon(29.99, 59.99, 30.03, 60.03))
    _, counts, _ = extract_layers(path, area, tmp_path / "features")
    assert counts["poi"] == 1


def test_selected_output_limit_fails_without_truncating_success(tmp_path, monkeypatch):
    import src.etl.osm_extract_parsing as parsing
    monkeypatch.setattr(parsing, "MAX_FEATURE_BYTES", 1)
    path = tmp_path / "limited.osm.pbf"
    with osmium.SimpleWriter(str(path)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30.001, 60.001),
                                              tags={"tourism": "museum"}))
    with pytest.raises(ValueError, match="extract_observations_too_large"):
        extract_layers(path, StudyArea("sample", polygon(30, 60, 30.01, 60.01)), tmp_path / "features")


def test_future_pbf_timestamp_is_not_reported_as_observation(tmp_path):
    path = tmp_path / "future.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2999-01-01T00:00:00Z")
    with osmium.SimpleWriter(str(path), header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=osmium.osm.Location(30.001, 60.001)))
    with pytest.raises(ValueError, match="invalid_pbf_base_timestamp"):
        extract_layers(path, StudyArea("sample", polygon(30, 60, 30.01, 60.01)), tmp_path / "features")


def test_native_tag_filter_preserves_support_nodes_and_geometry(tmp_path):
    path = tmp_path / "native-filter.osm.pbf"
    with osmium.SimpleWriter(str(path)) as writer:
        for identifier, coord, tags in [
            (1, (29.9, 60.005), {}), (2, (30.1, 60.005), {}),
            (3, (29.9, 59.9), {}), (4, (30.1, 59.9), {}),
            (5, (30.1, 60.1), {}), (6, (29.9, 60.1), {}),
            (7, (30.005, 60.005), {"tourism": "museum"}),
            (8, (30.006, 60.006), {"unrelated": "tag"}),
        ]:
            writer.add_node(osmium.osm.mutable.Node(id=identifier, location=osmium.osm.Location(*coord), tags=tags))
        writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[1, 2], tags={"highway": "residential"}))
        writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[3, 4, 5, 6, 3], tags={"leisure": "park"}))
    area = StudyArea("inside", polygon(30, 60, 30.01, 60.01))
    base, base_counts, base_stamp = extract_layers(path, area, tmp_path / "baseline")
    native, native_counts, native_stamp = extract_layers(path, area, tmp_path / "native", native_tag_filter=True)
    assert native_counts == base_counts
    assert native_stamp == base_stamp
    assert native_counts["poi"] == 2 and native_counts["network"] == 1
    for role in base:
        assert native[role].read_bytes() == base[role].read_bytes()
    bbox, bbox_counts, bbox_stamp = extract_layers(
        path, area, tmp_path / "native-bbox", native_tag_filter=True, way_bbox_prefilter=True)
    assert bbox_counts == base_counts and bbox_stamp == base_stamp
    for role in base:
        assert bbox[role].read_bytes() == base[role].read_bytes()
