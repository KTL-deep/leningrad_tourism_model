import argparse
import json
import sys
from pathlib import Path

import pandas as pd

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Subfactor -> input column proxy from stage 1 exports.
# Missing columns are treated as zero and reported.
SUBFACTOR_TO_COLUMN = {
    "ОКН": "okn_count",
    "События": "poi_count",
    "Учреждения": "poi_count",
    "Ремесла": "poi_count",
    "Транспорт": "transport_count",
    "Размещение": "accommodation_count",
    "Питание": "food_count",
    "Соц. услуги": "poi_count",
    "ООПТ": "oopt_share",
    "Леса": "forest_share",
    "Красные виды": None,
    "Животный мир": None,
    "Плотность озер": None,
    "Плотность рек": None,
    "Температурный режим": None,
    "Снежный покров": None,
    "Осадки": None,
    "Ветер": None,
    "Достопримечательности": "poi_count",
    "Рельеф": None,
    "Заболоченность": None,
    "Заболевания": None,
    "Опасные процессы": None,
}

LIMITING_FACTORS = {"Заболоченность", "Заболевания", "Опасные процессы"}


def _load_constants(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        constants = json.load(f)
    if "scenarios" not in constants:
        raise ValueError("No 'scenarios' in constants JSON")
    return constants


def _minmax(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0.0)
    s_min = s.min()
    s_max = s.max()
    if s_max == s_min:
        return pd.Series(0.0, index=s.index)
    return (s - s_min) / (s_max - s_min)


def run_stage2_ahp(
    blocks_path: Path,
    constants_path: Path,
    output_csv: Path,
    output_geojson: Path,
) -> None:
    constants = _load_constants(constants_path)

    with open(blocks_path, encoding="utf-8") as f:
        blocks_geojson = json.load(f)

    features = blocks_geojson.get("features", [])
    properties = [feat.get("properties", {}) for feat in features]
    df_blocks = pd.DataFrame(properties)
    if "block_id" not in df_blocks.columns:
        df_blocks["block_id"] = range(len(df_blocks))
    df_blocks["block_id"] = pd.to_numeric(df_blocks["block_id"], errors="coerce").fillna(-1).astype(int)

    score_df = pd.DataFrame({"block_id": df_blocks["block_id"]})
    
    # Pre-calculate normalized values for all subfactors present in constants
    all_subfactors = set()
    for scen_data in constants["scenarios"].values():
        for lu_data in scen_data.values():
            all_subfactors.update(lu_data.keys())
            
    normalized_data = {}
    missing_cols = set()
    for subfactor in all_subfactors:
        src_col = SUBFACTOR_TO_COLUMN.get(subfactor)
        if src_col is None or src_col not in df_blocks.columns:
            raw = pd.Series(0.0, index=df_blocks.index)
            missing_cols.add((subfactor, src_col))
        else:
            raw = pd.to_numeric(df_blocks[src_col], errors="coerce").fillna(0.0)

        normalized = _minmax(raw)
        if subfactor in LIMITING_FACTORS:
            normalized = 1.0 - normalized
        normalized_data[subfactor] = normalized
        
    # Calculate scores for each scenario and land use type
    scenarios = constants["scenarios"]
    result_lookup = pd.DataFrame({"block_id": df_blocks["block_id"]}).set_index("block_id")
    
    for scenario_name, lu_dict in scenarios.items():
        scenario_slug = scenario_name.replace("-", "_").replace(" ", "_")
        for lu_type, weights in lu_dict.items():
            lu_slug = lu_type.replace("/", "_").replace(" ", "_")
            
            score_series = pd.Series(0.0, index=df_blocks.index)
            for subfactor, weight in weights.items():
                score_series += normalized_data[subfactor] * float(weight)
                
            col_name = f"S_ik_{scenario_slug}_{lu_slug}"
            score_df[col_name] = score_series
            
            # Save to lookup
            result_lookup[col_name] = score_series.values

    # Assign scores back to GeoJSON features
    for feat in features:
        props = feat.setdefault("properties", {})
        block_id = int(pd.to_numeric(props.get("block_id", -1), errors="coerce"))
        if block_id in result_lookup.index:
            for col in result_lookup.columns:
                props[col] = float(result_lookup.loc[block_id, col])

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    
    try:
        score_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    except Exception as e:
        alt_csv = output_csv.parent / f"{output_csv.stem}_locked_{ts}.csv"
        print(f"⚠️  Не удалось перезаписать {output_csv} ({e}). Пишем в {alt_csv}")
        score_df.to_csv(alt_csv, index=False, encoding="utf-8-sig")
        output_csv = alt_csv
        
    try:
        with open(output_geojson, "w", encoding="utf-8") as f:
            json.dump(blocks_geojson, f, ensure_ascii=False)
    except Exception as e:
        alt_json = output_geojson.parent / f"{output_geojson.stem}_locked_{ts}.geojson"
        print(f"⚠️  Не удалось перезаписать {output_geojson} ({e}). Пишем в {alt_json}")
        with open(alt_json, "w", encoding="utf-8") as f:
            json.dump(blocks_geojson, f, ensure_ascii=False)
        output_geojson = alt_json

    print(f"AHP stage 2 completed: {len(df_blocks)} blocks processed")
    print(f"Constants: {constants_path}")
    print(f"Score table: {output_csv}")
    print(f"GeoJSON with score: {output_geojson}")
    if missing_cols:
        print("Missing proxies (filled with 0):")
        for subfactor, src_col in missing_cols:
            print(f"  - {subfactor}: {src_col}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 2 AHP scoring for city blocks with scenarios and WLC"
    )
    parser.add_argument(
        "--blocks",
        default="data/processed/ucm_blocks.geojson",
        help="Path to stage-1 blocks GeoJSON",
    )
    parser.add_argument(
        "--constants",
        default="configs/ahp_constants.json",
        help="Path to canonical AHP constants JSON",
    )
    parser.add_argument(
        "--output-csv",
        default="data/processed/ahp_block_scores.csv",
        help="Output CSV with WLC scores",
    )
    parser.add_argument(
        "--output-geojson",
        default="data/processed/ucm_blocks_with_attractiveness.geojson",
        help="Output GeoJSON with S_ik score fields",
    )
    args = parser.parse_args()

    run_stage2_ahp(
        blocks_path=Path(args.blocks),
        constants_path=Path(args.constants),
        output_csv=Path(args.output_csv),
        output_geojson=Path(args.output_geojson),
    )


if __name__ == "__main__":
    main()
