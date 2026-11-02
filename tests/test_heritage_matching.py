"""Offline contracts for E04 non-destructive heritage evidence matching."""

import copy
import json

import pytest
from pyproj import Geod

from src.etl import heritage_matching as matching


A = "471510293010006"  # Real OSM relation/11718736 ref:egrokn, Smolensk Gates.
B = "471510293010007"
OKN = "781610399040926"  # OSM node/1030878327; official record 6ad2e178482d0360a136d69d.
OKN_OTHER = "781510399040236"  # OSM relation/6017443; official record 6ad2e172482d0360a136cd2a.
OKN_WARNING = "ref_okn_namespace_length_documentation_conflict"


def locality_context():
    return {"context_version": 1, "status": "source_reported",
            "source_snapshot_id": "snapshot-1", "original_snapshot_id": "snapshot-1",
            "raw_sha256": "a" * 64, "locality_name": "Example locality",
            "region_name": "Example region", "query": "Example locality, Example region",
            "bbox": [29.9, 59.9, 30.1, 60.1], "guard_m": 5000,
            "interpretation": "locality_envelope_not_heritage_geometry_or_legal_boundary"}


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


def test_repeated_unmatched_reference_does_not_become_a_known_registry_value():
    records = [official(name="Garden", point=None)]
    features = [poi(eid, name="Garden", **{"ref:okn": B}) for eid in (1, 2)]
    result = matching.match_heritage(records, features)
    assert len(result["unmatched_valid_refs"]) == 2
    assert len(result["review_candidates"]) == 2
    assert result["excluded_review_candidates"] == []
    assert result["identifier_links"] == []


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


@pytest.mark.parametrize("eid,kind,number,source_id", [
    (1030878327, "node", OKN, "6ad2e178482d0360a136d69d"),
    (6017443, "relation", OKN_OTHER, "6ad2e172482d0360a136cd2a"),
])
def test_ref_okn_exact_registry_number_links_with_unverified_namespace(eid, kind, number, source_id):
    records = [official(source_id, registry=number, point=None)]
    features = [poi(eid, kind=kind, point=None, **{"ref:okn": number})]
    original = copy.deepcopy((records, features))
    result = matching.match_heritage(records, features)
    assert (records, features) == original
    assert len(result["identifier_links"]) == 1
    link = result["identifier_links"][0]
    assert (link["source_record_id"], link["poi_id"], link["registry_number"], link["status"]) == (
        source_id, f"{kind}/{eid}", number, "identifier_link")
    assert link["evidence"]["supporting_ref_tags"] == [{"tag": "ref:okn", "value": number}]
    assert link["evidence"]["identifier_namespace_status"] == (
        "ref_okn_exact_registry_value_namespace_unverified")
    assert OKN_WARNING in link["evidence"]["warnings"]
    assert result["summary"]["ref_okn_identifier_link_count"] == 1
    assert result["summary"]["ref_okn_only_identifier_link_count"] == 1


def test_ref_okn_rejects_16_digits_and_malformed_without_padding_or_inference():
    result = matching.match_heritage(
        [official(registry=OKN)],
        [poi(name="Different", website=f"https://example.org/{OKN}",
             **{"ref:okn": f"0{OKN};{OKN}0,78161039;bad"})])
    assert result["identifier_links"] == result["unmatched_valid_refs"] == []
    assert [(x["token"], x["reason"]) for x in result["invalid_ref_tokens"]] == [
        (f"0{OKN}", "ref_okn_not_national_15_digit"),
        ("78161039", "ref_okn_not_national_15_digit"),
        (f"{OKN}0", "ref_okn_not_national_15_digit"),
        ("bad", "ref_okn_not_national_15_digit")]
    assert result["summary"]["ref_okn_identifier_link_count"] == 0
    assert result["summary"]["ref_okn_only_identifier_link_count"] == 0


def test_ref_okn_unmatched_full_number_does_not_invent_registry_entry():
    result = matching.match_heritage([], [poi(**{"ref:okn": OKN})])
    assert result["identifier_links"] == []
    assert result["unmatched_valid_refs"] == [
        {"poi_id": "relation/11718736", "registry_number": OKN,
         "tag": "ref:okn", "namespace_status": "unverified"}]


def test_ref_okn_multiple_numbers_conflicting_canonical_and_duplicate_official_ids():
    records = [official("a", registry=OKN), official("b", registry=OKN),
               official("c", registry=OKN_OTHER), official("d", registry=A)]
    features = [poi(name="Different", **{"ref:okn": f"{OKN_OTHER}; {OKN}",
                                         "ref:egrokn": f"{OKN};{A}",
                                         "ref:ru:heritage": OKN})]
    result = matching.match_heritage(records, features)
    assert [(x["source_record_id"], x["registry_number"], x["status"])
            for x in result["identifier_links"]] == [
                ("a", OKN, "ambiguous_identifier"), ("b", OKN, "ambiguous_identifier"),
                ("c", OKN_OTHER, "identifier_link"), ("d", A, "identifier_link")]
    assert result["summary"]["ambiguous_registry_numbers"] == [OKN]
    assert result["summary"]["identifier_link_count"] == 4
    assert result["summary"]["ref_okn_identifier_link_count"] == 3
    assert result["summary"]["ref_okn_only_identifier_link_count"] == 1
    assert result["summary"]["comparisons"] == 4
    assert [x["evidence"]["supporting_ref_tags"] for x in result["identifier_links"]] == [
        [{"tag": "ref:egrokn", "value": OKN}, {"tag": "ref:ru:heritage", "value": OKN},
         {"tag": "ref:okn", "value": OKN}],
        [{"tag": "ref:egrokn", "value": OKN}, {"tag": "ref:ru:heritage", "value": OKN},
         {"tag": "ref:okn", "value": OKN}],
        [{"tag": "ref:okn", "value": OKN_OTHER}], [{"tag": "ref:egrokn", "value": A}]]
    assert [x["evidence"]["identifier_namespace_status"] for x in result["identifier_links"]] == [
        "explicit_national_ref", "explicit_national_ref",
        "ref_okn_exact_registry_value_namespace_unverified", "explicit_national_ref"]
    assert [OKN_WARNING in x["evidence"]["warnings"] for x in result["identifier_links"]] == [
        True, True, True, False]
    assert result == matching.match_heritage(records[::-1], features)


def test_ref_okn_provenance_is_number_specific_and_does_not_use_website_or_other_tags():
    records = [official("a", registry=A), official("b", registry=OKN)]
    result = matching.match_heritage(records, [poi(1, name="Different",
        website=f"https://example.org/{OKN}", **{"ref:egrokn": A, "ref:okn": OKN})])
    assert [(x["registry_number"], x["evidence"]["supporting_ref_tags"])
            for x in result["identifier_links"]] == [
                (A, [{"tag": "ref:egrokn", "value": A}]),
                (OKN, [{"tag": "ref:okn", "value": OKN}])]


@pytest.mark.parametrize("role_tags,role", [
    ({"tourism": "information"}, "information_about_feature"),
    ({"information": "board"}, "information_about_feature"),
    ({"information": "guidepost"}, "information_about_feature"),
    ({"information": "map"}, "information_about_feature"),
    ({"highway": "bus_stop"}, "transport_stop"),
    ({"public_transport": "platform"}, "transport_stop"),
    ({"public_transport": "stop_position"}, "transport_stop"),
])
def test_pure_non_object_subject_excluded_for_name_distance_and_unlocated(role_tags, role):
    records = [official("located", registry=None, source_address="Адрес 1"),
               official("unlocated", registry=None, point=None)]
    features = [poi(1, kind="node", name="Смоленские ворота", **role_tags)]
    originals = copy.deepcopy((records, features))
    result = matching.match_heritage(records, features)
    assert (records, features) == originals
    assert result["official_records"] == records
    assert result["poi_features"] == features
    assert result["review_candidates"] == []
    assert [(x["source_record_id"], x["status"], x["reason"])
            for x in result["excluded_review_candidates"]] == [
                ("located", "excluded_name_candidate", "osm_non_object_subject_role"),
                ("unlocated", "excluded_name_candidate", "osm_non_object_subject_role")]
    assert result["excluded_review_candidates"][0]["evidence"]["osm_subject_role"] == role
    assert result["excluded_review_candidates"][0]["evidence"]["official_address"] == "Адрес 1"
    assert result["summary"]["review_candidate_count"] == 0
    assert result["summary"]["excluded_review_candidate_count"] == 2
    assert result["summary"]["exclusion_counts"] == {
        "osm_non_object_subject_role": 2,
        "unlocated_name_conflicts_with_explicit_registry_reference": 0,
        "unlocated_address_locality_conflict": 0}
    assert result["summary"]["comparisons"] == 2


@pytest.mark.parametrize("marker,value", [
    ("heritage", "2"), ("historic", "yes"), ("building", "church"),
    ("amenity", "place_of_worship"),
])
def test_independent_object_marker_retains_name_candidate_despite_information_tag(marker, value):
    result = matching.match_heritage(
        [official(registry=None)],
        [poi(name="Смоленские ворота", tourism="information", **{marker: value})])
    assert len(result["review_candidates"]) == 1
    assert result["review_candidates"][0]["evidence"]["osm_subject_role"] == (
        "mixed_or_object_evidence")
    assert result["excluded_review_candidates"] == []


def test_id_link_on_information_board_kept_with_non_physical_identity_warning():
    result = matching.match_heritage(
        [official("linked", registry=OKN, source_address="Адрес 2")],
        [poi(name="Смоленские ворота", information="board", **{"ref:okn": OKN})])
    assert len(result["identifier_links"]) == 1
    evidence = result["identifier_links"][0]["evidence"]
    assert evidence["osm_subject_role"] == "information_about_feature"
    assert evidence["official_address"] == "Адрес 2"
    assert "identifier_reference_on_non_object_subject_not_physical_identity" in evidence["warnings"]
    assert OKN_WARNING in evidence["warnings"]
    assert result["review_candidates"] == result["excluded_review_candidates"] == []


def test_unlocated_name_conflict_requires_known_exact_ref_and_preserves_other_candidates():
    records = [official("same", registry=A, point=None),
               official("other", registry=B, point=None),
               official("extra", registry=OKN, point=None),
               official("unknown", registry=None, point=None)]
    feature = poi(name="Смоленские ворота", **{"ref:egrokn": A, "ref:okn": OKN})
    result = matching.match_heritage(records, [feature])
    assert [(x["source_record_id"], x["registry_number"]) for x in result["identifier_links"]] == [
        ("extra", OKN), ("same", A)]
    assert [x["source_record_id"] for x in result["review_candidates"]] == ["unknown"]
    assert [(x["source_record_id"], x["reason"])
            for x in result["excluded_review_candidates"]] == [
                ("other", "unlocated_name_conflicts_with_explicit_registry_reference")]
    assert result["summary"]["exclusion_counts"][
        "unlocated_name_conflicts_with_explicit_registry_reference"] == 1
    assert result == matching.match_heritage(records[::-1], [feature])


@pytest.mark.parametrize("ref_tags", [{}, {"ref:okn": "not-an-id"},
                                     {"ref:okn": OKN_OTHER}, {"ref:egrokn": OKN_OTHER}])
def test_unlocated_name_does_not_exclude_without_known_registry_reference(ref_tags):
    result = matching.match_heritage([official(registry=B, point=None)],
                                    [poi(name="Смоленские ворота", **ref_tags)])
    assert len(result["review_candidates"]) == 1
    assert result["excluded_review_candidates"] == []


def test_unlocated_conflict_when_poi_point_is_absent_and_official_is_located():
    result = matching.match_heritage([official("other", registry=B),
                                      official("known", registry=A, name="Different")], [
        poi(point=None, name="Смоленские ворота", **{"ref:egrokn": A})])
    assert result["review_candidates"] == []
    assert result["excluded_review_candidates"][0]["reason"] == (
        "unlocated_name_conflicts_with_explicit_registry_reference")


def test_source_reported_locality_excludes_only_far_unlocated_name_candidate():
    context = locality_context()
    records = [official("far", registry=None, point=None, source_address="Example locality",
                        address_locality_context=context)]
    features = [poi(1, kind="node", point=(40, 60), name="Смоленские ворота"),
                poi(2, kind="node", point=(30.12, 60), name="Смоленские ворота")]
    original = copy.deepcopy((records, features))
    result = matching.match_heritage(records, features)
    assert (records, features) == original
    assert result["official_records"] == records
    assert result["official_records"][0]["geometry"] is None
    assert result["summary"]["official_with_point"] == 0
    assert [(x["poi_id"], x["reason"]) for x in result["excluded_review_candidates"]] == [
        ("node/1", "unlocated_address_locality_conflict")]
    assert [x["poi_id"] for x in result["review_candidates"]] == ["node/2"]
    far = result["excluded_review_candidates"][0]["evidence"]
    near = result["review_candidates"][0]["evidence"]
    assert far["official_address_locality_context"] == context
    assert far["address_locality_comparison"]["status"] == "outside_guarded_context_envelope"
    assert near["address_locality_comparison"]["status"] == "inside_or_near_context_envelope"
    assert far["distance_m"] is near["distance_m"] is None
    assert result["summary"]["exclusion_counts"]["unlocated_address_locality_conflict"] == 1


def test_locality_unknown_geometry_and_located_official_not_excluded():
    context = locality_context()
    records = [official("unlocated", registry=None, point=None, address_locality_context=context),
               official("located", registry=None, point=(30, 60),
                        address_locality_context=context)]
    result = matching.match_heritage(records, [poi(point=None, name="Смоленские ворота")])
    assert result["excluded_review_candidates"] == []
    assert len(result["review_candidates"]) == 2
    assert all(x["evidence"]["address_locality_comparison"]["status"] == "unknown_geometry"
               for x in result["review_candidates"])
    located = matching.match_heritage(
        [official("located", registry=None, point=(40, 60), address_locality_context=context)],
        [poi(point=(40, 60), name="Смоленские ворота")])
    assert [x["status"] for x in located["review_candidates"]] == [
        "name_distance_review_candidate"]
    assert located["review_candidates"][0]["evidence"]["address_locality_comparison"][
        "status"] == "outside_guarded_context_envelope"
    assert located["excluded_review_candidates"] == []


def test_identifier_link_kept_with_locality_conflict_warning_and_reason_precedence():
    context = locality_context()
    records = [official("linked", point=None, address_locality_context=context),
               official("other", registry=B, point=None, address_locality_context=context),
               official("name", registry=None, point=None, address_locality_context=context)]
    result = matching.match_heritage(records, [poi(point=(40, 60), name="Смоленские ворота",
                                                  **{"ref:egrokn": A})])
    assert [x["source_record_id"] for x in result["identifier_links"]] == ["linked"]
    assert "identifier_reference_address_locality_conflict_not_physical_identity" in (
        result["identifier_links"][0]["evidence"]["warnings"])
    assert [(x["source_record_id"], x["reason"]) for x in result["excluded_review_candidates"]] == [
        ("name", "unlocated_address_locality_conflict"),
        ("other", "unlocated_name_conflicts_with_explicit_registry_reference")]
    assert result["summary"]["exclusion_counts"]["unlocated_address_locality_conflict"] == 1


def test_non_object_role_takes_precedence_over_address_locality_conflict():
    result = matching.match_heritage(
        [official(registry=None, point=None, address_locality_context=locality_context())],
        [poi(point=(40, 60), name="Смоленские ворота", information="board")])
    assert result["excluded_review_candidates"][0]["reason"] == "osm_non_object_subject_role"
    assert result["summary"]["exclusion_counts"]["unlocated_address_locality_conflict"] == 0


def test_bad_address_locality_context_fails_closed_without_changing_input():
    context = locality_context()
    context["bbox"] = [40, 60, 30, 61]
    record = official(point=None, address_locality_context=context)
    original = copy.deepcopy(record)
    with pytest.raises(ValueError):
        matching.match_heritage([record], [])
    assert record == original
    without_context = matching.match_heritage([official(point=None)],
                                              [poi(point=(40, 60), name="Смоленские ворота")])
    assert without_context["review_candidates"][0]["evidence"][
        "address_locality_comparison"] == {"status": "unavailable"}
    assert without_context["review_candidates"][0]["evidence"][
        "official_address_locality_context"] is None
    assert matching.match_heritage([official(point=None, address_locality_context=None)],
                                   [poi(point=(40, 60), name="Смоленские ворота")])[
                                       "review_candidates"][0]["evidence"][
                                           "address_locality_comparison"] == {"status": "unavailable"}


def test_locality_exclusions_share_global_output_limit(monkeypatch):
    monkeypatch.setattr(matching, "MAX_OUTPUT", 1)
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage(
            [official("one", registry=None, point=None, address_locality_context=locality_context()),
             official("two", registry=None, point=None, address_locality_context=locality_context())],
            [poi(point=(40, 60), name="Смоленские ворота")])


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
    with pytest.raises(ValueError, match="^too_many_comparisons$"):
        matching.match_heritage([official("a", registry=OKN), official("b", registry=OKN)],
                                [poi(name="Different", **{"ref:okn": OKN})])
    with pytest.raises(ValueError, match="^too_many_comparisons$"):
        matching.match_heritage([official("a", registry=None), official("b", registry=None)],
                                [poi(name="Смоленские ворота", information="board")])
    monkeypatch.setattr(matching, "MAX_COMPARISONS", 250000)
    monkeypatch.setattr(matching, "MAX_OUTPUT", 1)
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([official("a"), official("b")], [poi(**{"ref:egrokn": A})])
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([], [poi(**{"ref:egrokn": "invalid;invalid"})])
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([], [poi(**{"ref:okn": f"{OKN};{OKN_OTHER}"})])
    with pytest.raises(ValueError, match="^too_many_output_entries$"):
        matching.match_heritage([official("a", registry=None), official("b", registry=None)],
                                [poi(name="Смоленские ворота", information="board")])


def test_deterministic_reordered_inputs_and_empty_report():
    records = [official("b", registry=None), official("a")]
    pois = [poi(2, name="Смоленские ворота"), poi(1, **{"ref:egrokn": A})]
    assert matching.match_heritage(records, pois) == matching.match_heritage(records[::-1], pois[::-1])
    empty = matching.match_heritage([], [])
    assert empty["identifier_links"] == empty["review_candidates"] == []
    assert empty["summary"]["input_official_records"] == 0
