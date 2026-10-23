"""Collect observed OSM POI and raw road geometry, not routes or tourism forecasts."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from scripts.collect_boundary import ROOT, code_fingerprint
from src.etl.adapters.nominatim_boundary import BoundaryError, NominatimBoundaryAdapter
from src.etl.adapters.overpass_inventory import ENDPOINT, OverpassError, OverpassInventoryAdapter, build_query
from src.etl.source_policy import DatasetCandidate
from src.pipeline.input_assessment import prepare_inputs
from src.storage import SnapshotStore


def _candidate(snapshot, kind, area_id, *, coverage=True, completeness="complete"):
    return DatasetCandidate(
        source_id=snapshot.source_id, dataset_kind=kind, area_id=area_id,
        discovered_automatically=True, retrieved=True, content_validated=True,
        provenance_verified=True, coverage_verified=coverage, period_verified=True,
        units_verified=True, access_permitted=True, completeness=completeness,
        snapshot_id=snapshot.snapshot_id,
    )


def _export_layer(store, area_id, role, layer, raw_snapshot_id, quality):
    # An export is an independent copy: hard-linking a store blob to a file a
    # GIS editor might modify would corrupt the supposedly immutable snapshot.
    document = {**layer, "provenance": {
        "source_snapshot_id": raw_snapshot_id,
        "source_url": store.get_snapshot(raw_snapshot_id).url,
        "algorithm_version": "osm-observations-v1",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "license": "https://opendatacommons.org/licenses/odbl/1-0/",
        "quality": quality,
        "recommendations": False,
    }}
    content = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")
    snapshot = store.save_snapshot(
        source_id=f"osm_inventory_{role}", area_id=area_id,
        url=f"urn:tourism:derived:osm-inventory:{role}:v1", content=content,
        parser_version="osm-observations-v1", media_type="application/geo+json", completeness="complete",
    )
    directory = store.root / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{area_id}.{role}.{snapshot.content_hash}.geojson"
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".pending-", delete=False) as file:
            pending = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot.content_hash:
                raise ValueError("Existing export was modified; refusing to overwrite")
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return snapshot, path


def main(argv=None, *, boundary_adapter=None, inventory_adapter=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("territory")
    parser.add_argument("--store", type=Path, default=ROOT / "data" / "analysis")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    if not args.territory.strip():
        parser.error("territory must not be empty")
    store = SnapshotStore(args.store)
    try:
        geocoder = boundary_adapter if boundary_adapter is not None else NominatimBoundaryAdapter(
            store, args.store / "cache" / "nominatim", offline=args.offline,
            endpoint=os.environ.get("NOMINATIM_ENDPOINT", "https://nominatim.openstreetmap.org/search"),
            terms_confirmed=os.environ.get("NOMINATIM_TERMS_CONFIRMED") == "1",
        )
        boundary = geocoder.collect(args.territory)
        area = boundary.study_area
        candidates = [_candidate(boundary.snapshot, "boundary", area.area_id)]
        supporting = []
        exports = {}
        inventory_error = None
        collected = None
        data = None
        overpass_query = None
        endpoint = os.environ.get("OVERPASS_ENDPOINT", ENDPOINT)
        try:
            overpass_query = build_query(area)
            overpass = inventory_adapter if inventory_adapter is not None else OverpassInventoryAdapter(
                store, args.store / "cache" / "overpass_inventory", offline=args.offline, endpoint=endpoint,
                terms_confirmed=os.environ.get("OVERPASS_TERMS_CONFIRMED") == "1",
            )
            collected = overpass.collect(area)
            data = collected.inventory.to_dict()
            candidates.extend([
                _candidate(collected.snapshot, "poi", area.area_id),
                # Complete query != validated surrounding/access/turn restrictions.
                _candidate(collected.snapshot, "network", area.area_id, coverage=False, completeness="partial"),
            ])
            quality = {
                **data["quality"], "real_world_poi_completeness": data["real_world_poi_completeness"],
                "routing_ready": data["routing_ready"], "surrounding_adequacy": data["surrounding_adequacy"],
                "coverage_scope": data["coverage_scope"], "query_bbox": collected.query_bbox,
                "query_bbox_order": "south,west,north,east", "osm_base_timestamp": data["osm_base_timestamp"],
            }
            for role in ("poi", "network"):
                artifact, path = _export_layer(store, area.area_id, role, data[role], collected.snapshot.snapshot_id, quality)
                supporting.append(artifact.snapshot_id)
                exports[role] = {"snapshot_id": artifact.snapshot_id, "file": str(path), "sha256": artifact.content_hash}
        except OverpassError as exc:
            inventory_error = {"code": exc.code, "details": exc.details}
            for kind in ("poi", "network"):
                candidates.append(DatasetCandidate(
                    source_id="osm_overpass_inventory", dataset_kind=kind, area_id=area.area_id,
                    discovered_automatically=True, retrieval_error=exc.code,
                    access_permitted=(exc.code not in ("access_not_permitted", "access_denied") and
                                      (endpoint == ENDPOINT or os.environ.get("OVERPASS_TERMS_CONFIRMED") == "1")),
                ))

        assessment = prepare_inputs(
            area_id=area.area_id, candidates=candidates, store=store, code_version=code_fingerprint(),
            configuration={
                "stage": "osm_observation_collection", "query": args.territory, "study_area": area.to_dict(),
                "period_basis": "OSM source base timestamp and system observation; not visit/statistical/legal period",
                "coverage_basis": "declared query bbox plus full way-node references; real-world completeness unknown",
                "overpass_query": overpass_query, "overpass_endpoint": endpoint,
                "inventory_error": inventory_error, "exports": exports,
                "osm_base_timestamp": data["osm_base_timestamp"] if data is not None else None,
                "inventory_from_cache": collected.from_cache if collected is not None else None,
            }, supporting_snapshot_ids=tuple(supporting),
        )
        result = {
            "stage": "osm_observation_collection", "status": "partial",
            "boundary_status": "succeeded", "inventory_status": "failed" if inventory_error else "succeeded",
            "analysis_performed": False, "area_id": area.area_id, "territory": area.name,
            "boundary_snapshot_id": boundary.snapshot.snapshot_id,
            "inventory_error": inventory_error, "exports": exports,
            "input_assessment": assessment.to_dict(),
            "limitations": [
                "POI are source observations, not an exhaustive survey or proof of public opening.",
                "Way/relation centers are approximate; no verified construction sites or entrances inferred.",
                "Raw road geometry is not a mode-specific directed routing graph; no journey times computed.",
                "Collection bbox is not an accessibility-validated surrounding area.",
                "Official statistics discovery and review collection are not performed by this command.",
            ],
        }
        if collected is not None:
            result.update(inventory_snapshot=collected.snapshot.to_dict(), inventory_from_cache=collected.from_cache,
                          observed_at=collected.observed_at.isoformat(), query_bbox=collected.query_bbox,
                          query_bbox_order="south,west,north,east", osm_base_timestamp=data["osm_base_timestamp"],
                          counts=data["quality"]["counts"], routing_ready=data["routing_ready"],
                          attribution=collected.attribution)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if inventory_error else 0
    except (BoundaryError, ValueError, OSError) as exc:
        print(json.dumps({"stage": "osm_observation_collection", "status": "failed", "analysis_performed": False,
                          "error": getattr(exc, "code", "local_state_or_configuration_error"),
                          "details": getattr(exc, "details", str(exc))}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
