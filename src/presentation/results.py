"""Offline, read-only presentation of stored E03/E04 observations."""

from dataclasses import fields
from datetime import datetime
import hashlib
import json
from pathlib import Path

from src.data_models.contracts import RunManifest, valid_id
from src.storage.snapshots import SnapshotStore


CONFIG_LIMIT = 32 * 1024 * 1024
GEOJSON_LIMIT = 64 * 1024 * 1024


def _manifest(data: dict) -> RunManifest:
    # RunManifest has no from_dict in the current E02 contract.
    if not isinstance(data, dict) or set(data) != {field.name for field in fields(RunManifest)}:
        raise ValueError("Invalid run manifest")
    if any(not isinstance(data[key], list) for key in ("snapshot_ids", "evidence_ids", "limitations")):
        raise ValueError("Invalid run reference/limitation arrays")
    return RunManifest(**{**data, "started_at": datetime.fromisoformat(data["started_at"]),
                          **{key: tuple(data[key]) for key in ("snapshot_ids", "evidence_ids", "limitations")}})


def _valid_warnings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _document(store: SnapshotStore, snapshot_id: str, limit: int) -> dict:
    snapshot = store.get_snapshot(snapshot_id)
    if snapshot.byte_length > limit:
        raise ValueError("Presentation document too large")
    try:
        document = json.loads(store.read_content(snapshot_id))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid presentation JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Presentation document must be an object")
    return document


def _configs(store: SnapshotStore, run: RunManifest):
    snapshots = [store.get_snapshot(sid) for sid in run.snapshot_ids]
    def single(source_id):
        found = [item for item in snapshots if item.source_id == source_id]
        if len(found) != 1 or found[0].area_id != run.area_id:
            raise ValueError(f"Expected one {source_id} in run")
        return found[0]

    enrichment_ids = [item for item in snapshots if item.source_id == "registry_enrichment_config"]
    if len(enrichment_ids) > 1:
        raise ValueError("Multiple enrichment configurations")
    collection_snapshot = single("territory_collection_config")
    collection = _document(store, collection_snapshot.snapshot_id, CONFIG_LIMIT)
    if collection.get("stage") != "territory_collection" or not isinstance(collection.get("query"), str):
        raise ValueError("Invalid collection stage/query")
    area = collection.get("study_area")
    if not isinstance(area, dict) or area.get("area_id") != run.area_id or not isinstance(area.get("name"), str):
        raise ValueError("Collection area mismatch")
    enrichment = None
    if enrichment_ids:
        enrichment_snapshot = enrichment_ids[0]
        enrichment = _document(store, enrichment_snapshot.snapshot_id, CONFIG_LIMIT)
        if (enrichment.get("stage") != "official_registry_enrichment" or
                enrichment.get("area_id") != run.area_id or
                not isinstance(enrichment.get("source_run_id"), str)):
            raise ValueError("Invalid enrichment configuration")
        parent_id = valid_id(enrichment["source_run_id"])
        parent = _manifest(json.loads((store.root / "runs" / (parent_id + ".json")).read_text(encoding="utf-8")))
        if (parent.run_id != parent_id or parent.area_id != run.area_id or
                collection_snapshot.snapshot_id not in parent.snapshot_ids):
            raise ValueError("Enrichment source run does not contain collection")
        config_snapshot = enrichment_snapshot
    else:
        config_snapshot = collection_snapshot
    # E03 hashes canonical JSON without its final newline; E04 hashes the exact bytes.
    content = store.read_content(config_snapshot.snapshot_id)
    if run.config_hash not in (hashlib.sha256(content).hexdigest(),
                               hashlib.sha256(content.rstrip(b"\n")).hexdigest()):
        raise ValueError("Run configuration hash mismatch")
    return collection, enrichment, snapshots


def list_runs(store_path: Path) -> dict:
    """List supported run metadata; listing does not verify referenced blob bytes."""
    store = SnapshotStore(store_path)
    runs, errors = [], []
    try:
        paths = sorted((store.root / "runs").glob("*.json"))
    except OSError as exc:
        return {"runs": [], "errors": [str(exc)]}
    for path in paths:
        try:
            run = _manifest(json.loads(path.read_text(encoding="utf-8")))
            if path.stem != run.run_id:
                raise ValueError("Run ID mismatch")
            metadata = [store.get_snapshot(sid) for sid in run.snapshot_ids]
            if not any(item.source_id == "territory_collection_config" for item in metadata):
                continue
            collection, enrichment, _ = _configs(store, run)
            runs.append({"run_id": run.run_id, "name": collection["study_area"]["name"],
                         "query": collection["query"],
                         "stage": "official_registry_enrichment" if enrichment else "territory_collection",
                         "started_at": run.started_at.isoformat(), "status": run.status})
        except (ValueError, OSError, TypeError, KeyError, UnicodeError) as exc:
            errors.append(f"{path.name}: {exc}")
    runs.sort(key=lambda item: (item["started_at"], item["run_id"]), reverse=True)
    return {"runs": runs, "errors": errors}


def load_run_view(store_path: Path, run_id: str) -> dict:
    """Verify all run blobs via get_run before loading only bounded display documents."""
    store = SnapshotStore(store_path)
    run = store.get_run(valid_id(run_id))
    collection, enrichment, snapshots = _configs(store, run)
    snapshot_by_id = {item.snapshot_id: item for item in snapshots}
    exports = collection.get("exports")
    if not isinstance(exports, dict):
        raise ValueError("Missing collection exports")
    layers = {}
    for layer, source in (("poi", "geofabrik_poi"), ("network", "geofabrik_network")):
        entry = exports.get(layer)
        sid = entry.get("snapshot_id") if isinstance(entry, dict) else None
        snapshot = snapshot_by_id.get(sid)
        if snapshot is None or snapshot.source_id != source or snapshot.area_id != run.area_id:
            raise ValueError(f"Invalid {layer} export provenance")
        document = _document(store, sid, GEOJSON_LIMIT)
        if document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
            raise ValueError(f"Invalid {layer} FeatureCollection")
        seen = set()
        for feature in document["features"]:
            if (not isinstance(feature, dict) or feature.get("type") != "Feature" or
                    not isinstance(feature.get("properties"), dict) or
                    not isinstance(feature.get("geometry"), (dict, type(None)))):
                raise ValueError(f"Invalid {layer} features")
            identifier = feature.get("id")
            props = feature["properties"]
            if (not isinstance(identifier, str) or not identifier.strip() or identifier in seen or
                    ("tags" in props and not isinstance(props["tags"], dict)) or
                    ("categories" in props and
                     (not isinstance(props["categories"], list) or
                      any(not isinstance(category, str) for category in props["categories"])))):
                raise ValueError(f"Invalid {layer} feature identity/properties")
            seen.add(identifier)
        layers[layer] = document
    matching = None
    if enrichment is not None:
        info = enrichment.get("heritage_matching")
        if not isinstance(info, dict):
            raise ValueError("Invalid matching descriptor")
        sid = info.get("snapshot_id")
        if sid is not None:
            snapshot = snapshot_by_id.get(sid)
            if snapshot is None or snapshot.source_id != "heritage_matching" or snapshot.area_id != run.area_id:
                raise ValueError("Invalid matching provenance")
            matching = _document(store, sid, GEOJSON_LIMIT)
            if (matching.get("artifact_kind") != "advisory_heritage_matching" or
                    matching.get("area_id") != run.area_id or
                    matching.get("source_run_id") != enrichment["source_run_id"] or
                    matching.get("osm_poi_snapshot_id") != exports["poi"]["snapshot_id"]):
                raise ValueError("Matching source mismatch")
            for key in ("identifier_links", "review_candidates", "excluded_review_candidates"):
                items = matching.get(key)
                if not isinstance(items, list):
                    raise ValueError(f"Invalid matching {key}")
                for item in items:
                    if (not isinstance(item, dict) or not isinstance(item.get("evidence"), dict) or
                            ("warnings" in item and not _valid_warnings(item["warnings"])) or
                            ("warnings" in item.get("evidence", {}) and
                             not _valid_warnings(item["evidence"]["warnings"]))):
                        raise ValueError(f"Invalid matching {key} entry")
            if "warnings" in matching and not _valid_warnings(matching["warnings"]):
                raise ValueError("Invalid matching warnings")
    return {"run": run.to_dict(), "collection": collection, "enrichment": enrichment,
            **layers, "matching": matching, "sources": [item.to_dict() for item in snapshots]}
