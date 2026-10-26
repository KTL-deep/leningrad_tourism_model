"""Offline collection halo and provenance checks."""

from datetime import datetime, timezone
import hashlib
import json

import pytest
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

from src.data_models.study_area import StudyArea
from src.etl.adapters.nominatim_boundary import BoundaryCollection, BoundaryError
from src.pipeline.collection_scope import CollectionBoundaryAdapter, POLICY_VERSION, SOURCE_ID
from src.storage import SnapshotStore


GEOMETRY = {"type": "MultiPolygon", "coordinates": [
    [[[30, 59], [30.01, 59], [30.01, 59.01], [30, 59.01], [30, 59]],
      [[30.003, 59.003], [30.003, 59.005], [30.005, 59.005],
       [30.005, 59.003], [30.003, 59.003]]],
    [[[30.025, 59], [30.03, 59], [30.03, 59.005], [30.025, 59.005], [30.025, 59]]],
]}
OBSERVED = datetime(2025, 5, 1, tzinfo=timezone.utc)


class BoundaryStub:
    def __init__(self, store, geometry=GEOMETRY):
        self.calls = []
        self.area = StudyArea("Synthetic", geometry, jurisdictions=("Reported",),
                              jurisdiction_status="source_reported")
        self.raw = json.dumps([{
            "osm_type": "relation", "osm_id": 1, "category": "boundary",
            "type": "administrative", "display_name": "Synthetic",
            "address": {"state": "Reported"}, "geojson": geometry,
        }]).encode()
        self.snapshot = store.save_snapshot("osm_nominatim", self.area.area_id,
                                            "https://nominatim.openstreetmap.org/search?q=Synthetic",
                                            self.raw, "nominatim-boundary-v1", "application/json",
                                            "complete", retrieved_at=OBSERVED)

    def collect(self, query):
        self.calls.append(query)
        return BoundaryCollection(self.area, self.snapshot, True, OBSERVED)


def test_halo_rebind_provenance_and_idempotence(tmp_path):
    store = SnapshotStore(tmp_path / "store")
    boundary = BoundaryStub(store)
    adapter = CollectionBoundaryAdapter(store, boundary)
    first = adapter.collect("Synthetic")
    second = adapter.collect("Synthetic")
    area = first.study_area
    assert boundary.calls == ["Synthetic", "Synthetic"]
    assert first == second
    assert first.from_cache and first.observed_at == OBSERVED
    assert first.attribution == "© OpenStreetMap contributors (ODbL)"
    assert area.area_id != boundary.area.area_id
    assert area.request_geometry == boundary.area.request_geometry
    assert area.name == boundary.area.name
    assert area.jurisdictions == boundary.area.jurisdictions
    assert area.jurisdiction_status == "source_reported"
    assert area.to_dict()["surrounding_status"] == "explicit"
    assert area.metric_crs.startswith("EPSG:326")
    original = shape(area.request_geometry)
    analysis = shape(area.analysis_geometry)
    assert analysis.covers(original)
    assert original.geom_type == "MultiPolygon" and len(original.geoms) == 2
    assert sorted(len(piece.interiors) for piece in original.geoms) == [0, 1]
    assert not original.covers(Point(30.004, 59.004))  # original hole remains in request
    assert not original.covers(Point(30.04, 59.002))
    assert analysis.covers(Point(30.04, 59.002))
    forward = Transformer.from_crs("EPSG:4326", area.metric_crs, always_xy=True).transform
    projected = transform(forward, original)
    buffered = transform(forward, analysis)
    island_edge = transform(forward, Point(30.03, 59.0025))
    edge = Point(island_edge.x + 4000, island_edge.y)
    assert buffered.covers(edge)
    assert buffered.bounds[2] - projected.bounds[2] == pytest.approx(5000, abs=10)
    rebound = first.snapshot
    assert rebound.area_id == first.scope_snapshot.area_id == area.area_id
    assert rebound.snapshot_id != boundary.snapshot.snapshot_id
    assert rebound.content_hash == hashlib.sha256(boundary.raw).hexdigest()
    assert rebound.retrieved_at == boundary.snapshot.retrieved_at == OBSERVED
    assert (rebound.source_id, rebound.url, rebound.parser_version, rebound.media_type,
            rebound.completeness) == (boundary.snapshot.source_id, boundary.snapshot.url,
                                      boundary.snapshot.parser_version, boundary.snapshot.media_type,
                                      boundary.snapshot.completeness)
    assert store.read_content(rebound.snapshot_id) == store.read_content(boundary.snapshot.snapshot_id) == boundary.raw
    assert store.get_snapshot(boundary.snapshot.snapshot_id) == boundary.snapshot
    policy = json.loads(store.read_content(first.scope_snapshot.snapshot_id))
    assert first.scope_snapshot.source_id == SOURCE_ID
    assert policy["policy_version"] == POLICY_VERSION
    assert policy["surrounding_m"] == 5000 and policy["metric_crs"] == area.metric_crs
    assert policy["routing_ready"] is False
    assert policy["original_boundary_snapshot_id"] == boundary.snapshot.snapshot_id
    assert policy["original_boundary_content_hash"] == boundary.snapshot.content_hash
    assert policy["boundary_snapshot_id"] == rebound.snapshot_id
    assert StudyArea.from_dict(policy["study_area"]) == area


@pytest.mark.parametrize("radius", [True, False, None, "5000", 99, 10001,
                                     float("nan"), float("inf"), -float("inf")])
def test_invalid_radius_before_collection(tmp_path, radius):
    with pytest.raises(ValueError, match="surrounding_m"):
        CollectionBoundaryAdapter(SnapshotStore(tmp_path), object(), radius)


@pytest.mark.parametrize("geometry,code", [
    ({"type": "Polygon", "coordinates": [[[30, 85], [30.01, 85], [30.01, 85.01], [30, 85.01], [30, 85]]]},
     "unsupported_projection"),
    ({"type": "Polygon", "coordinates": [[[30, 59], [30.5, 59], [30.5, 59.5], [30, 59.5], [30, 59]]]},
     "bbox_too_large"),
    ({"type": "Polygon", "coordinates": [[[30, 59], [30.28, 59], [30.28, 59.28], [30, 59.28], [30, 59]]]},
     "bbox_too_large"),
])
def test_reject_unsupported_and_oversized_extents(tmp_path, geometry, code):
    store = SnapshotStore(tmp_path)
    stub = BoundaryStub(store, geometry)
    with pytest.raises(BoundaryError, match=code):
        CollectionBoundaryAdapter(store, stub).collect("Synthetic")


def test_foreign_boundary_and_corrupt_raw_rejected_before_rebind(tmp_path):
    store = SnapshotStore(tmp_path / "store")
    stub = BoundaryStub(store)
    foreign = SnapshotStore(tmp_path / "foreign")
    with pytest.raises(FileNotFoundError):
        CollectionBoundaryAdapter(foreign, stub).collect("Synthetic")
    stub.area = StudyArea("Foreign", stub.area.request_geometry)
    with pytest.raises(BoundaryError, match="invalid_boundary"):
        CollectionBoundaryAdapter(store, stub).collect("Synthetic")
    stub.area = StudyArea("Synthetic", GEOMETRY, jurisdictions=("Reported",),
                          jurisdiction_status="source_reported")
    (store.root / "blobs" / (stub.snapshot.content_hash + ".bin")).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Corrupt snapshot content"):
        CollectionBoundaryAdapter(store, stub).collect("Synthetic")
    assert len(list((store.root / "snapshots").glob("*.json"))) == 1


def test_untrusted_boundary_source_is_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    stub = BoundaryStub(store)
    stub.snapshot = store.save_snapshot("other_source", stub.area.area_id, stub.snapshot.url,
                                        stub.raw, stub.snapshot.parser_version, "application/json",
                                        "complete", retrieved_at=OBSERVED)
    with pytest.raises(BoundaryError, match="invalid_boundary"):
        CollectionBoundaryAdapter(store, stub).collect("Synthetic")
    assert len(list((store.root / "snapshots").glob("*.json"))) == 2
