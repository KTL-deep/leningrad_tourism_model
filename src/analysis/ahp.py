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
    "События": "leisure_count",
    "Учреждения": "amenity_count",
    "Ремесла": "poi_count",
    "Транспорт": "poi_count",
    "Размещение": "building_count",
    "Питание": "amenity_count",
    "Соц. услуги": "amenity_count",
    "ООПТ": "oopt_count",
    "Леса": "area",
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
    if "subfactor_global_weights" not in constants:
        raise ValueError("No 'subfactor_global_weights' in constants JSON")
    return constants


def _validate_consistency(constants: dict) -> None:
    consistency = constants.get("consistency", {})
    main = consistency.get("main_criteria", {})
    if main and not main.get("is_consistent", False):
        raise ValueError("Main criteria matrix is not consistent")
    for group_name, metrics in consistency.get("subgroups", {}).items():
        if not metrics.get("is_consistent", False):
            raise ValueError(f"Subgroup matrix is not consistent: {group_name}")


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
    _validate_consistency(constants)

    weights = constants["subfactor_global_weights"]
    with open(blocks_path, encoding="utf-8") as f:
        blocks_geojson = json.load(f)

    features = blocks_geojson.get("features", [])
    properties = [feat.get("properties", {}) for feat in features]
    df_blocks = pd.DataFrame(properties)
    if "block_id" not in df_blocks.columns:
        df_blocks["block_id"] = range(len(df_blocks))
    df_blocks["block_id"] = pd.to_numeric(df_blocks["block_id"], errors="coerce").fillna(-1).astype(int)

    missing_cols = []
    score_df = pd.DataFrame({"block_id": df_blocks["block_id"]})

    for subfactor, weight in weights.items():
        src_col = SUBFACTOR_TO_COLUMN.get(subfactor)
        if src_col is None or src_col not in df_blocks.columns:
            raw = pd.Series(0.0, index=df_blocks.index)
            missing_cols.append((subfactor, src_col))
        else:
            raw = pd.to_numeric(df_blocks[src_col], errors="coerce").fillna(0.0)

        normalized = _minmax(raw)
        if subfactor in LIMITING_FACTORS:
            normalized = 1.0 - normalized

        score_df[f"raw__{subfactor}"] = raw
        score_df[f"norm__{subfactor}"] = normalized
        score_df[f"w__{subfactor}"] = normalized * float(weight)

    weighted_cols = [c for c in score_df.columns if c.startswith("w__")]
    score_df["attractiveness_score"] = score_df[weighted_cols].sum(axis=1)
    score_df["attractiveness_rank"] = score_df["attractiveness_score"].rank(
        method="dense", ascending=False
    ).astype(int)

    result_lookup = score_df.set_index("block_id")[["attractiveness_score", "attractiveness_rank"]]
    for feat in features:
        props = feat.setdefault("properties", {})
        block_id = int(pd.to_numeric(props.get("block_id", -1), errors="coerce"))
        if block_id in result_lookup.index:
            props["attractiveness_score"] = float(result_lookup.loc[block_id, "attractiveness_score"])
            props["attractiveness_rank"] = int(result_lookup.loc[block_id, "attractiveness_rank"])

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    score_df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    with open(output_geojson, "w", encoding="utf-8") as f:
        json.dump(blocks_geojson, f, ensure_ascii=False)

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
        description="Stage 2 AHP scoring for city blocks"
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
        help="Output CSV with raw/norm/weighted values",
    )
    parser.add_argument(
        "--output-geojson",
        default="data/processed/ucm_blocks_with_attractiveness.geojson",
        help="Output GeoJSON with attractiveness fields",
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
