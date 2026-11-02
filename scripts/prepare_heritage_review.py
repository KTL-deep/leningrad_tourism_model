"""Freeze an entity-disjoint, unlabelled E04 follow-up packet from verified snapshots."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_heritage_matching import _bounded, _document, build_audit, publish_exclusive
from src.etl.heritage_review import prepare_review
from src.etl.heritage_validation import (REVIEW_BASIS, MAX_REPORTS, strict_json,
                                         validate_heritage_benchmark)
from src.storage import SnapshotStore


CODE_PATHS = ("src/etl/heritage_matching.py", "src/etl/heritage_locality.py",
              "src/pipeline/registry_enrichment.py")


def prepare_from_store(store, snapshot_ids, benchmark_path):
    if not 0 < len(snapshot_ids) <= MAX_REPORTS or len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError("invalid_matching_snapshots")
    benchmark = strict_json(_bounded(benchmark_path))
    validate_heritage_benchmark(benchmark)
    bundles, verification_cases = [], []
    for snapshot_id in snapshot_ids:
        meta, report = _document(store, snapshot_id, "heritage_matching")
        if report.get("area_id") != meta.area_id:
            raise ValueError("report_area_mismatch")
        run = store.get_run(report.get("source_run_id"))
        if run.area_id != meta.area_id or report.get("osm_poi_snapshot_id") not in run.snapshot_ids:
            raise ValueError("parent_run_poi_mismatch")
        registry_id, poi_id = report.get("official_registry_snapshot_id"), report.get("osm_poi_snapshot_id")
        if not isinstance(registry_id, str) or not isinstance(poi_id, str):
            raise ValueError("invalid_report_references")
        _, registry = _document(store, registry_id, "mkrf_egrkn", meta.area_id)
        _, poi = _document(store, poi_id, "geofabrik_poi", meta.area_id)
        if registry.get("area_id") != meta.area_id or not registry.get("records") or not poi.get("features"):
            raise ValueError("invalid_original_documents")
        bundles.append({"matching_snapshot_id": snapshot_id, "matching_sha256": meta.content_hash,
                        "report": report, "registry": registry, "poi": poi})
        verification_cases.append({"case_id": f"V{len(verification_cases):02d}",
                                   "source_run_id": run.run_id,
                                   "source_record_id": registry["records"][0]["source_record_id"],
                                   "poi_id": poi["features"][0]["id"],
                                   "official_registry_snapshot_id": registry_id,
                                   "osm_poi_snapshot_id": poi_id,
                                   "label": "unresolved", "rationale": "Provenance-only internal validation",
                                   "evidence": ["original registry and POI snapshots"]})
    # Reuse the full validation chain (including locality snapshots) without publishing its
    # provisional labels or any metrics. The temporary document is always removed.
    verification = {"schema_version": 1, "benchmark_id": "internal-provenance-only",
                    "selection_protocol": "internal provenance checks only", "review_basis": REVIEW_BASIS,
                    "cases": verification_cases}
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "verification.json"
        path.write_text(json.dumps(verification, ensure_ascii=False), encoding="utf-8")
        build_audit(path, store, snapshot_ids)
    return bundles, benchmark


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--matching-snapshot-id", action="append", required=True)
    parser.add_argument("--exclude-benchmark", type=Path, required=True)
    parser.add_argument("--seed", default="e04-followup-20261102-v1")
    parser.add_argument("--per-stratum", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output_dir.exists():
            raise ValueError("output_directory_exists")
        bundles, benchmark = prepare_from_store(SnapshotStore(args.store),
                                                 args.matching_snapshot_id, args.exclude_benchmark)
        result = prepare_review(bundles, benchmark, seed=args.seed, per_stratum=args.per_stratum)
        root = Path(__file__).resolve().parents[1]
        result["manifest"]["frozen_code_sha256"] = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in CODE_PATHS}
        args.output_dir.mkdir(parents=True, exist_ok=False)
        artifacts = {}
        for key, filename in (("blind_packet", "blind-packet.json"),
                              ("benchmark_template", "benchmark-template.json")):
            publish_exclusive(args.output_dir / filename, result[key])
            artifacts[filename] = hashlib.sha256((args.output_dir / filename).read_bytes()).hexdigest()
        result["manifest"]["artifact_raw_sha256"] = artifacts
        publish_exclusive(args.output_dir / "manifest.json", result["manifest"])
        artifacts["manifest.json"] = hashlib.sha256((args.output_dir / "manifest.json").read_bytes()).hexdigest()
        # Completion marker is written last; no files are replaced.
        publish_exclusive(args.output_dir / "complete.json", {"artifact_raw_sha256": artifacts})
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"heritage review preparation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
