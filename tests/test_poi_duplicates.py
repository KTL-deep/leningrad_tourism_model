"""Offline contracts for bounded, non-merging POI duplicate candidates."""

import copy
import json

import pytest
from pyproj import Geod

from src.etl import poi_duplicates as audit


def poi(eid, lon=30, lat=60, *, kind="node", categories=None, **tags):
    return {"type": "Feature", "id": f"{kind}/{eid}",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"osm_type": kind, "osm_id": eid, "tags": tags,
                           "categories": ["poi"] if categories is None else categories}}


def pairs(features, **kwargs):
    return audit.detect_poi_duplicates(features, **kwargs)["candidate_pairs"]


def test_unicode_semantic_tags_and_repeatability_without_mutation():
    features = [poi(2, 30.0003, name="  КАФЕ—Мир  ", amenity="Cafe"),
                poi(1, name="кафе мир", amenity="cafe")]
    original = copy.deepcopy(features)
    result = audit.detect_poi_duplicates(features)
    assert result == audit.detect_poi_duplicates(list(reversed(features)))
    assert json.loads(json.dumps(result)) == result
    assert features == original
    assert result["algorithm_version"] == audit.DUPLICATE_AUDIT_VERSION
    assert result["summary"] == {"raw_observation_count": 2,
                                 "candidate_pair_count": 1, "auto_merged_count": 0}
    assert result["candidate_pairs"][0]["pair_ids"] == ["node/1", "node/2"]
    assert result["candidate_pairs"][0]["evidence"]["matched_semantic_tags"] == {"amenity": "cafe"}


def test_entity_qids_conflicts_and_no_brand_or_category_only_matches():
    features = [poi(1, name="A", amenity="cafe", wikidata="Q42"),
                poi(2, 50, 60, name="B", shop="bakery", wikidata="Q42"),
                poi(3, name="A", amenity="cafe", wikidata="Q43"),
                poi(4, name="A", amenity="cafe", **{"brand:wikidata": "Q42"}),
                poi(5, 50, 60, name="A", amenity="cafe"),
                poi(6, name="A", tourism="museum"),
                poi(7, name="A", amenity="cafe", wikidata="Q0")]
    found = pairs(features)
    by_id = {tuple(p["pair_ids"]): p for p in found}
    assert ("node/1", "node/2") in by_id
    assert by_id[("node/1", "node/2")]["evidence"]["shared_entity_wikidata"] == "Q42"
    assert by_id[("node/1", "node/3")]["status"] == "conflicting_entity_wikidata"
    assert ("node/2", "node/5") not in by_id  # different name, no entity QID
    assert ("node/1", "node/5") not in by_id  # same name and tag but far away
    assert ("node/1", "node/6") not in by_id  # category/name alone
    assert ("node/2", "node/4") not in by_id  # brand QID is not entity QID


def test_missing_name_chain_and_centroid_warning():
    features = [poi(1, 0, 0, name="x", tourism="hotel"),
                poi(2, 0.0006, 0, kind="way", name="x", tourism="hotel"),
                poi(3, 0.0012, 0, name="x", tourism="hotel"),
                poi(4, 0, 0, tourism="hotel"),
                poi(5, 0, 0, name="", tourism="hotel")]
    found = pairs(features)
    assert [p["pair_ids"] for p in found] == [["node/1", "way/2"], ["node/3", "way/2"]]
    assert all(p["location_precision"] == "approximate_centroid" for p in found)
    assert all("no_entrances" in p["warnings"][0] for p in found)


def test_geodesic_threshold_dateline_and_poles():
    geod = Geod(ellps="WGS84")
    lon, lat, _ = geod.fwd(10, 40, 90, 99)
    near = poi(1, 10, 40, name="x", amenity="cafe")
    assert len(pairs([near, poi(2, lon, lat, name="x", amenity="cafe")])) == 1
    lon, lat, _ = geod.fwd(10, 40, 90, 101)
    assert pairs([near, poi(2, lon, lat, name="x", amenity="cafe")]) == []
    for a, b in [((179.9999, 0), (-179.9999, 0)),
                 ((0, 89.9999), (180, 89.9999))]:
        assert len(pairs([poi(1, *a, name="x", amenity="cafe"),
                          poi(2, *b, name="x", amenity="cafe")])) == 1


@pytest.mark.parametrize("edit,code", [
    (lambda f: f.update(id="way/1"), "inconsistent_id"),
    (lambda f: f["properties"].update(osm_id=True), "invalid_id"),
    (lambda f: f["properties"].update(tags={"name": None}), "invalid_tags"),
    (lambda f: f["properties"].update(categories=None), "invalid_categories"),
    (lambda f: f["properties"].update(categories={}), "invalid_categories"),
    (lambda f: f["properties"].update(categories=[True]), "invalid_categories"),
    (lambda f: f["geometry"].update(coordinates=[float("nan"), 0]), "invalid_coordinates"),
    (lambda f: f["geometry"].update(coordinates=[float("inf"), 0]), "invalid_coordinates"),
    (lambda f: f["geometry"].update(coordinates=[True, 0]), "invalid_coordinates"),
    (lambda f: f["geometry"].update(coordinates=[181, 0]), "invalid_coordinates"),
    (lambda f: f["geometry"].update(coordinates=[0, -91]), "invalid_coordinates"),
])
def test_malformed(edit, code):
    feature = poi(1)
    edit(feature)
    with pytest.raises(ValueError, match=f"^{code}$"):
        pairs([feature])


def test_duplicate_id_and_invalid_radius():
    with pytest.raises(ValueError, match="^duplicate_id$"):
        pairs([poi(1), poi(1)])
    for radius in (-1, float("nan"), True):
        with pytest.raises(ValueError, match="^invalid_radius$"):
            pairs([], radius_m=radius)


def test_bounds_are_errors_not_silent_truncation(monkeypatch):
    monkeypatch.setattr(audit, "MAX_FEATURES", 1)
    with pytest.raises(ValueError, match="^too_many_features$"):
        pairs([poi(1), poi(2)])
    monkeypatch.setattr(audit, "MAX_FEATURES", 200000)
    features = [poi(i, name="x", amenity="cafe", wikidata="Q1") for i in range(1, 5)]
    monkeypatch.setattr(audit, "MAX_PAIR_COMPARISONS", 1)
    with pytest.raises(ValueError, match="^too_many_pair_comparisons$"):
        pairs(features)
    monkeypatch.setattr(audit, "MAX_PAIR_COMPARISONS", 250000)
    monkeypatch.setattr(audit, "MAX_CANDIDATE_PAIRS", 1)
    with pytest.raises(ValueError, match="^too_many_candidate_pairs$"):
        pairs(features)


def test_repeat_encounters_count_against_global_comparison_cap(monkeypatch):
    monkeypatch.setattr(audit, "MAX_PAIR_COMPARISONS", 1)
    with pytest.raises(ValueError, match="^too_many_pair_comparisons$"):
        pairs([poi(1, name="x", amenity="cafe", wikidata="Q1"),
               poi(2, name="x", amenity="cafe", wikidata="Q1")])


def test_spatial_candidates_match_brute_geodesic_oracle():
    import random

    rng = random.Random(20261024)
    geod = Geod(ellps="WGS84")
    features = []
    for _ in range(24):
        lon, lat = rng.uniform(-180, 180), rng.uniform(-89.999, 89.999)
        other_lon, other_lat, _ = geod.fwd(lon, lat, rng.uniform(0, 360), rng.uniform(0, 200))
        features.extend([poi(len(features) + 1, lon, lat, name="same", tourism="museum"),
                         poi(len(features) + 2, other_lon, other_lat, name="same", tourism="museum")])
    expected = set()
    for index, left in enumerate(features):
        for right in features[index + 1:]:
            distance = geod.inv(*left["geometry"]["coordinates"], *right["geometry"]["coordinates"])[2]
            if distance <= 100:
                expected.add(tuple(sorted((left["id"], right["id"]))))
    assert {tuple(pair["pair_ids"]) for pair in pairs(features)} == expected


def test_empty_input_and_exact_colocation_do_not_create_establishment_counts():
    report = audit.detect_poi_duplicates([])
    assert report["summary"] == {"raw_observation_count": 0, "candidate_pair_count": 0, "auto_merged_count": 0}
    assert report["unique_establishment_count_status"] == "not_computed"
    assert len(pairs([poi(1, name="x", amenity="cafe"), poi(2, name="x", amenity="cafe")], radius_m=0)) == 1
