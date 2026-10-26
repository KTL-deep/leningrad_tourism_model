"""Stream node/way observations from PBF; use osmium's disk-backed node index."""

import json
from datetime import datetime, timezone
from pathlib import Path
import time

from pyproj import Geod
from shapely.geometry import LineString, Point, Polygon, shape
from shapely import wkb
from shapely.errors import GEOSException
from shapely.ops import unary_union

from src.etl.osm_parsing import _categories, _direction

MAX_FEATURES = 200000
MAX_FEATURE_BYTES = 64 * 1024 * 1024
MAX_PARSE_SECONDS = 1800
# Explicit safety cap for tracked relation members and their node references;
# exceeding it fails extraction rather than silently dropping observations.
MAX_RELATION_CANDIDATES = 200000
PARSER_VERSION_RELATIONS = "geofabrik-node-way-multipolygon-observations-v2"


def extract_layers(pbf: Path, area, directory: Path, *, native_tag_filter: bool = False,
                   way_bbox_prefilter: bool = False, include_relations: bool = False):
    """Extract observations; opt-in native tag filtering is a benchmark control.

    Libosmium's location handler sees all nodes before these filters run, so
    complete way coordinates (including outside the requested area) survive.
    Geometry/category checks below remain identical in either mode.
    """
    if any(type(flag) is not bool for flag in (native_tag_filter, way_bbox_prefilter, include_relations)):
        raise ValueError("benchmark filter flags must be bool")
    if include_relations and native_tag_filter:
        raise ValueError("relation_native_tag_filter_unsupported")
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError("missing_osmium_dependency: install osmium") from exc

    directory.mkdir(parents=True, exist_ok=True)
    request = shape(area.analysis_geometry or area.request_geometry)
    south, west, north, east = request.bounds[1], request.bounds[0], request.bounds[3], request.bounds[2]
    # OSM IDs are global and sparse: a dense array scales with the largest ID
    # (hundreds of GB), not the number of nodes in this regional extract.
    # sparse_file_array is disk-backed and scales with actual regional nodes.
    geod = Geod(ellps="WGS84")
    paths = {role: directory / (role + ".features") for role in ("poi", "network")}
    counts = {"poi": 0, "network": 0, "missing_way_locations": 0, "relations_unsupported": True}
    files = {}
    started = time.monotonic()
    written_bytes = 0

    def check_deadline():
        if time.monotonic() - started > MAX_PARSE_SECONDS:
            raise ValueError("extract_parse_timeout")

    candidates = {}
    member_ids = set()
    if include_relations:
        counts.update(parser_version=PARSER_VERSION_RELATIONS, relation_poi=0,
                      relation_candidates=0, relation_unassembled=0,
                      relation_invalid=0, relation_unsupported_poi=0,
                      relation_failure_scope="source_extract")

        class CandidateHandler(osmium.SimpleHandler):
            def relation(self, relation):
                check_deadline()
                tags = dict(relation.tags)
                if not _categories(tags):
                    return
                if tags.get("type") != "multipolygon":
                    counts["relation_unsupported_poi"] += 1
                    return
                counts["relation_candidates"] += 1
                if any(m.type != "w" or m.role not in ("outer", "inner") for m in relation.members):
                    counts["relation_invalid"] += 1
                    return
                roles = {}
                for member in relation.members:
                    if member.ref in roles and roles[member.ref] != member.role:
                        counts["relation_invalid"] += 1
                        return
                    roles[member.ref] = member.role
                if not roles or "outer" not in roles.values():
                    counts["relation_invalid"] += 1
                    return
                new_members = roles.keys() - member_ids
                if len(candidates) >= MAX_RELATION_CANDIDATES or len(member_ids) + len(new_members) > MAX_RELATION_CANDIDATES:
                    raise ValueError("extract_relation_candidates_too_large")
                candidates[relation.id] = {"tags": tags, "roles": roles}
                member_ids.update(roles)

        CandidateHandler().apply_file(osmium.io.File(str(pbf), "pbf"))

    seen_members = {}
    member_node_count = 0
    assembled = {}
    assembled_bytes = 0
    failed_assembly = set()
    factory = osmium.geom.WKBFactory()

    def write(role, feature):
        nonlocal written_bytes
        content = json.dumps(feature, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
        written_bytes += len(content.encode("utf-8"))
        if written_bytes > MAX_FEATURE_BYTES or counts["poi"] + counts["network"] >= MAX_FEATURES:
            raise ValueError("extract_observations_too_large")
        files[role].write(content)
        counts[role] += 1

    class Handler(osmium.SimpleHandler):
        def node(self, node):
            check_deadline()
            if not node.location.valid():
                return
            lon, lat = node.location.lon, node.location.lat
            if not west <= lon <= east or not south <= lat <= north:
                return
            tags = dict(node.tags)
            categories = _categories(tags)
            if categories and request.covers(Point(lon, lat)):
                write("poi", {"type": "Feature", "id": f"node/{node.id}",
                              "geometry": {"type": "Point", "coordinates": [lon, lat]},
                              "properties": {"osm_type": "node", "osm_id": node.id, "tags": tags,
                                             "categories": categories, "location_precision": "exact_source",
                                             "public_access": "unknown", "capacity_status": "unknown"}})

        def way(self, way):
            nonlocal member_node_count
            check_deadline()
            if include_relations and way.id in member_ids:
                if len(way.nodes) >= 2 and all(ref.location.valid() for ref in way.nodes):
                    member_node_count += len(way.nodes) - len(seen_members.get(way.id) or ())
                    if member_node_count > MAX_RELATION_CANDIDATES:
                        raise ValueError("extract_relation_candidates_too_large")
                    coords_member = [(ref.location.lon, ref.location.lat) for ref in way.nodes]
                    seen_members[way.id] = coords_member
                else:
                    seen_members[way.id] = None
            tags = dict(way.tags)
            categories = _categories(tags)
            if not categories and "highway" not in tags:
                return
            try:
                # InvalidLocationError is not a ValueError/RuntimeError. Check
                # validity before accessing lon/lat, so missing refs follow the
                # documented counted-skip path instead of escaping the parser.
                if len(way.nodes) < 2 or not all(ref.location.valid() for ref in way.nodes):
                    counts["missing_way_locations"] += 1
                    return
                coords = [[ref.location.lon, ref.location.lat] for ref in way.nodes]
            except (ValueError, RuntimeError):
                counts["missing_way_locations"] += 1
                return
            if way_bbox_prefilter:
                # Conservative only: a crossing segment or an enclosing polygon
                # need not have any vertex inside the requested geometry.
                xs, ys = zip(*coords)
                if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                    return
            line = LineString(coords)
            is_road = "highway" in tags and line.intersects(request)
            poi_geometry = line
            if categories and len(coords) >= 4 and coords[0] == coords[-1] and tags.get("area") != "no":
                polygon = Polygon(coords)
                if polygon.is_valid:
                    poi_geometry = polygon
            is_poi = bool(categories) and poi_geometry.intersects(request)
            if not is_road and not is_poi:
                return
            if is_poi:
                center = poi_geometry.centroid
                write("poi", {"type": "Feature", "id": f"way/{way.id}",
                              "geometry": {"type": "Point", "coordinates": [center.x, center.y]},
                              "properties": {"osm_type": "way", "osm_id": way.id, "tags": tags,
                                              "categories": categories, "location_precision": "approximate_way_centroid",
                                             "center_in_request": request.covers(center),
                                             "public_access": "unknown", "capacity_status": "unknown"}})
            if is_road:
                lengths = [geod.inv(*start, *end)[2] for start, end in zip(coords, coords[1:])]
                write("network", {"type": "Feature", "id": f"way/{way.id}",
                                  "geometry": {"type": "LineString", "coordinates": coords},
                                  "properties": {"osm_type": "way", "osm_id": way.id,
                                                 "node_ids": [ref.ref for ref in way.nodes], "tags": tags,
                                                 "segment_lengths_m": lengths, "length_m": sum(lengths),
                                                 "direction_status": _direction(tags), "time_min": None,
                                                 "extends_request": not request.covers(line),
                                                 "access": tags.get("access")}})

    class RelationHandler(Handler):
        def area(self, area_obj):
            nonlocal assembled_bytes
            check_deadline()
            if area_obj.from_way() or area_obj.orig_id() not in candidates:
                return
            identifier = area_obj.orig_id()
            try:
                data = factory.create_multipolygon(area_obj)
                if len(data) + assembled_bytes > MAX_FEATURE_BYTES:
                    raise ValueError("extract_observations_too_large")
                if identifier in assembled:
                    raise ValueError("extract_relation_duplicate_area")
                assembled[identifier] = data
                assembled_bytes += len(data)
            except (RuntimeError, ValueError) as exc:
                if str(exc) in ("extract_observations_too_large", "extract_relation_duplicate_area"):
                    raise
                failed_assembly.add(identifier)

    try:
        # Open output handles only after the candidate prepass succeeds. Keep
        # opening inside the cleanup guard in case opening a later layer fails.
        for role, path in paths.items():
            files[role] = path.open("w", encoding="utf-8")
        handler = RelationHandler() if include_relations else Handler()
        # Content-addressed cache paths end in a SHA-256, not .pbf. Never rely
        # on extension-based format inference for the verified PBF source.
        filters = [osmium.filter.KeyFilter(
            "tourism", "historic", "amenity", "leisure", "shop", "railway", "highway", "public_transport"
        )] if native_tag_filter else []
        handler.apply_file(osmium.io.File(str(pbf), "pbf"), locations=True,
                            idx="sparse_file_array", filters=filters)
        if include_relations:
            for identifier, candidate in candidates.items():
                check_deadline()
                data = assembled.get(identifier)
                if data is None:
                    counts["relation_invalid" if identifier in failed_assembly else "relation_unassembled"] += 1
                    continue
                try:
                    geometry = wkb.loads(data)
                    if not geometry.is_valid or geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
                        counts["relation_invalid"] += 1
                        continue
                    polygons = [geometry] if geometry.geom_type == "Polygon" else geometry.geoms
                    # Libosmium may repair a detached inner ring into an outer.
                    # Compare *each role's* complete source linework to the
                    # corresponding assembled rings; no role repair or missing
                    # component can be accepted as a valid relation POI.
                    source = {role: [] for role in ("outer", "inner")}
                    for ref, role in candidate["roles"].items():
                        coords = seen_members.get(ref)
                        if coords is None:
                            break
                        source[role].append(LineString(coords))
                    else:
                        result = {
                            "outer": [polygon.exterior for polygon in polygons],
                            "inner": [ring for polygon in polygons for ring in polygon.interiors],
                        }
                        valid_roles = all(unary_union(source[role]).equals(unary_union(result[role]))
                                          for role in ("outer", "inner"))
                    if len(source["outer"]) + len(source["inner"]) != len(candidate["roles"]):
                        valid_roles = False
                    if valid_roles:
                        intersects = geometry.intersects(request)
                        if intersects:
                            center = geometry.centroid
                            center_in_request = request.covers(center)
                except (ValueError, RuntimeError, GEOSException):
                    counts["relation_invalid"] += 1
                    continue
                if valid_roles:
                    if not intersects:
                        continue
                    write("poi", {"type": "Feature", "id": f"relation/{identifier}",
                                  "geometry": {"type": "Point", "coordinates": [center.x, center.y]},
                                  "properties": {"osm_type": "relation", "osm_id": identifier,
                                                 "tags": candidate["tags"],
                                                 "categories": _categories(candidate["tags"]),
                                                 "location_precision": "approximate_relation_centroid",
                                                 "center_in_request": center_in_request,
                                                 "public_access": "unknown", "capacity_status": "unknown"}})
                    counts["relation_poi"] += 1
                    continue
                counts["relation_invalid"] += 1
    finally:
        for file in files.values():
            file.close()
    try:
        reader = osmium.io.Reader(osmium.io.File(str(pbf), "pbf"))
        try:
            timestamp = reader.header().get("osmosis_replication_timestamp") or None
        finally:
            reader.close()
    except (RuntimeError, ValueError):
        timestamp = None
    if timestamp is not None:
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.utcoffset() is None or parsed > datetime.now(timezone.utc):
                raise ValueError("invalid source timestamp")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_pbf_base_timestamp") from exc
    return paths, counts, timestamp


def extract_layers_with_relations(pbf: Path, area, directory: Path):
    """Production extraction with assembled, tagged multipolygon relation POIs."""
    return extract_layers(pbf, area, directory, include_relations=True)
