"""Offline legacy smoke check; synthetic inputs are not empirical validation."""

import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("geopandas")
pytest.importorskip("pandas")
pytest.importorskip("tqdm")

from src.analysis.ahp import run_stage2_ahp
from src.analysis.optimizer import SimulatedAnnealingOptimizer


def test_synthetic_ahp_to_legacy_optimizer(tmp_path):
    """Exercise real AHP IO and annealing without network or production data."""
    scenario = "Synthetic"
    land_uses = [
        "Опорные центры (Хабы)",
        "Локальные точки притяжения",
        "Линейные элементы (Маршруты)",
    ]
    blocks_path = tmp_path / "blocks.geojson"
    constants_path = tmp_path / "constants.json"
    scores_path = tmp_path / "scores.csv"
    output_path = tmp_path / "scored.geojson"
    blocks_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [30, 59]},
             "properties": {"block_id": 10, "food_count": 1}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [30.1, 59.1]},
             "properties": {"block_id": 20, "food_count": 3}},
        ],
    }), encoding="utf-8")
    constants_path.write_text(json.dumps({
        "scenarios": {scenario: {name: {"Питание": 1.0} for name in land_uses}},
    }, ensure_ascii=False), encoding="utf-8")

    run_stage2_ahp(blocks_path, constants_path, scores_path, output_path)
    output = json.loads(output_path.read_text(encoding="utf-8"))
    columns = [f"S_ik_{scenario}_{name.replace(' ', '_')}" for name in land_uses]
    scores = np.array([
        [feature["properties"][column] for column in columns]
        for feature in output["features"]
    ])
    np.testing.assert_allclose(scores, [[0, 0, 0], [1, 1, 1]])
    assert scores_path.is_file()
    assert [f["properties"]["block_id"] for f in output["features"]] == [10, 20]

    optimizer = SimulatedAnnealingOptimizer(
        scores, np.array([[0.0, 10.0], [10.0, 0.0]]), land_uses,
        np.array([1.0, 1.0]),
    )
    # The legacy API uses global NumPy RNG; do not leak the test seed.
    previous_rng = np.random.get_state()
    try:
        np.random.seed(42)
        state, history = optimizer.run(max_iter=20)
    finally:
        np.random.set_state(previous_rng)
    assert state.shape == (2,)
    assert np.all((state >= 0) & (state < len(land_uses)))
    assert np.all(np.isfinite(history))
    assert np.isfinite(optimizer.calculate_energy(state))
