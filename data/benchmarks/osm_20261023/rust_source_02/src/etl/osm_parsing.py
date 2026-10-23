"""Offline parsing of a counted Overpass POI and raw highway topology response."""

from dataclasses import dataclass
from datetime import datetime
import json
import math
import re

from pyproj import Geod
from shapely.geometry import Point, shape

from src.data_models.study_area import StudyArea


MAX_ELEMENTS = 200_000
_GEOD = Geod(ellps="WGS84")
_COUNT = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_AMENITY = {
    "food": {"cafe", "restaurant", "fast_food", "bar", "pub", "food_court"},
    "sanitary": {"toilets", "drinking_water"},
    "transport": {"parking", "bus_station", "ferry_terminal"},
    "healthcare": {"hospital", "clinic", "pharmacy"},
}


class OSMParseError(ValueError):
    """Invalid or incomplete Overpass response, with a machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OSMInventory:
    """Inventory whose serialized representation cannot be mutated by callers."""

    _json: str

    def to_dict(self) -> dict:
        return json.loads(self._json)


def _id(value) -> int:
    if type(value) is not int or value <= 0 or value > 2**64 - 1:
        raise OSMParseError("invalid_id")
    return value


def _point(value: dict) -> list:
    if not isinstance(value, dict):
        raise OSMParseError("invalid_coordinates")
    lat, lon = value.get("lat"), value.get("lon")
    try:
        invalid = (any(type(v) not in (int, float) or not math.isfinite(v) for v in (lat, lon))
                   or not -90 <= lat <= 90 or not -180 <= lon <= 180)
    except (OverflowError, TypeError):
        invalid = True
    if invalid:
        raise OSMParseError("invalid_coordinates")
    return [lon, lat]


def _categories(tags: dict) -> list[str]:
    categories = [name for name, values in _AMENITY.items() if tags.get("amenity") in values]
    if isinstance(tags.get("tourism"), str) and tags["tourism"] not in ("", "no"):
        categories.append("tourism")
    if isinstance(tags.get("historic"), str) and tags["historic"] not in ("", "no"):
        categories.append("historic")
    if tags.get("leisure") in ("park", "garden", "nature_reserve"):
        categories.append("leisure")
    if tags.get("railway") in ("station", "halt"):
        categories.append("transport")
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") in ("station", "platform"):
        categories.append("transport")
    if tags.get("shop") in ("supermarket", "convenience"):
        categories.append("shop")
    return list(dict.fromkeys(categories))


def _direction(tags: dict) -> str:
    oneway = tags.get("oneway")
    if oneway in ("yes", "true", "1"):
        return "forward"
    if oneway == "-1":
        return "reverse"
    if oneway in ("no", "false", "0"):
        return "both"
    if oneway is None and tags.get("junction") == "roundabout":
        return "implicit_roundabout"
    return "unknown"


def parse_overpass(content: bytes, study_area: StudyArea) -> OSMInventory:
    """Reject uncounted/truncated responses rather than interpreting absence as zero."""
    if not isinstance(study_area, StudyArea):
        raise OSMParseError("invalid_study_area")
    try:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        data = json.loads(content.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (TypeError, UnicodeError, ValueError) as exc:
        raise OSMParseError("invalid_json") from exc
    if not isinstance(data, dict) or data.get("version") != 0.6 or type(data.get("version")) is bool:
        raise OSMParseError("invalid_schema")
    if "remark" in data:
        raise OSMParseError("overpass_remark")
    osm3s = data.get("osm3s")
    timestamp = osm3s.get("timestamp_osm_base") if isinstance(osm3s, dict) else None
    try:
        if not isinstance(timestamp, str):
            raise ValueError("missing timestamp")
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
    except ValueError as exc:
        raise OSMParseError("invalid_timestamp") from exc
    elements = data.get("elements")
    if not isinstance(elements, list):
        raise OSMParseError("invalid_schema")
    if len(elements) > MAX_ELEMENTS + 1:
        raise OSMParseError("too_many_elements")
    counts = [e for e in elements if isinstance(e, dict) and e.get("type") == "count"]
    if len(counts) != 1 or elements[-1] is not counts[0]:
        raise OSMParseError("missing_count")
    count = counts[0]
    tags = count.get("tags")
    required_counts = {"nodes", "ways", "relations", "total"}
    if (count.get("id") != 0 or type(count.get("id")) is not int or not isinstance(tags, dict)
            or not required_counts <= set(tags) or set(tags) - required_counts - {"areas"}):
        raise OSMParseError("invalid_count")
    if any(not isinstance(v, str) or not _COUNT.fullmatch(v) for v in tags.values()):
        raise OSMParseError("invalid_count")
    if any(len(v) > len(str(MAX_ELEMENTS)) or int(v) > MAX_ELEMENTS for v in tags.values()):
        raise OSMParseError("too_many_elements")
    if "areas" in tags and tags["areas"] != "0":
        raise OSMParseError("unsupported_areas")
    if len(elements) - 1 > MAX_ELEMENTS:
        raise OSMParseError("too_many_elements")
    unique: dict[tuple[str, int], dict] = {}
    for element in elements[:-1]:
        if not isinstance(element, dict) or element.get("type") not in ("node", "way", "relation"):
            raise OSMParseError("invalid_element")
        kind = element["type"]
        key = kind, _id(element.get("id"))
        raw_tags = element.get("tags", {})
        if not isinstance(raw_tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw_tags.items()):
            raise OSMParseError("invalid_tags")
        if kind == "node":
            _point(element)
        if "center" in element:
            _point(element["center"])
        if kind == "way":
            refs = element.get("nodes")
            if not isinstance(refs, list) or not refs:
                raise OSMParseError("invalid_node_refs")
            for ref in refs:
                _id(ref)
        if kind == "relation":
            members = element.get("members")
            if not isinstance(members, list):
                raise OSMParseError("invalid_members")
            for member in members:
                if (not isinstance(member, dict) or member.get("type") not in ("node", "way", "relation")
                        or not isinstance(member.get("role", ""), str)):
                    raise OSMParseError("invalid_members")
                _id(member.get("ref"))
        if key in unique and element != unique[key]:
            raise OSMParseError("conflicting_duplicate")
        unique[key] = element
    actual = {kind: sum(k[0] == kind for k in unique) for kind in ("node", "way", "relation")}
    if (any(int(tags[kind + "s"]) != actual[kind] for kind in actual)
            or int(tags["total"]) != len(unique)
            or int(tags["total"]) != sum(actual.values())):
        raise OSMParseError("count_mismatch")
    nodes = {eid: _point(e) for (kind, eid), e in unique.items() if kind == "node"}
    for (kind, _), element in unique.items():
        if kind == "way" and any(ref not in nodes for ref in element["nodes"]):
            raise OSMParseError("missing_node_ref")

    request = shape(study_area.request_geometry)
    pois, network = [], []
    missing_centers = 0
    for (kind, eid), element in unique.items():
        raw_tags = element.get("tags", {})
        categories = _categories(raw_tags)
        if categories:
            coords = nodes[eid] if kind == "node" else (
                _point(element["center"]) if "center" in element else None)
            if coords is None:
                missing_centers += 1
            pois.append({"type": "Feature", "id": f"{kind}/{eid}",
                         "geometry": {"type": "Point", "coordinates": coords} if coords else None,
                         "properties": {"osm_type": kind, "osm_id": eid, "categories": categories,
                                         "tags": raw_tags, "location_precision": "exact_source" if kind == "node" else "osm_bbox_center" if coords else None,
                                         "location_status": "observed" if kind == "node" else "estimated" if coords else "unknown",
                                         "in_request": request.covers(Point(coords)) if kind == "node" else None,
                                         "center_in_request": request.covers(Point(coords)) if kind != "node" and coords else None,
                                         "quality_flags": ["missing_center"] if coords is None else [],
                                         "capacity": None, "capacity_status": "unknown",
                                         "status": "unknown", "public_access": "unknown"}})
        if kind == "way" and "highway" in raw_tags:
            refs = element["nodes"]
            if len(refs) < 2:
                raise OSMParseError("invalid_node_refs")
            coords = [nodes[ref] for ref in refs]
            lengths = [_GEOD.inv(*start, *end)[2] for start, end in zip(coords, coords[1:])]
            network.append({"type": "Feature", "id": f"way/{eid}",
                            "geometry": {"type": "LineString", "coordinates": coords},
                            "properties": {"osm_type": "way", "osm_id": eid, "node_ids": refs,
                                           "tags": raw_tags, "segment_lengths_m": lengths,
                                            "length_m": sum(lengths), "time_min": None,
                                            "direction_status": _direction(raw_tags),
                                            "extends_request": any(not request.covers(Point(c)) for c in coords),
                                            "access": raw_tags.get("access")}})
    network_nodes = {ref for feature in network for ref in feature["properties"]["node_ids"]}
    segments = sum(len(feature["properties"]["node_ids"]) - 1 for feature in network)
    result = {"schema_version": 1, "osm_base_timestamp": parsed.isoformat(),
              "element_count": len(unique),
              "poi": {"type": "FeatureCollection", "features": pois},
              "network": {"type": "FeatureCollection", "features": network},
               "quality": {"counts": {**actual, "poi": len(pois), "network": len(network), "missing_centers": missing_centers,
                                      "network_nodes": len(network_nodes), "network_segments": segments},
                          "warnings": ["missing_center"] if missing_centers else []},
              "real_world_poi_completeness": "unknown", "coverage_scope": "query_bbox_with_full_way_nodes",
              "routing_ready": False, "surrounding_adequacy": "not_validated"}
    return OSMInventory(json.dumps(result, ensure_ascii=False, allow_nan=False))
