"""Stream node/way observations from PBF; use osmium's disk-backed node index."""

import json
from datetime import datetime, timezone
from pathlib import Path
import time

from pyproj import Geod
from shapely.geometry import LineString, Point, Polygon, shape

from src.etl.osm_parsing import _categories, _direction

MAX_FEATURES = 200000
MAX_FEATURE_BYTES = 64 * 1024 * 1024
MAX_PARSE_SECONDS = 1800


def extract_layers(pbf: Path, area, directory: Path, *, native_tag_filter: bool = False,
                   way_bbox_prefilter: bool = False):
    """Extract observations; opt-in native tag filtering is a benchmark control.

    Libosmium's location handler sees all nodes before these filters run, so
    complete way coordinates (including outside the requested area) survive.
    Geometry/category checks below remain identical in either mode.
    """
    if type(native_tag_filter) is not bool or type(way_bbox_prefilter) is not bool:
        raise ValueError("benchmark filter flags must be bool")
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
    files = {role: path.open("w", encoding="utf-8") for role, path in paths.items()}
    started = time.monotonic()
    written_bytes = 0

    def check_deadline():
        if time.monotonic() - started > MAX_PARSE_SECONDS:
            raise ValueError("extract_parse_timeout")

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
            check_deadline()
            tags = dict(way.tags)
            categories = _categories(tags)
            if not categories and "highway" not in tags:
                return
            try:
                coords = [[ref.location.lon, ref.location.lat] for ref in way.nodes]
                if len(coords) < 2 or not all(ref.location.valid() for ref in way.nodes):
                    counts["missing_way_locations"] += 1
                    return
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

    try:
        handler = Handler()
        # Content-addressed cache paths end in a SHA-256, not .pbf. Never rely
        # on extension-based format inference for the verified PBF source.
        filters = [osmium.filter.KeyFilter(
            "tourism", "historic", "amenity", "leisure", "shop", "railway", "highway", "public_transport"
        )] if native_tag_filter else []
        handler.apply_file(osmium.io.File(str(pbf), "pbf"), locations=True,
                           idx="sparse_file_array", filters=filters)
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
