"""Attach partial official observations to an existing collection, without merging POI."""

from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from scripts.collect_boundary import code_fingerprint
from src.data_models import RunManifest
from src.data_models.study_area import StudyArea
from src.etl.adapters.lenobl_oopt import SOURCE_ID as OOPT_SOURCE_ID
from src.etl.adapters.mkrf_registry import SOURCE_ID as HERITAGE_SOURCE_ID
from src.etl.heritage_matching import match_heritage
from src.storage import SnapshotStore


VERSION = "registry-enrichment-v1"
MAX_DOCUMENT_BYTES = 65 * 1024 * 1024


def _bytes(document):
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _document(store, snapshot_id):
    metadata = store.get_snapshot(snapshot_id)
    if metadata.byte_length > MAX_DOCUMENT_BYTES:
        raise ValueError("registry_document_too_large")
    document = json.loads(store.read_content(snapshot_id))
    if not isinstance(document, dict):
        raise ValueError("invalid_registry_document")
    return document


def _matching_report(store, area, parent, registry, result, poi_document, poi_id, raw_id, raw_osm):
    records = []
    for record in registry["records"]:
        number = record.get("reg_number")
        records.append({
            "source_record_id": record["source_record_id"],
            "registry_number": number if isinstance(number, str) and re.fullmatch(r"[0-9]{15}", number) else None,
            "source_registry_number": number, "name": record["name"],
            "geometry": record["geometry"], "source_url": registry["source_versions"]["archive_url"],
            "location_precision": "source_reported_point_coordinate_metadata_conflict",
            "spatial_status": record["spatial_status"], "legal_effective_status": "not_validated",
        })
    report = match_heritage(records, poi_document["features"])
    report.pop("official_records")
    report.pop("poi_features")
    report.update(source_run_id=parent.run_id, area_id=area.area_id,
                  official_registry_snapshot_id=result.registry_snapshot.snapshot_id,
                  osm_poi_snapshot_id=poi_id, osm_source_snapshot_id=raw_id,
                  osm_source_received_at=raw_osm.retrieved_at.isoformat(),
                  official_source_received_at=result.archive_snapshot.retrieved_at.isoformat(),
                  temporal_alignment="not_validated", observations_modified=False,
                  coordinate_order_warning=registry.get("coordinate_order_warning"))
    content = _bytes(report)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("matching_report_too_large")
    snapshot = store.save_snapshot("heritage_matching", area.area_id,
                                   "urn:tourism:derived:heritage-matching:v1", content,
                                   report["algorithm_version"], "application/json", "partial")
    return {"status": "advisory_only", "snapshot_id": snapshot.snapshot_id,
            "sha256": snapshot.content_hash, "summary": report["summary"]}


def enrich_territory_run(store: SnapshotStore, source_run_id: str, *, heritage_adapter, oopt_adapter):
    parent = store.get_run(source_run_id)
    if parent.status not in ("succeeded", "partial"):
        raise ValueError("territory_collection_run_required")
    configurations = [sid for sid in parent.snapshot_ids
                       if store.get_snapshot(sid).source_id == "territory_collection_config"]
    if len(configurations) != 1:
        raise ValueError("territory_collection_configuration_required")
    configuration = _document(store, configurations[0])
    try:
        if configuration["stage"] != "territory_collection":
            raise ValueError("invalid_territory_collection_configuration")
        area = StudyArea.from_dict(configuration["study_area"])
        poi_id = configuration["exports"]["poi"]["snapshot_id"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_territory_collection_configuration") from exc
    if area.area_id != parent.area_id or store.get_snapshot(configurations[0]).area_id != area.area_id:
        raise ValueError("enrichment_area_mismatch")
    if poi_id not in parent.snapshot_ids:
        raise ValueError("poi_not_in_source_run")
    poi = store.verify_content(poi_id)
    if poi.area_id != area.area_id or poi.source_id != "geofabrik_poi":
        raise ValueError("invalid_poi_provenance")
    poi_document = _document(store, poi_id)
    if (poi_document.get("type") != "FeatureCollection" or
            not isinstance(poi_document.get("features"), list)):
        raise ValueError("invalid_poi_document")
    provenance = poi_document.get("provenance")
    raw_id = provenance.get("source_snapshot_id") if isinstance(provenance, dict) else None
    if raw_id not in parent.snapshot_ids:
        raise ValueError("poi_source_not_in_run")
    raw_osm = store.verify_content(raw_id)
    if raw_osm.area_id != area.area_id or raw_osm.source_id != "geofabrik_extract":
        raise ValueError("invalid_poi_source_provenance")

    supporting = set(parent.snapshot_ids)
    outcomes, collected = {}, {}
    for kind, adapter in (("heritage", heritage_adapter), ("protected_areas", oopt_adapter)):
        try:
            result = adapter.collect(area)
            if kind == "heritage":
                metadata = (result.passport_snapshot, result.schema_snapshot,
                            result.archive_snapshot, result.registry_snapshot)
                derived = result.registry_snapshot
                expected = result.registry
            else:
                metadata = (result.raw_snapshot, result.catalog_snapshot)
                derived = result.catalog_snapshot
                expected = result.catalog
            source_ids = [snapshot.snapshot_id for snapshot in metadata]
            expected_source = HERITAGE_SOURCE_ID if kind == "heritage" else OOPT_SOURCE_ID
            for snapshot in metadata:
                if (snapshot.area_id != area.area_id or snapshot.source_id != expected_source or
                        store.verify_content(snapshot.snapshot_id) != snapshot):
                    raise ValueError("official_source_provenance_mismatch")
            document = _document(store, derived.snapshot_id)
            if document != expected or document.get("area_id") != area.area_id:
                raise ValueError("official_derived_document_mismatch")
            _bytes(document)
            outcome = {"status": "collected_partial", "snapshot_ids": source_ids,
                       "derived_snapshot_id": derived.snapshot_id,
                       "from_cache": result.from_cache, "checked_at": result.checked_at.isoformat(),
                       "coverage": document.get("coverage", document.get("source_coverage")),
                       "legal_effective_status": "not_validated"}
            if kind == "heritage":
                outcome["quality"] = document["quality"]
                outcome["source_versions"] = document["source_versions"]
                if not isinstance(document["records"], list):
                    raise ValueError("invalid_heritage_records")
            else:
                outcome["catalog_record_count"] = len(document["items"])
                outcome["objects_in_requested_area"] = "unknown"
        except Exception as exc:
            outcomes[kind] = {"status": "unavailable", "reason": getattr(exc, "code", "adapter_error"),
                              "error_type": type(exc).__name__, "snapshot_ids": []}
        else:
            supporting.update(source_ids)
            collected[kind] = (result, document)
            outcomes[kind] = outcome

    matching = {"status": "unavailable", "reason": "heritage_registry_unavailable"}
    if "heritage" in collected:
        result, registry = collected["heritage"]
        try:
            matching = _matching_report(store, area, parent, registry, result, poi_document,
                                        poi_id, raw_id, raw_osm)
        except Exception as exc:
            matching = {"status": "unavailable", "reason": "matching_error",
                        "error_type": type(exc).__name__}
        else:
            supporting.add(matching["snapshot_id"])

    configuration = {
        "stage": "official_registry_enrichment", "source_run_id": parent.run_id,
        "area_id": area.area_id, "sources": outcomes, "heritage_matching": matching,
        "base_observation_status": "stored_observations_not_refreshed",
        "osm_source_received_at": raw_osm.retrieved_at.isoformat(),
        "observations_modified": False, "legal_rules_ready": False,
        "analysis_performed": False, "computed_tourism_results": [],
    }
    config_bytes = _bytes(configuration)
    config = store.save_snapshot("registry_enrichment_config", area.area_id,
                                 "urn:tourism:derived:registry-enrichment:v1", config_bytes,
                                 VERSION, "application/json", "partial")
    supporting.add(config.snapshot_id)
    status = "partial" if collected else "failed"
    run = RunManifest(uuid4().hex, area.area_id, code_fingerprint(), hashlib.sha256(config_bytes).hexdigest(),
                      datetime.now(timezone.utc), tuple(sorted(supporting)), (), status, (
                          "Partial official observations and advisory links, not legal boundaries or permissions.",
                          "Stored OSM observations are not refreshed; source periods may differ.",
                          "No entities merged, capacities, recall or tourism recommendations computed.",
                      ))
    store.save_run(run)
    return {**configuration, "status": status, "run_manifest_id": run.run_id,
            "configuration_snapshot_id": config.snapshot_id, "territory": area.name,
            "source_run_preserved": True}
