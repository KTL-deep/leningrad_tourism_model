"""Offline, fresh-process benchmark of the verified cached OSM PBF parser."""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import pstats
import subprocess
import sys
import tempfile
import time

from src.data_models.study_area import StudyArea

ROOT = Path(__file__).resolve().parents[1]
CODE_FILES = (ROOT / "src/etl/osm_extract_parsing.py", ROOT / "src/etl/osm_parsing.py",
              ROOT / "src/data_models/study_area.py", ROOT / "scripts/_benchmark_worker.py",
              ROOT / "scripts/benchmark_osm.py")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid)


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def summarize_features(path):
    from shapely.geometry import shape

    features = {}
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError(f"duplicate JSON key: {key}")
            obj[key] = value
        return obj

    def invalid(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            feature = json.loads(line, object_pairs_hook=pairs, parse_constant=invalid)
            _finite(feature)
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise ValueError("invalid feature")
            identifier = feature.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in features:
                raise ValueError(f"missing or duplicate feature id: {identifier}")
            geometry = feature.get("geometry")
            if (not isinstance(geometry, dict) or shape(geometry).is_empty
                    or not shape(geometry).is_valid):
                raise ValueError(f"invalid geometry: {identifier}")
            canonical = json.dumps(feature, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False)
            features[identifier] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ordered = sorted(features.items())
    digest = hashlib.sha256()
    for identifier, feature_hash in ordered:
        digest.update(json.dumps([identifier, feature_hash], ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return {"count": len(ordered), "sha256": digest.hexdigest(), "features": ordered}


def _stop_tree(process, psutil):
    try:
        children = process.children(recursive=True)
        for child in children:
            child.terminate()
        process.terminate()
        _, alive = psutil.wait_procs(children + [process], timeout=3)
        for item in alive:
            item.kill()
    except psutil.NoSuchProcess:
        pass


def run_worker(pbf, area, directory, mode, timeout, profile, psutil, rust_executable=None):
    command = [sys.executable, "-m", "scripts._benchmark_worker", "--pbf", str(pbf),
               "--area", str(area), "--directory", str(directory), "--mode", mode]
    if profile:
        command.append("--profile")
    if rust_executable is not None:
        command.extend(("--rust-executable", str(rust_executable)))
    start = time.monotonic()
    with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          text=True) as child:
        process = psutil.Process(child.pid)
        peak_rss = 0
        peak_os_wset = None
        sampled_cpu = {}
        try:
            while child.poll() is None:
                try:
                    members = [process] + process.children(recursive=True)
                    memory = [member.memory_info() for member in members]
                    for member in members:
                        cpu = member.cpu_times()
                        sampled_cpu[(member.pid, member.create_time())] = (cpu.user, cpu.system)
                    rss = sum(item.rss for item in memory)
                    peak_rss = max(peak_rss, rss)
                    if os.name == "nt" and hasattr(memory[0], "peak_wset"):
                        peak_os_wset = max(peak_os_wset or 0, memory[0].peak_wset)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                if time.monotonic() - start > timeout:
                    _stop_tree(process, psutil)
                    child.communicate()
                    raise TimeoutError(f"worker exceeded {timeout}s")
                time.sleep(0.02)
            stderr = child.communicate()[1]
        finally:
            if child.poll() is None:
                _stop_tree(process, psutil)
    wall = time.monotonic() - start
    if child.returncode:
        raise RuntimeError(f"{mode} worker exited {child.returncode}: {stderr[-2000:]}")
    result = strict_json(directory / "result.json")
    layers = {}
    for role in ("poi", "network"):
        expected = (directory / (role + ".features")).resolve()
        if Path(result["paths"][role]).resolve() != expected:
            raise ValueError(f"unexpected {role} output path")
        layers[role] = summarize_features(expected)
        if layers[role]["count"] != result["counts"][role]:
            raise ValueError(f"{role} count differs from NDJSON")
    summary = {"mode": mode, "wall_s": wall, "cpu_user_s": result["cpu_user_s"],
               "cpu_system_s": result["cpu_system_s"],
               "spawned_cpu_user_s": result["spawned_cpu_user_s"],
               "spawned_cpu_system_s": result["spawned_cpu_system_s"],
               "peak_sampled_rss_bytes": peak_rss,
               "tree_sampled_cpu_user_s": sum(cpu[0] for cpu in sampled_cpu.values()),
               "tree_sampled_cpu_system_s": sum(cpu[1] for cpu in sampled_cpu.values()),
               "tree_cpu_measurement": "sum of last observed per-process CPU; lower bound; includes imports",
               "sample_interval_s": 0.02,
               "spawned_os_times_available": os.name != "nt",
               "worker_os_peak_wset_bytes": peak_os_wset, "counts": result["counts"],
               "source_timestamp": result["source_timestamp"], "layers": layers,
               "artifact_dir": str(directory)}
    if profile:
        stats_path = directory / "profile.pstats"
        hotspots = {}
        for sort in ("cumulative", "tottime"):
            output = io.StringIO()
            pstats.Stats(str(stats_path), stream=output).sort_stats(sort).print_stats(20)
            hotspots[sort + "_top20"] = output.getvalue()
        summary["profile"] = {"file": str(stats_path), "scope": "Python worker only; excludes external Rust process",
                              **hotspots}
    return summary


def compare_runs(reference, candidate):
    mismatches = {}
    for role in ("poi", "network"):
        expected = dict(reference["layers"][role]["features"])
        actual = dict(candidate["layers"][role]["features"])
        mismatches[role] = {"missing_ids": sorted(expected.keys() - actual.keys()),
                            "extra_ids": sorted(actual.keys() - expected.keys()),
                            "changed_ids": sorted(identifier for identifier in expected.keys() & actual.keys()
                                                  if expected[identifier] != actual[identifier])}
    return {"matches_reference": not any(any(ids for ids in role.values()) for role in mismatches.values())
            and candidate["counts"] == reference["counts"]
            and candidate["source_timestamp"] == reference["source_timestamp"],
            "feature_mismatches": mismatches,
            "counts_match": candidate["counts"] == reference["counts"],
            "timestamp_match": candidate["source_timestamp"] == reference["source_timestamp"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--area", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--mode", choices=("baseline", "native", "rust"), default="baseline")
    parser.add_argument("--rust-executable", type=Path)
    parser.add_argument("--compare-report", type=Path,
                        help="compare semantic output with an earlier benchmark report for the same input")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    if args.repetitions < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("repetitions and timeout must be positive")
    if (args.mode == "rust") != (args.rust_executable is not None):
        parser.error("--rust-executable is required only for rust mode")
    try:
        import psutil
    except ImportError:
        parser.error("psutil required for benchmark: install psutil in this environment")
    pbf = args.pbf.resolve(strict=True)
    area_path = args.area.resolve(strict=True)
    area = StudyArea.from_dict(strict_json(area_path))
    if args.output.exists():
        parser.error("output exists; refusing to overwrite")
    if not pbf.is_file():
        parser.error("PBF must be a file")
    source_sha = sha256_file(pbf)
    if not pbf.name.lower().endswith(".osm.pbf." + source_sha):
        parser.error("PBF is not a verified content-addressed cached .osm.pbf.<sha256> file")
    code_hashes = {str(path.relative_to(ROOT)): sha256_file(path) for path in CODE_FILES}
    rust_executable = None
    if args.mode == "rust":
        rust_executable = args.rust_executable.resolve(strict=True)
        if not rust_executable.is_file():
            parser.error("rust executable must be a file")
        bridge = ROOT / "benchmarks/osm_rust_bridge.py"
        code_hashes[str(bridge.relative_to(ROOT))] = sha256_file(bridge)
        code_hashes[str(rust_executable)] = sha256_file(rust_executable)
    try:
        from importlib.metadata import version, PackageNotFoundError
        osmium_version = version("osmium")
    except (ImportError, PackageNotFoundError):
        osmium_version = "unavailable"
    base = args.work_dir.resolve() if args.work_dir else None
    if base:
        base.mkdir(parents=True, exist_ok=True)
    runs = []
    for _ in range(args.repetitions):
        directory = Path(tempfile.mkdtemp(prefix="osm-benchmark-", dir=base))
        runs.append(run_worker(pbf, area_path, directory, args.mode, args.timeout,
                               args.profile, psutil, rust_executable))
    reference = runs[0]
    comparisons = []
    for run in runs[1:]:
        comparisons.append({"mode": run["mode"], **compare_runs(reference, run)})
    if args.compare_report:
        previous = strict_json(args.compare_report)
        if (previous["source"]["sha256"] != source_sha or previous["area_id"] != area.area_id):
            parser.error("comparison report must use the same source and StudyArea")
        comparisons.insert(0, {"mode": reference["mode"], "reference_report": str(args.compare_report),
                               **compare_runs(previous["runs"][0], reference)})
    report = {"source": {"path": str(pbf), "sha256": source_sha, "size_bytes": pbf.stat().st_size,
                         "verification": "content-addressed filename matches streamed SHA-256",
                         "cache_warmness": "unknown (not a cold-cache measurement)"},
              "area_id": area.area_id, "area_sha256": sha256_file(area_path),
              "environment": {"python": sys.version, "osmium": osmium_version,
                              "platform": platform.platform(), "cpu": platform.processor(),
                              "cpu_count": os.cpu_count()},
              "code_sha256": code_hashes, "instrumentation": "cProfile enabled; wall/CPU are not comparable to unprofiled runs"
              if args.profile else "none (unprofiled)",
              "runs": runs, "comparisons": comparisons}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return 0 if all(item["matches_reference"] for item in comparisons) else 2


if __name__ == "__main__":
    raise SystemExit(main())
