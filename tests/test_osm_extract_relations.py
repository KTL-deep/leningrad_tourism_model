import json

import osmium
import pytest

from src.data_models.study_area import StudyArea
from src.etl.osm_extract_parsing import extract_layers, extract_layers_with_relations


def box(west, south, east, north):
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south],
            [east, north], [west, north], [west, south]]]}


def fixture(path, *, missing=False, missing_ref=False, detached_inner=False):
    # Outer ring is split; the inner ring is untagged, as are the outer segments.
    coords = [(0, 0), (10, 0), (10, 10), (0, 10),
              (4, 4), (6, 4), (6, 6), (4, 6),
              (20, 20), (21, 20), (21, 21), (20, 21)]
    with osmium.SimpleWriter(str(path)) as writer:
        for identifier, (lon, lat) in enumerate(coords, 14_000_000_001):
            writer.add_node(osmium.osm.mutable.Node(id=identifier,
                            location=osmium.osm.Location(lon, lat)))
        base = 14_000_000_001
        ways = [(11, [base, base + 1, base + 2], {}),
                (12, [base + 2, base + 3, base], {}),
                (13, [base + 4, base + 5, base + 6, base + 7, base + 4], {}),
                (14, [base + 8, base + 9, base + 10, base + 11, base + 8], {})]
        for identifier, nodes, tags in ways:
            if missing and identifier == 12:
                continue
            if missing_ref and identifier == 12:
                nodes = [base + 2, 99_000_000_000, base]
            writer.add_way(osmium.osm.mutable.Way(id=identifier, nodes=nodes, tags=tags))
        writer.add_way(osmium.osm.mutable.Way(id=15, nodes=[base + 8, base + 9, base + 10,
                        base + 11, base + 8], tags={"leisure": "park"}))
        writer.add_relation(osmium.osm.mutable.Relation(id=100, tags={"type": "multipolygon", "leisure": "park"},
                            members=[("w", 11, "outer"), ("w", 12, "outer"),
                                      ("w", 13, "inner"), ("w", 14, "inner" if detached_inner else "outer")]))
        writer.add_relation(osmium.osm.mutable.Relation(id=101, tags={"type": "route", "tourism": "museum"},
                            members=[("w", 11, "")]))


@pytest.mark.parametrize("region,expected", [
    (box(1, 1, 2, 2), True),  # no outer vertex inside request
    (box(4.5, 4.5, 5.5, 5.5), False),  # hole
    (box(20.2, 20.2, 20.8, 20.8), True),  # second component
])
def test_assembled_geometry_and_scope(tmp_path, region, expected):
    path = tmp_path / "sample.pbf"
    fixture(path)
    area = StudyArea("sample", region)
    paths, counts, _ = extract_layers_with_relations(path, area, tmp_path / "output")
    features = [json.loads(line) for line in paths["poi"].read_text().splitlines()]
    relations = [f for f in features if f["id"] == "relation/100"]
    assert bool(relations) is expected
    assert counts["relation_poi"] == int(expected)
    assert counts["relation_candidates"] == 1
    assert counts["relation_unsupported_poi"] == 1
    assert counts["relation_unassembled"] == counts["relation_invalid"] == 0
    assert counts["relations_unsupported"] is True
    assert counts["relation_failure_scope"] == "source_extract"
    if expected:
        assert relations[0]["properties"]["location_precision"] == "approximate_relation_centroid"
    if region == box(20.2, 20.2, 20.8, 20.8):
        assert sorted(f["id"] for f in features) == ["relation/100", "way/15"]


@pytest.mark.parametrize("missing_ref", [False, True])
def test_missing_member_never_emits_partial_relation(tmp_path, missing_ref):
    path = tmp_path / "missing.pbf"
    fixture(path, missing=not missing_ref, missing_ref=missing_ref)
    paths, counts, _ = extract_layers_with_relations(
        path, StudyArea("sample", box(20.2, 20.2, 20.8, 20.8)), tmp_path / "output")
    assert all("relation/100" != json.loads(line)["id"] for line in paths["poi"].read_text().splitlines())
    assert counts["relation_invalid"] + counts["relation_unassembled"] >= 1


def test_legacy_and_flags(tmp_path):
    path = tmp_path / "sample.pbf"
    fixture(path)
    area = StudyArea("sample", box(20.2, 20.2, 20.8, 20.8))
    paths, counts, _ = extract_layers(path, area, tmp_path / "legacy")
    assert counts == {"poi": 1, "network": 0, "missing_way_locations": 0,
                      "relations_unsupported": True}
    assert json.loads(paths["poi"].read_text())["id"] == "way/15"
    for flag in (1, "yes", None):
        with pytest.raises(ValueError, match="bool"):
            extract_layers(path, area, tmp_path / "bad", include_relations=flag)
    with pytest.raises(ValueError, match="relation_native_tag_filter_unsupported"):
        extract_layers(path, area, tmp_path / "bad", include_relations=True, native_tag_filter=True)


def test_relation_output_limit_is_not_counted_as_invalid(tmp_path, monkeypatch):
    import src.etl.osm_extract_parsing as parsing

    path = tmp_path / "sample.pbf"
    fixture(path)
    # The assembled geometry fits, but the serialized relation observation does not.
    monkeypatch.setattr(parsing, "MAX_FEATURE_BYTES", 450)
    with pytest.raises(ValueError, match="extract_observations_too_large"):
        extract_layers_with_relations(path, StudyArea("sample", box(1, 1, 2, 2)), tmp_path / "limited")


def test_detached_inner_ring_cannot_become_an_outer_poi(tmp_path):
    path = tmp_path / "invalid-inner.pbf"
    fixture(path, detached_inner=True)
    paths, counts, _ = extract_layers_with_relations(
        path, StudyArea("sample", box(20.2, 20.2, 20.8, 20.8)), tmp_path / "output")
    assert all(json.loads(line)["id"] != "relation/100"
               for line in paths["poi"].read_text().splitlines())
    assert counts["relation_invalid"] + counts["relation_unassembled"] >= 1


def test_failed_relation_prepass_does_not_open_output_files(tmp_path, monkeypatch):
    import src.etl.osm_extract_parsing as parsing

    path = tmp_path / "sample.pbf"
    fixture(path)
    monkeypatch.setattr(parsing, "MAX_RELATION_CANDIDATES", 0)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="extract_relation_candidates_too_large"):
        extract_layers_with_relations(path, StudyArea("sample", box(1, 1, 2, 2)), output)
    assert not list(output.glob("*.features"))


def test_opening_second_output_failure_closes_first(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "sample.pbf"
    fixture(path)
    original = Path.open
    opened = []

    def fail_second(self, *args, **kwargs):
        if self.name == "network.features":
            raise OSError("cannot open second output")
        handle = original(self, *args, **kwargs)
        if self.name == "poi.features":
            opened.append(handle)
        return handle

    monkeypatch.setattr(Path, "open", fail_second)
    with pytest.raises(OSError, match="cannot open second output"):
        extract_layers_with_relations(path, StudyArea("sample", box(1, 1, 2, 2)), tmp_path / "output")
    assert opened and all(handle.closed for handle in opened)
