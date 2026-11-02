"""Deterministic, model-output-conditioned blind follow-up sampling (not ground truth)."""

import hashlib
import json

from src.etl.heritage_validation import (MAX_REPORTS, REVIEW_BASIS, STATUSES,
                                         EXCLUSION_REASONS, _copy, _require, _text)


CHANNELS = ("identifier_links", "review_candidates", "excluded_review_candidates")
SELECTION_PROTOCOL = ("Stratified post-freeze follow-up, model-output conditioned biased selection; "
                      "entity-disjoint from development set, not external ground truth or area recall.")
METHOD = "sha256-canonical-json-global-greedy-entity-disjoint-v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def prepare_review(bundles, development_benchmark, *, seed, per_stratum=4):
    """Select from frozen proposals; return provenance, raw-evidence packet and unlabelled template."""
    _require(_text(seed) and type(per_stratum) is int and 1 <= per_stratum <= 20,
             "invalid_selection_parameters")
    bundles, development_benchmark = _copy(bundles), _copy(development_benchmark)
    _require(isinstance(development_benchmark, dict) and
             isinstance(development_benchmark.get("cases"), list), "invalid_development_benchmark")
    excluded_records, excluded_pois = set(), set()
    for case in development_benchmark["cases"]:
        _require(isinstance(case, dict) and _text(case.get("source_record_id")) and
                 _text(case.get("poi_id")), "invalid_development_case")
        excluded_records.add(case["source_record_id"])
        excluded_pois.add(case["poi_id"])
    _require(isinstance(bundles, list) and 0 < len(bundles) <= MAX_REPORTS,
             "invalid_bundles")
    strata, proposals, snapshots, runs = {}, [], [], set()
    for bundle in bundles:
        _require(isinstance(bundle, dict) and _text(bundle.get("matching_snapshot_id")) and
                 _text(bundle.get("matching_sha256")) and
                 isinstance(bundle.get("report"), dict) and
                 isinstance(bundle.get("registry"), dict) and
                 isinstance(bundle.get("poi"), dict), "invalid_bundle")
        report, registry, poi = bundle["report"], bundle["registry"], bundle["poi"]
        run = report.get("source_run_id")
        _require(report.get("artifact_kind") == "advisory_heritage_matching" and
                 type(report.get("schema_version")) is int and report["schema_version"] == 1 and
                 _text(report.get("algorithm_version")) and _text(run) and run not in runs and
                 all(_text(report.get(key)) for key in ("official_registry_snapshot_id",
                                                      "osm_poi_snapshot_id")), "invalid_matching_report")
        runs.add(run)
        _require(isinstance(registry.get("records"), list) and
                 poi.get("type") == "FeatureCollection" and isinstance(poi.get("features"), list),
                 "invalid_original_documents")
        records, features = {}, {}
        for item in registry["records"]:
            _require(isinstance(item, dict) and _text(item.get("source_record_id")) and
                     item["source_record_id"] not in records, "invalid_registry_records")
            records[item["source_record_id"]] = item
        for item in poi["features"]:
            _require(isinstance(item, dict) and _text(item.get("id")) and
                     item["id"] not in features, "invalid_poi_features")
            features[item["id"]] = item
        seen = set()
        for channel in CHANNELS:
            entries = report.get(channel, [] if channel == "excluded_review_candidates" else None)
            _require(isinstance(entries, list), "invalid_report_channel")
            stratum = (run, channel)
            strata[stratum] = {"source_run_id": run, "channel": channel,
                                "eligible": 0, "selected": 0, "shortfall": per_stratum}
            for entry in entries:
                _require(isinstance(entry, dict) and _text(entry.get("source_record_id")) and
                         _text(entry.get("poi_id")), "invalid_report_pair")
                record, feature = entry["source_record_id"], entry["poi_id"]
                _require(record in records and feature in features, "foreign_report_pair")
                pair = (record, feature)
                _require(pair not in seen, "duplicate_output_pair")
                seen.add(pair)
                if channel == "excluded_review_candidates":
                    _require(entry.get("status") == "excluded_name_candidate" and
                             entry.get("reason") in EXCLUSION_REASONS and
                             isinstance(entry.get("evidence"), dict) and bool(entry["evidence"]),
                             "invalid_excluded_pair")
                else:
                    _require(entry.get("status") in STATUSES[channel], "invalid_report_pair")
                if record in excluded_records or feature in excluded_pois:
                    continue
                strata[stratum]["eligible"] += 1
                key = [seed, run, channel, record, feature]
                proposals.append((digest(key), canonical(key), stratum, record, feature,
                                  report, records[record], features[feature]))
        snapshots.append({"snapshot_id": bundle["matching_snapshot_id"],
                          "sha256": bundle["matching_sha256"], "source_run_id": run,
                          "algorithm_version": report["algorithm_version"],
                          "official_registry_snapshot_id": report["official_registry_snapshot_id"],
                          "osm_poi_snapshot_id": report["osm_poi_snapshot_id"]})
    _require(len({item["snapshot_id"] for item in snapshots}) == len(snapshots),
             "duplicate_matching_snapshot")
    selected, used_records, used_pois = [], set(), set()
    for _, _, stratum, record, feature, report, original, osm in sorted(proposals):
        if (len(selected) >= 120 or strata[stratum]["selected"] >= per_stratum or record in used_records or
                feature in used_pois):
            continue
        used_records.add(record)
        used_pois.add(feature)
        strata[stratum]["selected"] += 1
        strata[stratum]["shortfall"] -= 1
        selected.append((stratum, record, feature, report, original, osm))
    # Permute independently of proposal ranking before revealing case numbers.
    selected.sort(key=lambda item: (digest([seed, "blind-order-v1", item[3]["source_run_id"],
                                             item[1], item[2]]), item[3]["source_run_id"],
                                    item[1], item[2]))
    packet_cases, template_cases, mapping = [], [], []
    for number, (stratum, record, feature, report, original, osm) in enumerate(selected, 1):
        case_id = f"H{number:02d}"
        identity = {"case_id": case_id, "source_run_id": report["source_run_id"],
                    "source_record_id": record, "poi_id": feature,
                    "official_registry_snapshot_id": report["official_registry_snapshot_id"],
                    "osm_poi_snapshot_id": report["osm_poi_snapshot_id"]}
        packet_cases.append({**identity, "official_record": original, "osm_feature": osm})
        template_cases.append({**identity, "label": None, "rationale": "", "evidence": []})
        mapping.append({**identity, "stratum": {"source_run_id": stratum[0], "channel": stratum[1]}})
    blind_packet = {"schema_version": 1, "cases": packet_cases}
    template = {"schema_version": 1, "benchmark_id": "e04-followup-" +
                digest([seed, sorted(snapshots, key=lambda s: s["snapshot_id"])])[:16],
                "selection_protocol": SELECTION_PROTOCOL, "review_basis": REVIEW_BASIS,
                "cases": template_cases}
    manifest = {"schema_version": 1, "seed": seed, "method": METHOD,
                "constraints": {"per_stratum": per_stratum, "max_pairs": 120,
                                "development_endpoint_exclusion": "global",
                                "sample_endpoint_reuse": False},
                "selection_protocol": SELECTION_PROTOCOL,
                "excluded_development_benchmark_sha256": digest(development_benchmark),
                "blind_packet_sha256": digest(blind_packet),
                "matching_snapshots": sorted(snapshots, key=lambda s: s["snapshot_id"]),
                "strata": [strata[key] for key in sorted(strata)], "cases": mapping}
    return {"manifest": manifest, "blind_packet": blind_packet,
            "benchmark_template": template}
