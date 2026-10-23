use memmap2::Mmap;
use osmpbfreader::{OsmObj, OsmPbfReader};
use serde_json::json;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::Path;

const MAX_INPUT: u64 = 768 * 1024 * 1024;
const MAX_INDEX: u64 = 2 * 1024 * 1024 * 1024;
const MAX_OUTPUT: usize = 64 * 1024 * 1024;
const MAX_CANDIDATES: usize = 200_000;
const MAX_REFS: usize = 1_000_000;
const RECORD: usize = 16;

fn bbox(text: &str) -> Result<[f64; 4], String> {
    let parts: Vec<_> = text.split(',').map(str::parse::<f64>).collect();
    if parts.len() != 4 || parts.iter().any(|p| p.as_ref().map_or(true, |v| !v.is_finite())) {
        return Err("invalid bbox".into());
    }
    let b = [*parts[0].as_ref().unwrap(), *parts[1].as_ref().unwrap(),
             *parts[2].as_ref().unwrap(), *parts[3].as_ref().unwrap()];
    if b[0] < -90. || b[2] > 90. || b[1] < -180. || b[3] > 180.
        || b[0] >= b[2] || b[1] >= b[3] || b[3] - b[1] >= 180. {
        return Err("invalid bbox".into());
    }
    Ok(b)
}

fn relevant(tags: &osmpbfreader::Tags) -> bool {
    ["tourism", "historic", "amenity", "leisure", "shop", "railway", "highway", "public_transport"]
        .iter().any(|key| tags.contains_key(*key))
}

fn lookup(map: &Mmap, id: i64) -> Option<[f64; 2]> {
    let mut low = 0;
    let mut high = map.len() / RECORD;
    while low < high {
        let middle = low + (high - low) / 2;
        let start = middle * RECORD;
        let found = i64::from_le_bytes(map[start..start + 8].try_into().unwrap());
        if found < id { low = middle + 1; } else { high = middle; }
    }
    if low == map.len() / RECORD { return None; }
    let start = low * RECORD;
    if i64::from_le_bytes(map[start..start + 8].try_into().unwrap()) != id { return None; }
    let lon = i32::from_le_bytes(map[start + 8..start + 12].try_into().unwrap());
    let lat = i32::from_le_bytes(map[start + 12..start + 16].try_into().unwrap());
    Some([f64::from(lon) / 1e7, f64::from(lat) / 1e7])
}

fn overlaps(coords: &[[f64; 2]], b: [f64; 4]) -> bool {
    let (mut west, mut east, mut south, mut north) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for [lon, lat] in coords {
        west = west.min(*lon); east = east.max(*lon);
        south = south.min(*lat); north = north.max(*lat);
    }
    !coords.is_empty() && west <= b[3] && east >= b[1] && south <= b[2] && north >= b[0]
}

fn emit(out: &mut BufWriter<File>, value: serde_json::Value, count: &mut usize, bytes: &mut usize) -> Result<(), String> {
    let mut line = serde_json::to_vec(&value).map_err(|e| e.to_string())?;
    line.push(b'\n');
    *bytes = bytes.checked_add(line.len()).ok_or("candidate bytes overflow")?;
    if *count >= MAX_CANDIDATES || *bytes > MAX_OUTPUT { return Err("candidate output limit exceeded".into()); }
    out.write_all(&line).map_err(|e| e.to_string())?;
    *count += 1;
    Ok(())
}

fn scan(input: &Path, b: [f64; 4], output: &Path, index_dir: &Path) -> Result<(), String> {
    if fs::metadata(input).map_err(|e| e.to_string())?.len() > MAX_INPUT { return Err("input limit exceeded".into()); }
    fs::create_dir(index_dir).map_err(|e| format!("index directory must be new: {e}"))?;
    let mut created_output = false;
    let result = (|| -> Result<(), String> {
        let output_file = OpenOptions::new().write(true).create_new(true).open(output).map_err(|e| format!("output must be new: {e}"))?;
        created_output = true;
        let mut out = BufWriter::new(output_file);
        let file = File::open(input).map_err(|e| e.to_string())?;
        let mut reader = OsmPbfReader::new(file);
        let index_file = index_dir.join("nodes.bin");
        let mut writer = Some(BufWriter::new(File::create(&index_file).map_err(|e| e.to_string())?));
        let mut map: Option<Mmap> = None;
        let mut last_id: Option<i64> = None;
        let mut last_way_id: Option<i64> = None;
        let (mut nodes, mut ways, mut candidates, mut bytes) = (0_u64, 0_u64, 0_usize, 0_usize);
        for object in reader.iter() {
            match object.map_err(|e| e.to_string())? {
                OsmObj::Node(node) => {
                    if writer.is_none() { return Err("node after ways: unsupported source order".into()); }
                    let id = node.id.0;
                    if last_id.map_or(false, |previous| id <= previous) { return Err("duplicate or unsorted node ids".into()); }
                    last_id = Some(id);
                    nodes += 1;
                    // Keep the source lattice authoritative. Multiplication by
                    // 1e-7 and division by 1e7 can differ by one f64 ULP.
                    let lon = f64::from(node.decimicro_lon) / 1e7;
                    let lat = f64::from(node.decimicro_lat) / 1e7;
                    if !lon.is_finite() || !lat.is_finite() || !(-180. ..=180.).contains(&lon) || !(-90. ..=90.).contains(&lat) {
                        return Err("invalid node coordinates".into());
                    }
                    if nodes.checked_mul(RECORD as u64).ok_or("index overflow")? > MAX_INDEX { return Err("index limit exceeded".into()); }
                    let w = writer.as_mut().unwrap();
                    w.write_all(&id.to_le_bytes()).map_err(|e| e.to_string())?;
                    w.write_all(&node.decimicro_lon.to_le_bytes()).map_err(|e| e.to_string())?;
                    w.write_all(&node.decimicro_lat.to_le_bytes()).map_err(|e| e.to_string())?;
                    if relevant(&node.tags) && b[1] <= lon && lon <= b[3] && b[0] <= lat && lat <= b[2] {
                        emit(&mut out, json!({"type":"node","id":id,"lon":lon,"lat":lat,"tags":node.tags}), &mut candidates, &mut bytes)?;
                    }
                }
                OsmObj::Way(way) => {
                    if last_way_id.map_or(false, |previous| way.id.0 <= previous) {
                        return Err("duplicate or unsorted way ids".into());
                    }
                    last_way_id = Some(way.id.0);
                    if let Some(mut w) = writer.take() {
                        w.flush().map_err(|e| e.to_string())?;
                        drop(w);
                        let f = File::open(&index_file).map_err(|e| e.to_string())?;
                        if f.metadata().map_err(|e| e.to_string())?.len() > 0 {
                            // Read-only mapping remains valid while way references are resolved.
                            map = Some(unsafe { Mmap::map(&f) }.map_err(|e| e.to_string())?);
                        }
                    }
                    ways += 1;
                    if !relevant(&way.tags) { continue; }
                    if way.nodes.len() > MAX_REFS { return Err("way refs limit exceeded".into()); }
                    let mut coords = Vec::with_capacity(way.nodes.len());
                    for ref_id in &way.nodes {
                        let location = map.as_ref().and_then(|m| lookup(m, ref_id.0))
                            .ok_or_else(|| format!("missing node {} in relevant way {}", ref_id.0, way.id.0))?;
                        coords.push(location);
                    }
                    if overlaps(&coords, b) {
                        let refs: Vec<i64> = way.nodes.iter().map(|id| id.0).collect();
                        emit(&mut out, json!({"type":"way","id":way.id.0,"nodes":refs,"coordinates":coords,"tags":way.tags}), &mut candidates, &mut bytes)?;
                    }
                }
                OsmObj::Relation(_) => {} // Unsupported, as in the Python control.
            }
        }
        out.flush().map_err(|e| e.to_string())?;
        eprintln!("{}", json!({"stage":"complete","nodes":nodes,"ways":ways,"candidates":candidates,"candidate_bytes":bytes,"relations_unsupported":true}));
        Ok(())
    })();
    if result.is_err() && created_output { let _ = fs::remove_file(output); }
    let _ = fs::remove_dir_all(index_dir);
    result
}

fn run() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 9 || args[1] != "--pbf" || args[3] != "--bbox" || args[5] != "--output" || args[7] != "--index-dir" {
        return Err("usage: osm_rust_preselect --pbf INPUT --bbox south,west,north,east --output NEW_NDJSON --index-dir NEW_DIRECTORY".into());
    }
    scan(Path::new(&args[2]), bbox(&args[4])?, Path::new(&args[6]), Path::new(&args[8]))
}

fn main() {
    if let Err(e) = run() { eprintln!("preselection failed: {e}"); std::process::exit(1); }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sparse_index_and_overlap() {
        let path = std::env::temp_dir().join(format!("osm-rust-test-{}", std::process::id()));
        fs::write(&path, [14_000_000_001_i64.to_le_bytes().as_slice(), 300_000_000_i32.to_le_bytes().as_slice(), 600_000_000_i32.to_le_bytes().as_slice()].concat()).unwrap();
        let map = unsafe { Mmap::map(&File::open(&path).unwrap()) }.unwrap();
        assert_eq!(lookup(&map, 14_000_000_001), Some([30., 60.]));
        assert_eq!(lookup(&map, 5), None);
        assert!(overlaps(&[[29.9, 59.9], [30.1, 60.1]], [59.99, 29.99, 60.01, 30.01]));
        drop(map);
        fs::remove_file(path).unwrap();
    }
    #[test]
    fn invalid_bounds_and_limits() {
        assert!(bbox("NaN,1,2,3").is_err());
        assert!(bbox("1,1,1,2").is_err());
        let path = std::env::temp_dir().join(format!("osm-rust-limit-{}", std::process::id()));
        let mut output = BufWriter::new(File::create(&path).unwrap());
        let mut count = MAX_CANDIDATES;
        let mut bytes = 0;
        assert!(emit(&mut output, json!({}), &mut count, &mut bytes).is_err());
        drop(output);
        fs::remove_file(path).unwrap();
    }
}
