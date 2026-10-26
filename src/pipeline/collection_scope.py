"""Fixed 5 km collection policy for initial observations, not routing reachability.

The halo is an operational collection assumption, not a legal jurisdiction or
an empirically sufficient surrounding area. It does not make routing ready.
"""

from dataclasses import dataclass
import json
import math

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from src.data_models.study_area import StudyArea
from src.etl.adapters.nominatim_boundary import (
    ADAPTER_VERSION as BOUNDARY_VERSION, SOURCE_ID as BOUNDARY_SOURCE, BoundaryError, _select,
)
from src.etl.adapters.overpass_inventory import OverpassError, collection_bbox
from src.storage import SnapshotStore


SOURCE_ID = "study_area_collection_scope"
POLICY_VERSION = "collection-scope-v1"


@dataclass(frozen=True)
class ScopedBoundaryCollection:
    study_area: StudyArea
    snapshot: object
    from_cache: bool
    observed_at: object
    attribution: str
    scope_snapshot: object


class CollectionBoundaryAdapter:
    def __init__(self, store: SnapshotStore, boundary_adapter, surrounding_m=5000):
        if (isinstance(surrounding_m, bool) or not isinstance(surrounding_m, (int, float))
                or not math.isfinite(surrounding_m) or not 100 <= surrounding_m <= 10000):
            raise ValueError("surrounding_m must be finite metres between 100 and 10000")
        self.store = store
        self.boundary_adapter = boundary_adapter
        self.surrounding_m = surrounding_m

    def collect(self, query: str) -> ScopedBoundaryCollection:
        boundary = self.boundary_adapter.collect(query)
        original = boundary.study_area
        if not isinstance(original, StudyArea) or original.analysis_geometry is not None:
            raise BoundaryError("invalid_boundary")
        snapshot = self.store.get_snapshot(boundary.snapshot.snapshot_id)
        raw = self.store.read_content(snapshot.snapshot_id)
        if (snapshot != boundary.snapshot or snapshot.area_id != original.area_id
                or snapshot.source_id != BOUNDARY_SOURCE
                or snapshot.parser_version != BOUNDARY_VERSION
                or snapshot.media_type != "application/json" or snapshot.completeness != "complete"
                or _select(raw, query).area_id != original.area_id):
            raise BoundaryError("invalid_boundary")

        geom = shape(original.request_geometry)
        west, south, east, north = geom.bounds
        if south < -80 or north > 84 or east - west >= 180:
            raise BoundaryError("unsupported_projection")
        try:
            collection_bbox(original)
        except OverpassError as exc:
            raise BoundaryError("bbox_too_large") from exc
        lon, lat = geom.centroid.x, geom.centroid.y
        zone = min(60, int((lon + 180) // 6) + 1)
        metric_crs = f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"
        forward = Transformer.from_crs("EPSG:4326", metric_crs, always_xy=True).transform
        reverse = Transformer.from_crs(metric_crs, "EPSG:4326", always_xy=True).transform
        halo = transform(reverse, transform(forward, geom).buffer(self.surrounding_m))
        analysis = unary_union((geom, halo))  # preserve every original edge, hole and island
        if (analysis.bounds[1] < -80 or analysis.bounds[3] > 84
                or analysis.bounds[0] < -180 or analysis.bounds[2] > 180):
            raise BoundaryError("unsupported_projection")
        area = StudyArea(
            name=original.name, request_geometry=original.request_geometry,
            crs=original.crs, jurisdictions=original.jurisdictions,
            jurisdiction_status=original.jurisdiction_status,
            analysis_geometry=mapping(analysis), metric_crs=metric_crs,
        )
        try:
            collection_bbox(area)
        except OverpassError as exc:
            raise BoundaryError("bbox_too_large") from exc
        rebound = self.store.save_snapshot(
            snapshot.source_id, area.area_id, snapshot.url, raw, snapshot.parser_version,
            snapshot.media_type, snapshot.completeness, retrieved_at=snapshot.retrieved_at,
        )
        policy = {
            "schema_version": 1, "policy_version": POLICY_VERSION,
            "policy_kind": "fixed_initial_collection_halo_not_routing_reachability",
            "surrounding_m": self.surrounding_m, "metric_crs": metric_crs,
            "original_boundary_snapshot_id": snapshot.snapshot_id,
            "original_boundary_content_hash": snapshot.content_hash,
            "boundary_snapshot_id": rebound.snapshot_id,
            "study_area": area.to_dict(), "routing_ready": False,
        }
        scope = self.store.save_snapshot(
            SOURCE_ID, area.area_id, f"internal://{SOURCE_ID}/{area.area_id}",
            (json.dumps(policy, sort_keys=True, ensure_ascii=False, allow_nan=False,
                        separators=(",", ":")) + "\n").encode("utf-8"),
            POLICY_VERSION, "application/json", "complete", retrieved_at=snapshot.retrieved_at,
        )
        return ScopedBoundaryCollection(area, rebound, boundary.from_cache,
                                        boundary.observed_at, boundary.attribution, scope)
