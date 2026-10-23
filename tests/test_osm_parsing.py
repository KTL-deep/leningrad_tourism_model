"""Synthetic, offline contract tests for the new counted Overpass parser."""

import copy
import json
import math

import pytest
from pyproj import Geod

from src.data_models.study_area import StudyArea
from src.etl.osm_parsing import OSMParseError, parse_overpass


@pytest.fixture
def area():
    return StudyArea("square", {"type": "Polygon", "coordinates": [
        [[30, 59], [31, 59], [31, 60], [30, 60], [30, 59]]]})


def response(elements=(), **overrides):
    counts = {"nodes": 0, "ways": 0, "relations": 0}
    for element in elements:
        counts[element["type"] + "s"] += 1
    payload = {"version": 0.6, "osm3s": {"timestamp_osm_base": "2025-01-01T12:00:00Z"},
               "elements": [*elements, {"type": "count", "id": 0, "tags":
                            {**{k: str(v) for k, v in counts.items()}, "total": str(len(elements))}}]}
    payload.update(overrides)
    return payload


def parse(payload, area):
    return parse_overpass(json.dumps(payload).encode(), area).to_dict()


def node(eid, lon=30.2, lat=59.2, **tags):
    return {"type": "node", "id": eid, "lon": lon, "lat": lat, "tags": tags}


def way(eid, refs, **tags):
    return {"type": "way", "id": eid, "nodes": refs, "tags": tags}


def test_empty_count_is_valid_but_absence_is_not(area):
    result = parse(response(), area)
    assert result["element_count"] == 0
    assert result["poi"]["features"] == result["network"]["features"] == []
    assert result["schema_version"] == 1
    assert result["osm_base_timestamp"] == "2025-01-01T12:00:00+00:00"
    assert result["real_world_poi_completeness"] == "unknown"
    assert result["coverage_scope"] == "query_bbox_with_full_way_nodes"
    assert result["routing_ready"] is False and result["surrounding_adequacy"] == "not_validated"
    with pytest.raises(OSMParseError, match="missing_count"):
        parse({**response(), "elements": []}, area)


@pytest.mark.parametrize("change,code", [
    ({"version": 0.5}, "invalid_schema"),
    ({"remark": "runtime error"}, "overpass_remark"),
    ({"osm3s": {}}, "invalid_timestamp"),
    ({"osm3s": {"timestamp_osm_base": "2025-01-01T12:00:00"}}, "invalid_timestamp"),
])
def test_rejects_invalid_envelope(area, change, code):
    with pytest.raises(OSMParseError) as exc:
        parse(response(**change), area)
    assert exc.value.code == code


def test_count_mismatch_and_bad_count(area):
    payload = response([node(1)])
    payload["elements"][-1]["tags"]["total"] = "0"
    with pytest.raises(OSMParseError, match="count_mismatch"):
        parse(payload, area)
    payload["elements"][-1]["tags"]["total"] = "1.0"
    with pytest.raises(OSMParseError, match="invalid_count"):
        parse(payload, area)


def test_optional_zero_area_count_does_not_hide_unsupported_area_elements(area):
    payload = response()
    payload["elements"][-1]["tags"]["areas"] = "0"
    assert parse(payload, area)["element_count"] == 0
    payload["elements"][-1]["tags"]["areas"] = "1"
    with pytest.raises(OSMParseError, match="unsupported_areas"):
        parse(payload, area)


def test_conflicting_duplicates_and_bad_ids_coords_refs(area):
    for elements, code in [
        ([node(1), node(1, lon=30.3)], "conflicting_duplicate"),
        ([node(True)], "invalid_id"),
        ([node(0)], "invalid_id"),
        ([node(1, lat=float("inf"))], "invalid_json"),
        ([node(1, lon=181)], "invalid_coordinates"),
        ([way(1, [True, 2], highway="path")], "invalid_id"),
        ([node(1), way(2, [1, 999], highway="path")], "missing_node_ref"),
    ]:
        with pytest.raises(OSMParseError) as exc:
            parse(response(elements), area)
        assert exc.value.code == code


def test_poi_precision_categories_and_defensive_copy(area):
    elements = [node(1, lon=30, lat=59, amenity="cafe", historic="monument"),
                node(2, lon=32, lat=59, tourism="viewpoint"),
                {**way(3, [1, 2], tourism="museum"), "center": {"lon": 30.5, "lat": 59.5}},
                 {"type": "relation", "id": 4, "members": [], "tags": {"historic": "castle"}}]
    inventory = parse_overpass(json.dumps(response(elements)).encode(), area)
    result = inventory.to_dict()
    poi = result["poi"]["features"]
    assert len(poi) == 4
    assert poi[0]["properties"]["categories"] == ["food", "historic"]
    assert poi[0]["properties"]["location_precision"] == "exact_source"
    assert poi[0]["properties"]["in_request"] is True
    assert poi[1]["properties"]["in_request"] is False
    assert poi[2]["properties"]["location_precision"] == "osm_bbox_center"
    assert poi[2]["properties"]["in_request"] is None
    assert poi[3]["geometry"] is None
    assert poi[3]["properties"]["quality_flags"] == ["missing_center"]
    assert result["quality"]["counts"]["missing_centers"] == 1
    assert all(f["properties"]["capacity"] is None and f["properties"]["public_access"] == "unknown" for f in poi)
    prior = copy.deepcopy(result)
    result["poi"]["features"][0]["properties"]["tags"]["amenity"] = "changed"
    assert inventory.to_dict() == prior


def test_raw_topology_directions_and_geodesic_lengths(area):
    nodes = [node(1, lon=30.1), node(2, lon=30.9), node(3, lon=30.5, lat=59.1),
             node(4, lon=30.5, lat=59.3)]
    ways = [way(10, [1, 2], highway="residential", oneway="yes", access="private"),
            way(11, [3, 4], highway="path", oneway="-1"),
            way(12, [1, 3], highway="path", oneway="no"),
            way(13, [2, 4], highway="path", junction="roundabout"),
            way(14, [1, 4], highway="path")]
    result = parse(response(nodes + ways), area)
    features = result["network"]["features"]
    assert [f["properties"]["direction_status"] for f in features] == ["forward", "reverse", "both", "implicit_roundabout", "unknown"]
    assert features[0]["properties"]["access"] == "private"
    assert features[0]["properties"]["node_ids"] == [1, 2]
    assert features[1]["properties"]["node_ids"] == [3, 4]  # crossing is not a junction
    assert features[2]["properties"]["node_ids"] == [1, 3]  # shared ID is explicit
    assert features[0]["properties"]["time_min"] is None
    expected = Geod(ellps="WGS84").inv(30.1, 59.2, 30.9, 59.2)[2]
    assert features[0]["properties"]["length_m"] == pytest.approx(expected)
    assert features[0]["properties"]["segment_lengths_m"] == pytest.approx([expected])


def test_missing_relation_members_and_huge_coordinates_are_rejected(area):
    with pytest.raises(OSMParseError, match="invalid_members"):
        parse(response([{"type": "relation", "id": 4, "tags": {"historic": "castle"}}]), area)
    with pytest.raises(OSMParseError, match="invalid_coordinates"):
        parse(response([node(1, lon=10**1000)]), area)


def test_same_osm_number_different_types_not_merged_and_centers_are_estimates(area):
    elements = [node(1, amenity="drinking_water"), node(2),
                {**way(1, [1, 2], tourism="museum"), "center": {"lon": 30.5, "lat": 59.5}}]
    result = parse(response(elements), area)
    first, second = result["poi"]["features"]
    assert first["id"] == "node/1" and second["id"] == "way/1"
    assert first["properties"]["categories"] == ["sanitary"]
    assert second["properties"]["location_status"] == "estimated"
    assert second["properties"]["in_request"] is None
    assert second["properties"]["center_in_request"] is True
    assert second["properties"]["capacity_status"] == "unknown"


def test_full_way_nodes_outside_boundary_are_not_silently_clipped(area):
    result = parse(response([node(1), node(2, lon=32), way(3, [1, 2], highway="path")]), area)
    feature = result["network"]["features"][0]
    assert feature["geometry"]["coordinates"][-1][0] == 32
    assert feature["properties"]["extends_request"]
    assert result["quality"]["counts"]["network_nodes"] == 2
    assert result["quality"]["counts"]["network_segments"] == 1
    assert result["routing_ready"] is False


def test_metric_length_matches_independent_equatorial_wgs84_reference():
    area = StudyArea("Synthetic equator", {"type": "Polygon", "coordinates": [
        [[-0.01, -0.01], [0.01, -0.01], [0.01, 0.01], [-0.01, 0.01], [-0.01, -0.01]]]})
    result = parse(response([node(1, lon=0, lat=0), node(2, lon=0.001, lat=0),
                             way(3, [1, 2], highway="path")]), area)
    # On the equator, WGS84 geodesic arc = semi-major axis * longitude radians.
    reference_m = 6_378_137 * math.radians(0.001)
    link = result["network"]["features"][0]["properties"]
    assert link["length_m"] == pytest.approx(reference_m, abs=1e-6)
    assert link["time_min"] is None
