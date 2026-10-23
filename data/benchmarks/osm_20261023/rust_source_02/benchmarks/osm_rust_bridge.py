"""Experimental Rust candidate preselection; final semantics remain osm_extract_parsing's."""

import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

from shapely.geometry import shape

from src.etl.adapters.overpass_inventory import collection_bbox
from src.etl.osm_extract_parsing import extract_layers

MAX_INPUT = 768 * 1024 * 1024
MAX_CANDIDATES = 200_000
MAX_BYTES = 64 * 1024 * 1024
MAX_NODES = 1_000_000
MAX_REFS = 1_000_000
I64_MAX = 2**63 - 1


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate candidate JSON key")
        value[key] = item
    return value


def _integer(value):
    if type(value) is not int or not -I64_MAX - 1 <= value <= I64_MAX:
        raise ValueError("invalid OSM id")
    return value


def _coordinate(value, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("invalid candidate coordinate")
    if abs(value * 10_000_000 - round(value * 10_000_000)) > 0.00001:
        raise ValueError("unsupported candidate coordinate precision")
    # Compare source coordinates on the declared 1e-7 degree lattice, not an
    # arbitrary tolerance: distinct lattice points must still conflict.
    return round(value * 10_000_000) / 10_000_000


def _tags(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                              for k, v in value.items()):
        raise ValueError("invalid candidate tags")
    return value


def read_candidates(path):
    """Validate bounded Rust NDJSON; return sorted supporting nodes and candidate ways."""
    nodes = {}
    ways = {}
    selected_nodes = set()
    count = size = 0
    with Path(path).open("rb") as stream:
        for line in stream:
            size += len(line)
            count += 1
            if size > MAX_BYTES or count > MAX_CANDIDATES:
                raise ValueError("candidate output limit exceeded")
            if not line.endswith(b"\n"):
                raise ValueError("incomplete candidate line")
            item = json.loads(line, object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(item, dict):
                raise ValueError("invalid candidate object")
            kind = item.get("type")
            identifier = _integer(item.get("id"))
            tags = _tags(item.get("tags"))
            if kind == "node" and set(item) == {"type", "id", "lon", "lat", "tags"}:
                coord = (_coordinate(item["lon"], -180, 180), _coordinate(item["lat"], -90, 90))
                if identifier in selected_nodes:
                    raise ValueError("duplicate candidate node")
                if identifier in nodes and (nodes[identifier][0] != coord or nodes[identifier][1]):
                    raise ValueError("conflicting candidate node")
                selected_nodes.add(identifier)
                nodes[identifier] = (coord, tags)
            elif kind == "way" and set(item) == {"type", "id", "nodes", "coordinates", "tags"}:
                refs = item["nodes"]
                coords = item["coordinates"]
                if (not isinstance(refs, list) or not isinstance(coords, list) or len(refs) != len(coords)
                        or len(refs) > MAX_REFS or identifier in ways):
                    raise ValueError("invalid candidate way")
                for ref, coord in zip(refs, coords):
                    ref = _integer(ref)
                    if not isinstance(coord, list) or len(coord) != 2:
                        raise ValueError("invalid way coordinate")
                    location = (_coordinate(coord[0], -180, 180), _coordinate(coord[1], -90, 90))
                    previous = nodes.get(ref)
                    if previous is not None and previous[0] != location:
                        raise ValueError("conflicting shared node coordinates")
                    if previous is None:
                        nodes[ref] = (location, {})
                    if len(nodes) > MAX_NODES:
                        raise ValueError("supporting nodes limit exceeded")
                ways[identifier] = (refs, tags)
            else:
                raise ValueError("invalid candidate schema")
            if len(nodes) > MAX_NODES:
                raise ValueError("supporting nodes limit exceeded")
    return nodes, ways


def extract_layers_rust(pbf, area, directory, *, executable):
    """Run Rust plus reconstruction plus unchanged Python parser, all within caller's timer."""
    import osmium

    pbf = Path(pbf)
    directory = Path(directory)
    if pbf.stat().st_size > MAX_INPUT:
        raise ValueError("input limit exceeded")
    south, west, north, east = collection_bbox(area)
    # collection_bbox validates finite/range/geodesic area, including analysis geometry.
    if shape(area.analysis_geometry or area.request_geometry).is_empty:
        raise ValueError("empty request")
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rust-candidates-", dir=directory) as scratch:
        scratch = Path(scratch)
        candidates = scratch / "candidates.ndjson"
        command = [str(executable), "--pbf", str(pbf), "--bbox",
                   ",".join(map(repr, (south, west, north, east))), "--output", str(candidates),
                   "--index-dir", str(scratch / "index")]
        started = time.monotonic()
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        rust_wall = time.monotonic() - started
        if completed.returncode:
            raise RuntimeError(f"Rust preselection failed ({completed.returncode}): {completed.stderr[-1000:]}")
        started = time.monotonic()
        candidate_bytes = candidates.stat().st_size
        nodes, ways = read_candidates(candidates)
        validation_wall = time.monotonic() - started
        started = time.monotonic()
        reader = osmium.io.Reader(osmium.io.File(str(pbf), "pbf"))
        try:
            timestamp = reader.header().get("osmosis_replication_timestamp")
        finally:
            reader.close()
        header = osmium.io.Header()
        if timestamp:
            header.set("osmosis_replication_timestamp", timestamp)
        reduced = scratch / "selected.osm.pbf"
        with osmium.SimpleWriter(str(reduced), header=header) as writer:
            for identifier, (coord, tags) in sorted(nodes.items()):
                writer.add_node(osmium.osm.mutable.Node(
                    id=identifier, location=osmium.osm.Location(*coord), tags=tags))
            for identifier, (refs, tags) in sorted(ways.items()):
                writer.add_way(osmium.osm.mutable.Way(id=identifier, nodes=refs, tags=tags))
        write_wall = time.monotonic() - started
        reduced_bytes = reduced.stat().st_size
        started = time.monotonic()
        result = extract_layers(reduced, area, directory)
        stages = {
            "rust_process_wall_s": rust_wall,
            "candidate_validation_wall_s": validation_wall,
            "header_and_small_pbf_write_wall_s": write_wall,
            "final_python_parse_wall_s": time.monotonic() - started,
            "candidate_bytes": candidate_bytes, "reduced_pbf_bytes": reduced_bytes,
            "supporting_nodes": len(nodes), "candidate_ways": len(ways),
            "rust_stderr": completed.stderr[-4000:],
            "scope": "diagnostic phases; total benchmark includes all phases and setup/cleanup",
        }
        with (directory / "rust_stages.json").open("x", encoding="utf-8") as stream:
            json.dump(stages, stream, ensure_ascii=False, indent=2, allow_nan=False)
        return result
