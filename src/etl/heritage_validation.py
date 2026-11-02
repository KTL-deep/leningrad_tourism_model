"""Curated-pair evidence review of advisory heritage matching, not ground truth."""

import json
import math

from src.etl.heritage_locality import validate_locality_context


MAX_CASES = 10000
MAX_REPORTS = 100
MAX_BYTES = 64 * 1024 * 1024
REVIEW_BASIS = "developer_evidence_review_not_independent_ground_truth"
SCOPE = "curated_case_pairs_only_not_population_accuracy_or_geographic_recall"
LABELS = {"supported", "contradicted", "unresolved"}
STATUSES = {"identifier_links": {"identifier_link", "ambiguous_identifier"},
            "review_candidates": {"name_only_unlocated_candidate", "name_distance_review_candidate"}}
EXCLUSION_REASONS = {"osm_non_object_subject_role",
                     "unlocated_name_conflicts_with_explicit_registry_reference",
                     "unlocated_address_locality_conflict"}


def strict_json(data: bytes):
    """Parse finite JSON, rejecting duplicate object keys."""
    if len(data) > MAX_BYTES:
        raise ValueError("document_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite_json")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite_json")
        return number

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=finite_float)


def _copy(value):
    try:
        data = json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return strict_json(data)
    except (TypeError, OverflowError, UnicodeError) as exc:
        raise ValueError("invalid_json") from exc


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _validate_entry_context(entry):
    evidence = entry.get("evidence")
    if isinstance(evidence, dict) and "official_address_locality_context" in evidence:
        context = evidence["official_address_locality_context"]
        if context is not None:
            validate_locality_context(context)


def validate_heritage_benchmark(benchmark):
    _require(isinstance(benchmark, dict), "invalid_benchmark")
    _require(type(benchmark.get("schema_version")) is int and benchmark["schema_version"] == 1,
             "invalid_benchmark_schema")
    for key in ("benchmark_id", "selection_protocol"):
        _require(_text(benchmark.get(key)), "invalid_" + key)
    _require(benchmark.get("review_basis") == REVIEW_BASIS, "invalid_review_basis")
    cases = benchmark.get("cases")
    _require(isinstance(cases, list) and len(cases) <= MAX_CASES, "invalid_cases")
    ids, pairs = set(), set()
    for case in cases:
        _require(isinstance(case, dict), "invalid_case")
        for key in ("case_id", "source_run_id", "source_record_id", "poi_id",
                    "official_registry_snapshot_id", "osm_poi_snapshot_id", "rationale"):
            _require(_text(case.get(key)), "invalid_case_" + key)
        _require(isinstance(case.get("label"), str) and case["label"] in LABELS, "invalid_label")
        evidence = case.get("evidence")
        _require(isinstance(evidence, list) and bool(evidence) and
                 all(_text(ref) for ref in evidence), "invalid_evidence")
        if "stratum" in case:
            _require(_text(case["stratum"]), "invalid_stratum")
        pair = tuple(case[key] for key in ("source_run_id", "source_record_id", "poi_id"))
        _require(case["case_id"] not in ids and pair not in pairs, "duplicate_case")
        ids.add(case["case_id"])
        pairs.add(pair)


def _metrics(decisions, channel):
    counts = {key: 0 for key in ("emitted_supported", "emitted_contradicted",
                                  "emitted_unresolved", "missed_supported", "missed_contradicted",
                                  "missed_unresolved", "resolved_emitted", "supported_total")}
    for decision in decisions:
        label = decision["label"]
        emitted = decision[channel]["present"]
        counts[("emitted_" if emitted else "missed_") + label] += 1
        if label == "supported":
            counts["supported_total"] += 1
        if emitted and label != "unresolved":
            counts["resolved_emitted"] += 1
    counts["correspondence_rate_on_resolved_emitted_pairs"] = (
        counts["emitted_supported"] / counts["resolved_emitted"]
        if counts["resolved_emitted"] else None)
    counts["retrieval_rate_on_reviewed_supported_pairs"] = (
        counts["emitted_supported"] / counts["supported_total"]
        if counts["supported_total"] else None)
    return counts


def evaluate_heritage_benchmark(benchmark, reports: list[dict]) -> dict:
    """Score only explicitly reviewed case pairs; missing output is a miss."""
    benchmark, reports = _copy(benchmark), _copy(reports)
    validate_heritage_benchmark(benchmark)
    _require(isinstance(reports, list) and len(reports) <= MAX_REPORTS, "invalid_reports")
    by_run = {}
    for report in reports:
        _require(isinstance(report, dict) and report.get("artifact_kind") == "advisory_heritage_matching"
                 and type(report.get("schema_version")) is int and report["schema_version"] == 1
                 and _text(report.get("algorithm_version")), "invalid_matching_report")
        for key in ("source_run_id", "official_registry_snapshot_id", "osm_poi_snapshot_id"):
            _require(_text(report.get(key)), "invalid_report_" + key)
        run_id = report["source_run_id"]
        _require(run_id not in by_run, "duplicate_report_run")
        pairs = {}
        for channel, statuses in STATUSES.items():
            entries = report.get(channel)
            _require(isinstance(entries, list), "invalid_report_channel")
            for entry in entries:
                _require(isinstance(entry, dict) and _text(entry.get("source_record_id"))
                         and _text(entry.get("poi_id")) and isinstance(entry.get("status"), str)
                         and entry["status"] in statuses,
                          "invalid_report_pair")
                _validate_entry_context(entry)
                pair = (entry["source_record_id"], entry["poi_id"])
                _require(pair not in pairs, "duplicate_output_pair")
                pairs[pair] = {"channel": channel, "status": entry["status"]}
        excluded = {}
        entries = report.get("excluded_review_candidates", [])
        _require(isinstance(entries, list), "invalid_excluded_channel")
        for entry in entries:
            _require(isinstance(entry, dict) and _text(entry.get("source_record_id"))
                     and _text(entry.get("poi_id")) and entry.get("status") == "excluded_name_candidate"
                     and isinstance(entry.get("reason"), str) and entry["reason"] in EXCLUSION_REASONS
                     and isinstance(entry.get("evidence"), dict) and bool(entry["evidence"]),
                      "invalid_excluded_pair")
            _validate_entry_context(entry)
            pair = (entry["source_record_id"], entry["poi_id"])
            _require(pair not in pairs and pair not in excluded, "duplicate_output_pair")
            excluded[pair] = {"status": entry["status"], "reason": entry["reason"]}
        by_run[run_id] = (report, pairs, excluded)
    _require({c["source_run_id"] for c in benchmark["cases"]} == set(by_run),
             "missing_or_unreferenced_report")
    decisions = []
    for case in sorted(benchmark["cases"], key=lambda c: c["case_id"]):
        report, pairs, excluded = by_run[case["source_run_id"]]
        for key in ("official_registry_snapshot_id", "osm_poi_snapshot_id"):
            _require(report[key] == case[key], "snapshot_reference_mismatch")
        proposal = pairs.get((case["source_record_id"], case["poi_id"]))
        decision = {key: case[key] for key in ("case_id", "source_run_id", "source_record_id", "poi_id", "label")}
        decision["exclusion"] = excluded.get((case["source_record_id"], case["poi_id"]))
        if "stratum" in case:
            decision["stratum"] = case["stratum"]
        for channel in ("identifier_links", "candidate_retrieval"):
            present = proposal is not None and (channel == "candidate_retrieval" or
                                                proposal["channel"] == "identifier_links")
            decision[channel] = {"present": present, "status": proposal["status"] if present else None}
        decisions.append(decision)
    return {"schema_version": 1, "benchmark_id": benchmark["benchmark_id"],
            "selection_protocol": benchmark["selection_protocol"],
            "evaluated_algorithm_versions": sorted({report["algorithm_version"] for report in reports}),
            "review_basis": REVIEW_BASIS, "scope": SCOPE, "sample_biased": True,
            "auto_merges_evaluated": False, "external_ground_truth": False,
            "warnings": ["Developer-curated labels can depend on shared inputs and proposals; not independent ground truth.",
                         "Proposals are not verified physical facilities or access claims."],
            "channels": {channel: _metrics(decisions, channel)
                         for channel in ("identifier_links", "candidate_retrieval")},
            "cases": decisions}
