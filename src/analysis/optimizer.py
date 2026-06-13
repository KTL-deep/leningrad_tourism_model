import argparse
import json
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Синергия между типами объектов ТОКТ
# Индексы: 0: Хаб, 1: Точка притяжения, 2: Маршрут
SYNERGY_MATRIX = np.array([
    [-0.5,  1.0,  0.5],  # 0: Опорные центры (Хабы)
    [ 1.0, -0.2,  0.8],  # 1: Локальные точки притяжения
    [ 0.5,  0.8, -0.5],  # 2: Линейные элементы (Маршруты)
])

# Базовая обслуживающая способность (человек на гектар)
BASE_CAPACITY_FACTORS = {
    "Опорные центры (Хабы)": 2000,
    "Локальные точки притяжения": 500,
    "Линейные элементы (Маршруты)": 100
}

class SimulatedAnnealingOptimizer:
    def __init__(
        self,
        s_ik_matrix: np.ndarray,
        acc_matrix: np.ndarray,
        land_use_types: list[str],
        areas_ha: np.ndarray,
        lambda_dist: float = 0.5,
        lambda_cap: float = 1.0,
        capacity_multiplier: float = 1.0
    ):
        """
        Инициализация оптимизатора.
        :param s_ik_matrix: Матрица пригодности (N_blocks x K_land_uses)
        :param acc_matrix: Матрица транспортной доступности (N_blocks x N_blocks)
        :param land_use_types: Список названий типов использования
        :param areas_ha: Площади блоков в гектарах
        :param lambda_dist: Вес пространственной синергии
        :param lambda_cap: Вес штрафа за превышение емкости (Carrying Capacity)
        :param capacity_multiplier: Множитель пиковой нагрузки
        """
        self.s_ik = s_ik_matrix
        self.acc_matrix = acc_matrix
        self.n_blocks, self.k_uses = self.s_ik.shape
        self.land_use_types = land_use_types
        self.areas_ha = areas_ha
        self.lambda_dist = lambda_dist
        self.lambda_cap = lambda_cap
        self.capacity_multiplier = capacity_multiplier

        self.dist_utility = 1.0 / (self.acc_matrix + 1.0)
        np.fill_diagonal(self.dist_utility, 0.0)

        # Расчет предельной экологической емкости (Placeholder для Carrying Capacity)
        # В реальности зависит от okn_count, forest_share и т.д.
        self.max_carrying_capacity = self.areas_ha * 1000.0 # 1000 чел/га как абсолютный предел

    def calculate_energy(self, state: np.ndarray) -> float:
        """Полный расчет целевой функции."""
        # 1. Полезность S_ik
        utility_s_ik = np.sum(self.s_ik[np.arange(self.n_blocks), state])
        
        # 2. Пространственная синергия
        utility_dist = 0.0
        for i in range(self.n_blocks):
            lu_i = state[i]
            synergies = SYNERGY_MATRIX[lu_i, state]
            utility_dist += np.sum(synergies * self.dist_utility[i, :])
        utility_dist /= 2.0
        
        # 3. Штраф за превышение Carrying Capacity
        current_loads = self.areas_ha * np.array([BASE_CAPACITY_FACTORS[self.land_use_types[i]] for i in state])
        overload = np.maximum(0, current_loads - self.max_carrying_capacity)
        penalty_cap = np.sum(overload) * 0.01 # Коэффициент штрафа
            
        return utility_s_ik + self.lambda_dist * utility_dist - self.lambda_cap * penalty_cap

    def _calculate_delta(self, state: np.ndarray, block_idx: int, new_lu: int) -> float:
        """Быстрый расчет изменения целевой функции."""
        old_lu = state[block_idx]
        
        # Изменение S_ik
        delta_s_ik = self.s_ik[block_idx, new_lu] - self.s_ik[block_idx, old_lu]
        
        # Изменение пространственной синергии
        old_synergy = SYNERGY_MATRIX[old_lu, state]
        new_synergy = SYNERGY_MATRIX[new_lu, state]
        delta_dist = np.sum(new_synergy * self.dist_utility[block_idx, :]) - np.sum(old_synergy * self.dist_utility[block_idx, :])
        
        # Изменение штрафа емкости
        old_load = self.areas_ha[block_idx] * BASE_CAPACITY_FACTORS[self.land_use_types[old_lu]]
        new_load = self.areas_ha[block_idx] * BASE_CAPACITY_FACTORS[self.land_use_types[new_lu]]
        
        old_overload = max(0, old_load - self.max_carrying_capacity[block_idx])
        new_overload = max(0, new_load - self.max_carrying_capacity[block_idx])
        delta_cap_penalty = (new_overload - old_overload) * 0.01
        
        return delta_s_ik + self.lambda_dist * delta_dist - self.lambda_cap * delta_cap_penalty

    def run(self, max_iter: int = 50000, temp_init: float = 10.0, temp_min: float = 0.01) -> tuple[np.ndarray, list[float]]:
        """Запуск алгоритма имитации отжига."""
        # Жадная инициализация (берем лучший S_ik для каждого блока)
        current_state = np.argmax(self.s_ik, axis=1)
        current_energy = self.calculate_energy(current_state)
        
        best_state = current_state.copy()
        best_energy = current_energy
        
        temp = temp_init
        alpha = (temp_min / temp_init) ** (1.0 / max_iter)
        
        history = [current_energy]
        
        logging.info(f"Starting SA optimization. Initial Energy: {current_energy:.2f}")
        
        for i in tqdm(range(max_iter), desc="Simulated Annealing"):
            block_idx = np.random.randint(self.n_blocks)
            new_lu = np.random.randint(self.k_uses)
            
            if new_lu == current_state[block_idx]:
                new_lu = (new_lu + 1) % self.k_uses
                
            delta = self._calculate_delta(current_state, block_idx, new_lu)
            
            # Максимизируем энергию
            if delta > 0 or np.random.rand() < np.exp(delta / temp):
                current_state[block_idx] = new_lu
                current_energy += delta
                
                if current_energy > best_energy:
                    best_state = current_state.copy()
                    best_energy = current_energy
                    
            temp *= alpha
            if i % (max_iter // 10) == 0:
                history.append(current_energy)
                
        logging.info(f"Optimization finished. Final Best Energy: {best_energy:.2f}")
        return best_state, history

def calculate_capacity(area_m2: float, lu_type: str, s_ik_score: float) -> float:
    """Вычисление Capacity (обслуживающей мощности) блока."""
    area_ha = area_m2 / 10000.0
    base_density = BASE_CAPACITY_FACTORS.get(lu_type, 100)
    # Мощность масштабируется оценкой пригодности (S_ik) от 0 до 1
    return area_ha * base_density * (s_ik_score + 0.1)

def run_optimization(
    blocks_path: Path,
    acc_matrix_path: Path,
    output_geojson: Path,
    scenarios: list[str] = None,
    max_iter: int = 50000
) -> None:
    if scenarios is None:
        scenarios = ["Экоцентризм", "Историко-центризм", "Инфраструктурный"]
        
    logging.info(f"Loading blocks from {blocks_path}")
    blocks_gdf = gpd.read_file(blocks_path)
    
    logging.info(f"Loading accessibility matrix from {acc_matrix_path}")
    acc_df = pd.read_parquet(acc_matrix_path)
    acc_matrix = acc_df.values
    
    if len(blocks_gdf) != acc_matrix.shape[0]:
        raise ValueError("Number of blocks does not match accessibility matrix size")
        
    # Получаем площадь в гектарах
    if not blocks_gdf.crs.is_projected:
        utm_crs = blocks_gdf.estimate_utm_crs()
        areas_ha = blocks_gdf.to_crs(utm_crs).area / 10000.0
    else:
        areas_ha = blocks_gdf.area / 10000.0
        
    for scenario in scenarios:
        logging.info(f"--- Processing scenario: {scenario} ---")
        scenario_slug = scenario.replace("-", "_").replace(" ", "_")
        prefix = f"S_ik_{scenario_slug}_"
        score_cols = [c for c in blocks_gdf.columns if c.startswith(prefix)]
        
        if not score_cols:
            logging.warning(f"No S_ik columns found for scenario '{scenario}', skipping.")
            continue
            
        # Типы из колонок
        land_use_types = [c.replace(prefix, "").replace("_", " ") for c in score_cols]
        # В нашем случае это: "Опорные центры (Хабы)", "Локальные точки притяжения", "Линейные элементы (Маршруты)"
        
        # Формируем матрицу S_ik
        s_ik_matrix = blocks_gdf[score_cols].values
        
        # Запускаем оптимизацию
        optimizer = SimulatedAnnealingOptimizer(
            s_ik_matrix=s_ik_matrix,
            acc_matrix=acc_matrix,
            land_use_types=land_use_types,
            areas_ha=areas_ha.values,
            lambda_dist=0.5,
            lambda_cap=1.0
        )
        
        best_state, _ = optimizer.run(max_iter=max_iter)
        
        # Сохраняем результаты
        target_lu_names = [land_use_types[i] for i in best_state]
        blocks_gdf[f"Target_LandUse_{scenario}"] = target_lu_names

        # Считаем Capacity.
        # Используем enumerate, чтобы позиционный индекс pos корректно
        # соответствовал best_state[pos] — не зависит от значений индекса DataFrame.
        capacities = []
        for pos, (_, row) in enumerate(blocks_gdf.iterrows()):
            lu_idx = int(best_state[pos])
            lu_type = land_use_types[lu_idx]
            s_ik_score = float(s_ik_matrix[pos, lu_idx])

            area_m2 = float(areas_ha.iloc[pos]) * 10000.0
            cap = calculate_capacity(area_m2, lu_type, s_ik_score)
            capacities.append(round(cap, 2))

        blocks_gdf[f"Capacity_{scenario}"] = capacities
    
    logging.info(f"Saving optimized blocks to {output_geojson}")
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    
    # Приводим к EPSG:4326 перед сохранением для совместимости со Streamlit
    if blocks_gdf.crs and blocks_gdf.crs.to_epsg() != 4326:
        blocks_gdf = blocks_gdf.to_crs(epsg=4326)
        
    blocks_gdf.to_file(output_geojson, driver="GeoJSON")
    logging.info("Optimization process complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 5: Global Land Use Optimization (Simulated Annealing)")
    parser.add_argument("--blocks", default="data/processed/ucm_blocks_with_attractiveness.geojson")
    parser.add_argument("--matrix", default="data/processed/accessibility_matrix.parquet")
    parser.add_argument("--output", default="data/processed/ucm_blocks_optimized.geojson")
    parser.add_argument("--scenarios", nargs="+", default=["Экоцентризм", "Историко-центризм", "Инфраструктурный"], help="Scenarios to optimize for")
    parser.add_argument("--iter", type=int, default=50000, help="Number of iterations for SA")
    
    args = parser.parse_args()
    
    run_optimization(
        blocks_path=Path(args.blocks),
        acc_matrix_path=Path(args.matrix),
        output_geojson=Path(args.output),
        scenarios=args.scenarios,
        max_iter=args.iter
    )