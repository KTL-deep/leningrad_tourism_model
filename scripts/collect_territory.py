"""Collect scoped observed territory inputs; never compute tourism recommendations."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from scripts.collect_boundary import ROOT, code_fingerprint
from scripts.collect_osm_extract import main as collect_osm_extract
from src.data_models import RunManifest
from src.etl.adapters.geofabrik_extract import GeofabrikExtractAdapter
from src.etl.adapters.nominatim_boundary import NominatimBoundaryAdapter
from src.etl.adapters.official_statistics import OfficialStatisticsAdapter, SOURCE_ID as STATS_SOURCE
from src.etl.http_session import BoundedSession
from src.etl.source_policy import DatasetCandidate, assess_candidate
from src.pipeline.collection_scope import CollectionBoundaryAdapter
from src.pipeline.input_assessment import prepare_inputs
from src.storage import SnapshotStore


def _bytes(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _journal(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as file:
            pending = Path(file.name)
            file.write(_bytes(document))
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


class _CaptureBoundary:
    def __init__(self, adapter):
        self.adapter = adapter
        self.collected = None

    def collect(self, query):
        self.collected = self.adapter.collect(query)
        return self.collected


def _validated_discovery(discovery, store, area):
    if (discovery.status not in ("included", "excluded")
            or not isinstance(discovery.parser_version, str) or not discovery.parser_version
            or not isinstance(discovery.snapshot_ids, tuple)
            or not isinstance(discovery.statistics, tuple)
            or not isinstance(discovery.requests, tuple)):
        raise ValueError("Invalid statistics discovery result")
    disposition = {"status": discovery.status, "reason": discovery.reason,
                   "statistics": list(discovery.statistics),
                   "snapshot_ids": list(discovery.snapshot_ids),
                   "requests": list(discovery.requests), "parser_version": discovery.parser_version}
    if (any(not isinstance(entry, dict) for entry in disposition["requests"])
            or len(set(discovery.snapshot_ids)) != len(discovery.snapshot_ids)):
        raise ValueError("Invalid statistics discovery references")
    snapshots = {}
    for sid in discovery.snapshot_ids:
        snapshot = store.verify_content(sid)
        if snapshot.area_id != area.area_id or snapshot.source_id != STATS_SOURCE:
            raise ValueError("Statistics source snapshot provenance mismatch")
        snapshots[sid] = snapshot
    candidate = discovery.candidate
    if discovery.status == "included":
        if (discovery.reason is not None or not isinstance(candidate, DatasetCandidate)
                or candidate.source_id != STATS_SOURCE or candidate.dataset_kind != "official_statistics"
                or candidate.area_id != area.area_id or candidate.snapshot_id not in snapshots
                or snapshots[candidate.snapshot_id].completeness != "complete"
                or not assess_candidate(candidate, area.area_id).included
                or not discovery.statistics):
            raise ValueError("Invalid included statistics candidate")
        for row in discovery.statistics:
            if (not isinstance(row, dict) or row.get("area_id") != area.area_id
                    or row.get("snapshot_id") != candidate.snapshot_id
                    or row.get("source_url") != snapshots[candidate.snapshot_id].url
                    or row.get("municipality") != area.name
                    or row.get("jurisdiction") not in area.jurisdictions
                    or type(row.get("period")) is not int or not 1900 <= row["period"] <= 2100
                    or type(row.get("value")) is not int or row["value"] < 0
                    or any(not isinstance(row.get(key), str) or not row[key].strip()
                           for key in ("metric", "unit", "attribution", "license"))
                    or ("catalog_snapshot_id" in row and
                        row["catalog_snapshot_id"] not in snapshots)):
                raise ValueError("Invalid statistics observation provenance")
    elif (candidate is not None or discovery.statistics or
          not isinstance(discovery.reason, str) or not discovery.reason):
        raise ValueError("Invalid excluded statistics discovery")
    # Validate all adapter-owned payloads before a derived artifact/configuration is written.
    _bytes({"disposition": disposition, "candidate": asdict(candidate) if candidate else None})
    return disposition, candidate


def main(argv=None, *, boundary_adapter=None, extract_adapter=None, statistics_adapter=None,
         parser=None, session=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("territory")
    cli.add_argument("--store", type=Path, default=ROOT / "data" / "analysis")
    cli.add_argument("--offline", action="store_true")
    cli.add_argument("--surrounding-m", type=float, default=5000)
    cli.add_argument("--node-way-only", action="store_true")
    args = cli.parse_args(argv)
    if not args.territory.strip():
        cli.error("territory must not be empty")

    attempt_id = uuid4().hex
    started = datetime.now(timezone.utc)
    journal_path = args.store / "logs" / f"collection-{attempt_id}.json"
    record = {"attempt_id": attempt_id, "stage": "territory_collection", "status": "running",
              "query": args.territory, "started_at": started.isoformat()}
    _journal(journal_path, record)

    def finish(document, status):
        _journal(journal_path, {**record, "status": status, "result": document,
                                "finished_at": datetime.now(timezone.utc).isoformat()})
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0 if status == "succeeded" else 2

    store = SnapshotStore(args.store)
    shared = BoundedSession(session=session, cache_dir=args.store / "cache" / "http_session",
                            journal_path=args.store / "logs" / "http-requests.jsonl",
                            offline=args.offline)
    try:
        geocoder = boundary_adapter if boundary_adapter is not None else NominatimBoundaryAdapter(
            store, args.store / "cache" / "nominatim", session=shared, offline=args.offline,
            endpoint=os.environ.get("NOMINATIM_ENDPOINT", "https://nominatim.openstreetmap.org/search"),
            terms_confirmed=os.environ.get("NOMINATIM_TERMS_CONFIRMED") == "1")
        scoped = _CaptureBoundary(CollectionBoundaryAdapter(store, geocoder, args.surrounding_m))
        extract = extract_adapter if extract_adapter is not None else GeofabrikExtractAdapter(
            args.store / "cache" / "geofabrik_extract", session=shared, offline=args.offline)
        base = []
        base_args = [args.territory, "--store", str(args.store)]
        if args.offline:
            base_args.append("--offline")
        if args.node_way_only:
            base_args.append("--node-way-only")
        code = collect_osm_extract(base_args, boundary_adapter=scoped, extract_adapter=extract,
                                   parser=parser, result_callback=base.append)
        if code != 0:
            failure = base[0]
            return finish({"stage": "territory_collection", "status": "failed", "attempt_id": attempt_id,
                           "analysis_performed": False, "required_spatial_inputs_collected": False,
                           "error": failure["error"], "details": failure.get("details")}, "failed")

        observed = base[0]
        area = scoped.collected.study_area
        stats = statistics_adapter if statistics_adapter is not None else OfficialStatisticsAdapter(
            store, args.store / "cache" / "official_statistics", session=shared, offline=args.offline)
        try:
            discovery = stats.discover(area)
        except Exception as exc:
            # Optional adapter errors must not disguise a successful spatial collection.
            disposition = {"status": "excluded", "reason": "adapter_error",
                           "error_type": type(exc).__name__, "statistics": [], "snapshot_ids": [],
                           "requests": [], "parser_version": "official-statistics-discovery-v1"}
            candidate = None
        else:
            try:
                disposition, candidate = _validated_discovery(discovery, store, area)
            except Exception as exc:
                disposition = {"status": "excluded", "reason": "invalid_discovery_result",
                               "error_type": type(exc).__name__, "statistics": [], "snapshot_ids": [],
                               "requests": [], "parser_version": "official-statistics-discovery-v1"}
                candidate = None
        source_ids = disposition["snapshot_ids"]

        discovery_snapshot = store.save_snapshot(
            "statistics_discovery", area.area_id, "urn:tourism:derived:statistics-discovery:v1",
            _bytes({"schema_version": 1, "artifact_kind": "derived_statistics_discovery",
                    "area_id": area.area_id, **disposition}), "statistics-discovery-v1",
            "application/json", "complete")
        base_run = store.get_run(observed["run_manifest_id"])
        assessment_ids = [sid for sid in base_run.snapshot_ids
                          if store.get_snapshot(sid).source_id == "input_assessment"]
        if len(assessment_ids) != 1:
            raise ValueError("Base assessment artifact missing")
        base_assessment_id = assessment_ids[0]
        attestations = json.loads(store.read_content(base_assessment_id))["adapter_attestations"]
        candidates = [DatasetCandidate(**item) for item in attestations]
        candidates.append(candidate if candidate is not None else DatasetCandidate(
            STATS_SOURCE, "official_statistics", area.area_id, discovered_automatically=True,
            retrieval_error=disposition["reason"] or "unavailable"))
        version = code_fingerprint()
        configuration = {"stage": "territory_collection", "query": args.territory,
                         "study_area": area.to_dict(), "surrounding_m": args.surrounding_m,
                         "surrounding_assumption": "fixed_initial_collection_halo_not_routing_reachability",
                         "scope_snapshot_id": scoped.collected.scope_snapshot.snapshot_id,
                         "base_run_manifest_id": base_run.run_id, "statistics_discovery": disposition,
                         "exports": observed["exports"], "poi_duplicate_audit": observed["poi_duplicate_audit"],
                         "code_version": version}
        config_snapshot = store.save_snapshot(
            "territory_collection_config", area.area_id, "urn:tourism:derived:territory-collection-config:v1",
            _bytes(configuration), "territory-collection-v1", "application/json", "complete")
        supporting = tuple(sorted({*base_run.snapshot_ids, *source_ids,
                                   discovery_snapshot.snapshot_id, config_snapshot.snapshot_id}))
        assessment = prepare_inputs(area_id=area.area_id, candidates=candidates, store=store,
                                    code_version=version, configuration=configuration,
                                    supporting_snapshot_ids=supporting)
        final_ids = tuple(sorted({*assessment.manifest.snapshot_ids, *supporting}))
        config_hash = hashlib.sha256(_bytes(configuration).rstrip(b"\n")).hexdigest()
        run = RunManifest(uuid4().hex, area.area_id, version, config_hash, started, final_ids, (),
                          "partial" if assessment.manifest.status != "succeeded" else "succeeded",
                          tuple(assessment.manifest.limitations))
        store.save_run(run)
        result = {"stage": "territory_collection", "status": run.status,
                  "collection_status": "succeeded", "required_spatial_inputs_collected": True,
                  "analysis_performed": False, "recommendations": [], "attempt_id": attempt_id,
                  "territory": area.name, "area_id": area.area_id,
                  "scope_snapshot_id": scoped.collected.scope_snapshot.snapshot_id,
                  "boundary_snapshot_id": observed["boundary_snapshot_id"],
                  "source_snapshot_id": observed["source_snapshot_id"],
                  "exports": observed["exports"], "counts": observed["counts"],
                  "poi_duplicate_audit": observed["poi_duplicate_audit"],
                  "statistics_discovery": {**disposition, "discovery_snapshot_id": discovery_snapshot.snapshot_id},
                  "base_run_manifest_id": base_run.run_id,
                  "run_manifest_id": run.run_id, "input_assessment": assessment.to_dict()}
        return finish(result, "succeeded")
    except (ValueError, OSError, RuntimeError) as exc:
        return finish({"stage": "territory_collection", "status": "failed", "attempt_id": attempt_id,
                       "analysis_performed": False, "required_spatial_inputs_collected": False,
                       "error": getattr(exc, "code", type(exc).__name__),
                        "details": getattr(exc, "details", str(exc))}, "failed")
    finally:
        shared.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
