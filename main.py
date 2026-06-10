import sys
import os

# Путь проекта в sys.path — позволяет запускать скрипт напрямую
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Отключаем навязчивый FutureWarning от pandera при импорте blocksnet
os.environ['DISABLE_PANDERA_IMPORT_WARNING'] = 'True'

# Принудительно переключаем вывод на UTF-8 для корректного отображения кириллицы
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import geopandas as gpd
import pandas as pd
import numpy as np

# Применяем зеркало Overpass и снимаем системные прокси — ДО первого сетевого вызова
from src.utils.overpass_config import apply_overpass_config
apply_overpass_config()

from src.etl.osm_loader import OSMLoader
from src.etl.gis_loader import GISLoader
from src.generation.topological_generator import TopologicalGenerator
from src.generation.ucm_builder import UCMBuilder

# ---------------------------------------------------------------------------
# Константы путей (единое место для изменения)
# ---------------------------------------------------------------------------
PROCESSED_DIR   = "data/processed"
RAW_DIR         = "data/raw"

PATH_BOUNDARY           = f"{PROCESSED_DIR}/osm/boundary.geojson"
PATH_LANDUSE            = f"{PROCESSED_DIR}/osm/landuse.geojson"
PATH_AMENITIES          = f"{PROCESSED_DIR}/osm/amenities_buildings.geojson"
PATH_OKN_GIS            = f"{PROCESSED_DIR}/gis/okn.geojson"
PATH_OOPT_GIS           = f"{PROCESSED_DIR}/gis/oopt.geojson"
PATH_TOPO_BLOCKS        = f"{PROCESSED_DIR}/topological_blocks.geojson"
PATH_UCM_BLOCKS         = f"{PROCESSED_DIR}/ucm_blocks.geojson"
PATH_DRIVE_GRAPH        = f"{PROCESSED_DIR}/drive_graph_edges.geojson"
PATH_ACC_MATRIX         = f"{PROCESSED_DIR}/accessibility_matrix.parquet"
PATH_AHP_SCORES_CSV     = f"{PROCESSED_DIR}/ahp_block_scores.csv"
PATH_AHP_GEOJSON        = f"{PROCESSED_DIR}/ucm_blocks_with_attractiveness.geojson"
PATH_OPTIMIZED_GEOJSON  = f"{PROCESSED_DIR}/ucm_blocks_optimized.geojson"
PATH_AHP_CONSTANTS      = "configs/ahp_constants.json"


def _safe_export(gdf: gpd.GeoDataFrame, path: str) -> None:
    """Экспортирует GeoDataFrame в GeoJSON. При блокировке файла пишет рядом с временны́м суффиксом."""
    if gdf is None or getattr(gdf, "empty", True):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # GeoJSON ожидает EPSG:4326; приводим для удобства просмотра в ГИС
    try:
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
    except Exception:
        pass
    try:
        gdf.to_file(path, driver="GeoJSON")
    except PermissionError:
        # Windows/QGIS часто держит файл открытым: не падаем, а пишем рядом с суффиксом.
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        alt_path = f"{path}.locked-{ts}.geojson"
        print(f"⚠️  Не удалось перезаписать {path} (файл занят). Пишем в {alt_path}")
        gdf.to_file(alt_path, driver="GeoJSON")


def generate_ucm(
    region_names: list,
    output_path: str = PATH_UCM_BLOCKS
) -> None:
    """
    Главный пайплайн генерации UCM.

    :param region_names: Список названий регионов.
                         Например: [{"city": "Pushkin", "state": "Saint Petersburg"},
                                    "Gatchina, Leningrad Oblast, Russia"]
    :param output_path:  Путь для сохранения итогового слоя блоков.
    """
    import time
    start_time = time.time()
    print(f"=== Запуск сборки UCM для {region_names} ===")

    # ------------------------------------------------------------------
    # 1. Загрузка данных
    # ------------------------------------------------------------------
    osm = OSMLoader(locations=region_names)
    gis = GISLoader(data_dir=RAW_DIR)

    if os.path.exists(PATH_BOUNDARY):
        print(f"Загрузка границ из кэша: {PATH_BOUNDARY}")
        boundary_gdf = gpd.read_file(PATH_BOUNDARY)
    else:
        boundary_gdf = osm.get_boundary()
        _safe_export(boundary_gdf, PATH_BOUNDARY)

    if os.path.exists(PATH_LANDUSE):
        print(f"Загрузка землепользования из кэша: {PATH_LANDUSE}")
        land_use_gdf = gpd.read_file(PATH_LANDUSE)
    else:
        try:
            land_use_gdf = osm.get_land_use(boundary_poly=boundary_gdf)
            _safe_export(land_use_gdf, PATH_LANDUSE)
        except Exception as e:
            print(f"⚠️  Ошибка загрузки землепользования: {e}. Продолжаем с пустым слоем.")
            land_use_gdf = gpd.GeoDataFrame()

    if os.path.exists(PATH_AMENITIES):
        print(f"Загрузка POI/объектов из кэша: {PATH_AMENITIES}")
        amenities_gdf = gpd.read_file(PATH_AMENITIES)
    else:
        try:
            amenities_gdf = osm.get_amenities_and_buildings(boundary_poly=boundary_gdf)
            _safe_export(amenities_gdf, PATH_AMENITIES)
        except Exception as e:
            print(f"⚠️  Ошибка загрузки POI/объектов: {e}. Продолжаем с пустым слоем.")
            amenities_gdf = gpd.GeoDataFrame()

    okn_gdf = gis.load_cultural_heritage()
    _safe_export(okn_gdf, PATH_OKN_GIS)

    oopt_gdf = gis.load_protected_areas()
    _safe_export(oopt_gdf, PATH_OOPT_GIS)

    # ------------------------------------------------------------------
    # 2. Топологическая генерация блоков (BlocksNet)
    # ------------------------------------------------------------------
    if os.path.exists(PATH_TOPO_BLOCKS):
        print(f"Загрузка блоков из кэша: {PATH_TOPO_BLOCKS}")
        blocks_gdf = gpd.read_file(PATH_TOPO_BLOCKS)
    else:
        generator = TopologicalGenerator(boundary_gdf=boundary_gdf)
        blocks_gdf = generator.generate_blocks(min_area_m2=500.0)
        _safe_export(blocks_gdf, PATH_TOPO_BLOCKS)

    # ------------------------------------------------------------------
    # 3. Атрибутирование блоков (UCM)
    # ------------------------------------------------------------------
    builder = UCMBuilder(blocks_gdf=blocks_gdf)
    builder.attribute_land_use(landuse_gdf=land_use_gdf)

    if not land_use_gdf.empty:
        # Болота
        wetlands = land_use_gdf[land_use_gdf.get('natural') == 'wetland']
        builder.attribute_swampiness(wetlands_gdf=wetlands)

        # Водные объекты (полигоны и линии)
        water_tags = ['water', 'river', 'stream', 'canal', 'lake']
        is_water = pd.Series(False, index=land_use_gdf.index)
        for tag in ['natural', 'landuse', 'water', 'waterway']:
            if tag in land_use_gdf.columns:
                if tag in ('water', 'waterway'):
                    is_water |= land_use_gdf[tag].notna()
                else:
                    is_water |= land_use_gdf[tag].isin(water_tags)

        water_gdf = land_use_gdf[is_water]
        builder.attribute_water_density(water_gdf=water_gdf)

    builder.attribute_amenities(amenities_gdf=amenities_gdf)
    builder.attribute_cultural_heritage(okn_gdf=okn_gdf)
    builder.attribute_protected_areas(oopt_gdf=oopt_gdf)

    # ------------------------------------------------------------------
    # 4. Сохранение UCM
    # ------------------------------------------------------------------
    builder.export_to_geojson(filepath=output_path)
    print(f"=== Генерация UCM успешно завершена. Файл: {output_path} ===")

    # ------------------------------------------------------------------
    # 5. Матрица транспортной доступности
    # ------------------------------------------------------------------
    import iduedu
    import warnings

    print("\n=== Старт расчета матрицы транспортной доступности ===")
    
    if os.path.exists(PATH_ACC_MATRIX):
        print(f"Загрузка матрицы доступности из кэша: {PATH_ACC_MATRIX}")
        if os.path.exists(PATH_DRIVE_GRAPH):
            print(f"Дорожный граф уже существует: {PATH_DRIVE_GRAPH}")
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            boundary_poly_4326 = boundary_gdf.to_crs(epsg=4326).unary_union

            try:
                print("Построение графа дорожной сети (iduedu.get_drive_graph)...")
                intermodal_graph = iduedu.get_drive_graph(territory=boundary_poly_4326)
                graph_crs = intermodal_graph.graph.get('crs', 4326)

                print("Экспорт транспортного графа для дашборда...")
                import osmnx as ox
                edges = ox.graph_to_gdfs(intermodal_graph, nodes=False)
                _safe_export(edges, PATH_DRIVE_GRAPH)

                blocks_for_matrix = builder.get_ucm().copy()
                if not blocks_for_matrix.crs.is_projected:
                    blocks_for_matrix = blocks_for_matrix.to_crs(blocks_for_matrix.estimate_utm_crs())
                blocks_for_matrix['geometry'] = blocks_for_matrix.geometry.centroid
                blocks_for_matrix = blocks_for_matrix.to_crs(graph_crs)

                print("Расчет матрицы доступности (get_adj_matrix_gdf_to_gdf)...")
                acc_matrix = iduedu.get_adj_matrix_gdf_to_gdf(
                    gdf_from=blocks_for_matrix,
                    gdf_to=blocks_for_matrix,
                    nx_graph=intermodal_graph,
                    weight='time_min',
                    dtype=np.float32
                )
                acc_matrix.to_parquet(PATH_ACC_MATRIX)
                print(f"=== Матрица доступности успешно сохранена: {PATH_ACC_MATRIX} ===")

            except Exception as e:
                print(f"⚠️ Ошибка при построении графа или матрицы: {e}")
                print("Использование евклидова расстояния в качестве заглушки...")
                blocks_for_matrix = builder.get_ucm().copy()
                if not blocks_for_matrix.crs.is_projected:
                    blocks_for_matrix = blocks_for_matrix.to_crs(blocks_for_matrix.estimate_utm_crs())

                centroids = blocks_for_matrix.geometry.centroid
                n = len(centroids)
                dist_matrix = np.zeros((n, n), dtype=np.float32)
                for i in range(n):
                    for j in range(n):
                        # Приблизительное время в минутах (1 км ≈ 10 мин)
                        dist_matrix[i, j] = centroids.iloc[i].distance(centroids.iloc[j]) / 1000.0 * 10.0

                ids = blocks_for_matrix.index.astype(str)
                acc_matrix = pd.DataFrame(dist_matrix, index=ids, columns=ids)
                acc_matrix.to_parquet(PATH_ACC_MATRIX)
                print(f"=== Матрица (Евклид) сохранена: {PATH_ACC_MATRIX} ===")

    # ------------------------------------------------------------------
    # 6. Сценарное взвешивание AHP
    # ------------------------------------------------------------------
    from pathlib import Path
    from src.analysis.ahp import run_stage2_ahp

    print("\n=== Старт сценарного взвешивания (AHP) ===")
    run_stage2_ahp(
        blocks_path=Path(output_path),
        constants_path=Path(PATH_AHP_CONSTANTS),
        output_csv=Path(PATH_AHP_SCORES_CSV),
        output_geojson=Path(PATH_AHP_GEOJSON),
    )

    # ------------------------------------------------------------------
    # 7. Глобальная оптимизация (Simulated Annealing)
    # ------------------------------------------------------------------
    from src.analysis.optimizer import run_optimization

    print("\n=== Старт пространственной оптимизации (Simulated Annealing) ===")
    run_optimization(
        blocks_path=Path(PATH_AHP_GEOJSON),
        acc_matrix_path=Path(PATH_ACC_MATRIX),
        output_geojson=Path(PATH_OPTIMIZED_GEOJSON),
        max_iter=50000,
    )

    elapsed = time.time() - start_time
    print(f"=== Выполнение завершено. Общее время: {elapsed:.2f} сек. ({elapsed / 60:.2f} мин.) ===")


if __name__ == "__main__":
    import time
    script_start = time.time()

    # Пилотный полигон: Пушкин + Гатчина
    pilot_regions = [
        {"city": "Pushkin", "state": "Saint Petersburg"},
        "Gatchina, Leningrad Oblast, Russia",
    ]
    generate_ucm(region_names=pilot_regions)
