"""Offline developer-curated review of immutable advisory heritage proposals."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.etl.heritage_validation import (MAX_BYTES, MAX_REPORTS, evaluate_heritage_benchmark,
                                          strict_json, validate_heritage_benchmark)
from src.etl.heritage_locality import PARSER_VERSION, SOURCE_ID, validate_locality_context
from src.storage import SnapshotStore


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _bounded(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    _require(len(data) <= MAX_BYTES, "document_too_large")
    return data


def _document(store, snapshot_id, source_id, area_id=None):
    meta = store.verify_content(snapshot_id)
    _require(meta.source_id == source_id and (area_id is None or meta.area_id == area_id),
             "invalid_snapshot_provenance")
    _require(meta.byte_length <= MAX_BYTES, "document_too_large")
    document = strict_json(store.read_content(snapshot_id))
    _require(isinstance(document, dict), "invalid_snapshot_document")
    return meta, document


def _context_snapshot(store, snapshot_id, source_id, parser_version, area_id, raw_hash=None):
    meta = store.verify_content(snapshot_id)
    _require(meta.source_id == source_id and meta.parser_version == parser_version
             and (area_id is None or meta.area_id == area_id)
             and (raw_hash is None or meta.content_hash == raw_hash)
             and meta.byte_length <= 2 * 1024 * 1024
             and meta.media_type == "application/json"
             and meta.completeness == ("partial" if source_id == SOURCE_ID else "complete"),
             "invalid_context_snapshot")
    raw = store.read_content(snapshot_id)
    document = strict_json(raw)
    _require(isinstance(document, list) and 1 <= len(document) <= 5
             and all(isinstance(record, dict) and record.get("osm_type") in ("node", "way", "relation")
                     and type(record.get("osm_id")) is int and record["osm_id"] > 0
                     for record in document), "invalid_context_document")
    return meta


def build_audit(benchmark_path: Path, store: SnapshotStore, snapshot_ids: list[str]):
    raw = _bounded(benchmark_path)
    benchmark = strict_json(raw)
    validate_heritage_benchmark(benchmark)
    _require(0 < len(snapshot_ids) <= MAX_REPORTS and len(set(snapshot_ids)) == len(snapshot_ids),
             "invalid_matching_snapshots")
    reports, references, matching, contexts = [], {}, [], {}
    for snapshot_id in snapshot_ids:
        meta, report = _document(store, snapshot_id, "heritage_matching")
        _require(report.get("area_id") == meta.area_id, "report_area_mismatch")
        run = store.get_run(report.get("source_run_id"))
        _require(run.area_id == meta.area_id and report.get("osm_poi_snapshot_id") in run.snapshot_ids,
                 "parent_run_poi_mismatch")
        registry_id, poi_id = report.get("official_registry_snapshot_id"), report["osm_poi_snapshot_id"]
        _require(isinstance(registry_id, str) and isinstance(poi_id, str), "invalid_report_references")
        if registry_id not in references:
            registry_meta, registry = _document(store, registry_id, "mkrf_egrkn", meta.area_id)
            _require(registry.get("area_id") == meta.area_id and isinstance(registry.get("records"), list),
                     "invalid_registry_document")
            _require(all(isinstance(item, dict) for item in registry["records"]),
                     "invalid_registry_records")
            record_ids = [item.get("source_record_id") for item in registry["records"]]
            _require(all(isinstance(item, str) for item in record_ids) and
                     len(record_ids) == len(set(record_ids)), "invalid_registry_records")
            references[registry_id] = (registry_meta.content_hash, set(record_ids))
        else:
            _require(store.get_snapshot(registry_id).area_id == meta.area_id, "registry_area_mismatch")
        if poi_id not in references:
            poi_meta, poi = _document(store, poi_id, "geofabrik_poi", meta.area_id)
            _require(poi.get("type") == "FeatureCollection" and isinstance(poi.get("features"), list),
                     "invalid_poi_document")
            _require(all(isinstance(item, dict) for item in poi["features"]),
                     "invalid_poi_features")
            poi_ids = [item.get("id") for item in poi["features"]]
            _require(all(isinstance(item, str) for item in poi_ids) and
                     len(poi_ids) == len(set(poi_ids)), "invalid_poi_features")
            references[poi_id] = (poi_meta.content_hash, set(poi_ids))
        else:
            _require(store.get_snapshot(poi_id).area_id == meta.area_id, "poi_area_mismatch")
        for channel in ("identifier_links", "review_candidates", "excluded_review_candidates"):
            if channel == "excluded_review_candidates" and channel not in report:
                continue
            _require(isinstance(report.get(channel), list), "invalid_report_channel")
            for item in report[channel]:
                _require(isinstance(item, dict) and
                          item.get("source_record_id") in references[registry_id][1] and
                          item.get("poi_id") in references[poi_id][1], "foreign_report_pair")
                evidence = item.get("evidence")
                if isinstance(evidence, dict) and evidence.get("official_address_locality_context") is not None:
                    context = validate_locality_context(evidence["official_address_locality_context"])
                    rebound_id = context["source_snapshot_id"]
                    _require(rebound_id not in contexts or contexts[rebound_id] == context,
                             "conflicting_locality_context")
                    rebound = _context_snapshot(store, rebound_id, SOURCE_ID, PARSER_VERSION,
                                                meta.area_id, context["raw_sha256"])
                    original = _context_snapshot(store, context["original_snapshot_id"],
                                                 "osm_nominatim", "nominatim-boundary-v1", None,
                                                 context["raw_sha256"])
                    _require(rebound.url == original.url and rebound.retrieved_at == original.retrieved_at,
                             "context_provenance_mismatch")
                    contexts[rebound_id] = context
                    references[rebound_id] = (rebound.content_hash, set())
                    references[original.snapshot_id] = (original.content_hash, set())
        if "address_locality_context" in report:
            summary = report["address_locality_context"]
            _require(isinstance(summary, dict) and isinstance(summary.get("snapshot_ids"), list),
                     "invalid_locality_summary")
            for context_id in summary["snapshot_ids"]:
                _require(isinstance(context_id, str), "invalid_locality_summary")
                rebound = _context_snapshot(store, context_id, SOURCE_ID, PARSER_VERSION, meta.area_id)
                references[context_id] = (rebound.content_hash, set())
        for case in benchmark["cases"]:
            if case["source_run_id"] == run.run_id:
                _require(case["official_registry_snapshot_id"] == registry_id and
                         case["osm_poi_snapshot_id"] == poi_id, "snapshot_reference_mismatch")
                _require(case["source_record_id"] in references[registry_id][1] and
                         case["poi_id"] in references[poi_id][1], "foreign_case_pair")
        reports.append(report)
        matching.append({"snapshot_id": snapshot_id, "sha256": meta.content_hash})
    result = evaluate_heritage_benchmark(benchmark, reports)
    result["audit"] = {"benchmark_sha256": hashlib.sha256(raw).hexdigest(),
                       "matching_snapshots": sorted(matching, key=lambda x: x["snapshot_id"]),
                       "input_snapshots": [{"snapshot_id": sid, "sha256": references[sid][0]}
                                           for sid in sorted(references)]}
    return result


def publish_exclusive(path: Path, result: dict):
    content = (json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    _require(len(content) <= MAX_BYTES, "output_too_large")
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
            pending = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "analysis")
    parser.add_argument("--matching-snapshot-id", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_audit(args.benchmark, SnapshotStore(args.store), args.matching_snapshot_id)
        publish_exclusive(args.output, result)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"heritage validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
