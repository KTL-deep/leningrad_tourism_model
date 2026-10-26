import json

import pytest

from src.pipeline.poi_quality import MAX_POI_DOCUMENT_BYTES, audit_poi_snapshot
from src.storage import SnapshotStore


def snapshots(store, *, source_area="sample", source_hash=None, features=None):
    raw = store.save_snapshot("geofabrik_extract", source_area, "https://example.org/source.pbf",
                              b"source", "v2", "application/vnd.osm.pbf", "partial")
    if features is None:
        features = [{
            "type": "Feature", "id": f"{kind}/{identifier}",
            "geometry": {"type": "Point", "coordinates": [30, 60]},
            "properties": {"osm_type": kind, "osm_id": identifier,
                           "tags": {"name": "Museum", "tourism": "museum"},
                           "categories": ["tourism"], "location_precision": precision},
        } for kind, identifier, precision in (
            ("node", 1, "exact_source"), ("way", 2, "approximate_way_centroid"))]
    content = json.dumps({"type": "FeatureCollection", "features": features, "provenance": {
        "source_snapshot_id": raw.snapshot_id,
        "source_sha256": raw.content_hash if source_hash is None else source_hash,
    }}).encode("utf-8")
    poi = store.save_snapshot("geofabrik_poi", "sample", "urn:tourism:derived:poi", content,
                              "v2", "application/geo+json", "partial")
    return raw, poi


def test_audit_preserves_observations_and_binds_provenance(tmp_path):
    store = SnapshotStore(tmp_path)
    raw, poi = snapshots(store)
    before = store.read_content(poi.snapshot_id)
    saved, report = audit_poi_snapshot(store, poi.snapshot_id)
    artifact = json.loads(store.read_content(saved.snapshot_id))
    assert artifact["poi_snapshot_id"] == poi.snapshot_id
    assert artifact["source_snapshot_id"] == raw.snapshot_id
    assert artifact["source_sha256"] == raw.content_hash
    assert artifact["audit"] == report
    assert artifact["observations_modified"] is False
    assert artifact["computed_tourism_results"] == []
    assert saved.completeness == "partial"
    assert audit_poi_snapshot(store, poi.snapshot_id)[0] == saved
    assert store.read_content(poi.snapshot_id) == before


@pytest.mark.parametrize("kwargs,error", [
    ({"source_area": "foreign"}, "area_mismatch"),
    ({"source_hash": "0" * 64}, "hash_mismatch"),
    ({"features": {}}, "invalid_poi_audit_document"),
])
def test_audit_rejects_invalid_provenance_or_document(tmp_path, kwargs, error):
    store = SnapshotStore(tmp_path)
    _, poi = snapshots(store, **kwargs)
    with pytest.raises(ValueError, match=error):
        audit_poi_snapshot(store, poi.snapshot_id)
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2


def test_corrupt_source_cannot_publish_quality_artifact(tmp_path):
    store = SnapshotStore(tmp_path)
    raw, poi = snapshots(store)
    (tmp_path / "blobs" / (raw.content_hash + ".bin")).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Corrupt"):
        audit_poi_snapshot(store, poi.snapshot_id)
    assert len(list((tmp_path / "snapshots").glob("*.json"))) == 2


def test_document_bound_checked_before_reading_bytes(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store = SnapshotStore(tmp_path)
    monkeypatch.setattr(store, "get_snapshot", lambda _: SimpleNamespace(
        media_type="application/geo+json", byte_length=MAX_POI_DOCUMENT_BYTES + 1))

    def forbid_read(_):
        raise AssertionError("oversized content must not be loaded")

    monkeypatch.setattr(store, "read_content", forbid_read)
    with pytest.raises(ValueError, match="invalid_poi_audit_document"):
        audit_poi_snapshot(store, "oversized")
