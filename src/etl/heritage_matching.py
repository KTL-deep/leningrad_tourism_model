"""Advisory official-record/OSM evidence links; never deduplicates facilities."""

from collections import defaultdict
import json
import math
import re
import unicodedata

from pyproj import Geod


HERITAGE_MATCHING_VERSION = "heritage-matching-v1"
MAX_RECORDS = 50000
MAX_POI = 200000
MAX_COMPARISONS = 250000
MAX_OUTPUT = 10000
_REGISTRY = re.compile(r"[0-9]{15}\Z")
_SPLIT = re.compile(r"[;,\s]+")
_GEOD = Geod(ellps="WGS84")
_REF_KEYS = ("ref:egrokn", "ref:ru:heritage")
_LIMITATIONS = [
    "A shared supplied registry number is identifier linkage, not proof of a physical facility or permission to visit.",
    "Name/distance candidates are for review only; no records or POIs are merged.",
    "OSM way/relation locations can be approximate centres, not entrances; point distances do not establish access.",
    "Input and query coverage are not geographic completeness, recall or a unique-facility count.",
]


def _copy_json(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_json_input") from exc


def _point(geometry):
    if geometry is None:
        return None
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValueError("invalid_geometry")
    coords = geometry.get("coordinates")
    if (not isinstance(coords, (list, tuple)) or len(coords) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in coords)
            or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90):
        raise ValueError("invalid_coordinates")
    return coords


def _name(value):
    return " ".join("".join(" " if unicodedata.category(c).startswith("P") else c
                            for c in unicodedata.normalize("NFKC", value).casefold()).split())


def _cell(lon, lat, size):
    longitude, latitude = math.radians(lon), math.radians(lat)
    return tuple(math.floor(v / size) for v in (math.cos(latitude) * math.cos(longitude),
                                                math.cos(latitude) * math.sin(longitude),
                                                math.sin(latitude)))


def _official(record):
    if not isinstance(record, dict):
        raise ValueError("invalid_record")
    source_id, registry, name = (record.get(key) for key in
                                 ("source_record_id", "registry_number", "name"))
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("invalid_source_record_id")
    if registry is not None and (not isinstance(registry, str) or not _REGISTRY.fullmatch(registry)):
        raise ValueError("invalid_registry_number")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("invalid_name")
    if "geometry" not in record:
        raise ValueError("invalid_geometry")
    point = _point(record["geometry"])
    if "source_url" in record and not isinstance(record["source_url"], str):
        raise ValueError("invalid_source_url")
    return {"id": source_id, "registry": registry, "name": _name(name), "point": point,
            "record": _copy_json(record)}


def _poi(feature):
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
    point = _point(feature.get("geometry"))
    tags = props.get("tags")
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in tags.items()):
        raise ValueError("invalid_tags")
    return {"id": identifier, "name": _name(tags.get("name", "")), "point": point,
            "tags": tags, "precision": props.get("location_precision") or
            ("exact_source" if kind == "node" else "osm_bbox_center" if point else "unknown"),
            "feature": _copy_json(feature)}


def _evidence(record, poi):
    distance = (abs(_GEOD.inv(*record["point"], *poi["point"])[2])
                if record["point"] is not None and poi["point"] is not None else None)
    return {"official_name": record["record"]["name"],
            "osm_name": poi["tags"].get("name"),
            "normalized_name_equal": bool(record["name"] and record["name"] == poi["name"]),
            "distance_m": distance,
            "official_location_precision": (record["record"].get("location_precision") or "source_point")
            if record["point"] else "unlocated",
            "osm_location_precision": poi["precision"],
            "warnings": (["source_point_may_be_approximate_no_entrance_or_access_claim"]
                         if record["point"] else ["official_unlocated_no_distance_or_access_claim"])
            + (["osm_way_relation_centre_approximate_no_entrance_or_access_claim"]
               if poi["id"].split("/")[0] != "node" else [])}


def match_heritage(official_records: list, poi_features: list, radius_m=100.0) -> dict:
    """Link explicit registry IDs and report nearby exact-name review candidates.

    Inputs are caller-wired official records and E03 POI Features; all caps fail closed.
    Non-Point official source geometries must be mapped by the caller to None with a warning.
    """
    if type(radius_m) not in (int, float) or not math.isfinite(radius_m) or radius_m < 0:
        raise ValueError("invalid_radius")
    if not isinstance(official_records, list) or not isinstance(poi_features, list):
        raise ValueError("invalid_inputs")
    if len(official_records) > MAX_RECORDS:
        raise ValueError("too_many_records")
    if len(poi_features) > MAX_POI:
        raise ValueError("too_many_poi")
    records = sorted((_official(item) for item in official_records), key=lambda x: x["id"])
    pois = sorted((_poi(item) for item in poi_features), key=lambda x: x["id"])
    if len({x["id"] for x in records}) != len(records):
        raise ValueError("duplicate_source_record_id")
    if len({x["id"] for x in pois}) != len(pois):
        raise ValueError("duplicate_poi_id")
    refs = defaultdict(list)
    names_unlocated = defaultdict(list)
    names_located = defaultdict(list)
    located_by_name = defaultdict(list)
    size = max(radius_m / 6000000, 1e-12)
    for record in records:
        if record["registry"]:
            refs[record["registry"]].append(record)
        if record["name"]:
            if record["point"] is None:
                names_unlocated[record["name"]].append(record)
            else:
                cell = _cell(*record["point"], size)
                names_located[(record["name"], cell)].append(record)
                located_by_name[record["name"]].append(record)

    links, candidates, invalid, unmatched = [], [], [], []
    comparisons = 0

    def encounter():
        nonlocal comparisons
        comparisons += 1  # Every index encounter counts, including repeat evidence for a pair.
        if comparisons > MAX_COMPARISONS:
            raise ValueError("too_many_comparisons")

    def emit(target, item):
        if len(links) + len(candidates) + len(invalid) + len(unmatched) >= MAX_OUTPUT:
            raise ValueError("too_many_output_entries")
        target.append(item)

    for poi in pois:
        valid = set()
        for key in _REF_KEYS:
            if key in poi["tags"]:
                tokens = _SPLIT.split(poi["tags"][key].strip())
                for token in tokens:
                    if _REGISTRY.fullmatch(token):
                        valid.add(token)
                    else:
                        emit(invalid, {"poi_id": poi["id"], "tag": key, "token": token})
        linked = set()
        for number in sorted(valid):
            if not refs[number]:
                emit(unmatched, {"poi_id": poi["id"], "registry_number": number})
            for record in refs[number]:
                encounter()
                linked.add(record["id"])
                evidence = _evidence(record, poi)
                emit(links, {"source_record_id": record["id"], "poi_id": poi["id"],
                             "registry_number": number,
                             "status": "ambiguous_identifier" if len(refs[number]) > 1 else "identifier_link",
                             "conflicts": {"name": bool(poi["name"] and record["name"] and
                                                       poi["name"] != record["name"]),
                                           "distance_exceeds_radius": (evidence["distance_m"] is not None
                                                                       and evidence["distance_m"] > radius_m)},
                             "evidence": evidence})
        if not poi["name"]:
            continue
        for record in names_unlocated[poi["name"]]:
            encounter()
            if record["id"] not in linked:
                emit(candidates, {"source_record_id": record["id"], "poi_id": poi["id"],
                                  "status": "name_only_unlocated_candidate",
                                  "evidence": _evidence(record, poi)})
        if poi["point"] is None:
            for record in located_by_name[poi["name"]]:
                encounter()
                if record["id"] not in linked:
                    emit(candidates, {"source_record_id": record["id"], "poi_id": poi["id"],
                                      "status": "name_only_unlocated_candidate",
                                      "evidence": _evidence(record, poi)})
            continue
        cell = _cell(*poi["point"], size)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for record in names_located.get((poi["name"],
                                                     (cell[0] + dx, cell[1] + dy, cell[2] + dz)), ()):
                        encounter()
                        if record["id"] in linked:
                            continue
                        evidence = _evidence(record, poi)
                        if evidence["distance_m"] <= radius_m:
                            emit(candidates, {"source_record_id": record["id"], "poi_id": poi["id"],
                                              "status": "name_distance_review_candidate", "evidence": evidence})
    links.sort(key=lambda x: (x["source_record_id"], x["poi_id"], x["registry_number"]))
    candidates.sort(key=lambda x: (x["source_record_id"], x["poi_id"]))
    invalid.sort(key=lambda x: (x["poi_id"], x["tag"], x["token"]))
    unmatched.sort(key=lambda x: (x["poi_id"], x["registry_number"]))
    return {"schema_version": 1, "artifact_kind": "advisory_heritage_matching",
            "algorithm_version": HERITAGE_MATCHING_VERSION,
            "configuration": {"radius_m": radius_m, "max_records": MAX_RECORDS,
                              "max_poi": MAX_POI, "max_comparisons": MAX_COMPARISONS,
                              "max_output_entries": MAX_OUTPUT},
            "official_records": [x["record"] for x in records],
            "poi_features": [x["feature"] for x in pois],
            "identifier_links": links, "review_candidates": candidates,
            "invalid_ref_tokens": invalid, "unmatched_valid_refs": unmatched,
            "summary": {"input_official_records": len(records), "input_poi_features": len(pois),
                        "official_with_registry_number": sum(x["registry"] is not None for x in records),
                        "official_with_point": sum(x["point"] is not None for x in records),
                        "poi_with_point": sum(x["point"] is not None for x in pois),
                        "ambiguous_registry_numbers": sorted(k for k, v in refs.items() if len(v) > 1),
                        "identifier_link_count": len(links), "review_candidate_count": len(candidates),
                        "comparisons": comparisons, "auto_merged_count": 0},
            "limitations": list(_LIMITATIONS)}
