"""Offline contracts for E04 non-destructive heritage evidence matching."""

import copy
import json

import pytest
from pyproj import Geod

from src.etl import heritage_matching as matching


A = "471510293010006"  # Real OSM relation/11718736 ref:egrokn, Smolensk Gates.
B = "471510293010007"


def official(eid="native-1", *, registry=A, name="Смоленские ворота", point=(30, 60), **extra):
    return {"source_record_id": eid, "registry_number": registry, "name": name,
            "geometry": {"type": "Point", "coordinates": list(point)} if point is not None else None,
            "source_url": "https://example.org/catalogue/2026", **extra}


def poi(eid=11718736, *, kind="relation", point=(30, 60), **tags):
    return {"type": "Feature", "id": f"{kind}/{eid}",
            "geometry": {"type": "Point", "coordinates": list(point)} if point is not None else None,
            "properties": {"osm_type": kind, "osm_id": eid, "tags": tags,
                           "location_precision": "osm_bbox_center" if kind != "node" else "exact_source"}}


def test_real_tag_identifier_provenance_conflicts_and_far_outside_query():
    records = [official(point=(0, 0), location_precision="registry_approximation")]
    features = [poi(point=(30, 60), name="Другой объект", **{"ref:egrokn": A})]
    originals = copy.deepcopy((records, features))
    result = matching.match_heritage(records, features)
    assert (records, features) == originals
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["algorithm_version"] == matching.HERITAGE_MATCHING_VERSION
    assert result["official_records"] == records
    assert result["poi_features"] == features
    assert result["review_candidates"] == []
    link = result["identifier_links"][0]
    assert link["source_record_id"] == "native-1" and link["poi_id"] == "relation/11718736"
    assert link["status"] == "identifier_link"
    assert link["conflicts"] == {"name": True, "distance_exceeds_radius": True}
    assert link["evidence"]["official_location_precision"] == "registry_approximation"
    assert link["evidence"]["osm_location_precision"] == "osm_bbox_center"
    assert "no_entrance" in " ".join(link["evidence"]["warnings"])
    assert result["summary"]["auto_merged_count"] == 0
    assert not any("unique_establishment_count" in key for key in result)


def test_explicit_refs_split_only_whole_15_digit_tokens_and_preserve_ambiguous_ids():
    records = [official("z", registry=A), official("a", registry=A), official("b", registry=B)]
    features = [poi(**{"ref:egrokn": f"{A}; {B},12345678 9010006;garbage",
                       "ref:ru:heritage": A})]
    result = matching.match_heritage(records, features)
    assert [(x["source_record_id"], x["registry_number"], x["status"])
            for x in result["identifier_links"]] == [
                ("a", A, "ambiguous_identifier"), ("b", B, "identifier_link"),
                ("z", A, "ambiguous_identifier")]
    assert result["summary"]["ambiguous_registry_numbers"] == [A]
    assert [x["token"] for x in result["invalid_ref_tokens"]] == ["12345678", "9010006", "garbage"]
    assert result["summary"]["comparisons"] >= 3


def test_noncanonical_tags_numbers_and_alias_do_not_infer_identifier_or_name():
    records = [official()]
    features = [poi(name="Not the name", alt_name="Смоленские ворота",
                    heritage=A, **{"heritage:ref": A, "brand:wikidata": "Q42",
                                   "website": f"https://example.org/{A}"})]
    assert matching.match_heritage(records, features)["identifier_links"] == []
    assert matching.match_heritage(records, features)["review_candidates"] == []
    result = matching.match_heritage(records, [poi(**{"ref:ru:heritage": B})])
    assert result["unmatched_valid_refs"] == [{"poi_id": "relation/11718736", "registry_number": B}]


def test_exact_normalized_name_geodesic_radius_and_no_transitive_merging():
    geod = Geod(ellps="WGS84")
    near_lon, near_lat, _ = geod.fwd(179.9999, 0, 90, 99)
    far_lon, far_lat, _ = geod.fwd(179.9999, 0, 90, 101)
    records = [official("a", registry=None, name=" МУЗЕЙ—МИР ", point=(179.9999, 0))]
    features = [poi(1, kind="node", point=(near_lon, near_lat), name="музей мир"),
                poi(2, kind="node", point=(far_lon, far_lat), name="музей мир")]
    result = matching.match_heritage(records, features)
    assert [x["poi_id"] for x in result["review_candidates"]] == ["node/1"]
    assert result["review_candidates"][0]["status"] == "name_distance_review_candidate"
    assert result["summary"]["input_poi_features"] == 2
    assert result["summary"]["auto_merged_count"] == 0


def test_unlocated_name_only_and_direct_id_does_not_create_second_candidate():
    records = [official("a", point=None), official("b", registry=None, point=None)]
    result = matching.match_heritage(records, [poi(name="Смоленские ворота", **{"ref:egrokn": A})])
    assert len(result["identifier_links"]) == 1
    assert [(x["source_record_id"], x["status"]) for x in result["review_candidates"]] == [
        ("b", "name_only_unlocated_candidate")]
    assert result["review_candidates"][0]["evidence"]["distance_m"] is None
    assert matching.match_heritage([official(point=(30, 60))],
                                   [poi(point=None, name="Смоленские ворота")])["review_candidates"][0][
                                       "status"] == "name_only_unlocated_candidate"


@pytest.mark.parametrize("change,error", [
    (lambda x: x.update(registry_number="12345678 9010006"), "invalid_registry_number"),
    (lambda x: x.update(registry_number=123456789012345), "invalid_registry_number"),
    (lambda x: x.update(name=" "), "invalid_name"),
    (lambda x: x.update(source_record_id=""), "invalid_source_record_id"),
    (lambda x: x.update(geometry={"type": "Polygon", "coordinates": []}), "invalid_geometry"),
    (lambda x: x["geometry"].update(coordinates=[True, 60]), "invalid_coordinates"),
    (lambda x: x["geometry"].update(coordinates=[float("nan"), 60]), "invalid_coordinates"),
    (lambda x: x["geometry"].update(coordinates=[181, 60]), "invalid_coordinates"),
])
def test_invalid_official_records_fail_not_zero(change, error):
    record = official()
    change(record)
    with pytest.raises(ValueError, match=f"^{error}$"):
        matching.match_heritage([record], [])


@pytest.mark.parametrize("change,error", [
    (lambda x: x.update(id="way/11718736"), "inconsistent_id"),
    (lambda x: x["properties"].update(osm_id=True), "invalid_id"),
    (lambda x: x["properties"].update(tags={"name": None}), "invalid_tags"),
    (lambda x: x["geometry"].update(coordinates=[0, float("inf")]), "invalid_coordinates"),
    (lambda x: x["geometry"].update(coordinates=[0, False]), "invalid_coordinates"),
])
def test_invalid_poi_fail(change, error):
    feature = poi()
    change(feature)
    with pytest.raises(ValueError, match=f"^{error}$"):
        matching.match_heritage([], [feature])


def test_duplicate_ids_and_invalid_radius_fail():
    with pytest.raises(ValueError, match="^duplicate_source_record_id$"):
        matching.match_heritage([official("same", registry=A), official("same", registry=B)], [])
    with pytest.raises(ValueError, match="^duplicate_poi_id$"):
        matching.match_heritage([], [poi(), poi()])
    for radius in (True, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="^invalid_radius$"):
            matching.match_heritage([], [], radius)


def test_all_caps_fail_explicitly_including_repeat_index_encounters(monkeypatch):
    monkeypatch.setattr(matching, "MAX_RECORDS", 1)
    with pytest.raises(ValueError, match="^too_many_records$"):
        matching.match_heritage([official("a"), official("b")], [])
    monkeypatch.setattr(matching, "MAX_POI", 1)
    with pytest.raises(ValueError, match="^too_many_poi$"):
        matching.match_heritage([], [poi(1), poi(2)])
    monkeypatch.setattr(matching, "MAX_RECORDS", 50000)
    monkeypatch.setattr(matching, "MAX_POI", 200000)
    monkeypatch.setattr(matching, "MAX_COMPARISONS", 1)
    with pytest.raises(ValueError, match="^too_many_comparisons$"):
        matching.match_heritage([official()], [poi(name="Смоленские ворота", **{"ref:egrokn": A})])
    monkeypatch.setattr(matching, "MAX_COMPARISONS", 250000)
    monkeypatch.setattr(matching, "MAX_OUTPUT", 1)
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([official("a"), official("b")], [poi(**{"ref:egrokn": A})])
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([], [poi(**{"ref:egrokn": "invalid;invalid"})])


def test_deterministic_reordered_inputs_and_empty_report():
    records = [official("b", registry=None), official("a")]
    pois = [poi(2, name="Смоленские ворота"), poi(1, **{"ref:egrokn": A})]
    assert matching.match_heritage(records, pois) == matching.match_heritage(records[::-1], pois[::-1])
    empty = matching.match_heritage([], [])
    assert empty["identifier_links"] == empty["review_candidates"] == []
    assert empty["summary"]["input_official_records"] == 0
