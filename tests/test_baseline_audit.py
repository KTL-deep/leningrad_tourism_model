"""Offline characterization of the current optimizer and UCM behavior (synthetic data)."""

import pytest

np = pytest.importorskip("numpy")
gpd = pytest.importorskip("geopandas")
geometry = pytest.importorskip("shapely.geometry")

from src.analysis.optimizer import SimulatedAnnealingOptimizer, calculate_capacity
from src.generation.ucm_builder import UCMBuilder


def _optimizer(times):
    # Synthetic two-block, three-use case, including a hub capacity penalty.
    return SimulatedAnnealingOptimizer(
        s_ik_matrix=np.array([[0.8, 0.2, 0.1], [0.3, 0.7, 0.4]]),
        acc_matrix=np.array(times, dtype=float),
        land_use_types=[
            "Опорные центры (Хабы)",
            "Локальные точки притяжения",
            "Линейные элементы (Маршруты)",
        ],
        areas_ha=np.array([1.0, 2.0]),
    )


def _full_delta(optimizer, state, block_idx, new_lu):
    changed = state.copy()
    changed[block_idx] = new_lu
    return optimizer.calculate_energy(changed) - optimizer.calculate_energy(state)


def test_symmetric_optimizer_delta_matches_full_energy():
    optimizer = _optimizer([[0, 3], [3, 0]])
    state = np.array([0, 1])

    assert optimizer._calculate_delta(state, 0, 2) == pytest.approx(
        _full_delta(optimizer, state, 0, 2)
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Directed travel times: delta omits the incoming edge's different weight",
)
def test_asymmetric_optimizer_delta_matches_full_energy():
    # Synthetic directed 2x2 travel times: 0->1 costs 1, 1->0 costs 3.
    optimizer = _optimizer([[0, 1], [3, 0]])
    state = np.array([0, 1])

    assert optimizer._calculate_delta(state, 0, 2) == pytest.approx(
        _full_delta(optimizer, state, 0, 2)
    )


@pytest.mark.xfail(
    strict=True,
    raises=ZeroDivisionError,
    reason="Progress sampling uses max_iter // 10 as the modulo divisor",
)
def test_optimizer_run_five_iterations_does_not_raise():
    optimizer = _optimizer([[0, 3], [3, 0]])
    previous_rng = np.random.get_state()
    try:
        np.random.seed(42)
        optimizer.run(max_iter=5)
    finally:
        np.random.set_state(previous_rng)


def test_capacity_changes_with_suitability_not_just_area_and_use():
    # Synthetic one-hectare block; this is a suitability-scaled output,
    # NOT a valid physical carrying-capacity estimate.
    area_m2 = 10_000
    use = "Локальные точки притяжения"

    low = calculate_capacity(area_m2, use, 0.2)
    high = calculate_capacity(area_m2, use, 0.8)

    assert low == pytest.approx(150.0)
    assert high == pytest.approx(450.0)
    assert high > low


def test_heritage_within_counts_interior_but_not_boundary():
    # Synthetic metric square and points; the edge point is not strictly within.
    block = gpd.GeoDataFrame(
        {"block_id": [7]},
        geometry=[geometry.box(0, 0, 10, 10)],
        crs="EPSG:3857",
    )

    interior = geometry.Point(5, 5)
    boundary = geometry.Point(0, 5)

    def count_for(points):
        heritage = gpd.GeoDataFrame(geometry=points, crs=block.crs)
        builder = UCMBuilder(block)
        builder.attribute_cultural_heritage(heritage)
        return int(builder.get_ucm().loc[0, "okn_count"])

    assert count_for([interior, boundary]) == 1
    assert count_for([boundary]) == 0
