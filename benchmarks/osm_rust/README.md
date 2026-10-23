# Experimental Rust PBF preselector

This is **not** a replacement for the production osmium parser. The CLI scans one PBF,
stores every node in a sparse-ID, sorted, fixed-width disk index (16 bytes per node),
and emits tagged nodes in the bbox plus broadly tagged ways whose *full* node bbox
overlaps it. The Python bridge validates this bounded stream, builds a small PBF,
then runs the existing `extract_layers` for the authoritative geometry, categories,
network lengths, and timestamp. Relations are unsupported in both parsers. The
Rust stage reports scan counts on stderr and never emits success on failure.

The input must have strictly increasing node IDs and all nodes before ways; missing
references in relevant ways fail explicitly. Input is capped at 768 MiB, index at
2 GiB, each way at 1 million refs, and candidate NDJSON at 200,000 records / 64 MiB.
The Python bridge caps reconstructed supporting nodes at 1 million. The index
directory and NDJSON path must not exist; index scratch is deleted after the scan.
No network operations are performed at runtime. There is no measured speed claim.

Build with installed GNU Rust and generate/keep `Cargo.lock`:

```powershell
$env:RUSTUP_HOME='C:\Users\KTL\AppData\Local\Temp\opencode\rustup-osm-benchmark'
$env:CARGO_HOME='D:\PYTHON_PROJECTS\leningrad_tourism_model-1\data\tooling\cargo'
& "$env:CARGO_HOME\bin\cargo.exe" +1.99.1-x86_64-pc-windows-gnu build --release --locked --manifest-path benchmarks/osm_rust/Cargo.toml
& "$env:CARGO_HOME\bin\cargo.exe" +1.99.1-x86_64-pc-windows-gnu test --locked --manifest-path benchmarks/osm_rust/Cargo.toml
$env:OSM_RUST_PRESELECT=(Resolve-Path benchmarks/osm_rust/target/release/osm_rust_preselect.exe).Path
.\.venv-app\Scripts\python.exe -m pytest tests/test_osm_rust_bridge.py -q
```

API: `benchmarks.osm_rust_bridge.extract_layers_rust(pbf, area, directory,
executable=path)` returns `(paths, counts, source_timestamp)` exactly like
`src.etl.osm_extract_parsing.extract_layers`. Time the **entire bridge call**,
including the Rust scan, disk index, candidate reconstruction, and Python parse.
For a comparison, run against the same verified content-addressed regional PBF and
StudyArea used by the Python control and compare normalized features and timestamps.
