"""Private subprocess entry point for the offline OSM parser benchmark."""

import argparse
import cProfile
import json
import os
from pathlib import Path

from src.data_models.study_area import StudyArea


def baseline(pbf, area, directory):
    from src.etl.osm_extract_parsing import extract_layers
    return extract_layers(pbf, area, directory)


def native(pbf, area, directory):
    from src.etl.osm_extract_parsing import extract_layers
    return extract_layers(pbf, area, directory, native_tag_filter=True)


def rust(pbf, area, directory, executable):
    from benchmarks.osm_rust_bridge import extract_layers_rust
    return extract_layers_rust(pbf, area, directory, executable=executable)


MODES = {"baseline": baseline, "native": native, "rust": rust}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, required=True)
    parser.add_argument("--area", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--rust-executable", type=Path)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    if (args.mode == "rust") != (args.rust_executable is not None):
        parser.error("--rust-executable is required only for rust mode")
    area = StudyArea.from_dict(json.loads(args.area.read_text(encoding="utf-8")))
    profiler = cProfile.Profile() if args.profile else None
    before = os.times()
    if profiler:
        profiler.enable()
    try:
        if args.mode == "rust":
            paths, counts, timestamp = rust(args.pbf, area, args.directory, args.rust_executable)
        else:
            paths, counts, timestamp = MODES[args.mode](args.pbf, area, args.directory)
    finally:
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(args.directory / "profile.pstats"))
    after = os.times()
    (args.directory / "result.json").write_text(json.dumps({
        "paths": {role: str(path) for role, path in paths.items()},
        "counts": counts, "source_timestamp": timestamp,
        "cpu_user_s": after.user - before.user, "cpu_system_s": after.system - before.system,
        "spawned_cpu_user_s": after.children_user - before.children_user,
        "spawned_cpu_system_s": after.children_system - before.children_system,
    }, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
