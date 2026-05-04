import argparse
import json
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Синергия между типами застройки (чем выше значение, тем выгоднее их размещать рядом)
# Индексы: 0: Парк, 1: Жилье, 2: Коммерция, 3: Хаб
SYNERGY_MATRIX = np.array([
    [-0.5,  1.0,  0.5,  0.1],  # 0: Парк/Рекреация
    [ 1.0, -0.5,  1.0,  0.2],  # 1: Жилая застройка
    [ 0.5,  1.0, -0.5,  1.0],  # 2: Коммерция/Услуги
    [ 0.1,  0.2,  1.0, -0.5],  # 3: Инфраструктурный Хаб
])

# Коэффициенты плотности для расчета Capacity (человек на гектар)
DENSITY_FACTORS = {
    "Парк/Рекреация": 100,
    "Жилая застройка": 300,
    "Коммерция/Услуги": 500,
    "Инфраструктурный Хаб": 1000
}

class SimulatedAnnealingOptimizer:
    def __init__(
        self,
        s_ik_matrix: np.ndarray,
        acc_matrix: np.ndarray,
        land_use_types: list[str],
        lambda_dist: float = 1.0
    ):
        """
        Инициализация оптимизатора.
        :param s_ik_matrix: Матрица пригодности (N_blocks x K_land_uses)
        :param acc_matrix: Матрица транспортной доступности (N_blocks x N_blocks)
        :param land_use_types: Список названий типов использования
        :param lambda_dist: Весовой коэффициент для функции пространственной синергии
        """
        self.s_ik = s_ik_matrix
        self.acc_matrix = acc_matrix
        self.n_blocks, self.k_uses = self.s_ik.shape
        self.land_use_types = land_use_types
        self.lambda_dist = lambda_dist
        
        # Предрассчитываем матрицу обратных расстояний (утилит)
        # Добавляем 1.0 к расстояниям, чтобы избежать деления на ноль 
        # (внутри самого блока тоже может быть польза, но мы штрафуем одинаковые типы в SYNERGY)
        self.dist_utility = 1.0 / (self.acc_matrix + 1.0)
        
        # Обнуляем диагональ, чтобы не учитывать синергию блока с самим собой
        np.fill_diagonal(self.dist_utility, 0.0)

    def calculate_energy(self, state: np.ndarray) -> float:
        """Полный расчет целевой функции для текущего состояния."""
        # 1. Полезность S_ik
        utility_s_ik = np.sum(self.s_ik[np.arange(self.n_blocks), state])
        
        # 2. Пространственная синергия
        utility_dist = 0.0
        for i in range(self.n_blocks):
            lu_i = state[i]
            synergies = SYNERGY_MATRIX[lu_i, state]
            utility_dist += np.sum(synergies * self.dist_utility[i, :])
            
        # Так как пары считаются дважды, делим на 2
        utility_dist /= 2.0
        
        return utility_s_ik + self.lambda_dist * utility_dist

    def _calculate_delta(self, state: np.ndarray, block_idx: int, new_lu: int) -> float:
        """Быстрый расчет изменения целевой функции при изменении одного блока."""
        old_lu = state[block_idx]
        
        # Изменение S_ik
        delta_s_ik = self.s_ik[block_idx, new_lu] - self.s_ik[block_idx, old_lu]
        
        # Изменение пространственной синергии
        old_synergy = SYNERGY_MATRIX[old_lu, state]
        new_synergy = SYNERGY_MATRIX[new_lu, state]
        
        # Исключаем влияние блока самого на себя (уже учтено обнулением диагонали dist_utility)
        old_dist_util = np.sum(old_synergy * self.dist_utility[block_idx, :])
        new_dist_util = np.sum(new_synergy * self.dist_utility[block_idx, :])
        
        delta_dist = new_dist_util - old_dist_util
        
        # Не делим на 2, потому что при изменении одного блока меняются связи (i, j) и (j, i), 
        # а матрица dist_utility симметрична (или почти симметрична). Учтем это сполна.
        return delta_s_ik + self.lambda_dist * delta_dist

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
    base_density = DENSITY_FACTORS.get(lu_type, 100)
    # Мощность масштабируется оценкой пригодности (S_ik) от 0 до 1
    return area_ha * base_density * (s_ik_score + 0.1)  # Базовая мощность как минимум 10%

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
        
    # Получаем площадь в метрической проекции (UTM) один раз
    if not blocks_gdf.crs.is_projected:
        utm_crs = blocks_gdf.estimate_utm_crs()
        areas_m2 = blocks_gdf.to_crs(utm_crs).area
    else:
        areas_m2 = blocks_gdf.area
        
    for scenario in scenarios:
        logging.info(f"--- Processing scenario: {scenario} ---")
        scenario_slug = scenario.replace("-", "_").replace(" ", "_")
        # Вытаскиваем все типы Land Use из колонок для заданного сценария
        prefix = f"S_ik_{scenario_slug}_"
        score_cols = [c for c in blocks_gdf.columns if c.startswith(prefix)]
        
        if not score_cols:
            logging.warning(f"No S_ik columns found for scenario '{scenario}', skipping.")
            continue
            
        land_use_types_slugs = [c.replace(prefix, "") for c in score_cols]
        # Восстанавливаем оригинальные названия
        land_use_types = ["Парк/Рекреация", "Жилая застройка", "Коммерция/Услуги", "Инфраструктурный Хаб"]
        
        # Формируем матрицу S_ik
        s_ik_matrix = blocks_gdf[score_cols].values
        
        # Запускаем оптимизацию
        optimizer = SimulatedAnnealingOptimizer(
            s_ik_matrix=s_ik_matrix,
            acc_matrix=acc_matrix,
            land_use_types=land_use_types,
            lambda_dist=0.5  # Настраиваемый вес синергии
        )
        
        best_state, _ = optimizer.run(max_iter=max_iter)
        
        # Сохраняем результаты для конкретного сценария
        target_lu_names = [land_use_types[i] for i in best_state]
        blocks_gdf[f"Target_LandUse_{scenario}"] = target_lu_names
        
        # Считаем Capacity
        capacities = []
        for idx, row in blocks_gdf.iterrows():
            lu_type = target_lu_names[idx]
            lu_idx = best_state[idx]
            s_ik_score = s_ik_matrix[idx, lu_idx]
            
            area_m2 = areas_m2.iloc[idx]
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