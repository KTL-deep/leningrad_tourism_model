"""Advisory official-record/OSM evidence links; never deduplicates facilities."""

from collections import defaultdict
import json
import math
import re
import unicodedata

from pyproj import Geod

from src.etl.heritage_locality import compare_locality_context, validate_locality_context

HERITAGE_MATCHING_VERSION = "heritage-matching-v4"
MAX_RECORDS = 50000
MAX_POI = 200000
MAX_COMPARISONS = 250000
MAX_OUTPUT = 10000
_REGISTRY = re.compile(r"[0-9]{15}\Z")
_SPLIT = re.compile(r"[;,\s]+")
_GEOD = Geod(ellps="WGS84")
_REF_KEYS = ("ref:egrokn", "ref:ru:heritage")
_OKN_KEY = "ref:okn"
_OKN_WARNING = "ref_okn_namespace_length_documentation_conflict"
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
    context = record.get("address_locality_context")
    if context is not None:
        context = validate_locality_context(context)
    return {"id": source_id, "registry": registry, "name": _name(name), "point": point,
            "context": context, "record": _copy_json(record)}


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


def _subject_role(tags):
    object_markers = ("heritage", "historic", "building")
    if any(tags.get(key) and tags[key].lower() not in ("no", "0") for key in object_markers) or (
            tags.get("amenity") == "place_of_worship"):
        return "mixed_or_object_evidence"
    if tags.get("tourism") == "information" or tags.get("information") in ("board", "guidepost", "map"):
        return "information_about_feature"
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") in ("platform", "stop_position"):
        return "transport_stop"
    return "object_or_unspecified"


def _evidence(record, poi):
    distance = (abs(_GEOD.inv(*record["point"], *poi["point"])[2])
                if record["point"] is not None and poi["point"] is not None else None)
    comparison = (compare_locality_context(record["context"], poi["point"])
                  if record["context"] is not None else {"status": "unavailable"})
    return {"official_name": record["record"]["name"],
            "official_address": record["record"].get("source_address"),
            "official_address_locality_context": record["context"],
            "address_locality_comparison": comparison,
            "osm_name": poi["tags"].get("name"),
            "osm_subject_role": _subject_role(poi["tags"]),
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

    links, candidates, excluded, invalid, unmatched = [], [], [], [], []
    okn_links = okn_only_links = 0
    comparisons = 0

    def encounter():
        nonlocal comparisons
        comparisons += 1  # Every index encounter counts, including repeat evidence for a pair.
        if comparisons > MAX_COMPARISONS:
            raise ValueError("too_many_comparisons")

    def emit(target, item):
        if len(links) + len(candidates) + len(excluded) + len(invalid) + len(unmatched) >= MAX_OUTPUT:
            raise ValueError("too_many_output_entries")
        target.append(item)

    for poi in pois:
        valid = defaultdict(set)
        for key in (*_REF_KEYS, _OKN_KEY):
            if key in poi["tags"]:
                tokens = _SPLIT.split(poi["tags"][key].strip())
                for token in tokens:
                    if _REGISTRY.fullmatch(token):
                        valid[token].add(key)
                    else:
                        entry = {"poi_id": poi["id"], "tag": key, "token": token}
                        if key == _OKN_KEY:
                            entry["reason"] = "ref_okn_not_national_15_digit"
                        emit(invalid, entry)
        linked = set()
        known_numbers = {number for number in valid if refs.get(number)}
        for number in sorted(valid):
            tags = valid[number]
            matching_records = refs.get(number, ())
            if not matching_records:
                entry = {"poi_id": poi["id"], "registry_number": number}
                if tags == {_OKN_KEY}:
                    entry.update(tag=_OKN_KEY, namespace_status="unverified")
                emit(unmatched, entry)
            for record in matching_records:
                encounter()
                linked.add(record["id"])
                evidence = _evidence(record, poi)
                evidence["supporting_ref_tags"] = [{"tag": key, "value": number}
                                                   for key in (*_REF_KEYS, _OKN_KEY) if key in tags]
                evidence["identifier_namespace_status"] = (
                    "explicit_national_ref" if tags.intersection(_REF_KEYS)
                    else "ref_okn_exact_registry_value_namespace_unverified")
                if _OKN_KEY in tags:
                    evidence["warnings"].append(_OKN_WARNING)
                    okn_links += 1
                    if not tags.intersection(_REF_KEYS):
                        okn_only_links += 1
                if evidence["osm_subject_role"] in ("information_about_feature", "transport_stop"):
                    evidence["warnings"].append(
                        "identifier_reference_on_non_object_subject_not_physical_identity")
                if evidence["address_locality_comparison"]["status"] == "outside_guarded_context_envelope":
                    evidence["warnings"].append(
                        "identifier_reference_address_locality_conflict_not_physical_identity")
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
        def emit_candidate(record, status, evidence):
            if evidence["osm_subject_role"] in ("information_about_feature", "transport_stop"):
                reason = "osm_non_object_subject_role"
            elif (status == "name_only_unlocated_candidate" and known_numbers
                  and record["registry"] is not None and record["registry"] not in known_numbers):
                reason = "unlocated_name_conflicts_with_explicit_registry_reference"
            elif (status == "name_only_unlocated_candidate" and record["point"] is None
                  and evidence["address_locality_comparison"]["status"]
                  == "outside_guarded_context_envelope"):
                reason = "unlocated_address_locality_conflict"
            else:
                reason = None
            if reason:
                emit(excluded, {"source_record_id": record["id"], "poi_id": poi["id"],
                                "status": "excluded_name_candidate", "reason": reason,
                                "evidence": evidence})
            else:
                emit(candidates, {"source_record_id": record["id"], "poi_id": poi["id"],
                                  "status": status, "evidence": evidence})

        for record in names_unlocated[poi["name"]]:
            encounter()
            if record["id"] not in linked:
                emit_candidate(record, "name_only_unlocated_candidate", _evidence(record, poi))
        if poi["point"] is None:
            for record in located_by_name[poi["name"]]:
                encounter()
                if record["id"] not in linked:
                    emit_candidate(record, "name_only_unlocated_candidate", _evidence(record, poi))
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
                            emit_candidate(record, "name_distance_review_candidate", evidence)
    links.sort(key=lambda x: (x["source_record_id"], x["poi_id"], x["registry_number"]))
    candidates.sort(key=lambda x: (x["source_record_id"], x["poi_id"]))
    excluded.sort(key=lambda x: (x["source_record_id"], x["poi_id"]))
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
            "excluded_review_candidates": excluded,
            "invalid_ref_tokens": invalid, "unmatched_valid_refs": unmatched,
            "summary": {"input_official_records": len(records), "input_poi_features": len(pois),
                        "official_with_registry_number": sum(x["registry"] is not None for x in records),
                        "official_with_point": sum(x["point"] is not None for x in records),
                        "poi_with_point": sum(x["point"] is not None for x in pois),
                        "ambiguous_registry_numbers": sorted(k for k, v in refs.items() if len(v) > 1),
                        "identifier_link_count": len(links),
                        "ref_okn_identifier_link_count": okn_links,
                        "ref_okn_only_identifier_link_count": okn_only_links,
                        "review_candidate_count": len(candidates),
                        "excluded_review_candidate_count": len(excluded),
                        "exclusion_counts": {
                            reason: sum(x["reason"] == reason for x in excluded)
                            for reason in ("osm_non_object_subject_role",
                                           "unlocated_name_conflicts_with_explicit_registry_reference",
                                           "unlocated_address_locality_conflict")},
                        "comparisons": comparisons, "auto_merged_count": 0},
            "limitations": list(_LIMITATIONS)}
