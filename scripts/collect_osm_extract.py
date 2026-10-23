"""Collect partial observed POI and roads from verified Geofabrik NW PBF, not routes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime

from scripts.collect_boundary import ROOT, code_fingerprint
from src.etl.adapters.nominatim_boundary import BoundaryError, NominatimBoundaryAdapter
from src.etl.adapters.geofabrik_extract import (ATTRIBUTION, ExtractError,
                                                GeofabrikExtractAdapter, POLY_URL)
from src.etl.osm_extract_parsing import extract_layers
from src.etl.source_policy import DatasetCandidate
from src.pipeline.input_assessment import prepare_inputs
from src.storage import SnapshotStore


def _export(directory, area_id, role, feature_path, provenance):
    directory.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".pending-", delete=False) as file:
            pending = Path(file.name)
            digest = hashlib.sha256()

            def emit(content):
                file.write(content)
                digest.update(content)

            emit(b'{"type":"FeatureCollection","features":[')
            with feature_path.open("rb") as features:
                for index, line in enumerate(features):
                    if index:
                        emit(b",")
                    emit(line.rstrip(b"\r\n"))
            emit(b'],"provenance":')
            emit(json.dumps(provenance, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8"))
            emit(b"}\n")
            file.flush()
            os.fsync(file.fileno())
        target = directory / f"{area_id}.{role}.{digest.hexdigest()}.geojson"
        try:
            os.link(pending, target)
        except FileExistsError:
            existing = hashlib.sha256()
            with target.open("rb") as file:
                for chunk in iter(lambda: file.read(65536), b""):
                    existing.update(chunk)
            if existing.digest() != digest.digest():
                raise ExtractError("modified_export")
        return {"file": str(target), "sha256": digest.hexdigest()}
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def main(argv=None, *, boundary_adapter=None, extract_adapter=None, parser=extract_layers):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("territory")
    cli.add_argument("--store", type=Path, default=ROOT / "data" / "analysis")
    cli.add_argument("--offline", action="store_true")
    args = cli.parse_args(argv)
    if not args.territory.strip():
        cli.error("territory must not be empty")
    try:
        store = SnapshotStore(args.store)
        geocoder = boundary_adapter if boundary_adapter is not None else NominatimBoundaryAdapter(
            store, args.store / "cache" / "nominatim", offline=args.offline,
            endpoint=os.environ.get("NOMINATIM_ENDPOINT", "https://nominatim.openstreetmap.org/search"),
            terms_confirmed=os.environ.get("NOMINATIM_TERMS_CONFIRMED") == "1")
        boundary = geocoder.collect(args.territory)
        area = boundary.study_area
        adapter = extract_adapter if extract_adapter is not None else GeofabrikExtractAdapter(
            args.store / "cache" / "geofabrik_extract", offline=args.offline)
        pbf, record, cached, bbox, coverage = adapter.collect(area)
        raw = store.save_snapshot_file(
            source_id="geofabrik_extract", area_id=area.area_id, url=record["url"], file_path=pbf,
            parser_version="geofabrik-node-way-observations-v1", media_type="application/vnd.osm.pbf",
            completeness="partial", retrieved_at=datetime.fromisoformat(record["received_at"]),
            expected_hash=record["sha256"], expected_size=record["size"],
        )
        (args.store / "cache" / "geofabrik_extract").mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.store / "cache" / "geofabrik_extract") as temp:
            # Parse the archived copy, not the mutable adapter cache file.
            store.verify_content(raw.snapshot_id)
            archived_pbf = store.root / "blobs" / f"{raw.content_hash}.bin"
            paths, counts, source_timestamp = parser(archived_pbf, area, Path(temp))
            provenance = {
                "source": "Geofabrik northwestern-fed-district PBF extract", "source_url": record["url"],
                "resolved_url": record["resolved_url"], "source_sha256": record["sha256"],
                "source_size": record["size"], "source_received_at": record["received_at"],
                "osm_base_timestamp": source_timestamp, "coverage_poly_url": POLY_URL,
                "coverage_poly_sha256": coverage["sha256"],
                "coverage_scope": "verified_extract_poly; features intersect analysis_geometry or request_geometry",
                "algorithm_version": "geofabrik-node-way-observations-v1", "geometry_crs": "EPSG:4326",
                "query_bbox": bbox, "query_bbox_order": "south,west,north,east",
                "attribution": ATTRIBUTION, "license": "https://opendatacommons.org/licenses/odbl/1-0/",
                "quality": "partial: node/way POI and highway lines only; relations unsupported",
                "routing_ready": False, "surrounding_adequacy": "not_validated",
                "real_world_poi_completeness": "unknown",
                "missing_way_locations": counts.get("missing_way_locations", 0),
                "source_timestamp_status": "source_reported" if source_timestamp else "unknown",
                "source_snapshot_id": raw.snapshot_id, "run_manifest_id": None,
            }
            exports = {}
            for role in ("poi", "network"):
                exported = _export(args.store / "exports", area.area_id, role, paths[role], provenance)
                artifact = store.save_snapshot_file(
                    source_id=f"geofabrik_{role}", area_id=area.area_id,
                    url=f"urn:tourism:derived:geofabrik:{role}:v1", file_path=exported["file"],
                    parser_version="geofabrik-node-way-observations-v1", media_type="application/geo+json",
                    completeness="partial", expected_hash=exported["sha256"],
                )
                exports[role] = {**exported, "snapshot_id": artifact.snapshot_id}
        candidates = [DatasetCandidate(
            source_id=boundary.snapshot.source_id, dataset_kind="boundary", area_id=area.area_id,
            discovered_automatically=True, retrieved=True, content_validated=True,
            provenance_verified=True, coverage_verified=True, period_verified=True,
            units_verified=True, access_permitted=True, completeness="complete",
            snapshot_id=boundary.snapshot.snapshot_id,
        )]
        candidates.extend(DatasetCandidate(
            source_id=f"geofabrik_{role}",
            dataset_kind=role, area_id=area.area_id, discovered_automatically=True,
            retrieved=True, content_validated=True, provenance_verified=True,
            # Extract coverage is not an accessibility-validated surrounding network.
            coverage_verified=(role == "poi"), access_permitted=True, completeness="partial",
            snapshot_id=exports[role]["snapshot_id"],
        ) for role in ("poi", "network"))
        assessment = prepare_inputs(
            area_id=area.area_id, candidates=candidates, store=store, code_version=code_fingerprint(),
            configuration={"stage": "osm_extract_observation_collection", "query": args.territory,
                           "study_area": area.to_dict(), "extract_manifest": record,
                           "coverage_poly_url": POLY_URL, "coverage_poly_sha256": coverage["sha256"],
                           "query_bbox": bbox, "exports": exports,
                           "osm_base_timestamp": source_timestamp, "counts": counts,
                           "extract_from_cache": cached},
            supporting_snapshot_ids=(raw.snapshot_id, *(exports[role]["snapshot_id"] for role in ("poi", "network"))),
        )
        result = {"stage": "osm_extract_observation_collection", "status": "partial",
                  "analysis_performed": False, "territory": area.name, "area_id": area.area_id,
                  "boundary_snapshot_id": boundary.snapshot.snapshot_id,
                   "extract_manifest": record, "extract_from_cache": cached, "counts": counts,
                   "source_snapshot_id": raw.snapshot_id, "run_manifest_id": assessment.manifest.run_id,
                   "input_assessment": assessment.to_dict(),
                  "exports": exports, "limitations": [
                      "Geofabrik .poly verifies extract coverage only; not an administrative boundary or calculated surrounding.",
                      "Relations and their POI are unsupported; node/way inventory is partial, not exhaustive.",
                       "Roads are observed geometry, not a routable graph; opening, capacity and journey times unknown.",
                       "Way centroids are approximate source-geometry summaries, not entrances or verified sites.",
                        "Snapshots preserve observations only; no tourism recommendations or effects are computed."]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BoundaryError, ExtractError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"stage": "osm_extract_observation_collection", "status": "failed",
                          "analysis_performed": False, "error": getattr(exc, "code", type(exc).__name__),
                          "details": getattr(exc, "details", str(exc))}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
