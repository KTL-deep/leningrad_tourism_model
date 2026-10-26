"""Bind an advisory POI duplicate-candidate audit to immutable source snapshots."""

import json

from src.etl.poi_duplicates import DUPLICATE_AUDIT_VERSION, detect_poi_duplicates
from src.storage import SnapshotStore


MAX_POI_DOCUMENT_BYTES = 65 * 1024 * 1024


def audit_poi_snapshot(store: SnapshotStore, poi_snapshot_id: str):
    poi = store.get_snapshot(poi_snapshot_id)
    if poi.media_type != "application/geo+json" or poi.byte_length > MAX_POI_DOCUMENT_BYTES:
        raise ValueError("invalid_poi_audit_document")
    document = json.loads(store.read_content(poi_snapshot_id))
    if (not isinstance(document, dict) or document.get("type") != "FeatureCollection"
            or not isinstance(document.get("features"), list)):
        raise ValueError("invalid_poi_audit_document")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("source_snapshot_id"), str):
        raise ValueError("missing_poi_audit_source")
    raw = store.verify_content(provenance["source_snapshot_id"])
    if raw.area_id != poi.area_id:
        raise ValueError("poi_audit_source_area_mismatch")
    if provenance.get("source_sha256", raw.content_hash) != raw.content_hash:
        raise ValueError("poi_audit_source_hash_mismatch")
    report = detect_poi_duplicates(document.get("features"))
    artifact = {
        "schema_version": 1,
        "artifact_kind": "derived_poi_duplicate_candidates",
        "algorithm_version": DUPLICATE_AUDIT_VERSION,
        "area_id": poi.area_id,
        "poi_snapshot_id": poi.snapshot_id,
        "source_snapshot_id": raw.snapshot_id,
        "source_sha256": raw.content_hash,
        "source_received_at": raw.retrieved_at.isoformat(),
        "audit": report,
        "observations_modified": False,
        "computed_tourism_results": [],
    }
    snapshot = store.save_snapshot(
        source_id="poi_duplicate_candidates", area_id=poi.area_id,
        url="urn:tourism:derived:poi-duplicate-candidates:v1",
        content=json.dumps(artifact, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8"),
        parser_version=DUPLICATE_AUDIT_VERSION, media_type="application/json", completeness="partial",
    )
    return snapshot, report
