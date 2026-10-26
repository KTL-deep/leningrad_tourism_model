"""Bounded audit of possible duplicate OSM POI observations; never merges them."""

import math
import re
import unicodedata
from collections import defaultdict

from pyproj import Geod


DUPLICATE_AUDIT_VERSION = "poi-duplicate-candidates-v1"
MAX_FEATURES = 200000
MAX_PAIR_COMPARISONS = 250000
MAX_CANDIDATE_PAIRS = 10000
_SEMANTIC_KEYS = ("amenity", "tourism", "historic", "leisure", "railway",
                  "public_transport", "shop", "highway")
_QID = re.compile(r"Q[1-9][0-9]*\Z")
_GEOD = Geod(ellps="WGS84")


def _normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(" " if unicodedata.category(c).startswith("P") else c
                            for c in value).split())


def _observation(feature):
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise ValueError("invalid_feature")
    props = feature.get("properties")
    if not isinstance(props, dict):
        raise ValueError("invalid_properties")
    kind, eid = props.get("osm_type"), props.get("osm_id")
    if kind not in ("node", "way", "relation") or type(eid) is not int or eid <= 0:
        raise ValueError("invalid_id")
    identifier = f"{kind}/{eid}"
    if feature.get("id") != identifier:
        raise ValueError("inconsistent_id")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValueError("invalid_geometry")
    coords = geometry.get("coordinates")
    if (not isinstance(coords, (list, tuple)) or len(coords) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in coords)
            or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90):
        raise ValueError("invalid_coordinates")
    tags = props.get("tags")
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in tags.items()):
        raise ValueError("invalid_tags")
    categories = props.get("categories")
    if not isinstance(categories, list) or any(not isinstance(value, str) or not value for value in categories):
        raise ValueError("invalid_categories")
    name = _normalized(tags.get("name", ""))
    semantic = frozenset((key, _normalized(tags[key])) for key in _SEMANTIC_KEYS
                         if key in tags and _normalized(tags[key]))
    qid = tags.get("wikidata")
    qid = qid if isinstance(qid, str) and _QID.fullmatch(qid) else None
    return {"id": identifier, "kind": kind, "lon": coords[0], "lat": coords[1],
            "name": name, "semantic": semantic, "qid": qid}


def _cell(lon, lat, size):
    longitude, latitude = math.radians(lon), math.radians(lat)
    x = math.cos(latitude) * math.cos(longitude)
    y = math.cos(latitude) * math.sin(longitude)
    z = math.sin(latitude)
    return tuple(math.floor(v / size) for v in (x, y, z))


def detect_poi_duplicates(features, *, radius_m=100.0):
    """Return deterministic candidate pairs; caps raise ValueError rather than drop evidence."""
    if (type(radius_m) not in (float, int) or not math.isfinite(radius_m)
            or radius_m < 0):
        raise ValueError("invalid_radius")
    if not isinstance(features, (list, tuple)):
        raise ValueError("invalid_features")
    if len(features) > MAX_FEATURES:
        raise ValueError("too_many_features")
    observations = [_observation(feature) for feature in features]
    observations.sort(key=lambda item: item["id"])
    if len({item["id"] for item in observations}) != len(observations):
        raise ValueError("duplicate_id")

    # A WGS84 geodesic of radius_m has a chord on the unit sphere no longer
    # than radius_m / 6,000,000 (below the minimum WGS84 curvature radius).
    size = max(radius_m / 6000000, 1e-12)
    entity_index = defaultdict(list)
    spatial_index = defaultdict(list)
    candidates = []
    comparisons = 0
    seen = set()

    def compare(left, right):
        nonlocal comparisons
        comparisons += 1  # Counts repeat encounters across indexes too.
        if comparisons > MAX_PAIR_COMPARISONS:
            raise ValueError("too_many_pair_comparisons")
        identity = (left["id"], right["id"])
        if identity in seen:
            return
        seen.add(identity)
        shared_qid = left["qid"] if left["qid"] and left["qid"] == right["qid"] else None
        matched_tags = dict(sorted(left["semantic"] & right["semantic"]))
        matched_name = bool(left["name"] and left["name"] == right["name"])
        if not shared_qid and not (matched_name and matched_tags):
            return
        distance = abs(_GEOD.inv(left["lon"], left["lat"], right["lon"], right["lat"])[2])
        if not shared_qid and distance > radius_m:
            return
        if len(candidates) >= MAX_CANDIDATE_PAIRS:
            raise ValueError("too_many_candidate_pairs")
        approximate = any(item["kind"] != "node" for item in (left, right))
        candidates.append({"pair_ids": [left["id"], right["id"]],
                            "evidence": {"shared_entity_wikidata": shared_qid,
                                         "left_entity_wikidata": left["qid"],
                                         "right_entity_wikidata": right["qid"],
                                         "matched_name": matched_name,
                                         "normalized_name": left["name"] if matched_name else None,
                                        "matched_semantic_tags": matched_tags,
                                        "distance_m": distance},
                           "status": "conflicting_entity_wikidata" if
                           left["qid"] and right["qid"] and left["qid"] != right["qid"]
                           else "candidate",
                           "location_precision": "approximate_centroid" if approximate else "point",
                           "warnings": ["way_relation_centroids_approximate_no_entrances"]
                           if approximate else []})

    for item in observations:
        if item["qid"]:
            for other in entity_index[item["qid"]]:
                compare(other, item)
            entity_index[item["qid"]].append(item)
        if item["name"] and item["semantic"]:
            cell = _cell(item["lon"], item["lat"], size)
            for semantic in sorted(item["semantic"]):
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for dz in (-1, 0, 1):
                            for other in spatial_index.get((item["name"], semantic,
                                                            (cell[0] + dx, cell[1] + dy, cell[2] + dz)), ()):
                                compare(other, item)
                spatial_index[(item["name"], semantic, cell)].append(item)
    candidates.sort(key=lambda pair: pair["pair_ids"])
    return {"schema_version": 1, "artifact_kind": "advisory_poi_duplicate_candidates",
             "algorithm_version": DUPLICATE_AUDIT_VERSION,
             "observations_modified": False, "unique_establishment_count_status": "not_computed",
            "configuration": {"radius_m": radius_m, "max_features": MAX_FEATURES,
                              "max_pair_comparisons": MAX_PAIR_COMPARISONS,
                              "max_candidate_pairs": MAX_CANDIDATE_PAIRS},
            "summary": {"raw_observation_count": len(observations),
                        "candidate_pair_count": len(candidates), "auto_merged_count": 0},
             "candidate_pairs": candidates,
             "limitations": [
                 "Candidate pairs are not confirmed duplicates; no observations are merged or removed.",
                 "Name radius is an uncalibrated review threshold, not a confidence probability.",
                 "Shared entity identifiers may refer to a complex or contain mapping errors.",
                 "Point/centroid distances do not establish entrances, access or physical facility identity.",
                 "Pairs are not joined transitively into establishments; absence of a pair proves no uniqueness.",
             ]}
